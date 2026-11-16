"""The Feishu / Lark backend through LarkAgentX (github.com/cv-cat/LarkAgentX, the package `larkx`).

LarkAgentX is installed by the user (the setting larkx_python) at the commit this file is written against
(larkx_commit, checked by pin.verify); it is never shipped with the extension. This module only calls its API:
the QR sign-in (larkx.auth.QrLogin), the session (larkx.auth.LarkAuth, larkx.client.LarkClient), the push
decoding (larkx.proto.decoders), the send packet (larkx.proto.builders), history by position, search, chat and user
lookups, the generic gateway call (LarkClient.api) and media downloads (larkx.media). The live connection is our own
loop over those pieces, so it can say when it is really up and stop when asked.

Credentials: LarkAuth keeps cookies, the device id, the app key, the user id and the CSRF token, and writes them to a
JSON file whenever it changes them. Here its file is _NoDisk: it never exists and what is written is dropped, so the
sign-in lives only in memory and in the dict handed to the core (LoginDone.credentials at sign-in, on_credentials
when LarkAgentX changed it), which the core seals in the hub's connector secrets. LARKX_HOME points into the
extension's data directory, so the only thing LarkAgentX creates on disk is that empty directory. Its loguru output
is switched off (its messages can carry URLs with tickets), and its .env loading is skipped: the settings come from
Persona.

Errors are BackendError with a Chinese sentence and never the text of a library's exception (it can hold a URL with
a ticket or a proxy's password), a token, a cookie or a message body.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import queue
import re
import secrets
import threading
import time
import types
from typing import Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import quote, unquote, urlsplit

from openmate_core.backend import (Account, Backend, BackendError, Chat, CredentialsExpired, LoginDone, LoginQR, Media,
                                   Message, Person, SendRejected)

from . import launch, pin

log = logging.getLogger("openmate.larkx")

DEFAULT_COMMIT = "97ad48e69e34bd8c78ee30f795b7c3759dd81659"
REGION_LABELS = {"feishu": "飞书", "lark": "Lark"}
NO_PYTHON = "还没有设置 LarkAgentX 的 Python（larkx_python）：请先自己安装 LarkAgentX，再在扩展设置里填它的 Python 路径"
SOCKS_MISSING = ("系统代理是 SOCKS 代理，但 LarkAgentX 的 Python 里没有装 python-socks：请在这个 Python 里执行 "
                 "pip install python-socks，或者在扩展设置里填一个 http:// 代理")
REF_PREFIX = "lx1"

# LarkAgentX's message types (larkx.proto.decoders.MSG_TYPE_NAMES) that the core names differently
CORE_TYPES = {"MEDIA": "VIDEO", "SHARE_GROUP_CHAT": "SHARE_CHAT", "SHARE_USER_CARD": "SHARE_USER"}
MEDIA_KINDS = {5: "image", 3: "file", 15: "video", 7: "voice", 10: "sticker"}
TEXT, POST, SYSTEM, CARD = 4, 2, 6, 14
MERGE_FORWARD, CALENDAR, SHARE_CALENDAR_EVENT, GENERAL_CALENDAR = 11, 12, 16, 18
# message types whose text an older version read poorly: their chats are read again once (core: render_version)
RENDER_VERSION = "5"
RERENDER_TYPES = ("CARD", "CALENDAR", "SHARE_CALENDAR_EVENT", "GENERAL_CALENDAR", "MERGE_FORWARD")
CARD_TEXT_MOST = 4000
TREE_LIST_MOST = 8
# in a card's JSON: what is drawn as text, and what is not (buttons, pickers, styles, links, the other languages)
_CARD_TEXT_KEYS = ("content", "text", "title", "subtitle", "plain_text", "lark_md", "label", "placeholder_text")
_CARD_SKIP_KEYS = {"style", "styles", "color", "font", "action", "actions", "behaviors", "url", "multi_url", "href",
                   "img_key", "image_key", "icon", "token", "payload", "value", "confirm", "extra", "config",
                   "i18n_header", "i18n_elements", "en_us", "ja_jp", "fallback", "tag", "template", "id", "element_id"}
_CARD_SKIP_TAGS = {"button", "action", "overflow", "select_static", "select_person", "multi_select_static",
                   "date_picker", "picker_time", "picker_datetime", "input", "img", "image", "hr", "checker"}
_ID_LIKE = re.compile(r"^[0-9a-fA-F-]{8,}$|^\d+$|^https?://")
_LEADING_ID = re.compile(r"^(\s*)(\d{15,20}):", re.M)
_CJK = re.compile(r"[\u3400-\u9fff]")
_ASCII_WORD = re.compile(r"^[A-Za-z0-9_.:#/@%+=-]+$")  # an identifier, a key, a colour or a path: not words
CHAT_P2P = 1
AT_TAG = 5
PUSH_CMD = 6

FIRST_POSITION = 1  # LarkAgentX reads history from position 1 up
HISTORY_ROUNDS = 4  # gaps (recalled messages) are filled by asking for older positions at most this often
CHAT_TTL = 600.0
NAME_TTL = 6 * 3600.0
NAME_MISS_TTL = 600.0
QR_LIFE = 300.0
QR_KEEP = 4
WS_MAX = 32 << 20
OPEN_TIMEOUT = 20.0
DELIVER_JOIN = 10.0

_TAGS = re.compile(r"</?[a-zA-Z][^>]*>")


# ---------------------------------------------------------------- settings and loading


def proxy_setting(value) -> str:
    """The http_proxy setting, checked: an http:// or https:// proxy, or "" for the system's."""
    s = str(value or "").strip()
    if not s:
        return ""
    u = urlsplit(s)
    try:
        u.port
    except ValueError:
        u = None
    if u is None or u.scheme not in ("http", "https") or not u.hostname:
        raise BackendError("代理只支持 http:// 或 https:// 开头的地址，例如 http://127.0.0.1:7890；留空就用系统设置", "config")
    return s


def use_proxy(url: str, env) -> None:
    """Send this process's requests (requests) and the live connection (websockets) through url. Both read the
    *_proxy variables; wss_proxy wins over a SOCKS proxy in the environment for websockets."""
    for name in ("http_proxy", "https_proxy", "ws_proxy", "wss_proxy"):
        env[name] = url
        env[name.upper()] = url


def _skip_dotenv() -> None:
    """larkx.config loads a .env file from wherever it finds one; the settings come from Persona instead."""
    try:
        import dotenv
    except ImportError:
        return
    dotenv.load_dotenv = lambda *a, **k: False


def load_larkx(data_dir: str, region: str) -> types.SimpleNamespace:
    """The parts of LarkAgentX this backend calls, imported in this interpreter."""
    os.environ["LARKX_HOME"] = os.path.join(data_dir, "larkx")
    os.environ["LARKX_REGION"] = region
    _skip_dotenv()
    try:
        import requests
        import websockets
        import websockets.exceptions

        from larkx import auth, client, media, region as region_mod
        from larkx.proto import builders, decoders, ids
        from larkx.proto import proto_pb2
    except ImportError as e:
        raise BackendError("在 LarkAgentX 的 Python 里导入 LarkAgentX 失败（缺少 %s）：请确认它装在这个 Python 里" % (e.name or "某个模块"), "config")
    except Exception as e:
        raise BackendError("加载 LarkAgentX 时出错（%s）" % type(e).__name__, "unknown")
    try:
        from loguru import logger

        logger.disable("larkx")
    except ImportError:
        pass
    return types.SimpleNamespace(auth=auth, client=client, media=media, region=region_mod, builders=builders,
                                 decoders=decoders, ids=ids, P=proto_pb2, requests=requests, websockets=websockets)


