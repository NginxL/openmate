"""The extension's own HTTP, for another OpenMate extension's 今天 (Feishu's shows WeChat's chats). The hub forwards
`/api/v1/extensions/<id>/<path>` here (runtime.http, PERSONA_EXTENSION_SOCKET); the manager drops every X-Persona-*
a caller sent and adds X-Persona-Role and X-Persona-Device ("extension:<its id>" for another extension that holds
`extensions.call:<id>`). Standard library only.

    GET  /health   the host's probe, without a caller's identity: 200 once the server is up
    GET  /today    where the account stands, and when it is signed in: the needs (as ChatsToday counts them), the
                   counts, today's active groups
    GET  /chat?id=<chat>&account=<id>   a chat that needs the user: its latest lines
    POST /done {"chat", "version", "account"}   处理完 for that version of the need (a newer one stays)

Every route but /health answers only the callers of Platform.today_api_callers. Requests are small and few: at most
MOST_AT_ONCE at a time, each read within READ_WAIT, bodies of at most BODY_MOST bytes.
"""

from __future__ import annotations

import json
import logging
import os
import socketserver
import stat
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict, Optional, Tuple

from . import views
from .textutil import one_line
from .ui import ChatsToday, account_epoch, day_start, owned

log = logging.getLogger("openmate.http")

BODY_MOST = 4096
DRAIN_MOST = 65536  # a body this long at most is read before it is refused (one longer: refused unread)
MOST_AT_ONCE = 4
READ_WAIT = 10.0  # a connection that sends nothing this long is closed
NEEDS_MOST = 200  # as many as ChatsToday looks at (Timing.inbox_most): the counts and the list agree
GROUPS_MOST = 5
LINES_MOST = 30
TEXT_MOST = 500
DRAIN_WAIT = 3.0  # shutdown waits this long at most for the requests in flight


class Refused(Exception):
    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(message or code)
        self.status, self.code, self.message = status, code, message or code


def link_state(app) -> str:
    """signed_out | loading | error | expired | up | down: what the account's sign-in and live link are now."""
    if app.creds == "loading":
        return "loading"
    if app.creds == "error":
        return "error"
    if app.creds != "present" or app.store is None:
        return "signed_out"
    if app.link == "expired":
        return "expired"
    return "up" if app.link == "up" else "down"


def _account(app) -> str:
    acct = getattr(app, "account", None)
    return str(getattr(acct, "id", "") or "")


def today(app) -> dict:
    """GET /today: one ChatsToday read, the same rules as this extension's own chats page."""
    state = link_state(app)
    out: Dict[str, Any] = {"state": state, "platform": app.platform.name, "account": _account(app)}
    if state in ("signed_out", "loading", "error"):
        return out
    data = ChatsToday(app)
    needs = data.needs()
    out["counts"] = data.counts(needs)
    out["day"] = day_start(app.tz)
    out["needs"] = [{"chat": t["_chat"]["id"], "kind": t["kind"], "title": one_line(str(t.get("title") or ""), 80),
                     "sender": one_line(str(t.get("sender") or ""), 40), "preview": one_line(str(t.get("preview") or ""), 200),
                     "at": float(t.get("_at") or 0), "version": t["_version"]} for t in needs[:NEEDS_MOST]]
    me = app.me_name()
    out["groups"] = [{"chat": chat["id"], "title": one_line(views.chat_name(chat, chat["id"]), 80),
                      "sender": one_line(views.sender_of(last, me), 40), "preview": one_line(views.text_of(last), 200),
                      "at": float(last.get("at") or 0)} for chat, last in data.active_groups()[:GROUPS_MOST]]
    return out


def _signed_in_as(app, account: str) -> None:
    if link_state(app) in ("signed_out", "loading", "error"):
        raise Refused(409, "signed_out", "%s没有登录" % app.platform.name)
    if not account or account != _account(app):
        raise Refused(409, "account_changed", "%s换了账号，请刷新" % app.platform.name)


