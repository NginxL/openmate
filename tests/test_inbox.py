"""inbox.collect (kinds, done, noise, context, tags, rules, the window) and inbox.reply (once, as the user)."""

try:  # first: every temporary directory of this run in one, removed at its end
    import _runtmp  # noqa: F401
except ImportError:  # (started by its path from elsewhere: its directory is not on the path)
    pass
import time
import unittest

from harness import CID, Harness, HubSideError, fast, wait_until


def things(h):
    return {t["thread"]: t for t in h.c("inbox.collect")["things"]}


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self, config={"triage_rules": "客户的消息都要我处理"})
        self.h.sign_in()
        wait_until(lambda: len(things(self.h)) >= 4, what="the seeded chats")

    def test_kinds_and_done(self):
        t = things(self.h)
        self.assertEqual(t["chat_example_dm_a"]["kind"], "dm")
        self.assertEqual(t["chat_example_team"]["kind"], "mention")
        self.assertEqual(t["chat_example_chatter"]["kind"], "group")
        dm_b = t["chat_example_dm_b"]
        self.assertEqual(dm_b["done"], "replied")
        self.assertEqual(dm_b["last"], self.h.world.history["chat_example_dm_b"][-1].id)
        for th in t.values():
            self.assertIn(th["kind"], ("dm", "mention", "group"))
            self.assertTrue(th["at"].endswith("Z"))
            self.assertTrue(th["last"])

    def test_unanswered_is_the_trailing_run(self):
        dm = things(self.h)["chat_example_dm_a"]
        hist = self.h.world.history["chat_example_dm_a"]
        self.assertEqual(dm["last"], hist[-1].id)  # the picture after the question
        self.assertEqual(dm["preview"], "[图片]")
        self.assertEqual(dm["sender"], "示例同事甲")
        self.assertNotIn("done", dm)

    def test_group_without_mention_is_noise_by_rule(self):
        t = things(self.h)
        g = t["chat_example_chatter"]
        self.assertEqual(g["tier"], "noise")
        self.assertIn("规则", g["why"])
        self.assertNotIn("tier", t["chat_example_team"])  # a mention goes to the model
        self.assertNotIn("tier", t["chat_example_dm_a"])

    def test_context_tags_rules_reply(self):
        team = things(self.h)["chat_example_team"]
        lines = team["context"].split("\n")
        self.assertIn("示例项目群", lines[0])
        self.assertTrue(any("@示例用户 接口文档" in l for l in lines))
        self.assertFalse(any("加入了群聊" in l for l in lines), "system lines stay out")
        self.assertLessEqual(len(lines), 9)
        self.assertEqual(team["rules"], "客户的消息都要我处理")
        self.assertTrue(team["can_reply"])
        self.assertIn("群里的人都能看到", team["reply_to"])
        self.assertIn({"key": "chat", "value": "chat_example_team", "name": "示例项目群"}, team["tags"])
        self.assertIn({"key": "sender", "value": "user_example_b", "name": "示例同事乙"}, team["tags"])
        self.assertEqual(team["label"], "@我")

    def test_context_is_the_last_eight(self):
        for i in range(12):
            self.h.world.push("chat_example_chatter", "user_example_b", "第 %d 条" % i)
        wait_until(lambda: "第 11 条" in things(self.h)["chat_example_chatter"]["context"], what="new messages")
        lines = things(self.h)["chat_example_chatter"]["context"].split("\n")
        self.assertEqual(len(lines), 9)
        self.assertIn("第 4 条", lines[1])

    def test_my_reply_ends_it_and_a_new_message_reopens(self):
        self.h.world.push("chat_example_team", "me", "我来更新")
        wait_until(lambda: things(self.h)["chat_example_team"].get("done") == "replied", what="replied")
        m = self.h.world.push("chat_example_team", "user_example_b", "谢谢")
        t = wait_until(lambda: (lambda x: x if x.get("last") == m.id else None)(things(self.h)["chat_example_team"]), what="reopened")
        self.assertNotIn("done", t)
        self.assertEqual(t["kind"], "group")  # no @me since my reply
        self.assertEqual(t["tier"], "noise")

    def test_stable_threads(self):
        a = sorted(things(self.h))
        b = sorted(things(self.h))
        self.assertEqual(a, b)


