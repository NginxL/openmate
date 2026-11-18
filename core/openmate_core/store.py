"""The extension's own store: one SQLite file (mode 0600) in its data directory.

What it keeps: chats, messages (each once per chat by id; seq is the archive cursor), a name cache, the send journal
(each write's key once: sending → done | uncertain) and the /agent run journal. Every write is one transaction, so a
crash leaves either the old or the new row, never half of one.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Dict, Iterable, List, Optional, Tuple

from .backend import Chat, Message

SCHEMA_VERSION = 1

_SCHEMA = [
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS chats (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL DEFAULT 'group',
        name TEXT NOT NULL DEFAULT '',
        peer_id TEXT NOT NULL DEFAULT '',
        topic INTEGER NOT NULL DEFAULT 0,
        members INTEGER NOT NULL DEFAULT 0,
        muted INTEGER NOT NULL DEFAULT 0,
        last_at REAL NOT NULL DEFAULT 0,
        updated REAL NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS messages (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT NOT NULL,
        id TEXT NOT NULL,
        position INTEGER NOT NULL,
        sender_id TEXT NOT NULL DEFAULT '',
        sender_name TEXT NOT NULL DEFAULT '',
        mine INTEGER NOT NULL DEFAULT 0,
        type TEXT NOT NULL DEFAULT 'TEXT',
        text TEXT NOT NULL DEFAULT '',
        at REAL NOT NULL DEFAULT 0,
        root_id TEXT NOT NULL DEFAULT '',
        thread_id TEXT NOT NULL DEFAULT '',
        parent_id TEXT NOT NULL DEFAULT '',
        at_me INTEGER NOT NULL DEFAULT 0,
        at_all INTEGER NOT NULL DEFAULT 0,
        media TEXT NOT NULL DEFAULT '[]',
        from_history INTEGER NOT NULL DEFAULT 0,
        received REAL NOT NULL DEFAULT 0,
        UNIQUE (chat_id, id))""",
    "CREATE INDEX IF NOT EXISTS messages_chat_position ON messages (chat_id, position)",
    "CREATE INDEX IF NOT EXISTS messages_chat_root ON messages (chat_id, root_id)",
    "CREATE INDEX IF NOT EXISTS messages_id ON messages (id)",
    # the account's own lines and the @me it got, per chat: 需要我 finds the last of each without a scan
    "CREATE INDEX IF NOT EXISTS messages_chat_mine ON messages (chat_id, position, seq) WHERE mine = 1",
    "CREATE INDEX IF NOT EXISTS messages_chat_at_me ON messages (chat_id, position, seq) WHERE at_me = 1 AND mine = 0",
    """CREATE TABLE IF NOT EXISTS names (
        user_id TEXT NOT NULL,
        chat_id TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL,
        updated REAL NOT NULL,
        PRIMARY KEY (user_id, chat_id))""",
    """CREATE TABLE IF NOT EXISTS sends (
        key TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        state TEXT NOT NULL,
        result TEXT NOT NULL DEFAULT '{}',
        created REAL NOT NULL,
        updated REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        chat_id TEXT NOT NULL,
        request_id TEXT NOT NULL,
        thread_key TEXT NOT NULL,
        state TEXT NOT NULL,
        created REAL NOT NULL,
        updated REAL NOT NULL,
        data TEXT NOT NULL DEFAULT '{}')""",
    "CREATE INDEX IF NOT EXISTS runs_thread ON runs (chat_id, thread_key)",
    "CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, at REAL NOT NULL)",
    # who is a program: kind "bot" (bots and AI assistants) or "user", as the service told it (Backend.people_kinds)
    "CREATE TABLE IF NOT EXISTS people (id TEXT PRIMARY KEY, kind TEXT NOT NULL, checked REAL NOT NULL)",
    # what the user marked 处理完: hidden while it is still the same (version: its last message, its update time)
    "CREATE TABLE IF NOT EXISTS dismissed (key TEXT PRIMARY KEY, version TEXT NOT NULL, at REAL NOT NULL)",
    # groups the user marked for 自动代答: an @me there is answered by the Agent as the user, with no confirmation
    "CREATE TABLE IF NOT EXISTS autoreply (chat_id TEXT PRIMARY KEY, at REAL NOT NULL)",
    # groups the account left or deleted (Backend.on_chat_gone): out of every list until a newer message brings one back
    "CREATE TABLE IF NOT EXISTS gone (chat_id TEXT PRIMARY KEY, at REAL NOT NULL)",
    # one row per guest run started (自动代答, 文档代答): the hourly and daily most count these, not the pruned runs
    "CREATE TABLE IF NOT EXISTS guest_quota (chat_id TEXT NOT NULL, kind TEXT NOT NULL, at REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS guest_quota_chat ON guest_quota (chat_id, kind, at)",
]

