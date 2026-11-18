"""history.chats / archive.history: the knowledge base's per-chat settings and history import, read from the store."""

import os
import sys
import time
import unittest

from harness import ROOT, Harness, HubSideError, wait_until
from openmate_core.backend import Chat, Message

BIG = "chat_example_history"


def page_ok(test, q: dict, page: dict) -> None:
    """The protocol's rules for a page (Persona's connector conformance, checkHistoryPage)."""
    msgs = page.get("messages") or []
    if page.get("more"):
        test.assertTrue(page.get("older"), "more without an older cursor")
        test.assertTrue(msgs, "more on a page with no messages")
    else:
        test.assertNotIn("older", page, "an older cursor without more")
    if page.get("more") and q.get("before"):
        test.assertNotEqual(page["older"], q["before"], "the same page would be read again")
    positions = [int(m["position"]) for m in msgs]
    test.assertEqual(positions, sorted(positions), "oldest first")


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self)
        self.h.world.add_chat(Chat(BIG, "group", "示例历史群", members=30))
        self.h.sign_in()
        wait_until(lambda: self.h.app._catchup_done.is_set(), what="catch-up")
        # 300 lines straight into the store: every 7th a system line, every 11th empty (read as nothing)
        now = time.time()
        rows = []
        for i in range(1, 301):
            kind = "SYSTEM" if i % 7 == 0 else "TEXT"
            text = "" if i % 11 == 0 else "第 %d 条" % i
            rows.append(Message(id="msg_hist_%d" % i, chat_id=BIG, position=i, sender_id="user_example_a",
                                sender_name="示例同事甲", text=text, type=kind, at=now - 10000 + i))
        self.h.app.store.add_messages(rows, from_history=True)
        self.content = [i for i in range(1, 301) if i % 7 and i % 11]

    def read(self, **q):
        page = self.h.c("archive.history", chat=BIG, **q)
        page_ok(self, q, page)
        return page

    def test_chats_lists_what_is_stored(self):
        chats = {c["id"]: c for c in self.h.c("history.chats")["chats"]}
        self.assertEqual(chats[BIG]["name"], "示例历史群")
        self.assertEqual(chats[BIG]["kind"], "group")
        self.assertEqual(chats[BIG]["estimate"], 300 - 300 // 7)  # system lines not counted
        self.assertEqual(chats["chat_example_dm_a"]["kind"], "dm")
        for c in chats.values():
            self.assertTrue(c["id"] and c["name"])
            self.assertIn(c["kind"], ("dm", "group"))
        self.h.app.store.chat_gone("chat_example_chatter")
        self.assertNotIn("chat_example_chatter", [c["id"] for c in self.h.c("history.chats")["chats"]])

    def test_paging_back_from_the_newest_reads_everything_once(self):
        seen, q = [], {"limit": 40}
        for _ in range(20):
            page = self.read(**q)
            got = [int(m["position"]) for m in page.get("messages") or []]
            self.assertLessEqual(len(got), 40)
            if seen:
                self.assertLess(max(got), min(seen), "a page newer than the one before")
            seen = got + seen
            if not page.get("more"):
                break
            q = {"limit": 40, "before": page["older"]}
        else:
            self.fail("never reached the start")
        self.assertEqual(seen, self.content)

    def test_paging_forward(self):
        seen, q = [], {"limit": 50, "after": "0"}
        for _ in range(20):
            page = self.read(**q)
            got = [int(m["position"]) for m in page.get("messages") or []]
            if not got:
                break
            seen += got
            if not page.get("newer"):
                break
            q = {"limit": 50, "after": page["newer"]}
        self.assertEqual(seen, self.content)

    def test_around_holds_the_position(self):
        page = self.read(around="100", limit=10)
        got = [int(m["position"]) for m in page["messages"]]
        self.assertIn(100, got)
        self.assertEqual(len(got), 10)
        self.assertTrue(page["more"])
        self.assertTrue(page["newer"])

    def test_messages_have_the_archive_shape(self):
        page = self.read(limit=3)
        m = page["messages"][-1]
        self.assertEqual(m["id"], "msg_hist_300")
        self.assertEqual(m["text"], "第 300 条")
        self.assertEqual(m["sender_name"], "示例同事甲")
        self.assertEqual(m["sender_handles"], [{"kind": "lark", "value": "user_example_a"}])
        self.assertNotIn("newer", page)  # the newest page

    def test_limit_is_a_hint_held_to_200(self):
        self.assertEqual(len(self.read(limit=1000)["messages"]), 200)
        self.assertEqual(len(self.read()["messages"]), 50)

    def test_unreadable_and_mistakes(self):
        self.assertEqual(self.h.c("archive.history", chat="chat_nope"), {"unreadable": True})
        self.h.app.store.chat_gone(BIG)
        self.assertEqual(self.h.c("archive.history", chat=BIG), {"unreadable": True})
        with self.assertRaises(HubSideError):
            self.h.c("archive.history", chat="chat_example_team", before="3", after="1")
        with self.assertRaises(HubSideError):
            self.h.c("archive.history", chat="chat_example_team", before="not-ours")
        with self.assertRaises(HubSideError):
            self.h.c("archive.history")

    def test_start_of_a_short_chat(self):
        page = self.h.c("archive.history", chat="chat_example_team", limit=50)
        page_ok(self, {}, page)
        self.assertNotIn("more", page)
        texts = [m["text"] for m in page["messages"]]
        self.assertNotIn("示例同事丙 加入了群聊", texts, "system lines stay out, as in archive.changes")
        self.assertEqual(self.h.c("archive.history", chat="chat_example_team", before="1"), {})


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
