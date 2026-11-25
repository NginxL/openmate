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
import dataclasses
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

from openmate_core.backend import (Account, Backend, BackendError, Chat, CredentialsExpired, DocMention, LoginDone, LoginQR,
                                   Media, Message, Person, SendRejected)
from openmate_core.textutil import mention_spans, span_pieces

from . import DLP_BLOCKED_TEXT, launch, pin  # DLP_BLOCKED_TEXT: a blocked message's note (Message.blocked)

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
IMAGE = 5
IMAGE_MOST = 10 << 20  # a picture sent (the core checks and copies it first: images.take)
MERGE_FORWARD, CALENDAR, SHARE_CALENDAR_EVENT, GENERAL_CALENDAR = 11, 12, 16, 18
# message types whose text an older version read poorly: their chats are read again once (core: render_version)
RENDER_VERSION = "8"  # 7: a text whose content is empty takes the links of its preview hang points; 8: DLP marks
DLP_IN_PROGRESS, DLP_BLOCK = (1, "IN_PROGRESS"), (3, "BLOCK")  # Message.ContentState.dlpReviewState
RERENDER_EMPTY = ("TEXT", "POST")  # 6: a text that is only a link (an A element) was read as nothing
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
THREAD_REPLY = 2  # entities.Message.ThreadMessageType THREAD_REPLY_MESSAGE: a reply in a thread
SYNC_TARGET, SYNC_SOURCE = 1, 2  # entities.Message.SyncToChatMessageType: a thread reply's copy in the main stream, the reply
SYNCED_KEEP = 500  # how many messages' "also in the main stream" marks are remembered (a reply copies the request's)
AT_TAG = 5
STATUS_NORMAL, STATUS_MODIFIED = 1, 3  # entities.Message.Status: as sent, edited
CODE_BLOCK_TAGS = (31, 37)  # CODE_BLOCK, CODE_BLOCK_V2: their own properties, not a TextProperty
# DOCS (22) included: the newer rich text's block around its paragraphs (not a document; read "[文档]" before)
BLOCK_TAGS = frozenset((3, 4, 14, 22, 23, 24, 25, 26, 27, 28, 29, 31, 32, 37, 39, 40, 41, 42, 43, 44, 45, 46))
# RichTextElement.Tag: P, FIGURE, DIV, H1-H3, UL, OL, LI, QUOTE, CODE_BLOCK, HR, CODE_BLOCK_V2, TABLE…, COLUMNS, COLUMN,
# H4-H6: blocks (a new line where one starts, in an @自己 request); B, I, U, LINK, CODE, A run on in the line
PUSH_CMD = 6
PUSH_DOC_FEED_CMD = 14004  # improto.Command.PUSH_DOC_FEED: a document's notices (comments, @-mentions…)
PUSH_FEED_CARDS_CMD = 1002  # feed.PushFeedCardsRequest: feed cards changed, among them chats moved to 完成 (done)
FEED_DONE = (2, "DONE")  # entities.FeedType of a card in 完成, as a number (protobuf_to_dict) or a name (MessageToDict)
FEED_INBOX = (1, "INBOX")  # …and of one in the message list
FEED_CHAT = (1, "CHAT")  # entities.FeedCard.Type of a chat's card
FEED_GONE = (2, 3, "PENDING_DELETE", "DELETED")  # entities.FeedCard.Status of a card on its way out
DONE_ASK_MOST = 50  # chats asked about in one feed.PullFeedCardsByIdsRequest
PUSH_CHATS_CMD = 18  # chats.PushChatsRequest: chats changed (a group made, joined, renamed, left, dissolved)
PUSH_REACTIONS_CMD = 27  # reactions.PushReactions: a message's reactions changed
REACTIONS_ASK_MOST = 50  # messages asked about in one reactions.PullReactionsRequest
GROUPS_JOINED = 2  # chats.PullGroupChatsRequest.Type JOIN: the groups the account is in
GROUPS_PAGE = 100  # group ids per PullGroupChatsRequest…
GROUPS_PAGES = 200  # …at most this many pages (20000 groups)
GROUPS_INFO_MOST = 50  # groups per PullChatsByIdsRequest
GROUPS_EVERY = 3600.0  # every group is read again this often while the link is up (pushes say the changes between)
GROUPS_SPACING = 60.0  # and never more often than this
CHAT_GROUP_TYPES = (2, 3, "GROUP", "TOPIC_GROUP")  # entities.Chat.Type of a group
MENTIONS_MOST = 20  # people one message @-s at most
TOPIC_MODE_KEEP = 2000  # groups in topic mode remembered (_topic_mode)…
TOPIC_MODE_TTL = 3600.0  # …this long (a group may be switched back; a refusal teaches it again)
TOPIC_REFUSAL = 5801  # the error code of Feishu's refusal of a new message to a group in topic mode
TOPIC_TOO_OLD = "这个群的消息以话题形式展示，飞书要求更新的客户端才能在里面开新话题，这次没有发出；回复群里已有的话题可以发"
# A group in topic mode takes a new message (as a topic of its own) only from a client whose x-command-version is
# newer than the one LarkAgentX names (5.7.0: refused with TOPIC_REFUSAL, "update to the latest version"); the same
# packet with this one goes through (probed 2026-11-24, Feishu 8.7.x). Only that send carries it (newer_post).
NEWER_COMMAND_VERSION = "8.7.11"
CHAT_NOT_MEMBER = (2, 3, "VISITOR", "THREAD_FOLLOWER")  # entities.Chat.Role of one who only looks in
DOC_MENTION_TYPES = ("MENTION", "COMMENT", "REPLY", "1", "2", "3")  # DocMessage.Type

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


_extra_headers = threading.local()  # headers this thread's next gateway posts carry (newer_post), else none


class _HeaderedRequests:
    """What LarkAgentX's client module calls `requests`: the requests module itself, except that a post carries the
    headers the calling thread set (newer_post) over the ones LarkAgentX made. Nothing else changes, and no other
    module or thread sees it."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def post(self, *args, **kwargs):
        extra = getattr(_extra_headers, "value", None)
        if extra:
            kwargs["headers"] = dict(kwargs.get("headers") or {}, **extra)
        return self._real.post(*args, **kwargs)


def _headered(client_module) -> None:
    """Put _HeaderedRequests in place of LarkAgentX's client module's requests (once)."""
    real = getattr(client_module, "requests", None)
    if real is not None and not isinstance(real, _HeaderedRequests):
        client_module.requests = _HeaderedRequests(real)


def newer_post(client, pkt):
    """client._gateway_post(pkt) as a newer client: x-command-version NEWER_COMMAND_VERSION on this post only."""
    old = getattr(_extra_headers, "value", None)
    _extra_headers.value = {"x-command-version": NEWER_COMMAND_VERSION}
    try:
        return client._gateway_post(pkt)
    finally:
        _extra_headers.value = old


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
    _headered(client)
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
    from .agenda import LarkCLI

    b = LarkXBackend(load_larkx(data_dir, region), region)
    b.cli = LarkCLI(config)
    return b


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


def _packet_only(resp: dict, fields) -> bool:
    """An answer that is the gateway's packet (a status, a command) without any of the request's own fields: not an
    answer. An empty business answer leaves its empty fields out and carries no status."""
    return not any(f in resp for f in fields) and any(k in resp for k in ("status", "cmd", "payloadType", "sid"))


def group_over(info: dict) -> bool:
    """The details say the account's membership is over: dissolved, frozen, deleted, or only looking in."""
    return bool(info.get("isDissolved") or info.get("isFrozen") or info.get("status") not in (None, 0, "NORMAL")
                or info.get("role") in CHAT_NOT_MEMBER)


def group_alive(info: dict) -> bool:
    """A group the account is in and can use: a group's type, normal status, neither dissolved nor frozen, the
    account a member (not one who only looks in)."""
    return (info.get("type") in CHAT_GROUP_TYPES and info.get("status") in (None, 0, "NORMAL")
            and not info.get("isDissolved") and not info.get("isFrozen") and info.get("role") not in CHAT_NOT_MEMBER)


def reacted_by(d: dict, user_id: str, only: Optional[Set[str]] = None, ignore: Tuple[str, ...] = ()) -> Dict[str, float]:
    """{message id: when (seconds, 0 when unknown)} of the messages user_id has a reaction on, from a
    reactions.PullReactionsResponse or a reactions.PushReactions read back (protobuf_to_dict): each message's
    reactions with the users on them, and a response's own selfReactionStatus (not the cancelled ones). Reactions
    of the types in ignore do not count."""
    skip = {str(t).lower() for t in ignore if t}
    out: Dict[str, float] = {}
    uid = str(user_id or "")
    if not uid or not isinstance(d, dict):
        return out
    for mr in d.get("messageReactions") or []:
        mid = str((mr or {}).get("messageId") or "") if isinstance(mr, dict) else ""
        if not mid or (only is not None and mid not in only):
            continue
        for r in mr.get("reactions") or []:
            if not isinstance(r, dict) or str(r.get("type") or "").lower() in skip:
                continue
            users = {str(x) for x in (r.get("userIds") or []) + (r.get("chatterIds") or [])}
            if uid in users:
                times = r.get("udpateTimes") or {}
                t = _int(times.get(uid)) if isinstance(times, dict) else 0
                out[mid] = max(out.get(mid, 0.0), _seconds(t) if t else _int(r.get("nanoUpdateTime")) / 1e9)
    for st in d.get("selfReactionStatus") or []:
        if (not isinstance(st, dict) or st.get("isCancelled") or str(st.get("userId") or uid) != uid
                or str(st.get("reactionType") or "").lower() in skip):
            continue
        mid = str(st.get("messageId") or "")
        if mid and (only is None or mid in only):
            out[mid] = max(out.get(mid, 0.0), _int(st.get("nanoUpdateTime")) / 1e9)
    return out


def pushed_done(payload: bytes) -> Dict[str, float]:
    """The chats a feed.PushFeedCardsRequest moved to 完成 (done_cards)."""
    from google.protobuf.json_format import MessageToDict
    from larkx.proto import lark_all_pb2

    req = lark_all_pb2.feed.PushFeedCardsRequest()
    req.ParseFromString(payload)
    d = MessageToDict(req)
    return done_cards(d.get("cards"), d.get("type"))


def pushed_threads(payload: bytes) -> Dict[str, dict]:
    """What says where each message of a messages.PushMessagesRequest shows, which LarkAgentX's message dicts leave
    out: by message id, its thread kind (threadMessageType), and whether it also went to the main stream (sendToChat,
    or the syncToChat marks: syncToChatMessageType, the related message, the thread's root), and whether it carried
    the thread kind at all (thread_known; a proto2 field, unset reads as 0). Ids only, no content."""
    from larkx.proto import lark_all_pb2

    req = lark_all_pb2.messages.PushMessagesRequest()
    req.ParseFromString(payload)
    out: Dict[str, dict] = {}
    for key, m in req.messages.items():
        mid = _id(m.id) or _id(key)
        if mid:
            out[mid] = {"thread_type": int(m.threadMessageType), "thread_known": m.HasField("threadMessageType"),
                        "send_to_chat": bool(m.sendToChat),
                        "sync_type": int(m.syncToChatMessageType), "sync_related": _id(m.syncToChatRelatedMessageId),
                        "sync_root": _id(m.syncToChatThreadRootId),
                        "status": int(m.status) if m.HasField("status") else None,
                        "edited": push_edited(m)}
    return out


def push_edited(m) -> Optional[bool]:
    """Whether a pushed message entity is an edit: True (status MODIFIED, an edit version, an edit time after its
    creation), False only when it says status NORMAL with none of those, None when it does not say (no status, or
    another one: deleted…). Only a sure False may make a new request of an own @ of the account (@自己)."""
    if (m.HasField("status") and int(m.status) == STATUS_MODIFIED) or int(m.editVersion) > 0 or (
            m.HasField("editTimeMs") and int(m.editTimeMs) > int(m.createTimeMs)):
        return True
    return False if m.HasField("status") and int(m.status) == STATUS_NORMAL else None


