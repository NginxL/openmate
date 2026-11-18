"""A deterministic, in-memory chat service for tests (OPENMATE_BACKEND=fixture): synthetic people and chats, a QR
sign-in that scans itself, a live link the test can drop, and knobs for failures. No network, no real accounts.

The world is seeded relative to the time it is made, so its messages are always inside the inbox window.
"""

from __future__ import annotations

import base64
import struct
import threading
import time
import zlib
from typing import Callable, Dict, List, Optional, Tuple

from .backend import (Account, Backend, BackendError, Chat, CredentialsExpired, LoginDone, LoginQR, Media, Message,
                      Person, SendRejected)

ME = Account("user_example_me", "示例用户")


def tiny_png(color: Tuple[int, int, int] = (40, 120, 200), size: int = 2) -> bytes:
    """A valid PNG of size×size pixels of one color."""
    raw = b"".join(b"\x00" + bytes(color) * size for _ in range(size))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class FixtureWorld:
    """The service's state. Tests reach into it: push() a message, drop() the link, set the knobs."""

    def __init__(self, now: Optional[float] = None, seed: bool = True):
        self.lock = threading.RLock()
        self.start = now if now is not None else time.time()
        self.me = ME
        self.people: Dict[str, str] = {
            "user_example_a": "示例同事甲",
            "user_example_b": "示例同事乙",
            "user_example_c": "示例同事丙",
        }
        self.bots: Dict[str, str] = {"bot_example_notice": "示例通知机器人"}  # programs: people_kinds says "bot"
        self.people.update(self.bots)
        self.kinds_fail = False  # people_kinds raises
        self.reactions: List[Tuple[str, str, str]] = []  # (chat, message, reaction) put by react
        self.doc_replies: List[Tuple[str, str, str]] = []  # (doc url, comment id, text) by reply_doc_comment
        self.doc_reply_fail = False
        self.doc_reply_uncertain = False  # reply_doc_comment: the reply may have gone (BackendError "uncertain")
        self.doc_writer: Optional[str] = None  # who doc replies go out as (doc_writer_name); None: the account itself
        self.doc_texts: Dict[str, str] = {}  # doc url → its text, for doc_text
        self.doc_listener: Optional[Callable] = None
        self.done: Dict[str, float] = {}  # chats in the service's 完成 → when they went there (a new message reopens)
        self.done_listener: Optional[Callable] = None
        self.done_asks: List[List[str]] = []  # chat_done_states calls
        self.done_fail = False  # chat_done_states raises
        self.done_hook: Optional[Callable[[], None]] = None  # run by chat_done_states after it read its answer
        self.my_reacted: Dict[str, Tuple[float, str]] = {}  # message id → (when, type) of the account's reaction on it
        self.reactions_fail = False  # my_reactions raises
        self.reacted_listener: Optional[Callable] = None
        self.reactions_asks: List[List[str]] = []
        self.react_fail = False
        self.edits: List[Tuple[str, str, str]] = []  # (chat, message, new text) by edit
        self.edit_fail = False
        self.kinds_calls: List[List[str]] = []
        self.chats: Dict[str, Chat] = {}
        self.history: Dict[str, List[Message]] = {}
        self._ids = 0
        self._qr = 0
        self.qr_polls: Dict[str, int] = {}
        # knobs
        self.qr_script: List[str] = []  # statuses login_poll answers first (then scanned, confirmed)
        self.expired = False  # restore / listen say the sign-in is gone
        self.listen_error: Optional[BackendError] = None  # listen raises it
        self.hold_up = False  # listen connects but does not come up until cleared
        self.send_fail: Optional[str] = None  # "rejected" | "uncertain" for the next send
        self.send_fail_always: Optional[str] = None
        self.send_delay = 0.0
        self.create_fail = False
        self.history_fail = False
        # what happened
        self.sent: List[dict] = []
        self.history_calls: List[Tuple[str, Optional[int], int]] = []
        self.created_chats: List[str] = []
        self.connections = 0
        self.listener: Optional[Callable[[Message], None]] = None
        self.drop_event = threading.Event()
        self.up_event = threading.Event()
        if seed:
            self._seed()

    # ---- building ----

    def next_id(self, prefix: str = "msg_example_") -> str:
        with self.lock:
            self._ids += 1
            return "%s%d" % (prefix, self._ids)

    def add_chat(self, chat: Chat) -> Chat:
        with self.lock:
            self.chats[chat.id] = chat
            self.history.setdefault(chat.id, [])
        return chat

    def make(self, chat_id: str, sender: str, text: str, ago: float = 0.0, **kw) -> Message:
        """A message of chat from sender ("me" for the account), ago seconds before the world's start + now offset."""
        with self.lock:
            hist = self.history.setdefault(chat_id, [])
            pos = (hist[-1].position + 1) if hist else 1
            mine = sender == "me"
            sid = self.me.id if mine else sender
            name = self.me.name if mine else self.people.get(sender, "")
            at = kw.pop("at", None)
            if at is None:
                at = time.time() - ago
            m = Message(id=kw.pop("id", None) or self.next_id(), chat_id=chat_id, position=pos, sender_id=sid,
                        sender_name=name, mine=mine, text=text, at=at, **kw)
            hist.append(m)
            self.done.pop(chat_id, None)  # a new message takes the chat out of 完成
            return m

    def _seed(self) -> None:
        self.add_chat(Chat("chat_example_dm_a", "dm", "示例同事甲", peer_id="user_example_a"))
        self.add_chat(Chat("chat_example_team", "group", "示例项目群", members=5))
        self.add_chat(Chat("chat_example_chatter", "group", "示例闲聊群", members=40))
        self.add_chat(Chat("chat_example_dm_b", "dm", "示例同事乙", peer_id="user_example_b"))
        # the direct chat with A: A asked, I answered, A asked again (unanswered), with a picture
        self.make("chat_example_dm_a", "user_example_a", "周五评审的材料准备好了吗？", ago=3000)
        self.make("chat_example_dm_a", "me", "还差一点，今天下午发你", ago=2700)
        self.make("chat_example_dm_a", "user_example_a", "好的。另外麻烦看下这份文档 https://example.feishu.cn/docx/ExampleDoc001", ago=600)
        self.make("chat_example_dm_a", "user_example_a", "", ago=590, type="IMAGE",
                  media=[Media("image", ref="img:example-1", mime="image/png", width=2, height=2)])
        # the team group: a system line, a plain message, then B @-mentions me
        self.make("chat_example_team", "user_example_c", "示例同事丙 加入了群聊", ago=7200, type="SYSTEM")
        self.make("chat_example_team", "user_example_a", "下午三点开周会", ago=5400)
        self.make("chat_example_team", "user_example_b", "@示例用户 接口文档这周谁来更新？", ago=1800, at_me=True)
        # the chatter group: nobody mentions me
        self.make("chat_example_chatter", "user_example_b", "今天食堂有饺子", ago=1200)
        self.make("chat_example_chatter", "user_example_c", "哈哈，去晚了就没了", ago=1100, at_all=False)
        # the direct chat with B: I spoke last (done)
        self.make("chat_example_dm_b", "user_example_b", "收到你的邮件了", ago=4000)
        self.make("chat_example_dm_b", "me", "好的，有问题再找我", ago=3900)

    # ---- the live link ----

    def push(self, chat_id: str, sender: str, text: str, ago: float = 0.0, **kw) -> Message:
        """A new message: kept in the chat's history and handed to the live listener, if connected."""
        m = self.make(chat_id, sender, text, ago=ago, **kw)
        with self.lock:
            listener = self.listener
        if listener is not None:
            listener(m)
        return m

    def mention_in_doc(self, mention) -> None:
        """Someone @-mentions the user in a document's comment (handed to the live listener, if connected)."""
        with self.lock:
            listener = self.doc_listener
        if listener is not None:
            listener(mention)

    def mark_done(self, chat_id: str, when: Optional[float] = None, push: bool = True) -> None:
        """The user marks a chat 完成 in the service's own client: told to the live listener (push), if connected."""
        with self.lock:
            at = time.time() if when is None else when
            self.done[chat_id] = at
            listener = self.done_listener if push else None
        if listener is not None:
            listener({chat_id: at})

    def react_as_me(self, message_id: str, when: Optional[float] = None, push: bool = True, kind: str = "DONE") -> None:
        """The user puts a reaction on a message in the service's own client: told to the live listener (push)."""
        with self.lock:
            self.my_reacted[message_id] = (time.time() if when is None else when, kind)
            listener = self.reacted_listener if push else None
        if listener is not None:
            listener([message_id])

    def mark_inbox(self, chat_id: str) -> None:
        """The user moves a chat back from 完成 in the service's own client (no new message)."""
        with self.lock:
            self.done.pop(chat_id, None)

    def drop(self) -> None:
        """End the current live connection (listen returns)."""
        self.drop_event.set()

    @property
    def connected(self) -> bool:
        with self.lock:
            return self.listener is not None


