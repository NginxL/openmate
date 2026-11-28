"""AgentWechat as the link to WeChat: the account is signed in to an AgentWechat hub on this computer ("wx serve", or
the AgentWechat desktop app's own hub), and the extension is one more client of it, with a token of its own (read,
send, media). Nothing here signs in to WeChat or takes the account's computer slot: the user chats in the AgentWechat
desktop app at the same time.

HubLink answers the requests and gives the events the matewechat-native program does (native.py), so the backend's
rules stay one for both (what is live, names, groups, kinds, media, sends). Only the hub's public HTTP API is used
(thisnick/agent-wechat v2.7.1, MIT): /v1/accounts/{a}/… with "Authorization: Bearer".

What differs from the program, and how it is made the same:
- Messages come from the hub's event log (server-sent events, a decimal cursor). Live is what the hub committed after
  the stream it comes on was taken (the event's createdAt against that moment, one clock on this computer): what the
  log kept while the extension was off or the stream was down is caught up, history only, as the program's first
  sync after a reconnect is.
- Every send made through the hub (ours, the desktop app's) is echoed as local_send, and WeChat may sync ours back as
  an outgoing live message first. An outgoing message is ours when its ids are those a send of ours got: known from
  its answer, or, for a send whose outcome was not known, asked of the hub by the send's own key (its record holds
  WeChat's id and its message's). Not answerable now: taken as ours (never our own text as the user's command). Any
  other is the user's own, typed in the desktop app or on the phone: live, as the program gives one typed on the
  phone.
- A chat read on the phone or in the desktop app is the hub's thread.read-state event: the "read" event.
- Media: the message's media refers to the hub ("hub:<message id>:<copy>"); a fetch asks the hub to download that
  copy (a job, followed to its end) and saves its bytes, checked against their length and SHA-256.
"""

from __future__ import annotations

import http.client
import hashlib
import ipaddress
import itertools
import json
import logging
import os
import queue
import re
import ssl
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from .native import NativeError, NativeGone, clean_env

log = logging.getLogger("openmate.wechat.hub")

OPEN = "\x00open"  # events()'s first item: the hub took the stream
KEEPALIVE = "\x00keepalive"  # a comment from the hub, with no cursor to save
PROTOCOL = 2  # what native.Native.start expects of hello: the backend treats both the same
JSON_MOST = 8 << 20
LINE_MOST = 4 << 20
STREAM_IDLE = 60.0  # the hub writes a keepalive every 15 s: this long without a byte is a dead stream
SOFT_DOWN_RETRY = 60.0  # after this long without another tick failure, let the hub decide the next send
TAKEN_SLACK = 0.0  # one clock on this computer: committed before the stream was taken is before it
OWN_WAIT = 20.0  # a send to the chat still in flight is waited for this long before an outgoing message is judged
PENDING_KEEP = 30 * 86400.0  # how long a send with no known outcome is asked about (as long as the core keeps it)
SEND_WAIT = 40.0  # how long a send is followed before its outcome is "unknown"
IMAGE_WAIT = 120.0  # a picture: the hub checks the upload and puts it on WeChat's CDN before it is sent
IMAGE_MOST = 10 << 20  # what the core hands over at most (images.IMAGE_MOST)
GROUP_SYNC_EVERY = 3600.0  # at most this often the hub is asked to sync the group catalog from WeChat
CATCH_UP_CHATS = 200
CATCH_UP_EACH = 50
MEMBERS_MOST = 3000
RECORD_MOST = 4000  # a forwarded chat record's text, as matewechat-native's
ID_MOST = 512
TOKEN = re.compile(r"^[\x21-\x7e]{16,1024}$")  # printable ASCII, no spaces: what the hub issues
HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")  # a host name


def is_group(chat_id: str) -> bool:
    return "@chatroom" in chat_id or chat_id.endswith(("@im.chatroom", "@qy_g"))


def seconds(rfc3339: Any) -> int:
    """An RFC 3339 time as Unix seconds; 0 when missing or not one."""
    if not isinstance(rfc3339, str) or not rfc3339:
        return 0
    s = rfc3339.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except ValueError:
        # Python before 3.11 takes at most 6 fraction digits
        head, dot, rest = s.partition(".")
        if not dot:
            return 0
        frac = "".join(itertools.takewhile(str.isdigit, rest))
        try:
            return int(datetime.fromisoformat(head + "." + frac[:6] + rest[len(frac):]).timestamp())
        except ValueError:
            return 0


def stamp(rfc3339: Any) -> float:
    """An RFC 3339 time as Unix seconds with its fraction; 0.0 when missing or not one."""
    if not isinstance(rfc3339, str) or not rfc3339:
        return 0.0
    s = rfc3339.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    head, dot, rest = s.partition(".")
    if dot:  # Python before 3.11 takes at most 6 fraction digits
        frac = "".join(itertools.takewhile(str.isdigit, rest))
        s = head + "." + frac[:6] + rest[len(frac):]
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return 0.0


def strip_label(body: str, label: str) -> str:
    """The hub's English label off the front of a body ("[Link] title…"): what follows it."""
    b = (body or "").strip()
    return b[len(label):].strip() if b.startswith(label) else b



# an error code as the hub writes them (event_cursor_expired): the same shape the core's log keeps
ERROR_CODE = re.compile(r"[a-z]{1,12}(?:_[a-z]{1,12})+")


def frame_code(data: str) -> str:
    """The code of the hub's error frame ({"error":{"code":…}}), never its other text (a reason may end up in a log)."""
    try:
        err = json.loads(data).get("error")
    except (ValueError, AttributeError):
        return "unreadable"
    code = str(err.get("code") or "") if isinstance(err, dict) else ""
    return code if ERROR_CODE.fullmatch(code) and len(code) <= 64 else "unknown"

class HubError(Exception):
    """The hub answered with an error (status, its error code)."""

    def __init__(self, status: int, code: str):
        super().__init__("%s (%d)" % (code, status))
        self.status = status
        self.code = code


class HubDown(Exception):
    """The hub could not be reached, or the connection broke: whether it did anything is unknown."""


class BadSetting(ValueError):
    """The hub's address or token cannot be used (what: "address", "insecure" or "token")."""

    def __init__(self, what: str):
        super().__init__(what)
        self.what = what


def seg(value: Any) -> str:
    """One path segment (ids hold "@" and may hold anything else)."""
    v = str(value or "")
    if not v or len(v) > ID_MOST:
        raise HubError(400, "bad_id")
    return urllib.parse.quote(v, safe="")


def loopback(host: str) -> bool:
    if host.lower() in ("localhost", "localhost."):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _Raw:
    """A request body as it is (not JSON), with its content type."""

    def __init__(self, data: bytes, content_type: str):
        self.data = data
        self.content_type = content_type


