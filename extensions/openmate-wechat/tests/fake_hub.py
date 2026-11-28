"""A fake AgentWechat hub for the tests: the parts of its HTTP API hub.py uses, in memory, on 127.0.0.1."""

from __future__ import annotations

import itertools
import json
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional


def iso(t: Optional[float] = None) -> str:
    return datetime.fromtimestamp(time.time() if t is None else t, timezone.utc).isoformat().replace("+00:00", "Z")


class FakeHub:
    def __init__(self, token: str = "tok-test-0123456789", account: str = "account-1", me: str = "wxid_me"):
        self.token = token
        self.account = account
        self.me = me
        self.authenticated = True
        self.accounts_extra: List[dict] = []
        self.cv = threading.Condition()
        self.events: List[dict] = []
        self.oldest = 0  # events at or before this cursor are no longer kept (event_cursor_expired)
        self.messages: Dict[str, dict] = {}
        self.threads: Dict[str, dict] = {}
        self.contacts: Dict[str, dict] = {me: {"id": me, "username": me, "nickname": "我自己", "remark": None,
                                               "classification": {"category": "unknown"}}}
        self.lookup: Dict[str, dict] = {}
        self.groups: Dict[str, dict] = {}
        self.members: Dict[str, List[dict]] = {}
        self.assets: Dict[str, List[dict]] = {}
        self.blobs: Dict[str, bytes] = {}
        self.jobs: Dict[str, dict] = {}
        self.cut: Dict[str, int] = {}
        self.job_fail = ""  # a download job ends failed with this reason
        self.fail_messages = 0  # this many GET /messages/{id} answer 503 first
        self.drop = 0  # bumped to end every open stream (a blip)
        self.stream_delay = 0.0  # a new stream answers this late (a slow handshake)
        self.gone_members: set = set()  # members an @ of whom the hub refuses (no longer in the group)
        self.lookup_missing = False  # /sends/by-idempotency-key finds nothing (the request not in yet)
        self.lookup_missing_once = False  # only the next such lookup finds nothing
        self.big_chats: set = set()  # chats whose message list answers 503 pagination_capacity_exceeded
        self.sends: Dict[str, dict] = {}
        self.recall_operations: Dict[str, dict] = {}
        self.recall_outcome = "succeeded"
        self.uploads: Dict[str, dict] = {}  # staged sources: name, mime, the bytes
        self.upload_fail = ""  # /uploads answers this error code (409, or 500 for "operation_failed")
        self.send_outcome = "accepted"  # accepted | failed | needs_review | refuse | silent | lost_answer
        self.echo_first = False  # the echo is committed before the send says accepted
        self.echo_origin = "local_send"
        self.echo_delay = 0.0  # a picture's echo comes this long after its job says accepted  # "live": WeChat's sync brought our message back before the hub's own copy
        self.requests: List[tuple] = []
        self.streams = 0
        self._ids = itertools.count(1)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self._closed = False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        with self.cv:
            self._closed = True
            self.cv.notify_all()
        self.server.shutdown()
        self.server.server_close()

    # ---- the world

    def cursor(self) -> int:
        return int(self.events[-1]["cursor"]) if self.events else 0

    def push(self, kind: str, data: dict, created_at: Optional[float] = None) -> int:
        with self.cv:
            c = self.cursor() + 1
            self.events.append({"cursor": str(c), "kind": kind, "data": data, "createdAt": iso(created_at)})
            self.cv.notify_all()
            return c

    def message(self, chat: str, text: str = "", sender: str = "", direction: str = "incoming", wire: int = 1,
                origin: str = "live", at: Optional[float] = None, mentions_self: bool = False, server: str = "",
                app: Optional[int] = None, link: Optional[dict] = None, commit: bool = True,
                committed_at: Optional[float] = None) -> dict:
        n = next(self._ids)
        hub_id = "msg:%s:%d" % (chat, n)
        m = {"id": hub_id, "accountId": self.account, "threadId": chat, "serverId": server or str(9000 + n),
             "senderId": (self.me if direction == "outgoing" else (sender or chat)), "direction": direction,
             "type": wire, "appMessageType": app, "body": text, "createdAt": iso(at), "link": link,
             "mentions": {"state": "known", "self": mentions_self, "all": False, "userIds": [], "source": None},
             "quote": None, "media": [], "version": "v1"}
        self.messages[hub_id] = m
        t = self.threads.setdefault(chat, {"id": chat, "kind": "group" if "@chatroom" in chat else "direct",
                                           "unreadCount": 0, "displayName": chat, "readStateVersion": "1"})
        t["lastMessage"] = {"id": hub_id, "direction": direction, "text": text[:160], "type": wire,
                            "createdAt": m["createdAt"], "senderId": m["senderId"], "senderName": None}
        if direction == "incoming":
            t["unreadCount"] = (t.get("unreadCount") or 0) + 1
        if commit:
            self.push("message.committed", {"messageId": hub_id, "threadId": chat, "origin": origin,
                                            "direction": direction}, committed_at)
        return m

    def recall(self, m: dict) -> None:
        m.update(type=10000, body="[Message recalled]", recall={"observedAt": iso()})
        self.push("message.recalled", {"threadId": m["threadId"], "serverId": m["serverId"], "observedAt": iso(),
                                       "origin": "live", "messageId": m["id"]})

    def read(self, chat: str, origin: Optional[str] = "remote_read") -> None:
        self.threads[chat]["unreadCount"] = 0
        data = {"threadId": chat, "readScope": "thread", "throughTime": int(time.time())}
        if origin:
            data["origin"] = origin
        self.push("thread.read-state", data)

    def sent(self, path: Optional[str] = None) -> List[tuple]:
        return [r for r in self.requests if path is None or path in r[1]]

    # ---- HTTP

    def _handler(self):
        hub = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # quiet
                pass

            def _json(self, status: int, body: Any) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _err(self, status: int, code: str) -> None:
                self._json(status, {"error": {"code": code}})

            def _body(self) -> Any:
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n).decode()) if n else None

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

            def _route(self, method: str) -> None:
                u = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(u.query))
                ctype = self.headers.get("Content-Type") or ""
                if method == "POST" and ctype.startswith("multipart/form-data"):
                    body = {"_multipart": self._multipart(ctype)}
                else:
                    body = self._body() if method == "POST" else None
                hub.requests.append((method, urllib.parse.unquote(u.path), body, dict(self.headers), q))
                if self.headers.get("Authorization") != "Bearer " + hub.token:
                    return self._err(401, "unauthorized")
                parts = [urllib.parse.unquote(p) for p in u.path.strip("/").split("/")]
                if parts[:2] == ["v1", "accounts"] and len(parts) == 2:
                    return self._json(200, {"items": [hub.account_obj()] + hub.accounts_extra})
                if parts[:2] != ["v1", "accounts"] or len(parts) < 3 or parts[2] != hub.account:
                    return self._err(403, "forbidden")
                rest = parts[3:]
                if rest[:1] == ["media"] and rest[2:] == ["content"]:
                    data = hub.blobs.get(rest[1])
                    if data is None:
                        return self._err(404, "media_not_cached")
                    cut = hub.cut.get(rest[1], 0)  # sends fewer bytes than it says
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data[:len(data) - cut])
                    if cut:
                        self.close_connection = True
                    return
                if not rest:
                    return self._json(200, hub.account_obj())
                if rest == ["events", "stream"]:
                    return self._stream(int(q.get("after") or 0))
                needs_auth = rest[0] not in ("contacts", "groups", "threads", "messages") or method == "POST"
                if not hub.authenticated and needs_auth and rest[0] != "events":
                    return self._err(409, "auth_required")
                if rest[0] == "contacts":
                    if rest[1:] == ["lookup"]:
                        ids = (body or {}).get("ids") or []
                        if not 1 <= len(ids) <= 2:
                            return self._err(500, "operation_failed")
                        items = [{"id": i, "displayName": hub.lookup[i], "alias": None, "profileProjection": "pending"}
                                 for i in ids if i in hub.lookup]
                        return self._json(200, {"items": items, "results": [0] * len(ids)})
                    c = hub.contacts.get(rest[1]) if len(rest) == 2 else None
                    return self._json(200, c) if c else self._err(404, "contact_not_found")
                if rest[0] == "groups":
                    if len(rest) == 1:  # the list's rows: no isInGroup, no name of their own
                        rows = [{"id": g["id"], "roomId": g["id"], "displayName": g.get("remark") or g.get("name")
                                 or g["id"], "version": "v"} for g in hub.groups.values()]
                        return self._json(200, {"items": rows, "nextCursor": None})
                    g = hub.groups.get(rest[1])
                    if g is None:
                        return self._err(404, "group_not_found")
                    if rest[2:] == ["members"]:
                        return self._json(200, {"items": hub.members.get(rest[1], []), "nextCursor": None})
                    return self._json(200, g)
                if rest in (["group-sync"], ["contact-sync"]):
                    return self._json(202, {"id": "op-1", "kind": "metadata", "state": "queued"})
                if rest[0] == "threads":
                    if len(rest) == 1:
                        items = sorted(hub.threads.values(), key=lambda t: (t.get("lastMessage") or {}).get(
                            "createdAt") or "", reverse=True)[:int(q.get("limit") or 50)]
                        return self._json(200, {"items": items, "nextCursor": None,
                                                "asOfEventCursor": str(hub.cursor()), "sourceGeneration": "g"})
                    if rest[2:] == ["messages"]:
                        if rest[1] in hub.big_chats:
                            return self._err(503, "pagination_capacity_exceeded")
                        ms = [m for m in hub.messages.values() if m["threadId"] == rest[1]]
                        ms.sort(key=lambda m: m["createdAt"], reverse=True)
                        return self._json(200, {"items": ms[:int(q.get("limit") or 50)], "nextCursor": None})
                if rest[0] == "messages" and len(rest) == 2 and hub.fail_messages > 0:
                    hub.fail_messages -= 1
                    return self._err(503, "temporarily_unavailable")
                if rest[0] == "messages":
                    m = hub.messages.get(rest[1])
                    if m is None:
                        return self._err(404, "message_not_found")
                    if rest[2:] == ["recall"] and method == "POST":
                        return self._start_recall(m, body)
                    if rest[2:] == ["media"]:
                        return self._json(200, {"items": hub.assets.get(rest[1], [])})
                    if rest[2:] == ["media-transfers"]:
                        if (body or {}).get("messageId") != rest[1]:
                            return self._err(400, "message_identity_mismatch")
                        jid = "job_%d" % next(hub._ids)
                        hub.jobs[jid] = {"id": jid, "kind": "download", "state": "queued", "_m": rest[1],
                                         "_a": (body or {}).get("assetId"), "assetId": (body or {}).get("assetId"),
                                         "_fail": hub.job_fail}
                        return self._json(202, hub.jobs[jid])
                    return self._json(200, m)
                if rest[0] == "operations" and len(rest) == 2:
                    op = hub.recall_operations.get(rest[1])
                    if op is None:
                        return self._err(404, "not_found")
                    if op["state"] == "queued":
                        op["state"] = hub.recall_outcome
                        if op["state"] == "succeeded":
                            hub.recall(hub.messages[op["result"]["messageId"]])
                    return self._json(200, {k: v for k, v in op.items() if not k.startswith("_")})
                if rest[0] == "jobs" and len(rest) == 2:
                    j = hub.jobs.get(rest[1])
                    if j is None:
                        return self._err(404, "not_found")
                    if j.get("_fail"):
                        j.update(state="failed", reason=j["_fail"])
                    elif j["state"] == "queued":  # done on the second look
                        j["state"] = "running"
                    elif j["state"] == "running":
                        for a in hub.assets.get(j["_m"], []):
                            if a["id"] == j["_a"]:
                                a.update(state="available",
                                         contentUrl="/v1/accounts/%s/media/%s/content" % (hub.account, a["id"]))
                        j["state"] = "available"
                    return self._json(200, {k: v for k, v in j.items() if not k.startswith("_")})
                if rest == ["uploads"] and method == "POST":
                    if hub.upload_fail:
                        return self._err(500 if hub.upload_fail == "operation_failed" else 409, hub.upload_fail)
                    part = (body or {}).get("_multipart")
                    if not part:
                        return self._err(400, "invalid_upload")
                    uid = "src_%d" % next(hub._ids)
                    hub.uploads[uid] = part
                    return self._json(201, {"id": uid, "name": part["name"], "mimeType": part["mime"],
                                            "size": len(part["data"])})
                if rest == ["sends"]:
                    return self._send(body or {})
                if rest == ["sends", "by-idempotency-key"]:
                    if hub.lookup_missing or hub.lookup_missing_once:
                        hub.lookup_missing_once = False
                        return self._err(404, "not_found")
                    for s in hub.sends.values():
                        if s["_key"] == q.get("key"):
                            return self._json(200, {k: v for k, v in s.items() if not k.startswith("_")})
                    return self._err(404, "not_found")
                if rest[0] == "sends" and len(rest) == 2:
                    s = hub.sends.get(rest[1])
                    return self._json(200, s) if s else self._err(404, "send_not_found")
                return self._err(404, "no_route")

            def _start_recall(self, message: dict, body: Any) -> None:
                key = self.headers.get("Idempotency-Key")
                if not key:
                    return self._err(400, "idempotency_key_required")
                if body != {"serverId": message["serverId"]} or message["direction"] != "outgoing":
                    return self._err(409, "recall_target_changed")
                if hub.recall_outcome == "refused":
                    return self._err(409, "recall_target_unavailable")
                for op in hub.recall_operations.values():
                    if op["_key"] == key:
                        return self._json(202, {k: v for k, v in op.items() if not k.startswith("_")})
                op_id = "recall-%d" % next(hub._ids)
                op = {"id": op_id, "_key": key, "accountId": hub.account, "kind": "message_recall", "state": "queued",
                      "createdAt": iso(), "updatedAt": iso(),
                      "result": {"messageId": message["id"], "threadId": message["threadId"],
                                 "serverId": message["serverId"]}}
                hub.recall_operations[op_id] = op
                return self._json(202, {k: v for k, v in op.items() if not k.startswith("_")})

            def _multipart(self, ctype: str) -> Optional[dict]:
                """The one "file" field of a multipart body: its file name, content type and bytes."""
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                boundary = ctype.split("boundary=", 1)[-1].encode()
                parts = raw.split(b"--" + boundary)
                for p in parts:
                    head, sep, data = p.partition(b"\r\n\r\n")
                    if not sep or b'name="file"' not in head:
                        continue
                    name = head.split(b'filename="', 1)[-1].split(b'"', 1)[0].decode()
                    mime = ""
                    for line in head.split(b"\r\n"):
                        if line.lower().startswith(b"content-type:"):
                            mime = line.split(b":", 1)[1].strip().decode()
                    return {"name": name, "mime": mime, "data": data[:-2] if data.endswith(b"\r\n") else data}
                return None

            def _send(self, body: dict) -> None:
                key = self.headers.get("Idempotency-Key") or ""
                if not key:
                    return self._err(400, "idempotency_key_required")
                if hub.send_outcome == "refuse":
                    return self._err(400, "invalid_thread")
                if hub.gone_members and set((body.get("mentions") or {}).get("userIds") or []) & hub.gone_members:
                    return self._err(409, "mention_member_unavailable")
                for s in hub.sends.values():
                    if s["_key"] == key:
                        return self._json(202, {k: v for k, v in s.items() if not k.startswith("_")})
                if body.get("kind") == "image":
                    return self._send_image(body, key)
                sid = "send-%d" % next(hub._ids)
                s = {"id": sid, "_key": key, "accountId": hub.account, "destination": body.get("threadId"),
                     "state": "queued", "clientId": None, "serverId": None, "localMessageId": None,
                     "errorCode": None, "createdAt": iso()}
                hub.sends[sid] = s
                outcome = hub.send_outcome

                def settle():
                    nonlocal outcome
                    if outcome == "lost_answer":
                        outcome = "accepted"
                    if outcome == "silent":
                        return
                    if outcome == "accepted":
                        text = body.get("text") or ""
                        m = hub.message(body["threadId"], text, direction="outgoing", origin="local_send",
                                        commit=False)
                        echo = {"messageId": m["id"], "threadId": m["threadId"], "origin": hub.echo_origin,
                                "direction": "outgoing"}
                        if hub.echo_first:
                            hub.push("message.committed", echo)
                            time.sleep(0.3)
                        s.update(state="accepted", serverId=m["serverId"], localMessageId=m["id"])
                        if not hub.echo_first:
                            hub.push("message.committed", echo)
                    else:
                        s.update(state=outcome, errorCode="refused_by_wechat" if outcome == "failed" else "unclear")

                threading.Timer(0.2, settle).start()
                if outcome == "lost_answer":  # taken, but the answer never reaches the extension
                    s["_lost"] = True
                    self.close_connection = True
                    self.wfile.write(b"HTTP/1.1 202 Accepted\r\nContent-Length: 999\r\n\r\n{")
                    return
                return self._json(202, {k: v for k, v in s.items() if not k.startswith("_")})

            def _send_image(self, body: dict, key: str) -> None:
                """A picture's send: a media job (its WeChat id is serverMessageId), settled a moment later."""
                if set(body) - {"threadId", "kind", "sourceId"} or body.get("sourceId") not in hub.uploads:
                    return self._err(400, "invalid_request")
                jid = "job_%d" % next(hub._ids)
                s = {"id": jid, "jobId": jid, "_key": key, "accountId": hub.account, "kind": "send", "state": "queued",
                     "phase": "upload", "attempts": 1, "retryable": False, "reason": None, "assetId": None,
                     "sourceId": body["sourceId"], "threadId": body["threadId"], "serverMessageId": None,
                     "createdAt": iso(), "updatedAt": iso()}
                hub.sends[jid] = s
                outcome = hub.send_outcome

                def settle():
                    nonlocal outcome
                    if outcome == "lost_answer":
                        outcome = "accepted"
                    if outcome == "silent":
                        s["state"] = "running"
                        return
                    if outcome == "accepted":
                        m = hub.message(body["threadId"], "", direction="outgoing", origin="local_send", wire=3,
                                        commit=False)
                        echo = {"messageId": m["id"], "threadId": m["threadId"], "origin": hub.echo_origin,
                                "direction": "outgoing"}
                        if hub.echo_first:
                            hub.push("message.committed", echo)
                            time.sleep(0.3)
                        s.update(state="accepted", serverMessageId=m["serverId"], updatedAt=iso())
                        if hub.echo_delay:
                            threading.Timer(hub.echo_delay, hub.push, ("message.committed", echo)).start()
                        elif not hub.echo_first:
                            hub.push("message.committed", echo)
                    else:
                        s.update(state=outcome, reason="refused_by_wechat" if outcome == "failed" else "unclear")

                threading.Timer(0.2, settle).start()
                if outcome == "lost_answer":
                    self.close_connection = True
                    self.wfile.write(b"HTTP/1.1 202 Accepted\r\nContent-Length: 999\r\n\r\n{")
                    return
                return self._json(202, {k: v for k, v in s.items() if not k.startswith("_")})

            def _stream(self, after: int) -> None:
                if after and after < hub.oldest:
                    return self._err(410, "event_cursor_expired")
                if hub.stream_delay:
                    time.sleep(hub.stream_delay)
                hub.streams += 1
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    self.wfile.write(b":connected\n\n")
                    self.wfile.flush()
                    drop = hub.drop
                    while True:
                        if hub.drop != drop:
                            self.close_connection = True  # the connection goes too (no length: the end of the body)
                            return
                        with hub.cv:
                            pending = [e for e in hub.events if int(e["cursor"]) > after]
                            if not pending and not hub._closed:
                                hub.cv.wait(0.5)
                                pending = [e for e in hub.events if int(e["cursor"]) > after]
                            if hub._closed:
                                return
                        if hub.drop != drop:  # a blip: the stream ends before what came meanwhile is written
                            self.close_connection = True
                            return
                        if not pending:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            continue
                        for e in pending:
                            self.wfile.write(("id: %s\ndata: %s\n\n" % (e["cursor"], json.dumps(e))).encode())
                            after = int(e["cursor"])
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return

        return H

    def account_obj(self) -> dict:
        return {"id": self.account, "alias": "me", "authenticated": self.authenticated,
                "connection": "connected" if self.authenticated else "disconnected",
                "ownUsername": self.me if self.authenticated else None}