def chat(app, chat_id: str, account: str, most: int) -> dict:
    """GET /chat: a chat that needs the user now (only those: the 今天 page opens nothing else), its latest lines.
    One marked 处理完 is no longer one: 404."""
    _signed_in_as(app, account)
    t = next((t for t in ChatsToday(app).needs(only={chat_id}) if t["_chat"]["id"] == chat_id), None)
    if t is None:
        raise Refused(404, "not_found", "这个会话现在没有要处理的了")
    rows = [r for r in app.store.messages_before(chat_id, None, max(1, min(LINES_MOST, most)))
            if views.has_content(r) and not views.is_system(r)]
    me = app.me_name()
    return {"chat": chat_id, "title": one_line(str(t.get("title") or ""), 80), "kind": t["kind"], "version": t["_version"],
            "need": True,
            "lines": [{"who": "我" if r.get("mine") else views.sender_of(r, me), "mine": bool(r.get("mine")),
                       "text": views.text_of(r)[:TEXT_MOST], "at": float(r.get("at") or 0)} for r in rows]}


def done(app, body: dict) -> dict:
    """POST /done: 处理完 for the need's version the caller saw. Already so: ok again. A newer one: 409 changed."""
    chat_id, version, account = (str(body.get(k) or "") for k in ("chat", "version", "account"))
    if not chat_id or not version:
        raise Refused(400, "invalid_params", "要给出 chat 和 version")
    epoch = account_epoch(app)  # first: the account checked next is this generation's, and its mark is kept for it
    _signed_in_as(app, account)
    key = "chat:" + chat_id
    marked = app.store.dismissed([key]).get(key)  # read before the need: a mark written after this makes the write fail
    data = ChatsToday(app)
    needs = data.needs(only={chat_id})
    if any(t["_chat"]["id"] == chat_id and t["_version"] == version for t in data.done):
        with owned(app, epoch) as ok:
            if not ok:
                raise Refused(409, "account_changed", "%s换了账号，请刷新" % app.platform.name)
        return {"ok": True}
    t = next((t for t in needs if t["_chat"]["id"] == chat_id), None)
    if t is None:
        raise Refused(404, "not_found", "这个会话现在没有要处理的了")
    if t["_version"] != version:
        raise Refused(409, "changed", "有新消息，已刷新")
    with owned(app, epoch) as ok:
        if not ok:
            raise Refused(409, "account_changed", "%s换了账号，请刷新" % app.platform.name)
        if not app.store.dismiss_if(key, version, marked):
            raise Refused(409, "changed", "刚刚有了变化，已刷新")
    if hasattr(app, "mark_changed"):
        app.mark_changed("inbox")
    return {"ok": True}


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class TodayAPI:
    """The server on a Unix socket; start() binds and serves in a thread, stop() stops taking requests, waits a
    little for the ones in flight and removes the socket it bound."""

    def __init__(self, app, path: str, callers: Tuple[str, ...]):
        self.app, self.path, self.callers = app, path, tuple(callers)
        self._server: Optional[_Server] = None
        self._slots = threading.BoundedSemaphore(MOST_AT_ONCE)
        self._busy = 0
        self._mu = threading.Condition()
        self._ino: Optional[int] = None

    def start(self) -> None:
        api = self

        class Handler(BaseHTTPRequestHandler):
            timeout = READ_WAIT
            protocol_version = "HTTP/1.0"
            server_version = "OpenMate"
            sys_version = ""

            def address_string(self) -> str:  # a Unix socket's peer has no address
                return "unix"

            def log_message(self, fmt, *args) -> None:
                log.debug("http %s", fmt % args)

            def do_GET(self) -> None:
                api._handle(self, "GET")

            def do_POST(self) -> None:
                api._handle(self, "POST")

        try:
            if stat.S_ISSOCK(os.lstat(self.path).st_mode):
                os.unlink(self.path)  # a socket left by an earlier run (the host removes it too); nothing else
        except OSError:
            pass
        server = _Server(self.path, Handler)  # OSError when the path is taken: no API, the program goes on
        try:
            os.chmod(self.path, 0o600)
            st = os.lstat(self.path)
            if not stat.S_ISSOCK(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.getuid():
                raise OSError("the socket is not this user's alone")
            self._ino = st.st_ino
        except OSError:
            server.server_close()
            raise
        self._server = server
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, name="openmate-http",
                         daemon=True).start()
        log.info("serving the 今天 API on the extension's socket")

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.shutdown()
        deadline = time.monotonic() + DRAIN_WAIT
        with self._mu:
            while self._busy and time.monotonic() < deadline:
                self._mu.wait(max(0.0, deadline - time.monotonic()))
        server.server_close()
        try:
            if self._ino is not None and os.stat(self.path).st_ino == self._ino:
                os.unlink(self.path)  # only the node this one bound
        except OSError:
            pass

    # ---- one request

    def _handle(self, h: BaseHTTPRequestHandler, method: str) -> None:
        url = urllib.parse.urlsplit(h.path)
        route = url.path
        if method == "GET" and route == "/health":
            return self._send(h, 200, {"ok": True})
        if not self._slots.acquire(blocking=False):
            return self._send(h, 503, {"code": "busy", "message": "请求太多，稍后再试"})
        with self._mu:
            self._busy += 1
        try:
            self._send(h, 200, self._route(h, method, route, url.query))
        except Refused as e:
            self._send(h, e.status, {"code": e.code, "message": e.message})
        except Exception:
            log.exception("a request to the 今天 API failed")
            self._send(h, 500, {"code": "internal", "message": "出错了"})
        finally:
            self._slots.release()
            with self._mu:
                self._busy -= 1
                self._mu.notify_all()

    def _route(self, h: BaseHTTPRequestHandler, method: str, route: str, query: str) -> dict:
        role = h.headers.get("X-Persona-Role", "")
        device = h.headers.get("X-Persona-Device", "")
        if role != "extension" or device not in self.callers:
            raise Refused(403, "permission_denied", "只有指定的扩展能调用")
        app = self.app
        if app.store is None or getattr(app, "closing", threading.Event()).is_set():
            raise Refused(503, "not_ready", "扩展还在启动")
        q = urllib.parse.parse_qs(query)
        if method == "GET" and route == "/today":
            return today(app)
        if method == "GET" and route == "/chat":
            try:
                most = int((q.get("n") or ["20"])[0])
            except ValueError:
                most = 20
            return chat(app, (q.get("id") or [""])[0], (q.get("account") or [""])[0], most)
        if method == "POST" and route == "/done":
            return done(app, self._body(h))
        raise Refused(404, "not_found", "没有这个接口")

    @staticmethod
    def _body(h: BaseHTTPRequestHandler) -> dict:
        """The request's JSON object. Its body is read (up to DRAIN_MOST) before any refusal: a caller still
        sending it would otherwise meet a closed connection instead of the answer."""
        try:
            n = int(h.headers.get("Content-Length") or "")
        except ValueError:
            raise Refused(411, "length_required", "要给出 Content-Length")
        if n < 0 or n > DRAIN_MOST:
            raise Refused(413, "too_large", "请求太大")
        raw = h.rfile.read(n)
        if n > BODY_MOST:
            raise Refused(413, "too_large", "请求太大")
        if "json" not in (h.headers.get("Content-Type") or "").lower():
            raise Refused(415, "unsupported_media_type", "要 JSON")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise Refused(400, "invalid_json", "不是 JSON")
        if not isinstance(body, dict):
            raise Refused(400, "invalid_json", "要一个 JSON 对象")
        return body

    @staticmethod
    def _send(h: BaseHTTPRequestHandler, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            h.send_response(status)
            h.send_header("Content-Type", "application/json; charset=utf-8")
            h.send_header("Content-Length", str(len(data)))
            h.send_header("Cache-Control", "no-store")
            h.end_headers()
            h.wfile.write(data)
        except OSError:
            pass  # the caller went away
