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
from .ui import UI, ChatsToday
from .backend import Account, BackendError, Chat, CredentialsExpired, Message, SendRejected
from .events import Changed
from .hub import HubAPI
from .people import People
from .platform import Platform, Timing
from .store import Store
from .textutil import rfc3339, zone
from .wire import Peer, RPCError

log = logging.getLogger("openmate")

EMPTY_PAGES = 6  # a re-read of the messages stored with no text: at most this many pages per chat

PROTOCOL = 1
THREAD_MOST = 50
THREAD_MEDIA_MOST = 4 << 20
ARCHIVE_MEDIA_MOST = 20 << 20
ARCHIVE_ROWS_MOST = 2000
ARCHIVE_UNDECIDED_HOURS = 6  # a message of a chat the archive rule cannot decide on waits this long at most
ARCHIVE_ASK_MOST = 3  # undecided chats the backend is asked about per archive read
STARTUP_WAIT = 20.0  # a call that arrives while the program starts (store, backend, saved sign-in) waits this long
SENT_TEXT_MOST = 4000  # sent.record keeps this many characters of what was said
# History (capability "history": history.chats, archive.history), read from the store: what the program has stored of
# a chat since it signed in (live messages, catch-ups, history reads). Pages are hints of HISTORY_PAGE messages, at most
# HISTORY_MOST (the hub's own cap); the store is read in steps of HISTORY_STEP rows.
HISTORY_PAGE = 50
HISTORY_MOST = 200
HISTORY_STEP = 500
HISTORY_CHATS_MOST = 5000

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
        self.secrets: Dict[str, str] = {}
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
        # Every write of the sealed credentials holds _creds_mu, so they land in order. _creds_gen moves on at each
        # sign-in and logout: a rotation handed over before one is never written after it. From a logout until the
        # next sign-in starts (_creds_out) none is written at all, so logout's delete stays the last write.
        # _creds_seq numbers rotations as they are handed over: an older one never overwrites a newer.
        self._creds_mu = threading.Lock()
        self._creds_gen = 0
        self._creds_out = False
        self._login_gen = 0  # the credentials generation the QR sign-in under way started in
        self._creds_seq = 0
        self._creds_sealed_seq = 0
        self._creds_cv = threading.Condition()
        self._creds_next: Optional[tuple] = None  # (gen, seq, creds): the newest rotation the writer has not taken
        self._creds_writer: Optional[threading.Thread] = None
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
        self._done_heard: set = set()  # chats the live link heard marked done in the service's client: ask about them
        self._done_ask = False  # ask the service about every chat that needs the user (after a catch-up)
        self._done_wake = threading.Event()
        self._done_mu = threading.Lock()  # a round's store reads and writes; shutdown and an account switch wait for it
        self._account_epoch = 0  # bumped when another account's sign-in wiped the store
        self.done_rounds = 0  # finished rounds (tests wait on it)
        self.ready = threading.Event()
        self.closing = threading.Event()
        self._inflight: set = set()
        self._names: Dict[str, str] = {}
        self.changed: Optional[Changed] = None
        self.agent: Optional[AgentRunner] = None
        self.people: Optional[People] = None  # who is a bot (made at the start, with the store)
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
        if "history" in self._capabilities():
            # only for a connector whose manifest declares it (the hub asks no other, and the method stays off then)
            self._methods["history.chats"] = self.m_history_chats
            self._methods["archive.history"] = self.m_archive_history
        for m in getattr(platform, "methods_off", ()) or ():
            self._methods.pop(m, None)
        self.ui: Optional[UI] = None
        self.today_api = None  # todayapi.TodayAPI, for a platform with today_api_callers and a socket from the host
        self.connector_on = True  # initialize's active.connectors names this connector (off: no link, no /agent)
        self.hub_role = "center"  # "member": another hub is primary, the chat-driven work (/agent, 代答) is its own

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
        if method in ("ui.render", "ui.action"):  # the extension's pages and cards (ui.py), not the connector's
            if not self.ready.wait(STARTUP_WAIT):
                raise RPCError("扩展还在启动，请稍后再试")
            ui = self._ui()
            return ui.render(params) if method == "ui.render" else ui.action(params)
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
                sec = params.get("secrets")  # the manifest's secrets the user set (never logged, never shown)
                self.secrets = {str(k): str(v) for k, v in sec.items() if v} if isinstance(sec, dict) else {}
                self.data_dir = (ext.get("data_dir") or self.env.get("PERSONA_EXTENSION_DATA")
                                 or self.env.get("PERSONA_PLUGIN_DATA")
                                 or os.path.join(os.path.expanduser("~"), ".openmate", self.manifest["id"]))
                self.hub = HubAPI(hub.get("url") or self.env.get("PERSONA_HUB_URL", ""),
                                  hub.get("token") or self.env.get("PERSONA_HUB_TOKEN", ""))
                self.tz = zone(hub.get("timezone") or "")
                active = params.get("active")
                if isinstance(active, dict):
                    # Persona lists the contributions that are on, and leaves an extension point out when none of
                    # its own is: no "connectors" key is this connector off. Only an older hub sends no active (on).
                    self.connector_on = self.cid in [str(c) for c in active.get("connectors") or []]
                self.hub_role = str(hub.get("role") or "center")
                self.initialized = True
                self._start_today_api()  # /health answers from now on: the host checks it as soon as this returns
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
                self.backend.on_credentials_sync = self._credentials_rotated_now
                self.backend.on_history = self._history_from_backend
                self.backend.on_people_kinds = self._people_kinds_from_backend
                self.backend.on_chat_gone = self._chat_gone
                self.backend.on_doc_mention = self._doc_mention
                self.backend.on_chats_done = self._chats_done
                self.backend.on_reacted = self._reacted
            except BackendError as e:
                self.setup_error = e.message
                self.setup_kind = e.kind
                log.warning("backend not available: %s", e.kind)
            self.agent = AgentRunner(self)
            if not self.connector_on or self.hub_role == "member":
                # the user turned the connector off, or another hub does the chat-driven work: nothing answers or
                # reacts as the user from here (the pages still show what is stored)
                self.agent.enabled = False
                log.info("/agent and 代答 are off here (%s)", "the connector is off" if not self.connector_on else "a member hub")
            self.people = People(self)
            creds = self._load_credentials()
            if creds is not None:
                self._credentials = creds
                self.creds = "present"
                name = self.store.meta_get("account_name")
                if name:
                    self.account = Account(self.store.meta_get("account_id"), name)
                if self.backend is not None and self.connector_on:
                    self._start_link(restore=True)  # off: no link, nothing received or sent in the user's name
            self.agent.start()
            if self.backend is not None and getattr(self.backend, "done_marks", False):
                threading.Thread(target=self._done_loop, name="openmate-done", daemon=True).start()
            self._ui().update_badge()  # the hub forgets an extension's badges when it restarts
            if self.timing.warm_pages:
                self._ui().warm()
        except Exception:
            log.exception("startup failed")
            if not self.setup_error:
                self.setup_error = "扩展启动失败，详情见扩展日志"
        finally:
            self.ready.set()

    def shutdown(self) -> None:
        self.closing.set()
        self._done_wake.set()
        held = self._done_mu.acquire(timeout=5.0)  # a round touching the store finishes first
        if self.today_api is not None:
            try:
                self.today_api.stop()  # no new requests; those in flight finish (bounded) before the store closes
            except Exception:
                log.exception("stopping the 今天 API failed")
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
        if held:
            self._done_mu.release()

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

    def _rotation(self, creds: dict) -> tuple:
        with self._creds_cv:
            self._creds_seq += 1
            return self._creds_gen, self._creds_seq, dict(creds)

    def _credentials_rotated(self, creds: dict) -> None:
        """The adapter's session was refreshed: seal the new credentials in the background. One writer takes the
        newest handed over (older ones it has not started are skipped), so the hub never ends on stale ones; a write
        that failed is tried again (a few times) unless newer ones came meanwhile."""
        with self._creds_cv:  # numbered and queued at once: an older one can never take the place of a newer
            self._creds_seq += 1
            self._creds_next = (self._creds_gen, self._creds_seq, dict(creds))
            if self._creds_writer is None:
                self._creds_writer = threading.Thread(target=self._creds_write_loop, name="openmate-creds",
                                                      daemon=True)
                self._creds_writer.start()

    def _creds_write_loop(self) -> None:
        tries, last = 0, 0
        while True:
            with self._creds_cv:
                rot, self._creds_next = self._creds_next, None
                if rot is None:
                    self._creds_writer = None
                    return
            ok = False
            try:
                ok = self._seal_rotation(*rot)
            except Exception:
                log.exception("saving refreshed credentials failed")
            tries = 0 if ok else (tries + 1 if rot[1] == last else 1)  # counted per rotation
            last = rot[1]
            if not ok and tries < 4 and not self.closing.is_set():
                with self._creds_cv:
                    if self._creds_next is None and rot[0] == self._creds_gen and not self._creds_out:
                        self._creds_next = rot  # nothing newer: the same again, a little later
                self.closing.wait(self.timing.secrets_retry * tries)

    def _seal_rotation(self, gen: int, seq: int, creds: dict) -> bool:
        """Write rotated credentials unless a sign-in or logout came after they were handed over, or newer ones are
        already written."""
        with self._creds_mu:
            if gen != self._creds_gen or self._creds_out:
                log.info("rotated credentials from a session that ended: not saved")
                return False
            if seq <= self._creds_sealed_seq:
                return True  # newer ones are sealed already
            try:
                self._save_credentials(creds)
            except RPCError as e:
                log.warning("saving rotated credentials failed (%s)", e.code or "error")
                return False
            self._creds_sealed_seq = seq
            self._credentials = creds
            return True

    def _history_from_backend(self, msgs: list) -> None:
        """Messages the adapter hands over as history: kept like a catch-up read, never acted on."""
        try:
            self.ingest(list(msgs), live=False)
        except Exception:
            if not self.closing.is_set():
                log.exception("the adapter's history could not be kept")

    def _credentials_rotated_now(self, creds: dict) -> bool:
        """Seal rotated credentials before the adapter uses them: in the caller's thread, in order with every other
        write. False when they were not written (a failure, or a logout or sign-in came first)."""
        return self._seal_rotation(*self._rotation(creds))

    # ------------------------------------------------------------------ the sent-as-user ledger

    def record_sent(self, chat_id: str, text: str, message_ref: str, reply_to: str = "", root: str = "",
                    session_id: str = "", machine_id: str = "", setting: str = "", summary: str = "",
                    target: Optional[Dict[str, Any]] = None) -> None:
        """Tell the hub about a message sent in the user's name without the hub's gate (sent.record, which the
        manifest's sends_as_user allows), in the background: the send happened, so a failed record never undoes it.
        A gated write (actions.do) and the user's own Inbox Send are recorded by the hub, never here."""
        if target is None:  # a chat's message; a document's comment says its own target
            target = {"chat": chat_id}
            if root:
                target["thread"] = root
            if reply_to:
                target["reply_to"] = reply_to
        params: Dict[str, Any] = {"contribution": self.cid, "target": target, "text": (text or "")[:SENT_TEXT_MOST]}
        for k, v in (("summary", summary), ("message_ref", message_ref), ("session_id", session_id),
                     ("machine_id", machine_id), ("setting", setting)):
            if v:
                params[k] = v
        threading.Thread(target=self._record_sent, args=(params,), name="openmate-sent", daemon=True).start()

    def _record_sent(self, params: dict) -> None:
        waits = list(self.timing.sent_retry)
        while True:
            try:
                self.hub_call("sent.record", params)
                return
            except RPCError as e:
                code = e.code or ""
                retry_after = (e.data or {}).get("retry_after") if isinstance(e.data, dict) else None
                if code == "rate_limited" and waits:
                    try:
                        wait = min(60.0, max(0.0, float(retry_after)))
                    except (TypeError, ValueError):
                        wait = waits[0]
                elif code == "" and waits:  # the ledger could not be written: worth another try later
                    wait = waits[0]
                else:  # permission_denied, invalid_params, no_database, unknown_method (an older hub), closed, timeout
                    log.warning("sent.record failed (%s): the send is not in the hub's ledger", code or "error")
                    return
                waits.pop(0)
                if self.closing.wait(wait):
                    return

    def _emit_changed(self, scope: str) -> None:
        if self.peer is not None:
            self.peer.notify("connector.changed", {"contribution": self.cid, "scope": scope})
        if scope == "inbox":
            self._ui().update_badge()

    def _doc_mention(self, mention) -> None:
        if self.agent is not None:
            try:
                self.agent.on_doc_mention(mention)
            except Exception:
                log.exception("a document mention was not taken")

    def _start_today_api(self) -> None:
        callers = tuple(getattr(self.platform, "today_api_callers", ()) or ())
        path = self.env.get("PERSONA_EXTENSION_SOCKET") or ""
        if not callers or not path:
            return
        from .todayapi import TodayAPI

        api = TodayAPI(self, path, callers)
        try:
            api.start()
        except OSError as e:
            log.warning("the 今天 API could not listen on the extension's socket (%s)", e.__class__.__name__)
            return
        self.today_api = api

    # ------------------------------------------------------------------ the service's done mark (Feishu's 完成)

    def _ask_kind(self, chat_id: str) -> bool:
        """Ask the backend now what the account of a direct chat is (WeChat looks it up), for the archive rule.
        Whether it was asked (False: signed out, or the request failed)."""
        if self.backend is None or self.store is None or not self.signed_in:
            return False
        chat = self.store.get_chat(chat_id) or {}
        # a backend's ask_kinds raises when the request failed (WeChat); people_kinds otherwise
        ask = getattr(self.backend, "ask_kinds", None) or self.backend.people_kinds
        try:
            ask([chat.get("peer_id") or chat_id])
            return True
        except Exception as e:
            log.info("an account could not be looked up for the archive (%s)", e.__class__.__name__)
            return False

    def _chat_gone(self, chat_id: str) -> None:
        """The account left or deleted a chat in the service: out of the lists until a newer message."""
        if self.store is None or self.closing.is_set() or not chat_id:
            return
        try:
            self.store.chat_gone(str(chat_id))
            self.mark_changed("inbox")
        except Exception:
            if not self.closing.is_set():
                log.exception("a chat gone could not be kept")

    def _people_kinds_from_backend(self, kinds: Dict[str, str]) -> None:
        """The backend learnt what someone is after it was asked (an official account found later): kept, and the
        pages and the badge count them again."""
        if self.store is None or self.closing.is_set():
            return
        try:
            kinds = {str(i): k for i, k in (kinds or {}).items() if i and k in ("bot", "user")}
            if kinds:
                self.store.set_people_kinds(kinds)
                self.mark_changed("inbox")
        except Exception:
            if not self.closing.is_set():
                log.exception("kinds from the backend could not be kept")

    def _chats_done(self, done: Dict[str, float]) -> None:
        """The live link heard chats marked done in the service's own client: ask about them (off its thread). A push
        says neither which message it covers nor whether one came after, so only the answer to a question counts."""
        self.ask_done(list(done or {}))

    def _reacted(self, message_ids: List[str]) -> None:
        """The live link heard the account react to messages: ask about their chats (off its thread)."""
        if self.store is None or self.closing.is_set():
            return
        try:
            self.ask_done(self.store.chats_of_messages(message_ids))
        except Exception:
            if not self.closing.is_set():
                log.exception("a reaction could not be taken")

    def ask_done(self, chat_ids: List[str]) -> None:
        """Ask the service soon whether these chats are done there (a backend with done_marks; others: nothing)."""
        if self.backend is None or not getattr(self.backend, "done_marks", False):
            return
        with self.lock:
            self._done_heard.update(str(c) for c in chat_ids if c)
        self._done_wake.set()

    def _ask_done(self) -> None:
        with self.lock:
            self._done_ask = True
        self._done_wake.set()

    def _done_loop(self) -> None:
        """The service's done mark as 处理完: the chats the live link heard about, at once; every chat that needs the
        user after each catch-up and every done_check (marks made while the link was down, or never pushed). While
        the link is not up nothing is asked: the catch-up after it comes back asks."""
        t = self.timing
        next_check = time.monotonic() + t.done_check
        while not self.closing.is_set():
            self._done_wake.wait(max(0.0, next_check - time.monotonic()))
            self._done_wake.clear()
            if self.closing.is_set():
                return
            with self.lock:
                heard, self._done_heard = self._done_heard, set()
                ask, self._done_ask = self._done_ask, False
            if time.monotonic() >= next_check:
                ask = True
            if ask:
                next_check = time.monotonic() + t.done_check
            if not (ask or heard) or self.link != "up":
                continue
            try:
                n = self._done_round(None if ask else heard)
                if n:
                    log.info("%d need(s) marked done in the service's client: 处理完", n)
            except CredentialsExpired:
                log.info("could not ask which chats are done: the sign-in is gone")
            except BackendError as e:
                log.info("could not ask which chats are done: %s", e.kind)
            except Exception:
                if not self.closing.is_set():
                    log.exception("the done marks could not be applied")
            finally:
                self.done_rounds += 1

    def _done_round(self, only: Optional[set]) -> int:
        """One question to the service about the needs (those of only, or all) as they stand now; the answer is put
        on exactly those, unless the link, the account or the extension changed while it was out."""
        with self.lock:
            gen, epoch = self._gen, self._account_epoch
        with self._done_mu:
            if self.closing.is_set() or self.store is None:
                return 0
            today = ChatsToday(self)
            needs, marked = today.done_snapshot(only)
        if not needs:
            return 0
        states: Dict[str, Optional[float]] = {}
        reacted: Dict[str, float] = {}
        failed: Optional[BackendError] = None
        try:
            states = self.backend.chat_done_states([t["_chat"]["id"] for t in needs])
        except CredentialsExpired:
            raise
        except BackendError as e:
            failed = e
        agent = self.agent
        ignore = tuple(r for r in (getattr(agent, "ack", ""), getattr(agent, "fail_reaction", "")) if r)
        try:  # a reaction of the account's on the message that made the need: answered without words
            reacted = self.backend.my_reactions([t["_version"] for t in needs], ignore)
        except CredentialsExpired:
            raise
        except BackendError as e:
            failed = failed or e
            states = {c: w for c, w in states.items() if w is not None}  # not known not to be answered: 恢复 marks stay
        if failed is not None and not states and not reacted:
            raise failed
        for t in needs:
            if t["_version"] in reacted:  # that very message: its version (no time to weigh), whatever 完成 said
                states[t["_chat"]["id"]] = 0.0
        with self._done_mu:
            with self.lock:
                same = gen == self._gen and epoch == self._account_epoch
            if not same or self.closing.is_set():
                return 0
            return today.service_done(needs, marked, states)

    def _ui(self) -> "UI":
        with self.lock:
            if self.ui is None:
                self.ui = UI(self, self.platform.ui_pages)
            return self.ui

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
                    if stop.is_set() or gen != self._gen:
                        return  # signed out or in again meanwhile: this restore is not the account's any more
                    self._bind_account(acct)  # a sign-in stopped half-way may have left another account's store
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

    def _bind_account(self, acct: Account) -> None:
        """The store belongs to this account from now on: another account's chats and marks are forgotten first."""
        prev = self.store.meta_get("account_id") if self.store is not None else None
        if prev and acct.id and prev != acct.id:
            with self._done_mu:  # a done round of the account before must not write into this one's
                self.store.wipe_account_data()  # another account: forget the previous one's chats
                with self.lock:
                    self._account_epoch += 1

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
            raise  # not taken: the backend must not count it delivered (it is delivered again, or read as history)

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
        """After the link is really up: what came while it was down. The service's most recent chats (the first time:
        history_chats of them, their initial history) and the known recent ones, each read back to what is stored.
        A chat that cannot be read is skipped; the first time counts as done only once every read worked."""
        try:
            t = self.timing
            started = time.time()
            done_now = set()
            first = self.store.meta_get("initial_history_done") != "1"
            whole = True
            n = _int(self.config.get("history_chats"), 20, 0, 50) if first else t.catchup_chats
            plan: List[str] = []
            if n > 0:
                try:
                    listed = self.backend.recent_chats(n)[:n]
                    if gen != self._gen:
                        return
                    for c in listed:
                        self.store.upsert_chat(c)
                        plan.append(c.id)
                except CredentialsExpired:
                    raise
                except BackendError as e:
                    whole = False
                    log.warning("the recent chats could not be read (%s): only the known ones are read", e.kind)
            for c in self.store.chats_since(time.time() - t.inbox_window, t.catchup_chats):
                if c["id"] not in plan:
                    plan.append(c["id"])
            since = None if first else self._caught_up_since()
            # the first time: enough for every chat's initial history besides the known ones
            budget = [t.catchup_budget + (len(plan) * t.initial_messages if first else 0)]
            failed = 0
            for chat_id in plan:
                if gen != self._gen or self.link != "up":
                    return
                if budget[0] <= 0:
                    whole = False
                    log.info("catch-up: the message budget is spent, %d chat(s) left", len(plan) - len(done_now))
                    break
                try:
                    if not self._catch_up_chat(chat_id, first, since, budget, gen):
                        whole = False  # the budget cut it short: the first time is not done yet
                    failed = 0
                except CredentialsExpired:
                    raise
                except BackendError as e:
                    whole, failed = False, failed + 1
                    log.warning("catch-up: a chat could not be read (%s)", e.kind)
                    if failed >= 3:
                        log.warning("catch-up stopped after %d failed chats in a row", failed)
                        break
                    continue
                done_now.add(chat_id)
            if gen != self._gen:
                return  # signed out or another account meanwhile: nothing marked for the store it found
            if whole:
                if first:
                    self.store.meta_set("initial_history_done", "1")
                self.store.meta_set("caught_up_at", "%.3f" % started)
            # a newer backend reads some messages better: their recent chats once more, so the stored text is renewed
            version = str(getattr(self.backend, "render_version", "") or "")
            kinds = tuple(getattr(self.backend, "rerender_types", ()) or ())
            if version and self.store.meta_get("render_version") != version:
                if kinds and not first:  # a first history was just read this way
                    for chat_id in self.store.chats_with_types(kinds, time.time() - t.rerender_window, t.rerender_chats):
                        if gen != self._gen or self.link != "up":
                            return
                        if chat_id not in done_now:
                            got = self.backend.history(chat_id, None, t.rerender_messages)
                            if gen != self._gen:
                                return
                            self.ingest(got, live=False)
                empty = tuple(getattr(self.backend, "rerender_empty", ()) or ())
                again = False
                if empty:
                    # messages stored with no text (an older reading, also from a first sync cut short): read again
                    # where they are, from right after the newest back to the oldest (bounded); a chat that cannot be
                    # read is passed over, and the whole re-read is tried again next time only for a network failure
                    gaps = self.store.empty_positions(empty, time.time() - t.rerender_window, t.rerender_chats)
                    for chat_id, (lo, hi, _) in gaps.items():
                        before = hi + 1
                        try:
                            for _ in range(EMPTY_PAGES):
                                if gen != self._gen or self.link != "up":
                                    return
                                got = self.backend.history(chat_id, before, t.rerender_messages)
                                if gen != self._gen:
                                    return
                                self.ingest(got, live=False)
                                if not got:
                                    break
                                before = min(m.position for m in got)
                                if before <= lo:
                                    break
                        except CredentialsExpired:
                            raise
                        except BackendError as e:
                            log.info("re-reading messages stored empty: a chat could not be read (%s)", e.kind)
                            again = again or e.kind in ("unreachable", "unknown", "proxy")
                if not again:
                    self.store.meta_set("render_version", version)
            self.last_sync = time.time()
            self.mark_changed("inbox", "chats")
            self._ask_done()
        except BackendError as e:
            log.warning("history catch-up stopped: %s", e.kind)
        except Exception:
            if not self.closing.is_set():
                log.exception("history catch-up failed")

    def _caught_up_since(self) -> float:
        """Messages of a chat new to the store are kept from here: the last whole catch-up (less a margin), else the
        inbox window."""
        t = self.timing
        try:
            return float(self.store.meta_get("caught_up_at") or "") - t.catchup_margin
        except ValueError:
            return time.time() - t.inbox_window

    def _catch_up_chat(self, chat_id: str, first: bool, since: Optional[float], budget: List[int],
                       gen: int = 0) -> bool:
        """Read chat back to its newest stored message (catchup_messages at a time, catchup_pages times at most). One
        with nothing stored: the first time one page of initial history; later one page, what came since the last
        catch-up. Nothing read is stored once the account changed (gen)."""
        t = self.timing
        known = self.store.newest_position(chat_id)
        initial = known is None and (first or since is None)
        before: Optional[int] = None
        for _ in range(max(1, t.catchup_pages) if known is not None else 1):
            want = t.initial_messages if initial else t.catchup_messages
            limit = min(want, budget[0])
            if limit <= 0:
                return False
            got = self.backend.history(chat_id, before, limit)
            budget[0] -= len(got)
            if gen and gen != self._gen:
                return True  # read for an account signed out meanwhile: not into the store
            if known is None and not initial:
                self.ingest([m for m in got if m.at >= since], live=False)
            else:
                self.ingest(got, live=False)
            if len(got) < limit:
                return limit == want  # the chat's start (or the budget's end)
            oldest = min(got, key=lambda m: m.position)
            if known is not None and oldest.position <= known:
                return True  # back to what is stored
            if known is None and not initial and oldest.at < since:
                return True
            if limit < want:
                return False
            before = oldest.position
        return True

    # ------------------------------------------------------------------ storing messages

    def ingest(self, msgs: List[Message], live: bool) -> List[dict]:
        if not msgs or self.store is None:
            return []
        latest: Dict[str, float] = {}
        for m in msgs:  # a chat gone comes back with a newer message of someone's (joined again)
            if (m.type or "") != "SYSTEM" and (m.at or 0) > latest.get(m.chat_id, 0):
                latest[m.chat_id] = float(m.at or 0)
        if latest:
            self.store.chats_back(latest)
        for chat_id in {m.chat_id for m in msgs}:
            known = self.store.get_chat(chat_id)
            # no name, or only the id as a stand-in (kept by older versions): ask the backend again
            if known is None or not known.get("name") or known.get("name") == chat_id:
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

    def _login_start(self, renew: bool = False):
        """A new QR sign-in: from here the adapter's rotations are written again (WeChat seals the accepted sign-in
        before login_finish answers). The flow keeps the credentials generation it started in: a logout meanwhile
        ends it. renew: a QR the flow renews by itself, which never reopens what a logout closed."""
        with self._creds_mu:
            if renew:
                if self._creds_out and self._login_gen != self._creds_gen:
                    self._login = None
                    raise RPCError("已经退出登录了，请重新扫码登录")
                self._login_gen = self._creds_gen  # no logout since: the renewed QR is this generation's
            else:
                self._creds_out = False
                self._login_gen = self._creds_gen
        return self.backend.login_start()

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
                    qr = self._login_start(renew=bool(step))  # a poll of a flow that ended: never past a logout
                except BackendError as e:
                    raise RPCError("没能开始登录：%s" % e.message)
                self._login = {"state": qr.state, "url": qr.url}
                return self._qr_prompt(qr.url, p.login_hint)
            flow = self._login
            try:
                status = self.backend.login_poll(flow["state"])
            except BackendError as e:
                if e.kind != "login":
                    return self._qr_prompt(flow["url"], p.login_hint, "暂时查不到扫码结果：%s" % e.message)
                # this attempt failed for a reason the user can act on (WeChat: confirm on the same LAN): a new QR
                # with the reason, not "expired"
                try:
                    qr = self._login_start(renew=True)
                except BackendError as e2:
                    raise RPCError("扫码没成功（%s），换新二维码时出错：%s" % (e.message, e2.message))
                self._login = {"state": qr.state, "url": qr.url}
                return self._qr_prompt(qr.url, p.login_hint, "上次扫码没成功：%s。请按提示再扫一次" % e.message)
            if status == "scanned":
                return self._qr_prompt(flow["url"], p.login_hint_scanned)
            if status == "expired":
                try:
                    qr = self._login_start(renew=True)
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
            voided = False
            with self._creds_mu:
                if self._creds_out or self._login_gen != self._creds_gen:
                    voided = True  # a logout came while this QR was scanned: it stays signed out
                else:
                    # a rotation handed over before this sign-in is never written after it
                    with self._creds_cv:
                        self._creds_gen += 1
                    try:
                        self._save_credentials(done.credentials)
                    except RPCError:
                        self._login = None
                        raise RPCError("登录成功了，但保存登录信息失败，请重新登录")
                    self._credentials = done.credentials
                    self._creds_out = False
            if voided:
                self._login = None
                try:  # outside _creds_mu: the adapter may be sealing under its own lock and waiting for it
                    self.backend.logout()
                except Exception:
                    log.exception("backend logout failed")
                raise RPCError("登录过程中退出了登录，这次扫码作废，请重新扫码")
            self._login = None
            acct = done.account
            self._bind_account(acct)
            self.creds = "present"
            self._remember_account(acct)
            self._catchup_done.clear()
            if not self.connector_on:
                # signed in, but the user turned the connector off in Persona: nothing is received or sent
                self.mark_changed("inbox", "chats")
                return {"done": True, "account": acct.name or acct.id}
            with self.lock:  # a logout sets _creds_out first, then stops links under this lock: never a link after it
                if self._creds_out:
                    raise RPCError("登录过程中退出了登录，这次扫码作废，请重新扫码")
                self._start_link(restore=False)
            # Report done once the link is really up and the first history is in (bounded); polls meanwhile wait.
            deadline = time.monotonic() + self.timing.login_wait
            if self._link_up.wait(max(0.0, deadline - time.monotonic())):
                self._catchup_done.wait(max(0.0, deadline - time.monotonic()))
        self.mark_changed("inbox", "chats")
        return {"done": True, "account": acct.name or acct.id}

    def m_logout(self, params: dict) -> dict:
        with self._creds_mu:
            # from here no rotation is written: the delete below is the last write of the credentials
            with self._creds_cv:
                self._creds_gen += 1
            self._creds_out = True
            self._credentials = None
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
        with self._creds_mu:  # after any write in flight; none comes after it
            try:
                self.hub_call("connector.secrets.delete", {"contribution": self.cid})
            except RPCError as e:
                raise RPCError("退出登录时删除保存的登录信息失败（%s），请再试一次" % (e.code or "error"))
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
            rows = self.store.inbox_rows(chat["id"], 60)
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
        keep = getattr(self.platform, "archive_chat", None)
        kept: Dict[str, Optional[bool]] = {}
        tried: set = set()  # undecided chats this read tried to ask about (ARCHIVE_ASK_MOST)
        asked: set = set()  # of those, the ones the service was really asked about

        def rule(cid: str) -> Optional[bool]:
            if cid not in kept:
                try:
                    v = keep(cid, self.store.get_chat(cid), self.config or {})
                    kept[cid] = None if v is None else bool(v)
                except Exception:
                    log.exception("the archive rule failed: the chat is left out")
                    kept[cid] = False
            if kept[cid] is None and cid not in tried and len(tried) < ARCHIVE_ASK_MOST:
                tried.add(cid)  # undecided: ask the service about the account now, then the rule again
                if self._ask_kind(cid):
                    asked.add(cid)
                del kept[cid]
                return rule(cid)
            return kept[cid]

        for r in rows:
            if r["chat_id"] not in batches and len(batches) >= most_chats:
                more = True
                break
            if not views.is_system(r) and views.has_content(r) and keep is not None:
                # the platform's rule (WeChat: no groups unless turned on, no official accounts)
                decided = rule(r["chat_id"])
                if decided is None:
                    if r["chat_id"] not in tried:
                        more = True  # not asked about in this read (its asks are used up): the next read does
                        break
                    age = time.time() - float(r.get("received") or r.get("at") or 0)
                    if r["chat_id"] not in asked and age < ARCHIVE_UNDECIDED_HOURS * 4 * 3600:
                        more = False  # could not be asked (signed out, a failed request): read again later
                        break
                    if r["chat_id"] in asked and age < ARCHIVE_UNDECIDED_HOURS * 3600:
                        more = False  # the cursor stops before it: a later read goes on from this message
                        break
                    decided = False  # asked, and long undecided: left out (an official account's must not get in)
                if not decided:
                    scanned = r["seq"]
                    continue
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

    # ------------------------------------------------------------------ history (the knowledge base's import and its
    # per-chat settings: Persona lists history.chats in 设置 → 知识库选择 → 添加聊天)

    def _capabilities(self) -> List[str]:
        for c in ((self.manifest.get("contributes") or {}).get("connectors") or []):
            if isinstance(c, dict) and c.get("id") == self.cid:
                return [str(x) for x in (c.get("capabilities") or [])]
        return []

    def _history_open(self, chat_id: str, chat: Optional[dict]) -> bool:
        """The archive would hand this chat over: no platform rule, or the rule says yes (undecided is no)."""
        keep = getattr(self.platform, "archive_chat", None)
        if keep is None:
            return True
        try:
            return keep(chat_id, chat, self.config or {}) is True
        except Exception:
            log.exception("the archive rule failed: the chat is left out of the history")
            return False

    @staticmethod
    def _history_row(r: dict) -> bool:
        return not views.is_system(r) and views.has_content(r)  # what archive.changes hands over

    def m_history_chats(self, params: dict) -> dict:
        if self.store is None:
            return {"chats": []}
        counts = self.store.message_counts()
        out = []
        for chat in self.store.chats_recent(HISTORY_CHATS_MOST):  # the newest first, without the chats gone
            cid = chat.get("id") or ""
            if not cid or not self._history_open(cid, chat):
                continue
            item: Dict[str, Any] = {"id": cid, "name": views.chat_name(chat, cid), "kind": views.chat_kind(chat)}
            if chat.get("muted"):
                item["muted"] = True
            if counts.get(cid):
                item["estimate"] = counts[cid]
            out.append(item)
        return {"chats": out}

    def _history_back(self, chat_id: str, before: Optional[int], want: int):
        """Up to want rows to hand over older than before (from the newest when None), oldest first, and the oldest
        position looked at (None when nothing was): rows the archive leaves out are passed over, however many."""
        got: List[dict] = []
        oldest: Optional[int] = None
        cur = before
        while len(got) < want:
            rows = self.store.messages_before(chat_id, cur, HISTORY_STEP)
            for r in reversed(rows):
                oldest = int(r["position"])
                if self._history_row(r):
                    got.append(r)
                    if len(got) >= want:
                        break
            if len(rows) < HISTORY_STEP or oldest is None:
                break
            cur = oldest
        got.reverse()
        return got, oldest

    def _history_forward(self, chat_id: str, after: int, want: int):
        """Up to want rows to hand over newer than after, oldest first, and the newest position looked at."""
        got: List[dict] = []
        newest: Optional[int] = None
        cur = after
        while len(got) < want:
            rows = self.store.messages_after(chat_id, cur, HISTORY_STEP)
            for r in rows:
                newest = int(r["position"])
                if self._history_row(r):
                    got.append(r)
                    if len(got) >= want:
                        break
            if len(rows) < HISTORY_STEP or newest is None:
                break
            cur = newest
        return got, newest

    def m_archive_history(self, params: dict) -> dict:
        chat_id = str(params.get("chat") or "")
        if not chat_id:
            raise RPCError("archive.history 没有 chat")
        marks = {k: str(params[k]) for k in ("before", "after", "around") if params.get(k) not in (None, "")}
        if len(marks) > 1:
            raise RPCError("before、after、around 最多给一个")

        def position(s: str) -> int:
            try:
                return int(s)
            except ValueError:
                raise RPCError("位置不是这个连接器给出的：%s" % s[:40])

        want = _int(params.get("limit"), HISTORY_PAGE, 1, HISTORY_MOST)
        if self.store is None:
            return {"pace": {"stop": True, "reason": "扩展还没有准备好（没有登录或设置有误），请先在连接器设置里登录"}}
        chat = self.store.get_chat(chat_id)
        if chat is None or self.store.is_gone(chat_id) or not self._history_open(chat_id, chat):
            return {"unreadable": True}  # never seen here, left, or kept out of the archive: skip it for good
        back: List[dict] = []
        fwd: List[dict] = []
        oldest = newest = None
        if "around" in marks:
            p = position(marks["around"])
            back, oldest = self._history_back(chat_id, p + 1, (want + 1) // 2)  # the message at p, and older
            fwd, newest = self._history_forward(chat_id, p, want - len(back))
        elif "after" in marks:
            fwd, newest = self._history_forward(chat_id, position(marks["after"]), want)
        else:
            back, oldest = self._history_back(chat_id, position(marks["before"]) if "before" in marks else None, want)
        rows = back + fwd
        if not rows:
            return {}  # nothing there: the start (or, going forward, nothing newer yet)
        me = self.me_name()
        page: Dict[str, Any] = {"messages": [views.archive_message(r, me, self.platform.handle_kind) for r in rows]}
        low = oldest if back else int(rows[0]["position"])
        if self.store.has_before(chat_id, low):
            page["more"] = True
            page["older"] = str(low)
        high = newest if fwd else int(rows[-1]["position"])
        if self.store.has_after(chat_id, high):
            page["newer"] = str(high)
        return page


def _int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    if n <= 0 and lo > 0:
        return default
    return max(lo, min(hi, n))
