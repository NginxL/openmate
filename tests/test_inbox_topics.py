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


def mutes(h, ids):
    return h.app.store.mutes(ids, h.app.store.meta_get("account_id"))


def signed_in(test, mute_ok=False, mutes=None, **config):
    h = Harness(test, config=config)
    h.world.mute_ok = mute_ok
    h.world.mutes.update(mutes or {})
    h.sign_in()
    wait_until(lambda: len(things(h)) >= 4, what="the seeded chats")
    return h


class MuteTest(unittest.TestCase):
    def test_a_muted_group_without_an_at_is_done_and_an_at_stays_in(self):
        h = signed_in(self, mute_ok=True, mutes={CHATTER: True, TEAM: True})
        wait_until(lambda: things(h)[CHATTER].get("done") == "read", what="the muted group's item done")
        t = things(h)
        self.assertIn("免打扰", t[CHATTER]["why"])  # handed over done: an item the Inbox kept from before ends too
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
        wait_until(lambda: mutes(h, [CHATTER, TEAM]).get(TEAM, ("",))[0] == "unknown", what="asked")
        t = things(h)
        self.assertEqual(t[CHATTER]["tier"], "noise")  # a group the user did not mute: by rule, as before
        self.assertEqual(mutes(h, [CHATTER])[CHATTER][0], "normal")
        h.world.push(TEAM, "user_example_a", "大家看下这个")
        h.world.push(TEAM, "me", "收到")
        h.world.push(TEAM, "user_example_a", "好的")
        wait_until(lambda: things(h)[TEAM]["kind"] == "group", what="the team group without an @")
        self.assertEqual(things(h)[TEAM]["tier"], "noise")  # the service did not say: as before

    def test_a_failed_read_keeps_the_inbox_as_it_was(self):
        h = Harness(self)
        h.world.mute_ok = True
        h.world.mutes[CHATTER] = True
        h.sign_in()
        wait_until(lambda: things(h) and mutes(h, [CHATTER]).get(CHATTER, ("",))[0] == "muted", what="muted")
        # the state goes stale and the next read fails: not known any more, so the old way (not the stale mute)
        with h.app.store._lock:
            h.app.store._db.execute("UPDATE mutes SET checked = 0")
        h.app._mutes_tried.clear()
        h.world.mute_fail = True
        wait_until(lambda: things(h) and mutes(h, [CHATTER])[CHATTER][0] == "unknown", what="unknown after a failure")
        self.assertEqual(things(h)[CHATTER]["tier"], "noise")
        self.assertNotIn("done", things(h)[CHATTER])

    def test_a_muted_group_s_at_is_the_item_not_the_chatter_after_it(self):
        h = signed_in(self, mute_ok=True, mutes={TEAM: True})
        wait_until(lambda: mutes(h, [TEAM]).get(TEAM), what="asked")
        at = [m for m in h.world.history[TEAM] if m.at_me][-1]
        h.world.push(TEAM, "user_example_c", "顺便说一句，今天下雨")
        wait_until(lambda: "今天下雨" in str(h.app.store.messages_before(TEAM, None, 5)), what="kept")
        t = things(h)[TEAM]
        self.assertEqual((t["kind"], t["last"]), ("mention", at.id))
        self.assertNotIn("今天下雨", t["context"])

    def test_a_row_of_another_account_is_never_read(self):
        h = signed_in(self)
        h.app.store.set_mute(CHATTER, "muted", "someone_else")
        self.assertEqual(mutes(h, [CHATTER]), {})

    def test_a_reply_in_a_muted_group_still_ends_its_item(self):
        h = signed_in(self, mute_ok=True, mutes={CHATTER: True})
        wait_until(lambda: things(h)[CHATTER].get("done") == "read", what="muted")
        h.world.push(CHATTER, "me", "我也去")
        t = wait_until(lambda: (lambda x: x if x.get("done") == "replied" else None)(things(h)[CHATTER]), what="done")
        self.assertEqual(t["why"], "我已经回复")

    def test_no_mute_asked_without_the_capability(self):
        h = signed_in(self, mute_ok=False, mutes={CHATTER: True})
        self.assertIn(CHATTER, things(h))
        self.assertEqual(h.world.mute_asked, [])


