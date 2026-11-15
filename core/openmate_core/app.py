"""The connector program: Persona's process protocol (initialize, ping, manifest) and the connector namespace
(docs/protocol/connector-protocol.md) over one platform Backend.

Threads: the reader (wire.Peer) answers initialize in order and everything else on a pool; the link thread runs
Backend.listen with the reconnect policy; a catch-up thread reads history after each link up; the debouncer sends
connector.changed; the /agent runner has its own (agent.py). The store is the one shared state.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from . import tools as toolmod
from . import views
from .agent import AgentRunner
from .backend import Account, BackendError, Chat, CredentialsExpired, Message, SendRejected
from .events import Changed
from .hub import HubAPI
from .platform import Platform, Timing
from .store import Store
from .textutil import rfc3339, zone
from .wire import Peer, RPCError

log = logging.getLogger("openmate")

PROTOCOL = 1
THREAD_MOST = 50
THREAD_MEDIA_MOST = 4 << 20
ARCHIVE_MEDIA_MOST = 20 << 20
ARCHIVE_ROWS_MOST = 2000
STARTUP_WAIT = 20.0  # a call that arrives while the program starts (store, backend, saved sign-in) waits this long

# Connector methods of capabilities this connector does not declare: answered no_capability (unknown ones are
# unknown_method).
UNDECLARED = frozenset({
    "catalog.list", "catalog.call", "recall", "recall.can", "recall.do", "inbox.settle", "history.chats",
    "archive.history", "history.anchors", "items.list", "items.doc_title", "snapshot.get", "group.tool",
    "group.append", "group.fallback", "options.get", "options.set", "machines.online", "auth.get", "auth.set",
})


def load_manifest(path: str) -> dict:
    with open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


def packaged_manifest(m: dict) -> dict:
    """The manifest as packages, indexes and the manifest method carry it: without the source-only tags and icon."""
    return {k: v for k, v in m.items() if k not in ("tags", "icon")}


def config_defaults(m: dict) -> dict:
    props = ((m.get("configuration") or {}).get("properties") or {})
    return {k: v["default"] for k, v in props.items() if isinstance(v, dict) and "default" in v}


class ConnectorApp:
    def __init__(self, platform: Platform, timing: Optional[Timing] = None, backend_factory=None, env=None):
        self.platform = platform
        self.cid = platform.connector_id
        self.timing = timing or Timing()
        self.env = env if env is not None else os.environ
        self._make_backend = backend_factory or platform.make_backend
        self.manifest = load_manifest(platform.manifest_path)
        self.peer: Optional[Peer] = None
        self.lock = threading.RLock()
        self.initialized = False
        self._started = False
        self.config: Dict[str, Any] = {}
        self.hub = HubAPI("", "")
        self.tz = None
        self.data_dir = ""
        self.store: Optional[Store] = None
        self.backend = None
        self.setup_error = ""
        self.setup_kind = ""  # the BackendError kind make_backend raised: config is not_configured, others error
        self.creds = "loading"  # loading | none | present | error
        self.creds_error = ""
        self._credentials: Optional[dict] = None
        self.account: Optional[Account] = None
        self.link = "idle"  # idle | connecting | up | down | expired | error
        self.link_reason = ""
        self.last_sync = 0.0
        self.last_event = 0.0
        self._gen = 0
        self._link_stop = threading.Event()
        self._login_lock = threading.Lock()
        self._login: Optional[dict] = None
        self._catchup_pending = 0  # the link generation a catch-up was asked for (0: none)
        self._catchup_running = False
        self._catchup_done = threading.Event()
        self._link_up = threading.Event()
        self.ready = threading.Event()
        self.closing = threading.Event()
        self._inflight: set = set()
        self._names: Dict[str, str] = {}
        self.changed: Optional[Changed] = None
        self.agent: Optional[AgentRunner] = None
        self._methods = {
            "status": self.m_status,
            "tools.list": lambda p: {"tools": toolmod.read_tools(self)},
            "tools.call": lambda p: toolmod.call_tool(self, p),
            "actions.list": lambda p: {"tools": toolmod.write_tools(self)},
            "actions.describe": lambda p: toolmod.describe(self, p),
            "actions.do": lambda p: toolmod.do(self, p),
            "inbox.collect": self.m_inbox_collect,
            "inbox.reply": self.m_inbox_reply,
            "thread.page": self.m_thread_page,
            "thread.media": lambda p: self._media(p, THREAD_MEDIA_MOST),
            "archive.changes": self.m_archive_changes,
            "archive.media": lambda p: self._media(p, ARCHIVE_MEDIA_MOST),
            "login.step": self.m_login_step,
            "logout": self.m_logout,
        }

    # ------------------------------------------------------------------ the protocol

    def attach(self, peer: Peer) -> None:
        self.peer = peer

    def handle(self, method: str, params: Any) -> Any:
        params = params if isinstance(params, dict) else {}
        if method == "initialize":
            return self._initialize(params)
        if method == "manifest":
            return packaged_manifest(self.manifest)
        if not self.initialized:
            raise RPCError("还没有 initialize", "not_initialized")
        if method == "ping":
            return {}
        if method.startswith("connector."):
            rest = method[len("connector."):]
            contribution = params.get("contribution")
            if contribution and contribution != self.cid:
                raise RPCError("没有连接器 %s" % contribution, "no_contribution")
            fn = self._methods.get(rest)
            if fn is not None:
                if rest != "status" and not self.ready.wait(STARTUP_WAIT):
                    raise RPCError("扩展还在启动，请稍后再试")
                return fn(params)
            if rest in UNDECLARED:
                raise RPCError("连接器 %s 没有声明这项能力：%s" % (self.cid, rest), "no_capability")
        raise RPCError("未知方法 %s" % method, "unknown_method")

    def on_event(self, name: str, params: Any) -> None:
        """Hub events: none subscribed; ignored."""

    def after(self, method: str, result: Any) -> None:
        if method == "initialize" and not self._started:
            self._started = True
            threading.Thread(target=self._startup, name="openmate-startup", daemon=True).start()

    def _initialize(self, params: dict) -> dict:
        if params.get("protocol") != PROTOCOL:
            raise RPCError("只支持扩展协议 %d" % PROTOCOL, "wrong_protocol")
        with self.lock:
            if not self.initialized:
                ext = params.get("extension") or params.get("plugin") or {}
                hub = params.get("hub") or {}
                config = params.get("config")
                self.config = dict(config_defaults(self.manifest))
                if isinstance(config, dict):
                    self.config.update({k: v for k, v in config.items() if v is not None})
                self.data_dir = (ext.get("data_dir") or self.env.get("PERSONA_EXTENSION_DATA")
                                 or self.env.get("PERSONA_PLUGIN_DATA")
                                 or os.path.join(os.path.expanduser("~"), ".openmate", self.manifest["id"]))
                self.hub = HubAPI(hub.get("url") or self.env.get("PERSONA_HUB_URL", ""),
                                  hub.get("token") or self.env.get("PERSONA_HUB_TOKEN", ""))
                self.tz = zone(hub.get("timezone") or "")
                self.initialized = True
        out = {"protocol": PROTOCOL, "id": self.manifest["id"], "version": self.manifest["version"]}
        connectors = (self.manifest.get("contributes") or {}).get("connectors")
        if connectors:
            out["contributes"] = {"connectors": connectors}
        return out

    # ------------------------------------------------------------------ startup and shutdown

    def _startup(self) -> None:
        try:
            self.store = Store(os.path.join(self.data_dir, "openmate.db"))
            cut = self.store.sends_interrupted()
            if cut:
                log.warning("%d send(s) were cut off by a restart: marked uncertain", cut)
            self.store.sends_prune(time.time() - 30 * 86400)
            self.changed = Changed(self._emit_changed, self.timing.debounce)
            try:
                self.backend = self._make_backend(self.config, self.data_dir)
                self.backend.on_credentials = self._credentials_rotated
            except BackendError as e:
                self.setup_error = e.message
                self.setup_kind = e.kind
                log.warning("backend not available: %s", e.kind)
            self.agent = AgentRunner(self)
            creds = self._load_credentials()
            if creds is not None:
                self._credentials = creds
                self.creds = "present"
                name = self.store.meta_get("account_name")
                if name:
                    self.account = Account(self.store.meta_get("account_id"), name)
                if self.backend is not None:
                    self._start_link(restore=True)
            self.agent.start()
        except Exception:
            log.exception("startup failed")
            if not self.setup_error:
                self.setup_error = "扩展启动失败，详情见扩展日志"
        finally:
            self.ready.set()

    def shutdown(self) -> None:
        self.closing.set()
        with self.lock:
            self._gen += 1
            self._link_stop.set()
        if self.agent is not None:
            self.agent.stop()
        if self.changed is not None:
            self.changed.stop()
        if self.backend is not None:
            try:
                self.backend.close()
            except Exception:
                pass
        if self.store is not None:
            self.store.close()

    # ------------------------------------------------------------------ calls to the hub over stdio

    def hub_call(self, method: str, params: dict, timeout: float = 30.0) -> Any:
        if self.peer is None:
            raise RPCError("没有和 hub 的连接", "closed")
        return self.peer.call(method, params, timeout)

    def _load_credentials(self) -> Optional[dict]:
        for attempt in range(3):
            try:
                res = self.hub_call("connector.secrets.load", {"contribution": self.cid}) or {}
                data = res.get("data") if isinstance(res, dict) else None
                if not data:
                    self.creds = "none"
                    return None
                return json.loads(base64.b64decode(data).decode("utf-8"))
            except (ValueError, TypeError):
                log.warning("stored credentials are unreadable: treated as signed out")
                self.creds = "none"
                return None
            except RPCError as e:
                log.warning("secrets.load failed (%s), attempt %d", e.code or "error", attempt + 1)
                time.sleep(self.timing.secrets_retry)
        self.creds = "error"
        self.creds_error = "读取保存的登录信息失败，请重启扩展"
        return None

    def _save_credentials(self, creds: dict) -> None:
        data = base64.b64encode(json.dumps(creds, ensure_ascii=False).encode("utf-8")).decode("ascii")
        self.hub_call("connector.secrets.save", {"contribution": self.cid, "data": data})

    def _credentials_rotated(self, creds: dict) -> None:
        """The adapter's session was refreshed: seal the new credentials (in the background)."""
        def run():
            try:
                self._save_credentials(creds)
                self._credentials = creds
            except RPCError as e:
                log.warning("saving refreshed credentials failed (%s)", e.code or "error")

        threading.Thread(target=run, name="openmate-creds", daemon=True).start()

    def _emit_changed(self, scope: str) -> None:
        if self.peer is not None:
            self.peer.notify("connector.changed", {"contribution": self.cid, "scope": scope})

    def mark_changed(self, *scopes: str) -> None:
        if self.changed is not None:
            for s in scopes:
                self.changed.mark(s)

    # ------------------------------------------------------------------ the live link

    @property
    def signed_in(self) -> bool:
        return self.creds == "present" and self.backend is not None

    def _start_link(self, restore: bool) -> None:
        with self.lock:
            self._gen += 1
            gen = self._gen
            self._link_stop.set()
            stop = threading.Event()
            self._link_stop = stop
            self.link, self.link_reason = "connecting", ""
            self._link_up.clear()
        threading.Thread(target=self._link_loop, args=(gen, stop, restore), name="openmate-link", daemon=True).start()

    def _set_link(self, gen: int, state: str, reason: str = "") -> None:
        with self.lock:
            if gen != self._gen:
                return
            changed = state != self.link
            self.link, self.link_reason = state, reason
            if state == "up":
                self._link_up.set()
            else:
                self._link_up.clear()
        if changed:
            log.info("link %s", state)

    def _link_loop(self, gen: int, stop: threading.Event, restore: bool) -> None:
        t = self.timing
        backoff = t.backoff_min
        while not stop.is_set() and gen == self._gen:
            kind, reason = "down", "连接断开了"
            if restore:
                try:
                    acct = self.backend.restore(dict(self._credentials or {}))
                    self._remember_account(acct)
                    restore = False
                except CredentialsExpired as e:
                    self._set_link(gen, "expired", e.message)
                    return
                except BackendError as e:
                    kind = "error" if e.kind in ("proxy", "config") else "down"
                    self._set_link(gen, kind, e.message)
                    stop.wait(backoff)
                    backoff = min(backoff * 2, t.backoff_max)
                    continue
                except Exception:
                    log.exception("restore failed")
                    self._set_link(gen, "down", "恢复登录时出错")
                    stop.wait(backoff)
                    backoff = min(backoff * 2, t.backoff_max)
                    continue
            self._set_link(gen, "connecting")
            up_at = [0.0]

            def on_link(state: str, why: str = "") -> None:
                if gen != self._gen:
                    return
                if state == "up":
                    up_at[0] = time.monotonic()
                    self._set_link(gen, "up")
                    self._request_catch_up(gen)
                elif state == "expired":
                    self._set_link(gen, "expired", why or CredentialsExpired().message)
                else:
                    self._set_link(gen, "down", why or "连接断开了")

            try:
                self.backend.listen(lambda m: self._on_live(gen, m), on_link, stop)
            except CredentialsExpired as e:
                self._set_link(gen, "expired", e.message)
                return
            except BackendError as e:
                kind, reason = ("error" if e.kind in ("proxy", "config") else "down"), e.message
            except Exception:
                log.exception("listen failed")
                reason = "连接出错了"
            if stop.is_set() or gen != self._gen:
                return
            if up_at[0] and time.monotonic() - up_at[0] >= t.stable_after:
                backoff = t.backoff_min  # it was stable: start over
            with self.lock:
                expired = self.link == "expired" and gen == self._gen
            if expired:
                return
            self._set_link(gen, kind, reason)
            stop.wait(backoff)
            backoff = min(backoff * 2, t.backoff_max)

    def _remember_account(self, acct: Account) -> None:
        self.account = acct
        if self.store is not None:
            if acct.id:
                self.store.meta_set("account_id", acct.id)
            if acct.name:
                self.store.meta_set("account_name", acct.name)

    def _on_live(self, gen: int, msg: Message) -> None:
        if gen != self._gen:
            return
        self.last_event = time.time()
        try:
            self.ingest([msg], live=True)
        except Exception:
            if not self.closing.is_set():
                log.exception("a live message could not be stored")

    def _request_catch_up(self, gen: int) -> None:
        """Read history after a link up; one asked for while one runs is run after it (a second outage's messages)."""
        with self.lock:
            self._catchup_pending = gen
            if self._catchup_running:
                return
            self._catchup_running = True
        threading.Thread(target=self._catch_up_loop, name="openmate-catchup", daemon=True).start()

    def _catch_up_loop(self) -> None:
        while True:
            with self.lock:
                gen, self._catchup_pending = self._catchup_pending, 0
                if not gen:
                    self._catchup_running = False
                    self._catchup_done.set()
                    return
            self._catch_up(gen)

    def _catch_up(self, gen: int) -> None:
        """After the link is really up: the one-time initial history, then recent chats again (what came while down)."""
        try:
            t = self.timing
            done_now = set()
            if self.store.meta_get("initial_history_done") != "1":
                n = _int(self.config.get("history_chats"), 20, 0, 50)
                if n > 0:
                    chats = self.backend.recent_chats(n)
                    for c in chats[:n]:
                        if gen != self._gen or self.link != "up":
                            return
                        self.store.upsert_chat(c)
                        self.ingest(self.backend.history(c.id, None, t.initial_messages), live=False)
                        done_now.add(c.id)
                self.store.meta_set("initial_history_done", "1")
            for c in self.store.chats_since(time.time() - t.inbox_window, t.catchup_chats):
                if gen != self._gen or self.link != "up":
                    return
                if c["id"] in done_now:
                    continue
                self.ingest(self.backend.history(c["id"], None, t.catchup_messages), live=False)
            self.last_sync = time.time()
            self.mark_changed("inbox", "chats")
        except BackendError as e:
            log.warning("history catch-up stopped: %s", e.kind)
        except Exception:
            if not self.closing.is_set():
                log.exception("history catch-up failed")

    # ------------------------------------------------------------------ storing messages

    def ingest(self, msgs: List[Message], live: bool) -> List[dict]:
        if not msgs or self.store is None:
            return []
        for chat_id in {m.chat_id for m in msgs}:
            known = self.store.get_chat(chat_id)
            if known is None or not known.get("name"):
                try:
                    self.store.upsert_chat(self.backend.chat_info(chat_id))
                except BackendError:
                    pass
                except Exception:
                    log.exception("chat_info failed")
        me = self.account
        for m in msgs:
            if m.mine:
                if not m.sender_name and me is not None:
                    m.sender_name = me.name
                continue
            if m.sender_id:
                if m.sender_name:
                    if self._names.get(m.sender_id) != m.sender_name:
                        self._names[m.sender_id] = m.sender_name
                        self.store.put_name(m.sender_id, m.sender_name)
                else:
                    m.sender_name = self._names.get(m.sender_id) or self.store.name(m.sender_id, m.chat_id)
                    if not m.sender_name and self.backend is not None:
                        try:
                            m.sender_name = self.backend.user_name(m.sender_id, m.chat_id) or ""
                        except Exception:
                            m.sender_name = ""
                        if m.sender_name:
                            self._names[m.sender_id] = m.sender_name
                            self.store.put_name(m.sender_id, m.sender_name)
        new = self.store.add_messages(msgs, from_history=not live)
        if new:
            self.mark_changed("inbox", "chats")
            if live and self.agent is not None:
                for row in new:
                    try:
                        self.agent.on_message(row)
                    except Exception:
                        log.exception("/agent could not look at a message")
        return new

    def me_name(self) -> str:
        return self.account.name if self.account and self.account.name else "我"

    # ------------------------------------------------------------------ status

    def m_status(self, params: dict) -> dict:
        pname = self.platform.name
        out: Dict[str, Any] = {}
        if self.account and self.account.name:
            out["account"] = self.account.name
        if self.last_sync:
            out["last_sync"] = rfc3339(self.last_sync)
        if self.last_event:
            out["last_event"] = rfc3339(self.last_event)
        if self.setup_error:
            if self.setup_kind in ("", "config"):
                out.update(state="not_configured", error=self.setup_error,
                           guide=["在 设置 › 扩展 › %s 里把设置填好，扩展会自动重启" % self.manifest.get("name", "")])
            else:
                out.update(state="error", error=self.setup_error)
            return out
        if not self.ready.is_set() or self.creds == "loading":
            out.update(state="error", error="正在启动，读取登录信息…")
            return out
        if self.creds == "error":
            out.update(state="error", error=self.creds_error)
            return out
        if self.creds == "none":
            out.pop("account", None)
            out.update(state="not_signed_in", error="还没有登录%s" % pname,
                       guide=["在连接器设置里点“登录”，用%s手机 App 扫码" % pname])
            return out
        link, reason = self.link, self.link_reason
        if link == "up":
            out["state"] = "ok"
        elif link == "expired":
            out.update(state="error", error=reason or CredentialsExpired().message,
                       guide=["先退出登录，再点“登录”重新扫码"])
        elif link == "error":
            out.update(state="error", error=reason or "连接%s出错了" % pname)
        elif link == "down":
            out.update(state="error", error="连不上%s：%s，正在自动重连" % (pname, reason or "连接断开了"))
        else:
            out.update(state="error", error="正在连接%s…" % pname)
        return out

    # ------------------------------------------------------------------ sign-in

    def _qr_prompt(self, url: str, hint: str, error: str = "") -> dict:
        out = {"step": "qr", "kind": "qr", "url": url, "interval": 2, "hint": hint}
        if error:
            out["error"] = error
        return out

    def m_login_step(self, params: dict) -> dict:
        if self.backend is None or self.store is None:
            raise RPCError(self.setup_error or "扩展还没准备好，请稍后再试")
        step = params.get("step") or ""
        p = self.platform
        with self._login_lock:
            if step and self._login is None and self.creds == "present" and self.link == "up":
                # a poll that arrives after the sign-in finished: still done (an empty step starts over)
                return {"done": True, "account": self.me_name()}
            if not step or self._login is None:
                try:
                    qr = self.backend.login_start()
                except BackendError as e:
                    raise RPCError("没能开始登录：%s" % e.message)
                self._login = {"state": qr.state, "url": qr.url}
                return self._qr_prompt(qr.url, p.login_hint)
            flow = self._login
            try:
                status = self.backend.login_poll(flow["state"])
            except BackendError as e:
                return self._qr_prompt(flow["url"], p.login_hint, "暂时查不到扫码结果：%s" % e.message)
            if status == "scanned":
                return self._qr_prompt(flow["url"], p.login_hint_scanned)
            if status == "expired":
                try:
                    qr = self.backend.login_start()
                except BackendError as e:
                    raise RPCError("二维码过期了，换新的时出错：%s" % e.message)
                self._login = {"state": qr.state, "url": qr.url}
                return self._qr_prompt(qr.url, p.login_hint_expired)
            if status != "confirmed":
                return self._qr_prompt(flow["url"], p.login_hint)
            try:
                done = self.backend.login_finish(flow["state"])
            except BackendError as e:
                self._login = None
                raise RPCError("登录没有完成：%s" % e.message)
            try:
                self._save_credentials(done.credentials)
            except RPCError:
                self._login = None
                raise RPCError("登录成功了，但保存登录信息失败，请重新登录")
            self._login = None
            acct = done.account
            prev = self.store.meta_get("account_id")
            if prev and acct.id and prev != acct.id:
                self.store.wipe_account_data()  # another account: forget the previous one's chats
            self._credentials = done.credentials
            self.creds = "present"
            self._remember_account(acct)
            self._catchup_done.clear()
            self._start_link(restore=False)
            # Report done once the link is really up and the first history is in (bounded); polls meanwhile wait.
            deadline = time.monotonic() + self.timing.login_wait
            if self._link_up.wait(max(0.0, deadline - time.monotonic())):
                self._catchup_done.wait(max(0.0, deadline - time.monotonic()))
        self.mark_changed("inbox", "chats")
        return {"done": True, "account": acct.name or acct.id}

    def m_logout(self, params: dict) -> dict:
        with self.lock:
            self._gen += 1
            self._link_stop.set()
            self.link, self.link_reason = "idle", ""
            self._link_up.clear()
        with self._login_lock:
            self._login = None
        if self.backend is not None:
            try:
                self.backend.logout()
            except Exception:
                log.exception("backend logout failed")
        try:
            self.hub_call("connector.secrets.delete", {"contribution": self.cid})
        except RPCError as e:
            raise RPCError("退出登录时删除保存的登录信息失败（%s），请再试一次" % (e.code or "error"))
        self._credentials = None
        self.creds = "none"
        self.account = None
        self.mark_changed("inbox")
        return {}

    # ------------------------------------------------------------------ sends (journal: each key once)

    def send_once(self, key: str, kind: str, send) -> dict:
        """Run send() once for key. A key that went out answers its first result again; one whose earlier attempt has
        an unknown outcome is refused, never sent again; one that surely failed may try again."""
        row = self.store.send_get(key)
        if row is not None:
            if row["state"] == "done":
                return row["result"]
            if key in self._inflight:
                raise RPCError("同一个请求正在发送，请稍等，不会重复发送")
            raise RPCError("上一次用同一个请求发送时结果不确定（可能已经发出去了），为避免重复不会再发。请先在%s里确认是否已发出。" % self.platform.name)
        if not self.store.send_claim(key, kind):
            raise RPCError("同一个请求正在发送，请稍等，不会重复发送")
        self._inflight.add(key)
        try:
            try:
                result = send()
            except SendRejected as e:
                self.store.send_release(key)
                raise RPCError("没有发出去：%s" % e.message)
            except RPCError:
                self.store.send_release(key)
                raise
            except Exception as e:
                msg = e.message if isinstance(e, BackendError) else "发送时出错"
                self.store.send_finish(key, "uncertain", {"error": msg})
                log.warning("a send ended with an unknown outcome (%s)", type(e).__name__)
                raise RPCError("发送结果不确定（%s），可能已经发出去了。不会自动重发，请先在%s里确认。" % (msg, self.platform.name))
            self.store.send_finish(key, "done", result)
            return result
        finally:
            self._inflight.discard(key)

    def send_as_user(self, chat_id: str, text: str, reply_to: Optional[str] = None, root: Optional[str] = None) -> str:
        if not self.signed_in:
            raise SendRejected("还没有登录%s" % self.platform.name)
        if self.link == "expired":
            raise SendRejected("%s登录已失效，请重新扫码登录" % self.platform.name)
        return self.backend.send(chat_id, text, reply_to or None, root or None)

    # ------------------------------------------------------------------ inbox

    def m_inbox_collect(self, params: dict) -> dict:
        if self.store is None:
            return {"things": []}
        t = self.timing
        rules = str(self.config.get("triage_rules") or "").strip()
        things = []
        me = self.me_name()
        for chat in self.store.chats_since(time.time() - t.inbox_window, t.inbox_most):
            rows = self.store.messages_before(chat["id"], None, 60)
            thing = views.inbox_thing(chat, rows, platform=self.platform.name, tz=self.tz, me=me, rules=rules,
                                      can_reply=self.signed_in)
            if thing is not None:
                things.append(thing)
        return {"things": things}

    def m_inbox_reply(self, params: dict) -> dict:
        if params.get("changes"):
            raise RPCError("%s会话不支持修改状态，只能回复文字" % self.platform.name)
        chat_id = str(params.get("thread") or "")
        text = str(params.get("text") or "")
        if not text.strip():
            raise RPCError("回复内容是空的")
        if not chat_id or self.store is None:
            raise RPCError("缺少要回复的会话")
        last = str(params.get("last") or "")
        chat = self.store.get_chat(chat_id)
        if chat is None:
            raise RPCError("本地没有这个%s会话" % self.platform.name)
        reply_to, root = None, None
        last_row = self.store.message(chat_id, last) if last else None
        if last_row is not None and not last_row.get("mine"):
            if last_row.get("root_id"):
                reply_to, root = last_row["id"], last_row["root_id"]
            elif chat.get("kind") != "dm":
                reply_to = last_row["id"]
        client_id = str(params.get("client_id") or "")
        if client_id:
            key = "inbox:" + client_id
        else:
            key = "inbox:" + hashlib.sha256("\x00".join([chat_id, last, text]).encode("utf-8")).hexdigest()

        def send():
            mid = self.send_as_user(chat_id, text, reply_to, root)
            return {"text": "已发送", "message_ref": mid}

        self.send_once(key, "inbox", send)
        return {}

    # ------------------------------------------------------------------ threads

    def fill_older(self, chat_id: str, before: Optional[int], want: int) -> bool:
        """Read older messages of chat from the service into the store; True when it has none older."""
        if not self.signed_in or self.link == "expired":
            return False
        try:
            got = self.backend.history(chat_id, before, max(want, 1))
        except BackendError as e:
            log.info("history for a thread page failed: %s", e.kind)
            return False
        self.ingest(got, live=False)
        return len(got) < want

    def m_thread_page(self, params: dict) -> dict:
        chat_id = str(params.get("thread") or "")
        if not chat_id or self.store is None:
            raise RPCError("缺少会话")
        limit = _int(params.get("limit"), 20, 1, THREAD_MOST)
        before = params.get("before") or ""
        before_pos: Optional[int] = None
        if before:
            try:
                before_pos = int(before)
            except ValueError:
                raise RPCError("before 不是这个连接器给出的位置")
        chat = self.store.get_chat(chat_id)
        if chat is None and self.signed_in:
            try:
                self.store.upsert_chat(self.backend.chat_info(chat_id))
                chat = self.store.get_chat(chat_id)
            except BackendError:
                chat = None
        if chat is None:
            raise RPCError("本地没有这个%s会话" % self.platform.name)
        rows = [r for r in self.store.messages_before(chat_id, before_pos, limit) if views.has_content(r)]
        at_start = False
        if len(rows) < limit:
            oldest = rows[0]["position"] if rows else before_pos
            at_start = self.fill_older(chat_id, oldest, limit - len(rows))
            rows = [r for r in self.store.messages_before(chat_id, before_pos, limit) if views.has_content(r)]
        rows.sort(key=lambda r: (r.get("at") or 0, r["position"]))
        me = self.me_name()
        messages = []
        for r in rows:
            parent = self.store.message(chat_id, r["parent_id"]) if r.get("parent_id") else None
            messages.append(views.thread_message(r, me, parent))
        page = {
            "chat": {"id": chat_id, "name": views.chat_name(chat, chat_id), "kind": views.chat_kind(chat),
                     "reply": self.cid + "_send"},
            "messages": messages,
        }
        if chat.get("members"):
            page["chat"]["members"] = int(chat["members"])
        if rows and not (len(rows) < limit and at_start):
            page["before"] = str(min(r["position"] for r in rows))
        return page

    def _media(self, params: dict, most: int) -> dict:
        ref = str(params.get("ref") or "")
        if not ref:
            raise RPCError("缺少 ref")
        if self.backend is None or not self.signed_in:
            raise RPCError("还没有登录%s" % self.platform.name)
        try:
            mime, data = self.backend.media(ref, most)
        except BackendError as e:
            raise RPCError(e.message)
        if len(data) > most:
            raise RPCError("附件超过 %d MB" % (most >> 20))
        return {"mime": mime or "application/octet-stream", "data": base64.b64encode(data).decode("ascii")}

    # ------------------------------------------------------------------ archive

    def m_archive_changes(self, params: dict) -> dict:
        cursor = str(params.get("cursor") or "")
        if cursor and not cursor.isdigit():
            raise RPCError("cursor 不是这个连接器给出的")
        after = int(cursor) if cursor else 0
        most_chats = _int(params.get("limit"), 100, 1, 500)
        if self.store is None:
            return {"batches": [], "next": cursor, "more": False}
        rows = self.store.archive_rows(after, ARCHIVE_ROWS_MOST)
        batches: Dict[str, List[dict]] = {}
        scanned = after
        more = len(rows) == ARCHIVE_ROWS_MOST
        for r in rows:
            if r["chat_id"] not in batches and len(batches) >= most_chats:
                more = True
                break
            scanned = r["seq"]
            if views.is_system(r) or not views.has_content(r):
                continue
            batches.setdefault(r["chat_id"], []).append(r)
        me = self.me_name()
        out = []
        for chat_id, msgs in batches.items():
            msgs.sort(key=lambda r: (r.get("at") or 0, r["position"]))
            chat = self.store.get_chat(chat_id)
            out.append({"chat": views.archive_chat(chat, chat_id),
                        "messages": [views.archive_message(r, me, self.platform.handle_kind) for r in msgs]})
        return {"batches": out, "next": str(scanned), "more": more}


def _int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    if n <= 0 and lo > 0:
        return default
    return max(lo, min(hi, n))
