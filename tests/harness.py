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
                sent_retry=(0.05, 0.05, 0.05))
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
    """The connector ids the extension's manifest says send as the user (what the hub reads at install)."""
    with open(os.path.join(ROOT, "extensions", EXT, "persona-extension.json"), encoding="utf-8") as f:
        m = json.load(f)
    return {c["id"] for c in (m.get("contributes") or {}).get("connectors") or [] if c.get("sends_as_user") is True}


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
        self.cli: Optional[Callable[[dict], Any]] = None  # params → result; raise HubSideError for an error answer
        self.sent_records: List[dict] = []  # sent.record calls the hub accepted
        self.sent_calls = 0
        self.sent_answer: Optional[Callable[[dict], Any]] = None  # before accepting: raise HubSideError to refuse
        self.calls: List[tuple] = []
        self.events: List[tuple] = []
        self.raw_lines: List[bytes] = []
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _send(self, obj: dict) -> None:
        with self._wl:
            self._w.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            self._w.flush()

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
        self.kb: Dict[str, dict] = {}
        self.full: Dict[str, str] = {}  # item id → full text
        self.stop_status: Dict[str, int] = {}  # session id → HTTP status for …/stop
        self.start_fail: Optional[int] = None
        self.offline = 0  # the next this many requests (any route) get the manager's 503 manager_offline
        self.lost_starts = 0  # the next this many conversation starts open a conversation, but the answer is lost (502)
        self.lost_code = "hub_unreachable"  # … with this code (the relay's, or the hub's node_unreachable)
        self.machine_offline = 0  # the next this many starts find the machine offline (503, nothing ran)
        self.lost_sends = 0  # the next this many messages are typed, but the answer is lost (502)
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
                       "auth": self.headers.get("Authorization"), "lang": self.headers.get("Accept-Language")}
                with hub.lock:
                    hub.requests.append(rec)
                if rec["auth"] != "Bearer " + hub.token:
                    return self._reply(401, {"error": "bad token"})
                status, obj = hub.route(method, rec["path"].split("/")[1:], query, body)
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

    def session_rows(self) -> List[dict]:
        with self.lock:
            return [r for r in self.requests if r["method"] == "GET" and r["path"] == "/api/v1/sessions"]

    def starts(self) -> List[dict]:
        with self.lock:
            return [r for r in self.requests if r["method"] == "POST" and r["path"] == "/api/v1/assistant/conversations"]

    def posts(self, suffix: str) -> List[dict]:
        with self.lock:
            return [r for r in self.requests if r["method"] == "POST" and r["path"].endswith(suffix)]

    # ---- routes ----

    def route(self, method: str, parts: List[str], query: dict, body: Any):
        p = "/" + "/".join(parts)
        with self.lock:
            if self.offline > 0:
                self.offline -= 1
                return 503, {"error": "the manager is not connected to the hub right now: try again shortly",
                             "code": "manager_offline"}
            if method == "GET" and p == "/api/v1/health":
                return 200, {"ok": True}
            if method == "POST" and p == "/api/v1/assistant/conversations":
                if self.start_fail:
                    return self.start_fail, {"error": "refused"}
                if self.machine_offline > 0:
                    self.machine_offline -= 1
                    return 503, {"error": "示例电脑离线了", "code": "node_unreachable", "retryable": True,
                                 "delivered": "none"}
                sid = "sess-%d" % next(self._n)
                mid = (body or {}).get("machine_id") or "mac-example"
                self.sessions[sid] = {"machine": mid, "items": [], "reporter": bool((body or {}).get("report_tasks")),
                                      "working": True, "stopped": False}
                self.add(sid, "user", "text", (body or {}).get("prompt") or "")
                if self.lost_starts > 0:
                    self.lost_starts -= 1
                    return 502, {"error": "the manager could not reach the hub", "code": self.lost_code}
                return 200, {"session": {"machine_id": mid, "session_id": sid, "title": ""}}
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
            if method == "GET" and p == "/api/v1/sessions":
                sid, mid = query.get("session_id"), query.get("machine_id")
                s = self.sessions.get(sid or "")
                if s is None or s["machine"] != mid:
                    return 200, {"sessions": [], "total": 0, "has_more": False}
                status = "working" if s["working"] else "done"
                return 200, {"sessions": [{"session_id": sid, "machine_id": mid, "status": status,
                                           "state": "working" if s["working"] else "unread"}],
                             "total": 1, "has_more": False}
            if len(parts) >= 6 and parts[:3] == ["api", "v1", "sessions"]:
                mid, sid = parts[3], parts[4]
                s = self.sessions.get(sid)
                if s is None or s["machine"] != mid:
                    return 404, {"error": "no such session"}
                rest = parts[5:]
                if method == "POST" and rest == ["messages"]:
                    kw = {}
                    cmid = (body or {}).get("client_msg_id")
                    if cmid:
                        kw["client_msg_id"] = cmid
                        if any(it.get("client_msg_id") == cmid for it in s["items"]):
                            return 200, {"ok": True}  # the hub sends each client_msg_id once
                    s["working"] = True
                    self.add(sid, "user", "text", (body or {}).get("text") or "", **kw)
                    if self.lost_sends > 0:
                        self.lost_sends -= 1
                        return 502, {"error": "the manager could not reach the hub", "code": "hub_unreachable"}
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
                 backend_factory=None):
        self.test = test
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
        factory = backend_factory or (lambda cfg, d: FixtureBackend(self.world))
        self.app = ConnectorApp(openmate_feishu.platform(), timing=self.timing, backend_factory=factory, env={})
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
            "extension": {"id": EXT, "version": "0.1.0", "dir": ROOT, "data_dir": self.data_dir},
            "config": self.config,
            "secrets": {},
            "granted": {},
            "active": {"connectors": [CID]},
        }
        params.update(over)
        res = self.hub.request("initialize", params)
        wait_until(self.app.ready.is_set, what="startup")
        return res

    def c(self, method: str, **params) -> Any:
        params.setdefault("contribution", CID)
        return self.hub.request("connector." + method, params)

    def status(self) -> dict:
        return self.c("status")

    def tool(self, name: str, **args) -> str:
        return self.c("tools.call", name=CID + "_" + name, args=args)["text"]

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