def create(config: Dict, data_dir: str) -> Backend:
    if not str(config.get("larkx_python") or "").strip():
        raise BackendError(NO_PYTHON, "config")
    why = launch.problem()
    if why:
        raise BackendError(why, "config")
    region = str(config.get("region") or "feishu").strip().lower()
    if region not in REGION_LABELS:
        raise BackendError("区域只能是 feishu（飞书）或 lark（Lark 国际版）", "config")
    commit = str(config.get("larkx_commit") or DEFAULT_COMMIT).strip().lower()
    if not re.match(r"^[0-9a-f]{40}$", commit):
        raise BackendError("LarkAgentX 版本（larkx_commit）要写 40 位的 git 提交号", "config")
    proxy = proxy_setting(config.get("http_proxy"))
    pin.verify(commit)
    if proxy:
        use_proxy(proxy, os.environ)
    return LarkXBackend(load_larkx(data_dir, region), region)


# ---------------------------------------------------------------- small helpers


class _NoDisk:
    """LarkAuth's credentials file, which is never on disk: it does not exist, and what LarkAuth writes is dropped
    (owner hears that LarkAgentX changed something, and hands the new credentials to the core)."""

    def __init__(self, owner: "LarkXBackend"):
        self.owner = owner
        self.auth = None

    def exists(self) -> bool:
        return False

    def read_text(self, *a, **k) -> str:
        raise FileNotFoundError("OpenMate keeps no LarkAgentX credentials file")

    def write_text(self, *a, **k) -> int:
        self.owner._auth_saved(self)
        return 0

    def __str__(self) -> str:
        return "（不写入磁盘）"

    __repr__ = __str__


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _seconds(v) -> float:
    t = float(_int(v))
    return t / 1000.0 if t > 1e12 else t  # LarkAgentX gives seconds; a millisecond time is taken as well


def _id(v) -> str:
    s = str(v or "").strip()
    return "" if s in ("", "0") else s