class FixtureBackend(Backend):
    done_marks = True

    def __init__(self, world: Optional[FixtureWorld] = None):
        self.world = world or FixtureWorld()
        self.signed_in: Optional[Account] = None

    # sign-in

    def login_start(self) -> LoginQR:
        w = self.world
        with w.lock:
            w._qr += 1
            state = "qr%d" % w._qr
            w.qr_polls[state] = 0
        return LoginQR(url="https://example.com/openmate-fixture/qr/%s" % state, state=state)

    def login_poll(self, state: str) -> str:
        w = self.world
        with w.lock:
            if state not in w.qr_polls:
                return "expired"
            if w.qr_script:
                return w.qr_script.pop(0)
            w.qr_polls[state] += 1
            return "scanned" if w.qr_polls[state] == 1 else "confirmed"

    def login_finish(self, state: str) -> LoginDone:
        self.signed_in = self.world.me
        token = base64.b64encode(("fixture:" + state).encode()).decode()
        return LoginDone(credentials={"fixture_token": token, "user": self.world.me.id}, account=self.world.me)

    def restore(self, credentials: Dict) -> Account:
        if self.world.expired:
            raise CredentialsExpired()
        if not credentials.get("fixture_token"):
            raise CredentialsExpired()
        self.signed_in = self.world.me
        return self.world.me

    def logout(self) -> None:
        self.signed_in = None

    # the live link

    def listen(self, on_message, on_link, stop: threading.Event) -> None:
        w = self.world
        if w.expired:
            raise CredentialsExpired()
        if w.listen_error is not None:
            raise w.listen_error
        w.drop_event.clear()
        with w.lock:
            w.connections += 1
        while w.hold_up and not stop.is_set() and not w.drop_event.is_set():
            time.sleep(0.01)
        if stop.is_set() or w.drop_event.is_set():
            return
        with w.lock:
            w.listener = on_message
            w.doc_listener = lambda m: self.on_doc_mention(m) if self.on_doc_mention is not None else None
            w.done_listener = lambda d: self.on_chats_done(d) if self.on_chats_done is not None else None
            w.reacted_listener = lambda ids: self.on_reacted(ids) if self.on_reacted is not None else None
        w.up_event.set()
        on_link("up", "")
        try:
            while not stop.is_set() and not w.drop_event.is_set():
                if w.expired:
                    on_link("expired", "")
                    raise CredentialsExpired()
                stop.wait(0.02)
        finally:
            with w.lock:
                w.listener = None
                w.doc_listener = None
                w.done_listener = None
                w.reacted_listener = None
            w.up_event.clear()

    # reads

    def history(self, chat_id: str, before_position: Optional[int] = None, limit: int = 50) -> List[Message]:
        w = self.world
        with w.lock:
            w.history_calls.append((chat_id, before_position, limit))
            if w.history_fail:
                raise BackendError("暂时连不上示例服务", "unreachable")
            msgs = list(w.history.get(chat_id, []))
        if before_position is not None:
            msgs = [m for m in msgs if m.position < before_position]
        return msgs[-limit:] if limit > 0 else []

    def recent_chats(self, limit: int) -> List[Chat]:
        w = self.world
        with w.lock:
            def last(cid: str) -> float:
                h = w.history.get(cid) or []
                return h[-1].at if h else 0.0

            ids = sorted(w.chats, key=last, reverse=True)
            return [w.chats[c] for c in ids[:limit]]

    def search(self, query: str, limit: int = 20) -> List[Person]:
        w = self.world
        q = (query or "").lower()
        out: List[Person] = []
        with w.lock:
            for uid, name in sorted(w.people.items()):
                if q and (q in name.lower() or q == uid):
                    out.append(Person("user", uid, name))
            for c in w.chats.values():
                if c.kind == "group" and q and q in c.name.lower():
                    out.append(Person("group", c.id, c.name, "%d 人" % c.members if c.members else ""))
        return out[:limit]

    def chat_info(self, chat_id: str) -> Chat:
        with self.world.lock:
            c = self.world.chats.get(chat_id)
        if c is None:
            raise BackendError("没有这个会话", "not_found")
        return c

    def chat_done_states(self, chat_ids: List[str]) -> Dict[str, Optional[float]]:
        w = self.world
        with w.lock:
            w.done_asks.append(list(chat_ids))
            hook = w.done_hook
            if w.done_fail:
                raise BackendError("连不上（读取会话的完成状态时）", "unreachable")
            answer = {c: w.done.get(c) for c in chat_ids}
        if hook is not None:
            hook()  # what happens while the answer is on its way back
        return answer

    def my_reactions(self, message_ids: List[str], ignore: Tuple[str, ...] = ()) -> Dict[str, float]:
        w = self.world
        with w.lock:
            w.reactions_asks.append(list(message_ids))
            if w.reactions_fail:
                raise BackendError("连不上（读取表情回应时）", "unreachable")
            return {m: w.my_reacted[m][0] for m in message_ids if m in w.my_reacted and w.my_reacted[m][1] not in ignore}

    def doc_text(self, doc_url: str, most: int) -> str:
        with self.world.lock:
            return self.world.doc_texts.get(doc_url, "")[:most]

    def doc_writer_name(self) -> str:
        w = self.world
        return w.me.name if w.doc_writer is None else w.doc_writer

    def reply_doc_comment(self, doc_url: str, comment_id: str, text: str) -> str:
        w = self.world
        with w.lock:
            if w.doc_reply_fail or not comment_id:
                raise BackendError("这条评论回复不了", "unavailable")
            w.doc_replies.append((doc_url, comment_id, text))
            if w.doc_reply_uncertain:  # it went, but the answer did not come back
                raise BackendError("lark-cli 回复评论超时了", "uncertain")
            return "reply_%d" % len(w.doc_replies)

    def react(self, chat_id: str, message_id: str, reaction: str) -> None:
        w = self.world
        with w.lock:
            if w.react_fail:
                raise BackendError("表情回应失败", "unavailable")
            w.reactions.append((chat_id, message_id, reaction))

    def unreact(self, chat_id: str, message_id: str, reaction: str) -> None:
        w = self.world
        with w.lock:
            if w.react_fail:
                raise BackendError("撤回表情失败", "unavailable")
            if (chat_id, message_id, reaction) in w.reactions:
                w.reactions.remove((chat_id, message_id, reaction))

    def edit(self, chat_id: str, message_id: str, text: str) -> None:
        w = self.world
        with w.lock:
            if w.edit_fail:
                raise BackendError("这条消息改不了", "unavailable")
            mine = [s for s in w.sent if s["chat"] == chat_id and s["id"] == message_id]
            if not mine:
                raise BackendError("不是自己发的消息", "not_found")
            mine[-1]["text"] = text
            w.edits.append((chat_id, message_id, text))

    def people_kinds(self, user_ids: List[str]) -> Dict[str, str]:
        w = self.world
        with w.lock:
            w.kinds_calls.append(list(user_ids))
            if w.kinds_fail:
                raise BackendError("暂时读不到", "unavailable")
            return {u: ("bot" if u in w.bots else "user") for u in user_ids if u in w.people or u == w.me.id}

    def user_name(self, user_id: str, chat_id: Optional[str] = None) -> str:
        if user_id == self.world.me.id:
            return self.world.me.name
        return self.world.people.get(user_id, "")

    def media(self, ref: str, max_bytes: int) -> Tuple[str, bytes]:
        if ref.startswith("img:"):
            data = tiny_png()
            mime = "image/png"
        elif ref.startswith("big:"):
            data = b"\0" * int(ref.split(":", 1)[1])
            mime = "application/octet-stream"
        elif ref.startswith("file:"):
            data = ("示例文件 " + ref).encode("utf-8")
            mime = "text/plain"
        else:
            raise BackendError("没有这个附件", "not_found")
        if len(data) > max_bytes:
            raise BackendError("附件超过 %d MB" % (max_bytes >> 20), "too_large")
        return mime, data

    # writes

    def send(self, chat_id: str, text: str, reply_to: Optional[str] = None, root: Optional[str] = None) -> str:
        w = self.world
        if w.send_delay:
            time.sleep(w.send_delay)
        with w.lock:
            fail = w.send_fail or w.send_fail_always
            w.send_fail = None
        if fail == "rejected":
            raise SendRejected("示例服务拒绝了这条消息")
        if fail == "uncertain":
            raise BackendError("发送超时", "unreachable")
        if chat_id not in w.chats:
            raise SendRejected("没有这个会话")
        m = w.push(chat_id, "me", text, parent_id=reply_to or "", root_id=root or "")
        with w.lock:
            w.sent.append({"chat": chat_id, "text": text, "reply_to": reply_to, "root": root, "id": m.id})
        return m.id

    def create_chat(self, user_id: str) -> str:
        w = self.world
        if w.create_fail:
            raise BackendError("暂时建不了会话", "unreachable")
        with w.lock:
            for c in w.chats.values():
                if c.kind == "dm" and c.peer_id == user_id:
                    return c.id
            if user_id not in w.people:
                raise BackendError("没有这个人", "not_found")
            cid = "chat_example_dm_" + user_id.rsplit("_", 1)[-1]
            w.add_chat(Chat(cid, "dm", w.people[user_id], peer_id=user_id))
            w.created_chats.append(cid)
            return cid
