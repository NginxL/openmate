"""history.chats / archive.history: the knowledge base's per-chat settings and history import, read from the store."""

import os
import sys
import time
import unittest
from datetime import datetime

from harness import ROOT, Harness, HubSideError, wait_until
from openmate_core.backend import Chat, Message
from openmate_core.store import history_sql

BIG = "chat_example_history"


def when(m: dict) -> float:
    return datetime.fromisoformat(m["at"].replace("Z", "+00:00")).timestamp()


def page_ok(test, q: dict, page: dict) -> None:
    """The protocol's rules for a page (Persona's connector conformance, checkHistoryPage), and what Persona decodes."""
    msgs = page.get("messages") or []
    if page.get("more"):
        test.assertTrue(page.get("older"), "more without an older cursor")
        test.assertTrue(msgs, "more on a page with no messages")
    else:
        test.assertNotIn("older", page, "an older cursor without more")
    if page.get("more") and q.get("before"):
        test.assertNotEqual(page["older"], q["before"], "the same page would be read again")
    ids = [m["id"] for m in msgs]
    test.assertTrue(all(ids), "a message without an id")
    test.assertEqual(len(ids), len(set(ids)), "a message twice on a page")
    test.assertTrue(all(m["at"] for m in msgs), "a message without a time")
    ats = [when(m) for m in msgs]
    test.assertEqual(ats, sorted(ats), "oldest first")