def sniff_image(data: bytes) -> str:
    """The image type data starts with ("" when it is not a picture this recognises)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1", b"msf1"):
        return "image/heic"
    if data[4:8] == b"ftyp" and data[8:12] == b"avif":
        return "image/avif"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    return ""


def media_ref(kind: str, chat_id: str, msg_id: str, key: str) -> str:
    """A ref that names the bytes for good: the message, its resource key, its chat (what the download needs)."""
    return ":".join([REF_PREFIX] + [quote(x, safe="") for x in (kind, chat_id, msg_id, key)])


def parse_ref(ref: str) -> Optional[Tuple[str, str, str, str]]:
    parts = (ref or "").split(":")
    if len(parts) != 5 or parts[0] != REF_PREFIX:
        return None
    kind, chat_id, msg_id, key = (unquote(p) for p in parts[1:])
    if kind not in MEDIA_KINDS.values() or not chat_id.isdigit() or not msg_id.isdigit() or not key:
        return None
    return kind, chat_id, msg_id, key


# ---------------------------------------------------------------- the backend


class LarkXBackend(Backend):
    """Thread-safe: the session, the sign-in flows and the caches are behind one lock; network calls run outside it."""

    render_version = RENDER_VERSION
    rerender_types = RERENDER_TYPES

    # timings (tests shorten them)
    poll_timeout = 0.2  # one QrLogin.wait round: a single poll (interval ≥ timeout), then back to the caller
    poll_interval = 0.25
    expiry_checks = 2  # a sign-in is gone only when the ticket is refused this many times in a row…
    expiry_wait = 2.0  # …this far apart (a refusal can be a hiccup, and "expired" makes the user scan again)
    up_grace = 3.0  # a connection that stayed open this long after the handshake is up, even before a frame came
    heartbeat = 30.0

    def __init__(self, lx: types.SimpleNamespace, region: str):
        self.lx = lx
        self.region_name = region
        self.region = lx.region.get_region(region)
        self._lock = threading.RLock()
        self._auth = None
        self._client = None
        self._me: Optional[Account] = None
        self._creds: Optional[dict] = None
        self._qr: Dict[str, dict] = {}
        self._chats: Dict[str, Tuple[float, Chat]] = {}
        self._names: Dict[Tuple[str, str], Tuple[float, str]] = {}
        self._peers: Dict[str, str] = {}  # dm chat id → the other person (the dms we opened)
        self._own: Optional[Tuple[str, str, float]] = None  # (account id, its chat with itself or "", ask again after)
        self._last_pos: Dict[str, int] = {}  # chat id → the newest position seen

    # ------------------------------------------------------------ the session

    def _new_auth(self):
        disk = _NoDisk(self)
        auth = self.lx.auth.LarkAuth(path=disk)
        auth.region = self.region
        disk.auth = auth
        return auth

    def _creds_of(self, auth, name: str) -> dict:
        return {
            "v": 1,
            "region": getattr(auth.region, "name", self.region_name),
            "cookies": dict(auth.cookies or {}),
            "device_id": auth.device_id or "",
            "app_key": auth.app_key or "",
            "user_id": str(auth.user_id or ""),
            "csrf_token": auth.csrf_token or "",
            "web_app_id": auth.web_app_id or "",
            "saved_at": float(auth.saved_at or 0),
            "user_name": name or "",
        }

    def _auth_saved(self, disk: _NoDisk) -> None:
        """LarkAuth saved its credentials: when they are the session's and changed, they go to the core."""
        with self._lock:
            if disk.auth is None or disk.auth is not self._auth or self._me is None:
                return  # a sign-in or restore in progress: it hands over the credentials itself
            new = self._creds_of(disk.auth, self._me.name)
            if new == self._creds:
                return
            self._creds = new
        self._rotated(new)

    def _rotated(self, creds: dict) -> None:
        cb = self.on_credentials
        if cb is not None:
            try:
                cb(dict(creds))
            except Exception:
                log.exception("handing over refreshed credentials failed")

    def _check_ticket(self, auth) -> None:
        """Raise CredentialsExpired when the service no longer gives this session a ticket (refused expiry_checks times
        in a row), BackendError when it cannot be asked."""
        for attempt in range(max(1, self.expiry_checks)):
            try:
                auth.get_ticket()
                return
            except self.lx.auth.AuthExpired:
                if attempt + 1 < self.expiry_checks:
                    time.sleep(self.expiry_wait)
            except Exception as e:
                raise self._net(e, "检查登录")
        raise CredentialsExpired()

    def _new_client(self, auth):
        try:
            return self.lx.client.LarkClient(auth)
        except self.lx.auth.AuthExpired:
            self._check_ticket(auth)  # gone for good: CredentialsExpired
            raise BackendError("连接%s时出错了，稍后自动重试" % self.region.label, "unreachable")
        except Exception as e:
            raise self._net(e, "连接")

    @staticmethod
    def _close_client(client) -> None:
        loop = getattr(client, "loop", None)  # LarkClient makes an event loop it only runs for its own listener
        if loop is not None and not getattr(client, "_loop_started", False):
            try:
                loop.close()
            except Exception:
                pass

    def _set_session(self, auth, client, acct: Account, creds: dict) -> None:
        with self._lock:
            old = self._client
            self._auth, self._client, self._me, self._creds = auth, client, acct, creds
        if old is not None and old is not client:
            self._close_client(old)

    def _session(self):
        with self._lock:
            return self._client, self._me

    def _signed_in(self):
        client, me = self._session()
        if client is None or me is None:
            raise BackendError("还没有登录%s" % self.region.label, "expired")
        return client, me

    # ------------------------------------------------------------ sign-in

    def _prune_qr(self, now: float) -> None:
        for state in [s for s, f in self._qr.items() if now - f["at"] > QR_LIFE]:
            del self._qr[state]
        while len(self._qr) >= QR_KEEP:
            del self._qr[min(self._qr, key=lambda s: self._qr[s]["at"])]

    def login_start(self) -> LoginQR:
        try:
            ql = self.lx.auth.QrLogin(self.region)
        except self.lx.auth.AuthExpired:
            raise BackendError("%s没有给出登录二维码，请稍后再试" % self.region.label, "unknown")
        except Exception as e:
            raise self._net(e, "生成登录二维码")
        content = str(getattr(ql, "qr_content", "") or "")
        if not content:
            raise BackendError("%s没有给出登录二维码，请稍后再试" % self.region.label, "unknown")
        state = "qr_" + secrets.token_urlsafe(18)
        now = time.monotonic()
        with self._lock:
            self._prune_qr(now)
            self._qr[state] = {"ql": ql, "at": now, "status": "waiting", "lock": threading.Lock()}
        return LoginQR(url=content, state=state)

    def login_poll(self, state: str) -> str:
        with self._lock:
            flow = self._qr.get(state)
        if flow is None:
            return "expired"
        if flow["status"] == "confirmed":
            return "confirmed"
        if time.monotonic() - flow["at"] > QR_LIFE:
            with self._lock:
                self._qr.pop(state, None)
            return "expired"
        seen: List = []
        with flow["lock"]:
            if flow["status"] == "confirmed":
                return "confirmed"
            try:
                ok, data = flow["ql"].wait(timeout=self.poll_timeout, interval=self.poll_interval,
                                           on_status=lambda status, step: seen.append(status))
            except Exception as e:
                raise self._net(e, "查询扫码结果")
            if ok:
                flow["status"] = "confirmed"
                return "confirmed"
        if data is not None:  # the flow ended without a sign-in: cancelled on the phone, an error, or expired
            with self._lock:
                self._qr.pop(state, None)
            return "expired"
        if seen and seen[-1] == 2:
            flow["status"] = "scanned"
        return "scanned" if flow["status"] == "scanned" else "waiting"

    def login_finish(self, state: str) -> LoginDone:
        with self._lock:
            flow = self._qr.pop(state, None)  # the flow's session holds the new cookies: forget it either way
        if flow is None or flow["status"] != "confirmed":
            raise BackendError("这次扫码登录已经失效，请重新扫码", "unknown")
        auth = self._new_auth()
        try:
            flow["ql"].finish(auth)
        except self.lx.auth.AuthExpired:
            raise BackendError("%s没有确认这次登录，请重新扫码" % self.region.label, "unknown")
        except Exception as e:
            raise self._net(e, "完成登录")
        if not auth.user_id or not auth.app_key:
            raise BackendError("登录成功了，但没拿到实时连接需要的账号信息，请稍后重新扫码", "unknown")
        client = self._new_client(auth)
        uid = str(auth.user_id)
        name = self._chatter_name(client, uid)
        acct = Account(uid, name)
        creds = self._creds_of(auth, name)
        self._set_session(auth, client, acct, creds)
        log.info("signed in (%s)", self.region_name)
        return LoginDone(credentials=dict(creds), account=acct)

    def restore(self, credentials: Dict) -> Account:
        c = credentials if isinstance(credentials, dict) else {}
        if not c.get("cookies") or not isinstance(c.get("cookies"), dict) or not c.get("user_id"):
            raise CredentialsExpired("保存的%s登录信息不完整，需要重新扫码登录" % self.region.label)
        region = str(c.get("region") or "feishu")
        if region != self.region_name:
            raise CredentialsExpired("区域设置换成了%s，之前的登录属于%s，需要重新扫码登录"
                                     % (REGION_LABELS.get(self.region_name, self.region_name), REGION_LABELS.get(region, region)))
        auth = self._new_auth()
        auth.cookies = dict(c["cookies"])
        auth.saved_at = float(c.get("saved_at") or 0)
        auth.device_id = str(c.get("device_id") or "")
        auth.app_key = str(c.get("app_key") or "")
        auth.user_id = str(c["user_id"])
        auth.csrf_token = str(c.get("csrf_token") or "")
        auth.web_app_id = str(c.get("web_app_id") or "")
        self._check_ticket(auth)
        client = self._new_client(auth)
        uid = str(auth.user_id or c["user_id"])
        name = str(c.get("user_name") or "") or self._chatter_name(client, uid)
        acct = Account(uid, name)
        creds = self._creds_of(auth, name)
        self._set_session(auth, client, acct, creds)
        if creds != {k: c.get(k) for k in creds}:
            self._rotated(creds)  # LarkAgentX filled something in (the app key, the name): seal the new copy
        return acct

    def logout(self) -> None:
        with self._lock:
            old = self._client
            self._auth = self._client = self._me = self._creds = None
            self._own = None
            self._qr.clear()
            self._names.clear()
        if old is not None:
            self._close_client(old)

    def close(self) -> None:
        self.logout()

    # ------------------------------------------------------------ the live link

    def listen(self, on_message, on_link, stop: threading.Event) -> None:
        client, me = self._session()
        if client is None or me is None:
            raise CredentialsExpired("还没有登录%s，需要扫码登录" % self.region.label)
        try:
            url = client.build_ws_url()
        except self.lx.auth.AuthExpired:
            self._check_ticket(client.auth)
            raise BackendError("没拿到%s实时连接的票据，稍后自动重试" % self.region.label, "unreachable")
        except Exception as e:
            raise self._net(e, "准备实时连接")
        inbox: "queue.Queue" = queue.Queue()
        worker = threading.Thread(target=self._deliver, args=(inbox, client, me, on_message),
                                  name="openmate-larkx-deliver", daemon=True)
        worker.start()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._connection(client, url, inbox, on_link, stop))
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            inbox.put(None)
            worker.join(DELIVER_JOIN)

    async def _connection(self, client, url: str, inbox: "queue.Queue", on_link, stop: threading.Event) -> None:
        wsx = self.lx.websockets
        exc = wsx.exceptions
        proxy_errors = tuple(c for c in (getattr(exc, "InvalidProxy", None), getattr(exc, "ProxyError", None)) if c)
        try:
            ws = await asyncio.wait_for(wsx.connect(url, open_timeout=OPEN_TIMEOUT, max_size=WS_MAX), OPEN_TIMEOUT + 10)
        except ImportError:
            raise BackendError(SOCKS_MISSING, "proxy")
        except proxy_errors:
            raise BackendError("连不上代理，%s的实时连接建不起来：请检查代理设置" % self.region.label, "proxy")
        except exc.InvalidStatus as e:
            status = _int(getattr(getattr(e, "response", None), "status_code", 0))
            if status in (401, 403):
                self._check_ticket(client.auth)
            raise BackendError("%s拒绝了实时连接（HTTP %d），稍后自动重试" % (self.region.label, status), "unreachable")
        except (OSError, EOFError, asyncio.TimeoutError, exc.WebSocketException):
            raise BackendError("连不上%s的实时消息服务" % self.region.label, "unreachable")
        if stop.is_set():
            await self._close_ws(ws)
            return
        up = [False]

        def mark_up() -> None:
            if not up[0] and not stop.is_set():
                up[0] = True
                on_link("up", "")

        loop = asyncio.get_running_loop()
        grace = loop.call_later(self.up_grace, mark_up)
        tasks = [asyncio.ensure_future(client.heartbeat_loop(ws, self.heartbeat)),
                 asyncio.ensure_future(self._watch_stop(ws, stop))]
        try:
            try:
                await client.send_heartbeat(ws)
            except Exception:
                pass  # a connection that is already gone ends the loop below
            try:
                async for raw in ws:
                    mark_up()
                    await self._frame(client, ws, raw, inbox)
            except exc.ConnectionClosed:
                pass
        finally:
            grace.cancel()
            for t in tasks:
                t.cancel()
            await self._close_ws(ws)
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    async def _close_ws(ws) -> None:
        try:
            await ws.close()
        except Exception:
            pass

    @staticmethod
    async def _watch_stop(ws, stop: threading.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(0.2)
        try:
            await ws.close()
        except Exception:
            pass

    async def _frame(self, client, ws, raw, inbox: "queue.Queue") -> None:
        d = self.lx.decoders
        try:
            _, packet = d.parse_ws_frame(raw)
        except Exception:
            return
        sid, cmd = packet.get("sid"), packet.get("cmd")
        if sid is not None:
            try:
                await client.send_ack(ws, sid)
            except self.lx.websockets.exceptions.ConnectionClosed:
                raise
            except Exception:
                pass
        if cmd != PUSH_CMD:
            return
        try:
            pushed = d.decode_push_messages(raw)
        except Exception:
            log.info("a push could not be decoded")
            return
        for m in pushed:
            if m.get("from_id"):
                inbox.put(m)

    def _deliver(self, inbox: "queue.Queue", client, me: Account, on_message) -> None:
        """Maps pushed messages and hands them to the core in order, off the connection's loop (the core may look
        names up), so acks and heartbeats never wait for it."""
        while True:
            item = inbox.get()
            if item is None:
                return
            try:
                msg = self._message(item, me.id, client)
            except Exception as e:
                log.warning("a pushed message could not be read (%s)", type(e).__name__)
                continue
            if msg is None:
                continue
            try:
                on_message(msg)
            except Exception as e:
                log.warning("a pushed message was not taken (%s)", type(e).__name__)

    # ------------------------------------------------------------ messages

    def _message(self, d: dict, me_id: str, client) -> Optional[Message]:
        """One LarkAgentX message dict (decode_push_messages, pull_history) as the core's Message."""
        dec = self.lx.decoders
        msg_id, chat_id = _id(d.get("msg_id")), _id(d.get("chat_id"))
        if not msg_id or not chat_id:
            return None
        mt = dec.enum_to_int(d.get("msg_type"))
        name = dec.MSG_TYPE_NAMES.get(mt) or str(d.get("msg_type_name") or "") or "UNKNOWN"
        data = d.get("content_data")
        data = data if isinstance(data, dict) else None
        root, parent, anchor = _id(d.get("root_id")), _id(d.get("parent_id")), _id(d.get("anchor"))
        # LarkAgentX's anchor is the message's threadId (else its rootId). A reply in a thread has the thread's root
        # as its threadId; a reply that quotes in the main stream has its own id there (every message in an
        # ordinary group may open a thread), so its root is only the quoted chain's.
        in_thread = bool(root) and anchor == root
        topic_root = not root and d.get("scope") == "topic" and anchor == msg_id  # a thread-first group's root
        thread_id = anchor if in_thread else (msg_id if topic_root else "")
        sender = _id(d.get("from_id"))
        if sender and sender != me_id and _int(d.get("chat_type")) == CHAT_P2P:
            self._learn_peer(chat_id, sender)
        mentions = self._mentions(data)
        at_all = "all" in mentions
        at_me = bool(me_id) and (me_id in mentions or (bool(d.get("at_me")) and not at_all))
        return Message(
            id=msg_id, chat_id=chat_id, position=self._position(chat_id, d.get("position"), client),
            sender_id=sender, mine=bool(me_id) and sender == me_id, type=CORE_TYPES.get(name, name),
            text=self._read_text(mt, name, data, str(d.get("content") or "")), at=_seconds(d.get("create_time")),
            root_id=root if in_thread else "", thread_id=thread_id, parent_id=parent, at_me=at_me, at_all=at_all,
            media=self._media_of(mt, data, chat_id, msg_id))

    def _learn_peer(self, chat_id: str, user_id: str) -> None:
        """The other person of a direct chat, from their message in it (the chat's own record may have no name)."""
        with self._lock:
            if self._peers.get(chat_id) == user_id:
                return
            self._peers[chat_id] = user_id
            hit = self._chats.get(chat_id)
            if hit is not None and not hit[1].name:
                del self._chats[chat_id]  # read again, named after them

    def _position(self, chat_id: str, value, client) -> int:
        """A message's position; one without (a reply in a thread may have none in the main stream) takes the
        newest position seen in its chat, so it sorts with the messages of its time."""
        p = _int(value)
        with self._lock:
            known = self._last_pos.get(chat_id)
            if p > 0:
                if known is None or p > known:
                    self._last_pos[chat_id] = p
                return p
        if known is None:
            info = None
            try:
                info = client.get_chat_info(chat_id)
            except Exception:
                pass
            known = _int((info or {}).get("lastMessagePosition"))
            with self._lock:
                known = max(known, self._last_pos.get(chat_id, 0))
                self._last_pos[chat_id] = known
        return max(known, FIRST_POSITION)

    def _card_text(self, data: dict) -> str:
        """A card: its header, description, rich text and the text of its JSON body (not its buttons or styles)."""
        header = data.get("cardHeader") or {}
        lines = [str(header.get("title") or header.get("mainTitle") or ""), str(header.get("subtitle") or ""),
                 str(data.get("cardDesc") or "")]
        rich = data.get("richtext") or data.get("richText")
        if isinstance(rich, dict):
            try:
                lines.append(str(self.lx.decoders.extract_rich_text(rich) or ""))
            except Exception:
                pass
        raw = data.get("jsonCard")
        if isinstance(raw, str) and raw.strip().startswith(("{", "[")):
            try:
                lines += card_json_texts(json.loads(raw))
            except ValueError:
                pass
        if not any(str(x).strip() for x in lines):  # a newer card: its content is in a bytes field
            for key in ("universalCardEntity", "openCardContent"):
                words, _ = loose_texts(data.get(key), decode=True)
                lines += words
        text = clip_lines(lines, CARD_TEXT_MOST)
        if not text:  # LarkAgentX handed over nothing of it (or only its link): say it is there
            url = str(data.get("actionUrl") or "").strip()
            text = "[卡片] " + url if url.startswith("https://") else "[卡片]（内容请在飞书里查看）"
        return text

    def _named(self, text: str) -> str:
        """LarkAgentX's lines of a merged forward start with the sender's id: their names instead, where known."""
        cache: Dict[str, str] = {}

        def name(m) -> str:
            uid = m.group(2)
            if uid not in cache:
                try:
                    cache[uid] = self.user_name(uid) or uid
                except Exception:
                    cache[uid] = uid
            return "%s%s:" % (m.group(1), cache[uid])

        return _LEADING_ID.sub(name, text or "")

    def _rich_text(self, data: dict) -> str:
        return str(self.lx.decoders.extract_rich_text(data.get("richText") or {}) or data.get("text") or "")

    def _read_text(self, mt: int, name: str, data: Optional[dict], summary: str) -> str:
        text = self._text(mt, name, data, summary)
        if mt in (CARD, SHARE_CALENDAR_EVENT) and data is not None and (not text or text == summary):
            note_shape(name, data)
        return text

    def _text(self, mt: int, name: str, data: Optional[dict], summary: str) -> str:
        if data is None:
            # not decoded: LarkAgentX's line is a placeholder or a decoding error; say what it is, nothing more
            if mt == GENERAL_CALENDAR:
                return "[日程邀请]（详情请在飞书里查看）"  # LarkAgentX hands over nothing of these
            return "" if mt in MEDIA_KINDS or mt == SYSTEM else "[不支持的消息：%s]" % name
        if mt == TEXT:
            return self._rich_text(data)
        if mt == POST:
            title, body = str(data.get("title") or ""), self._rich_text(data)
            return "%s\n%s" % (title, body) if title and body else (title or body)
        if mt in MEDIA_KINDS:
            return ""  # the media and the core's placeholder say it
        if mt == CARD:
            return self._card_text(data)
        if mt == CALENDAR:
            return calendar_text(data.get("calendar")) or summary
        if mt == SHARE_CALENDAR_EVENT:
            return share_calendar_text(data) or summary
        if mt == MERGE_FORWARD:
            return self._named(summary)
        if mt == SYSTEM:
            texts = []
            for cv in (data.get("sysContentValues") or {}).values():
                for item in (cv or {}).get("items") or []:
                    if isinstance(item, dict) and item.get("text"):
                        texts.append(str(item["text"]))
            return " ".join(texts)
        return summary  # LarkAgentX's description: a forward, a location, a shared chat or card, a meeting…

    def _mentions(self, data: Optional[dict]) -> Set[str]:
        """The user ids a rich text @-mentions ("all" for @all)."""
        out: Set[str] = set()
        rich = (data or {}).get("richText") or {}
        elements = ((rich.get("elements") or {}).get("dictionary")) or {}
        for el in elements.values():
            if not isinstance(el, dict) or self.lx.decoders.enum_to_int(el.get("tag")) != AT_TAG or not el.get("property"):
                continue
            try:
                at = self.lx.P.AtProperty()
                at.ParseFromString(el["property"])
            except Exception:
                continue
            if at.userId:
                out.add(str(at.userId))
        return out

    def _media_of(self, mt: int, data: Optional[dict], chat_id: str, msg_id: str) -> List[Media]:
        kind = MEDIA_KINDS.get(mt)
        if kind is None or data is None:
            return []
        try:
            key = str(self.lx.media.extract_resource_key(mt, data) or "")
        except Exception:
            key = ""
        m = Media(kind, ref=media_ref(kind, chat_id, msg_id, key) if key else "")
        if mt == 5:
            v2 = data.get("imageV2") or {}
            for variant in v2.get("variants") or []:
                if isinstance(variant, dict) and _int(variant.get("type")) == 1:
                    size = variant.get("size") or {}
                    m.width, m.height = _int(size.get("width")), _int(size.get("height"))
                    break
            if not m.width:
                origin = (data.get("image") or {}).get("origin") or {}
                m.width, m.height = _int(origin.get("width")), _int(origin.get("height"))
        elif mt in (3, 15):
            m.name, m.size, m.mime = str(data.get("name") or ""), _int(data.get("size")), str(data.get("mime") or "")
        elif mt == 7:
            m.size = _int(data.get("size"))
        elif mt == 10:
            m.width, m.height = _int(data.get("width")), _int(data.get("height"))
        return [m]

    # ------------------------------------------------------------ reads

    def history(self, chat_id: str, before_position: Optional[int] = None, limit: int = 50) -> List[Message]:
        client, me = self._signed_in()
        if limit <= 0:
            return []
        if before_position is None:
            info = self._chat_raw(client, chat_id)
            hi = _int(info.get("lastMessagePosition")) + 1
        else:
            hi = _int(before_position)
        out: Dict[str, Message] = {}
        for _ in range(HISTORY_ROUNDS):
            missing = limit - len(out)
            if missing <= 0 or hi <= FIRST_POSITION:
                break
            lo = max(FIRST_POSITION, hi - missing)
            try:
                got = client.pull_history(chat_id, list(range(lo, hi)))
            except Exception as e:
                raise self._net(e, "读取历史消息")
            for d in got or []:
                m = self._message(d, me.id, client)
                if m is not None and (before_position is None or m.position < before_position):
                    out[m.id] = m
            hi = lo
        msgs = sorted(out.values(), key=lambda m: (m.position, m.at))
        return msgs[-limit:]

    def _chat_raw(self, client, chat_id: str) -> dict:
        try:
            info = client.get_chat_info(chat_id)
        except Exception as e:
            raise self._net(e, "读取会话信息")
        if not info:
            raise BackendError("没有这个%s会话" % self.region.label, "not_found")
        chat = self._chat_of(chat_id, info)
        with self._lock:
            self._chats[chat_id] = (time.monotonic() + CHAT_TTL, chat)
            last = _int(info.get("lastMessagePosition"))
            if last > self._last_pos.get(chat_id, 0):
                self._last_pos[chat_id] = last
        return info

    def _chat_of(self, chat_id: str, info: dict) -> Chat:
        dec = self.lx.decoders
        kind = "dm" if dec.enum_to_int(info.get("type")) == CHAT_P2P else "group"
        name = str(info.get("name") or "")
        if not name:
            names = info.get("i18nNames") or {}
            if isinstance(names, dict) and names:
                name = str(names.get(self.region.locale.lower()) or names.get(self.region.locale) or next(iter(names.values())) or "")
        try:
            topic = bool(dec.is_thread_chat(info))
        except Exception:
            topic = False
        with self._lock:
            peer = self._peers.get(chat_id, "")
        if kind == "dm" and not name and not peer:
            # nobody else has written in it: it may be the account's chat with itself, which has no name of its own
            _, me = self._session()
            if me is not None and me.id and chat_id == self._own_chat():
                peer = me.id
        if kind == "dm" and not name and peer:
            name = self.user_name(peer)
        members = _int(info.get("memberCount")) or _int(info.get("userCount"))
        return Chat(id=chat_id, kind=kind, name=name, peer_id=peer if kind == "dm" else "", topic=topic and kind == "group",
                    members=members if kind == "group" else 0)

    def _own_chat(self) -> str:
        """The account's chat with itself, "" when unknown: Feishu's answer to opening a direct chat with oneself (it
        opens nothing new, and sends nothing). Asked once per account, and only for a direct chat with no name and
        nobody else known in it; a failure is asked again an hour later."""
        client, me = self._session()
        if client is None or me is None or not me.id:
            return ""
        with self._lock:
            hit = self._own
        if hit is not None and hit[0] == me.id and (hit[1] or hit[2] > time.monotonic()):
            return hit[1]
        try:
            chat_id = _id(client.create_chat(me.id))
        except Exception as e:
            log.info("the chat with yourself is not known: %s", type(e).__name__)
            chat_id = ""
        with self._lock:
            self._own = (me.id, chat_id, time.monotonic() + 3600.0)
        return chat_id

    def recent_chats(self, limit: int) -> List[Chat]:
        client, _ = self._signed_in()
        if limit <= 0:
            return []
        chats: List[Chat] = []
        failed: Optional[Exception] = None
        for how in (self._recent_from_chats, self._recent_from_feed):
            try:
                chats = how(client, limit)
            except Exception as e:
                failed = failed or e
                continue
            if chats:
                break
        if not chats and failed is not None:
            raise self._net(failed, "读取最近的会话")
        now = time.monotonic()
        with self._lock:
            for c in chats:
                self._chats[c.id] = (now + CHAT_TTL, c)
        return chats[:limit]

    def _recent_from_chats(self, client, limit: int) -> List[Chat]:
        resp = client.api("chats.PullRecentChatsRequest", {"count": int(limit)}) or {}
        raw = resp.get("chats") or {}
        order = [str(x) for x in resp.get("orderedChatIds") or [] if str(x) in raw]
        if not order:
            order = sorted(raw, key=lambda k: -_int((raw[k] or {}).get("updateTime")))
        return [self._chat_of(cid, raw[cid]) for cid in order if raw.get(cid)]

    def _recent_from_feed(self, client, limit: int) -> List[Chat]:
        resp = client.api("feed.PullFeedCardsRequest", {"type": 1, "pullType": 1, "count": int(limit), "pullFeedType": 1}) or {}
        raw = resp.get("chats") or {}
        out, seen = [], set()
        for card in resp.get("cards") or []:
            cid = str((card or {}).get("id") or "")
            if cid in raw and cid not in seen:
                seen.add(cid)
                out.append(self._chat_of(cid, raw[cid]))
        return out

    def search(self, query: str, limit: int = 20) -> List[Person]:
        client, _ = self._signed_in()
        q = (query or "").strip()
        if not q:
            return []
        try:
            hits = client.search(q) or []
        except Exception as e:
            raise self._net(e, "搜索")
        out: List[Person] = []
        for h in hits:
            kind = "user" if h.get("type") == "user" else "group" if h.get("type") == "group" else ""
            hid = _id(h.get("id"))
            name = _TAGS.sub("", str(h.get("title") or "")).strip()
            if kind and hid:
                out.append(Person(kind, hid, name or hid))
        return out[: max(0, limit)]

    def chat_info(self, chat_id: str) -> Chat:
        with self._lock:
            hit = self._chats.get(chat_id)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]
        client, _ = self._signed_in()
        self._chat_raw(client, chat_id)
        with self._lock:
            return self._chats[chat_id][1]

    def user_name(self, user_id: str, chat_id: Optional[str] = None) -> str:
        user_id = _id(user_id)
        if not user_id:
            return ""
        client, me = self._session()
        if me is not None and user_id == me.id and me.name:
            return me.name
        key = (user_id, chat_id or "")
        now = time.monotonic()
        with self._lock:
            hit = self._names.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
        if client is None:
            return ""
        name = ""
        if chat_id and chat_id.isdigit() and user_id.isdigit():
            try:
                name = str(client.get_user_name(user_id, chat_id) or "")
            except Exception:
                name = ""
        if not name:
            name = self._chatter_name(client, user_id)
        with self._lock:
            self._names[key] = (now + (NAME_TTL if name else NAME_MISS_TTL), name)
        return name

    def _chatter_name(self, client, user_id: str) -> str:
        try:
            resp = client.api("chatters.PullChattersByIdsRequest", {"chatterIds": [str(user_id)]}) or {}
        except Exception:
            return ""
        chatters = resp.get("chatters") or {}
        c = chatters.get(str(user_id))
        if c is None and len(chatters) == 1:  # keyed otherwise: the one answered, unless it says it is someone else
            only = next(iter(chatters.values())) or {}
            if isinstance(only, dict) and str(only.get("id") or user_id) == str(user_id):
                c = only
        c = c if isinstance(c, dict) else {}
        return str(c.get("name") or c.get("alias") or c.get("enUsName") or "")

    def media(self, ref: str, max_bytes: int) -> Tuple[str, bytes]:
        parsed = parse_ref(ref)
        if parsed is None:
            raise BackendError("没有这个附件", "not_found")
        kind, chat_id, msg_id, key = parsed
        client, _ = self._signed_in()
        R = self.lx.requests.exceptions
        try:
            url = self.lx.media.message_resource_url(msg_id, key, chat_id, host=client.auth.region.file_host)
            data, ctype = self.lx.media.download(client.auth, url, max_bytes=max_bytes)
        except R.HTTPError as e:
            status = _int(getattr(getattr(e, "response", None), "status_code", 0))
            if status in (404, 410):
                raise BackendError("附件不在了（可能已被撤回或过期）", "not_found")
            if status in (401, 403):
                raise BackendError("没有权限下载这个附件", "rejected")
            raise BackendError("下载附件时%s出错了（HTTP %d）" % (self.region.label, status), "unreachable")
        except R.RequestException as e:
            raise self._net(e, "下载附件")
        except ValueError:  # larkx.media.download: more than max_bytes
            raise BackendError("附件超过 %d MB" % max(1, max_bytes >> 20), "too_large")
        except Exception as e:
            raise self._net(e, "下载附件")
        data = bytes(data or b"")
        if len(data) > max_bytes:
            raise BackendError("附件超过 %d MB" % max(1, max_bytes >> 20), "too_large")
        mime = str(ctype or "").split(";", 1)[0].strip().lower()
        picture = sniff_image(data)
        if kind in ("image", "sticker"):
            if not picture:
                raise BackendError("这张图片下载下来不是能识别的格式（可能是加密的图片），暂时不能预览", "unknown")
            mime = picture
        elif not mime or mime in ("application/octet-stream", "binary/octet-stream"):
            mime = picture or "application/octet-stream"
        return mime, data

    # ------------------------------------------------------------ writes

    def _is_dm(self, client, chat_id: str) -> bool:
        with self._lock:
            hit = self._chats.get(chat_id)
            if chat_id in self._peers:
                return True
        if hit is not None:
            return hit[1].kind == "dm"
        try:
            return self.chat_info(chat_id).kind == "dm"
        except BackendError:
            return False  # not known: an ordinary group's way (a thread) is what the core asked for

    def send(self, chat_id: str, text: str, reply_to: Optional[str] = None, root: Optional[str] = None) -> str:
        if not (text or "").strip():
            raise SendRejected("消息内容是空的")
        chat_id = str(chat_id or "").strip()
        if not chat_id.isdigit():
            raise SendRejected("%s会话 id 不对" % self.region.label)
        reply_to, root = _id(reply_to), _id(root)
        for v in (reply_to, root):
            if v and not v.isdigit():
                raise SendRejected("要回复的%s消息 id 不对" % self.region.label)
        client, _ = self._session()
        if client is None:
            raise SendRejected("还没有登录%s" % self.region.label)
        b = self.lx.builders
        try:
            rid = self.lx.ids.generate_long_request_id()
            if root and not self._is_dm(client, chat_id):
                # into the thread on root (LarkAgentX opens it when there is none yet); a thread-first group takes
                # the reply into the topic without the reply-in-thread mark
                pkt = b.build_send_message_packet(text, chat_id, rid, root_id=root,
                                                  thread_chat=bool(client.is_thread_chat(chat_id)))
            elif root or reply_to:
                # a reply that quotes and stays in the main stream (a direct chat has no threads here):
                # thread_chat=True is the builder's way to leave the reply-in-thread mark out
                pkt = b.build_send_message_packet(text, chat_id, rid, root_id=reply_to or root, thread_chat=True)
            else:
                pkt = b.build_send_message_packet(text, chat_id, rid)
        except Exception as e:
            raise SendRejected("没能组成这条%s消息（%s）" % (self.region.label, type(e).__name__))
        try:
            content = client._gateway_post(pkt)
        except Exception as e:
            raise self._send_error(e)
        try:
            msg_id = _id(b.decode_put_message_response(content))
        except Exception:
            raise BackendError("%s收下了这条消息，但回答看不懂，不确定是否发出" % self.region.label, "unknown")
        if not msg_id:
            raise BackendError("%s的回答里没有消息 id，不确定是否发出" % self.region.label, "unknown")
        return msg_id

    def _send_error(self, e: Exception) -> BackendError:
        """What a failed send post means: SendRejected only when nothing can have reached the service."""
        R = self.lx.requests.exceptions
        label = self.region.label
        if isinstance(e, R.ConnectTimeout):
            return SendRejected("连不上%s（连接超时），消息没有发出" % label)
        if isinstance(e, R.ProxyError):
            return SendRejected("连不上代理，消息没有发出")
        if isinstance(e, (R.InvalidSchema, R.InvalidURL, R.MissingSchema)):
            return SendRejected("代理或地址设置不能用，消息没有发出")
        if isinstance(e, R.ConnectionError) and _never_connected(e):
            return SendRejected("连不上%s，消息没有发出" % label)
        if isinstance(e, R.HTTPError):
            status = _int(getattr(getattr(e, "response", None), "status_code", 0))
            if 400 <= status < 500:
                return SendRejected("%s拒绝了这条消息（HTTP %d）" % (label, status))
            return BackendError("%s出错了（HTTP %d），不确定消息是否发出" % (label, status), "unknown")
        return BackendError("发送时和%s的连接出错，不确定消息是否发出" % label, "unreachable")

    def create_chat(self, user_id: str) -> str:
        user_id = _id(user_id)
        if not user_id.isdigit():
            raise BackendError("%s用户 id 不对" % self.region.label, "not_found")
        client, _ = self._signed_in()
        try:
            chat_id = _id(client.create_chat(user_id))
        except Exception as e:
            raise self._net(e, "打开私聊")
        if not chat_id:
            raise BackendError("%s没有给出和这个人的私聊" % self.region.label, "not_found")
        with self._lock:
            self._peers[chat_id] = user_id
            hit = self._chats.get(chat_id)
            if hit is not None:
                hit[1].peer_id = user_id
        return chat_id

    # ------------------------------------------------------------ errors

    def _net(self, e: Exception, what: str) -> BackendError:
        """A library's exception as a sentence for people (never its own text)."""
        if isinstance(e, BackendError):
            return e
        R = self.lx.requests.exceptions
        label = self.region.label
        if isinstance(e, R.ProxyError):
            return BackendError("连不上代理（%s时）：请检查代理设置" % what, "proxy")
        if isinstance(e, R.InvalidSchema):
            return BackendError("代理不能用（%s时）：代理要写 http:// 或 https://；系统代理是 SOCKS 时，LarkAgentX 的 Python 里要装 PySocks" % what, "proxy")
        if isinstance(e, (R.ConnectionError, R.Timeout)):
            return BackendError("连不上%s（%s时）" % (label, what), "unreachable")
        if isinstance(e, R.HTTPError):
            status = _int(getattr(getattr(e, "response", None), "status_code", 0))
            return BackendError("%s回答 HTTP %d（%s时）" % (label, status, what), "not_found" if status == 404 else "unreachable")
        if isinstance(e, ValueError):
            return BackendError("%s的回答看不懂（%s时）" % (label, what), "unknown")
        log.info("%s failed: %s", what, type(e).__name__)
        return BackendError("%s时出错了（%s）" % (what, type(e).__name__), "unknown")


