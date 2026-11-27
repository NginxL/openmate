"""inbox.collect for groups muted in the service, the groups listed for AI triage (inbox_triage_chats), the direct chats
no Inbox model reads (inbox_private_dms), and a group's topics as Inbox items of their own (Platform.inbox_topics),
with their reply and page; WeChat's Inbox as before."""

try:  # first: every temporary directory of this run in one, removed at its end
    import _runtmp  # noqa: F401
except ImportError:  # (started by its path from elsewhere: its directory is not on the path)
    pass
import os
import sys
import unittest

from harness import ROOT, Harness, HubSideError, wait_until

sys.path.insert(0, os.path.join(ROOT, "extensions", "openmate-wechat"))
import openmate_wechat  # noqa: E402

TEAM, CHATTER, DM_A = "chat_example_team", "chat_example_chatter", "chat_example_dm_a"


def things(h):
    return {t["thread"]: t for t in h.c("inbox.collect")["things"]}


def signed_in(test, mute_ok=False, mutes=None, **config):
    h = Harness(test, config=config)
    h.world.mute_ok = mute_ok
    h.world.mutes.update(mutes or {})
    h.sign_in()
    wait_until(lambda: len(things(h)) >= 4, what="the seeded chats")
    return h


class MuteTest(unittest.TestCase):
    def test_a_muted_group_without_an_at_stays_out_and_an_at_stays_in(self):
        h = signed_in(self, mute_ok=True, mutes={CHATTER: True, TEAM: True})
        wait_until(lambda: CHATTER not in things(h), what="the muted group out of the Inbox")
        t = things(h)
        self.assertEqual(t[TEAM]["kind"], "mention")  # an @ of the user in a muted group is sorted as usual
        self.assertNotIn("tier", t[TEAM])
        self.assertIn(DM_A, t)
        self.assertEqual(set(h.world.mute_asked), {TEAM, CHATTER})  # groups only, never a direct chat
        self.assertTrue(h.hub.changed("inbox"), "the Inbox is told to read again once a state is known")
        # still archived: the knowledge base gets its messages
        archived = h.c("archive.changes", cursor="", limit=100)
        self.assertIn(CHATTER, {b["chat"]["id"] for b in archived["batches"]})

    def test_not_muted_or_not_known_is_as_before(self):
        h = signed_in(self, mute_ok=True, mutes={CHATTER: False})
        wait_until(lambda: h.app.store.mutes([CHATTER, TEAM]).get(TEAM, ("",))[0] == "unknown", what="asked")
        t = things(h)
        self.assertEqual(t[CHATTER]["tier"], "noise")  # a group the user did not mute: by rule, as before
        self.assertEqual(h.app.store.mutes([CHATTER])[CHATTER][0], "normal")
        h.world.push(TEAM, "user_example_a", "大家看下这个")
        h.world.push(TEAM, "me", "收到")
        h.world.push(TEAM, "user_example_a", "好的")
        wait_until(lambda: things(h)[TEAM]["kind"] == "group", what="the team group without an @")
        self.assertEqual(things(h)[TEAM]["tier"], "noise")  # the service did not say: as before

    def test_a_failed_read_keeps_the_inbox_as_it_was(self):
        h = Harness(self)
        h.world.mute_ok = True
        h.world.mute_fail = True
        h.sign_in()
        wait_until(lambda: things(h) and h.world.mute_asked, what="asked")
        self.assertEqual(things(h)[CHATTER]["tier"], "noise")
        self.assertEqual(h.app.store.mutes([CHATTER]), {})

    def test_a_reply_in_a_muted_group_still_ends_its_item(self):
        h = signed_in(self, mute_ok=True, mutes={CHATTER: True})
        wait_until(lambda: CHATTER not in things(h), what="muted")
        h.world.push(CHATTER, "me", "我也去")
        t = wait_until(lambda: things(h).get(CHATTER), what="done")
        self.assertEqual(t["done"], "replied")

    def test_no_mute_asked_without_the_capability(self):
        h = signed_in(self, mute_ok=False, mutes={CHATTER: True})
        self.assertIn(CHATTER, things(h))
        self.assertEqual(h.world.mute_asked, [])


class TriageChatsTest(unittest.TestCase):
    def test_a_listed_group_goes_to_the_model_muted_or_not(self):
        h = signed_in(self, mute_ok=True, mutes={CHATTER: True}, inbox_triage_chats=[CHATTER])
        wait_until(lambda: h.app.store.mutes([CHATTER]).get(CHATTER), what="asked")
        g = things(h)[CHATTER]
        self.assertNotIn("tier", g)
        self.assertEqual(g["extra"], {"rule_tier": "noise"})  # the rule's tier only where the model cannot sort
        self.assertEqual(g["kind"], "group")

    def test_default_is_as_before(self):
        h = signed_in(self)
        self.assertEqual(things(h)[CHATTER]["tier"], "noise")
        self.assertNotIn("extra", things(h)[CHATTER])


class PrivateDMsTest(unittest.TestCase):
    def test_listed_by_chat_or_by_person_no_model(self):
        for listed in (DM_A, "user_example_a"):
            h = signed_in(self, inbox_private_dms=[listed])
            dm = things(h)[DM_A]
            self.assertEqual(dm["tier"], "action")
            self.assertIn("不交给收件箱 AI", dm["why"])
            self.assertNotIn("tier", things(h)[TEAM])

    def test_default_is_as_before(self):
        h = signed_in(self)
        self.assertNotIn("tier", things(h)[DM_A])

    def test_a_string_setting_is_split(self):
        h = signed_in(self, inbox_private_dms="%s， other" % DM_A)
        self.assertEqual(things(h)[DM_A]["tier"], "action")