_MESSAGE_COLUMNS = (
    "seq, chat_id, id, position, sender_id, sender_name, mine, type, text, at, root_id, thread_id, parent_id, "
    "at_me, at_all, media, from_history, received"
)


_INSERT_COLUMNS = ("chat_id, id, position, sender_id, sender_name, mine, type, text, at, root_id, thread_id, parent_id, "
                   "at_me, at_all, media, from_history, received")


def _message_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in ("mine", "at_me", "at_all", "from_history"):
        d[k] = bool(d[k])
    try:
        d["media"] = json.loads(d["media"] or "[]")
    except ValueError:
        d["media"] = []
    return d


def _chat_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["topic"] = bool(d["topic"])
    d["muted"] = bool(d["muted"])
    return d


class Store:
    """Thread-safe: one connection behind one lock."""

    def __init__(self, path: str):
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        # Create the file 0600 before SQLite opens it: its journal files take the database file's mode.
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA foreign_keys=ON")
            for stmt in _SCHEMA:
                self._db.execute(stmt)
            self._db.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)

    def close(self) -> None:
        with self._lock:
            try:
                self._db.close()
            except sqlite3.Error:
                pass

    # ---- small helpers ----

    def _tx(self, fn):
        """fn(db) in one write transaction. Whatever fails (fn, or the COMMIT itself: SQLITE_BUSY), the transaction is
        rolled back before the error goes up, so the next write starts clean."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                out = fn(self._db)
                self._db.execute("COMMIT")
                return out
            except BaseException:
                if self._db.in_transaction:
                    try:
                        self._db.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass  # the original error says more
                raise

    def _all(self, sql: str, args: Iterable = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, tuple(args)).fetchall()

    def _one(self, sql: str, args: Iterable = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, tuple(args)).fetchone()

    # ---- meta ----

    def meta_get(self, key: str, default: str = "") -> str:
        r = self._one("SELECT value FROM meta WHERE key = ?", (key,))
        return r["value"] if r else default

    def meta_set(self, key: str, value: str) -> None:
        self._tx(lambda db: db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value)))

    def meta_delete(self, key: str) -> None:
        self._tx(lambda db: db.execute("DELETE FROM meta WHERE key = ?", (key,)))

    # ---- chats ----

    def upsert_chat(self, chat: Chat, now: Optional[float] = None) -> None:
        now = now or time.time()

        def run(db):
            db.execute(
                """INSERT INTO chats (id, kind, name, peer_id, topic, members, muted, updated)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     kind = excluded.kind,
                     name = CASE WHEN excluded.name != '' THEN excluded.name ELSE chats.name END,
                     peer_id = CASE WHEN excluded.peer_id != '' THEN excluded.peer_id ELSE chats.peer_id END,
                     topic = excluded.topic,
                     members = CASE WHEN excluded.members > 0 THEN excluded.members ELSE chats.members END,
                     muted = excluded.muted,
                     updated = excluded.updated""",
                (chat.id, chat.kind or "group", chat.name or "", chat.peer_id or "", int(bool(chat.topic)), int(chat.members or 0), int(bool(chat.muted)), now),
            )

        self._tx(run)

    def people_kinds(self, ids: Iterable[str]) -> Dict[str, str]:
        """{id: "bot" | "user"} of those of ids the service told about."""
        ids = sorted({str(i) for i in ids if i})
        out: Dict[str, str] = {}
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            rows = self._all("SELECT id, kind FROM people WHERE id IN (%s)" % ",".join("?" * len(part)), part)
            out.update({r["id"]: r["kind"] for r in rows})
        return out

    def set_people_kinds(self, kinds: Dict[str, str], now: Optional[float] = None) -> None:
        now = now or time.time()
        rows = [(str(i), k, now) for i, k in kinds.items() if i and k in ("bot", "user")]
        if rows:
            self._tx(lambda db: db.executemany(
                "INSERT INTO people (id, kind, checked) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET kind = excluded.kind, checked = excluded.checked", rows))

    def dismissed(self, keys: Iterable[str]) -> Dict[str, str]:
        """{key: version} of those of keys the user marked 处理完."""
        keys = sorted({str(k) for k in keys if k})
        out: Dict[str, str] = {}
        for i in range(0, len(keys), 500):
            part = keys[i:i + 500]
            rows = self._all("SELECT key, version FROM dismissed WHERE key IN (%s)" % ",".join("?" * len(part)), part)
            out.update({r["key"]: r["version"] for r in rows})
        return out

    def dismiss(self, key: str, version: str, now: Optional[float] = None) -> None:
        now = now or time.time()

        def run(db):
            db.execute("INSERT INTO dismissed (key, version, at) VALUES (?, ?, ?) "
                       "ON CONFLICT(key) DO UPDATE SET version = excluded.version, at = excluded.at", (key, version, now))
            db.execute("DELETE FROM dismissed WHERE at < ?", (now - 30 * 86400,))

        self._tx(run)

    def undismiss(self, key: str) -> None:
        self._tx(lambda db: db.execute("DELETE FROM dismissed WHERE key = ?", (key,)))

    def chats_of_messages(self, message_ids: Iterable[str]) -> List[str]:
        """The chats these stored messages are in."""
        ids = sorted({str(i) for i in message_ids if i})
        out: set = set()
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            rows = self._all("SELECT DISTINCT chat_id FROM messages WHERE id IN (%s)" % ",".join("?" * len(part)), part)
            out.update(r["chat_id"] for r in rows)
        return sorted(out)

    def dismiss_if(self, key: str, version: str, expected: Optional[str], now: Optional[float] = None) -> bool:
        """dismiss, only while the key's mark is still expected (None: no mark): a background writer read it before a
        slow step, and the user may have changed it meanwhile. Whether it wrote."""
        now = now or time.time()
        out = [False]

        def run(db):
            row = db.execute("SELECT version FROM dismissed WHERE key = ?", (key,)).fetchone()
            if (row[0] if row is not None else None) != expected:
                return
            db.execute("INSERT INTO dismissed (key, version, at) VALUES (?, ?, ?) "
                       "ON CONFLICT(key) DO UPDATE SET version = excluded.version, at = excluded.at", (key, version, now))
            db.execute("DELETE FROM dismissed WHERE at < ?", (now - 30 * 86400,))  # as dismiss: a month at most
            out[0] = True

        self._tx(run)
        return out[0]

    def undismiss_if(self, key: str, expected: str) -> bool:
        """undismiss, only while the key's mark is still expected. Whether it deleted."""
        out = [False]

        def run(db):
            out[0] = db.execute("DELETE FROM dismissed WHERE key = ? AND version = ?", (key, expected)).rowcount > 0

        self._tx(run)
        return out[0]

    def count_since(self, since: float) -> dict:
        """Other people's messages since then (not system lines), and in how many chats."""
        r = self._one("SELECT COUNT(*) AS n, COUNT(DISTINCT chat_id) AS c FROM messages "
                      "WHERE at >= ? AND mine = 0 AND type != 'SYSTEM'", (since,))
        return {"messages": int(r["n"] or 0), "chats": int(r["c"] or 0)} if r else {"messages": 0, "chats": 0}

    def chats_with_types(self, types, since: float, limit: int) -> List[str]:
        """Chats with a message of one of these types since then, the most recent first."""
        types = [str(t).upper() for t in types]
        if not types:
            return []
        rows = self._all(
            "SELECT chat_id, MAX(at) AS last FROM messages WHERE type IN (%s) AND at >= ? GROUP BY chat_id "
            "ORDER BY last DESC LIMIT ?" % ",".join("?" * len(types)), types + [since, limit])
        return [r["chat_id"] for r in rows]

    def empty_positions(self, types, since: float, limit: int) -> Dict[str, Tuple[int, int, int]]:
        """{chat: (lowest, highest position, how many)} of the messages of these types stored with no text since then
        (read as nothing by an older reading), the chats with the most recent first, at most limit chats."""
        types = [str(t).upper() for t in types]
        if not types:
            return {}
        rows = self._all(
            "SELECT chat_id, MIN(position) AS lo, MAX(position) AS hi, COUNT(*) AS n, MAX(at) AS last FROM messages "
            "WHERE type IN (%s) AND text = '' AND at >= ? AND chat_id NOT IN (SELECT chat_id FROM gone) "
            "GROUP BY chat_id ORDER BY last DESC LIMIT ?"
            % ",".join("?" * len(types)), types + [since, limit])
        return {r["chat_id"]: (int(r["lo"]), int(r["hi"]), int(r["n"])) for r in rows}

    def get_chat(self, chat_id: str) -> Optional[dict]:
        r = self._one("SELECT * FROM chats WHERE id = ?", (chat_id,))
        return _chat_row(r) if r else None

    def newest_position(self, chat_id: str) -> Optional[int]:
        """The position of the newest message stored of chat, None when none is."""
        rows = self._all("SELECT MAX(position) AS p FROM messages WHERE chat_id = ?", (chat_id,))
        p = rows[0]["p"] if rows else None
        return None if p is None else int(p)

    def chats_since(self, since: float, limit: int, only: Optional[Iterable[str]] = None) -> List[dict]:
        """Chats active since then (none the account left), the most recent first; with only, just those ids."""
        if only is not None:
            ids = sorted({str(i) for i in only})
            if not ids:
                return []
            rows = self._all("SELECT * FROM chats WHERE last_at >= ? AND id IN (%s) AND id NOT IN (SELECT chat_id FROM gone) "
                             "ORDER BY last_at DESC, id LIMIT ?" % ",".join("?" * len(ids)), [since] + ids + [limit])
            return [_chat_row(r) for r in rows]
        rows = self._all("SELECT * FROM chats WHERE last_at >= ? AND id NOT IN (SELECT chat_id FROM gone) "
                         "ORDER BY last_at DESC, id LIMIT ?", (since, limit))
        return [_chat_row(r) for r in rows]

    def groups_i_spoke_in(self, limit: int) -> List[dict]:
        """Group chats with the account's own messages, the one it wrote in last first (mine_at: that time)."""
        rows = self._all("SELECT c.*, MAX(m.at) AS mine_at FROM messages m JOIN chats c ON c.id = m.chat_id "
                         "WHERE m.mine = 1 AND m.type != 'SYSTEM' AND c.kind != 'dm' "
                         "AND c.id NOT IN (SELECT chat_id FROM gone) "
                         "GROUP BY m.chat_id ORDER BY mine_at DESC, c.id LIMIT ?",
                         (int(limit),))
        out = []
        for r in rows:
            d = _chat_row(r)
            d["mine_at"] = float(r["mine_at"] or 0)
            out.append(d)
        return out

    def chats_recent(self, limit: int, query: str = "") -> List[dict]:
        if query:
            rows = self._all(
                "SELECT * FROM chats WHERE (name LIKE ? OR id = ?) AND id NOT IN (SELECT chat_id FROM gone) "
                "ORDER BY last_at DESC, id LIMIT ?",
                ("%" + query.replace("%", "").replace("_", "") + "%", query, limit),
            )
        else:
            rows = self._all("SELECT * FROM chats WHERE id NOT IN (SELECT chat_id FROM gone) "
                             "ORDER BY last_at DESC, id LIMIT ?", (limit,))
        return [_chat_row(r) for r in rows]

    # ---- messages ----

    def add_messages(self, msgs: List[Message], from_history: bool, now: Optional[float] = None) -> List[dict]:
        """Store messages not stored yet (by chat and id). A later copy only renews the text and type when it reads
        otherwise (an edit, or a better reading by a newer version). Answers the new rows, and, for a live copy of a
        message a history read stored first, that row once (its first live delivery, claimed here: from_history
        turns 0), so a command or an @me read in by a catch-up before its push still reaches /agent."""
        now = now or time.time()

        def run(db):
            new = []
            for m in msgs:
                if not m.id or not m.chat_id:
                    continue
                cur = db.execute(
                    "INSERT OR IGNORE INTO messages (" + _MESSAGE_COLUMNS.replace("seq, ", "") + ") "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        m.chat_id, m.id, int(m.position), m.sender_id or "", m.sender_name or "", int(bool(m.mine)),
                        (m.type or "TEXT").upper(), m.text or "", float(m.at or 0), m.root_id or "", m.thread_id or "",
                        m.parent_id or "", int(bool(m.at_me)), int(bool(m.at_all)),
                        json.dumps([x.to_dict() for x in (m.media or [])], ensure_ascii=False), int(bool(from_history)), now,
                    ),
                )
                if cur.rowcount != 1:
                    blank = db.execute("SELECT " + _INSERT_COLUMNS + " FROM messages WHERE chat_id = ? AND id = ? "
                                       "AND text = ''", (m.chat_id, m.id)).fetchone() if m.text else None
                    if blank is not None:
                        # stored with no text by an older reading: moved to the end (a new seq), so the archive's
                        # cursor, already past it, hands it over with its text now
                        db.execute("DELETE FROM messages WHERE chat_id = ? AND id = ?", (m.chat_id, m.id))
                        db.execute("INSERT INTO messages (" + _INSERT_COLUMNS + ") VALUES (%s)"
                                   % ",".join("?" * len(tuple(blank))), tuple(blank))
                    if m.text:
                        db.execute("UPDATE messages SET text = ?, type = ? WHERE chat_id = ? AND id = ? AND text != ?",
                                   (m.text, (m.type or "TEXT").upper(), m.chat_id, m.id, m.text))
                    if m.at_me or m.at_all:  # a history read may not know whom a message mentions (Feishu)
                        db.execute("UPDATE messages SET at_me = MAX(at_me, ?), at_all = MAX(at_all, ?) "
                                   "WHERE chat_id = ? AND id = ?",
                                   (int(bool(m.at_me)), int(bool(m.at_all)), m.chat_id, m.id))
                    if not from_history and db.execute(
                            "UPDATE messages SET from_history = 0 WHERE chat_id = ? AND id = ? AND from_history = 1",
                            (m.chat_id, m.id)).rowcount == 1:
                        r = db.execute("SELECT " + _MESSAGE_COLUMNS + " FROM messages WHERE chat_id = ? AND id = ?",
                                       (m.chat_id, m.id)).fetchone()
                        if r is not None:
                            new.append(_message_row(r))  # its first live delivery
                    continue
                if (m.type or "").upper() != "SYSTEM":
                    db.execute(
                        "INSERT INTO chats (id, updated, last_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(id) DO UPDATE SET last_at = MAX(chats.last_at, excluded.last_at)",
                        (m.chat_id, now, float(m.at or 0)),
                    )
                r = db.execute("SELECT " + _MESSAGE_COLUMNS + " FROM messages WHERE seq = ?", (cur.lastrowid,)).fetchone()
                new.append(_message_row(r))
            return new

        return self._tx(run)

    def message(self, chat_id: str, msg_id: str) -> Optional[dict]:
        r = self._one("SELECT " + _MESSAGE_COLUMNS + " FROM messages WHERE chat_id = ? AND id = ?", (chat_id, msg_id))
        return _message_row(r) if r else None

    def find_message(self, msg_id: str) -> Optional[dict]:
        r = self._one("SELECT " + _MESSAGE_COLUMNS + " FROM messages WHERE id = ? ORDER BY seq LIMIT 1", (msg_id,))
        return _message_row(r) if r else None

    def messages_before(self, chat_id: str, before_position: Optional[int], limit: int) -> List[dict]:
        """Up to limit messages of chat before before_position (the newest when None), oldest first."""
        if before_position is None:
            rows = self._all(
                "SELECT " + _MESSAGE_COLUMNS + " FROM messages WHERE chat_id = ? ORDER BY position DESC, seq DESC LIMIT ?",
                (chat_id, limit),
            )
        else:
            rows = self._all(
                "SELECT " + _MESSAGE_COLUMNS + " FROM messages WHERE chat_id = ? AND position < ? ORDER BY position DESC, seq DESC LIMIT ?",
                (chat_id, int(before_position), limit),
            )
        return [_message_row(r) for r in reversed(rows)]

    def thread_messages(self, chat_id: str, root_id: str, limit: int) -> List[dict]:
        """The thread rooted at root_id (its root first), the newest limit messages, oldest first."""
        rows = self._all(
            "SELECT " + _MESSAGE_COLUMNS + " FROM messages WHERE chat_id = ? AND (root_id = ? OR id = ?) "
            "ORDER BY position DESC, seq DESC LIMIT ?",
            (chat_id, root_id, root_id, limit),
        )
        return [_message_row(r) for r in reversed(rows)]

    def inbox_rows(self, chat_id: str, limit: int = 60) -> List[dict]:
        """The chat's last limit messages (oldest first), and before them its newest @me the account has not answered
        when later chatter pushed it out of them: a mention stays a mention however busy the group is."""
        rows = self.messages_before(chat_id, None, limit)
        if not rows or len(rows) < limit or any(r.get("mine") and r.get("type") != "SYSTEM" for r in rows):
            return rows  # the whole chat, or the account spoke in it: the window says it all

        def order(r) -> tuple:  # a place in the chat: its position, then when (a thread reply shares a position)
            return (int(r["position"]), float(r["at"] or 0), int(r["seq"]))

        first = min(rows, key=order)
        mine = self._one("SELECT position, at, seq FROM messages INDEXED BY messages_chat_mine WHERE chat_id = ? "
                         "AND mine = 1 AND type != 'SYSTEM' ORDER BY position DESC, seq DESC LIMIT 1", (chat_id,))
        lo = order(mine) if mine is not None else (-1, -1.0, -1)
        found = self._all(
            "SELECT " + _MESSAGE_COLUMNS + " FROM messages INDEXED BY messages_chat_at_me WHERE chat_id = ? "
            "AND at_me = 1 AND mine = 0 AND position <= ? AND position >= ? ORDER BY position DESC, seq DESC LIMIT 20",
            (chat_id, int(first["position"]), max(lo[0], 0)))
        cands = [r for r in found if lo < order(r) < order(first)]
        if not cands:
            return rows
        return [_message_row(max(cands, key=order))] + rows

    def messages_after(self, chat_id: str, position: int, limit: int) -> List[dict]:
        rows = self._all(
            "SELECT " + _MESSAGE_COLUMNS + " FROM messages WHERE chat_id = ? AND position > ? ORDER BY position, seq LIMIT ?",
            (chat_id, int(position), limit),
        )
        return [_message_row(r) for r in rows]

    def archive_rows(self, after_seq: int, limit: int) -> List[dict]:
        rows = self._all("SELECT " + _MESSAGE_COLUMNS + " FROM messages WHERE seq > ? ORDER BY seq LIMIT ?", (after_seq, limit))
        return [_message_row(r) for r in rows]

    def message_count(self) -> int:
        r = self._one("SELECT COUNT(*) AS n FROM messages")
        return int(r["n"]) if r else 0

    # ---- names ----

    def put_name(self, user_id: str, name: str, chat_id: str = "", now: Optional[float] = None) -> None:
        if not user_id or not name:
            return
        now = now or time.time()
        self._tx(
            lambda db: db.execute(
                "INSERT INTO names (user_id, chat_id, name, updated) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id, chat_id) DO UPDATE SET name = excluded.name, updated = excluded.updated",
                (user_id, chat_id or "", name, now),
            )
        )

    def name(self, user_id: str, chat_id: str = "") -> str:
        if not user_id:
            return ""
        if chat_id:
            r = self._one("SELECT name FROM names WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))
            if r:
                return r["name"]
        r = self._one("SELECT name FROM names WHERE user_id = ? ORDER BY chat_id = '' DESC, updated DESC LIMIT 1", (user_id,))
        return r["name"] if r else ""

    # ---- the send journal ----

    def send_get(self, key: str) -> Optional[dict]:
        r = self._one("SELECT * FROM sends WHERE key = ?", (key,))
        if not r:
            return None
        d = dict(r)
        try:
            d["result"] = json.loads(d["result"] or "{}")
        except ValueError:
            d["result"] = {}
        return d

    def send_claim(self, key: str, kind: str, now: Optional[float] = None) -> bool:
        """Claim key for one send (state sending). False: someone has it already."""
        now = now or time.time()
        return self._tx(
            lambda db: db.execute(
                "INSERT OR IGNORE INTO sends (key, kind, state, result, created, updated) VALUES (?, ?, 'sending', '{}', ?, ?)",
                (key, kind, now, now),
            ).rowcount
            == 1
        )

    def send_finish(self, key: str, state: str, result: Dict, now: Optional[float] = None) -> None:
        now = now or time.time()
        self._tx(
            lambda db: db.execute(
                "UPDATE sends SET state = ?, result = ?, updated = ? WHERE key = ?",
                (state, json.dumps(result, ensure_ascii=False), now, key),
            )
        )

    def send_release(self, key: str) -> None:
        """Drop a claim whose send surely did not go out: the same key may try again."""
        self._tx(lambda db: db.execute("DELETE FROM sends WHERE key = ? AND state = 'sending'", (key,)))

    def sends_interrupted(self, now: Optional[float] = None) -> int:
        """At start: a send still 'sending' was cut off by a crash; it may have gone out."""
        now = now or time.time()
        return self._tx(lambda db: db.execute("UPDATE sends SET state = 'uncertain', updated = ? WHERE state = 'sending'", (now,)).rowcount)

    def sends_prune(self, before: float) -> None:
        self._tx(lambda db: db.execute("DELETE FROM sends WHERE updated < ? AND state != 'sending'", (before,)))

    # ---- /agent runs ----

    def run_put(self, run: dict, now: Optional[float] = None) -> None:
        now = now or time.time()
        run["updated"] = now
        data = {k: v for k, v in run.items() if k not in ("id", "chat_id", "request_id", "thread_key", "state", "created", "updated")}
        self._tx(
            lambda db: db.execute(
                """INSERT INTO runs (id, chat_id, request_id, thread_key, state, created, updated, data)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET state = excluded.state, updated = excluded.updated, data = excluded.data,
                     thread_key = excluded.thread_key""",
                (run["id"], run["chat_id"], run["request_id"], run["thread_key"], run["state"], run["created"], now,
                 json.dumps(data, ensure_ascii=False)),
            )
        )

    @staticmethod
    def _run_row(r: sqlite3.Row) -> dict:
        d = dict(r)
        try:
            data = json.loads(d.pop("data") or "{}")
        except ValueError:
            data = {}
        data.update(d)
        return data

    def run_get(self, run_id: str) -> Optional[dict]:
        r = self._one("SELECT * FROM runs WHERE id = ?", (run_id,))
        return self._run_row(r) if r else None

    def runs_in_state(self, states: Iterable[str]) -> List[dict]:
        states = list(states)
        rows = self._all("SELECT * FROM runs WHERE state IN (%s) ORDER BY created" % ",".join("?" * len(states)), states)
        return [self._run_row(r) for r in rows]

    def runs_in_thread(self, chat_id: str, thread_key: str) -> List[dict]:
        rows = self._all("SELECT * FROM runs WHERE chat_id = ? AND thread_key = ? ORDER BY created DESC", (chat_id, thread_key))
        return [self._run_row(r) for r in rows]

    def runs_in_chat(self, chat_id: str, limit: int = 50) -> List[dict]:
        rows = self._all("SELECT * FROM runs WHERE chat_id = ? ORDER BY created DESC LIMIT ?", (chat_id, limit))
        return [self._run_row(r) for r in rows]

    def runs_since(self, since: float, limit: int = 50) -> List[dict]:
        rows = self._all("SELECT * FROM runs WHERE created >= ? ORDER BY created DESC LIMIT ?", (since, limit))
        return [self._run_row(r) for r in rows]

    def chat_gone(self, chat_id: str, now: Optional[float] = None) -> None:
        """The account left or deleted this chat: out of the lists, and no longer marked for 自动代答."""
        at = now or time.time()

        def run(db):
            # told again (at a start): the first time stays, so a message after it still brings the chat back
            db.execute("INSERT OR IGNORE INTO gone (chat_id, at) VALUES (?, ?)", (chat_id, at))
            db.execute("DELETE FROM autoreply WHERE chat_id = ?", (chat_id,))

        self._tx(run)

    def chats_back(self, latest: Dict[str, float]) -> int:
        """Chats with a message newer than when they went (joined again): back in the lists. How many came back."""
        gone = {r["chat_id"] for r in self._all("SELECT chat_id FROM gone")} if latest else set()
        latest = {c: at for c, at in latest.items() if c in gone}  # nearly always none: no write then
        if not latest:
            return 0
        n = [0]

        def run(db):
            for cid, at in latest.items():
                n[0] += db.execute("DELETE FROM gone WHERE chat_id = ? AND at < ?", (cid, at)).rowcount

        self._tx(run)
        return n[0]

    def autoreply_chats(self) -> List[str]:
        return [r["chat_id"] for r in self._all("SELECT chat_id FROM autoreply ORDER BY at")]

    def autoreply_set(self, chat_id: str, on: bool, now: Optional[float] = None) -> None:
        if on:
            self._tx(lambda db: db.execute("INSERT OR IGNORE INTO autoreply (chat_id, at) VALUES (?, ?)", (chat_id, now or time.time())))
        else:
            self._tx(lambda db: db.execute("DELETE FROM autoreply WHERE chat_id = ?", (chat_id,)))

    def runs_prune(self, before: float, keep: int) -> None:
        def run(db):
            db.execute("DELETE FROM runs WHERE created < ? AND state NOT IN ('starting', 'running')", (before,))
            db.execute(
                "DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY created DESC LIMIT ?) "
                "AND state NOT IN ('starting', 'running')",
                (keep,),
            )

        self._tx(run)

    def run_count(self) -> int:
        r = self._one("SELECT COUNT(*) AS n FROM runs")
        return int(r["n"]) if r else 0

    # ---- handled-once keys ----

    def seen_add(self, key: str, now: Optional[float] = None) -> bool:
        """Record key; False when it was recorded already (the thing was handled)."""
        now = now or time.time()
        return self._tx(lambda db: db.execute("INSERT OR IGNORE INTO seen (key, at) VALUES (?, ?)", (key, now)).rowcount == 1)

    def seen_prune(self, before: float) -> None:
        self._tx(lambda db: db.execute("DELETE FROM seen WHERE at < ?", (before,)))

    # ---- another account ----

    def quota_backfill(self) -> None:
        """Once: the guest runs still kept count in the new ledger (so an upgrade never resets a group's quota)."""
        def run(db):
            if db.execute("SELECT 1 FROM meta WHERE key = 'guest_quota_backfilled'").fetchone():
                return
            since = time.time() - 3 * 86400
            for r in db.execute("SELECT chat_id, created, data FROM runs WHERE created >= ?", (since,)).fetchall():
                try:
                    kind = json.loads(r["data"] or "{}").get("kind")
                except ValueError:
                    kind = None
                if kind in ("auto", "doc"):
                    db.execute("INSERT INTO guest_quota (chat_id, kind, at) VALUES (?, ?, ?)",
                               (r["chat_id"], kind, r["created"]))
            db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('guest_quota_backfilled', '1')")

        self._tx(run)

    def quota_add(self, chat_id: str, kind: str, at: float) -> None:
        def run(db):
            db.execute("INSERT INTO guest_quota (chat_id, kind, at) VALUES (?, ?, ?)", (chat_id, kind, at))
            db.execute("DELETE FROM guest_quota WHERE at < ?", (at - 3 * 86400,))

        self._tx(run)

    def quota_count(self, chat_id: str, kind: str, since: float) -> int:
        r = self._one("SELECT COUNT(*) AS n FROM guest_quota WHERE chat_id = ? AND kind = ? AND at >= ?",
                      (chat_id, kind, since))
        return int(r["n"]) if r else 0

    def wipe_account_data(self) -> None:
        """Forget the chats of the account signed in before (another one signed in). Message seqs keep growing, so
        the hub's archive cursor stays valid. The send journal stays: request ids are the hub's."""

        def run(db):
            # the groups marked for 自动代答 and who is a person were the old account's choices and knowledge
            for table in ("chats", "messages", "names", "runs", "seen", "autoreply", "people", "guest_quota", "gone"):
                db.execute("DELETE FROM " + table)
            db.execute("DELETE FROM dismissed WHERE key LIKE 'chat:%'")  # its chats' 处理完 (and 恢复) marks
            db.execute("DELETE FROM meta WHERE key IN ('initial_history_done')")

        self._tx(run)