def _never_connected(e: Exception) -> bool:
    """A requests ConnectionError from before anything was sent: the connection itself was never made."""
    reason = getattr(e.args[0], "reason", None) if e.args else None
    names = {type(x).__name__ for x in (reason, getattr(reason, "__cause__", None)) if x is not None}
    return bool(names & {"NewConnectionError", "NameResolutionError", "ConnectTimeoutError"})


# ---------------------------------------------------------------- reading rich messages


def clip_lines(lines: List[str], most: int) -> str:
    """Non-empty lines, each once (a card says its title in its header and its body), at most `most` characters."""
    out: List[str] = []
    seen = set()
    for line in lines:
        t = _TAGS.sub("", str(line or "")).strip()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    text = "\n".join(out)
    return text if len(text) <= most else text[: most - 1] + "…"


def card_json_texts(node, out: Optional[List[str]] = None) -> List[str]:
    """The text a card's JSON draws, in order: text-like fields, not buttons, pickers, styles or other languages."""
    if out is None:
        out = []
    if isinstance(node, list):
        for x in node:
            card_json_texts(x, out)
    elif isinstance(node, dict):
        if str(node.get("tag") or "") in _CARD_SKIP_TAGS:
            return out
        for k, v in node.items():
            if k in _CARD_SKIP_KEYS:
                continue
            if isinstance(v, str):
                if k in _CARD_TEXT_KEYS and v.strip() and not _ID_LIKE.match(v.strip()):
                    out.append(v)
            else:
                card_json_texts(v, out)
    return out


