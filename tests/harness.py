"""Test harness: the connector app driven over the real JSON-line protocol (a pair of OS pipes), with the fixture
backend and a fake hub on both of its channels: the stdio peer (secrets.*, cli.call, events) and an in-process HTTP
server for the hub API (/agent's routes)."""

from __future__ import annotations

import base64
import http.server
import itertools
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "core"), os.path.join(ROOT, "extensions", "openmate-feishu")):
    if p not in sys.path:
        sys.path.insert(0, p)

import openmate_feishu  # noqa: E402
from openmate_core.app import ConnectorApp  # noqa: E402
from openmate_core.fixture import FixtureBackend, FixtureWorld  # noqa: E402
from openmate_core.platform import Timing  # noqa: E402
from openmate_core.wire import Peer  # noqa: E402

CID = "matefeishu"
EXT = "openmate-feishu"
TOKEN = "pext_test_token"


def fast(**kw) -> Timing:
    base = dict(debounce=0.15, backoff_min=0.05, backoff_max=0.2, stable_after=0.3, login_wait=3.0, agent_poll=0.05,
                agent_timeout=60.0, agent_hub_wait=5.0, agent_hub_probe=0.05, agent_settle=0.1, secrets_retry=0.05,
                sent_retry=(0.05, 0.05, 0.05), warm_pages=False, agent_model_wait=1.0)
    if "channel_poll" in Timing.__dataclass_fields__:  # the channel's (older code has none: its tests fail elsewhere)
        base.update(channel_poll=0.05, channel_retry=(0.05, 0.05), channel_unseen=0.3)
    base.update(kw)
    return Timing(**base)


