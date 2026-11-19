"""A message the service keeps from this client (Message.blocked, Feishu's data-loss policy): its text is only a note,
shown in the chat and never handed to the knowledge base, by archive.changes or archive.history, whether the backend
could be made or not. The mark is the message's own, not its text: a message that says the note's words is archived."""

import os
import shutil
import sqlite3
import tempfile
import time
import unittest

from harness import Harness, wait_until
from openmate_core.backend import BackendError, Message
from openmate_core import store as store_module
from openmate_core.store import Store

import openmate_feishu

# the note the Feishu backend gives a blocked message, as older versions stored it (the migration looks for it)
DLP_BLOCKED_TEXT = "[飞书的安全策略拦下了这条消息，内容看不到]"

CHAT = "chat_example_team"


def line(mid: str, text: str, ago: float, blocked: bool = False) -> Message:
    m = Message(id=mid, chat_id=CHAT, position=int(1000 - ago), sender_id="user_example_b", sender_name="示例同事乙",
                text=text, at=time.time() - ago)
    m.blocked = blocked  # set after: the mark is the message's own
    return m


def archived(h) -> list:
    """Every line archive.changes hands over, from the start."""
    out, cursor = [], ""
    for _ in range(50):
        page = h.c("archive.changes", cursor=cursor, limit=100)
        out += [m for b in page["batches"] for m in b["messages"]]
        cursor = page.get("next") or cursor
        if not page.get("more"):
            return out
    raise AssertionError("archive.changes never ended")


def imported(h, chat=CHAT) -> list:
    """Every line archive.history hands over for chat, newest page first."""
    out, q = [], {"limit": 50}
    for _ in range(50):
        page = h.c("archive.history", chat=chat, **q)
        out = (page.get("messages") or []) + out
        if not page.get("more"):
            return out
        q = {"limit": 50, "before": page["older"]}
    raise AssertionError("archive.history never ended")


def no_backend(cfg, data_dir):
    raise BackendError("还没有设置 LarkAgentX 的 Python（larkx_python）", "config")


class BlockedTest(unittest.TestCase):
    def store_lines(self, h):
        h.app.store.add_messages([line("msg_blocked", DLP_BLOCKED_TEXT, 300, blocked=True),
                                  line("msg_says_the_note", DLP_BLOCKED_TEXT, 200),  # someone pasted the note's words
                                  line("msg_plain", "普通的一条", 100)], from_history=True)

    def check(self, h, rows):
        ids = [m["id"] for m in rows]
        self.assertNotIn("msg_blocked", ids, "a blocked message is never the knowledge base's")
        self.assertIn("msg_says_the_note", ids, "a message that only says the note's words is archived")
        self.assertIn("msg_plain", ids)
        self.assertEqual([m["text"] for m in rows if m["id"] == "msg_says_the_note"], [DLP_BLOCKED_TEXT])

    def signed_in(self):
        h = Harness(self)
        h.sign_in()
        wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
        h.app.backend.unarchived_texts = (DLP_BLOCKED_TEXT,)  # what the Feishu backend told before the mark
        return h

    def test_the_platform_names_the_old_note(self):
        self.assertEqual(getattr(openmate_feishu.platform(), "legacy_blocked_texts", ()), (DLP_BLOCKED_TEXT,))

    def test_the_store_keeps_the_mark(self):
        h = self.signed_in()
        self.store_lines(h)
        self.assertTrue(h.app.store.message(CHAT, "msg_blocked").get("blocked"))
        self.assertFalse(h.app.store.message(CHAT, "msg_says_the_note").get("blocked"))

    def test_archive_changes_goes_by_the_mark(self):
        h = self.signed_in()
        self.store_lines(h)
        self.check(h, archived(h))

    def test_archive_history_goes_by_the_mark(self):
        h = self.signed_in()
        self.store_lines(h)
        self.check(h, imported(h))

    def test_without_a_backend_too(self):
        h = Harness(self, backend_factory=no_backend)
        self.assertIsNone(h.app.backend)
        self.store_lines(h)
        self.check(h, archived(h))
        self.check(h, imported(h))

    def test_a_later_reading_never_unmarks_it(self):
        h = self.signed_in()
        store = h.app.store
        store.add_messages([line("msg_held", DLP_BLOCKED_TEXT, 300, blocked=True)], from_history=True)
        store.add_messages([line("msg_held", "[不支持的消息：TEXT]", 300)], from_history=False)  # a decode failure
        row = store.message(CHAT, "msg_held")
        self.assertEqual((row["text"], row["blocked"]), (DLP_BLOCKED_TEXT, True))
        for rows in (archived(h), imported(h)):
            self.assertNotIn("msg_held", [m["id"] for m in rows])
        store.add_messages([line("msg_plain_later", "普通", 250)], from_history=True)
        store.add_messages([line("msg_plain_later", DLP_BLOCKED_TEXT, 250, blocked=True)], from_history=True)
        self.assertTrue(store.message(CHAT, "msg_plain_later")["blocked"], "a reading that is blocked marks it")
        store.add_messages([line("msg_plain_bare", "普通", 240)], from_history=True)
        store.add_messages([line("msg_plain_bare", "", 240, blocked=True)], from_history=True)
        self.assertTrue(store.message(CHAT, "msg_plain_bare")["blocked"], "even one with no text")
        for rows in (archived(h), imported(h)):
            self.assertFalse({"msg_plain_later", "msg_plain_bare"} & {m["id"] for m in rows})

    def test_a_blocked_line_at_a_position_does_not_move_the_history_bound(self):
        h = self.signed_in()
        now = time.time()

        def at(mid, pos, ago, blocked=False):
            m = Message(id=mid, chat_id=CHAT, position=pos, sender_id="user_example_b", text="第 %s 条" % mid, at=now - ago)
            if blocked:
                m.text, m.blocked = DLP_BLOCKED_TEXT, True
            return m

        # a topic reply shares its root's position and comes later than the line after it
        h.app.store.add_messages([at("p7", 7007, 30), at("p8", 7008, 20), at("p7_reply", 7007, 10, blocked=True)],
                                 from_history=True)
        page = h.c("archive.history", chat=CHAT, after="7007", limit=10)
        self.assertEqual([m["id"] for m in page.get("messages") or []], ["p8"])