def _tree_text(block, eid: str, depth: int = 0) -> str:
    """One element of a Feishu element tree ({element_ids, elements: {id: {property, child_ids}}}) as text."""
    els = (block or {}).get("elements") or {}
    el = els.get(eid) or {}
    if depth > 8 or not isinstance(el, dict):
        return ""
    prop = el.get("property") or {}
    own = str(prop.get("content") or "").strip() if isinstance(prop, dict) else ""
    ids = [str(c) for c in el.get("child_ids") or []]
    kids = [_tree_text(block, c, depth + 1) for c in ids]
    if own and not any(kids):
        return own
    if len(ids) == 2 and ids[0].endswith("key") and kids[0] and kids[1] and not own:
        return "%s：%s" % (kids[0], kids[1])  # a field's name and its value
    parts = [t for t in ([own] + kids) if t]
    if len(parts) > TREE_LIST_MOST:  # a long list (the attendees): the first ones and how many
        parts = parts[:TREE_LIST_MOST] + ["等 %d 人" % len(parts)]
    return "、".join(parts)


def calendar_text(raw) -> str:
    """A calendar card (an invitation, a change, a reminder): its title and fields as one line, not its buttons."""
    if not isinstance(raw, str) or not raw.strip().startswith("{"):
        return ""
    try:
        d = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(d, dict):
        return ""
    parts = []
    for name in ("title", "contents"):
        block = d.get(name)
        if isinstance(block, dict):
            for eid in block.get("element_ids") or []:
                t = _tree_text(block, eid)
                if t:
                    parts.append(t)
    loc = str(d.get("location") or "").strip()
    if loc and not any(loc in p for p in parts):
        parts.append("地点：" + loc)
    if not parts and d.get("summary"):
        parts.append(str(d["summary"]))
    return ("[日程] " + " · ".join(parts)) if parts else ""