def wait_until(cond: Callable[[], Any], timeout: float = 5.0, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    while True:
        v = cond()
        if v:
            return v
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for " + what)
        time.sleep(0.01)


class HubSideError(Exception):
    def __init__(self, code: str, message: str, data: Optional[dict] = None):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message
        self.data = data


def _sends_as_user() -> set:
    """The connector ids the extensions' manifests say send as the user (what the hub reads at install)."""
    out = set()
    base = os.path.join(ROOT, "extensions")
    for ext in sorted(os.listdir(base)):
        path = os.path.join(base, ext, "persona-extension.json")
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
        out |= {c["id"] for c in (m.get("contributes") or {}).get("connectors") or [] if c.get("sends_as_user") is True}
    return out


class StdioHub:
    """The hub's end of the pipes: calls the program, answers its calls, keeps its events."""

    def __init__(self, rfile, wfile):
        self._r, self._w = rfile, wfile
        self._wl = threading.Lock()
        self._pending: Dict[str, dict] = {}
        self._cv = threading.Condition()
        self._ids = itertools.count(1)
        self.secrets: Dict[str, str] = {}
        self.secrets_fail = False
        self.secrets_delay = 0.0
        self.save_hook: Optional[Callable[[dict], None]] = None  # run before a secrets.save is stored (to delay one)
        self.cli: Optional[Callable[[dict], Any]] = None  # params → result; raise HubSideError for an error answer
        self.sent_records: List[dict] = []  # sent.record calls the hub accepted
        self.sent_calls = 0
        self.sent_answer: Optional[Callable[[dict], Any]] = None  # before accepting: raise HubSideError to refuse
        self.received: List[dict] = []  # channel.receive calls the hub took (the owner's messages handed over)
        self.receive_answer: Optional[Callable[[dict], Any]] = None  # before taking one: raise HubSideError to refuse
        self.receive_calls = 0
        self.calls: List[tuple] = []
        self.events: List[tuple] = []
        self.raw_lines: List[bytes] = []
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _send(self, obj: dict) -> None:
        with self._wl:
            try:
                self._w.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
                self._w.flush()
            except (ValueError, OSError):  # the program's side closed (a test ending): nobody to answer
                pass

    def _read(self) -> None:
        for line in self._r:
            self.raw_lines.append(line)
            msg = json.loads(line)
            if "method" in msg:
                threading.Thread(target=self._answer, args=(msg,), daemon=True).start()
            elif "event" in msg:
                with self._cv:
                    self.events.append((time.monotonic(), msg["event"], msg.get("params")))
                    self._cv.notify_all()
            else:
                with self._cv:
                    self._pending[str(msg["id"])] = msg
                    self._cv.notify_all()

    def _answer(self, msg: dict) -> None:
        method, params = msg["method"], msg.get("params") or {}
        self.calls.append((method, params))
        try:
            if method.startswith("connector.secrets."):
                if self.secrets_delay:
                    time.sleep(self.secrets_delay)
                if self.secrets_fail:
                    raise HubSideError("unavailable", "secrets unavailable")
                c = params.get("contribution")
                if method.endswith(".load"):
                    result = {"data": self.secrets[c]} if c in self.secrets else {}
                elif method.endswith(".save"):
                    if self.save_hook is not None:
                        self.save_hook(secret_json(params["data"]))
                    self.secrets[c] = params["data"]
                    result = {}
                else:
                    self.secrets.pop(c, None)
                    result = {}
            elif method == "sent.record":
                # The hub's checks, in its order (docs/extensions/extensions.md "Sent as the user: sent.record").
                with self._cv:
                    self.sent_calls += 1
                if self.sent_answer is not None:
                    self.sent_answer(params)
                if params.get("contribution") not in _sends_as_user():
                    raise HubSideError("permission_denied", "this connector does not declare sends_as_user")
                if not isinstance(params.get("text"), str):
                    raise HubSideError("invalid_params", "sent.record: no text")
                if not isinstance(params.get("target"), dict):
                    raise HubSideError("invalid_params", "sent.record: target is not an object")
                with self._cv:
                    self.sent_records.append(params)
                    self._cv.notify_all()
                result = {}
            elif method == "channel.receive":
                # docs/extensions/channels.md "Protocol": the owner's message, deduplicated by its id
                with self._cv:
                    self.receive_calls += 1
                if self.receive_answer is not None:
                    self.receive_answer(params)
                m = params.get("message")
                if not isinstance(m, dict) or not isinstance(m.get("id"), str) or not m["id"]:
                    raise HubSideError("invalid_params", "channel.receive: message.id")
                with self._cv:
                    if not any(r["message"]["id"] == m["id"] and r.get("contribution") == params.get("contribution")
                               for r in self.received):
                        self.received.append(params)
                    self._cv.notify_all()
                result = {}
            elif method == "cli.call":
                if self.cli is None:
                    raise HubSideError("permission_denied", "host CLI: integration is not enabled by the manager")
                result = self.cli(params)
            else:
                raise HubSideError("unknown_method", "unknown method " + method)
            self._send({"id": msg["id"], "result": result})
        except HubSideError as e:
            err = {"code": e.code, "message": e.message} if e.code else {"message": e.message}
            if e.data is not None:
                err["data"] = e.data
            self._send({"id": msg["id"], "error": err})

    def request(self, method: str, params: dict, timeout: float = 15.0) -> Any:
        rid = "h%d" % next(self._ids)
        self._send({"id": rid, "method": method, "params": params})
        with self._cv:
            ok = self._cv.wait_for(lambda: rid in self._pending, timeout)
            if not ok:
                raise AssertionError("no answer to " + method)
            msg = self._pending.pop(rid)
        if "error" in msg:
            raise HubSideError(msg["error"].get("code", ""), msg["error"].get("message", ""))
        return msg["result"]

    def changed(self, scope: Optional[str] = None) -> List[tuple]:
        with self._cv:
            return [e for e in self.events if e[1] == "connector.changed" and (scope is None or e[2]["scope"] == scope)]

    def close(self) -> None:
        try:
            self._w.close()
        except OSError:
            pass


class FakeHubHTTP:
    """The hub's HTTP API, as far as /agent uses it."""

    def __init__(self, token: str = TOKEN):
        self.token = token
        self.lock = threading.RLock()
        self.requests: List[dict] = []
        self.sessions: Dict[str, dict] = {}
        self.model_late = 0  # the session list leaves the model out of this many answers
        self.no_guest = False  # an older hub: no /api/v1/assistant/capabilities
        self.kb: Dict[str, dict] = {}
        self.full: Dict[str, str] = {}  # item id → full text
        self.stop_status: Dict[str, int] = {}  # session id → HTTP status for …/stop
        self.start_fail: Optional[int] = None
        self.offline = 0  # the next this many requests (any route) get the manager's 503 manager_offline
        self.lost_starts = 0  # the next this many conversation starts open a conversation, but the answer is lost (502)
        self.lost_code = "hub_unreachable"  # … with this code (the relay's, or the hub's node_unreachable)
        self.machine_offline = 0  # the next this many starts find the machine offline (503, nothing ran; kept as the receipt)
        self.lost_sends = 0  # the next this many messages are typed, but the answer is lost (502)
        self.ops: Dict[str, dict] = {}  # X-Persona-Op-Id of conversation starts → their receipt (assistant_start_ops)
        self.start_pending = 0  # the next this many asks about a start find it still under way (202 pending)
        self.start_unknown = False  # the next start ends with nobody knowing whether it started (202 unknown)
        self.todos: List[dict] = []  # GET /api/v1/todos (todos.read): the to-dos, as the hub lists them
        self.todos_status: Optional[int] = None  # an HTTP status GET /api/v1/todos answers instead (403: no todos.read)
        self.todos_delay = 0.0  # seconds GET /api/v1/todos takes
        self.send_refused: Dict[str, Any] = {}  # session id → status, or (status, code, delivered), its messages get
        self.send_hook: Dict[str, Any] = {}  # session id → fn() run (once) as a message to it arrives; may answer for it
        self.lost_send_status = (502, "hub_unreachable")  # what a lost send (lost_sends) answers
        self.interactions: List[dict] = []  # GET /api/v1/interactions (agents.read): the cards waiting for an answer
        self.interactions_status: Optional[int] = None  # an HTTP status that route answers instead
        self.history: List[dict] = []  # GET /api/v1/interactions/history: [{interaction: {id, machine_id, …}, status}]
        # placement (machines.read, assistant.ask): None leaves the route out (an older hub, or no permission: 404)
        self.assistant_mid: Optional[str] = None  # GET /api/v1/assistant's machine_id
        self.machine_list: Optional[List[dict]] = None  # GET /api/v1/machines: [{id, online}]
        self.metrics: Dict[str, dict] = {}  # machine id → GET /api/v1/machines/{id}/metrics's metrics
        # machine id → how many sessions it has working a turn (GET /api/v1/sessions?state=working lists them, with a
        # session done but unseen beside each, as the hub's working list holds those too); None: that list refused (403)
        self.working: Optional[Dict[str, int]] = {}
        self.working_more = False  # that list says it has more than one page
        self.refuse_on: Dict[str, Any] = {}  # machine id → (status, code, delivered) a start there is refused with
        self._n = itertools.count(1)
        hub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, status: int, obj: Any) -> None:
                raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self, method: str) -> None:
                u = urllib.parse.urlsplit(self.path)
                query = dict(urllib.parse.parse_qsl(u.query))
                body = None
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    body = json.loads(self.rfile.read(n))
                rec = {"method": method, "path": urllib.parse.unquote(u.path), "query": query, "body": body,
                       "auth": self.headers.get("Authorization"), "lang": self.headers.get("Accept-Language"),
                       "op": self.headers.get("X-Persona-Op-Id") or ""}
                with hub.lock:
                    hub.requests.append(rec)
                if rec["auth"] != "Bearer " + hub.token:
                    return self._reply(401, {"error": "bad token"})
                status, obj = hub.route(method, rec["path"].split("/")[1:], query, body, rec["op"])
                self._reply(status, obj)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # ---- transcript control ----

    def add(self, sid: str, role: str, kind: str, text: str = "", **kw) -> dict:
        with self.lock:
            s = self.sessions[sid]
            seq = (s["items"][-1]["seq"] + 1) if s["items"] else 1
            it = {"id": "it%d#0" % seq, "seq": seq, "ts": "2026-11-15T00:00:00Z", "role": role, "kind": kind}
            if text:
                it["text"] = text
            it.update(kw)
            s["items"].append(it)
            return it

    def end_turn(self, sid: str) -> None:
        self.add(sid, "system", "turn_duration", turn_duration={"duration_ms": 1000})
        with self.lock:
            self.sessions[sid]["working"] = False

    def finish(self, sid: str) -> None:
        """The turn ends without a turn_duration item (Claude Code does not always write one): only the status says."""
        with self.lock:
            self.sessions[sid]["working"] = False

    def add_agent(self, sid: str, machine: str = "mac-agent", working: bool = True) -> dict:
        """An Agent's session a task runs in (not a conversation /agent started)."""
        with self.lock:
            s = self.sessions[sid] = {"machine": machine, "items": [], "working": working, "stopped": False}
            return s

    def add_todo(self, todo_id: str, origin: str, agents, title: str = "小任务", **kw) -> dict:
        """A to-do the conversation `origin` dispatched, with its Agents [(machine, session)] (active ones)."""
        t = {"id": todo_id, "title": title, "origin": origin, "done_at": None,
             "agents": [{"machine_id": m, "session_id": s, "linked_at": "2026-11-19T00:00:00Z", "source": "spawned"}
                        for m, s in agents]}
        t.update(kw)
        with self.lock:
            self.todos.append(t)
        return t

    def _todo_of(self, mid: str, sid: str) -> Optional[dict]:
        for t in self.todos:
            if t.get("done_at"):
                continue
            for a in t.get("agents") or []:
                if (a.get("machine_id"), a.get("session_id")) == (mid, sid) and not a.get("released_at") \
                        and not a.get("dismissed_at"):
                    return t
        return None

    def messages_to(self, mid: str, sid: str) -> List[dict]:
        return self.posts("/api/v1/sessions/%s/%s/messages" % (mid, sid))

    def session_rows(self) -> List[dict]:
        with self.lock:
            return [r for r in self.requests if r["method"] == "GET" and r["path"] == "/api/v1/sessions"]

    def interactions_asked(self) -> int:
        with self.lock:
            return sum(1 for r in self.requests if r["method"] == "GET" and r["path"] == "/api/v1/interactions")

    def starts(self) -> List[dict]:
        with self.lock:
            return [r for r in self.requests if r["method"] == "POST" and r["path"] == "/api/v1/assistant/conversations"]

    def posts(self, suffix: str) -> List[dict]:
        with self.lock:
            return [r for r in self.requests if r["method"] == "POST" and r["path"].endswith(suffix)]

    # ---- routes ----

    def route(self, method: str, parts: List[str], query: dict, body: Any, op: str = ""):
        p = "/" + "/".join(parts)
        with self.lock:
            if self.offline > 0:
                self.offline -= 1
                return 503, {"error": "the manager is not connected to the hub right now: try again shortly",
                             "code": "manager_offline"}
            if method == "GET" and p == "/api/v1/health":
                return 200, {"ok": True}
            if method == "GET" and p == "/api/v1/assistant/capabilities":
                return (404, {"error": "no route"}) if self.no_guest else (200, {"guest_conversations": True})
            if method == "POST" and p == "/api/v1/assistant/conversations":
                if op and op in self.ops:  # a start asked again: its receipt, never a second conversation
                    rec = self.ops[op]
                    if rec["body"] != body:
                        return 409, {"error": "op id reused with other parameters", "code": "op_id_reused"}
                    if rec["state"] == "pending":
                        if self.start_pending > 0:
                            self.start_pending -= 1
                            return 202, {"state": "pending"}
                        rec["state"] = "done"
                    if rec["state"] == "unknown":
                        return 202, {"state": "unknown"}
                    if self.lost_starts > 0:  # the relay loses the answer again
                        self.lost_starts -= 1
                        return 502, {"error": "the manager could not reach the hub", "code": self.lost_code}
                    return rec["status"], rec["answer"]
                if self.start_fail:
                    return self.start_fail, {"error": "refused"}
                refusal_on = self.refuse_on.get((body or {}).get("machine_id") or "")
                if refusal_on:  # that computer cannot take it (a launcher it lacks; away): nothing ran
                    answer = {"error": "这台电脑开不了这个会话", "code": refusal_on[1]}
                    if refusal_on[2]:  # the hub's word that nothing ran (an away node's is also retryable)
                        answer.update(delivered=refusal_on[2], retryable=refusal_on[0] == 503)
                    if op:
                        self.ops[op] = {"body": body, "state": "done", "status": refusal_on[0], "answer": answer}
                    return refusal_on[0], answer
                if self.machine_offline > 0:
                    self.machine_offline -= 1
                    refusal = {"error": "示例电脑离线了", "code": "node_unreachable", "retryable": True, "delivered": "none"}
                    if op:  # as the hub does (clientOpRetry lets only assistant_offline go): the refusal is the receipt
                        self.ops[op] = {"body": body, "state": "done", "status": 503, "answer": refusal}
                    return 503, refusal
                sid = "sess-%d" % next(self._n)
                mid = (body or {}).get("machine_id") or "mac-example"
                self.sessions[sid] = {"machine": mid, "items": [], "reporter": bool((body or {}).get("report_tasks")),
                                      "working": True, "stopped": False}
                self.add(sid, "user", "text", (body or {}).get("prompt") or "")
                answer = {"session": {"machine_id": mid, "session_id": sid, "title": ""}}
                if op:
                    self.ops[op] = {"body": body, "state": "done", "status": 200, "answer": answer}
                    if self.start_unknown:
                        self.start_unknown = False
                        self.ops[op]["state"] = "unknown"
                        return 202, {"state": "unknown"}
                    if self.start_pending > 0:
                        self.start_pending -= 1
                        self.ops[op]["state"] = "pending"
                        return 202, {"state": "pending"}
                if self.lost_starts > 0:
                    self.lost_starts -= 1
                    return 502, {"error": "the manager could not reach the hub", "code": self.lost_code}
                return 200, answer
            if method == "POST" and len(parts) == 6 and parts[:3] == ["api", "v1", "assistant"] and parts[5] == "stop":
                sid = parts[4]
                if sid in self.stop_status:
                    return self.stop_status[sid], {"error": "not a reporting conversation"}
                s = self.sessions.get(sid)
                if s is None:
                    return 404, {"error": "unknown"}
                was = s["working"] and not s["stopped"]
                s["stopped"], s["working"] = True, False
                tasks = s.get("tasks") or []
                return 200, {"conversation": was, "tasks": tasks, "already": not was and not tasks}
            if method == "GET" and p == "/api/v1/assistant" and self.assistant_mid is not None:
                return 200, {"assistant": {"machine_id": self.assistant_mid}}
            if method == "GET" and p == "/api/v1/machines" and self.machine_list is not None:
                return 200, {"machines": [dict(m) for m in self.machine_list]}
            if method == "GET" and len(parts) == 5 and parts[:3] == ["api", "v1", "machines"] and parts[4] == "metrics":
                if parts[3] in self.metrics:
                    return 200, {"metrics": dict(self.metrics[parts[3]]), "range": query.get("range"), "step": 10}
                return 404, {"error": "unknown machine"}
            if method == "GET" and p == "/api/v1/assistant/reporters":
                return 200, {"reporters": [dict({"session_id": sid, "machine_id": s["machine"], "at": "2026-11-19T00:00:00Z"},
                                                **({"stopped_at": "2026-11-19T00:00:01Z", "stopped_by": "lark"}
                                                   if s.get("stopped") else {}))
                                           for sid, s in self.sessions.items() if s.get("reporter")]}
            if method == "GET" and p == "/api/v1/interactions/history":
                # newest first, paged by limit with a cursor (the hub's next_before / next_before_id: here a position)
                recs = [dict(r) for r in reversed(self.history)
                        if r["interaction"].get("machine_id") == query.get("machine_id")
                        and r["interaction"].get("session_id") == query.get("session_id")]
                start = int(query.get("before_id") or 0) if query.get("before") else 0
                limit = int(query.get("limit") or 500)
                page = recs[start:start + limit]
                out = {"interactions": page}
                if start + limit < len(recs):
                    out.update(next_before="2026-11-19T00:00:00Z", next_before_id=str(start + limit))
                return 200, out
            if method == "GET" and p == "/api/v1/interactions":
                if self.interactions_status:
                    return self.interactions_status, {"error": "refused"}
                return 200, {"interactions": [dict(i) for i in self.interactions]}
            if method == "GET" and p == "/api/v1/todos":
                if self.todos_delay:
                    self.lock.release()
                    try:
                        time.sleep(self.todos_delay)
                    finally:
                        self.lock.acquire()
                if self.todos_status:
                    return self.todos_status, {"error": "refused", "code": "forbidden"}
                return 200, {"todos": [dict(t) for t in self.todos]}
            if method == "GET" and p == "/api/v1/sessions" and query.get("state") == "working" \
                    and not query.get("session_id"):
                if self.working is None:
                    return 403, {"error": "refused", "code": "forbidden"}
                rows = []
                for wmid, n in self.working.items():
                    rows += [{"session_id": "%s-working-%d" % (wmid, i), "machine_id": wmid, "status": "working",
                              "state": "working"} for i in range(n)]
                    rows.append({"session_id": "%s-done" % wmid, "machine_id": wmid, "status": "done",
                                 "state": "working"})
                return 200, {"sessions": rows, "total": len(rows), "has_more": self.working_more}
            if method == "GET" and p == "/api/v1/sessions":
                sid, mid = query.get("session_id"), query.get("machine_id")
                s = self.sessions.get(sid or "")
                if s is None or s["machine"] != mid:
                    return 200, {"sessions": [], "total": 0, "has_more": False}
                status = "offline" if s.get("offline") else "working" if s["working"] else "done"
                state = {"offline": "offline", "working": "working"}.get(status, "unread")
                row = {"session_id": sid, "machine_id": mid, "status": status, "state": state,
                       "agent": s.get("agent", "claude")}  # the Agent is known from the start
                if self.model_late > 0:  # a session just started is listed before its model
                    self.model_late -= 1
                else:
                    row.update(model=s.get("model", "claude-opus-5-5"), effort=s.get("effort", "xhigh"))
                if s.get("background"):
                    row.update(background_working=True, state="working")
                on = self._todo_of(mid, sid)
                if on is not None:  # the hub's decoration: the open to-do the session is active on
                    row["todo"] = {"id": on["id"], "title": on.get("title") or ""}
                return 200, {"sessions": [row],
                             "total": 1, "has_more": False}
            if len(parts) >= 6 and parts[:3] == ["api", "v1", "sessions"]:
                mid, sid = parts[3], parts[4]
                s = self.sessions.get(sid)
                if s is None or s["machine"] != mid:
                    return 404, {"error": "no such session"}
                rest = parts[5:]
                if method == "POST" and rest == ["messages"]:
                    hook = self.send_hook.pop(sid, None)
                    if hook is not None:
                        answer = hook()
                        if answer is not None:
                            return answer
                    if sid in self.send_refused:
                        r = self.send_refused[sid]
                        status, code, delivered = r if isinstance(r, tuple) else (r, "refused", "")
                        if code == "peer_held":  # written, held for approval at its computer
                            self.add(sid, "user", "text", (body or {}).get("text") or "",
                                     client_msg_id=(body or {}).get("client_msg_id") or "")
                        return status, dict({"error": "refused", "code": code}, **({"delivered": delivered} if delivered else {}))
                    kw = {}
                    cmid = (body or {}).get("client_msg_id")
                    if cmid:
                        kw["client_msg_id"] = cmid
                        if any(it.get("client_msg_id") == cmid for it in s["items"]):
                            return 200, {"ok": True}  # the hub sends each client_msg_id once
                    s["working"] = True
                    s["stopped"] = False  # asked again: its task notices are told again (the hub's reporter book)
                    self.add(sid, "user", "text", (body or {}).get("text") or "", **kw)
                    if self.lost_sends > 0:
                        self.lost_sends -= 1
                        status, code = self.lost_send_status
                        return status, {"error": "the answer was lost", "code": code}
                    return 200, {"ok": True}
                if method == "POST" and rest == ["interrupt"]:
                    s["working"] = False
                    s["interrupted"] = True
                    return 200, {"ok": True}
                if method == "GET" and rest == ["items"]:
                    items = s["items"]
                    if "after_seq" in query:
                        after = int(query["after_seq"])
                        page = [it for it in items if it["seq"] > after]
                        more = len(page) > 3
                        return 200, {"items": page[:3], "more_after": more}
                    return 200, {"items": items[-20:], "has_more": len(items) > 20}
                if method == "GET" and len(rest) == 3 and rest[0] == "items" and rest[2] == "full":
                    iid = rest[1]
                    for it in s["items"]:
                        if it["id"] == iid:
                            out = dict(it)
                            out.pop("full_size", None)
                            out["text"] = self.full.get(iid, it.get("text", ""))
                            return 200, out
                    return 404, {"error": "no item"}
            if method == "GET" and len(parts) == 6 and parts[:4] == ["api", "v1", "kb", "chats"] and parts[5] == "context":
                ctx = self.kb.get(parts[4])
                if ctx is None:
                    return 404, {"error": "not found"}
                return 200, ctx
        return 404, {"error": "no route " + p}


