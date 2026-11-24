"""The catch-up after a reconnect: chats the service lists that the store never saw (a direct chat opened while
away), each chat read back to what is stored (a busy group's whole outage), a chat that cannot be read skipped, and
the first time done only once every read worked."""

import _runtmp  # noqa: F401  (first: every temporary directory of this run in one, removed at its end)
import unittest

from harness import Harness, wait_until
from openmate_core.backend import BackendError, Chat


def stored(h, chat_id):
    return [r["id"] for r in h.app.store._all("SELECT id FROM messages WHERE chat_id = ? ORDER BY position", (chat_id,))]


def caught_up(h):
    wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")


def away(h, while_down):
    """Drop the link, run while_down (the world changes), bring it back and wait for its catch-up."""
    caught_up(h)
    h.world.listen_error = BackendError("网络不通", "unreachable")
    h.world.drop()
    wait_until(lambda: h.status()["state"] == "error", what="down")
    h.app._catchup_done.clear()
    while_down()
    h.world.listen_error = None
    h.wait_ok()
    caught_up(h)


class CatchUpTest(unittest.TestCase):
    def test_a_chat_opened_while_away_is_read(self):
        h = Harness(self)
        h.sign_in()
        msgs = []

        def opened():
            h.world.people["user_example_d"] = "示例同事丁"
            h.world.add_chat(Chat("chat_example_dm_d", "dm", "示例同事丁", peer_id="user_example_d"))
            msgs.append(h.world.make("chat_example_dm_d", "user_example_d", "在吗？新开的私聊"))

        away(h, opened)
        self.assertEqual(stored(h, "chat_example_dm_d"), [msgs[0].id])
        thing = [t for t in h.c("inbox.collect")["things"] if t["thread"] == "chat_example_dm_d"]
        self.assertTrue(thing, "in the inbox")

    def test_a_busy_chat_is_read_back_to_what_is_stored(self):
        h = Harness(self)
        h.sign_in()
        missed = []

        def busy():
            for i in range(100):
                missed.append(h.world.make("chat_example_chatter", "user_example_b", "断线时的第%d条" % i))

        away(h, busy)
        ids = set(stored(h, "chat_example_chatter"))
        self.assertEqual([m.id for m in missed if m.id not in ids], [], "every message of the outage")

    def test_the_pages_and_the_budget_bound_it(self):
        h = Harness(self)
        h.sign_in()
        t = h.app.timing
        t.catchup_pages, t.catchup_budget = 2, 1000

        def flood():
            for i in range(200):
                h.world.make("chat_example_chatter", "user_example_b", "刷屏第%d条" % i)

        away(h, flood)
        reads = [c for c in h.world.history_calls if c[0] == "chat_example_chatter" and c[2] == t.catchup_messages]
        self.assertEqual(len(reads), 2, "two pages, then the next chat")

    def test_a_chat_that_cannot_be_read_does_not_stop_the_others(self):
        h = Harness(self)
        h.sign_in()
        caught_up(h)
        cut = h.app.store.meta_get("caught_up_at")
        bad = h.app.store.chats_since(0, 1)[0]["id"]  # the one read first
        other = "chat_example_dm_b" if bad != "chat_example_dm_b" else "chat_example_dm_a"
        got = []

        def two():
            h.world.history_fail_chats.add(bad)
            got.append(h.world.make(other, "user_example_b", "断线时的私聊"))
            h.world.make(bad, "user_example_a", "读不到的消息")  # the newest: first in the service's list too

        away(h, two)
        self.assertIn(got[0].id, stored(h, other))
        self.assertEqual(h.app.store.meta_get("caught_up_at"), cut, "not whole: the next one reads from the same cut")

    def test_old_messages_of_a_chat_new_to_the_store_are_not_kept(self):
        h = Harness(self)
        h.sign_in()
        new = []

        def opened():
            h.world.add_chat(Chat("chat_example_old_group", "group", "示例老群", members=9))
            h.world.make("chat_example_old_group", "user_example_a", "很早以前的", ago=2 * 86400)
            new.append(h.world.make("chat_example_old_group", "user_example_a", "断线时的"))

        away(h, opened)
        self.assertEqual(stored(h, "chat_example_old_group"), [new[0].id])

    def test_the_first_time_counts_only_when_the_recent_chats_were_read(self):
        h = Harness(self, initialize=False)
        h.world.recent_fail = True
        h.initialize()
        h.sign_in()
        caught_up(h)
        self.assertNotEqual(h.app.store.meta_get("initial_history_done"), "1")
        h.world.recent_fail = False
        away(h, lambda: None)
        self.assertEqual(h.app.store.meta_get("initial_history_done"), "1")
        self.assertTrue(stored(h, "chat_example_dm_b"), "the initial history, read on the next link up")


if __name__ == "__main__":
    unittest.main()