class HubClient:
    """The hub's HTTP API, with the extension's token. No proxy: the hub is reached directly. Plain http only to this
    computer (the token goes in every request), as the AgentWechat clients themselves require."""

    def __init__(self, url: str, token: str):
        if any(ord(c) < 0x20 or ord(c) == 0x7f for c in (url or "")) or " " in (url or "").strip():
            raise BadSetting("address")
        try:
            u = urllib.parse.urlsplit((url or "").strip())
            host, port = u.hostname, u.port
        except ValueError:  # its message holds the address: never shown
            raise BadSetting("address")
        if u.scheme not in ("http", "https") or not host or u.username or u.password or u.query or u.fragment:
            raise BadSetting("address")
        if any(c.isspace() or ord(c) < 0x21 or c in "%[]" for c in host):
            raise BadSetting("address")
        if not HOST.match(host) and not loopback(host):
            try:
                ipaddress.ip_address(host)
            except ValueError:
                raise BadSetting("address")
        if u.scheme == "http" and not loopback(host):
            raise BadSetting("insecure")
        if not TOKEN.match(token or ""):
            raise BadSetting("token")
        self.https = u.scheme == "https"
        self.host = host
        self.port = port or (443 if self.https else 80)
        self.base = u.path.rstrip("/")
        self.token = token

    def _conn(self, timeout: float) -> http.client.HTTPConnection:
        if self.https:
            return http.client.HTTPSConnection(self.host, self.port, timeout=timeout,
                                               context=ssl.create_default_context())
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def _headers(self, extra: Optional[Dict[str, str]] = None, accept: str = "application/json") -> Dict[str, str]:
        h = {"Authorization": "Bearer " + self.token, "Accept": accept, "User-Agent": "openmate-wechat"}
        h.update(extra or {})
        return h

    @staticmethod
    def _error(status: int, raw: bytes) -> HubError:
        code = "http_%d" % status
        try:
            e = json.loads(raw.decode("utf-8"))
            if isinstance(e, dict) and isinstance(e.get("error"), dict) and e["error"].get("code"):
                code = str(e["error"]["code"])[:100]
        except (ValueError, UnicodeDecodeError):
            pass
        return HubError(status, code)

    def call(self, method: str, path: str, body: Any = None, headers: Optional[Dict[str, str]] = None,
             timeout: float = 20.0) -> Any:
        """One request; its JSON answer. HubError for an error answer, HubDown when there was no answer."""
        h = self._headers(headers)
        data = None
        if isinstance(body, _Raw):
            data = body.data
            h["Content-Type"] = body.content_type
        elif body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")  # the hub takes 128 KiB at most
            h["Content-Type"] = "application/json"
        conn = self._conn(timeout)
        try:
            conn.request(method, self.base + path, body=data, headers=h)
            r = conn.getresponse()
            raw = r.read(JSON_MOST + 1)
            status = r.status
        except (OSError, http.client.HTTPException) as e:
            raise HubDown(type(e).__name__)
        finally:
            conn.close()
        if status >= 400:
            raise self._error(status, raw)
        if len(raw) > JSON_MOST:
            raise HubDown("answer too long")
        if not raw.strip():
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HubDown("answer is not JSON")

    def upload(self, path: str, name: str, data: bytes, mime: str, timeout: float = 120.0) -> Any:
        """One file to the hub's /uploads (multipart, the field "file"); its answer (the staged source: its id)."""
        boundary = "openmate-" + os.urandom(12).hex()
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:80] or "image"
        head = ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\nContent-Type: %s\r\n\r\n'
                % (boundary, safe, mime)).encode("ascii")
        raw = _Raw(head + data + ("\r\n--%s--\r\n" % boundary).encode("ascii"),
                   "multipart/form-data; boundary=" + boundary)
        return self.call("POST", path, raw, timeout=timeout)

    def download(self, url: str, out: str, most: int, sha256: str = "", timeout: float = 120.0) -> Tuple[int, str]:
        """The bytes at the hub's url ("/v1/…", as its contentUrl) into out, whole and checked (its length, its
        SHA-256 when given) before it is renamed into place; (size, content type)."""
        if not url.startswith("/v1/"):
            raise HubError(400, "not_a_hub_path")
        conn = self._conn(timeout)
        tmp = out + ".part"
        try:
            conn.request("GET", self.base + url, headers=self._headers(accept="*/*"))
            r = conn.getresponse()
            if r.status >= 400:
                raise self._error(r.status, r.read(65536))
            ctype = str(r.getheader("Content-Type") or "")
            try:
                length = int(r.getheader("Content-Length") or -1)
            except ValueError:
                length = -1
            if length > most:
                raise HubError(413, "too_large")
            h = hashlib.sha256()
            n = 0
            with open(tmp, "wb") as f:
                os.chmod(tmp, 0o600)
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    n += len(chunk)
                    if n > most:
                        raise HubError(413, "too_large")
                    h.update(chunk)
                    f.write(chunk)
            if (length >= 0 and n != length) or (sha256 and h.hexdigest() != sha256.lower()):
                raise HubDown("the download was cut short or is not the hub's copy")
            os.replace(tmp, out)
            return n, ctype
        except (OSError, http.client.HTTPException) as e:
            raise HubDown(type(e).__name__)
        finally:
            conn.close()
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def events(self, path: str, stop: threading.Event,
               hold: Optional[Callable[[Optional[http.client.HTTPConnection]], None]] = None, *,
               keepalives: bool = False
               ) -> Iterator[Tuple[str, str]]:
        """The server-sent events at path: (id, data) each, after (OPEN, "") once the hub took the stream. If
        requested, comments yield (KEEPALIVE, ""). Ends when the hub closes it. HubError when the hub refuses it
        (an expired cursor, a revoked token), HubDown when it breaks or goes quiet too long. hold(conn) gets the
        connection while it is open (closing it ends the read)."""
        conn = self._conn(STREAM_IDLE)
        if hold is not None:
            hold(conn)
        try:
            try:
                conn.request("GET", self.base + path, headers=self._headers(accept="text/event-stream"))
                r = conn.getresponse()
            except (OSError, http.client.HTTPException) as e:
                raise HubDown(type(e).__name__)
            if r.status >= 400:
                raise self._error(r.status, r.read(65536))
            yield OPEN, ""
            eid, name, data = "", "", []
            while not stop.is_set():
                try:
                    raw = r.readline(LINE_MOST)
                except (OSError, ValueError, AttributeError, http.client.HTTPException) as e:
                    raise HubDown(type(e).__name__)  # socket.timeout among them; a closed connection too
                if not raw:
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    if data and name == "error":
                        # the hub's last frame before it closes the stream ({"error":{"code":…}}): whatever it says,
                        # the reconnect's own answer tells (a cursor really too old is a 410 then)
                        raise HubDown("the stream ended with an error: %s" % frame_code("".join(data)))
                    if data:
                        yield eid, "\n".join(data)
                    eid, name, data = "", "", []
                    continue
                if line.startswith(":"):
                    if keepalives:
                        yield KEEPALIVE, ""
                    continue
                field, _, value = line.partition(":")
                if value.startswith(" "):
                    value = value[1:]
                if field == "data":
                    data.append(value)
                elif field == "id":
                    eid = value
                elif field == "event":
                    name = value
        finally:
            if hold is not None:
                hold(None)
            conn.close()


class _Closed(Exception):
    """The link was closed (signed out here, or the extension stopping) before the request went."""


class _OtherAccount(Exception):
    """Another WeChat account than the one this extension serves is signed in on the hub."""


class _SignedOut(Exception):
    """The account is not signed in on the hub (signed out in the desktop app, pushed out, or never signed in)."""


class _Listen:
    """One listen: its stop, and the stream it reads (closed to wake the read)."""

    def __init__(self) -> None:
        self.stop = threading.Event()
        self.conn: Optional[http.client.HTTPConnection] = None
        self.mu = threading.Lock()

    def hold(self, conn: Optional[http.client.HTTPConnection]) -> None:
        with self.mu:
            self.conn = conn
            closing = conn is not None and self.stop.is_set()
        if closing:
            self._close(conn)

    def end(self) -> None:
        self.stop.set()
        with self.mu:
            conn = self.conn
        if conn is not None:
            self._close(conn)

    @staticmethod
    def _close(conn: http.client.HTTPConnection) -> None:
        try:
            sock = conn.sock
            if sock is not None:
                sock.shutdown(2)  # wakes a read in another thread
        except OSError:
            pass


