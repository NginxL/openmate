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
GROUPS_EVERY = 6 * 3600.0  # the contacts' group list is read again this often (and on every contact change)
GROUPS_SPACING = 60.0
GROUPS_CHECK_EVERY = 600.0
GROUP_NAME_PAUSE = 0.3
SEND_WAIT = 45.0
# Accounts that are WeChat's own or a program, never a person (official accounts are gh_…, ClawBot bots …@im.bot)
SERVICE_IDS = {"filehelper", "weixin", "weibo", "fmessage", "floatbottle", "medianote", "qmessage", "tmessage",
               "masssendapp", "newsapp", "blogapp", "qqmail", "qqsync", "lbsapp", "shakeapp", "brandsessionholder",
               "brandservicesessionholder", "appbrandcustomerservicemsg", "service_officialaccounts", "notifymessage",
               "notification_messages", "wa_app_kefu_message", "wxzhifu", "voip", "voipapp"}  # agent-wechat's list
SERVICE_SUFFIXES = ("@im.bot", "@stranger", "@kefu.openim", "@opencustomerservicemsg", "@placeholder_foldgroup",
                    "@weclaw", "@im.chatroom")
PERSON_ID = re.compile(r"^(wxid_[A-Za-z0-9_-]{4,64}|[A-Za-z][A-Za-z0-9_-]{5,31})$")

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY, chat TEXT NOT NULL, position INTEGER NOT NULL, at INTEGER NOT NULL, body TEXT NOT NULL,
        delivered INTEGER NOT NULL DEFAULT 1)""",
    "CREATE INDEX IF NOT EXISTS messages_chat ON messages (chat, position)",
    """CREATE TABLE IF NOT EXISTS chats (
        id TEXT PRIMARY KEY, last_at INTEGER NOT NULL, name TEXT NOT NULL DEFAULT '', named_at REAL NOT NULL DEFAULT 0,
        members INTEGER NOT NULL DEFAULT 0)""",
    "CREATE TABLE IF NOT EXISTS names (key TEXT PRIMARY KEY, name TEXT NOT NULL, mention TEXT NOT NULL, at REAL NOT NULL)",
)


GROUP_SQL = "(id LIKE '%@chatroom%' OR id LIKE '%@im.chatroom' OR id LIKE '%@qy_g')"


def is_group(chat_id: str) -> bool:
    """An ordinary group (…@chatroom), a WeCom interop group (…@im.chatroom) or a WeCom group (…@qy_g)."""
    return "@chatroom" in chat_id or chat_id.endswith(("@im.chatroom", "@qy_g"))


# What WeChat said about a contact when its name was read: a verified official / service account, or a person.
# One backend per program, so the platform's archive rule (openmate_wechat.archive_chat) reads them here.
BUSINESS: set = set()
PERSONS: set = set()


def kind_of(user_id: str) -> str:
    """"bot", "user" or "" (can't tell). Personal WeChat accounts are people's: a group's members are accounts people
    signed in to (an account someone automates still is one, as ours is; the core's AI marks and limits cover that)."""
    if not user_id:
        return ""
    if user_id in SERVICE_IDS or user_id.startswith("gh_") or user_id.endswith(SERVICE_SUFFIXES) or user_id in BUSINESS:
        return "bot"
    if is_group(user_id):
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
        cols = [r[1] for r in self._db.execute("PRAGMA table_info(messages)").fetchall()]
        if "delivered" not in cols:  # a database from before the column: everything in it was handled
            self._db.execute("ALTER TABLE messages ADD COLUMN delivered INTEGER NOT NULL DEFAULT 1")

    def close(self) -> None:
        with self._mu:
            self._db.close()

    def get(self, key: str) -> str:
        with self._mu:
            row = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else ""

    def keys(self, prefix: str) -> List[str]:
        with self._mu:
            return [r[0] for r in self._db.execute("SELECT key FROM kv WHERE key LIKE ?", (prefix + "%",)).fetchall()]

    def put(self, key: str, value: str) -> None:
        with self._mu:
            self._db.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))

    def wipe(self) -> None:
        with self._mu:
            for t in ("messages", "chats", "names"):
                self._db.execute("DELETE FROM " + t)
            self._db.execute("DELETE FROM kv WHERE key IN ('cursor', 'groups_at') OR key LIKE 'acked:%' "
                             "OR key LIKE 'read:%' OR key LIKE 'biz:%' OR key LIKE 'person:%'")
        BUSINESS.clear()
        PERSONS.clear()

    def seen(self, mid: str) -> Optional[Tuple[int, bool]]:
        """(position, delivered) of a kept message, None when new."""
        with self._mu:
            row = self._db.execute("SELECT position, delivered FROM messages WHERE id=?", (mid,)).fetchone()
        return (row[0], bool(row[1])) if row else None

    def position_for(self, chat: str, at: int) -> int:
        with self._mu:
            n = self._db.execute("SELECT COUNT(*) FROM messages WHERE chat=? AND at=?", (chat, at)).fetchone()[0]
        return at * 1000 + min(n, 999)

    def mark_delivered(self, mid: str) -> None:
        with self._mu:
            self._db.execute("UPDATE messages SET delivered=1 WHERE id=?", (mid,))

    def add_message(self, ev: dict, pos: Optional[int] = None, delivered: bool = True) -> Tuple[bool, int]:
        """Keep a message (once); (new, position). position grows with time: second * 1000 + its place that second."""
        mid, chat, at = str(ev.get("id") or ""), str(ev.get("chat") or ""), int(ev.get("at") or 0)
        with self._mu:
            row = self._db.execute("SELECT position FROM messages WHERE id=?", (mid,)).fetchone()
            if row:
                return False, row[0]
            if pos is None:
                n = self._db.execute("SELECT COUNT(*) FROM messages WHERE chat=? AND at=?", (chat, at)).fetchone()[0]
                pos = at * 1000 + min(n, 999)
            self._db.execute("INSERT INTO messages (id, chat, position, at, body, delivered) VALUES (?, ?, ?, ?, ?, ?)",
                             (mid, chat, pos, at, json.dumps(ev, ensure_ascii=False), 1 if delivered else 0))
            self._db.execute("INSERT INTO chats (id, last_at) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET "
                             "last_at=MAX(last_at, excluded.last_at)", (chat, at))
        return True, pos

    def message(self, mid: str) -> Optional[Tuple[dict, int]]:
        with self._mu:
            row = self._db.execute("SELECT body, position FROM messages WHERE id=?", (mid,)).fetchone()
        return (json.loads(row[0]), row[1]) if row else None

    def messages(self, chat: str, before: Optional[int], limit: int) -> List[Tuple[dict, int]]:
        q = "SELECT body, position FROM messages WHERE chat=? AND delivered=1"  # a live one only once the core has it
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
                             "members=excluded.members", (chat_id, name, time.time(), members))

    def add_groups(self, ids: List[str]) -> int:
        """Groups from the contacts list: kept (with no message yet) so they can be marked; how many were new."""
        with self._mu:
            n = 0
            self._db.execute("BEGIN")
            try:
                for gid in ids:
                    cur = self._db.execute("INSERT OR IGNORE INTO chats (id, last_at) VALUES (?, 0)", (gid,))
                    n += cur.rowcount or 0
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
            return n

    def groups(self, limit: int) -> List[Tuple[str, str, int]]:
        """(id, name, members) of every group kept: the ones with messages, newest first, then the rest by name."""
        with self._mu:
            return self._db.execute("SELECT id, name, members FROM chats WHERE " + GROUP_SQL +
                                    " ORDER BY last_at DESC, name = '', name, id LIMIT ?", (limit,)).fetchall()

    def unnamed_groups(self, older_than: float, limit: int) -> List[str]:
        with self._mu:
            return [r[0] for r in self._db.execute(
                "SELECT id FROM chats WHERE " + GROUP_SQL + " AND name = '' AND named_at < ? "
                "ORDER BY last_at DESC LIMIT ?", (older_than, limit)).fetchall()]

    def unclassified_people(self, known: set, limit: int) -> List[str]:
        """Direct chats with no kind learnt yet (kept from before kinds were read): to look up in the background."""
        with self._mu:
            rows = self._db.execute("SELECT id FROM chats WHERE NOT (" + GROUP_SQL + ") ORDER BY last_at DESC LIMIT ?",
                                    (limit * 4,)).fetchall()
        return [r[0] for r in rows if r[0] not in known and kind_of(r[0]) == "user"][:limit]

    def last_incoming(self, chat: str) -> int:
        """When someone else last wrote in the chat (0: never, as far as kept here)."""
        with self._mu:
            rows = self._db.execute("SELECT body FROM messages WHERE chat=? ORDER BY position DESC LIMIT 200",
                                    (chat,)).fetchall()
        for (body,) in rows:
            ev = json.loads(body)
            if not ev.get("mine") and ev.get("type") != "SYSTEM":
                return int(ev.get("at") or 0)
        return 0

    def set_chat_name(self, chat_id: str, name: str) -> None:
        """A name read now ("" when WeChat has none: an unnamed group), without touching the member count."""
        with self._mu:
            self._db.execute("UPDATE chats SET name = CASE WHEN ? != '' THEN ? ELSE name END, named_at = ? WHERE id = ?",
                             (name, name, time.time(), chat_id))

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
    # a chat read on the phone (WeChat syncs the read to every device) after the last message from someone else
    # counts as 处理完 here, as Feishu's 完成 does; a new message brings it back
    done_marks = True

    def __init__(self, native_path: Any, data_dir: str, ack_text: str = ""):
        """native_path: the program's path, or a callable that returns it (waiting for a download) or raises.
        ack_text: the line put in the chat when a /agent or 自动代答 request is taken (the setting agent_ack_reaction);
        WeChat has no reactions, so the core's "reaction" is this message."""
        self.ack_text = (ack_text or "").strip()
        self._native_path = native_path
        os.makedirs(data_dir, exist_ok=True)
        self.store = Store(os.path.join(data_dir, "wechat.db"))
        self._native: Optional[Native] = None
        BUSINESS.update(k[4:] for k in self.store.keys("biz:"))
        PERSONS.update(k[7:] for k in self.store.keys("person:"))
        self._mu = threading.RLock()
        self._state: Optional[dict] = None  # the sign-in, as last sealed (to restore a restarted program)
        self._device: Optional[dict] = None  # the device of the sign-in in progress (memory only)
        self.me = ""
        self.me_name = ""
        self._gen = 0
        self._groups_mu = threading.Lock()
        self._groups_busy = False
        self._groups_pending = ""  # "full" or "names": asked for while the worker was busy
        self._kind_tried: set = set()  # direct chats asked about this run (WeChat may not say)
        self._groups_check_at = 0.0
        self._closed = False
        self._groups_full_at = 0.0  # the last time the contacts' group list was read (this run)
        self._ready_for: Optional[Native] = None  # the program that has the sign-in (restored or just signed in)

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
                if restore and self._ready_for is not n and self._state is not None:
                    self._call(n, "restore", 20, state=self._state)  # started for a QR: give it the sign-in first
                    self._ready_for = n
                return n
            n = Native(self._path(wait), self._persist)
            try:
                n.start()
            except NativeGone as e:
                raise BackendError(e.message, "unreachable")
            except NativeError as e:
                raise BackendError(e.message, "config")
            except OSError as e:
                raise BackendError("微信组件启动不了：%s" % e, "config")
            if restore and self._state is not None:  # a restarted program knows nothing: give it the sign-in again
                try:
                    self._call(n, "restore", 20, state=self._state)
                except BackendError:
                    n.close()
                    raise
            self._native = n  # only now: a program that could not take the sign-in is never reused
            self._ready_for = n if (restore and self._state is not None) else None
            return n

    def _mark_ready(self, n: Native) -> None:
        """n took the sign-in: only that program counts as signed in (a later one started meanwhile does not)."""
        with self._mu:
            if self._native is n:
                self._ready_for = n

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
        self._closed = True
        n = self._native
        if n is not None:
            n.close()
        self.store.close()

    # ---------------------------------------------------------------- sign-in

    def login_start(self) -> LoginQR:
        n = self._link(restore=False, wait=5.0)  # a sign-in step answers the hub within 10 s
        device = self._device or (self._state or {}).get("device")  # the same computer as before, when known
        r = self._call(n, "login_start", 9, device=device)
        self._device = r.get("device") or None
        return LoginQR(url=str(r.get("qr") or ""), state="qr")

    def login_poll(self, state: str) -> str:
        n = self._link(restore=False, wait=1.0)
        try:
            r = n.call("login_poll", timeout=9)
        except NativeGone as e:
            raise BackendError(e.message, "unreachable")
        except NativeError as e:
            if e.kind in ("login", "input"):  # this QR attempt ended, or the program lost it (restarted): a new one
                log.warning("WeChat sign-in attempt ended: %s", e.message)
                return "expired"
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
        self._mark_ready(n)
        acct = self._adopt(r.get("account") or {})
        return LoginDone(credentials={"v": 1, "state": st}, account=acct)

    def restore(self, credentials: Dict) -> Account:
        st = credentials.get("state") if isinstance(credentials, dict) else None
        if not isinstance(st, dict):
            raise CredentialsExpired(EXPIRED)
        self._state = st
        n = self._link(restore=False)
        r = self._call(n, "restore", 20, state=st)
        self._mark_ready(n)
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
        with self._mu:
            self._gen += 1
            gen = self._gen
        events = n.subscribe(gen)  # this listen's own queue: an older listen never takes its events
        try:
            self._call(n, "listen", 10, cursor=self.store.get("cursor"), gen=gen)
        except BackendError:
            n.unsubscribe(gen)
            raise
        held = False  # a live message the core did not take: the cursor stays where it was, so it comes again
        try:
            while not stop.is_set():
                if time.time() >= self._groups_check_at:  # while online too, quiet or not: every 6 hours
                    self._groups_check_at = time.time() + GROUPS_CHECK_EVERY
                    if self.me and self._groups_stale():
                        self._refresh_groups(full=True)
                try:
                    ev = events.get(timeout=1.0)
                except Exception:
                    continue
                if ev is None:
                    raise BackendError("微信组件退出了，正在重新连接", "unreachable")
                if ev.get("gen") != gen:
                    continue  # an older listen's last words (another account's, maybe): never ours to act on
                kind = ev.get("event")
                if kind == "message":
                    if not self._on_event(ev, on_message):
                        held = True
                elif kind == "cursor":
                    if not held:
                        self.store.put("cursor", str(ev.get("cursor") or ""))  # after the messages before it
                elif kind == "link":
                    on_link("up" if ev.get("up") else "down", "" if ev.get("up") else "连接断开，正在重连")
                    if ev.get("up"):
                        self._refresh_groups(full=self._groups_stale())
                elif kind == "read":  # read on the phone or another device: handled up to then
                    self._on_read(str(ev.get("chat") or ""), int(ev.get("at") or 0))
                elif kind == "contacts":  # a contact change: a group made, joined or renamed among them
                    self._refresh_groups(full=True)
                elif kind == "auth_lost":
                    raise CredentialsExpired(EXPIRED)
        finally:
            n.unsubscribe(gen)
            if n.alive():
                try:
                    n.call("stop", timeout=5, gen=gen)
                except NativeError:
                    pass

    def _on_event(self, ev: dict, on_message: OnMessage) -> bool:
        """Keep the message; give a live one to the core first (so a history read can't take it before /agent and
        自动代答 see it). False when the core could not take it."""
        mid, chat = str(ev.get("id") or ""), str(ev.get("chat") or "")
        if not mid or not chat:
            return True
        seen = self.store.seen(mid)
        if seen is not None and seen[1]:
            return True  # kept already, and handed over when it was live
        new_group = is_group(chat) and self.store.chat(chat) is None
        try:
            return self._keep_and_hand_over(ev, seen, on_message)
        finally:
            if new_group:  # made or joined just now: kept by now, its name is read in the background
                self._refresh_groups(full=False)

    def _keep_and_hand_over(self, ev: dict, seen, on_message: OnMessage) -> bool:
        mid, chat = str(ev.get("id") or ""), str(ev.get("chat") or "")
        if seen is None and ev.get("origin") != "live":
            # caught up after a reconnect, or our own send's echo: history only, to the core too (a chat new while
            # the link was down reaches the inbox), never acted on
            new, pos = self.store.add_message(ev)
            if new and self.on_history is not None:
                try:
                    self.on_history([self._message(ev, pos)])
                except Exception:
                    log.exception("the core did not keep a caught-up WeChat message")
            return True
        # live now, or live once and the core did not take it then (the core's freshness rule still applies)
        # kept first (not yet handed over: history() leaves it out), so a reply right away finds what it quotes
        pos = seen[0] if seen else self.store.add_message(ev, delivered=False)[1]
        try:
            on_message(self._message(ev, pos))
        except Exception:
            log.exception("a WeChat message could not be handled; it will come again")
            return False
        self.store.mark_delivered(mid)
        return True

    def _message(self, ev: dict, pos: int) -> Message:
        chat = str(ev.get("chat") or "")
        sender = str(ev.get("sender") or "")
        mine = bool(ev.get("mine"))
        name = self.me_name if mine else ""
        if not mine and sender:
            cached = self.store.name(self._name_key(sender, chat))
            name = cached[0] if cached else ""
        text = str(ev.get("text") or "")  # as written: a command or a mark is read from the message itself
        return Message(
            id=str(ev.get("id")), chat_id=chat, position=pos, sender_id=sender, sender_name=name, mine=mine,
            type=str(ev.get("type") or "TEXT"), text=text, at=float(ev.get("at") or 0),
            parent_id=str(ev.get("parent") or ""), at_all=bool(ev.get("at_all")),
            at_me=bool(ev.get("at_me")) and not bool(ev.get("at_all")),  # @me among @everyone is a notice
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
            members = self._roster(chat_id)[1]  # the full count, or 0 (unknown) when WeChat gave none
        if name or members:
            self.store.name_chat(chat_id, name, members)

    def _lookup_names(self, ids: List[str]) -> Dict[str, Tuple[str, str]]:
        """id → (shown name: remark or nickname, mention name: nickname) from WeChat."""
        ids = [i for i in ids if i]  # never ask WeChat about nobody (a system line has no sender)
        if not ids:
            return {}
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
                if c.get("verify_flag") is None:
                    pass  # WeChat said nothing about what it is: whatever was known stays
                elif c.get("business"):
                    BUSINESS.add(cid)
                    PERSONS.discard(cid)
                    self.store.put("biz:" + cid, "1")
                else:
                    PERSONS.add(cid)
                    self.store.put("person:" + cid, "1")
        return out

    def _roster(self, chat_id: str) -> Tuple[List[dict], int]:
        try:
            r = self._call(self._link(), "room", 20, room_id=chat_id)
        except BackendError:
            return [], 0
        members = r.get("members") or []
        for m in members:
            uid = str(m.get("id") or "")
            if not uid:
                continue
            shown = str(m.get("display") or "") or str(m.get("nickname") or "")
            if shown:
                self.store.set_name(self._name_key(uid, chat_id), shown, shown)
        return members, int(r.get("member_count") or 0)

    @staticmethod
    def _name_key(user_id: str, chat_id: Optional[str]) -> str:
        return "%s|%s" % (chat_id if chat_id and is_group(chat_id) else "", user_id)

    def user_name(self, user_id: str, chat_id: Optional[str] = None) -> str:
        if not user_id:
            return ""
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

    def _on_read(self, chat: str, at: int) -> None:
        if not chat or at <= 0:
            return
        key = "read:" + chat
        try:
            before = int(self.store.get(key) or 0)
        except ValueError:
            before = 0
        if at <= before:
            return
        self.store.put(key, str(at))
        if self.on_chats_done is not None:
            try:
                self.on_chats_done({chat: float(at)})  # a hint: the core asks chat_done_states
            except Exception:
                log.exception("the core did not take a WeChat read")

    def chat_done_states(self, chat_ids: List[str]) -> Dict[str, Optional[float]]:
        """Done (its time) when the chat was read on the phone after the last message from someone else; None only
        when a read is known and a message came after it. A chat with no read seen here is left out: not knowing is
        not "not done" (a None would undo the user's own 恢复 on the WeChat page)."""
        out: Dict[str, Optional[float]] = {}
        for cid in chat_ids:
            try:
                read = int(self.store.get("read:" + cid) or 0)
            except ValueError:
                read = 0
            if not read:
                continue
            last = self.store.last_incoming(cid)
            if not last:
                continue
            # the same second cannot tell which came first: then it still needs the user
            out[cid] = float(read) if read > last else None
        return out

    def group_candidates(self, limit: int) -> List[Chat]:
        """Every group known here: the contacts' groups and every group with a message, from the local cache."""
        out = []
        for gid, name, members in self.store.groups(limit):
            out.append(Chat(id=gid, kind="group", name=name, members=members))  # "" for an unnamed group
        return out

    def _groups_stale(self) -> bool:
        try:
            at = float(self.store.get("groups_at") or 0)
        except ValueError:
            at = 0.0
        return time.time() - at > GROUPS_EVERY

    def _refresh_groups(self, full: bool) -> None:
        """In the background, one worker at a time: read the contacts' groups (full, at most once a minute), then the
        names of the groups that have none yet. A request while it works is kept and done right after, never lost."""
        with self._groups_mu:
            if self._closed:
                return
            if self._groups_busy:
                self._groups_pending = self._groups_pending or ("full" if full else "names")
                if full:
                    self._groups_pending = "full"
                return
            self._groups_busy = True
        threading.Thread(target=self._groups_work, args=(full,), name="matewechat-groups", daemon=True).start()

    def _groups_work(self, full: bool) -> None:
        while True:
            me = self.me
            try:
                if full and time.time() - self._groups_full_at < GROUPS_SPACING:
                    time.sleep(max(0.0, GROUPS_SPACING - (time.time() - self._groups_full_at)))
                if self._closed:
                    return
                if full:
                    self._groups_full_at = time.time()
                    r = self._call(self._link(), "groups", 180)
                    ids = [str(g) for g in r.get("groups") or [] if is_group(str(g))]
                    if self.me != me or self._closed:
                        return  # another account signed in meanwhile: these groups are not its
                    new = self.store.add_groups(ids)
                    if r.get("complete"):
                        self.store.put("groups_at", str(time.time()))
                    log.info("WeChat groups: %d in the contacts of %s (%s), %d new here", len(ids),
                             r.get("contacts", "?"), "all" if r.get("complete") else "not all yet", new)
                pending = self.store.unnamed_groups(time.time() - NAME_TTL, 400)
                for i in range(0, len(pending), 2):  # two per request, as WeChat takes them
                    if self._closed or self.me != me:
                        return
                    pair = pending[i:i + 2]
                    found = self._lookup_names(pair)
                    for gid in pair:  # one WeChat would not name is tried again after NAME_TTL, the rest go on
                        self.store.set_chat_name(gid, found.get(gid, ("", ""))[0])
                    time.sleep(GROUP_NAME_PAUSE)  # gently: a person's account reading its own groups
                # direct chats kept before kinds were read: learn person / official account (once per run each)
                todo = self.store.unclassified_people(PERSONS | BUSINESS | self._kind_tried, 200)
                for i in range(0, len(todo), 2):
                    if self._closed or self.me != me:
                        return
                    pair = todo[i:i + 2]
                    self._kind_tried.update(pair)
                    self._lookup_names(pair)
                    time.sleep(GROUP_NAME_PAUSE)
            except BackendError as e:
                log.info("WeChat groups were not read now: %s", e.message)
            except Exception:
                if not self._closed:
                    log.exception("reading the WeChat groups failed")
            with self._groups_mu:
                nxt, self._groups_pending = self._groups_pending, ""
                if not nxt or self._closed:
                    self._groups_busy = False
                    return
            full = nxt == "full"

    def media(self, ref: str, max_bytes: int) -> Tuple[str, bytes]:
        raise BackendError("这一版还不能查看微信里的图片和文件", "not_found")

    # ---------------------------------------------------------------- writes

    def react(self, chat_id: str, message_id: str, reaction: str) -> Optional[str]:
        """The request was taken: the ack line, quoting the request, as the account (no @: the answer will @ them);
        its message id. Only the configured line, once per request; any other "reaction" (a Feishu code such as DONE,
        or a done reaction set to the same words) is not a WeChat message."""
        if not self.ack_text or reaction.strip() != self.ack_text:
            return None
        key = "acked:%s:%s" % (chat_id, message_id)
        if self.store.get(key):
            return None
        self.store.put(key, "1")
        return self.send(chat_id, self.ack_text, reply_to=message_id, mention=False)

    def unreact(self, chat_id: str, message_id: str, reaction: str) -> None:
        """Nothing to take back: the ack line stays (a recall works only for 2 minutes and leaves a notice)."""

    def send(self, chat_id: str, text: str, reply_to: Optional[str] = None, root: Optional[str] = None,
             mention: bool = True) -> str:
        """Text as the account into chat_id (root names no other chat on WeChat). reply_to is quoted; in a group, its
        sender is @-ed too when it is someone else (unless mention is False). Only a send that surely did not go out
        raises SendRejected."""
        if not text.strip():
            raise SendRejected("消息内容是空的")
        params: Dict[str, Any] = {"chat": chat_id, "text": text, "key": "om-" + uuid.uuid4().hex}
        got = self.store.message(reply_to) if reply_to else None
        if got is not None and got[0].get("chat") == chat_id:
            ev = got[0]
            sender = str(ev.get("sender") or "")
            mine = bool(ev.get("mine")) or sender == self.me
            name = ""
            if is_group(chat_id) and sender and not mine:
                name = self._mention_name(sender, chat_id)  # their group nickname, shown in the quote too
                if mention and (chat_id.endswith("@chatroom") or not ev.get("quotable")):
                    params["mentions"] = [{"id": sender, "name": name}]  # answering someone in a group: @ them
            if ev.get("quotable"):  # a text message: quoted (anything else gets the plain answer)
                params["quote"] = {"server_id": str(ev.get("id")), "sender": sender, "outgoing": mine,
                                   "display_name": name or (self.me_name if mine else sender),
                                   "content": str(ev.get("content") or ""), "created_at": int(ev.get("at") or 0),
                                   "msgsource": str(ev.get("msgsource") or "")}
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
            mid = str(r.get("message_id") or "")
            try:
                self._keep_own_send(chat_id, mid, text, params, int(r.get("created_at") or 0))
            except Exception:
                log.exception("a sent WeChat message could not be kept here (it went out)")
            return mid
        if status in ("rejected", "not_sent"):
            raise SendRejected(str(r.get("message") or "微信没有接受这条消息"))
        raise BackendError("发送结果不确定：%s；不会自动重发" % (r.get("message") or "微信没有确认"), "unknown")

    def _keep_own_send(self, chat_id: str, mid: str, text: str, params: Dict[str, Any], sent_at: int = 0) -> None:
        """WeChat does not echo this device's sends back: keep the sent message, and give it to the core as history
        (the inbox sees the reply, the conversation page and the knowledge base have it). Never acted on."""
        if not mid:
            return
        names = [m.get("name") for m in params.get("mentions") or [] if m.get("name")]
        shown = "".join("@%s\u2005" % n for n in names) + text
        ev = {"event": "message", "origin": "local_send", "id": mid, "chat": chat_id, "group": is_group(chat_id),
              "sender": self.me, "mine": True, "type": "TEXT", "text": shown, "at": sent_at or int(time.time()),
              "parent": str((params.get("quote") or {}).get("server_id") or ""),
              "quotable": True, "content": shown, "msgsource": ""}
        new, pos = self.store.add_message(ev)
        if new and self.on_history is not None:
            msg = self._message(ev, pos)
            threading.Thread(target=self._history_to_core, args=([msg],), name="matewechat-own-send",
                             daemon=True).start()

    def _history_to_core(self, msgs: List[Message]) -> None:
        try:
            if self.on_history is not None:
                self.on_history(msgs)
        except Exception:
            log.exception("the core did not keep a sent WeChat message")

    def create_chat(self, user_id: str) -> str:
        if not user_id or is_group(user_id):
            raise BackendError("不是一个微信用户", "not_found")
        return user_id  # a direct chat's id is the person's id