def _proto_fields(buf: bytes):
    """(field, value) of a protobuf message without its schema: ints for varints, bytes for length-delimited."""
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        if key is None:
            return
        field, wire = key >> 3, key & 7
        if wire == 0:
            v, i = _varint(buf, i)
            if v is None:
                return
            yield field, v
        elif wire == 2:
            ln, i = _varint(buf, i)
            if ln is None or i + ln > n:
                return
            yield field, buf[i:i + ln]
            i += ln
        elif wire == 1:
            i += 8
        elif wire == 5:
            i += 4
        else:
            return


def _varint(buf: bytes, i: int):
    shift = v = 0
    while i < len(buf) and shift < 70:
        b = buf[i]
        i += 1
        v |= (b & 0x7F) << shift
        if not b & 0x80:
            return v, i
        shift += 7
    return None, i


def _proto_strings(buf: bytes, out: List[str], times: List[int], depth: int = 0) -> None:
    """Readable strings and epoch-second times anywhere in a protobuf message (nested ones too)."""
    if depth > 6 or len(out) > 40:
        return
    for _, v in _proto_fields(buf):
        if isinstance(v, int):
            if 1_500_000_000 <= v <= 2_500_000_000:
                times.append(v)
            continue
        try:
            s = v.decode("utf-8")
        except UnicodeDecodeError:
            s = None
        if s is not None and s.isprintable() and s.strip():
            out.append(s.strip())
        elif len(v) > 1:
            _proto_strings(v, out, times, depth + 1)