def cursor(store, chat_id: str, msg_id: str) -> str:
    r = store.message(chat_id, msg_id)
    return "%r:%d" % (float(r["at"] or r["received"]), r["seq"])


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self)
        self.h.world.add_chat(Chat(BIG, "group", "示例历史群", members=30))
        self.h.sign_in()
        wait_until(lambda: self.h.app._catchup_done.is_set(), what="catch-up")
        # 300 lines straight into the store: every 7th a system line, every 11th empty (read as nothing)
        self.now = now = time.time()
        rows = []
        for i in range(1, 301):
            kind = "SYSTEM" if i % 7 == 0 else "TEXT"
            text = "" if i % 11 == 0 else "第 %d 条" % i
            rows.append(Message(id="msg_hist_%d" % i, chat_id=BIG, position=i, sender_id="user_example_a",
                                sender_name="示例同事甲", text=text, type=kind, at=now - 10000 + i))
        self.h.app.store.add_messages(rows, from_history=True)
        self.content = ["msg_hist_%d" % i for i in range(1, 301) if i % 7 and i % 11]

    def read(self, chat=BIG, **q):
        page = self.h.c("archive.history", chat=chat, **q)
        page_ok(self, q, page)
        return page

    def back(self, chat=BIG, limit=40):
        """Every line, reading back from the newest; pages must go back in time without overlapping."""
        seen, q, last = [], {"limit": limit}, None
        for _ in range(400):
            page = self.read(chat, **q)
            msgs = page.get("messages") or []
            self.assertLessEqual(len(msgs), limit)
            if msgs and last:
                self.assertLessEqual(when(msgs[-1]), when(last[0]), "a page newer than the one read before it")
            last = msgs or last
            seen = [m["id"] for m in msgs] + seen
            if not page.get("more"):
                return seen
            q = {"limit": limit, "before": page["older"]}
        self.fail("never reached the start")

    def forward(self, chat=BIG, limit=50, after="0.0:0"):
        seen, q = [], {"limit": limit, "after": after}
        for _ in range(400):
            page = self.read(chat, **q)
            got = [m["id"] for m in page.get("messages") or []]
            seen += got
            if not page.get("newer"):
                return seen
            self.assertTrue(got, "newer on an empty page")
            q = {"limit": limit, "after": page["newer"]}
        self.fail("never reached the newest")

    def test_chats_lists_what_is_stored(self):
        listed = self.h.c("history.chats")["chats"]
        self.assertEqual(len(listed), len({c["id"] for c in listed}), "a chat listed twice")
        chats = {c["id"]: c for c in listed}
        self.assertEqual(chats[BIG]["name"], "示例历史群")
        self.assertEqual(chats[BIG]["kind"], "group")
        self.assertEqual(chats[BIG]["estimate"], 300 - 300 // 7)  # system lines not counted
        self.assertEqual(chats["chat_example_dm_a"]["kind"], "dm")
        for c in chats.values():
            self.assertTrue(c["id"] and c["name"])
            self.assertIn(c["kind"], ("dm", "group"))
        self.h.app.store.chat_gone("chat_example_chatter")
        self.assertNotIn("chat_example_chatter", [c["id"] for c in self.h.c("history.chats")["chats"]])

    def test_paging_back_and_forward_read_everything_once(self):
        self.assertEqual(self.back(), self.content)
        self.assertEqual(self.forward(), self.content)
        for limit in (1, 3, 7):
            self.assertEqual(self.back(limit=limit), self.content, "limit %d" % limit)

    def test_order_is_time_not_position(self):
        # a Feishu topic reply can share its group's position; one filled in late can be much older than its
        # neighbours. Persona stops importing at the first line past its cutoff, so a page must never put an old line
        # among new ones: 50 new lines, then an August reply on the newest position.
        rows = [Message(id="msg_new_%d" % i, chat_id=BIG, position=400 + i, sender_id="user_example_b",
                        text="新 %d" % i, at=self.now - 50 + i) for i in range(50)]
        rows.append(Message(id="msg_old_reply", chat_id=BIG, position=449, sender_id="user_example_b", text="八月的回复",
                            at=self.now - 100 * 86400))
        self.h.app.store.add_messages(rows, from_history=True)
        newest = [m["id"] for m in self.read(limit=50)["messages"]]
        self.assertNotIn("msg_old_reply", newest)
        self.assertEqual(newest[0], "msg_new_0")
        everything = self.back(limit=50)
        self.assertEqual(everything[0], "msg_old_reply")  # read last, as the oldest
        self.assertEqual(sorted(everything), sorted(self.content + [r.id for r in rows]))

    def test_lines_sharing_a_position_and_a_time(self):
        rows = [Message(id="msg_same_%d" % i, chat_id=BIG, position=500, sender_id="user_example_b",
                        text="同位 %d" % i, at=self.now - 5) for i in range(9)]
        self.h.app.store.add_messages(rows, from_history=False)
        want = self.content + ["msg_same_%d" % i for i in range(9)]
        for limit in (1, 2, 4):
            self.assertEqual(self.back(limit=limit), want, "limit %d back" % limit)
            self.assertEqual(self.forward(limit=limit), want, "limit %d forward" % limit)

    def test_around_holds_the_line_at_the_cursor(self):
        at = cursor(self.h.app.store, BIG, "msg_hist_100")
        for limit in (1, 2, 10):
            ids = [m["id"] for m in self.read(around=at, limit=limit)["messages"]]
            self.assertIn("msg_hist_100", ids, "limit %d" % limit)
            self.assertEqual(len(ids), limit)
        page = self.read(around=at, limit=10)
        self.assertTrue(page["more"])
        self.assertTrue(page["newer"])
        # a page's own older cursor, handed back as around, reads that line again
        older = self.read(limit=2)["older"]
        self.assertIn(self.content[-2], [m["id"] for m in self.read(around=older, limit=2)["messages"]])

    def test_cursors_never_lead_to_an_empty_page(self):
        now = time.time()
        self.h.app.store.upsert_chat(Chat("chat_example_edge", "group", "示例边界群"))
        rows = [Message(id="msg_edge_1", chat_id="chat_example_edge", position=1, sender_id="user_example_a", text="",
                        at=now - 9),
                Message(id="msg_edge_2", chat_id="chat_example_edge", position=2, sender_id="user_example_a",
                        text="唯一一条", at=now - 8),
                Message(id="msg_edge_3", chat_id="chat_example_edge", position=3, sender_id="user_example_a",
                        text="示例同事丙 加入了群聊", type="SYSTEM", at=now - 7),
                Message(id="msg_edge_4", chat_id="chat_example_edge", position=4, sender_id="user_example_a",
                        text=" 　 ", at=now - 6)]  # only blanks: archive.changes leaves it out, so here too
        self.h.app.store.add_messages(rows, from_history=False)
        page = self.read("chat_example_edge", limit=1)
        self.assertEqual([m["id"] for m in page["messages"]], ["msg_edge_2"])
        self.assertNotIn("more", page)
        self.assertNotIn("newer", page)

    def test_a_missing_time_reads_as_when_it_came_in(self):
        self.h.app.store.add_messages([Message(id="msg_no_time", chat_id=BIG, position=900, sender_id="user_example_a",
                                               text="没有时间", at=0)], from_history=False)
        page = self.read(limit=1)
        self.assertEqual(page["messages"][0]["id"], "msg_no_time")
        self.assertGreater(when(page["messages"][0]), self.now - 60)

    def test_messages_have_the_archive_shape(self):
        page = self.read(limit=3)
        m = page["messages"][-1]
        self.assertEqual(m["id"], "msg_hist_300")
        self.assertEqual(m["text"], "第 300 条")
        self.assertEqual(m["position"], "300")
        self.assertEqual(m["sender_name"], "示例同事甲")
        self.assertEqual(m["sender_handles"], [{"kind": "lark", "value": "user_example_a"}])
        self.assertNotIn("newer", page)  # the newest page

    def test_limit_is_a_hint_held_to_200(self):
        self.assertEqual(len(self.read(limit=1000)["messages"]), 200)
        self.assertEqual(len(self.read()["messages"]), 50)

    def test_unreadable_and_mistakes(self):
        self.assertEqual(self.h.c("archive.history", chat="chat_nope"), {"unreadable": True})
        with self.assertRaises(HubSideError):
            self.h.c("archive.history", chat="chat_example_team", before="1.0:3", after="1.0:1")
        for bad in ("not-ours", "12", "x:1", "1.0:9223372036854775808", "nan:1", "inf:1", "1.0:"):
            with self.assertRaises(HubSideError, msg=bad):
                self.h.c("archive.history", chat="chat_example_team", before=bad)
        with self.assertRaises(HubSideError):
            self.h.c("archive.history")
        self.h.app.store.chat_gone(BIG)
        self.assertEqual(self.h.c("archive.history", chat=BIG), {"unreadable": True})

    def test_start_of_a_short_chat(self):
        page = self.read("chat_example_team", limit=50)
        self.assertNotIn("more", page)
        texts = [m["text"] for m in page["messages"]]
        self.assertNotIn("示例同事丙 加入了群聊", texts, "system lines stay out, as in archive.changes")
        self.assertEqual(self.h.c("archive.history", chat="chat_example_team", before="0.0:0"), {})
        self.assertEqual(self.h.c("archive.history", chat="chat_example_team", after="%r:0" % (time.time() + 60)), {})

    def test_deep_pages_seek_the_index(self):
        store = self.h.app.store
        for bounds in ({"older_than": (self.now, 5)}, {"older_than": (self.now, 5), "inclusive": True},
                       {"newer_than": (self.now, 5)}):
            sql, args = history_sql(BIG, bounds.get("older_than"), bounds.get("newer_than"),
                                    bounds.get("inclusive", False), 1)
            with store._lock:  # the store's own connection: how its history queries are planned
                plan = " ".join(str(r[3]) for r in store._db.execute("EXPLAIN QUERY PLAN " + sql, args))
            self.assertIn("messages_chat_when", plan)
            self.assertIn("<expr>", plan, "the time bound seeks the index: " + plan)


class UndeclaredTest(unittest.TestCase):
    def test_wechat_does_not_offer_history(self):
        sys.path.insert(0, os.path.join(ROOT, "extensions", "openmate-wechat"))
        import openmate_wechat

        h = Harness(self, platform=openmate_wechat.platform())
        for method in ("history.chats", "archive.history"):
            with self.assertRaises(HubSideError) as e:
                h.c(method, chat="x")
            self.assertEqual(e.exception.code, "no_capability")


if __name__ == "__main__":
    unittest.main()