class Harness:
    """One run of the program: app + pipes + fake hub. restart() starts another run on the same data, world and hub."""

    def __init__(self, test, config: Optional[dict] = None, timing: Optional[Timing] = None,
                 world: Optional[FixtureWorld] = None, http: Optional[FakeHubHTTP] = None, data_dir: Optional[str] = None,
                 secrets: Optional[Dict[str, str]] = None, hub_url: Optional[str] = None, initialize: bool = True,
                 backend_factory=None, platform=None, env: Optional[Dict[str, str]] = None,
                 hub_capabilities: Optional[List[str]] = None):
        self.test = test
        self.hub_capabilities = hub_capabilities  # initialize's hub.capabilities (None: an older hub sends none)
        self.platform = platform or openmate_feishu.platform()
        self.cid = self.platform.connector_id
        self.ext = self.platform.extension_id
        self.config = dict(config or {})
        self.timing = timing or fast()
        self.world = world or FixtureWorld()
        self.http = http
        if self.http is None:
            self.http = FakeHubHTTP()
            test.addCleanup(self.http.close)
        if data_dir is None:
            data_dir = tempfile.mkdtemp(prefix="openmate-test-")
            test.addCleanup(shutil.rmtree, data_dir, True)
        self.data_dir = data_dir
        self.hub_url = self.http.url if hub_url is None else hub_url
        r1, w1 = os.pipe()
        r2, w2 = os.pipe()
        self._files = [os.fdopen(r1, "rb"), os.fdopen(w2, "wb"), os.fdopen(r2, "rb"), os.fdopen(w1, "wb")]
        factory = backend_factory or self._fixture
        self.app = ConnectorApp(self.platform, timing=self.timing, backend_factory=factory, env=dict(env or {}))
        self.peer = Peer(self._files[0], self._files[1], self.app.handle, on_event=self.app.on_event,
                         after=self.app.after)
        self.app.attach(self.peer)
        self._serving = threading.Thread(target=self._serve, daemon=True)
        self._serving.start()
        self.hub = StdioHub(self._files[2], self._files[3])
        if secrets:
            self.hub.secrets.update(secrets)
        self.closed = False
        test.addCleanup(self.close)
        if initialize:
            self.initialize()

    def _fixture(self, cfg, data_dir) -> FixtureBackend:
        """The example backend, with what the platform it stands in for can do (WeChat: no @s by id, no topic groups)."""
        b = FixtureBackend(self.world)
        if self.platform.name != "飞书":
            b.can_mention = b.topic_groups = False
        return b

    def _serve(self) -> None:
        self.peer.serve()
        self.app.shutdown()
        self._files[1].close()  # the program's stdout: the hub's reader ends

    def initialize(self, **over) -> dict:
        params = {
            "protocol": 1,
            "hub": {"build": 202611130000, "extension_points": {"connectors": 2}, "role": "center",
                    "timezone": "Asia/Shanghai", "locale": "zh-Hans", "url": self.hub_url, "token": TOKEN,
                    "scopes": ["agents.control", "agents.read", "assistant.ask", "kb.read"]},
            "extension": {"id": self.ext, "version": "0.1.0", "dir": ROOT, "data_dir": self.data_dir},
            "config": self.config,
            "secrets": {},
            "granted": {},
            "active": {"connectors": [self.cid]},
        }
        if self.hub_capabilities is not None:
            params["hub"]["capabilities"] = list(self.hub_capabilities)
        params.update(over)
        res = self.hub.request("initialize", params)
        wait_until(self.app.ready.is_set, what="startup")
        return res

    def c(self, method: str, **params) -> Any:
        params.setdefault("contribution", self.cid)
        return self.hub.request("connector." + method, params)

    def status(self) -> dict:
        return self.c("status")

    def tool(self, name: str, **args) -> str:
        return self.c("tools.call", name=self.cid + "_" + name, args=args)["text"]

    def sign_in(self) -> dict:
        p = self.c("login.step", step="", values={})
        for _ in range(5):
            if p.get("done"):
                break
            p = self.c("login.step", step=p["step"])
        assert p.get("done"), p
        wait_until(lambda: self.status()["state"] == "ok", what="status ok")
        return p

    def wait_ok(self) -> None:
        wait_until(lambda: self.status()["state"] == "ok", what="status ok")

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.hub.close()
        self._serving.join(5)
        self.hub.reader.join(5)
        for f in self._files:
            try:
                f.close()
            except OSError:
                pass

    def restart(self, **over) -> "Harness":
        """Stop this run and start the next on the same data directory, world, HTTP hub and saved secrets."""
        self.close()
        kw = dict(config=self.config, timing=self.timing, world=self.world, http=self.http, data_dir=self.data_dir,
                  secrets=dict(self.hub.secrets), hub_url=self.hub_url)
        kw.update(over)
        return Harness(self.test, **kw)

    def sent(self) -> List[dict]:
        with self.world.lock:
            return list(self.world.sent)


def secret_json(data: str) -> dict:
    return json.loads(base64.b64decode(data).decode("utf-8"))