def _wordy(s: str) -> bool:
    """A string people read: not an id, a link, a key, a colour or a JSON text."""
    t = s.strip()
    if len(t) < 2 or _ID_LIKE.match(t) or t.startswith(("{", "[", "http")):
        return False
    return bool(_CJK.search(t)) or (" " in t and not _ASCII_WORD.match(t.replace(" ", "")))


def loose_texts(value, decode: bool = False, depth: int = 0):
    """Readable strings and epoch-second times in a value of unknown shape: a dict or list (decoded already), a JSON
    text, or (decode) the base64 of a protobuf message. Best effort, in order, each string once."""
    words: List[str] = []
    times: List[int] = []

    def add(s: str) -> None:
        t = s.strip()
        if t.startswith(("{", "[")):
            try:
                walk(json.loads(t), depth + 1)
                return
            except ValueError:
                pass
        if _wordy(t) and t not in words:
            words.append(t)

    def walk(v, d: int) -> None:
        if d > 8 or len(words) > 60:
            return
        if isinstance(v, dict):
            if str(v.get("tag") or "") in _CARD_SKIP_TAGS:
                return
            for k, x in v.items():
                if k in _CARD_SKIP_KEYS:
                    continue
                if isinstance(x, int) and not isinstance(x, bool) and 1_500_000_000 <= x <= 2_500_000_000:
                    times.append(x)
                elif isinstance(x, str) and x.isdigit() and "time" in k.lower() and 1_500_000_000 <= int(x) <= 2_500_000_000:
                    times.append(int(x))
                else:
                    walk(x, d + 1)
        elif isinstance(v, list):
            for x in v:
                walk(x, d + 1)
        elif isinstance(v, str):
            add(v)

    if decode and isinstance(value, (bytes, bytearray)):
        buf = bytes(value)  # LarkAgentX hands some bytes fields over as they are
    elif decode and isinstance(value, str) and value and not value.lstrip().startswith(("{", "[")):
        try:
            buf = base64.b64decode(value + "=" * (-len(value) % 4))
        except (ValueError, TypeError):
            buf = b""
    else:
        buf = None
    if buf is not None:
        strings: List[str] = []
        _proto_strings(buf, strings, times)
        for x in strings:
            add(x)
    else:
        walk(value, depth)
    return words, times