class MigrationTest(unittest.TestCase):
    def old_store(self, d: str) -> str:
        """openmate.db as a version before messages.blocked left it: a blocked line, and a plain one."""
        path = os.path.join(d, "openmate.db")
        db = sqlite3.connect(path)
        for stmt in store_module._SCHEMA:  # without the column (no DROP COLUMN: SQLite before 3.35 has none)
            db.execute(stmt.replace("blocked INTEGER NOT NULL DEFAULT 0,", ""))
        self.assertNotIn("blocked", [r[1] for r in db.execute("PRAGMA table_info(messages)")])
        now = time.time()
        for mid, text, at in (("msg_old_blocked", DLP_BLOCKED_TEXT, now - 300), ("msg_old_plain", "旧的普通消息", now - 200)):
            db.execute("INSERT INTO messages (chat_id, id, position, sender_id, text, at, received) "
                       "VALUES (?, ?, ?, 'user_example_b', ?, ?, ?)", (CHAT, mid, int(at), text, at, at))
            db.execute("INSERT OR IGNORE INTO chats (id, kind, last_at) VALUES (?, 'group', ?)", (CHAT, at))
        db.commit()
        db.close()
        return path

    def test_an_old_stores_blocked_lines_are_marked(self):
        d = tempfile.mkdtemp(prefix="openmate-migrate-")
        self.addCleanup(shutil.rmtree, d, True)
        self.old_store(d)
        h = Harness(self, data_dir=d, backend_factory=no_backend)
        self.assertTrue(h.app.store.message(CHAT, "msg_old_blocked")["blocked"])
        self.assertFalse(h.app.store.message(CHAT, "msg_old_plain")["blocked"])
        for rows in (archived(h), imported(h)):
            self.assertEqual([m["id"] for m in rows if m["id"].startswith("msg_old_")], ["msg_old_plain"])

    def test_the_old_texts_mark_only_once(self):
        d = tempfile.mkdtemp(prefix="openmate-migrate-")
        self.addCleanup(shutil.rmtree, d, True)
        path = self.old_store(d)
        st = Store(path, legacy_blocked_texts=(DLP_BLOCKED_TEXT,))
        st.add_messages([line("msg_new_says_the_note", DLP_BLOCKED_TEXT, 50)], from_history=False)
        st.close()
        st = Store(path, legacy_blocked_texts=(DLP_BLOCKED_TEXT,))  # opened again: no second pass over the texts
        self.addCleanup(st.close)
        self.assertTrue(st.message(CHAT, "msg_old_blocked")["blocked"])
        self.assertFalse(st.message(CHAT, "msg_new_says_the_note")["blocked"])

    def test_a_rollback_and_upgrade_marks_again(self):
        d = tempfile.mkdtemp(prefix="openmate-migrate-")
        self.addCleanup(shutil.rmtree, d, True)
        path = self.old_store(d)
        Store(path, legacy_blocked_texts=(DLP_BLOCKED_TEXT,)).close()
        db = sqlite3.connect(path)  # an older version opens it again: user_version 1, and stores a blocked line
        db.execute("PRAGMA user_version=1")
        db.execute("INSERT INTO messages (chat_id, id, position, text, at) VALUES (?, 'msg_rolled_back', 5, ?, 5)",
                   (CHAT, DLP_BLOCKED_TEXT))
        db.commit()
        db.close()
        st = Store(path, legacy_blocked_texts=(DLP_BLOCKED_TEXT,))
        self.addCleanup(st.close)
        self.assertTrue(st.message(CHAT, "msg_rolled_back")["blocked"])


if __name__ == "__main__":
    unittest.main()
