"""The WeChat backend: OpenMate's Backend over the matewechat-native program.

Phase one does two things with WeChat: /agent (the user's own message, from the phone or any device) and 自动代答 in
the groups the user marked (someone else's real @ of the user). So:
- mine is the message's direction (the account's own, wherever it was typed); at_me only from the message's own
  @-list, never from a typed name; a group member's id from the group envelope.
- Only live messages reach the core's on_message (and so /agent and 自动代答). What a reconnect catches up and the
  echo of our own sends are kept here, for history(), and never acted on.
- A send quotes the message it answers; in a group it also @s that message's sender when it is someone else (the
  自动代答 asker; never the user's own /agent message). A send whose outcome is unknown is never sent again.
- Everything this backend keeps (messages, names, the sync cursor) is in its own SQLite file in the extension's data
  directory; the sign-in itself only in the hub's sealed connector secrets, through the core.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from openmate_core.backend import (
    Account,
    Backend,
    BackendError,
    Chat,
    CredentialsExpired,
    LoginDone,
    LoginQR,
    Message,
    OnLink,
    OnMessage,
    Person,
    SendRejected,
)

from .native import Native, NativeError, NativeGone

log = logging.getLogger("openmate.wechat")

EXPIRED = "微信登录已失效，需要重新扫码登录"
NAME_TTL = 6 * 3600.0
SEND_WAIT = 45.0
# Accounts that are WeChat's own or a program, never a person (official accounts are gh_…, ClawBot bots …@im.bot)
SERVICE_IDS = {"weixin", "filehelper", "fmessage", "medianote", "floatbottle", "qqmail", "newsapp", "notifymessage",
               "tmessage", "qmessage", "qqsync", "voip", "voipapp", "brandsessionholder", "officialaccounts"}
PERSON_ID = re.compile(r"^(wxid_[A-Za-z0-9_-]{4,64}|[A-Za-z][A-Za-z0-9_-]{5,31})$")

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY, chat TEXT NOT NULL, position INTEGER NOT NULL, at INTEGER NOT NULL, body TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS messages_chat ON messages (chat, position)",
    """CREATE TABLE IF NOT EXISTS chats (
        id TEXT PRIMARY KEY, last_at INTEGER NOT NULL, name TEXT NOT NULL DEFAULT '', named_at REAL NOT NULL DEFAULT 0,
        members INTEGER NOT NULL DEFAULT 0)""",
    "CREATE TABLE IF NOT EXISTS names (key TEXT PRIMARY KEY, name TEXT NOT NULL, mention TEXT NOT NULL, at REAL NOT NULL)",
)


def is_group(chat_id: str) -> bool:
    return chat_id.endswith("@chatroom")


def kind_of(user_id: str) -> str:
    """"bot", "user" or "" (can't tell). Personal WeChat accounts are people's: a group's members are accounts people
    signed in to (an account someone automates still is one, as ours is; the core's AI marks and limits cover that)."""
    if not user_id:
        return ""
    if user_id in SERVICE_IDS or user_id.startswith("gh_") or user_id.endswith("@im.bot") or user_id.endswith("@stranger"):
        return "bot"
    if user_id.endswith("@chatroom"):
        return ""
    if user_id.endswith("@openim"):  # a WeCom member in a WeChat group: a person's work account
        return "user"
    return "user" if PERSON_ID.match(user_id) else ""


class Store:
    """This backend's own small database (messages for history(), names, the cursor)."""

    def __init__(self, path: str):
        self.path = path
        self._mu = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        for s in SCHEMA:
            self._db.execute(s)

    def close(self) -> None:
        with self._mu:
            self._db.close()

    def get(self, key: str) -> str:
        with self._mu:
            row = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else ""

    def put(self, key: str, value: str) -> None:
        with self._mu:
            self._db.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))

    def wipe(self) -> None:
        with self._mu:
            for t in ("messages", "chats", "names"):
                self._db.execute("DELETE FROM " + t)
            self._db.execute("DELETE FROM kv WHERE key IN ('cursor')")

    def add_message(self, ev: dict) -> Tuple[bool, int]:
        """Keep a message (once); (new, position). position grows with time: second * 1000 + its place that second."""
        mid, chat, at = str(ev.get("id") or ""), str(ev.get("chat") or ""), int(ev.get("at") or 0)
        with self._mu:
            row = self._db.execute("SELECT position FROM messages WHERE id=?", (mid,)).fetchone()
            if row:
                return False, row[0]
            n = self._db.execute("SELECT COUNT(*) FROM messages WHERE chat=? AND at=?", (chat, at)).fetchone()[0]
            pos = at * 1000 + min(n, 999)
            self._db.execute("INSERT INTO messages (id, chat, position, at, body) VALUES (?, ?, ?, ?, ?)",
                             (mid, chat, pos, at, json.dumps(ev, ensure_ascii=False)))
            self._db.execute("INSERT INTO chats (id, last_at) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET "
                             "last_at=MAX(last_at, excluded.last_at)", (chat, at))
        return True, pos

    def message(self, mid: str) -> Optional[Tuple[dict, int]]:
        with self._mu:
            row = self._db.execute("SELECT body, position FROM messages WHERE id=?", (mid,)).fetchone()
        return (json.loads(row[0]), row[1]) if row else None

    def messages(self, chat: str, before: Optional[int], limit: int) -> List[Tuple[dict, int]]:
        q = "SELECT body, position FROM messages WHERE chat=?"
        args: List[Any] = [chat]
        if before is not None:
            q += " AND position < ?"
            args.append(before)
        q += " ORDER BY position DESC LIMIT ?"
        args.append(max(1, min(limit, 200)))
        with self._mu:
            rows = self._db.execute(q, args).fetchall()
        return [(json.loads(b), p) for b, p in reversed(rows)]

    def chats(self, limit: int) -> List[Tuple[str, str, int]]:
        with self._mu:
            return self._db.execute("SELECT id, name, members FROM chats ORDER BY last_at DESC LIMIT ?", (limit,)).fetchall()

    def chat(self, chat_id: str) -> Optional[Tuple[str, float, int]]:
        with self._mu:
            return self._db.execute("SELECT name, named_at, members FROM chats WHERE id=?", (chat_id,)).fetchone()

    def name_chat(self, chat_id: str, name: str, members: int) -> None:
        with self._mu:
            self._db.execute("INSERT INTO chats (id, last_at, name, named_at, members) VALUES (?, 0, ?, ?, ?) "
                             "ON CONFLICT(id) DO UPDATE SET name=excluded.name, named_at=excluded.named_at, "
                             "members=MAX(members, excluded.members)", (chat_id, name, time.time(), members))

    def name(self, key: str) -> Optional[Tuple[str, str, float]]:
        with self._mu:
            return self._db.execute("SELECT name, mention, at FROM names WHERE key=?", (key,)).fetchone()

    def set_name(self, key: str, name: str, mention: str) -> None:
        with self._mu:
            self._db.execute("INSERT OR REPLACE INTO names (key, name, mention, at) VALUES (?, ?, ?, ?)",
                             (key, name, mention, time.time()))

    def search(self, query: str, limit: int) -> List[Tuple[str, str]]:
        like = "%" + query.replace("%", "").replace("_", "") + "%"
        with self._mu:
            return self._db.execute("SELECT id, name FROM chats WHERE name LIKE ? OR id LIKE ? ORDER BY last_at DESC "
                                    "LIMIT ?", (like, like, limit)).fetchall()