_MARK_NAMES = {"THREAD_ROOT_MESSAGE": 1, "THREAD_REPLY_MESSAGE": 2, "SYNC_TO_CHAT_TARGET_MESSAGE": 1,
               "SYNC_TO_CHAT_SOURCE_MESSAGE": 2}  # entities.Message's enums by name


def entity_marks(entity: dict) -> dict:
    """pushed_threads' marks of one message entity as LarkClient.api answers it (a dict; enums as numbers or names),
    read from history (never remembered for a reply to copy: a history page must not push out a live request's)."""
    def num(v) -> int:
        return _MARK_NAMES.get(v, 0) if isinstance(v, str) and not v.isdigit() else _int(v)

    return {"thread_type": num(entity.get("threadMessageType")), "thread_known": "threadMessageType" in entity,
            "history": True, "send_to_chat": bool(entity.get("sendToChat")),
            "sync_type": num(entity.get("syncToChatMessageType")),
            "sync_related": _id(entity.get("syncToChatRelatedMessageId")),
            "sync_root": _id(entity.get("syncToChatThreadRootId"))}


def _thread_reply_synced(marks: dict) -> bool:
    """Whether the marks are a thread reply's also sent to the main stream (its side of syncToChat: the source, or
    the thread's root named), which only a reply in a thread has."""
    return marks.get("sync_type") == SYNC_SOURCE or bool(marks.get("sync_root"))


def synced_mark(marks: Optional[dict]) -> str:
    """How a message's marks (pushed_threads) say it also went to the main stream: "sync" (the syncToChat marks),
    "send" (sendToChat), "" (it did not, or nothing is known)."""
    if not marks:
        return ""
    if marks.get("sync_type") in (SYNC_TARGET, SYNC_SOURCE) or marks.get("sync_related"):
        return "sync"
    return "send" if marks.get("send_to_chat") else ""


def also_to_chat(pkt, parent_id: str, mark: str):
    """LarkAgentX's thread-reply packet (build_send_message_packet), made to go to the main stream as well: quoting
    parent_id (the request, in that thread) and marked the way the request was ("send": sendToChat, else syncToChat).
    Through lark_all_pb2's PutMessageRequest, which has both fields (the builder's proto_pb2 one has no syncToChat)."""
    from larkx.proto import lark_all_pb2

    req = lark_all_pb2.messages.PutMessageRequest()
    req.ParseFromString(pkt.payload)
    req.parentId = str(parent_id)
    if mark == "send":
        req.sendToChat = True
    else:
        req.syncToChat = True
    pkt.payload = req.SerializeToString()
    return pkt


def as_image(pkt, token: str):
    """LarkAgentX's text packet (build_send_message_packet, its place already decided: a thread, a topic, a quote)
    made a picture: type IMAGE, its content only the uploaded picture's cryptoToken (what the web client sends)."""
    from larkx.proto import lark_all_pb2

    req = lark_all_pb2.messages.PutMessageRequest()
    req.ParseFromString(pkt.payload)
    req.type = IMAGE
    req.ClearField("content")
    req.content.cryptoToken = str(token)
    pkt.payload = req.SerializeToString()
    return pkt


def image_token(body: bytes) -> str:
    """The cryptoToken of the file host's answer to an uploaded picture (pan.PutImageResponse), "" when it has none."""
    from larkx.proto import lark_all_pb2

    resp = lark_all_pb2.pan.PutImageResponse()
    resp.ParseFromString(bytes(body or b""))
    return str(resp.cryptoToken or "").strip()


def wire_varints(data: bytes) -> Optional[Dict[int, List[int]]]:
    """The top-level varint fields of a protobuf message, {field number: [values]}; None when it does not read to its
    end (cut off, or not a message)."""
    out: Dict[int, List[int]] = {}
    i, n = 0, len(data or b"")

    def varint() -> int:
        nonlocal i
        v, shift = 0, 0
        while i < n and shift < 64:
            c = data[i]
            i += 1
            v |= (c & 0x7F) << shift
            if not c & 0x80:
                if v >> 64:
                    raise ValueError("over 64 bits")
                return v
            shift += 7
        raise ValueError("cut off or too long")

    try:
        while i < n:
            key = varint()
            field, kind = key >> 3, key & 7
            if field < 1:
                return None
            if kind == 0:
                out.setdefault(field, []).append(varint())
            elif kind == 2:
                size = varint()  # (read before i moves on: "i += varint()" would add to the old i)
                i += size
            elif kind == 1:
                i += 8
            elif kind == 5:
                i += 4
            else:
                return None
    except ValueError:
        return None
    return out if i == n else None


def _element_id(taken: Set[str]) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    while True:
        eid = "".join(secrets.choice(alphabet) for _ in range(10))
        if eid not in taken:
            taken.add(eid)
            return eid


def with_mentions(pkt, text: str, mentions):
    """LarkAgentX's text packet (build_send_message_packet) with real @s: mentions [(user id, name, offset)], each
    "@name" at offset in text, become @ elements of those people, and the request lists who is @-ed (atUserIdList),
    so Feishu tells them. The rest of the text stays as it was written."""
    from larkx.proto import lark_all_pb2

    E = lark_all_pb2.entities.RichTextElement
    req = lark_all_pb2.messages.PutMessageRequest()
    req.ParseFromString(pkt.payload)
    names = {uid: name for uid, name, _ in mentions}
    rich = req.content.richText
    del rich.elementIds[:]
    del rich.atIds[:]
    rich.elements.dictionary.clear()
    taken: Set[str] = set()
    words: List[str] = []
    for kind, value in span_pieces(text, mentions):
        eid = _element_id(taken)
        el = rich.elements.dictionary[eid]
        if kind == "at":
            el.tag = E.AT
            el.property = E.AtProperty(userId=value, content="@" + (names.get(value) or value)).SerializeToString()
            rich.atIds.append(eid)
            words.append("@" + (names.get(value) or value))
        else:
            el.tag = E.TEXT
            el.property = E.TextProperty(content=value).SerializeToString()
            words.append(value)
        rich.elementIds.append(eid)
    rich.innerText = "".join(words)
    del req.atUserIdList[:]
    for uid in dict.fromkeys(uid for uid, _, _ in mentions):
        req.atUserIdList.append(int(uid))
    pkt.payload = req.SerializeToString()
    return pkt


def done_cards(cards, box=None, only: Optional[Set[str]] = None) -> Dict[str, float]:
    """{chat id: when it last changed (seconds, 0 when unknown)} of the chats' feed cards in 完成: by each card's
    feedType, else the push's own (box). Other cards (a thread's, a document's), cards on their way out and, with
    only, chats not asked about are left out."""
    out: Dict[str, float] = {}
    for c in cards or []:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "")
        if not cid or (only is not None and cid not in only) or c.get("type") not in FEED_CHAT:
            continue
        if c.get("feedType", box) not in FEED_DONE or c.get("status") in FEED_GONE:
            continue
        ms = _int(c.get("updateTimeMs"))
        out[cid] = ms / 1000.0 if ms > 0 else _seconds(c.get("updateTime"))
    return out


def _seconds(v) -> float:
    t = float(_int(v))
    return t / 1000.0 if t > 1e12 else t  # LarkAgentX gives seconds; a millisecond time is taken as well


def _id(v) -> str:
    s = str(v or "").strip()
    return "" if s in ("", "0") else s


def doc_mentions(d: dict, me_id: str) -> List[DocMention]:
    """The @-mentions of the user in a PushDocFeedRequest (as MessageToDict writes it): the document, who, what, and
    the comment's id (DocMessage.extra, a JSON string, names it)."""
    feed = d.get("docFeed") if isinstance(d.get("docFeed"), dict) else {}
    url, title = str(feed.get("url") or ""), str(feed.get("title") or "")
    out: List[DocMention] = []
    for key, m in sorted((d.get("docMessages") or {}).items()):
        if not isinstance(m, dict) or not m.get("isAtMe"):
            continue
        if str(m.get("type") or "") not in DOC_MENTION_TYPES:
            continue
        frm = _id(m.get("fromId"))
        if not frm or frm == me_id or not url:
            continue
        extra: dict = {}
        raw = m.get("extra")
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                v = json.loads(raw)
                extra = v if isinstance(v, dict) else {}
            except ValueError:
                extra = {}
        cid = _find_key(extra, ("comment_id", "commentId", "commentID", "comment_id_str"))
        text = str(m.get("content") or "").strip() or _plain(m.get("richText"))
        at = _int(m.get("createTime") or m.get("updateTime"))
        out.append(DocMention(id=_id(m.get("id")) or str(key), doc_url=url, doc_title=title, comment_id=cid, from_id=frm,
                              text=text, kind=str(m.get("type") or "").lower(), at=at / 1000.0 if at > 10 ** 12 else float(at)))
    return out


def _find_key(d, names, depth: int = 0) -> str:
    """The first value of one of names in a nested dict (a JSON's), as a string; "" when none."""
    if not isinstance(d, dict) or depth > 4:
        return ""
    for n in names:
        if d.get(n) not in (None, "", 0):
            return str(d[n])
    for v in d.values():
        if isinstance(v, dict):
            got = _find_key(v, names, depth + 1)
            if got:
                return got
    return ""


def _plain(rich) -> str:
    """The text of a RichText as MessageToDict writes it: its elements' text properties, in order."""
    out: List[str] = []

    def walk(v, depth=0):
        if depth > 8:
            return
        if isinstance(v, dict):
            for k, x in v.items():
                if k in ("text", "content") and isinstance(x, str):
                    out.append(x)
                else:
                    walk(x, depth + 1)
        elif isinstance(v, list):
            for x in v:
                walk(x, depth + 1)

    walk(rich)
    return "".join(out).strip()


def chatter_kind(v) -> str:
    """A Chatter's type (ChatterType: USER 1, BOT 2, AI 3; a name or a number): "user", "bot" or "" (unknown)."""
    s = str(v if v is not None else "").strip().upper()
    if s in ("BOT", "AI", "2", "3"):
        return "bot"
    if s in ("USER", "1"):
        return "user"
    return ""


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


_AVATAR_KEY = re.compile(r"[0-9A-Za-z_-]{1,200}")  # fullmatch: Feishu's picture keys (v3_…); nothing else is put in a URL


def avatar_address(file_host: str, key: str) -> str:
    """The https address of a Feishu picture by its key (a person's or a group's, "" for no key): the 240 px one the
    clients draw. It names that key's bytes for good (a new picture is a new key), and Feishu serves it without the
    session, so Persona fetches it itself (InboxThing.avatar, an address of the open internet)."""
    key = str(key or "").strip()
    host = str(file_host or "").rstrip("/")
    if not _AVATAR_KEY.fullmatch(key) or not host.startswith("https://"):
        return ""
    return "%s/static-resource/v1/%s~?image_size=240x240&cut_type=&quality=&format=image&sticker_format=.webp" % (host, key)