class HubLink:
    """native.Native's requests and events over an AgentWechat hub (see the module's docstring)."""

    def __init__(self, client: HubClient, prefer: str = ""):
        self.client = client
        self.prefer = prefer  # the hub account this extension used before: chosen when the token reads several
        self.epoch = 0  # the backend stamps it; nothing here is sealed
        self.events: "queue.Queue[Optional[dict]]" = queue.Queue()
        self.account = ""  # the hub's account id
        self.me = ""  # its WeChat username
        # the WeChat account this extension serves (bound once, never switched in place): another one signed in on
        # the hub is neither read nor sent as
        self.expect = ""
        self.foreign = False
        self._mu = threading.Lock()
        self._sent = threading.Condition(self._mu)
        self._subs: Dict[int, "queue.Queue[Optional[dict]]"] = {}
        self._listens: Dict[int, _Listen] = {}
        self._alive = False
        # our own sends: the hub's message ids and WeChat's ids they answered with, and their texts by chat
        self._own_hub: Dict[str, float] = {}
        self._own_server: Dict[str, float] = {}
        self._pending: Dict[str, List[Tuple[float, str]]] = {}  # chat → (at, key): sends with no known outcome
        self.on_settled: Optional[Callable[[str], None]] = None  # a pending send's key whose message is known now
        self._inflight: Dict[str, int] = {}  # sends being made, by chat
        self._group_sync_at = 0.0

    # ---------------------------------------------------------------- life

    def alive(self) -> bool:
        return self._alive

    def start(self) -> None:
        """Which account the token reads: the one used before, else the only one (or the only one signed in)."""
        try:
            got = self.client.call("GET", "/v1/accounts", timeout=10)
        except HubDown as e:
            raise NativeGone("连不上 AgentWechat（%s）：确认 AgentWechat 在运行" % e)
        except HubError as e:
            raise NativeError("config", self._refusal(e))
        items = [a for a in (got.get("items") if isinstance(got, dict) else None) or []
                 if isinstance(a, dict) and a.get("id")]
        if not items:
            raise NativeError("config", "这个 AgentWechat 访问密钥读不到任何微信账号：发密钥时要用 --accounts 指定账号")
        chosen = [a for a in items if a.get("id") == self.prefer] if self.prefer else []
        if not chosen:
            chosen = items if len(items) == 1 else [a for a in items if a.get("authenticated")]
        if len(chosen) != 1:
            raise NativeError("config", "这个 AgentWechat 访问密钥能读 %d 个账号：请发一个只限一个账号的密钥（--accounts）"
                              % len(items))
        self.account = str(chosen[0]["id"])
        with self._mu:
            self._alive = True

    def close(self) -> None:
        with self._mu:
            self._alive = False
            listens = list(self._listens.values())
            self._listens.clear()
            subs = list(self._subs.values())
            self._sent.notify_all()
        for li in listens:
            li.end()
        for q in subs:
            q.put(None)
        self.events.put(None)

    def subscribe(self, gen: int) -> "queue.Queue[Optional[dict]]":
        q: "queue.Queue[Optional[dict]]" = queue.Queue()
        with self._mu:
            self._subs[gen] = q
            if not self._alive:
                q.put(None)
        return q

    def unsubscribe(self, gen: int) -> None:
        with self._mu:
            self._subs.pop(gen, None)

    def _emit(self, gen: int, ev: dict) -> None:
        ev["gen"] = gen
        with self._mu:
            q = self._subs.get(gen)
        if q is not None:
            q.put(ev)

    # ---------------------------------------------------------------- requests

    def call(self, op: str, timeout: float = 20.0, **params: Any) -> dict:
        if not self._alive:
            raise NativeGone("AgentWechat 的连接已关闭")
        fn = getattr(self, "_op_" + op, None)
        if fn is None:
            raise NativeError("unsupported", "AgentWechat 模式不支持 %s" % op)
        if self.foreign and op not in ("hello", "restore", "logout", "stop", "listen"):
            raise NativeError("config", OTHER_ACCOUNT)
        try:
            return fn(timeout, **params)
        except HubDown as e:
            raise NativeGone("连不上 AgentWechat（%s）" % e)
        except HubError as e:
            if e.code in ("auth_required", "profile_keys_missing"):
                raise NativeError("hub_signed_out", SIGNED_OUT)
            if e.status in (401, 403):
                raise NativeError("config", self._refusal(e))
            raise NativeError("input" if e.status < 500 else "error", "AgentWechat 拒绝了：%s" % e.code)

    @staticmethod
    def _refusal(e: HubError) -> str:
        if e.status == 401:
            return "AgentWechat 不认这个访问密钥（填错了，或已被收回）"
        if e.status == 403:
            return "AgentWechat 访问密钥的权限不够（要 read、send、media）：%s" % e.code
        return "AgentWechat 拒绝了：%s" % e.code

    def _path(self, tail: str) -> str:
        return "/v1/accounts/%s%s" % (seg(self.account), tail)

    def _get(self, tail: str, timeout: float = 20.0) -> Any:
        if not self._alive:
            raise HubDown("closed")
        return self.client.call("GET", self._path(tail), timeout=timeout)

    def _post(self, tail: str, body: Any = None, headers: Optional[Dict[str, str]] = None,
              timeout: float = 20.0) -> Any:
        if not self._alive:
            raise _Closed()
        return self.client.call("POST", self._path(tail), body, headers=headers, timeout=timeout)

    def _op_hello(self, timeout: float) -> dict:
        return {"protocol": PROTOCOL, "hub": True}

    def _op_login_start(self, timeout: float, **_: Any) -> dict:
        raise NativeError("unsupported", "AgentWechat 模式在 AgentWechat 里登录微信，这里不用扫码")

    _op_login_poll = _op_login_start
    _op_login_finish = _op_login_start

    def _op_logout(self, timeout: float, **_: Any) -> dict:
        return {}  # the account stays signed in on the hub: only this extension lets go of it

    def _op_restore(self, timeout: float, **_: Any) -> dict:
        a = self._get("", timeout=min(timeout, 15))
        if not isinstance(a, dict) or not a.get("authenticated") or not a.get("ownUsername"):
            raise NativeError("hub_signed_out", SIGNED_OUT)
        if self.expect and str(a.get("ownUsername")) != self.expect:
            self.foreign = True
            raise NativeError("config", OTHER_ACCOUNT)
        self.me = str(a.get("ownUsername"))
        self.expect = self.expect or self.me
        self.foreign = False
        name = ""
        try:
            c = self._get("/contacts/" + seg(self.me), timeout=10)
            if isinstance(c, dict):
                name = str(c.get("nickname") or "")
        except (HubError, HubDown):
            pass
        if not name:  # the hub keeps the account's own row without its nickname: asked of WeChat
            try:
                got = self._post("/contacts/lookup", {"ids": [self.me]}, timeout=10)
                for c in (got.get("items") if isinstance(got, dict) else None) or []:
                    if isinstance(c, dict) and c.get("id") == self.me and c.get("displayName") != self.me:
                        name = str(c.get("displayName") or "")
            except (HubError, HubDown):
                pass
        return {"account": {"id": self.me, "name": name or self.me, "hub_account": self.account}}

    # ---- names, groups, members

    @staticmethod
    def _contact(c: dict) -> dict:
        cls = c.get("classification") if isinstance(c.get("classification"), dict) else {}
        cat = str((cls or {}).get("category") or "")
        out = {"id": str(c.get("id") or c.get("username") or ""), "nickname": str(c.get("nickname") or ""),
               "remark": str(c.get("remark") or "")}
        if cat and cat != "unknown":  # the hub said what it is: an official / service account, or a person
            out["verify_flag"] = 8 if cat in ("business", "system") else 0
            out["business"] = cat in ("business", "system")
        return out

    def _op_contacts(self, timeout: float, ids: Any = None, **_: Any) -> dict:
        out: List[dict] = []
        failed: List[str] = []
        live: List[str] = []
        for cid in [str(i) for i in (ids or []) if i][:40]:
            try:
                if is_group(cid):
                    g = self._get("/groups/" + seg(cid))
                    out.append({"id": cid, "nickname": str(g.get("name") or ""), "remark": str(g.get("remark") or "")})
                else:
                    out.append(self._contact(self._get("/contacts/" + seg(cid))))
            except HubError as e:
                if e.status == 404 and not is_group(cid):
                    live.append(cid)  # not saved on the hub: asked of WeChat, two at a time
                elif e.status != 404:
                    failed.append(cid)
            except HubDown:
                failed.append(cid)
        for i in range(0, len(live), 2):
            pair = live[i:i + 2]
            try:
                got = self._post("/contacts/lookup", {"ids": pair}, timeout=timeout)
            except (HubError, HubDown):
                failed.extend(pair)
                continue
            for c in (got.get("items") if isinstance(got, dict) else None) or []:
                cid = str(c.get("id") or "") if isinstance(c, dict) else ""
                if cid in pair:
                    # a live lookup gives one name (remark, else nickname) and says nothing of what the account is
                    shown = str(c.get("displayName") or "")
                    out.append({"id": cid, "nickname": "", "remark": "" if shown == cid else shown})
        return {"contacts": out, "failed": failed}

    def _pages(self, tail: str, most: int, query: Optional[Dict[str, Any]] = None) -> List[dict]:
        q = dict(query or {})
        q["limit"] = min(200, max(1, most))
        out: List[dict] = []
        cursor = ""
        while len(out) < most:
            if cursor:
                q["cursor"] = cursor
            got = self._get(tail + "?" + urllib.parse.urlencode(q), timeout=30)
            items = got.get("items") if isinstance(got, dict) else None
            out.extend(i for i in items or [] if isinstance(i, dict) and not i.get("deleted"))
            cursor = str(got.get("nextCursor") or "") if isinstance(got, dict) else ""
            if not cursor or not items:
                break
        return out[:most]

    def _op_room(self, timeout: float, room_id: str = "", **_: Any) -> dict:
        g = self._get("/groups/" + seg(room_id))
        members = []
        for m in self._pages("/groups/%s/members" % seg(room_id), MEMBERS_MOST):
            uid = str(m.get("id") or m.get("username") or "")
            if uid:
                members.append({"id": uid, "display": str(m.get("roomDisplayName") or ""),
                                "nickname": str(m.get("nickname") or "")})
        out: Dict[str, Any] = {"members": members, "member_count": len(members)}
        if isinstance(g.get("isInGroup"), bool):
            out["me_in"] = g["isInGroup"]  # null on the hub: not known (never taken as gone)
        return out

    def _op_groups(self, timeout: float, catalog: bool = False, **_: Any) -> dict:
        if catalog and time.time() - self._group_sync_at > GROUP_SYNC_EVERY:
            self._group_sync_at = time.time()
            for what in ("/group-sync", "/contact-sync"):  # the hub reads WeChat's catalogs in the background
                try:
                    self._post(what, timeout=15)
                except (HubError, HubDown) as e:
                    log.info("AgentWechat %s not started: %s", what, e)
        ids: List[str] = []
        names: Dict[str, str] = {}
        # the list's rows say no more than id and displayName (remark, else name, else the id); whether the account
        # is still in a group is the room request's (the backend's member check) and group.updated's
        for g in self._pages("/groups", 20000):
            gid = str(g.get("id") or g.get("roomId") or "")
            if not is_group(gid):
                continue
            ids.append(gid)
            name = str(g.get("displayName") or "").strip()
            if name and name != gid:
                names[gid] = name
        return {"groups": ids, "names": names, "complete": True, "catalog_read": False, "catalog_complete": True,
                "catalog": len(ids), "contacts": len(ids)}

    # ---- media

    @staticmethod
    def _ref(file_id: Any) -> Tuple[str, str]:
        s = str(file_id or "")
        if not s.startswith("hub:"):
            raise NativeError("input", "不是 AgentWechat 的附件")
        hub_id, _, quality = s[4:].rpartition(":")
        if not hub_id or quality not in ("original", "standard", "thumbnail", "normal", "encoded"):
            raise NativeError("input", "不是 AgentWechat 的附件")
        return hub_id, quality

    def _assets(self, hub_id: str) -> List[dict]:
        got = self._get("/messages/%s/media" % seg(hub_id))
        return [a for a in (got.get("items") if isinstance(got, dict) else None) or [] if isinstance(a, dict)]

    @staticmethod
    def _pick(assets: List[dict], quality: str) -> Optional[dict]:
        for a in assets:
            if a.get("variant") == quality:
                return a
        return None

    def fetch(self, hub_id: str, quality: str, out: str, most: int, wait: float) -> Tuple[int, str]:
        """One copy (original, standard, thumbnail, normal) of a message's media into out: downloaded by the hub
        first when it has not got it (a job, followed to its end). Never "best": that is whichever copy is there
        first, a thumbnail as often as not."""
        deadline = time.time() + wait
        assets = self._assets(hub_id)
        a = self._pick(assets, quality)
        if quality == "normal" and (a is None or a.get("state") in ("expired", "unsupported")):
            o = self._pick(assets, "original")  # a video whose original copy is what there is
            if o is not None and o.get("state") not in ("expired", "unsupported"):
                a, quality = o, "original"
        if a is None:
            raise NativeError("input", "这条消息没有这份附件（%s）" % quality)
        state = str(a.get("state") or "")
        if state in ("expired", "unsupported"):
            raise NativeError("expired" if state == "expired" else "input",
                              "文件已过期，微信服务器上取不到了" if state == "expired" else "AgentWechat 不支持下载这份附件")
        advertised = a.get("advertisedByteLength") or a.get("byteLength")
        if isinstance(advertised, int) and advertised > most:
            raise NativeError("input", "附件超过 %d MB 上限" % (most >> 20))  # the hub is not asked for it at all
        if state != "available":
            job = self._post("/messages/%s/media-transfers" % seg(hub_id),
                             {"messageId": hub_id, "assetId": a.get("id"), "quality": quality})
            retried = False
            while True:
                js = str(job.get("state") or "") if isinstance(job, dict) else ""
                if js == "available":
                    break
                if js == "failed" and job.get("retryable") and job.get("id") and not retried:
                    retried = True  # the hub keeps one job per copy: a failed one is started again, once
                    job = self._post("/jobs/%s/retry" % seg(job["id"]), {})
                    continue
                if js in ("failed", "expired", "unsupported", "cancelled"):
                    why = str(job.get("reason") or js)
                    expired = js == "expired" or why == "media_expired"
                    raise NativeError("expired" if expired else "input",
                                      "文件已过期，微信服务器上取不到了" if expired else "AgentWechat 没能下载：%s" % why)
                if time.time() > deadline or not job.get("id"):
                    raise NativeGone("AgentWechat 下载附件超时")
                time.sleep(1.0)
                job = self._get("/jobs/" + seg(job["id"]), timeout=15)
            a = self._pick(self._assets(hub_id), quality) or {}
        size = a.get("byteLength")
        if isinstance(size, int) and size > most:
            raise NativeError("input", "附件超过 %d MB 上限" % (most >> 20))
        url, sha = str(a.get("contentUrl") or ""), str(a.get("sha256") or "")
        preview = a.get("preview") if isinstance(a.get("preview"), dict) else {}
        if a.get("contentType") == "image/x-wxgf" and preview.get("contentUrl"):
            # WeChat's own picture format: the hub's JPEG still of it
            url, sha = str(preview["contentUrl"]), str(preview.get("sha256") or "")
        if not url:
            raise NativeError("input", "AgentWechat 还没有这份附件")
        if not self._alive:
            raise NativeGone("AgentWechat 的连接已关闭")
        return self.client.download(url, out, most, sha256=sha, timeout=max(30.0, deadline - time.time()))

    def _op_media(self, timeout: float, file_id: Any = None, path: str = "", most: int = 25 << 20,
                  size: Any = None, **_: Any) -> dict:
        hub_id, quality = self._ref(file_id)
        n, _ = self.fetch(hub_id, quality, path, int(most), timeout)
        if quality == "original" and isinstance(size, int) and size and n != size and os.path.isfile(path):
            # a file's size is the one the message gave: anything else is not that file
            os.remove(path)
            raise NativeError("input", "下载的文件大小不对（%d，消息说 %d）" % (n, size))
        return {"size": n}

    def _op_voice(self, timeout: float, path: str = "", ref: str = "", **_: Any) -> dict:
        """A voice note as WAV: the hub's playable copy (WAV already), else turned into WAV with ffmpeg."""
        if not ref:
            raise NativeError("input", "这条语音没有 AgentWechat 的下载信息")
        raw = path + ".hub"
        try:
            _, ctype = self.fetch(ref, "normal", raw, 8 << 20, timeout)
            with open(raw, "rb") as f:
                head = f.read(12)
            if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
                os.replace(raw, path)
                return {}
            from .media import find_ffmpeg

            ff = find_ffmpeg()
            if not ff:
                raise NativeError("input", "语音是 %s，转成 WAV 要装 ffmpeg" % (ctype or "未知格式"))
            r = subprocess.run([ff, "-nostdin", "-v", "error", "-y", "-i", raw, "-ar", "16000", "-ac", "1", path],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=60, check=False, env=clean_env())
            if r.returncode != 0 or not os.path.isfile(path):
                raise NativeError("input", "语音转 WAV 没成功")
            return {}
        finally:
            if os.path.exists(raw):
                try:
                    os.remove(raw)
                except OSError:
                    pass

    # ---- sends

    def _keep_own(self, hub_id: str, server_id: str) -> None:
        now = time.time()
        with self._mu:
            if hub_id:
                self._own_hub[hub_id] = now
            if server_id:
                self._own_server[server_id] = now
            for d in (self._own_hub, self._own_server):
                if len(d) > 2000:
                    for k, _ in sorted(d.items(), key=lambda kv: kv[1])[:1000]:
                        d.pop(k, None)

    def _receipt(self, chat: str, entry: Tuple[float, str], hub_id: str, server_id: str) -> None:
        """A send's ids from its answer, and the send off the pending ones, in one step (a message judged meanwhile
        sees one or the other). Known by no id yet: it stays pending (its echo is asked about by its key)."""
        if server_id.startswith("hub-"):
            server_id = ""
        if not (hub_id or server_id):
            return
        now = time.time()
        with self._mu:
            if hub_id:
                self._own_hub[hub_id] = now
            if server_id:
                self._own_server[server_id] = now
            entries = self._pending.get(chat, [])
            if entry in entries:
                entries.remove(entry)  # the backend drops its record on disk once its message is kept
            for d in (self._own_hub, self._own_server):
                if len(d) > 2000:
                    for k, _ in sorted(d.items(), key=lambda kv: kv[1])[:1000]:
                        d.pop(k, None)

    def _known_id(self, hub_id: str, server_id: str) -> bool:
        with self._mu:
            return bool((hub_id and hub_id in self._own_hub) or (server_id and server_id in self._own_server))

    def remember_own(self, hub_id: str, server_id: str) -> None:
        """A message of ours known by its ids (kept by the backend across a restart): its echo is ours."""
        self._keep_own(hub_id, server_id)

    def remember_pending(self, chat: str, at: float, key: str) -> None:
        """A send of ours whose outcome is not known (kept by the backend across a restart): its echo is ours."""
        if chat and time.time() - at < PENDING_KEEP:
            with self._mu:
                self._pending.setdefault(chat, []).append((at, key))

    def _settle(self, chat: str, entry: Tuple[float, str], tell: bool = True) -> None:
        with self._mu:
            entries = self._pending.get(chat, [])
            if entry not in entries:
                return
            entries.remove(entry)
        if tell and self.on_settled is not None:
            try:
                self.on_settled(entry[1])
            except Exception:
                log.exception("a settled send could not be noted")

    def _own(self, chat: str, hub_id: str, server_id: str, text: str = "") -> bool:
        """An outgoing message is ours: its ids are a send of ours's, known from its answer or asked of the hub by the
        key of a send of ours to this chat whose outcome was not known. A record that cannot be read now: ours (the
        safe side)."""
        now = time.time()
        with self._mu:
            if (hub_id and hub_id in self._own_hub) or (server_id and server_id in self._own_server):
                return True
            entries = [e for e in self._pending.get(chat, []) if now - e[0] < PENDING_KEEP]
            self._pending[chat] = list(entries)  # the snapshot below is its own: a settle never moves it
        for entry in entries:
            s = None
            for key in (entry[1], entry[1] + "-no-at"):  # the send itself, or its answer again without an @
                try:
                    s = self._get("/sends/by-idempotency-key?" + urllib.parse.urlencode({"key": key}), timeout=10)
                    break
                except HubError as e:
                    if e.status != 404:
                        return True
                except HubDown:
                    return True
            if s is None:
                continue  # never made: not this message
            if not isinstance(s, dict):
                return True
            ids = (str(s.get("localMessageId") or ""), str(s.get("serverId") or s.get("serverMessageId") or ""))
            state = str(s.get("state") or "")
            if (hub_id and hub_id == ids[0]) or (server_id and server_id == ids[1]):
                self._keep_own(hub_id, server_id)
                self._settle(chat, entry)
                return True
            if state in ("failed", "cancelled", "unsupported"):
                self._settle(chat, entry)  # never went out: no message of it will come
            elif not any(ids):
                # went out or may be going out (submitting, needs_review, accepted not saved yet) and the hub cannot
                # say which message it is: this one may be it (the safe side)
                return True
        # a receipt may have landed while the records were read: its ids say last
        return self._known_id(hub_id, server_id)

    def _wait_sends(self, chat: str, stop: Optional[threading.Event]) -> None:
        """A send of ours to the chat still in flight: its answer tells whether this message is it."""
        deadline = time.time() + OWN_WAIT
        with self._sent:
            while (self._inflight.get(chat) and time.time() < deadline and self._alive
                   and not (stop is not None and stop.is_set())):
                self._sent.wait(0.5)

    def _hub_id_of(self, chat: str, server_id: str) -> str:
        """The hub's id of a message known here by WeChat's id only (kept before the hub, or before its echo): its
        chat's latest 600 messages looked through."""
        cursor = ""
        for _ in range(3):
            q = {"limit": 200}
            if cursor:
                q["cursor"] = cursor
            try:
                got = self._get("/threads/%s/messages?%s" % (seg(chat), urllib.parse.urlencode(q)), timeout=15)
            except (HubError, HubDown):
                return ""
            for m in (got.get("items") if isinstance(got, dict) else None) or []:
                if isinstance(m, dict) and str(m.get("serverId") or "") == server_id:
                    return str(m.get("id") or "")
            cursor = str(got.get("nextCursor") or "") if isinstance(got, dict) else ""
            if not cursor:
                return ""
        return ""

    def _remarked(self, user_id: str) -> bool:
        """The user may have a private remark for this account. Only a saved contact read without one counts as
        none: a former contact still in a group keeps its remark on the hub while its contact row is not given."""
        if not user_id:
            return True
        try:
            c = self._get("/contacts/" + seg(user_id), timeout=10)
        except (HubError, HubDown):
            return True
        return not (isinstance(c, dict) and c.get("id") and not str(c.get("remark") or "").strip())

    def _op_send(self, timeout: float, chat: str = "", text: str = "", key: str = "", mentions: Any = None,
                 quote: Any = None, **_: Any) -> dict:
        body: Dict[str, Any] = {"kind": "text", "threadId": chat, "text": text}
        ids = [str(m.get("id")) for m in mentions or [] if isinstance(m, dict) and m.get("id")]
        if ids and chat.endswith("@chatroom"):  # the hub @s in ordinary groups only (WeCom ones get the plain text)
            body["mentions"] = {"userIds": list(dict.fromkeys(ids))}
        if isinstance(quote, dict) and quote.get("server_id"):
            server = str(quote["server_id"])
            hub_id = str(quote.get("hub_id") or "") or self._hub_id_of(chat, server)
            who = str(quote.get("sender") or "")
            if quote.get("envelope") or (is_group(chat) and not chat.endswith("@chatroom")):
                # a WeCom group: AgentWechat names the card by the line's own sender (the group), not the one checked
                hub_id = ""
            if hub_id and (quote.get("outgoing") or not self._remarked(who)):
                body["quote"] = {"messageId": hub_id, "serverId": server}
            # AgentWechat names the quoted sender by the user's private remark for them when there is one (its quote
            # card carries that name to the group, or to the person): such a reply goes without the quote
        entry = (time.time(), key)
        with self._mu:
            self._inflight[chat] = self._inflight.get(chat, 0) + 1
            # pending before it goes: its echo may come before this send's answer, or as an outgoing live one
            self._pending.setdefault(chat, []).append(entry)
        try:
            r = self._send(body, key, min(timeout, SEND_WAIT))
            if r.get("status") == "sent":
                self._receipt(chat, entry, str(r.get("hub_id") or ""), str(r.get("message_id") or ""))
            elif r.get("status") == "rejected":
                self._settle(chat, entry, tell=False)  # surely not sent
            r["quoted"] = "quote" in body
            r["mentioned"] = "mentions" in body
            return r
        finally:
            with self._sent:
                self._inflight[chat] -= 1
                if not self._inflight[chat]:
                    del self._inflight[chat]
                self._sent.notify_all()

    def _op_send_image(self, timeout: float, chat: str = "", path: str = "", key: str = "", **_: Any) -> dict:
        """A picture into chat: staged on the hub (/uploads), then sent as an image under key, followed to its end like
        a text. Its echo is told by its ids (WeChat's, from the send's answer), or by its key while those are not
        known."""
        try:
            with open(path, "rb") as f:
                data = f.read(IMAGE_MOST + 1)
        except OSError as e:
            return {"status": "rejected", "message": "读不了要发的图片（%s）" % (e.strerror or type(e).__name__)}
        mime = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg" if data.startswith(b"\xff\xd8\xff") else ""
        if not data or len(data) > IMAGE_MOST or not mime:
            return {"status": "rejected", "message": "只能发 10 MB 以内的 PNG 或 JPG 图片"}

        def stage(body: dict) -> Optional[dict]:
            try:
                up = self.client.upload(self._path("/uploads"), os.path.basename(path), data, mime,
                                        timeout=min(timeout, 60.0))
            except _Closed:
                return {"status": "rejected", "message": "已退出登录，没有发送"}
            except HubDown:
                return {"status": "rejected", "message": "图片没传到 AgentWechat，没有发送"}
            except HubError as e:
                if e.code in ("auth_required", "profile_keys_missing"):
                    return {"status": "rejected", "message": SIGNED_OUT}
                return {"status": "rejected", "message": "AgentWechat 没收下这张图片：%s" % e.code}
            sid = str(up.get("id") or "") if isinstance(up, dict) else ""
            if not sid:
                return {"status": "rejected", "message": "AgentWechat 没给出上传的编号，没有发送"}
            body["sourceId"] = sid
            return None

        entry = (time.time(), key)
        with self._mu:
            self._inflight[chat] = self._inflight.get(chat, 0) + 1
            self._pending.setdefault(chat, []).append(entry)  # pending before it goes, as a text's
        try:
            r = self._send({"threadId": chat, "kind": "image"}, key, min(timeout, IMAGE_WAIT), before=stage)
            if r.get("status") == "sent":
                self._receipt(chat, entry, str(r.get("hub_id") or ""), str(r.get("message_id") or ""))
            elif r.get("status") == "rejected":
                self._settle(chat, entry, tell=False)  # surely not sent
            return r
        finally:
            with self._sent:
                self._inflight[chat] -= 1
                if not self._inflight[chat]:
                    del self._inflight[chat]
                self._sent.notify_all()

    def _send(self, body: dict, key: str, wait: float,
              before: Optional[Callable[[dict], Optional[dict]]] = None) -> dict:
        try:
            a = self._get("", timeout=10)
            own = str(a.get("ownUsername") or "") if isinstance(a, dict) else ""
            if not isinstance(a, dict) or not a.get("authenticated"):
                log.info("a send was refused before it went out (hub_signed_out)")
                return {"status": "rejected", "message": OTHER_ACCOUNT if self.foreign else SIGNED_OUT}
            if not own:
                # A moment without the account's name (its profile being read or rewritten) is not another account:
                # taking it for one would refuse every send until the stream reconnects.
                log.info("a send was refused before it went out (hub_no_username)")
                return {"status": "rejected", "message": "AgentWechat 暂时读不到微信号，没有发送"}
            if self.expect and own != self.expect:
                self.foreign = True
                log.info("a send was refused before it went out (other_account)")
                return {"status": "rejected", "message": OTHER_ACCOUNT}
        except HubDown:
            log.info("a send was refused before it went out (hub_unreachable)")
            return {"status": "rejected", "message": "连不上 AgentWechat，没有发送"}
        except HubError as e:
            log.info("a send was refused before it went out (hub_error %s %s)", e.status, e.code)
            return {"status": "rejected", "message": "AgentWechat 拒绝了：%s" % e.code}
        except _Closed:
            log.info("a send was refused before it went out (closed)")
            return {"status": "rejected", "message": "已退出登录，没有发送"}
        if before is not None:
            refused = before(body)  # what the send needs first (a picture's upload): nothing is sent when it fails
            if refused is not None:
                return refused
        try:
            try:
                s = self._post("/sends", body, headers={"Idempotency-Key": key})
            except HubError as e:
                if e.code != "mention_member_unavailable" or "mentions" not in body:
                    raise
                # refused before any intent was made: the same answer without the @, under a key of its own
                body.pop("mentions", None)
                key = key + "-no-at"
                s = self._post("/sends", body, headers={"Idempotency-Key": key})
        except _Closed:
            return {"status": "rejected", "message": "已退出登录，没有发送"}
        except HubError as e:
            if e.status >= 500:
                return {"status": "unknown", "message": "AgentWechat 出错了（%s）" % e.code}
            if e.code in ("auth_required", "profile_keys_missing"):
                return {"status": "rejected", "message": SIGNED_OUT}
            return {"status": "rejected", "message": "AgentWechat 没有接受：%s" % e.code}
        except HubDown:
            # the answer was lost: the send is looked up by its key (never made again), once the hub had the time
            # to finish taking it
            time.sleep(2.0)
            try:
                s = self._get("/sends/by-idempotency-key?" + urllib.parse.urlencode({"key": key}), timeout=10)
            except HubError:
                # not found (yet) is not "not sent": the request may still be on its way in; never sent again
                return {"status": "unknown", "message": "连不上 AgentWechat，发送结果不确定"}
            except HubDown:
                return {"status": "unknown", "message": "连不上 AgentWechat，发送结果不确定"}
        deadline = time.time() + wait
        while True:
            state = str(s.get("state") or "") if isinstance(s, dict) else ""
            if state in ("failed", "cancelled", "unsupported"):
                why = s.get("errorCode") or s.get("reason") or state
                return {"status": "rejected", "message": "微信没有接受这条消息（%s）" % why}
            if state == "needs_review":
                why = s.get("errorCode") or s.get("reason") or ""
                return {"status": "unknown", "message": "AgentWechat 说结果要核对（%s）" % why}
            server = str(s.get("serverId") or s.get("serverMessageId") or "") if isinstance(s, dict) else ""
            if state == "accepted" and (server or time.time() > deadline):
                # WeChat's id is the message's id here (its echo has the same); the hub's own id comes once the
                # message is saved there, a moment later: the echo brings it then (a picture's job has none)
                hub_id = str(s.get("localMessageId") or "")
                return {"status": "sent", "message_id": server or (
                    "hub-" + hashlib.sha256(hub_id.encode()).hexdigest()[:32] if hub_id else ""),
                        "created_at": seconds(s.get("createdAt")) or int(time.time()), "hub_id": hub_id}
            if time.time() > deadline or not s.get("id"):
                return {"status": "unknown", "message": "AgentWechat %d 秒没有确认" % int(wait)}
            time.sleep(0.5)
            try:
                s = self._get("/sends/" + seg(s["id"]), timeout=10)
            except (HubError, HubDown) as e:
                log.info("a send's state could not be read now: %s", e)

    def _op_recall(self, timeout: float, hub_id: str, server_id: str, chat: str = "") -> dict:
        deadline = time.monotonic() + timeout
        if not hub_id and chat:
            # A send of a moment ago: among the chat's latest messages, looked up within the same budget.
            got = self._get("/threads/%s/messages?%s" % (seg(chat), urllib.parse.urlencode({"limit": 50})),
                            timeout=timeout)
            for m in (got.get("items") if isinstance(got, dict) else None) or []:
                if isinstance(m, dict) and str(m.get("serverId") or "") == server_id:
                    hub_id = str(m.get("id") or "")
                    break
            if not hub_id:
                raise NativeError("not_found", "AgentWechat 里还找不到这条消息")
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise NativeError("timeout", "AgentWechat 撤回确认超时")
        key = "om-recall-" + hashlib.sha256(hub_id.encode()).hexdigest()
        try:
            op = self._post("/messages/%s/recall" % seg(hub_id), {"serverId": server_id},
                            headers={"Idempotency-Key": key}, timeout=timeout)
        except _Closed:
            raise NativeGone("AgentWechat 的连接已关闭")
        op_id = op.get("id") if isinstance(op, dict) else None
        while op_id and isinstance(op, dict) and op.get("id") == op_id:
            state = op.get("state")
            if state == "succeeded":
                return {}
            if state in ("rejected", "failed", "cancelled", "expired", "needs_review"):
                result = op.get("result")
                rejected = state == "rejected" or (isinstance(result, dict) and result.get("remoteOutcome") == "rejected")
                kind = "input" if rejected else ("unknown" if state == "needs_review" else "failed")
                raise NativeError(kind, "AgentWechat 没能确认撤回（%s）" % state)
            if state not in ("queued", "running"):
                break
            time.sleep(max(0.0, min(0.25, deadline - time.monotonic())))
            left = deadline - time.monotonic()
            if left <= 0:
                raise NativeError("timeout", "AgentWechat 撤回确认超时")
            op = self._get("/operations/" + seg(op_id), timeout=left)
        raise NativeError("unknown", "AgentWechat 返回了无效的撤回操作")

    # ---------------------------------------------------------------- the live link

    def _op_listen(self, timeout: float, cursor: str = "", gen: int = 0, **_: Any) -> dict:
        li = _Listen()
        with self._mu:
            if not self._alive:
                raise NativeGone("AgentWechat 的连接已关闭")
            old = self._listens.pop(gen, None)
            self._listens[gen] = li
        if old is not None:
            old.end()
        threading.Thread(target=self._listen, args=(gen, str(cursor or ""), li), name="matewechat-hub-listen",
                         daemon=True).start()
        return {}

    def _op_stop(self, timeout: float, gen: int = 0, **_: Any) -> dict:
        with self._mu:
            li = self._listens.pop(gen, None)
        if li is not None:
            li.end()
        return {}

    def _head(self, chats: int = 1) -> Tuple[int, List[dict]]:
        """The event log's cursor now, with the chats' first page read at that same moment (newest first)."""
        got = self._get("/threads?limit=%d" % chats, timeout=30)
        head = str((got or {}).get("asOfEventCursor") or "0") if isinstance(got, dict) else "0"
        items = [t for t in (got.get("items") if isinstance(got, dict) else None) or []
                 if isinstance(t, dict) and not t.get("deleted")]
        return (int(head) if head.isdigit() else 0), items

    def cursor_of(self, saved: str) -> int:
        """A cursor saved here ("<hub account>:<n>"): its n when it is this hub account's, else -1 (none; 0 is the
        start of an empty log, a cursor like any other)."""
        acct, _, n = (saved or "").rpartition(":")
        return int(n) if acct == self.account and n.isdigit() else -1

    def _listen(self, gen: int, saved: str, li: _Listen) -> None:
        """The log, from the kept cursor. Live is what the hub committed after the stream this event came on was taken
        (its createdAt against that moment, one clock on this computer): what it kept meanwhile is caught up, history
        only, as the program's first sync after a reconnect is."""
        stop = li.stop
        after = self.cursor_of(saved)
        backoff = 1.0
        caught_up = -1e9
        while not stop.is_set() and self._alive:
            reason = "连接断开，正在重连"
            try:
                a = self._get("", timeout=10)
                if not isinstance(a, dict) or not a.get("authenticated"):
                    raise _SignedOut()
                own = str(a.get("ownUsername") or "")
                if not own:
                    raise _SignedOut()  # no name for the account yet: as restore takes it, never another account
                if self.expect and own != self.expect:
                    self.foreign = True
                    raise _OtherAccount()
                self.foreign = False
                self.me = own or self.me
                head, _ = self._head()
                if after < 0 or after > head:
                    # none yet, or from another log (a new hub, a log started over): the recent chats as history,
                    # then from there
                    after = self._catch_up(gen, stop)
                    self._emit(gen, {"event": "cursor", "cursor": "%s:%d" % (self.account, after)})
                began = time.monotonic()
                taken = float("inf")  # when this stream was taken (wall clock): nothing is live before it is
                soft_down_at: Optional[float] = None
                disconnected = False
                path = self._path("/events/stream?after=%d" % after)
                for eid, data in self.client.events(path, stop, li.hold, keepalives=True):
                    if stop.is_set():
                        return
                    if eid == OPEN:
                        taken = time.time()
                        self._emit(gen, {"event": "link", "up": True})
                        continue
                    if eid == KEEPALIVE:
                        # The worker emits no success after a failed tick. A quiet, still-open stream lets us
                        # clear the soft down after a while without treating the account's reported status as proof.
                        if (soft_down_at is not None and not self.foreign
                                and time.monotonic() - soft_down_at >= SOFT_DOWN_RETRY):
                            soft_down_at = None
                            self._emit(gen, {"event": "link", "up": True})
                        continue
                    try:
                        ev = json.loads(data)
                    except ValueError:
                        continue
                    cur = str(ev.get("cursor") or eid or "") if isinstance(ev, dict) else ""
                    if not cur.isdigit():
                        continue
                    # committed before this stream was taken: kept while not listening, history only
                    replay = stamp(ev.get("createdAt")) < taken - TAKEN_SLACK
                    signal = self._on_event(gen, ev, stop, replay=replay)
                    if signal == "disconnected":
                        disconnected = True
                        soft_down_at = None
                        self._emit(gen, {"event": "link", "up": False, "why": "AgentWechat 断开了与微信的连接"})
                    elif signal == "connection_unavailable" and not disconnected:
                        if soft_down_at is None:
                            self._emit(gen, {"event": "link", "up": False, "why": "AgentWechat 暂时连不上微信，正在重连"})
                        soft_down_at = time.monotonic()
                    elif not self.foreign and (
                            (disconnected and signal == "push_connected")
                            or (soft_down_at is not None and signal in ("activity", "push_connected"))):
                        disconnected = False
                        soft_down_at = None
                        self._emit(gen, {"event": "link", "up": True})
                    after = int(cur)
                    self._emit(gen, {"event": "cursor", "cursor": "%s:%d" % (self.account, after)})
                    if signal == "signed_out":
                        raise _SignedOut()  # after its cursor: a reconnect never reads this event again
                    if time.monotonic() - began > 60:
                        backoff = 1.0  # a stable stream: start over
            except _SignedOut:
                reason = SIGNED_OUT
            except _OtherAccount:
                reason = OTHER_ACCOUNT
            except HubError as e:
                if e.code == "event_cursor_expired" and time.monotonic() - caught_up > 60:
                    caught_up = time.monotonic()  # once a minute at most: a log that keeps expiring backs off
                    log.warning("AgentWechat no longer has events after %s: catching up from the chat list", after)
                    try:
                        after = self._catch_up(gen, stop)
                        self._emit(gen, {"event": "cursor", "cursor": "%s:%d" % (self.account, after)})
                        continue
                    except (HubError, HubDown) as e2:
                        reason = "补读漏掉的消息时出错：%s" % e2
                elif e.status in (401, 403):
                    reason = self._refusal(e)
                elif e.code in ("auth_required", "profile_keys_missing"):
                    reason = SIGNED_OUT
                else:
                    reason = "AgentWechat 出错了：%s" % e.code
            except HubDown as e:
                reason = "连不上 AgentWechat（%s）" % e
            except Exception:
                log.exception("the AgentWechat stream failed")
            if stop.is_set():
                return
            self._emit(gen, {"event": "link", "up": False, "why": reason})
            stop.wait(backoff)
            backoff = min(backoff * 2, 60.0)

    def _on_event(self, gen: int, ev: dict, stop: threading.Event, replay: bool) -> str:
        """Handle one log event and return any signal it carries about the WeChat connection."""
        kind = str(ev.get("kind") or "")
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        if kind == "message.committed":
            origin = str(data.get("origin") or "")
            mid = str(data.get("messageId") or "")
            if not mid or origin == "profile_import":
                return ""
            activity = "activity" if origin in ("live", "local_send") else ""
            msg = self._message(mid)
            if msg is None:
                return activity
            if replay:
                origin = "catchup"  # kept while the extension was not listening: history, never acted on
            out = self.message_event(msg, origin, stop)
            if out is not None:
                self._emit(gen, out)
            return activity
        elif kind == "message.recalled":
            mid = str(data.get("messageId") or "")
            msg = self._message(mid) if mid else None
            out = self.message_event(msg, "catchup", stop) if msg is not None else None
            if out is None and data.get("serverId") and data.get("threadId"):
                # the hub had not kept it: known here by WeChat's id, which is the message's id here
                out = {"event": "message", "origin": "catchup", "id": str(data["serverId"]),
                       "chat": str(data["threadId"]), "type": "SYSTEM", "text": "[撤回了一条消息]"}
            if out is not None:
                out["update"] = True  # a message kept already: its new text (a recall notice) replaces the old
                out["recalled"] = True
                self._emit(gen, out)
        elif kind == "thread.read-state":
            # read on the phone (remote_read, or entering / leaving the chat there) or in the desktop app
            tid = str(data.get("threadId") or "")
            through = data.get("throughTime")
            at = int(through) if isinstance(through, (int, float)) and through > 0 else seconds(ev.get("createdAt"))
            if tid and at:
                self._emit(gen, {"event": "read", "chat": tid, "at": at})
        elif kind in ("contacts.updated", "contact.updated"):
            self._emit(gen, {"event": "contacts"})
        elif kind == "group.updated":
            gid = str(data.get("groupId") or "")
            if is_group(gid):
                try:
                    g = self._get("/groups/" + seg(gid))
                    if isinstance(g, dict) and g.get("isInGroup") is False:
                        self._emit(gen, {"event": "removed", "id": gid, "origin": "live"})
                        return ""
                except HubError:
                    pass
            self._emit(gen, {"event": "contacts"})
        elif kind == "operation.updated":
            if data.get("kind") == "metadata" and data.get("state") == "succeeded":
                self._emit(gen, {"event": "contacts"})  # a catalog sync finished: the groups are read again
        elif kind == "account.updated":
            if str(data.get("authentication") or "") == "logged_out":
                return "signed_out"  # signed out there, or pushed out by another computer's sign-in
            if str(data.get("connection") or "") == "disconnected":
                return "disconnected"
        elif kind == "account.connection":
            st = str(data.get("state") or "")
            if st in ("auth_required", "logged_out") or data.get("authentication") == "logged_out":
                return "signed_out"
            if st == "connection_unavailable":
                return st
            if st:
                return "activity"
        elif kind == "account.push":
            if data.get("state") == "connected":
                return "push_connected"
            if data.get("state") == "notified":
                return "activity"
        elif kind in ("sync.received", "sync.committed") or (kind == "send.updated" and data.get("state") == "accepted"):
            return "activity"
        return ""

    def _message(self, mid: str) -> Optional[dict]:
        for attempt in range(3):  # a passing error is tried again here: a reconnect would make it history
            try:
                msg = self._get("/messages/" + seg(mid))
                return msg if isinstance(msg, dict) and not msg.get("deleted") else None
            except HubError as e:
                if e.status in (404, 410):
                    return None  # gone already: nothing to keep
                if e.status < 500 or attempt == 2:
                    raise
            except HubDown:
                if attempt == 2 or not self._alive:
                    raise
            time.sleep(0.5)
        return None

    def _op_message_gone(self, timeout: float, hub_id: str, chat: str) -> dict:
        """Read once before a reply, with a shorter timeout than the event stream's reads."""
        msg = self._get("/messages/" + seg(hub_id), timeout=timeout)
        if not isinstance(msg, dict) or msg.get("id") != hub_id or msg.get("threadId") != chat:
            return {}
        if isinstance(msg.get("recall"), dict):
            return {"gone": True}
        return {} if msg.get("deleted") else {"gone": False}

    def _catch_up(self, gen: int, stop: threading.Event) -> int:
        """After a gap the event log no longer covers: the chats active since, read at one moment (the log's head
        then), each up to its last message at that moment, as history. Returns that head: what comes after it is
        read from the log, so nothing is both."""
        head, threads = self._head(CATCH_UP_CHATS)
        for t in threads:
            if stop.is_set():
                break
            tid = str(t.get("id") or "")
            last = t.get("lastMessage") if isinstance(t.get("lastMessage"), dict) else {}
            if not tid or not last.get("id"):
                continue
            try:
                got = self._get("/threads/%s/messages?limit=%d" % (seg(tid), CATCH_UP_EACH), timeout=30)
            except (HubError, HubDown) as e:  # a chat too big to page, or slow: the others go on
                log.info("a chat was not caught up: %s", getattr(e, "code", e))
                continue
            items = [m for m in (got.get("items") if isinstance(got, dict) else None) or []
                     if isinstance(m, dict) and not m.get("deleted")]
            ids = [str(m.get("id") or "") for m in items]
            if last["id"] in ids:
                items = items[ids.index(last["id"]):]  # newest first: the ones after that moment are the log's
            else:  # pushed off the page by newer ones: those after the snapshot's last one are the log's
                until = seconds(last.get("createdAt"))
                items = [m for m in items if until and seconds(m.get("createdAt")) < until]
            for m in reversed(items):
                if stop.is_set():
                    break
                out = self.message_event(m, "catchup", stop)
                if out is not None:
                    self._emit(gen, out)
        return head

    # ---------------------------------------------------------------- messages

    def message_event(self, msg: dict, origin: str, stop: Optional[threading.Event] = None) -> Optional[dict]:
        """A hub message as the program's message event."""
        hub_id = str(msg.get("id") or "")
        chat = str(msg.get("threadId") or "")
        if not hub_id or not chat:
            return None
        server = str(msg.get("serverId") or "")
        outgoing = msg.get("direction") == "outgoing"
        body = str(msg.get("body") or "")
        if outgoing:
            if not self._known_id(hub_id, server):
                self._wait_sends(chat, stop)  # a send of ours to the chat in flight: its answer tells first
            if self._own(chat, hub_id, server, body):  # judged once: a text of ours answers one message only
                origin = "local_send"  # ours: history (the backend kept it when it sent it)
            elif origin == "local_send":
                origin = "live"  # the user's own, typed in the desktop app (one typed on the phone is live already)
        group = is_group(chat)
        envelope = False
        sender = self.me if outgoing else str(msg.get("senderId") or ("" if group else chat))
        if group and not outgoing and sender in ("", chat):
            # a WeCom group's line the hub did not split ("<member id>:\n<text>"), as matewechat-native reads it
            who, nl, rest = body.partition(":\n")
            if nl and who and " " not in who and len(who) <= 128 and who != chat:
                sender, body, envelope = who, rest, True
            else:
                sender = ""  # never the group itself: someone in it, not known
        wire = msg.get("type") if isinstance(msg.get("type"), int) else 0
        app = msg.get("appMessageType") if isinstance(msg.get("appMessageType"), int) else 0
        link = msg.get("link") if isinstance(msg.get("link"), dict) else {}
        title = str(link.get("title") or "")
        m = msg.get("mentions") if isinstance(msg.get("mentions"), dict) else {}
        known = m.get("state") == "known"
        quote = msg.get("quote") if isinstance(msg.get("quote"), dict) else None
        parent = quoted_who = quoted_text = ""
        media: Optional[dict] = None

        def ref(quality: str) -> str:
            return "hub:%s:%s" % (hub_id, quality)

        if wire == 1:
            kind, text = "TEXT", body
        elif wire == 3:
            kind, text = "IMAGE", "[图片]"
            # the fetcher tries "mid" first, then "big", then "thumb": the original picture first here
            media = {"kind": "image", "mid": ref("original"), "big": ref("standard"), "thumb": ref("thumbnail"),
                     "aes_key": "hub", "hub": True}
        elif wire == 34:
            kind, text = "AUDIO", "[语音]"
            media = {"kind": "voice", "length": 1, "ref": hub_id, "hub": True}
        elif wire in (43, 62):
            kind, text = "VIDEO", "[视频]"
            if wire == 43:  # the hub keeps no copies of an old short video (62): a placeholder only
                media = {"kind": "video", "thumb": ref("thumbnail"), "url": ref("normal"), "aes_key": "hub",
                         "hub": True}
        elif wire == 42:
            kind, text = "CARD", ("[名片] %s" % strip_label(body, "[Shared contact]")) if body else "[名片]"
        elif wire == 47:
            kind, text = "STICKER", "[表情]"
        elif wire == 48:
            loc = msg.get("location") if isinstance(msg.get("location"), dict) else {}
            place = " ".join(str(loc.get(k) or "") for k in ("name", "address") if loc.get(k))
            kind, text = "CARD", ("[位置] %s" % place) if place else "[位置]"
        elif wire == 49 and app == 57:
            kind, text = "TEXT", body
        elif wire == 49 and app == 6:
            media = self._file_media(hub_id, title)  # the file's name is its media's (a file message has no body)
            kind, text = "FILE", ("[文件] %s" % media["name"]).rstrip()
        elif wire == 49 and app == 19:
            # the hub gives the forwarded chat's lines in the body ("[Chat History] title\nwho: text…")
            kind, text = "CARD", "[聊天记录] %s" % strip_label(body, "[Chat History]")[:RECORD_MOST]
        elif wire == 49 and app in (33, 36):
            kind, text = "CARD", "[小程序] %s" % (title or body)
        elif wire == 49:
            kind, text = "CARD", ("[链接] %s" % title) if title else (
                "[链接] %s" % strip_label(body, "[Link]") if body else "[卡片消息]")
        elif wire == 50:
            kind, text = "SYSTEM", "[通话]"
        elif wire == 10000:
            kind, text = "SYSTEM", body or "[系统消息]"
        elif wire == 10002:
            kind, text = "SYSTEM", "[撤回了一条消息]"
        else:
            kind, text = "SYSTEM", "[暂不支持的消息]"
        if quote is not None:
            parent = str(quote.get("serverId") or "")
            quoted_who = str(quote.get("senderName") or quote.get("senderId") or "")
            quoted_text = str(quote.get("embeddedText") or "")
        recalled = isinstance(msg.get("recall"), dict)
        if recalled:
            kind = "SYSTEM"
            text = str(msg["recall"].get("replacementText") or "[撤回了一条消息]")
            media = None
        quotable = wire == 1 and bool(server) and (outgoing or bool(sender))
        ev: Dict[str, Any] = {
            "event": "message", "origin": origin, "id": server or ("hub-" + hashlib.sha256(hub_id.encode()).hexdigest()[:32]),
            "hub_id": hub_id, "chat": chat,
            "group": group, "sender": sender, "mine": outgoing, "type": kind, "wire_type": wire, "text": text,
            "at": seconds(msg.get("createdAt")) or int(time.time()),
            "at_me": (not outgoing) and known and bool(m.get("self")),
            "at_all": (not outgoing) and known and bool(m.get("all")),
            "mentions_known": known, "parent": parent, "quoted_sender": quoted_who, "quoted_text": quoted_text,
            "quotable": quotable, "content": body if wire == 1 else "", "msgsource": "",
        }
        if media is not None:
            ev["media"] = media
        if envelope:
            ev["envelope"] = True  # its sender read from the text: a quote of it would be named otherwise
        if recalled:
            ev["update"] = True  # kept already: its text and media go (wherever the recall was seen)
            ev["recalled"] = True
        return ev

    def _file_media(self, hub_id: str, name: str) -> dict:
        media: Dict[str, Any] = {"kind": "file", "url": "hub:%s:original" % hub_id, "aes_key": "hub", "name": name,
                                 "hub": True}
        try:
            for a in self._assets(hub_id):
                if a.get("kind") == "file":
                    if a.get("filename"):
                        media["name"] = str(a["filename"])
                    size = a.get("advertisedByteLength") or a.get("byteLength")
                    if isinstance(size, int) and size > 0:
                        media["size"] = size
                    break
        except (HubError, HubDown) as e:
            log.info("a file message's details were not read: %s", e)
        return media


SIGNED_OUT = "AgentWechat 里的微信还没登录：在 AgentWechat 桌面端扫码登录后，这里会自动连上"
OTHER_ACCOUNT = ("AgentWechat 里登录的是另一个微信号：这里只服务原来那个号，换回原来的号会自动连上（要改用新号，"
                 "先在 Persona 里清掉这个扩展的数据）")