class WeChatBackend(Backend):
    render_version = "1"

    def __init__(self, native_path: Any, data_dir: str):
        """native_path: the program's path, or a callable that returns it (waiting for a download) or raises."""
        self._native_path = native_path
        os.makedirs(data_dir, exist_ok=True)
        self.store = Store(os.path.join(data_dir, "wechat.db"))
        self._native: Optional[Native] = None
        self._mu = threading.RLock()
        self._state: Optional[dict] = None  # the sign-in, as last sealed (to restore a restarted program)
        self._device: Optional[dict] = None  # the device of the sign-in in progress (memory only)
        self.me = ""
        self.me_name = ""

    # ---------------------------------------------------------------- the program

    def _path(self, wait: float) -> str:
        p = self._native_path(wait) if callable(self._native_path) else self._native_path
        if not p or not os.path.isfile(p):
            raise BackendError("没有找到微信组件 matewechat-native", "config")
        return p

    def _link(self, restore: bool = True, wait: float = 30.0) -> Native:
        with self._mu:
            n = self._native
            if n is not None and n.alive():
                return n
            n = Native(self._path(wait), self._persist)
            n.start()
            self._native = n
            if restore and self._state is not None:  # a restarted program knows nothing: give it the sign-in again
                self._call(n, "restore", 20, state=self._state)
            return n

    def _call(self, n: Native, op: str, timeout: float, **params: Any) -> dict:
        try:
            return n.call(op, timeout=timeout, **params)
        except NativeGone as e:
            raise BackendError(e.message, "unreachable")
        except NativeError as e:
            if e.kind in ("expired", "signed_out"):
                raise CredentialsExpired(EXPIRED if e.kind == "expired" else "微信还没登录")
            raise BackendError(e.message, {"input": "rejected", "unsupported": "unsupported"}.get(e.kind, "unknown"))

    def _persist(self, state: Dict[str, Any]) -> bool:
        """The program renewed the sign-in: seal it before it is used."""
        creds = {"v": 1, "state": state}
        ok = False
        if self.on_credentials_sync is not None:
            ok = bool(self.on_credentials_sync(creds))
        elif self.on_credentials is not None:
            self.on_credentials(creds)
            ok = True
        if ok:
            self._state = state
        return ok

    def close(self) -> None:
        n = self._native
        if n is not None:
            n.close()
        self.store.close()

    # ---------------------------------------------------------------- sign-in

    def login_start(self) -> LoginQR:
        n = self._link(restore=False, wait=5.0)  # a sign-in step answers the hub within 10 s
        r = self._call(n, "login_start", 9, device=self._device)
        self._device = r.get("device") or None
        return LoginQR(url=str(r.get("qr") or ""), state="qr")

    def login_poll(self, state: str) -> str:
        n = self._link(restore=False, wait=1.0)
        try:
            r = n.call("login_poll", timeout=9)
        except NativeGone as e:
            raise BackendError(e.message, "unreachable")
        except NativeError as e:
            raise BackendError(e.message, "unknown")
        s = str(r.get("status") or "pending")
        return {"pending": "waiting", "cancelled": "expired", "rejected": "expired"}.get(s, s)

    def login_finish(self, state: str) -> LoginDone:
        n = self._link(restore=False, wait=1.0)
        r = self._call(n, "login_finish", 9)
        st = r.get("state")
        if not isinstance(st, dict):
            raise BackendError("登录完成了，但没有拿到登录信息", "unknown")
        self._state = st
        acct = self._adopt(r.get("account") or {})
        return LoginDone(credentials={"v": 1, "state": st}, account=acct)

    def restore(self, credentials: Dict) -> Account:
        st = credentials.get("state") if isinstance(credentials, dict) else None
        if not isinstance(st, dict):
            raise CredentialsExpired(EXPIRED)
        self._state = st
        n = self._link(restore=False)
        r = self._call(n, "restore", 20, state=st)
        return self._adopt(r.get("account") or {})

    def _adopt(self, account: dict) -> Account:
        me = str(account.get("id") or "")
        if me and self.store.get("account") not in ("", me):
            self.store.wipe()  # another account: none of the previous one's messages or names
        if me:
            self.store.put("account", me)
        self.me, self.me_name = me, str(account.get("name") or me)
        return Account(id=me, name=self.me_name)

    def logout(self) -> None:
        n = self._native
        self._state = None
        if n is not None and n.alive():
            try:
                n.call("logout", timeout=12)
            except NativeError:
                pass
        self.store.put("cursor", "")

    # ---------------------------------------------------------------- the live link

    def listen(self, on_message: OnMessage, on_link: OnLink, stop: threading.Event) -> None:
        n = self._link()
        self._call(n, "listen", 10, cursor=self.store.get("cursor"))
        events = n.events
        try:
            while not stop.is_set():
                try:
                    ev = events.get(timeout=1.0)
                except Exception:
                    continue
                if ev is None:
                    raise BackendError("微信组件退出了，正在重新连接", "unreachable")
                kind = ev.get("event")
                if kind == "message":
                    self._on_event(ev, on_message)
                elif kind == "cursor":
                    self.store.put("cursor", str(ev.get("cursor") or ""))  # after the messages before it
                elif kind == "link":
                    on_link("up" if ev.get("up") else "down", "" if ev.get("up") else "连接断开，正在重连")
                elif kind == "auth_lost":
                    raise CredentialsExpired(EXPIRED)
        finally:
            if n.alive():
                try:
                    n.call("stop", timeout=5)
                except NativeError:
                    pass

    def _on_event(self, ev: dict, on_message: OnMessage) -> None:
        if not ev.get("id") or not ev.get("chat"):
            return
        new, pos = self.store.add_message(ev)
        if not new or ev.get("origin") != "live":
            return  # caught up after a reconnect, or our own send's echo: kept for history, never acted on
        try:
            on_message(self._message(ev, pos))
        except Exception:
            log.exception("a WeChat message could not be handled")

    def _message(self, ev: dict, pos: int) -> Message:
        chat = str(ev.get("chat") or "")
        sender = str(ev.get("sender") or "")
        mine = bool(ev.get("mine"))
        name = self.me_name if mine else ""
        if not mine and sender:
            cached = self.store.name(self._name_key(sender, chat))
            name = cached[0] if cached else ""
        return Message(
            id=str(ev.get("id")), chat_id=chat, position=pos, sender_id=sender, sender_name=name, mine=mine,
            type=str(ev.get("type") or "TEXT"), text=str(ev.get("text") or ""), at=float(ev.get("at") or 0),
            parent_id=str(ev.get("parent") or ""), at_me=bool(ev.get("at_me")), at_all=bool(ev.get("at_all")),
        )

    # ---------------------------------------------------------------- reads

    def history(self, chat_id: str, before_position: Optional[int] = None, limit: int = 50) -> List[Message]:
        return [self._message(ev, pos) for ev, pos in self.store.messages(chat_id, before_position, limit)]

    def recent_chats(self, limit: int) -> List[Chat]:
        out = []
        for cid, name, members in self.store.chats(limit):
            out.append(Chat(id=cid, kind="group" if is_group(cid) else "dm", name=name,
                            peer_id="" if is_group(cid) else cid, members=members))
        return out

    def search(self, query: str, limit: int = 20) -> List[Person]:
        out = []
        for cid, name in self.store.search(query.strip(), limit):
            out.append(Person(kind="group" if is_group(cid) else "user", id=cid, name=name or cid))
        return out

    def chat_info(self, chat_id: str) -> Chat:
        row = self.store.chat(chat_id)
        if row is None or not row[0] or time.time() - row[1] > NAME_TTL:
            self._read_chat_name(chat_id)
            row = self.store.chat(chat_id) or ("", 0.0, 0)
        name, _, members = row
        group = is_group(chat_id)
        if not name:
            name = "群聊" if group else (self._lookup_names([chat_id]).get(chat_id, ("", ""))[0] or chat_id)
        return Chat(id=chat_id, kind="group" if group else "dm", name=name, peer_id="" if group else chat_id,
                    members=members)

    def _read_chat_name(self, chat_id: str) -> None:
        names = self._lookup_names([chat_id])
        name = names.get(chat_id, ("", ""))[0]
        members = 0
        if is_group(chat_id):
            members = len(self._roster(chat_id))
        if name or members:
            self.store.name_chat(chat_id, name, members)

    def _lookup_names(self, ids: List[str]) -> Dict[str, Tuple[str, str]]:
        """id → (shown name: remark or nickname, mention name: nickname) from WeChat."""
        try:
            r = self._call(self._link(), "contacts", 20, ids=ids[:40])
        except BackendError:
            return {}
        out = {}
        for c in r.get("contacts") or []:
            cid = str(c.get("id") or "")
            nick = str(c.get("nickname") or "")
            out[cid] = (str(c.get("remark") or "") or nick, nick)
            if cid and not is_group(cid):
                self.store.set_name(self._name_key(cid, ""), out[cid][0], nick)
        return out

    def _roster(self, chat_id: str) -> List[dict]:
        try:
            r = self._call(self._link(), "room", 20, id=chat_id)
        except BackendError:
            return []
        members = r.get("members") or []
        for m in members:
            uid = str(m.get("id") or "")
            if not uid:
                continue
            shown = str(m.get("display") or "") or str(m.get("nickname") or "")
            if shown:
                self.store.set_name(self._name_key(uid, chat_id), shown, shown)
        return members

    @staticmethod
    def _name_key(user_id: str, chat_id: Optional[str]) -> str:
        return "%s|%s" % (chat_id if chat_id and is_group(chat_id) else "", user_id)

    def user_name(self, user_id: str, chat_id: Optional[str] = None) -> str:
        if user_id == self.me and self.me_name:
            return self.me_name
        key = self._name_key(user_id, chat_id)
        cached = self.store.name(key)
        if cached and time.time() - cached[2] < NAME_TTL:
            return cached[0]
        if chat_id and is_group(chat_id):
            self._roster(chat_id)
            cached = self.store.name(key)
            if cached:
                return cached[0]
        found = self._lookup_names([user_id]).get(user_id)
        return found[0] if found else ""

    def _mention_name(self, user_id: str, chat_id: str) -> str:
        """The name put after @ on the wire: the group nickname, else the WeChat nickname, never a private remark."""
        cached = self.store.name(self._name_key(user_id, chat_id))
        if cached is None:
            self._roster(chat_id)
            cached = self.store.name(self._name_key(user_id, chat_id))
        if cached and cached[1]:
            return cached[1]
        found = self._lookup_names([user_id]).get(user_id)
        return (found[1] if found else "") or user_id

    def people_kinds(self, user_ids: List[str]) -> Dict[str, str]:
        out = {}
        for uid in user_ids:
            k = kind_of(uid)
            if k:
                out[uid] = k
        return out

    def media(self, ref: str, max_bytes: int) -> Tuple[str, bytes]:
        raise BackendError("这一版还不能查看微信里的图片和文件", "not_found")

    # ---------------------------------------------------------------- writes

    def send(self, chat_id: str, text: str, reply_to: Optional[str] = None, root: Optional[str] = None) -> str:
        """Text as the account into chat_id (root names no other chat on WeChat). reply_to is quoted; in a group, its
        sender is @-ed too when it is someone else. Only a send that surely did not go out raises SendRejected."""
        if not text.strip():
            raise SendRejected("消息内容是空的")
        params: Dict[str, Any] = {"chat": chat_id, "text": text, "key": "om-" + uuid.uuid4().hex}
        if reply_to:
            params["reply_to"] = reply_to
            got = self.store.message(reply_to)
            if got is not None:
                ev = got[0]
                sender = str(ev.get("sender") or "")
                if ev.get("chat") == chat_id and is_group(chat_id) and sender and not ev.get("mine") and sender != self.me:
                    name = self._mention_name(sender, chat_id)
                    params["mentions"] = [{"id": sender, "name": name}]
                    params["reply_name"] = name
        try:
            n = self._link()
        except BackendError as e:
            raise SendRejected("微信组件没准备好，没有发送：%s" % e.message)
        try:
            r = n.call("send", timeout=SEND_WAIT, **params)
        except NativeGone as e:
            raise BackendError("发送结果不确定（%s），不会自动重发" % e.message, "unknown")
        except NativeError as e:
            raise SendRejected("没有发送：%s" % e.message)  # refused before anything was transmitted
        status = r.get("status")
        if status == "sent":
            return str(r.get("message_id") or "")
        if status in ("rejected", "not_sent"):
            raise SendRejected(str(r.get("message") or "微信没有接受这条消息"))
        raise BackendError("发送结果不确定：%s；不会自动重发" % (r.get("message") or "微信没有确认"), "unknown")

    def create_chat(self, user_id: str) -> str:
        if not user_id or is_group(user_id):
            raise BackendError("不是一个微信用户", "not_found")
        return user_id  # a direct chat's id is the person's id
