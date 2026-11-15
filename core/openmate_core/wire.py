"""Newline-delimited JSON-RPC over a pair of byte streams (the hub's stdin / stdout), as Persona's process protocol
speaks it (docs/extensions/extensions.md "Process extension protocol"):

    request   {"id": "h1", "method": "connector.status", "params": {…}}
    response  {"id": "h1", "result": {…}}   or   {"id": "h1", "error": {"message": "…", "code": "…"}}
    event     {"event": "connector.changed", "params": {…}}

Both sides send requests: the hub calls the program (initialize, connector.*), the program calls the hub
(connector.secrets.*, cli.call). The output stream carries nothing but protocol lines.
"""

from __future__ import annotations

import itertools
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("openmate.wire")

# Methods answered on the reader thread, in order: nothing after initialize is read before it is answered.
INLINE_METHODS = frozenset({"initialize", "manifest", "ping"})


class RPCError(Exception):
    """An error answer: message is one sentence for people (no secrets, no message bodies); code is the protocol's
    (not_initialized, unknown_method, no_capability, no_contribution, wrong_protocol, rate_limited…) or empty."""

    def __init__(self, message: str, code: str = "", data: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.data = data

    def to_wire(self) -> dict:
        out: Dict[str, Any] = {"message": self.message or "error"}
        if self.code:
            out["code"] = self.code
        if self.data is not None:
            out["data"] = self.data
        return out


class _Pending:
    __slots__ = ("event", "result", "error")

    def __init__(self):
        self.event = threading.Event()
        self.result: Any = None
        self.error: Optional[dict] = None


Handler = Callable[[str, Any], Any]


class Peer:
    """One side of the line protocol.

    handler(method, params) answers the other side's requests (raise RPCError for an error answer); on_event(name,
    params) hears its events; after(method, result) runs once a request's answer is written (initialize starts the
    program's own work there, since it may call back). Requests are answered on a pool of threads, except
    INLINE_METHODS, which are answered in order before anything else is read.
    """

    def __init__(self, rfile, wfile, handler: Handler, on_event: Optional[Callable[[str, Any], None]] = None,
                 after: Optional[Callable[[str, Any], None]] = None, workers: int = 16, prefix: str = "o"):
        self._rfile = rfile
        self._wfile = wfile
        self._handler = handler
        self._on_event = on_event
        self._after = after
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="openmate-rpc")
        self._wlock = threading.Lock()
        self._plock = threading.Lock()
        self._pending: Dict[str, _Pending] = {}
        self._ids = itertools.count(1)
        self._prefix = prefix
        self._closed = threading.Event()

    # ---- reading ----

    def serve(self) -> None:
        """Read until the other side closes the stream."""
        try:
            while True:
                raw = self._rfile.readline()
                if not raw:
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    log.warning("ignored a line that is not JSON (%d bytes)", len(line))
                    continue
                if isinstance(msg, dict):
                    self._dispatch(msg)
        except (OSError, ValueError) as e:  # a closed stream
            log.info("input closed: %s", type(e).__name__)
        finally:
            self._closed.set()
            with self._plock:
                pending, self._pending = self._pending, {}
            for p in pending.values():
                p.error = {"message": "连接已关闭", "code": "closed"}
                p.event.set()
            self._pool.shutdown(wait=False)

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def _dispatch(self, msg: dict) -> None:
        if "method" in msg:
            method = msg.get("method")
            if not isinstance(method, str):
                return
            if "id" not in msg:  # a notification: nothing to answer
                return
            rid = msg.get("id")
            params = msg.get("params")
            if method in INLINE_METHODS:
                self._answer(rid, method, params)
            else:
                try:
                    self._pool.submit(self._answer, rid, method, params)
                except RuntimeError:  # shutting down
                    pass
            return
        if "event" in msg:
            if self._on_event is not None:
                try:
                    self._on_event(str(msg.get("event")), msg.get("params"))
                except Exception:  # an event handler never stops the reader
                    log.exception("event handler failed")
            return
        rid = msg.get("id")
        if rid is None:
            return
        with self._plock:
            p = self._pending.pop(str(rid), None)
        if p is None:
            return
        if "error" in msg and msg["error"] is not None:
            err = msg["error"]
            p.error = err if isinstance(err, dict) else {"message": str(err)}
        else:
            p.result = msg.get("result")
        p.event.set()

    def _answer(self, rid: Any, method: str, params: Any) -> None:
        try:
            result = self._handler(method, params)
            reply = {"id": rid, "result": result if result is not None else {}}
            ok = True
        except RPCError as e:
            reply = {"id": rid, "error": e.to_wire()}
            ok = False
        except Exception:  # never let a bug take the program down; say nothing private
            log.exception("%s failed", method)
            reply = {"id": rid, "error": {"message": "内部错误，详情见扩展日志"}}
            ok = False
        self._write(reply)
        if ok and self._after is not None:
            try:
                self._after(method, reply["result"])
            except Exception:
                log.exception("after %s failed", method)

    # ---- writing ----

    def _write(self, obj: dict) -> None:
        data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._wlock:
            try:
                self._wfile.write(data)
                self._wfile.flush()
            except (OSError, ValueError):
                self._closed.set()

    def notify(self, event: str, params: dict) -> None:
        """Send a one-way event."""
        if not self._closed.is_set():
            self._write({"event": event, "params": params})

    def call(self, method: str, params: dict, timeout: float = 30.0) -> Any:
        """Call the other side and wait for its answer. Never from the reader thread (INLINE_METHODS)."""
        if self._closed.is_set():
            raise RPCError("连接已关闭", "closed")
        rid = "%s%d" % (self._prefix, next(self._ids))
        p = _Pending()
        with self._plock:
            self._pending[rid] = p
        self._write({"id": rid, "method": method, "params": params})
        if not p.event.wait(timeout):
            with self._plock:
                self._pending.pop(rid, None)
            raise RPCError("%s 没有在 %d 秒内回答" % (method, int(timeout)), "timeout")
        if p.error is not None:
            raise RPCError(str(p.error.get("message") or "error"), str(p.error.get("code") or ""), p.error.get("data"))
        return p.result