class WindowTest(unittest.TestCase):
    def test_three_days_and_most(self):
        h = Harness(self, timing=fast(inbox_most=3))
        h.world.make("chat_example_chatter", "user_example_a", "很久以前", ago=5 * 86400)
        h.sign_in()
        wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
        t = h.c("inbox.collect")["things"]
        self.assertEqual(len(t), 3)
        h2 = Harness(self)
        for cid in list(h2.world.history):
            for m in h2.world.history[cid]:
                m.at -= 4 * 86400
        h2.sign_in()
        wait_until(lambda: h2.app._catchup_done.is_set(), what="catch-up")
        self.assertEqual(h2.c("inbox.collect")["things"], [])

    def test_no_reply_offer_when_signed_out(self):
        h = Harness(self)
        h.sign_in()
        wait_until(lambda: h.c("inbox.collect")["things"], what="things")
        h.c("logout")
        for t in h.c("inbox.collect")["things"]:
            self.assertNotIn("can_reply", t)


class ReplyTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self)
        self.h.sign_in()
        wait_until(lambda: len(things(self.h)) >= 4, what="the seeded chats")

    def test_reply_once_per_thread_last_text(self):
        t = things(self.h)["chat_example_team"]
        for _ in range(3):
            self.assertEqual(self.h.c("inbox.reply", thread=t["thread"], item_id="x", last=t["last"], text="我来更新"), {})
        sent = self.h.sent()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["chat"], "chat_example_team")
        self.assertEqual(sent[0]["reply_to"], t["last"])  # a group reply quotes the message it answers
        # another text is another reply
        self.h.c("inbox.reply", thread=t["thread"], last=t["last"], text="明天给")
        self.assertEqual(len(self.h.sent()), 2)
        wait_until(lambda: things(self.h)["chat_example_team"].get("done") == "replied", what="done")

    def test_reply_once_per_client_id(self):
        t = things(self.h)["chat_example_dm_a"]
        self.h.c("inbox.reply", thread=t["thread"], last=t["last"], text="好的", client_id="c-1")
        self.h.c("inbox.reply", thread=t["thread"], last="other", text="好的，改了措辞", client_id="c-1")
        sent = self.h.sent()
        self.assertEqual(len(sent), 1)
        self.assertIsNone(sent[0]["reply_to"])  # a direct chat reply is a plain message

    def test_uncertain_reply_is_never_sent_again(self):
        t = things(self.h)["chat_example_dm_a"]
        self.h.world.send_fail = "uncertain"
        with self.assertRaises(HubSideError) as e:
            self.h.c("inbox.reply", thread=t["thread"], last=t["last"], text="好的", client_id="c-2")
        self.assertIn("不确定", e.exception.message)
        with self.assertRaises(HubSideError) as e:
            self.h.c("inbox.reply", thread=t["thread"], last=t["last"], text="好的", client_id="c-2")
        self.assertIn("不会再发", e.exception.message)
        self.assertEqual(self.h.sent(), [])

    def test_rejected_reply_may_be_tried_again(self):
        t = things(self.h)["chat_example_dm_a"]
        self.h.world.send_fail = "rejected"
        with self.assertRaises(HubSideError) as e:
            self.h.c("inbox.reply", thread=t["thread"], last=t["last"], text="好的", client_id="c-3")
        self.assertIn("没有发出去", e.exception.message)
        self.h.c("inbox.reply", thread=t["thread"], last=t["last"], text="好的", client_id="c-3")
        self.assertEqual(len(self.h.sent()), 1)

    def test_refusals(self):
        t = things(self.h)["chat_example_dm_a"]
        with self.assertRaises(HubSideError):
            self.h.c("inbox.reply", thread=t["thread"], last=t["last"], text="  ")
        with self.assertRaises(HubSideError):
            self.h.c("inbox.reply", thread=t["thread"], text="", changes=[{"ref": "r", "field": "f", "value": "v"}])
        with self.assertRaises(HubSideError):
            self.h.c("inbox.reply", thread="chat_unknown", text="你好")
        self.assertEqual(self.h.sent(), [])


if __name__ == "__main__":
    unittest.main()
