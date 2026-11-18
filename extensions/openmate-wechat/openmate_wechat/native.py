"""The matewechat-native program: start it, ask it things, hear its events.

JSON lines both ways (see native/matewechat-native/src/main.rs). Three threads keep it from ever waiting on itself:
- the reader only sorts lines: an answer wakes its caller, a persist event goes to the persist worker, every other
  event goes in order onto `events` (the live link takes them from there);
- the persist worker seals a rotated sign-in (the backend's handler) and acks it; it never calls the program, so a
  persist that holds the account's lock cannot wait on a request that waits on that lock;
- stderr is read only to keep its pipe from filling up (and its last lines for a crash's message).

When the program exits, every caller still waiting gets NativeGone, and `events` gets None.
"""

from __future__ import annotations

import collections
import itertools
import json
import logging
import os
import queue
import subprocess
import threading
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("openmate.wechat.native")

PROTOCOL = 2  # matewechat-native's: hello must say it, or the program is not used
PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


class NativeError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


class NativeGone(NativeError):
    """The program exited (or could not be reached) while a request was out: whether it did it is unknown."""

    def __init__(self, message: str = "微信组件退出了"):
        super().__init__("gone", message)


def clean_env(base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The environment for the program: WeChat is reached directly, never through a proxy someone set for other things."""
    env = dict(os.environ if base is None else base)
    for k in PROXY_VARS:
        env.pop(k, None)
    return env


class Native:
    def __init__(self, path: str, on_persist: Callable[[Dict[str, Any]], bool], env: Optional[Dict[str, str]] = None):
        self.path = path
        self.on_persist = on_persist
        self.events: "queue.Queue[Optional[dict]]" = queue.Queue()
        self._env = clean_env(env)
        self._proc: Optional[subprocess.Popen] = None
        self._ids = itertools.count(1)
        self._pending: Dict[int, "queue.Queue[dict]"] = {}
        self._mu = threading.Lock()
        self._write_mu = threading.Lock()
        self._persist_q: "queue.Queue[Optional[dict]]" = queue.Queue()
        self._stderr_tail: "collections.deque[str]" = collections.deque(maxlen=20)
        self._gone = True
        self._subs: Dict[int, "queue.Queue[Optional[dict]]"] = {}  # a listen's own events, by its generation

    # ---------------------------------------------------------------- life

    def alive(self) -> bool:
        p = self._proc
        return p is not None and p.poll() is None and not self._gone

    def start(self) -> None:
        with self._mu:
            if self.alive():
                return
            self.events = queue.Queue()
            self._persist_q = queue.Queue()
            self._proc = subprocess.Popen(
                [self.path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self._env,
                close_fds=True,
            )
            self._gone = False
            proc = self._proc
        threading.Thread(target=self._read, args=(proc,), name="matewechat-read", daemon=True).start()
        threading.Thread(target=self._read_err, args=(proc,), name="matewechat-stderr", daemon=True).start()
        threading.Thread(target=self._persist_worker, args=(self._persist_q, proc), name="matewechat-persist",
                         daemon=True).start()
        try:
            hello = self.call("hello", timeout=10)
        except NativeError as e:
            self.close()
            raise NativeGone("微信组件没有回应：%s" % e.message)
        if hello.get("protocol") != PROTOCOL:
            self.close()
            raise NativeError("config", "微信组件的版本不对（协议 %s，需要 %d），请更新扩展或检查 native_path"
                              % (hello.get("protocol"), PROTOCOL))

    def close(self) -> None:
        with self._mu:
            proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()  # the program stops when its stdin closes
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    def subscribe(self, gen: int) -> "queue.Queue[Optional[dict]]":
        """The queue of one listen's events (made before the listen starts, so none is missed)."""
        q: "queue.Queue[Optional[dict]]" = queue.Queue()
        with self._mu:
            self._subs[gen] = q
            if self._gone:
                q.put(None)
        return q

    def unsubscribe(self, gen: int) -> None:
        with self._mu:
            self._subs.pop(gen, None)

    # ---------------------------------------------------------------- requests

    def call(self, op: str, timeout: float = 20.0, **params: Any) -> dict:
        """One request; its answer (without id/ok). NativeError for a refusal, NativeGone when the program went away
        or did not answer in time (it may have done it)."""
        if not self.alive():
            raise NativeGone("微信组件没有在运行")
        rid = next(self._ids)
        box: "queue.Queue[dict]" = queue.Queue(maxsize=1)
        with self._mu:
            self._pending[rid] = box
        try:
            self._write(dict(params, id=rid, op=op))
            try:
                ans = box.get(timeout=timeout)
            except queue.Empty:
                raise NativeGone("微信组件 %d 秒没有回应" % int(timeout))
        finally:
            with self._mu:
                self._pending.pop(rid, None)
        if ans.get("gone"):
            raise NativeGone(self._exit_reason())
        if not ans.get("ok"):
            e = ans.get("error") or {}
            raise NativeError(str(e.get("kind") or "error"), str(e.get("message") or "微信组件出错了"))
        return {k: v for k, v in ans.items() if k not in ("id", "ok")}

    def _write(self, msg: dict) -> None:
        line = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise NativeGone()
        with self._write_mu:
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
            except (OSError, ValueError):
                raise NativeGone()

    # ---------------------------------------------------------------- threads

    def _read(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        try:
            for raw in proc.stdout:
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(msg, dict):
                    continue
                if "event" not in msg:  # an answer (an event has "event"; a message event also has an "id")
                    with self._mu:
                        box = self._pending.get(msg["id"]) if isinstance(msg["id"], int) else None
                    if box is not None:
                        try:
                            box.put_nowait(msg)
                        except queue.Full:
                            pass
                    continue
                if msg.get("event") == "persist":
                    self._persist_q.put(msg)
                    continue
                with self._mu:
                    q = self._subs.get(msg.get("gen")) if isinstance(msg.get("gen"), int) else None
                if q is not None:
                    q.put(msg)  # only the listen it belongs to sees it
                # an event of a listen nobody hears any more (it ended, or never started here) is dropped
        except (OSError, ValueError):
            pass
        finally:
            self._went_away(proc)

    def _went_away(self, proc: subprocess.Popen) -> None:
        with self._mu:
            if proc is not self._proc:
                return
            self._gone = True
            pending = list(self._pending.values())
            subs = list(self._subs.values())
        for box in pending:
            try:
                box.put_nowait({"gone": True})
            except queue.Full:
                pass
        self._persist_q.put(None)
        self.events.put(None)
        for q in subs:
            q.put(None)
        try:
            code = proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            code = None
        log.warning("matewechat-native exited (%s)%s", code, (": " + self._stderr_tail[-1]) if self._stderr_tail else "")

    def _read_err(self, proc: subprocess.Popen) -> None:
        assert proc.stderr is not None
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", "replace").rstrip()[:500]
                if line:
                    self._stderr_tail.append(line)
                    log.info("%s", line)
        except (OSError, ValueError):
            pass

    def _persist_worker(self, q: "queue.Queue[Optional[dict]]", proc: subprocess.Popen) -> None:
        while True:
            msg = q.get()
            if msg is None:
                return
            ok = False
            try:
                ok = bool(self.on_persist(msg.get("state") or {}))
            except Exception:
                log.exception("sealing the rotated WeChat sign-in failed")
            if proc is not self._proc:
                return
            try:
                self._write({"op": "persist_ack", "seq": msg.get("seq"), "ok": ok})
            except NativeGone:
                return

    def _exit_reason(self) -> str:
        return "微信组件退出了" + ("：" + self._stderr_tail[-1] if self._stderr_tail else "")