class TriageChatsTest(unittest.TestCase):
    def test_a_listed_group_goes_to_the_model_muted_or_not(self):
        h = signed_in(self, mute_ok=True, mutes={CHATTER: True}, inbox_triage_chats=[CHATTER])
        wait_until(lambda: mutes(h, [CHATTER]).get(CHATTER), what="asked")
        g = things(h)[CHATTER]
        self.assertNotIn("tier", g)
        self.assertEqual(g["extra"], {"rule_tier": "noise"})  # the rule's tier only where the model cannot sort
        self.assertEqual(g["kind"], "group")

    def test_default_is_as_before(self):
        h = signed_in(self)
        self.assertEqual(things(h)[CHATTER]["tier"], "noise")
        self.assertNotIn("extra", things(h)[CHATTER])


class PrivateDMsTest(unittest.TestCase):
    def test_listed_by_chat_or_by_person_no_model_no_words(self):
        for listed in (DM_A, "user_example_a"):
            h = signed_in(self, inbox_private_dms=[listed])
            dm = things(h)[DM_A]
            self.assertEqual(dm["tier"], "action")
            self.assertIn("不交给收件箱 AI", dm["why"])
            self.assertIn("不交给收件箱 AI", dm["preview"])
            for words in ("评审", "文档", "图片"):  # nothing of what was said: no model reads it from the item
                self.assertNotIn(words, dm["preview"] + dm["context"])
            self.assertNotIn("tier", things(h)[TEAM])

    def test_by_person_when_the_chat_does_not_say_who(self):
        h = signed_in(self, inbox_private_dms=["user_example_a"])
        with h.app.store._lock:
            h.app.store._db.execute("UPDATE chats SET peer_id = '' WHERE id = ?", (DM_A,))
        self.assertEqual(things(h)[DM_A]["tier"], "action")  # the other person as their lines say

    def test_default_is_as_before(self):
        h = signed_in(self)
        self.assertNotIn("tier", things(h)[DM_A])

    def test_by_chat_id_whatever_kind_it_was_kept_as(self):
        h = signed_in(self, inbox_private_dms=[DM_A])
        with h.app.store._lock:  # first kept when its kind could not be read: a group till it is
            h.app.store._db.execute("UPDATE chats SET kind = 'group' WHERE id = ?", (DM_A,))
        t = things(h)[DM_A]
        self.assertEqual((t["tier"], t["preview"]), ("action", "（这个私聊设置为不交给收件箱 AI：内容不在这里，打开会话查看）"))
        self.assertNotIn("评审", t["context"])

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
        self.assertIn("这个方案大家怎么看？（话题的根消息）", topic["context"])
        self.assertNotIn("周会改到四点", topic["context"])
        self.assertIn("话题", topic["context"].split("\n")[0])
        self.assertIn("这个话题", topic["reply_to"])
        self.assertIn({"key": "chat", "value": TEAM, "name": "示例项目群"}, topic["tags"])
        self.assertNotIn("我觉得可以", chat["context"])
        self.assertIn("这个方案大家怎么看", chat["context"])  # the root was said in the main stream: the chat's too
        self.assertIn("周会改到四点", chat["context"])
        self.assertEqual(chat["kind"], "mention")  # the @ before the topic is the chat's, as it was

    def test_a_reply_in_the_topic_ends_the_topic_and_what_its_root_asked(self):
        w = self.h.world
        w.push(TEAM, "me", "那就这么定", root_id=self.root.id, parent_id=self.reply.id)
        wait_until(lambda: things(self.h)[self.topic].get("done") == "replied", what="the topic done")
        # the chat's own item asked with the topic's root, and was answered there: done too (never left behind)
        self.assertEqual(things(self.h)[TEAM].get("done"), "replied")
        later = w.push(TEAM, "user_example_c", "下周呢？")
        wait_until(lambda: things(self.h)[TEAM]["last"] == later.id, what="the chat open again")
        self.assertNotIn("done", things(self.h)[TEAM])
        self.assertEqual(things(self.h)[self.topic].get("done"), "replied")  # another line in the main stream

    def test_a_topic_mode_group_has_an_item_per_topic(self):
        h = signed_in(self)
        with h.app.store._lock:
            h.app.store._db.execute("UPDATE chats SET topic = 1 WHERE id = ?", (CHATTER,))
        a = h.world.push(CHATTER, "user_example_a", "话题甲")
        b = h.world.push(CHATTER, "user_example_b", "话题乙")
        r = h.world.push(CHATTER, "me", "回甲", root_id=a.id, parent_id=a.id)
        wait_until(lambda: ("%s:%s" % (CHATTER, b.id)) in things(h), what="topics")
        t = things(h)
        self.assertEqual(t["%s:%s" % (CHATTER, a.id)]["done"], "replied")
        self.assertNotIn("done", t["%s:%s" % (CHATTER, b.id)])
        self.assertIn("话题乙", t["%s:%s" % (CHATTER, b.id)]["context"])
        self.assertNotIn("话题甲", t["%s:%s" % (CHATTER, b.id)]["context"])
        del r

    def test_a_busy_topic_pushes_no_other_topic_out(self):
        w = self.h.world
        at = w.push(TEAM, "user_example_b", "@示例用户 这个你看下", root_id=self.root.id, parent_id=self.root.id, at_me=True)
        other = w.push(TEAM, "user_example_c", "另一个话题")
        for i in range(80):
            w.push(TEAM, "user_example_c", "刷屏 %d" % i, root_id=other.id, parent_id=other.id)
        w.push(TEAM, "user_example_a", "补充一句", root_id=self.root.id, parent_id=at.id)
        wait_until(lambda: "补充一句" in things(self.h)[self.topic]["context"], what="the topic's new line")
        self.assertEqual(things(self.h)[self.topic]["kind"], "mention")

    def test_an_old_topic_is_left_out(self):
        w = self.h.world
        old = w.push(TEAM, "user_example_a", "四天前的话题", ago=4 * 86400)
        w.push(TEAM, "user_example_b", "四天前的回复", ago=4 * 86400 - 60, root_id=old.id, parent_id=old.id)
        w.push(TEAM, "user_example_c", "今天的消息")
        wait_until(lambda: "今天的消息" in things(self.h)[TEAM]["context"], what="today")
        self.assertNotIn("%s:%s" % (TEAM, old.id), things(self.h))

    def test_the_send_tool_refuses_a_line_of_another_topic(self):
        other = self.h.world.push(TEAM, "user_example_c", "别的话题")
        r = self.h.world.push(TEAM, "user_example_a", "回复", root_id=other.id, parent_id=other.id)
        wait_until(lambda: self.h.app.store.message(TEAM, r.id), what="kept")
        with self.assertRaises(HubSideError):
            self.h.c("actions.describe", name="matefeishu_send", args={"chat": self.topic, "text": "x", "reply_to": r.id})

    def test_a_topic_group_s_one_item_from_before_is_ended(self):
        h = signed_in(self)
        old = [m for m in h.world.history[CHATTER]][-1]
        with h.app.store._lock:
            h.app.store._db.execute("UPDATE chats SET topic = 1 WHERE id = ?", (CHATTER,))
        t = things(h)
        self.assertEqual((t[CHATTER]["done"], t[CHATTER]["last"]), ("read", old.id))  # the item before topics: ended
        self.assertIn("%s:%s" % (CHATTER, old.id), t)
        new = h.world.push(CHATTER, "user_example_a", "新话题")
        wait_until(lambda: "%s:%s" % (CHATTER, new.id) in things(h), what="the new topic")
        self.assertEqual(things(h)[CHATTER]["last"], old.id, "that ended item never moves")

    def test_the_send_tool_takes_a_topic_s_thread(self):
        a = {"chat": self.topic, "text": "页里回复"}
        card = self.h.c("actions.describe", name="matefeishu_send", args=a)
        self.assertIn("示例项目群", card["summary"])
        self.h.c("actions.do", name="matefeishu_send", args=a, request_id="r-topic")
        sent = self.h.sent()[-1]
        self.assertEqual((sent["chat"], sent["reply_to"], sent["root"]), (TEAM, self.root.id, self.root.id))

    def test_inbox_reply_goes_into_the_topic(self):
        chat = things(self.h)[TEAM]  # the chat's own item replies to the chat, as before
        self.h.c("inbox.reply", thread=TEAM, last=chat["last"], text="好")
        sent = self.h.sent()[-1]
        self.assertEqual((sent["chat"], sent["reply_to"], sent["root"]), (TEAM, self.main.id, None))
        t = things(self.h)[self.topic]
        self.h.c("inbox.reply", thread=t["thread"], last=t["last"], text="同意")
        sent = self.h.sent()[-1]
        self.assertEqual((sent["chat"], sent["reply_to"], sent["root"]), (TEAM, self.reply.id, self.root.id))
        wait_until(lambda: things(self.h)[self.topic].get("done") == "replied", what="done")

    def test_a_reply_to_a_topic_whose_last_is_mine_goes_under_its_root(self):
        self.h.c("inbox.reply", thread=self.topic, last="", text="补充一下")
        sent = self.h.sent()[-1]
        self.assertEqual((sent["reply_to"], sent["root"]), (self.root.id, self.root.id))

    def test_the_topic_page_and_the_chat_page(self):
        page = self.h.c("thread.page", thread=self.topic, limit=20)
        self.assertEqual([m["id"] for m in page["messages"]], [self.root.id, self.reply.id])
        self.assertEqual(page["chat"]["id"], self.topic)
        self.assertEqual(page["chat"]["reply"], "matefeishu_send")
        self.assertNotIn("before", page)
        more = [self.h.world.push(TEAM, "user_example_b", "第 %d 条" % i, root_id=self.root.id, parent_id=self.root.id)
                for i in range(5)]
        wait_until(lambda: len(self.h.app.store.thread_messages(TEAM, self.root.id, 50)) == 7, what="kept")
        first = self.h.c("thread.page", thread=self.topic, limit=3)
        self.assertEqual([m["id"] for m in first["messages"]], [m.id for m in more[-3:]])
        second = self.h.c("thread.page", thread=self.topic, limit=3, before=first["before"])
        self.assertEqual([m["id"] for m in second["messages"]], [self.reply.id] + [m.id for m in more[:2]])
        newer = self.h.world.push(TEAM, "user_example_c", "又一条", root_id=self.root.id, parent_id=self.root.id)
        wait_until(lambda: self.h.app.store.message(TEAM, newer.id), what="kept")
        third = self.h.c("thread.page", thread=self.topic, limit=3, before=second["before"])
        self.assertEqual([m["id"] for m in third["messages"]], [self.root.id])  # a newer line moves no page
        self.assertNotIn("before", third)
        with self.assertRaises(HubSideError):
            self.h.c("thread.page", thread=self.topic, limit=3, before="o3")
        whole = self.h.c("thread.page", thread=TEAM, limit=50)
        self.assertIn(self.main.id, [m["id"] for m in whole["messages"]])
        self.assertEqual(whole["chat"]["reply"], "matefeishu_send")

    def test_a_muted_group_s_topic_without_an_at_stays_out(self):
        h = signed_in(self, mute_ok=True, mutes={TEAM: True})
        root = h.world.push(TEAM, "user_example_a", "话题一")
        h.world.push(TEAM, "user_example_b", "回复", root_id=root.id, parent_id=root.id)
        wait_until(lambda: mutes(h, [TEAM]).get(TEAM), what="asked")
        t = things(h)
        self.assertEqual(t["%s:%s" % (TEAM, root.id)]["done"], "read")
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



class StoreTest(unittest.TestCase):
    def test_a_mutes_table_made_without_owner_gains_it(self):
        import sqlite3
        import tempfile

        from openmate_core.store import Store

        d = tempfile.mkdtemp()
        path = os.path.join(d, "openmate.db")
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE mutes (chat_id TEXT PRIMARY KEY, state TEXT NOT NULL, checked REAL NOT NULL)")
        db.commit()
        db.close()
        st = Store(path)
        st.set_mute("c1", "muted", "a1")
        self.assertEqual(st.mutes(["c1"], "a1")["c1"][0], "muted")
        st.close()


if __name__ == "__main__":
    unittest.main()
