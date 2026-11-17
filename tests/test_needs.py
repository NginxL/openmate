"""需要我 keeps what still needs the user: an @me or a direct message older than needs_hours goes, and so does a
direct chat where the other side only answered the user ("27层" after "几楼呀")."""

import time
import unittest

from harness import Harness, wait_until
from openmate_core.backend import Chat
from openmate_core.fixture import FixtureWorld
from openmate_core.ui import ChatsToday, asks_something, talk_closed

T = 1_800_000_000.0


def msg(text, mine=False, at=0.0, **kw):
    return dict({"text": text, "mine": mine, "at": T + at, "type": "TEXT", "sender_name": "我" if mine else "同事"}, **kw)


class TalkClosedTest(unittest.TestCase):
    def test_an_answer_soon_after_the_users_question_closes_the_talk(self):
        self.assertTrue(talk_closed([msg("在吗"), msg("几楼呀", mine=True, at=60), msg("一会发你", at=120), msg("27层", at=180)]))
        self.assertTrue(talk_closed([msg("我打算这么搞", mine=True), msg("我研究一下，确实该买一个", at=120)]))

    def test_a_question_a_request_or_a_late_or_long_answer_still_needs_the_user(self):
        self.assertFalse(talk_closed([msg("几楼呀", mine=True), msg("27层，你几点到？", at=60)]), "a question back")
        self.assertFalse(talk_closed([msg("好的", mine=True), msg("https://example.com/approval/1", at=30), msg("给点一波", at=40)]),
                         "a request")
        self.assertFalse(talk_closed([msg("好的", mine=True), msg("另一件事", at=2 * 3600)]), "hours later: a new thing")
        self.assertFalse(talk_closed([msg("好的", mine=True)] + [msg("第%d句" % i, at=60 + i) for i in range(4)]), "a long answer")
        self.assertFalse(talk_closed([msg("在吗"), msg("有空吗")]), "the user never spoke")
        self.assertFalse(talk_closed([msg("在吗", mine=True)]), "the user spoke last: nothing to close")

    def test_asks_something(self):
        self.assertTrue(asks_something(msg("能不能帮我看下")))
        self.assertTrue(asks_something(msg("收到", at_me=True)))
        self.assertFalse(asks_something(msg("27层")))


class NeedsTest(unittest.TestCase):
    def setUp(self):
        w = FixtureWorld()
        w.add_chat(Chat("dm_closed", "dm", "接上话的同事", peer_id="user_example_a"))
        w.make("dm_closed", "me", "几楼呀", ago=600)
        w.make("dm_closed", "user_example_a", "27层", ago=540)
        w.add_chat(Chat("dm_old", "dm", "昨天的同事", peer_id="user_example_b"))
        w.make("dm_old", "user_example_b", "在吗，有个事", ago=30 * 3600)
        w.add_chat(Chat("group_old", "group", "老群", members=9))
        w.make("group_old", "user_example_b", "@示例用户 看下这个", ago=3 * 86400 - 600, at_me=True)
        w.make("group_old", "user_example_c", "今天的闲聊", ago=60)  # the group is active, its @me is old
        self.h = Harness(self, world=w)
        self.h.sign_in()
        wait_until(lambda: self.h.app._catchup_done.is_set(), what="catch-up")

    def titles(self, chats):
        return [t["_chat"]["name"] for t in chats.needs()]

    def test_old_and_closed_ones_are_left_out_and_counted(self):
        chats = ChatsToday(self.h.app)
        got = self.titles(chats)
        for gone in ("接上话的同事", "昨天的同事", "老群"):
            self.assertNotIn(gone, got)
        self.assertIn("示例同事甲", got, "the seed's unanswered direct chat stays")
        self.assertEqual((chats.closed, chats.expired), (1, 2))
        self.h.app.config["needs_hours"] = 72
        got = self.titles(chats)
        self.assertIn("昨天的同事", got)
        self.assertIn("老群", got)
        self.assertEqual(chats.expired, 0)
        self.h.app.config["needs_hours"] = "x"
        self.assertEqual(chats.needs_hours(), 24)


if __name__ == "__main__":
    unittest.main()
