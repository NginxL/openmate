"""The hub's HTTP API, with the scoped token the hub gives the extension (initialize hub.url / hub.token, or the
environment's PERSONA_HUB_URL / PERSONA_HUB_TOKEN). Only the routes /agent needs, each under the scope the manifest
asks for (docs/extensions/extensions.md "Hub API: scopes"):

    assistant.ask   POST /api/v1/assistant/conversations, POST …/conversations/{id}/stop
    agents.control  POST /api/v1/sessions/{mid}/{sid}/messages, …/interrupt
    agents.read     GET  /api/v1/sessions/{mid}/{sid}/items, …/items/{item}/full,
                    GET  /api/v1/sessions?machine_id=&session_id= (the session's status)
    kb.read         GET  /api/v1/kb/chats/{id}/context
    inbox.read      GET  /api/v1/inbox (the 今天 page)
    todos.read      GET  /api/v1/todos (the 今天 page)
    (none)          GET  /api/v1/health, the hub's one route without authentication: is the hub reachable now?

The manager relays these to the hub. While its bus connection is down it answers 503 manager_offline (nothing was sent);
when the request to the hub breaks it answers 502 hub_unreachable, and the hub may or may not have got it.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

_LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


# Codes for failures on this side of the relay (status 0): the connection was refused, so nothing was sent; or it
# broke or timed out after it was made, so the request may have arrived.
NOT_CONNECTED = "not_connected"
NETWORK = "network"

UNSENT = ("manager_offline", NOT_CONNECTED)
UNREACHABLE = UNSENT + ("hub_unreachable", NETWORK)  # the hub itself was not reached
# The hub's answers for a call to a machine that broke or timed out on the way (apierr.Call): it may have run.
MACHINE_UNSURE = ("node_unreachable", "timeout")


class HubError(Exception):
    def __init__(self, status: int, message: str, code: str = "", retryable: Optional[bool] = None,
                 delivered: str = ""):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.retryable = retryable  # the hub's "retryable" and "delivered" ("none": nothing ran), when it said them
        self.delivered = delivered

    @property
    def unreachable(self) -> bool:
        """The hub could not be reached (a network failure, not the hub's answer)."""
        return self.code in UNREACHABLE

    @property
    def unsent(self) -> bool:
        """Nothing ran: it never reached the hub, or the hub says nothing ran and it may be tried again (the machine
        is offline). Sending it again cannot do it twice."""
        return self.code in UNSENT or (self.delivered == "none" and self.retryable is True)

    @property
    def transient(self) -> bool:
        """A network failure on the way (to the hub, or from the hub to the machine): it may work a little later.
        Unless unsent, it may also have run."""
        return self.unsent or self.unreachable or (self.code in MACHINE_UNSURE and self.status >= 500)


class HubAPI:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0):
        self.base = (base_url or "").rstrip("/")
        self.token = token or ""
        self.timeout = timeout
        host = urllib.parse.urlsplit(self.base).hostname or ""
        handlers = []
        if host in _LOOPBACK:  # the hub on this machine: never through a proxy
            handlers.append(urllib.request.ProxyHandler({}))
        self._opener = urllib.request.build_opener(*handlers)

    @property
    def available(self) -> bool:
        return bool(self.base and self.token)

    def request(self, method: str, path: str, body: Optional[dict] = None, query: Optional[Dict[str, Any]] = None,
                timeout: Optional[float] = None) -> Any:
        if not self.available:
            raise HubError(0, "没有 Persona hub 的地址或令牌")
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        data = None
        # the hub words its errors in the request's language; they are shown in the chat
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json", "Accept-Language": "zh-CN"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raw = b""
            try:
                raw = e.read()
            except Exception:
                pass
            finally:
                e.close()
            message, code, retryable, delivered = "", "", None, ""
            try:
                doc = json.loads(raw or b"{}")
                if isinstance(doc, dict):
                    message = str(doc.get("error") or doc.get("message") or "")
                    code = str(doc.get("code") or "")
                    if isinstance(doc.get("retryable"), bool):
                        retryable = doc["retryable"]
                    delivered = str(doc.get("delivered") or "")
            except ValueError:
                pass
            raise HubError(e.code, message or ("hub 回答 HTTP %d" % e.code), code, retryable, delivered)
        except (urllib.error.URLError, OSError) as e:
            cause = getattr(e, "reason", e) if isinstance(e, urllib.error.URLError) else e
            refused = isinstance(cause, (ConnectionRefusedError, socket.gaierror))  # not connected: nothing was sent
            raise HubError(0, "连不上 Persona hub（%s）" % type(cause).__name__, NOT_CONNECTED if refused else NETWORK)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise HubError(0, "hub 的回答不是 JSON")

    # ---- the routes ----

    def health(self, timeout: float = 10.0) -> None:
        """Raises HubError unless the hub answers now."""
        self.request("GET", "/api/v1/health", timeout=timeout)

    def capabilities(self) -> dict:
        """What the assistant offers ({"guest_conversations": true}); {} from a hub that doesn't say (404)."""
        try:
            return self.request("GET", "/api/v1/assistant/capabilities", timeout=15) or {}
        except HubError as e:
            if e.status == 404:
                return {}
            raise

    def start_conversation(self, prompt: str, purpose: str, report_tasks: bool, machine_id: str = "", launcher: str = "",
                           guest_tools: Optional[List[str]] = None) -> dict:
        """A conversation; with guest_tools a guest one (docs/assistant/assistant.md "Guest conversations"): it answers
        someone else for the user, with none of the assistant's powers but these connector tools."""
        body: Dict[str, Any] = {"prompt": prompt, "purpose": purpose, "report_tasks": bool(report_tasks)}
        if guest_tools:
            body["guest"] = {"tools": list(guest_tools)}
        if machine_id:
            body["machine_id"] = machine_id
        if launcher:
            body["launcher"] = launcher
        out = self.request("POST", "/api/v1/assistant/conversations", body, timeout=120)
        sess = (out or {}).get("session") or {}
        if not sess.get("session_id"):
            raise HubError(0, "hub 没有给出会话 id")
        return sess

    def send_message(self, machine_id: str, session_id: str, text: str, client_msg_id: str = "") -> dict:
        body: Dict[str, Any] = {"text": text}
        if client_msg_id:
            body["client_msg_id"] = client_msg_id
        return self.request("POST", "/api/v1/sessions/%s/%s/messages" % (_seg(machine_id), _seg(session_id)), body, timeout=120)

    def items(self, machine_id: str, session_id: str, after_seq: Optional[int] = None) -> dict:
        query = {"after_seq": after_seq} if after_seq is not None else {"turns": 1}
        return self.request("GET", "/api/v1/sessions/%s/%s/items" % (_seg(machine_id), _seg(session_id)), query=query)

    def item_full(self, machine_id: str, session_id: str, item_id: str) -> dict:
        return self.request("GET", "/api/v1/sessions/%s/%s/items/%s/full" % (_seg(machine_id), _seg(session_id), _seg(item_id)))

    def session_row(self, machine_id: str, session_id: str) -> Optional[dict]:
        """The session's row in the session list (status, state, updated_at), None when the list has no such row."""
        res = self.request("GET", "/api/v1/sessions", query={"machine_id": machine_id, "session_id": session_id,
                                                             "summary": 1, "limit": 1}, timeout=15) or {}
        for row in res.get("sessions") or []:
            if isinstance(row, dict) and row.get("session_id") == session_id:
                return row
        return None

    def stop_conversation(self, session_id: str) -> dict:
        return self.request("POST", "/api/v1/assistant/conversations/%s/stop" % _seg(session_id), {})

    def interrupt(self, machine_id: str, session_id: str) -> dict:
        return self.request("POST", "/api/v1/sessions/%s/%s/interrupt" % (_seg(machine_id), _seg(session_id)), {})

    def kb_chat_context(self, kb_chat_id: str) -> Optional[dict]:
        try:
            return self.request("GET", "/api/v1/kb/chats/%s/context" % _seg(kb_chat_id), timeout=15)
        except HubError as e:
            if e.status == 404:
                return None
            raise


def _seg(s: str) -> str:
    return urllib.parse.quote(str(s), safe="")