class TopicsTest(unittest.TestCase):
    def setUp(self):
        self.h = signed_in(self)
        w = self.h.world
        self.root = w.push(TEAM, "user_example_a", "这个方案大家怎么看？")
        self.reply = w.push(TEAM, "user_example_b", "我觉得可以", root_id=self.root.id, parent_id=self.root.id)
        self.main = w.push(TEAM, "user_example_c", "另外，周会改到四点")
        self.topic = "%s:%s" % (TEAM, self.root.id)
        wait_until(lambda: self.topic in things(self.h) and things(self.h)[TEAM]["last"] == self.main.id,
                   what="the topic's item and the chat's")

    def test_a_topic_is_an_item_of_its_own(self):
        t = things(self.h)
        topic, chat = t[self.topic], t[TEAM]
        self.assertEqual(topic["last"], self.reply.id)
        self.assertIn("我觉得可以", topic["context"])
        self.assertIn("这个方案", topic["context"])
        self.assertNotIn("周会改到四点", topic["context"])
        self.assertIn("话题", topic["context"].split("\n")[0])
        self.assertIn("这个话题", topic["reply_to"])
        self.assertIn({"key": "chat", "value": TEAM, "name": "示例项目群"}, topic["tags"])
        self.assertNotIn("我觉得可以", chat["context"])
        self.assertIn("周会改到四点", chat["context"])
        self.assertEqual(chat["kind"], "mention")  # the @ before the topic is the chat's, as it was

    def test_a_reply_in_the_topic_ends_only_the_topic(self):
        self.h.world.push(TEAM, "me", "那就这么定", root_id=self.root.id, parent_id=self.reply.id)
        wait_until(lambda: things(self.h)[self.topic].get("done") == "replied", what="the topic done")
        self.assertNotIn("done", things(self.h)[TEAM])

    def test_inbox_reply_goes_into_the_topic(self):
        t = things(self.h)[self.topic]
        self.h.c("inbox.reply", thread=t["thread"], last=t["last"], text="同意")
        sent = self.h.sent()[-1]
        self.assertEqual((sent["chat"], sent["reply_to"], sent["root"]), (TEAM, self.reply.id, self.root.id))
        wait_until(lambda: things(self.h)[self.topic].get("done") == "replied", what="done")
        chat = things(self.h)[TEAM]  # the chat's own item still replies to the chat, as before
        self.h.c("inbox.reply", thread=TEAM, last=chat["last"], text="好")
        sent = self.h.sent()[-1]
        self.assertEqual((sent["chat"], sent["reply_to"], sent["root"]), (TEAM, self.main.id, None))

    def test_a_reply_to_a_topic_whose_last_is_mine_goes_under_its_root(self):
        self.h.c("inbox.reply", thread=self.topic, last="", text="补充一下")
        sent = self.h.sent()[-1]
        self.assertEqual((sent["reply_to"], sent["root"]), (self.root.id, self.root.id))

    def test_the_topic_page_and_the_chat_page(self):
        page = self.h.c("thread.page", thread=self.topic, limit=20)
        self.assertEqual([m["id"] for m in page["messages"]], [self.root.id, self.reply.id])
        self.assertEqual(page["chat"]["id"], self.topic)
        self.assertNotIn("reply", page["chat"])
        self.assertNotIn("before", page)
        whole = self.h.c("thread.page", thread=TEAM, limit=50)
        self.assertIn(self.main.id, [m["id"] for m in whole["messages"]])
        self.assertEqual(whole["chat"]["reply"], "matefeishu_send")

    def test_a_muted_group_s_topic_without_an_at_stays_out(self):
        h = signed_in(self, mute_ok=True, mutes={TEAM: True})
        root = h.world.push(TEAM, "user_example_a", "话题一")
        h.world.push(TEAM, "user_example_b", "回复", root_id=root.id, parent_id=root.id)
        wait_until(lambda: h.app.store.mutes([TEAM]).get(TEAM), what="asked")
        t = things(h)
        self.assertNotIn("%s:%s" % (TEAM, root.id), t)
        self.assertEqual(t[TEAM]["kind"], "mention")  # the chat's own @ of the user stays


class WeChatTest(unittest.TestCase):
    def test_no_topics_no_mutes_one_item_per_chat(self):
        from openmate_wechat.backend import WeChatBackend

        self.assertFalse(WeChatBackend.has_mute)  # it never says a chat is muted: nothing is asked or left out
        h = Harness(self, platform=openmate_wechat.platform(),
                    config={"inbox_triage_chats": [CHATTER], "inbox_private_dms": [DM_A]})
        h.sign_in()
        wait_until(lambda: len(things(h)) >= 4, what="the seeded chats")
        root = h.world.push(TEAM, "user_example_a", "这个方案大家怎么看？")
        r = h.world.push(TEAM, "user_example_b", "我觉得可以", root_id=root.id, parent_id=root.id)
        wait_until(lambda: things(h)[TEAM]["last"] == r.id, what="one item for the chat")
        self.assertEqual(sorted(things(h)), sorted([TEAM, CHATTER, DM_A, "chat_example_dm_b"]))
        self.assertEqual(things(h)[CHATTER]["tier"], "noise")  # no such settings there: as before
        self.assertNotIn("tier", things(h)[DM_A])
        with self.assertRaises(HubSideError):  # no topic ids there: a thread is a chat's id, whole
            h.c("thread.page", thread=TEAM + ":" + root.id, limit=5)


if __name__ == "__main__":
    unittest.main()