def image_key(v) -> str:
    """The key of an Image or ImageSet the service answered (a chat's avatar, a person's), "" when it names none."""
    if not isinstance(v, dict):
        return ""
    for k in (v.get("key"), v.get("imageKey"), (v.get("origin") or {}).get("key") if isinstance(v.get("origin"), dict) else ""):
        if k and str(k).strip():
            return str(k).strip()
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
    rerender_empty = RERENDER_EMPTY
    done_marks = True  # a chat's 完成
    has_avatars = True  # chat_info's Chat carries the chat's picture (avatar_address)
    thread_reply_quotes_root = True  # send() passes only the root to LarkAgentX's builder

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
        self._session_no = 0  # moves on at every sign-in and logout: a restore begun before one never takes the place
        self._me: Optional[Account] = None
        self._creds: Optional[dict] = None
        self._qr: Dict[str, dict] = {}
        self._chats: Dict[str, Tuple[float, Chat]] = {}
        self._names: Dict[Tuple[str, str], Tuple[float, str]] = {}
        self._person_avatars: Dict[str, Tuple[float, str]] = {}  # person → (good till, their picture's key or "")
        self._peers: Dict[str, str] = {}  # dm chat id → the other person (the dms we opened)
        self._own: Optional[Tuple[str, str, float]] = None  # (account id, its chat with itself or "", ask again after)
        self._last_pos: Dict[str, int] = {}  # chat id → the newest position seen
        # groups whose messages show as topics though their record does not say so (an ordinary group switched to 话题
        # 模式), learnt from a send Feishu refused so (TOPIC_REFUSAL): a reply there goes into the topic the thread
        # way, a new message cannot be posted (newest last)
        self._topic_mode: Dict[str, float] = {}
        self._synced: Dict[str, str] = {}  # message id → how it also went to the main stream (synced_mark), the newest
        self.cli = None  # agenda.LarkCLI for a reply in a document's comment (create sets it)
        self._doc_feeds_heard = False
        self._done_heard = False
        # every group the account is in (group_candidates): newest joined first, for the account that read them
        self._groups: List[Chat] = []
        self._groups_join: Dict[str, int] = {}
        self._groups_owner = ""
        self._groups_at = 0.0  # time.monotonic() of the last whole read (0: none yet)
        self._groups_whole_ids: Set[str] = set()  # every id the last whole read listed (details or not): the baseline
        self._groups_tried = 0.0
        self._groups_mu = threading.Lock()
        self._groups_busy = False
        self._groups_soon = False  # a push named a group the list lacks: read again at the next chance
        self._groups_delta: List[tuple] = []  # changes heard while a whole read is out: put on its result

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

    def _set_session(self, auth, client, acct: Account, creds: dict, since: Optional[int] = None) -> None:
        """The session from now on. since: the session number a restore began at; a sign-in or logout since then
        wins, and so does another account's session already here (a sign-in the core is still taking in): this one
        is let go (BackendError)."""
        with self._lock:
            stale = since is not None and (since != self._session_no or (self._me is not None and self._me.id != acct.id))
            if not stale:
                old = self._client
                if self._me is not None and self._me.id != acct.id:
                    self._chats.clear()  # another account: what the one before was told is not this one's
                    self._names.clear()
                    self._person_avatars.clear()
                    self._peers.clear()
                    self._doc_writer = None
                self._auth, self._client, self._me, self._creds = auth, client, acct, creds
                self._session_no += 1
        if stale:
            self._close_client(client)
            raise BackendError("恢复登录时已经退出或换了账号：这次恢复作废", "unknown")
        if old is not None and old is not client:
            self._close_client(old)

    def _session(self):
        with self._lock:
            return self._client, self._me

    def _still(self, me: Optional[Account]) -> bool:
        """Under _lock: the account a request was made for is still the one signed in (the same account scanned again
        is the same one; a sign-out or another account is not)."""
        return me is not None and self._me is not None and self._me.id == me.id

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
        with self._lock:
            since = self._session_no  # a sign-in or logout while this one asks the service wins over it
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
        self._set_session(auth, client, acct, creds, since=since)
        if creds != {k: c.get(k) for k in creds}:
            self._rotated(creds)  # LarkAgentX filled something in (the app key, the name): seal the new copy
        return acct

    def logout(self) -> None:
        with self._lock:
            old = self._client
            self._auth = self._client = self._me = self._creds = None
            self._session_no += 1
            self._own = None
            self._qr.clear()
            self._names.clear()
            self._person_avatars.clear()
            self._chats.clear()
            self._peers.clear()
            self._doc_writer = None
        with self._groups_mu:
            self._groups, self._groups_join, self._groups_owner, self._groups_at = [], {}, "", 0.0
            self._groups_whole_ids = set()
            self._groups_tried, self._groups_soon, self._groups_delta = 0.0, False, []
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
        if cmd == PUSH_DOC_FEED_CMD and packet.get("payload"):
            inbox.put({"doc_feed": packet["payload"]})  # read off the loop
            return
        if cmd == PUSH_FEED_CARDS_CMD and packet.get("payload"):
            inbox.put({"feed_cards": packet["payload"]})
            return
        if cmd == PUSH_CHATS_CMD and packet.get("payload"):
            inbox.put({"chats": packet["payload"]})
            return
        if cmd == PUSH_REACTIONS_CMD and packet.get("payload"):
            inbox.put({"reactions": packet["payload"]})
            return
        if cmd != PUSH_CMD:
            return
        try:
            pushed = d.decode_push_messages(raw)
        except Exception:
            log.info("a push could not be decoded")
            return
        marks: Dict[str, dict] = {}
        if any(m.get("from_id") for m in pushed) and packet.get("payload"):
            try:
                marks = pushed_threads(packet["payload"])
            except Exception as e:  # the messages are read all the same: where each shows is then not known
                log.info("where pushed messages show could not be read (%s)", type(e).__name__)
        for m in pushed:
            if m.get("from_id"):
                hit = marks.get(_id(m.get("msg_id")))
                if hit is not None:
                    m["_placement"] = hit
                inbox.put(m)

    def _deliver(self, inbox: "queue.Queue", client, me: Account, on_message) -> None:
        """Maps pushed messages and hands them to the core in order, off the connection's loop (the core may look
        names up), so acks and heartbeats never wait for it. Meanwhile keeps the list of every group fresh."""
        while True:
            self._refresh_groups(client, me)  # cheap unless due: due ones read in a thread of their own
            try:
                item = inbox.get(timeout=GROUPS_SPACING)
            except queue.Empty:
                continue
            if item is None:
                return
            if "doc_feed" in item:
                self._doc_feed(item["doc_feed"], me, client)
                continue
            if "feed_cards" in item:
                self._feed_cards(item["feed_cards"])
                continue
            if "chats" in item:
                self._pushed_chats(item["chats"], me)
                continue
            if "reactions" in item:
                self._pushed_reactions(item["reactions"], me)
                continue
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
            self._seen_group(msg.chat_id, me)  # after the core: it reads a chat it does not know (chat_info)

    # ------------------------------------------------------------ every group (自动代答's list)

    def group_candidates(self, limit: int) -> List[Chat]:
        _, me = self._session()
        with self._groups_mu:
            if me is None or self._groups_owner != me.id:
                return []
            return list(self._groups[: max(0, limit)])

    def _refresh_groups(self, client, me: Account) -> None:
        """Read every group again when due (GROUPS_EVERY, never read for this account, or a push named one the list
        lacks), in a thread of its own: one at a time, never more often than GROUPS_SPACING."""
        now = time.monotonic()
        with self._groups_mu:
            mine = self._groups_owner == me.id
            due = not mine or not self._groups_at or now - self._groups_at >= GROUPS_EVERY or self._groups_soon
            if not due or self._groups_busy or (self._groups_tried and now - self._groups_tried < GROUPS_SPACING):
                return
            self._groups_busy = True
            self._groups_soon = False
            self._groups_tried = now
            self._groups_delta = []
        threading.Thread(target=self._groups_work, args=(client, me), name="openmate-larkx-groups", daemon=True).start()

    def _groups_work(self, client, me: Account) -> None:
        try:
            groups, joined, whole, listed, ended = self._read_groups(client)
            _, now_me = self._session()
            if now_me is None or now_me.id != me.id:
                return  # another account signed in meanwhile: these are not its groups
            left: Set[str] = set()
            with self._groups_mu:  # published, then what was heard while it was out (newer), in order, at once
                if self._groups_owner != me.id:
                    self._groups_whole_ids = set()
                    old: List[Chat] = []
                else:
                    old = list(self._groups)
                seen = self._groups_whole_ids | {g.id for g in old}  # every id known as a member before this read
                left = ended & seen  # its details say it is over (dissolved, frozen, only looked into)
                if whole:
                    if seen:
                        # an id seen as a member and no longer listed at all was left or dissolved; one listed without
                        # details is kept as it was
                        left |= seen - listed
                    self._groups_whole_ids = set(listed) - ended
                    self._groups_at = time.monotonic()
                else:
                    self._groups_whole_ids |= listed - ended  # a part read adds what it saw, and drops nothing
                    self._groups_soon = True
                ids = {g.id for g in groups}
                # what was shown stays: one this read listed without details, or every one a part read did not reach
                keep = [g for g in old if g.id not in ids and g.id not in left and (not whole or g.id in listed)]
                groups = groups + keep
                for g in keep:
                    joined.setdefault(g.id, self._groups_join.get(g.id, 0))
                self._groups, self._groups_join, self._groups_owner = groups, joined, me.id
                for owner, infos, gone in self._groups_delta:
                    # only onto this read's list: the core heard of these when they were pushed (this account's only)
                    if owner == me.id:
                        self._apply_chats_locked(infos, gone, me)
                self._groups_delta = []
                self._groups_busy = False
            self._report_gone(left, me)
            log.info("Feishu groups: %d%s", len(groups), "" if whole else " (not all of them: read again soon)")
        except BackendError as e:
            log.info("the groups could not be read: %s", e.kind)
            with self._groups_mu:
                self._groups_soon = True  # again after GROUPS_SPACING
        except Exception as e:
            log.warning("the groups could not be read (%s)", type(e).__name__)
            with self._groups_mu:
                self._groups_soon = True
        finally:
            with self._groups_mu:
                self._groups_busy = False

    @staticmethod
    def _answer(resp, what: str) -> dict:
        """A request's answer, or BackendError for a packet without one (the service said a status, not a reply)."""
        if not isinstance(resp, dict):
            raise BackendError("飞书的回答看不懂（%s时）" % what, "unknown")
        status = resp.get("status")
        if status is not None and _int(status) not in (0, 200):
            raise BackendError("飞书暂时没有回答（%s时）" % what, "unreachable")
        return resp

    def _read_groups(self, client) -> Tuple[List[Chat], Dict[str, int], bool, Set[str], Set[str]]:
        """Every group the account is in: their ids page by page (chats.PullGroupChatsRequest), then their names in
        batches (chats.PullChatsByIdsRequest); dissolved, frozen, deleted ones and ones it only looks in left out.
        Newest joined first. Whether it read them all (else a part, read again soon), and every id listed (with
        details or not). BackendError: nothing kept."""
        ids: List[str] = []
        joined: Dict[str, int] = {}
        cursor, whole = 0, False
        for _ in range(GROUPS_PAGES):
            try:
                resp = self._answer(client.api("chats.PullGroupChatsRequest",
                                               {"type": GROUPS_JOINED, "count": GROUPS_PAGE, "updateTime": cursor}) or {},
                                    "读取群列表")
            except BackendError:
                raise
            except Exception as e:
                raise self._net(e, "读取群列表")
            if _packet_only(resp, ("chatIds", "hasMore", "nextUpdateTime", "chatJoinTimes")):
                raise BackendError("飞书没有给出群列表", "unreachable")  # never read as "in no group at all"
            times = resp.get("chatJoinTimes") or {}
            for cid in (str(x) for x in resp.get("chatIds") or [] if str(x)):
                if cid not in joined:
                    ids.append(cid)
                    joined[cid] = _int(times.get(cid) if isinstance(times, dict) else 0)
            nxt = _int(resp.get("nextUpdateTime"))
            if not resp.get("hasMore"):
                # the end: said outright ("hasMore": false), or a page with ids; an answer with neither is no end
                whole = "hasMore" in resp or bool(ids)
                break
            if not nxt or nxt == cursor:
                break  # more said, but no way on: what was read is a part
            cursor = nxt
        infos: Dict[str, dict] = {}
        for i in range(0, len(ids), GROUPS_INFO_MOST):
            part = ids[i:i + GROUPS_INFO_MOST]
            try:
                resp = self._answer(client.api("chats.PullChatsByIdsRequest", {"chatIds": part}) or {}, "读取群信息")
            except BackendError:
                raise
            except Exception as e:
                raise self._net(e, "读取群信息")
            chats = resp.get("chats") or {}
            for cid in part:
                info = chats.get(cid) if isinstance(chats, dict) else None
                if isinstance(info, dict):
                    infos[cid] = info
        out = []
        ended: Set[str] = set()
        for cid in ids:
            info = infos.get(cid)
            if info is None:
                continue
            if group_over(info):
                ended.add(cid)  # its details say the account is not (or no longer) a member
                continue
            if not group_alive(info):
                continue  # not a group's details (its type left out): kept as it was shown
            joined[cid] = _int(info.get("joinTimeMs")) // 1000 or joined.get(cid) or _int(info.get("createTime"))
            out.append(self._chat_of(cid, info))
        out.sort(key=lambda c: -joined.get(c.id, 0))
        return out, {c.id: joined.get(c.id, 0) for c in out}, whole, set(ids), ended

    def _pushed_chats(self, payload: bytes, me: Account) -> None:
        """Chats that changed (chats.PushChatsRequest): a listed group renamed gets its new name, one left, deleted
        or dissolved goes; a group the list lacks has it read again soon (a push alone does not say the account is
        a member)."""
        try:
            from larkx.proto import lark_all_pb2
            from protobuf_to_dict import protobuf_to_dict

            req = lark_all_pb2.chats.PushChatsRequest()
            req.ParseFromString(payload)
            d = protobuf_to_dict(req)
        except Exception as e:
            log.info("a chat change could not be decoded (%s)", type(e).__name__)
            return
        self._apply_chats(d.get("chats") or {}, [str(x) for x in d.get("inValidChatIds") or []], me)

    def _apply_chats(self, chats: dict, gone: List[str], me: Account) -> None:
        infos = {}
        for key, info in (chats.items() if isinstance(chats, dict) else []):
            if isinstance(info, dict):
                infos[str(info.get("id") or key)] = info
        with self._groups_mu:
            if self._groups_busy:
                self._groups_delta.append((me.id, infos, list(gone)))  # put on the read's result too, when it comes
            left = self._apply_chats_locked(infos, gone, me)
        self._report_gone(left, me)

    def _report_gone(self, chat_ids, me: Account) -> None:
        """Tell the core the account (me) left these chats (or they were dissolved): out of its lists, unmarked for
        自动代答, until a newer message brings one back; the core keeps it only while me is the one signed in. Outside
        any lock of the adapter's."""
        cb = self.on_chat_gone
        if cb is None:
            return
        for cid in sorted(set(chat_ids)):
            try:
                cb(cid, account=me.id)
            except Exception as e:
                log.warning("a chat gone was not taken (%s)", type(e).__name__)

    def _apply_chats_locked(self, infos: Dict[str, dict], gone: List[str], me: Account) -> Set[str]:
        """Put a push's chat changes on the list (the caller holds _groups_mu). The chats the account left or that
        were dissolved, deleted, frozen or left only looked into: told to the core by the caller."""
        if self._groups_owner != me.id:
            return set()
        known = {g.id: g for g in self._groups}
        drop = {str(g) for g in gone}
        update: Dict[str, Chat] = {}
        soon = False
        for cid, info in infos.items():
            if cid in drop:
                continue
            old = known.get(cid)
            ended = (info.get("status") not in (None, 0, "NORMAL") or info.get("isDissolved") or info.get("isFrozen")
                     or info.get("role") in CHAT_NOT_MEMBER)
            if old is not None:
                if ended:
                    drop.add(cid)
                    continue
                name = str(info.get("name") or "") or old.name  # a push carries only what changed
                if not info.get("name"):
                    names = info.get("i18nNames") or {}
                    if isinstance(names, dict) and names:
                        name = str(names.get(self.region.locale.lower()) or next(iter(names.values())) or "") or old.name
                members = _int(info.get("memberCount")) or _int(info.get("userCount")) or old.members
                update[cid] = Chat(id=cid, kind="group", name=name, members=members, topic=old.topic)
            elif not ended and info.get("type") in CHAT_GROUP_TYPES:
                soon = True  # made or joined: the next read lists it (with what the account is in it)
        self._groups = [update.get(g.id, g) for g in self._groups if g.id not in drop]
        self._groups_whole_ids -= drop  # told now: a whole read without it does not tell it again
        for cid in drop:
            self._groups_join.pop(cid, None)  # joined again later: a new time, on top
        if soon:
            self._groups_soon = True
        return drop

    def _seen_group(self, chat_id: str, me: Account) -> None:
        """A message in a group the list lacks (a group just made or joined says so first): read every group again
        soon (the read says whether the account is a member, not one who only looks in)."""
        with self._lock:
            hit = self._chats.get(chat_id)
        chat = hit[1] if hit is not None else None
        if chat is None or chat.kind != "group":
            return
        with self._groups_mu:
            if self._groups_owner == me.id and chat_id not in self._groups_join:
                self._groups_soon = True

    # ------------------------------------------------------------ the account's reactions (an answer without words)

    def my_reactions(self, message_ids: List[str], ignore: Tuple[str, ...] = ()) -> Dict[str, float]:
        client, me = self._signed_in()
        ids = list(dict.fromkeys(_id(m) for m in message_ids if _id(m)))
        out = Reacted()
        failed: Optional[BackendError] = None
        answered = False
        for i in range(0, len(ids), REACTIONS_ASK_MOST):
            part = ids[i:i + REACTIONS_ASK_MOST]
            try:
                resp = self._answer(client.api("reactions.PullReactionsRequest", {"messageIds": part}) or {},
                                    "读取表情回应")
            except BackendError as e:
                failed = failed or e
                out.unasked.update(part)  # not known: the core leaves their marks as they are
                continue
            except Exception as e:
                failed = failed or self._net(e, "读取表情回应")
                out.unasked.update(part)
                continue
            if _packet_only(resp, ("messageReactions", "selfReactionStatus", "reactions")):
                failed = failed or BackendError("飞书没有给出表情回应", "unreachable")
                out.unasked.update(part)  # a status alone: these stay unknown
                continue
            answered = True
            out.update(reacted_by(resp, me.id, set(part), ignore))
        if failed is not None and not answered:
            raise failed
        return out

    def _pushed_reactions(self, payload: bytes, me: Account) -> None:
        """A message's reactions changed (reactions.PushReactions): the ones with the account's go to the core."""
        try:
            from larkx.proto import lark_all_pb2
            from protobuf_to_dict import protobuf_to_dict

            req = lark_all_pb2.reactions.PushReactions()
            req.ParseFromString(payload)
            d = protobuf_to_dict(req)
        except Exception as e:
            log.info("a reaction change could not be decoded (%s)", type(e).__name__)
            return
        ids = set(reacted_by(d, me.id))
        extra = d.get("reactionExtraInfos") or {}
        if isinstance(extra, dict) and any(isinstance(x, dict) and str(x.get("reactionChatterId") or "") == me.id
                                           and not x.get("isCancelled") for x in extra.values()):
            ids.update(str(mr.get("messageId")) for mr in d.get("messageReactions") or []  # the account did it: ask
                       if isinstance(mr, dict) and mr.get("messageId"))
        ids = sorted(ids)
        cb = self.on_reacted
        if ids and cb is not None:
            try:
                cb(ids)
            except Exception as e:
                log.warning("a reaction was not taken (%s)", type(e).__name__)

    # ------------------------------------------------------------ 完成 (a chat's done mark)

    def _feed_cards(self, payload: bytes) -> None:
        """Feed cards that changed: the chats the user moved to 完成 go to the core (on_chats_done)."""
        try:
            done = pushed_done(payload)
        except Exception as e:
            log.info("a feed card change could not be decoded (%s)", type(e).__name__)
            return
        if not done:
            return
        if not self._done_heard:
            self._done_heard = True
            log.info("chats marked 完成 are heard on the live link")
        cb = self.on_chats_done
        if cb is not None:
            try:
                cb(done)
            except Exception as e:
                log.warning("a 完成 mark was not taken (%s)", type(e).__name__)

    def chat_done_states(self, chat_ids: List[str]) -> Dict[str, Optional[float]]:
        client, _ = self._signed_in()
        ids = list(dict.fromkeys(str(c) for c in chat_ids if c))
        out: Dict[str, Optional[float]] = {}
        failed: Optional[BackendError] = None
        for i in range(0, len(ids), DONE_ASK_MOST):
            part = ids[i:i + DONE_ASK_MOST]
            try:
                resp = client.api("feed.PullFeedCardsByIdsRequest",
                                  {"feedCardIdentities": [{"id": c, "type": FEED_CHAT[0]} for c in part]}) or {}
            except Exception as e:  # this batch only: the others' answers still count
                failed = failed or self._net(e, "读取会话的完成状态")
                continue
            cards = resp.get("cards") or {}
            if isinstance(cards, dict):  # a card keyed by its id may leave the id out of itself
                cards = [dict(c, id=c.get("id") or k) for k, c in cards.items() if isinstance(c, dict)]
            asked = set(part)
            out.update(done_cards(cards, None, asked))
            for c in cards:  # explicitly in the inbox: not done. No card, or no feedType: not answered (left out)
                cid = str(c.get("id") or "") if isinstance(c, dict) else ""
                if (cid in asked and cid not in out and c.get("type") in FEED_CHAT and c.get("feedType") in FEED_INBOX
                        and c.get("status") not in FEED_GONE):
                    out[cid] = None
        if failed is not None and not out:
            raise failed
        return out

    # ------------------------------------------------------------ documents

    def _doc_feed(self, payload: bytes, me: Account, client) -> None:
        """A document's notices: an @-mention of the user in a comment goes to the core (on_doc_mention)."""
        try:
            from google.protobuf.json_format import MessageToDict
            from larkx.proto import lark_all_pb2

            req = lark_all_pb2.docs.PushDocFeedRequest()
            req.ParseFromString(payload)
            d = MessageToDict(req)
        except Exception as e:
            log.info("a document notice could not be decoded (%s)", type(e).__name__)
            return
        mentions = doc_mentions(d, me.id)
        if not self._doc_feeds_heard:
            self._doc_feeds_heard = True
            log.info("document notices are heard on the live link")
        if any(not m.comment_id for m in mentions):
            note_shape("document notice (no comment id)", d)
        for m in mentions:
            if not m.from_name and m.from_id:
                m.from_name = self._chatter_name(client, m.from_id)
            cb = self.on_doc_mention
            if cb is not None:
                try:
                    cb(m, account=me.id)  # heard for me: taken only while me is the one signed in
                except Exception as e:
                    log.warning("a document mention was not taken (%s)", type(e).__name__)

    def doc_text(self, doc_url: str, most: int) -> str:
        return self.cli.fetch_doc(doc_url, most) if self.cli is not None else ""

    def doc_writer_name(self, fresh: bool = False) -> str:
        if self.cli is None:
            return ""
        now = time.monotonic()
        with self._lock:
            hit = getattr(self, "_doc_writer", None)
            me = self._me
        if hit is not None and hit[0] > now and not fresh:
            return hit[1]
        name = self.cli.user_name()
        with self._lock:
            if self._still(me):  # asked while the account is the one signed in: kept for it
                self._doc_writer = (now + (600.0 if name else 60.0), name)
        return name

    def reply_doc_comment(self, doc_url: str, comment_id: str, text: str) -> str:
        if self.cli is None:
            raise BackendError("没有 lark-cli，回复不了文档评论", "unsupported")
        from .agenda import CalendarError

        try:
            return self.cli.add_reply(doc_url, comment_id, text)
        except CalendarError as e:
            raise BackendError(e.message, e.kind)

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
        # A quote in the main stream may have no threadId yet (no one replied in its thread), so the anchor falls back
        # to its root; the marks tell (threadMessageType): only a thread reply (2) is in a thread. Feishu leaves the
        # field unset on a quote in the main stream (seen: a quote's push, and the quotes this backend sends), and
        # sets it on every reply in a thread; unset with the marks of a thread reply sent to the chat as well keeps
        # the old reading. Only in a group known as an ordinary one: else (thread-first, not known) the old reading.
        marks = d.get("_placement") if isinstance(d.get("_placement"), dict) else None
        if (in_thread and marks is not None and marks.get("thread_type") != THREAD_REPLY
                and (marks.get("thread_known") or not _thread_reply_synced(marks))
                and (marks.get("plain_group") or self._plain_group(client, chat_id, d.get("chat_type"), me_id))):
            in_thread = False
        topic_root = not root and d.get("scope") == "topic" and anchor == msg_id  # a thread-first group's root
        thread_id = anchor if in_thread else (msg_id if topic_root else "")
        sender = _id(d.get("from_id"))
        placement = self._placement(d, chat_id, msg_id, in_thread, topic_root, bool(me_id) and sender == me_id, client)
        marks = marks or {}
        # a copy in the main stream: of the reply it names, else of one it does not (still a copy, never a command)
        same_as = (marks.get("sync_related") or msg_id) if marks.get("sync_type") == SYNC_TARGET else ""
        if sender and sender != me_id and _int(d.get("chat_type")) == CHAT_P2P:
            self._learn_peer(chat_id, sender, me_id)
        mentions = self._mentions(data)
        # where the leading @ elements end (TEXT, and a POST without a title): the core cuts there, not at spaces
        cut = None
        if data is not None and mt in (TEXT, POST):
            title = str(data.get("title") or "") if mt == POST else ""
            body = rich_after_mentions(data.get("richText") or {}, self.lx.P, cut=not title)
            if title:  # a title first: nothing is cut, the title's words stay as typed
                cut = (title + "\n" + (body or "")).strip()
            else:
                cut = body
        at_all = "all" in mentions
        # the service's own at-me mark counts for someone else's message only: an own message @-mentions the account
        # only by a real @ of it in the text (@自己 asks the Agent, so a reply or quote of an own message never does)
        at_me = bool(me_id) and (me_id in mentions or (bool(d.get("at_me")) and not at_all and sender != me_id))
        return Message(
            id=msg_id, chat_id=chat_id, position=self._position(chat_id, d.get("position"), client),
            sender_id=sender, mine=bool(me_id) and sender == me_id, type=CORE_TYPES.get(name, name),
            text=self._read_text(mt, name, data, str(d.get("content") or "")), at=_seconds(d.get("create_time")),
            root_id=root if in_thread else "", thread_id=thread_id, parent_id=parent, at_me=at_me, at_all=at_all,
            media=self._media_of(mt, data, chat_id, msg_id), placement=placement, same_as=same_as,
            text_after_mentions=cut, edited=marks["edited"] if "edited" in marks else None)  # None: not known

    def _placement(self, d: dict, chat_id: str, msg_id: str, in_thread: bool, topic_root: bool, mine: bool,
                   client) -> str:
        """Where a message shows (Message.placement): a group's reply in a thread is "thread", or "thread_chat" when
        its marks (pushed_threads: live pushes only) say it also went to the main stream; anything else in a group
        is "chat"; a direct chat and a thread-first group's root, "" (replies there keep their own way), and so is a
        message its marks call a thread reply though it was not read as one. How it also went is remembered for a
        reply to copy (send). The account's own say what they carried in the log (ids and the marks, never
        content)."""
        if _int(d.get("chat_type")) == CHAT_P2P or topic_root or self._known_dm(chat_id):
            return ""
        marks = d.get("_placement") if isinstance(d.get("_placement"), dict) else None
        mark = synced_mark(marks)
        if in_thread:
            placement = "thread_chat" if mark else "thread"
        elif (marks or {}).get("thread_type") == THREAD_REPLY or self._topic_group(client, chat_id, mine):
            placement = ""  # not as it was read: replies keep the old way
        else:
            placement = "chat"
        if mark and not marks.get("history"):
            with self._lock:
                self._synced[msg_id] = mark
                while len(self._synced) > SYNCED_KEEP:
                    del self._synced[next(iter(self._synced))]
        quote = bool(_id(d.get("root_id")))
        if (mine and marks is not None and not marks.get("history")
                and (in_thread or mark or marks.get("thread_type") or quote)):
            log.info("own message %s: threadMessageType=%s sendToChat=%s syncToChatMessageType=%d related=%s "
                     "syncRoot=%s; read as %s", msg_id,
                     marks.get("thread_type", 0) if marks.get("thread_known", True) else "unset",
                     marks.get("send_to_chat", False),
                     marks.get("sync_type", 0), marks.get("sync_related") or "-", marks.get("sync_root") or "-",
                     placement or "unknown")
        return placement

    def _known_dm(self, chat_id: str) -> bool:
        with self._lock:
            hit = self._chats.get(chat_id)
            return chat_id in self._peers or (hit is not None and hit[1].kind == "dm")

    def _topic_group(self, client, chat_id: str, ask: bool) -> bool:
        """Whether the chat is a thread-first group: from the chats read (no request), or, with ask, LarkAgentX's
        own check (it keeps the answer); False when it cannot tell."""
        with self._lock:
            hit = self._chats.get(chat_id)
        if hit is not None:
            return bool(hit[1].topic)
        if not ask or client is None:
            return False
        try:
            return bool(client.is_thread_chat(chat_id))
        except Exception:
            return False

    def topic_mode(self, chat_id: str) -> bool:
        """Whether the group shows its messages as topics though its record does not say so (_topic_mode), as
        learnt within TOPIC_MODE_TTL."""
        with self._lock:
            at = self._topic_mode.get(str(chat_id))
            if at is not None and time.time() - at > TOPIC_MODE_TTL:
                del self._topic_mode[str(chat_id)]
                at = None
            return at is not None

    def _learn_topic_mode(self, chat_id: str, why: str) -> None:
        with self._lock:
            known = chat_id in self._topic_mode
            self._topic_mode.pop(chat_id, None)
            self._topic_mode[chat_id] = time.time()
            while len(self._topic_mode) > TOPIC_MODE_KEEP:
                del self._topic_mode[next(iter(self._topic_mode))]
        if not known:
            log.info("group %s shows its messages as topics (%s)", chat_id, why)

    def _plain_group(self, client, chat_id: str, chat_type, me_id: str) -> bool:
        """Whether the chat is known as an ordinary group (not thread-first), for the account me_id while it is the
        one signed in: the chats read (while still fresh), else its own record read now (and kept), which must say a
        group (entities.Chat.Type GROUP) in no thread mode. Not known (a failure, no type, the push's chat type says
        thread-first, another account or none signed in): False."""
        if chat_type in (3, "TOPIC_GROUP") or client is None or not me_id:
            return False
        with self._lock:
            me = self._me
            if me is None or me.id != me_id:
                return False
            hit = self._chats.get(chat_id)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1].kind == "group" and not hit[1].topic
        try:
            info, chat = self._chat_read(client, chat_id, me)
        except Exception:
            return False
        with self._lock:
            if not self._still(me):  # signed out or another account while it was read (the same one again is it)
                return False
        return "type" in info and chat.kind == "group" and not chat.topic

    def _learn_peer(self, chat_id: str, user_id: str, me_id: str) -> None:
        """The other person of a direct chat, from their message in it (the chat's own record may have no name), for
        the account that read it while it is the one signed in."""
        with self._lock:
            if self._me is None or self._me.id != me_id or self._peers.get(chat_id) == user_id:
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
        rich = data.get("richText") or {}
        text = rich_text(rich, self.lx.P)
        if text:
            return text
        inner = str(rich.get("innerText") or "")
        return inner if inner.strip() else str(data.get("text") or "")

    def _read_text(self, mt: int, name: str, data: Optional[dict], summary: str) -> str:
        text = self._text(mt, name, data, summary)
        if mt in (CARD, SHARE_CALENDAR_EVENT) and data is not None and (not text or text == summary):
            note_shape(name, data)
        elif mt in (TEXT, POST) and data is not None and not text.strip():
            note_shape(name, data, rich_shape(data.get("richText") or {}))
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
            info = self._chat_raw(client, chat_id, me)
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
                if not got:
                    # LarkAgentX reads an answer that is only a status as no messages: ask once more and look at the
                    # answer itself (an empty page is rare: every message there recalled)
                    raw = self._answer(client.api("messages.PullMessagesByPositionsRequest",
                                                  {"chatId": str(chat_id), "positions": list(range(lo, hi))}),
                                       "读取历史消息")
                    if raw.get("messages"):  # it has them now: the first answer was a failure
                        raise BackendError("飞书这次没给出历史消息（读取历史消息时）", "unreachable")
            except BackendError:
                raise
            except Exception as e:
                raise self._net(e, "读取历史消息")
            got = got or []
            self._history_marks(client, chat_id, got, me.id)
            for d in got:
                m = self._message(d, me.id, client)
                if m is not None and (before_position is None or m.position < before_position):
                    out[m.id] = m
            hi = lo
        msgs = sorted(out.values(), key=lambda m: (m.position, m.at))[-limit:]
        self._fill_blank(client, chat_id, msgs)
        return msgs

    def _history_marks(self, client, chat_id: str, got: list, me_id: str) -> None:
        """History carries no thread marks (pushed_threads), and a quote in the main stream no one replied to in its
        thread reads as a reply in that thread (its anchor is its root): for those a page has, the entities are asked
        once and their marks kept (_message reads them as a push's, an unset threadMessageType too). One whose entity
        is not in the answer, a group not known as an ordinary one (asked once a page), or a failed read: the old
        reading."""
        maybe = {}
        for d in got:
            if (isinstance(d, dict) and "_placement" not in d and _id(d.get("root_id"))
                    and _id(d.get("anchor")) == _id(d.get("root_id")) and _int(d.get("position")) > 0):
                maybe[_id(d.get("msg_id"))] = d
        if not maybe or not self._plain_group(client, chat_id, next(iter(maybe.values())).get("chat_type"), me_id):
            return
        try:
            raw = self._answer(client.api("messages.PullMessagesByPositionsRequest", {
                "chatId": str(chat_id), "positions": sorted({_int(d.get("position")) for d in maybe.values()})}),
                "读取历史消息")
        except Exception as e:
            log.info("the thread marks of a history page could not be read (%s)", type(e).__name__)
            return
        for v in (raw.get("messages") or {}).values():
            d = maybe.get(_id(v.get("id"))) if isinstance(v, dict) else None
            if d is not None:  # its own entity: a field it does not carry is unset
                d["_placement"] = dict(entity_marks(v), plain_group=True)

    def _fill_blank(self, client, chat_id: str, msgs: List[Message]) -> None:
        """A text or post read as nothing: the message's own entity is asked for once more and its preview hang points
        (urlPreviewHangPointMap: a link shown as a card) give the text; its flags are noted (never its content)."""
        blank = [m for m in msgs if m.type in ("TEXT", "POST") and not (m.text or "").strip()]
        if not blank:
            return
        try:
            raw = self._answer(client.api("messages.PullMessagesByPositionsRequest",
                                          {"chatId": str(chat_id), "positions": sorted({m.position for m in blank})}),
                               "读取历史消息")
        except Exception as e:  # the text stays empty; the next re-read tries again
            log.info("the entity of a message read as nothing could not be read (%s)", type(e).__name__)
            return
        entities = {}
        for v in (raw.get("messages") or {}).values():
            if isinstance(v, dict) and v.get("id"):
                entities[str(v["id"])] = v
        for m in blank:
            ent = entities.get(m.id)
            if ent is None:
                continue
            dlp = dlp_state(ent)  # first: a text Feishu holds is never shown through its preview links either
            if dlp in DLP_BLOCK:
                m.text = DLP_BLOCKED_TEXT  # Feishu's data-loss policy keeps it from this client: said, never worked around
                m.blocked = True  # shown in the chat, never handed to the knowledge base
                note_flags(m.type, entity_flags(ent))
                continue
            if dlp in DLP_IN_PROGRESS:
                m.text = ""  # held for review: left empty (never its preview link), read again by the core's catch-ups
                note_flags(m.type, entity_flags(ent))
                continue
            links = hang_links(ent)
            if links:
                m.text = " ".join(links)
            else:
                note_flags(m.type, entity_flags(ent))

    def _chat_raw(self, client, chat_id: str, me: Optional[Account] = None) -> dict:
        return self._chat_read(client, chat_id, me)[0]

    def _chat_read(self, client, chat_id: str, me: Optional[Account] = None) -> Tuple[dict, Chat]:
        """The service's answer about a chat and the chat it makes, kept for the session that asked (while it is the
        one)."""
        try:
            info = client.get_chat_info(chat_id)
        except Exception as e:
            raise self._net(e, "读取会话信息")
        if not info:
            raise BackendError("没有这个%s会话" % self.region.label, "not_found")
        if isinstance(info, dict) and set(info) <= {"status", "message", "code"}:
            # only a status: the service did not answer (a chat's own status comes with the chat)
            raise BackendError("飞书暂时没有回答（读取会话信息时）", "unreachable")
        chat = self._chat_of(chat_id, info)
        if chat.kind == "dm" and chat.avatar is None and chat.peer_id:
            # a chat's own read: the other person's picture may be asked for (a list's row takes a known one only)
            key = self._person_avatar(chat.peer_id)
            chat.avatar = None if key is None else avatar_address(self.region.file_host, key)
        with self._lock:
            if self._still(me) or (me is None and self._client is client):  # still the account it was asked for
                self._chats[chat_id] = (time.monotonic() + CHAT_TTL, chat)
                last = _int(info.get("lastMessagePosition"))
                if last > self._last_pos.get(chat_id, 0):
                    self._last_pos[chat_id] = last
        return info, chat

    def _chat_of(self, chat_id: str, info: dict) -> Chat:
        """Its picture: the record's own; a direct chat without one, the other person's when it is known already
        (_chat_read asks for it), else not known (avatar None)."""
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
        # the picture: the group's own; a direct chat's is the other person's (the chat's, or else theirs). A record
        # that does not carry the field, or a direct chat whose other person is not known, says nothing (None)
        key: Optional[str] = image_key(info.get("avatar")) if isinstance(info.get("avatar"), dict) else None
        if not key and kind == "dm":
            key = self._person_avatar(peer, ask=False) if peer else None
        avatar = None if key is None else avatar_address(self.region.file_host, key)
        return Chat(id=chat_id, kind=kind, name=name, peer_id=peer if kind == "dm" else "", topic=topic and kind == "group",
                    members=members if kind == "group" else 0, avatar=avatar)

    def _person_avatar(self, user_id: str, ask: bool = True, of: Optional[Account] = None) -> Optional[str]:
        """The key of a person's picture ("" when they have none), None when the service could not be asked (or,
        without ask, when it is not known already; or, with of, when that account is not the one signed in: the
        client asked is the one of that account)."""
        user_id = _id(user_id)
        client, me = self._session()
        if client is None or not user_id:
            return None
        if of is not None and (me is None or me.id != of.id):
            return None
        now = time.monotonic()
        with self._lock:
            hit = self._person_avatars.get(user_id)
        if hit is not None and hit[0] > now:
            return hit[1]
        if not ask:
            return None
        try:
            resp = client.api("chatters.PullChattersByIdsRequest", {"chatterIds": [user_id]}) or {}
        except Exception:
            return None
        chatters = resp.get("chatters") if isinstance(resp, dict) else None
        if not isinstance(chatters, dict):
            return None
        c = chatters.get(user_id)
        if c is None and len(chatters) == 1:  # keyed otherwise: the one answered, unless it says it is someone else
            only = next(iter(chatters.values()))
            if isinstance(only, dict) and _id(only.get("id") or user_id) == user_id:
                c = only
        if not isinstance(c, dict):
            return None
        key = image_key(c.get("avatar")) or image_key(c.get("avatarImage"))
        with self._lock:
            if self._still(me):
                self._person_avatars[user_id] = (now + NAME_TTL, key)
        return key

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
        client, me = self._signed_in()
        if limit <= 0:
            return []
        chats: List[Chat] = []
        failed: Optional[Exception] = None
        answered = False
        for how in (self._recent_from_chats, self._recent_from_feed):
            try:
                chats = how(client, limit)
            except Exception as e:
                failed = failed or e
                continue
            answered = True
            if chats:
                break
        if not answered and failed is not None:  # none could say: an error, not "no chats"
            if isinstance(failed, BackendError):
                raise failed
            raise self._net(failed, "读取最近的会话")
        now = time.monotonic()
        with self._lock:
            if self._still(me):
                for c in chats:
                    self._chats[c.id] = (now + CHAT_TTL, c)
        return chats[:limit]

    def _recent_from_chats(self, client, limit: int) -> List[Chat]:
        resp = self._answer(client.api("chats.PullRecentChatsRequest", {"count": int(limit)}) or {}, "读取最近的会话")
        if _packet_only(resp, ("chats", "orderedChatIds")):
            raise BackendError("飞书没有给出最近的会话", "unreachable")  # a status alone, even 200
        raw = resp.get("chats") or {}
        order = [str(x) for x in resp.get("orderedChatIds") or [] if str(x) in raw]
        if not order:
            order = sorted(raw, key=lambda k: -_int((raw[k] or {}).get("updateTime")))
        return [self._chat_of(cid, raw[cid]) for cid in order if raw.get(cid)]

    def _recent_from_feed(self, client, limit: int) -> List[Chat]:
        resp = self._answer(client.api("feed.PullFeedCardsRequest",
                                       {"type": 1, "pullType": 1, "count": int(limit), "pullFeedType": 1}) or {},
                            "读取最近的会话")
        if _packet_only(resp, ("chats", "cards")):
            raise BackendError("飞书没有给出最近的会话", "unreachable")
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
            chat = hit[1]
            if chat.kind == "dm" and chat.avatar is None and chat.peer_id:  # kept from a list's row: asked for now
                key = self._person_avatar(chat.peer_id)
                if key is not None:
                    chat.avatar = avatar_address(self.region.file_host, key)
            return chat
        client, me = self._signed_in()
        return self._chat_read(client, chat_id, me)[1]

    def chat_picture(self, chat_id: str, peer_id: str = "") -> Chat:
        """The chat as the service says it now (not a row kept from a list, which may not say its picture): what the
        core asks when it wants a chat's picture. peer_id is the other person of a direct chat as the core's store
        knows them: after a restart this backend knows them only once they write again, and a direct chat's record
        has no picture of its own, so without it most direct chats would have none."""
        client, me = self._signed_in()
        chat = self._chat_read(client, chat_id, me)[1]
        peer_id = _id(peer_id)
        if (chat.kind == "dm" and chat.avatar is None and not chat.peer_id and peer_id
                and not (me is not None and me.id == peer_id)):  # (told by a store of another account: not its own)
            key = self._person_avatar(peer_id, of=me)  # (another account signed in meanwhile: not asked in its name)
            if key is not None:  # on a copy: the chat kept for the session stays as the service said it
                chat = dataclasses.replace(chat, avatar=avatar_address(self.region.file_host, key))
        return chat

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
            if self._still(me):
                self._names[key] = (now + (NAME_TTL if name else NAME_MISS_TTL), name)
        return name

    def react(self, chat_id: str, message_id: str, reaction: str) -> None:
        client, _ = self._signed_in()
        mid = _id(message_id)
        if not mid or not reaction:
            raise BackendError("没有要回应的消息", "not_found")
        try:
            client.api("reactions.PutReactionRequest", {"messageId": mid, "reactionType": reaction})
        except Exception as e:  # the service's words stay out of the user's sight; the kind is enough
            raise BackendError("飞书没有接受这个表情回应（%s）" % e.__class__.__name__, "unavailable")

    def unreact(self, chat_id: str, message_id: str, reaction: str) -> None:
        client, _ = self._signed_in()
        mid = _id(message_id)
        if not mid or not reaction:
            raise BackendError("没有要撤回的表情回应", "not_found")
        try:
            client.api("reactions.DeleteReactionRequest", {"messageId": mid, "reactionType": reaction})
        except Exception as e:
            raise BackendError("飞书没有撤回这个表情回应（%s）" % e.__class__.__name__, "unavailable")

    def edit(self, chat_id: str, message_id: str, text: str) -> None:
        """Replace a text message of the account's (one send() made: plain text in one text element)."""
        if not (text or "").strip():
            raise BackendError("消息内容是空的", "rejected")
        mid = _id(message_id)
        if not mid.isdigit():
            raise BackendError("要编辑的%s消息 id 不对" % self.region.label, "not_found")
        client, _ = self._signed_in()
        b = self.lx.builders
        try:
            element = b.generate_request_cid()
            prop = b.P.TextProperty()
            prop.content = str(text)
            data = {"msgId": mid, "cid": b.generate_request_cid(), "type": TEXT,
                    "content": {"richText": {"elementIds": [element], "innerText": str(text),
                                             "elements": {"dictionary": {element: {
                                                 "tag": 1, "property": base64.b64encode(prop.SerializeToString()).decode()}}}}}}
        except Exception as e:
            raise BackendError("没能组成这次编辑（%s）" % type(e).__name__, "rejected")
        try:
            resp = client.api("messages.EditMessageRequest", data)
        except Exception as e:
            raise BackendError("%s没有接受这次编辑（%s）" % (self.region.label, e.__class__.__name__), "unavailable")
        status = resp.get("status") if isinstance(resp, dict) else None
        if status is not None and _int(status) not in (0, 200):  # only a status came back: it was refused
            raise BackendError("%s没有接受这次编辑（状态 %d）" % (self.region.label, _int(status)), "unavailable")

    can_recall = True

    def recall(self, chat_id: str, message_id: str) -> None:
        """Take back (撤回) a message of the account's, for everyone in the chat: LarkAgentX's public api with the
        client's own delete-message request (its one field: the message's id)."""
        mid = _id(message_id)
        if not mid.isdigit():
            raise BackendError("要撤回的%s消息 id 不对" % self.region.label, "not_found")
        client, _ = self._signed_in()
        try:
            resp = client.api("messages.DeleteMessageRequest", {"id": mid})
        except Exception as e:
            raise BackendError("%s没有撤回这条消息（%s）" % (self.region.label, e.__class__.__name__), "unavailable")
        status = resp.get("status") if isinstance(resp, dict) else None
        if status is not None and _int(status) not in (0, 200):  # only a status came back: it was refused
            raise BackendError("%s没有撤回这条消息（状态 %d）" % (self.region.label, _int(status)), "unavailable")

    def people_kinds(self, user_ids: List[str]) -> Dict[str, str]:
        ids = [i for i in (_id(u) for u in user_ids) if i]
        if not ids:
            return {}
        client, _ = self._signed_in()
        resp = client.api("chatters.PullChattersByIdsRequest", {"chatterIds": ids}) or {}
        out: Dict[str, str] = {}
        for key, c in (resp.get("chatters") or {}).items():
            if not isinstance(c, dict):
                continue
            cid = _id(c.get("id")) or _id(key)
            kind = chatter_kind(c.get("type"))
            if cid in ids and kind:
                out[cid] = kind
        return out

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
            if status == 401:  # the session, not this file: signing in again brings it back
                raise BackendError("%s登录已失效，下载不了附件" % self.region.label, "expired")
            if status == 403:
                raise BackendError("没有权限下载这个附件", "rejected")
            if status in (408, 429) or status >= 500 or status == 0:
                raise BackendError("下载附件时%s出错了（HTTP %d）" % (self.region.label, status), "unreachable")
            # any other answer would be the same next time: not a moment's trouble, and not this file's alone
            raise BackendError("下载附件时%s回答 HTTP %d" % (self.region.label, status), "unknown")
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
                if _answer_not_file(mime, data):  # a page or an error instead of the picture: the session, not the file
                    raise BackendError("下载图片时%s回答的不是图片" % self.region.label, "unknown")
                raise BackendError("这张图片下载下来不是能识别的格式（可能是加密的图片），暂时不能预览", "unsupported")
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

    #: send takes mentions: real @s, Feishu tells those people
    can_mention = True
    #: chats may be thread-first (a topic group, a group in topic mode): Chat.topic says which
    topic_groups = True

    #: send_image: a picture, uploaded to the file host and sent where a text would go (reply_to, root)
    can_send_images = True
    image_threads = True

    def send_image(self, chat_id: str, path: str, reply_to: Optional[str] = None, root: Optional[str] = None,
                   to_chat: bool = False) -> str:
        """Backend.send_image: the picture at path (a PNG or JPEG the core checked) uploaded as the web client does
        (the file host's /api/image, with the session's cookies), then sent as an IMAGE message in the place send()
        gives a text with the same reply_to and root. SendRejected while nothing was sent: an upload that failed sends
        nothing."""
        client, _ = self._session()  # the session asked for it: it uploads and posts, or nothing goes
        if client is None:
            raise SendRejected("还没有登录%s" % self.region.label)
        try:
            with open(path, "rb") as f:
                data = f.read(IMAGE_MOST + 1)
        except OSError as e:
            raise SendRejected("读不到要发的图片（%s）" % (e.strerror or type(e).__name__))
        if not data or len(data) > IMAGE_MOST:
            raise SendRejected("图片是空的或超过 %d MB" % (IMAGE_MOST >> 20))
        mime = sniff_image(data)
        if mime not in ("image/png", "image/jpeg"):
            raise SendRejected("只能发 PNG 或 JPEG 图片")
        token = self._upload_image(client, data, mime)
        # posted by the session that uploaded it, or not at all (another account's sign-in meanwhile)
        return self.send(chat_id, "[图片]", reply_to, root, to_chat=to_chat, _image=token, _client=client)

    def _upload_image(self, client, data: bytes, mime: str) -> str:
        """The cryptoToken of the picture uploaded to the file host (the web client's way: one multipart field
        "image", the session's cookies); SendRejected when it was not taken (nothing is sent without it)."""
        auth = client.auth
        region = auth.region
        R = self.lx.requests
        headers = {"origin": region.web_origin, "referer": region.web_origin + "/", "x-source": "web",
                   "x-appid": str(getattr(auth, "gateway_app_id", "") or ""), "locale": region.locale,
                   "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                                 "Chrome/150.0.0.0 Safari/537.36"}
        try:
            r = R.post(region.file_host + "/api/image", files={"image": ("image", data, mime)}, headers=headers,
                       cookies=auth.cookies, timeout=60)
        except R.exceptions.RequestException as e:
            raise SendRejected("上传图片时连不上%s（%s）" % (self.region.label, type(e).__name__))
        status = _int(getattr(r, "status_code", 0))
        if status == 401:
            raise SendRejected("%s登录已失效，图片没有发出" % self.region.label)
        if status != 200:
            raise SendRejected("%s没有收下这张图片（HTTP %d）" % (self.region.label, status))
        try:
            token = image_token(r.content)
        except Exception:
            token = ""
        if not token:
            raise SendRejected("%s收下了图片，但回答里没有图片的凭据，图片没有发出" % self.region.label)
        return token

    def send(self, chat_id: str, text: str, reply_to: Optional[str] = None, root: Optional[str] = None,
             to_chat: bool = False, mentions: Optional[List[Tuple[str, str]]] = None, _image: str = "",
             _client=None) -> str:
        """Backend.send. mentions [(user id, name, offset)]: real @s (with_mentions). A group in topic mode
        (topic_mode) takes a reply only into its topic, the thread way, and a new message (a topic of its own) only
        from a newer client: a new message it refused so goes once more that way (newer_post), and only that one.
        _image: an uploaded picture's cryptoToken (send_image): the message is that picture, in the same place, sent
        only by _client, the session that uploaded it."""
        if not (text or "").strip():
            raise SendRejected("消息内容是空的")
        chat_id = str(chat_id or "").strip()
        if not chat_id.isdigit():
            raise SendRejected("%s会话 id 不对" % self.region.label)
        reply_to, root = _id(reply_to), _id(root)
        for v in (reply_to, root):
            if v and not v.isdigit():
                raise SendRejected("要回复的%s消息 id 不对" % self.region.label)
        text, people = self._mention_list(text, mentions)
        client, _ = self._session()
        if client is None:
            raise SendRejected("还没有登录%s" % self.region.label)
        if _client is not None and client is not _client:
            raise SendRejected("刚换了%s账号，这张图片没有发出" % self.region.label)
        b = self.lx.builders

        def build(root_id: str = "", thread_chat: bool = False):
            rid = self.lx.ids.generate_long_request_id()
            if root_id:
                pkt = b.build_send_message_packet(text, chat_id, rid, root_id=root_id, thread_chat=thread_chat)
            else:
                pkt = b.build_send_message_packet(text, chat_id, rid)
            if _image:
                return as_image(pkt, _image)
            return with_mentions(pkt, text, people) if people else pkt

        new_in_group = False  # a new message to a group (one in topic mode takes it from a newer client only)
        into_topic = ""  # a quote in a group: the message whose topic it goes into when the group shows topics
        try:
            if root and not self._is_dm(client, chat_id):
                # into the thread on root (LarkAgentX opens it when there is none yet); a thread-first group takes
                # the reply into the topic without the reply-in-thread mark
                topic = bool(client.is_thread_chat(chat_id))
                pkt = build(root, topic)
                if to_chat and not topic:
                    # …and into the main stream too, quoting the request, marked the way the request itself was
                    with self._lock:
                        mark = self._synced.get(reply_to or "", "")
                    pkt = also_to_chat(pkt, reply_to or root, mark or "sync")
            elif root or reply_to:
                group = not self._known_dm(chat_id)  # (known, no request: a quote never waited for a chat's read)
                if group and self.topic_mode(chat_id):
                    pkt = build(reply_to or root, False)  # a group in topic mode: into the topic, the thread way
                else:
                    # a reply that quotes and stays in the main stream (a direct chat has no threads here):
                    # thread_chat=True is the builder's way to leave the reply-in-thread mark out
                    pkt = build(reply_to or root, True)
                    into_topic = (reply_to or root) if group else ""
            else:
                pkt = build()
                new_in_group = not self._known_dm(chat_id)  # (known, no request; only a group refuses so anyway)
        except Exception as e:
            raise SendRejected("没能组成这条%s消息（%s）" % (self.region.label, type(e).__name__))
        if _client is not None:
            with self._lock:
                same = self._client is client  # right before it goes: the uploading session is still the one
            if not same:
                raise SendRejected("刚换了%s账号，这张图片没有发出" % self.region.label)
        try:
            content = client._gateway_post(pkt)
        except Exception as e:
            err = self._send_error(e)
            if not (isinstance(err, SendRejected) and (new_in_group or into_topic) and self._topic_refusal(e)):
                raise err
            if self._is_dm(client, chat_id):  # (asked only now: a group refuses so, a direct chat never should)
                raise err
            with self._lock:
                same = self._client is client  # signed out or another account meanwhile: nothing learnt or sent
            if not same:
                raise err
            # refused (surely not sent) as a group in topic mode refuses a message outside a topic: known from now on
            self._learn_topic_mode(chat_id, "it refused a message outside a topic")
            if new_in_group:
                return self._new_topic_again(client, chat_id, build)  # (only this retry goes as a newer client)
            try:
                pkt = build(into_topic, False)  # the quote, once more: into that message's topic, the thread way
            except Exception:
                raise err
            with self._lock:
                same = self._client is client  # (again, right before it goes: the packet took a moment)
            if not same:
                raise err
            log.info("group %s: the quote goes into the topic instead", chat_id)
            try:
                content = client._gateway_post(pkt)
            except Exception as e2:
                raise self._send_error(e2)
        try:
            msg_id = _id(b.decode_put_message_response(content))
        except Exception:
            raise BackendError("%s收下了这条消息，但回答看不懂，不确定是否发出" % self.region.label, "unknown")
        if not msg_id:
            raise BackendError("%s的回答里没有消息 id，不确定是否发出" % self.region.label, "unknown")
        return msg_id

    def _new_topic_again(self, client, chat_id: str, build) -> str:
        """A new message a group in topic mode refused (surely not sent), once more as a newer client (a new request
        id): it opens a topic of its own. Its new message's id; SendRejected when that is refused too."""
        try:
            pkt = build()
        except Exception as e:
            raise SendRejected("没能组成这条%s消息（%s）" % (self.region.label, type(e).__name__))
        with self._lock:
            same = self._client is client  # right before it goes: signed out or another account meanwhile, not sent
        if not same:
            raise SendRejected("刚换了%s账号，这条没有发出" % self.region.label)
        log.info("group %s: the new message goes once more as a newer client, a topic of its own", chat_id)
        try:
            content = newer_post(client, pkt)
        except Exception as e:
            err = self._send_error(e)
            if isinstance(err, SendRejected) and self._topic_refusal(e):
                raise SendRejected(TOPIC_TOO_OLD, 400)
            raise err
        try:
            msg_id = _id(self.lx.builders.decode_put_message_response(content))
        except Exception:
            raise BackendError("%s收下了这条消息，但回答看不懂，不确定是否发出" % self.region.label, "unknown")
        if not msg_id:
            raise BackendError("%s的回答里没有消息 id，不确定是否发出" % self.region.label, "unknown")
        return msg_id

    def _mention_list(self, text: str, mentions) -> Tuple[str, List[Tuple[str, str, int]]]:
        """send's text and mentions checked: [(user id, name, offset)] where the text writes "@name" at offset (the
        core's, mention_spans), or [(user id, name)] placed here the same way; each user id canonical, at most
        MENTIONS_MOST people (SendRejected else)."""
        spans: List[Tuple[str, str, int]] = []
        pairs: List[Tuple[str, str]] = []
        for m in mentions or ():
            if not isinstance(m, (tuple, list)) or len(m) not in (2, 3):
                raise SendRejected("要 @ 的人不对")
            uid, name = _id(m[0]), str(m[1] or "").strip()
            if not uid.isdigit() or str(int(uid)) != uid:
                raise SendRejected("要 @ 的%s用户 id 不对" % self.region.label)
            if len(m) == 3:
                at = m[2]
                if not isinstance(at, int) or at < 0 or not name or not text.startswith("@" + name, at):
                    raise SendRejected("要 @ 的人在正文里的位置不对")
                spans.append((uid, name, at))
            elif uid not in {u for u, _ in pairs}:
                pairs.append((uid, name))
        if pairs:
            if spans:
                raise SendRejected("要 @ 的人不对")
            text, spans = mention_spans(text, pairs)
        if len({uid for uid, _, _ in spans}) > MENTIONS_MOST:
            raise SendRejected("一条消息最多 @ %d 个人" % MENTIONS_MOST)
        return text, spans

    def _topic_refusal(self, e: Exception) -> bool:
        """Whether a refused post is Feishu's refusal of a message outside a topic in a group in topic mode (HTTP 400
        whose answer carries TOPIC_REFUSAL as its code)."""
        if self._http_status(e) != 400:
            return False
        body = getattr(getattr(e, "response", None), "content", b"") or b""
        codes = wire_varints(body)
        return codes is not None and codes.get(2, [None])[-1] == TOPIC_REFUSAL

    def _http_status(self, e: Exception) -> int:
        """The HTTP status of a refused post (0 when it is no HTTP answer)."""
        if not isinstance(e, self.lx.requests.exceptions.HTTPError):
            return 0
        return _int(getattr(getattr(e, "response", None), "status_code", 0))

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
                return SendRejected("%s拒绝了这条消息（HTTP %d）" % (label, status), status)
            return BackendError("%s出错了（HTTP %d），不确定消息是否发出" % (label, status), "unknown")
        return BackendError("发送时和%s的连接出错，不确定消息是否发出" % label, "unreachable")

    def create_chat(self, user_id: str) -> str:
        user_id = _id(user_id)
        if not user_id.isdigit():
            raise BackendError("%s用户 id 不对" % self.region.label, "not_found")
        client, me = self._signed_in()
        try:
            chat_id = _id(client.create_chat(user_id))
        except Exception as e:
            raise self._net(e, "打开私聊")
        if not chat_id:
            raise BackendError("%s没有给出和这个人的私聊" % self.region.label, "not_found")
        with self._lock:
            if self._still(me):  # the account that opened it is still the one: kept for it
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
        if isinstance(e, (R.ConnectionError, R.Timeout, getattr(R, "ChunkedEncodingError", R.Timeout))):
            return BackendError("连不上%s（%s时）" % (label, what), "unreachable")
        if isinstance(e, R.HTTPError):
            status = _int(getattr(getattr(e, "response", None), "status_code", 0))
            return BackendError("%s回答 HTTP %d（%s时）" % (label, status, what), "not_found" if status == 404 else "unreachable")
        if isinstance(e, ValueError):
            return BackendError("%s的回答看不懂（%s时）" % (label, what), "unknown")
        log.info("%s failed: %s", what, type(e).__name__)
        return BackendError("%s时出错了（%s）" % (what, type(e).__name__), "unknown")


def _answer_not_file(mime: str, data: bytes) -> bool:
    """A download that came back as an answer (a page, an error: HTML, XML, JSON, text) rather than the file asked
    for. An SVG is a picture (one this adapter does not hand over), not an answer. When in doubt it is an answer: that
    stops a history import for a person to look, where taking it for a bad picture would skip the file for good."""
    if mime.startswith("text/") or mime in ("application/json", "application/xhtml+xml", "application/xml") \
            or mime.endswith(("+json", "+xml")) and mime != "image/svg+xml":
        return True
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n\x0b\x0c").lower()
    if head.startswith(b"<?xml"):
        head = head[head.find(b"?>") + 2:].lstrip() if b"?>" in head else b""
    if head.startswith(b"<svg"):
        return False
    return head.startswith((b"<", b"{"))


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


# ---------------------------------------------------------------- rich text

TEXT_TAGS = (1, 3, 7, 8, 9, 17, 23, 24, 25, 28, 29, 30, 31)  # text, p, b, i, u, link, h1-h3, li, quote, code: TextProperty
AT_TAG_RICH = 5
ANCHOR_TAG = 6  # A: a link, its address and words in the property
PLACEHOLDERS = {2: "[图片]", 10: "[表情]", 18: "[视频]"}
URL_RE = re.compile(r"^https?://\S+$")
WORDLESS_RE = re.compile(r"^[A-Za-z0-9_.:/#-]+$")  # a key, a target, an id: not the words a link shows


def _varint(b: bytes, i: int) -> Tuple[int, int]:
    out, shift = 0, 0
    while True:
        if i >= len(b) or shift > 63:
            raise ValueError("a varint runs past the end")
        c = b[i]
        i += 1
        out |= (c & 0x7F) << shift
        if not c & 0x80:
            return out, i
        shift += 7


def wire_strings(b: bytes) -> List[str]:
    """The UTF-8 strings among a protobuf message's top-level fields, in order ([] when it does not read)."""
    return [s for _, s in wire_fields(b)]


def wire_fields(b: bytes) -> List[Tuple[int, str]]:
    """(field number, UTF-8 string) of a protobuf message's top-level length-delimited fields, in order."""
    out: List[Tuple[int, str]] = []
    i, n = 0, len(b or b"")
    try:
        while i < n:
            key, i = _varint(b, i)
            wt, field = key & 7, key >> 3
            if wt == 0:
                _, i = _varint(b, i)
            elif wt == 2:
                size, i = _varint(b, i)
                if i + size > n:
                    raise ValueError("a field runs past the end")
                raw = b[i:i + size]
                i += size
                try:
                    out.append((field, raw.decode("utf-8")))
                except UnicodeDecodeError:
                    pass
            elif wt == 1:
                i += 8
            elif wt == 5:
                i += 4
            else:
                raise ValueError("an unknown wire type")
    except ValueError:
        return []
    return out


ANCHOR_HREF, ANCHOR_CONTENT, ANCHOR_TEXT, ANCHOR_PC_HREF = 1, 2, 3, 8  # RichTextElement.AnchorProperty


def link_text(prop: bytes) -> str:
    """An A element (AnchorProperty: href 1, content 2, textContent 3, pcHref 8): "words address", or the address
    alone when the words are it or there are none."""
    f: Dict[int, str] = {}
    for n, s in wire_fields(prop):
        if s.strip() and n not in f:
            f[n] = s.strip()
    href = next((f[n] for n in (ANCHOR_HREF, ANCHOR_PC_HREF) if URL_RE.match(f.get(n, ""))), "")
    words = f.get(ANCHOR_TEXT) or f.get(ANCHOR_CONTENT) or ""
    if href and words and words != href:
        return "%s %s" % (words, href)
    return href or words


def rich_pieces(rich: dict, P) -> List[Tuple[str, str]]:
    """A rich text's pieces in order, as (kind, words): "text" runs, "at" names, "other" (links, placeholders),
    "code" (a code block's lines; in an @自己 request only, the message's text reads as before), and "break" (no
    words) where a block element starts (a paragraph, a heading, a list item, a rule…: BLOCK_TAGS)."""
    try:
        dictionary = (rich.get("elements") or {}).get("dictionary") or {}
    except AttributeError:
        return []
    ids = [str(k) for k in rich.get("elementIds") or []]
    if not ids:
        try:
            ids = sorted(dictionary, key=lambda k: int(k))
        except (TypeError, ValueError):
            ids = list(dictionary)
    order: List[str] = []
    seen: Set[str] = set()

    def visit(k: str, depth: int = 0) -> None:
        if k in seen or k not in dictionary or depth > 20:
            return
        seen.add(k)
        order.append(k)
        el = dictionary.get(k)
        for c in (el.get("childIds") or []) if isinstance(el, dict) else []:
            visit(str(c), depth + 1)

    for k in ids:
        visit(k)
    for k in dictionary:
        visit(k)
    out: List[Tuple[str, str]] = []
    for k in order:
        el = dictionary.get(k)
        if not isinstance(el, dict):
            continue
        tag = el.get("tag", 1)
        try:
            tag = int(tag)
        except (TypeError, ValueError):
            tag = -1
        prop = el.get("property") or b""
        if tag in BLOCK_TAGS:
            out.append(("break", ""))
        if tag in CODE_BLOCK_TAGS and prop:
            words = code_block_words(tag, prop)
            if words:
                out.append(("code", words))
            continue
        if tag in TEXT_TAGS and prop:
            try:
                tp = P.TextProperty()
                tp.ParseFromString(prop)
                out.append(("text", tp.content + ("\n" if tag == 3 else "")))
                continue
            except Exception:
                pass
        if tag == AT_TAG_RICH:
            try:
                ap = P.AtProperty()
                ap.ParseFromString(prop)
                out.append(("at", ap.content or "[@]"))
            except Exception:
                out.append(("at", "[@]"))
            continue
        if tag == ANCHOR_TAG and prop:
            words = link_text(prop)
            if words:
                out.append(("other", words))
                continue
        if tag in PLACEHOLDERS:
            out.append(("other", PLACEHOLDERS[tag]))
            continue
    return out


def code_block_words(tag: int, prop: bytes) -> str:
    """A code block's text (CODE_BLOCK's content, CODE_BLOCK_V2's lines), "" when it cannot be read."""
    try:
        from larkx.proto import lark_all_pb2

        E = lark_all_pb2.entities.RichTextElement
        if tag == 31:
            cb = E.CodeBlockProperty()
            cb.ParseFromString(prop)
            return cb.content
        v2 = E.CodeBlockV2Property()
        v2.ParseFromString(prop)
        return "\n".join("".join(c.content for c in line.contents) for line in v2.contents)
    except Exception:
        return ""


def rich_text(rich: dict, P) -> str:
    """A rich text's words in order: text runs, @names, links (their words and address), placeholders; a block (a
    paragraph, a DOCS block…) after words starts a new line, so two never run into one word ("/st" + "op")."""
    out: List[str] = []
    gap = False
    for k, w in rich_pieces(rich, P):
        if k == "code":
            continue
        if k == "break":
            gap = bool(out)
            continue
        if gap and w:
            out.append("\n")
            gap = False
        out.append(w)
    text = "".join(out)
    return text if text.strip() else ""


def rich_after_mentions(rich: dict, P, cut: bool = True) -> Optional[str]:
    """A rich text's words after the @ elements it starts with (and the spaces around them), cut where those
    elements end: "@Jane Doe 总结" → "总结", "@张三总结" → "总结"; all of them when it starts with none (a typed
    "@types/node" is words). cut=False: nothing cut (a POST's body after its title). A new block starts a new line.
    None when it has no pieces (the core cuts the text itself then)."""
    pieces = rich_pieces(rich, P)
    if not pieces:
        return None
    i = 0
    while cut and i < len(pieces):
        kind, words = pieces[i]
        if kind not in ("at", "break") and not (kind == "text" and not words.strip()):
            break
        i += 1
    # a new paragraph starts a new line (two paragraphs never run into one word: "/st" + "op" is no /stop), a code
    # block stands on lines of its own, its indents as written (only the words' own leading spaces go)
    rest = pieces[i:]
    if rest and rest[0][0] != "code":
        rest = [(rest[0][0], rest[0][1].lstrip())] + rest[1:]
    return "".join("\n" if kind == "break" else w + "\n" if kind == "code" else w
                   for kind, w in rest).lstrip("\n").rstrip()


def hang_links(entity: dict) -> List[str]:
    """The links of a message entity's preview hang points (urlPreviewHangPointMap: {key: {url, previewId, …}})."""
    out: List[str] = []
    hangs = entity.get("urlPreviewHangPointMap") or {}
    for hp in (hangs.values() if isinstance(hangs, dict) else hangs if isinstance(hangs, list) else []):
        if isinstance(hp, dict) and "value" in hp and isinstance(hp["value"], dict):
            hp = hp["value"]  # a map read back as entries
        url = str((hp or {}).get("url") or "").strip() if isinstance(hp, dict) else ""
        if URL_RE.match(url) and url not in out:
            out.append(url)
    return out


class Reacted(dict):
    """{message id: when} of the account's reactions, and unasked: the ids a batch that failed left unknown."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.unasked: Set[str] = set()


def dlp_state(entity: dict):
    """The entity's contentState.dlpReviewState (a number or a name), None when it has none."""
    state = entity.get("contentState") if isinstance(entity.get("contentState"), dict) else {}
    v = state.get("dlpReviewState")
    return v if v not in (None, 0, "UNKNOWN_STATE") else None


def entity_flags(entity: dict) -> dict:
    """What says why a message entity's content reads as nothing: flags and sizes only, never content."""
    state = entity.get("contentState") if isinstance(entity.get("contentState"), dict) else {}
    crypto = entity.get("cryptoInfo") if isinstance(entity.get("cryptoInfo"), dict) else {}
    return {
        "isEncrypted": bool(entity.get("isEncrypted")), "encryptedInfo": bool(entity.get("encryptedInfo")),
        "cryptoType": crypto.get("cryptoType"), "isTruncated": bool(entity.get("isTruncated")),
        "content": len(entity.get("content") or b""), "characterLength": entity.get("characterLength"),
        "hangPoints": len(entity.get("urlPreviewHangPointMap") or {}), "isIncludeDocUrl": bool(entity.get("isIncludeDocUrl")),
        "state": {k: v for k, v in sorted(state.items()) if v not in (None, False, 0, "")},
        "displayMode": entity.get("displayMode"),
        "references": len(entity.get("references") or []),
    }


def rich_shape(rich: dict) -> str:
    """The tags of a rich text's elements and their properties' field layout, never their content."""
    parts = []
    dictionary = ((rich or {}).get("elements") or {}).get("dictionary") or {}
    for el in list(dictionary.values())[:12]:
        if not isinstance(el, dict):
            continue
        prop = el.get("property") or b""
        parts.append("%s:%d strings" % (el.get("tag"), len(wire_strings(prop)) if prop else 0))
    return "elements=[%s] innerText=%s" % (",".join(parts), "yes" if (rich or {}).get("innerText") else "no")


def note_flags(name: str, flags: dict) -> None:
    """A message read as nothing: its entity's flags in the log (values: flags and sizes, no content), once each."""
    line = json.dumps(flags, sort_keys=True, default=str)
    key = name + "flags" + line
    if key in _SHAPES_SEEN or len(_SHAPES_SEEN) > 200:
        return
    _SHAPES_SEEN.add(key)
    log.info("a %s message read as nothing; its entity: %s", name, line[:800])


def note_shape(name: str, data: dict, extra: str = "") -> None:
    """A message read as nothing: its decoded shape in the log, once per shape (to learn where its text is)."""
    shape = shape_of(data) + ((" " + extra) if extra else "")
    key = name + shape
    if key in _SHAPES_SEEN or len(_SHAPES_SEEN) > 200:
        return
    _SHAPES_SEEN.add(key)
    log.info("a %s message read as nothing; its shape: %s", name, shape[:1500])