def share_calendar_text(data: dict) -> str:
    """A shared calendar event: LarkAgentX keeps the event as bytes (or a decoded dict); its readable strings and
    times, best effort."""
    raw = data.get("calendarEvent")
    if not raw:
        return ""
    words, times = loose_texts(raw, decode=isinstance(raw, (str, bytes, bytearray)))
    words = words[:3]
    if not words:
        return ""
    parts = [" · ".join(words)]
    if times:
        lo, hi = min(times), max(times)
        stamp = time.strftime("%m-%d %H:%M", time.localtime(lo))
        if hi > lo:
            stamp += time.strftime("–%H:%M", time.localtime(hi)) if hi - lo < 86400 else time.strftime(" – %m-%d %H:%M", time.localtime(hi))
        parts.append(stamp)
    count = data.get("attendeesCount")
    if isinstance(count, int) and count > 0:
        parts.append("%d 人参与" % count)
    return "[分享日程] " + " · ".join(parts)


_SHAPES_SEEN: Set[str] = set()


def shape_of(value, depth: int = 0) -> str:
    """The shape of a decoded value, never its content: keys, types and sizes."""
    if depth > 3:
        return "…"
    if isinstance(value, dict):
        return "{" + ",".join("%s:%s" % (k, shape_of(v, depth + 1)) for k, v in sorted(value.items())[:24]) + "}"
    if isinstance(value, list):
        return "[%d×%s]" % (len(value), shape_of(value[0], depth + 1) if value else "")
    if isinstance(value, str):
        kind = "json" if value.lstrip().startswith(("{", "[")) else "str"
        return "%s~%d" % (kind, len(str(len(value))) if value else 0)  # its length's order only: one shape for many
    return type(value).__name__


def note_shape(name: str, data: dict) -> None:
    """A message read as nothing: its decoded shape in the log, once per shape (to learn where its text is)."""
    shape = shape_of(data)
    key = name + shape
    if key in _SHAPES_SEEN or len(_SHAPES_SEEN) > 200:
        return
    _SHAPES_SEEN.add(key)
    log.info("a %s message read as nothing; its shape: %s", name, shape[:1500])

