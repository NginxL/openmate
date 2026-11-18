"""history.chats / archive.history: the knowledge base's per-chat settings and history import, read from the store."""

import os
import sys
import time
import unittest

from harness import ROOT, Harness, HubSideError, wait_until
from openmate_core.backend import Chat, Message

BIG = "chat_example_history"


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
    ats = [m["at"] for m in msgs]
    test.assertTrue(all(ats), "a message without a time")
    test.assertEqual(ats, sorted(ats), "oldest first")


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

    def back(self, chat=BIG, limit=40):
        seen, q = [], {"limit": limit}
        for _ in range(400):
            page = self.h.c("archive.history", chat=chat, **q)
            page_ok(self, q, page)
            got = [m["id"] for m in page.get("messages") or []]
            self.assertLessEqual(len(got), limit)
            seen = got + seen
            if not page.get("more"):
                return seen
            q = {"limit": limit, "before": page["older"]}
        self.fail("never reached the start")

    def forward(self, chat=BIG, limit=50, after="0"):
        seen, q = [], {"limit": limit, "after": after}
        for _ in range(400):
            page = self.h.c("archive.history", chat=chat, **q)
            page_ok(self, q, page)
            got = [m["id"] for m in page.get("messages") or []]
            seen += got
            if not page.get("newer"):
                self.assertTrue(got or not seen, "newer led to an empty page")
                return seen
            q = {"limit": limit, "after": page["newer"]}
        self.fail("never reached the newest")

    def test_paging_back_from_the_newest_reads_everything_once(self):
        self.assertEqual(self.back(), ["msg_hist_%d" % i for i in self.content])

    def test_paging_forward(self):
        self.assertEqual(self.forward(), ["msg_hist_%d" % i for i in self.content])

    def test_lines_sharing_a_position_are_never_cut(self):
        # a Feishu topic reply takes its group's newest position: several lines share one, stored out of time order
        now = time.time()
        # 4 positions with 3 lines each; within a position the later-stored lines are older (a reply filled in late)
        rows = [Message(id="msg_same_%d" % i, chat_id=BIG, position=500 + i // 3, sender_id="user_example_b",
                        text="同位 %d" % i, at=now - 100 + (i // 3) * 10 - (i % 3)) for i in range(12)]
        self.h.app.store.add_messages(rows, from_history=False)
        want = ["msg_hist_%d" % i for i in self.content] + ["msg_same_%d" % i for i in range(12)]
        for limit in (1, 2, 3, 5):
            self.assertEqual(sorted(self.back(limit=limit)), sorted(want), "limit %d back" % limit)
            self.assertEqual(sorted(self.forward(limit=limit, after="300")), sorted(want[len(self.content):]),
                             "limit %d forward" % limit)
        page = self.read(around="501", limit=2)  # a bare position: around it with every line it has
        self.assertTrue({"msg_same_3", "msg_same_4", "msg_same_5"} & {m["id"] for m in page["messages"]})

    def test_cursors_never_lead_to_an_empty_page(self):
        now = time.time()
        rows = [Message(id="msg_edge_%d" % i, chat_id="chat_example_edge", position=i, sender_id="user_example_a",
                        text="" if i != 2 else "唯一一条", type="SYSTEM" if i == 3 else "TEXT", at=now - 10 + i)
                for i in (1, 2, 3)]
        self.h.world.add_chat(Chat("chat_example_edge", "group", "示例边界群"))
        self.h.app.store.add_messages(rows, from_history=False)
        self.h.app.store.upsert_chat(Chat("chat_example_edge", "group", "示例边界群"))
        page = self.h.c("archive.history", chat="chat_example_edge", limit=1)
        self.assertEqual([m["id"] for m in page["messages"]], ["msg_edge_2"])
        self.assertNotIn("more", page)
        self.assertNotIn("newer", page)

    def test_a_missing_time_reads_as_when_it_came_in(self):
        self.h.app.store.add_messages([Message(id="msg_no_time", chat_id=BIG, position=900, sender_id="user_example_a",
                                               text="没有时间", at=0)], from_history=False)
        page = self.read(limit=1)
        self.assertEqual(page["messages"][0]["id"], "msg_no_time")
        self.assertTrue(page["messages"][0]["at"])

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
        for huge in ("9223372036854775808", "1:9223372036854775808", "x:1"):
            with self.assertRaises(HubSideError):
                self.h.c("archive.history", chat="chat_example_team", before=huge)
        self.assertEqual(self.h.c("archive.history", chat="chat_example_team", around="9223372036854775807",
                                  limit=4)["messages"][-1]["text"], "@示例用户 接口文档这周谁来更新？")

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
