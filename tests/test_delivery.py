"""Delivery between a backend and the core: a message a history read stored first still reaches /agent when its live
copy comes (once); a message the core could not store is not counted delivered."""

import time
import unittest

from harness import wait_until

from test_agent import agent, started


class LateLiveTest(unittest.TestCase):
    def test_history_first_then_live_reaches_agent_once(self):
        h = agent(self)
        m = h.world.make("chat_example_team", "me", "/agent 帮我看看这个", at=time.time())
        h.app.ingest([m], live=False)  # a catch-up read it in first
        self.assertEqual(h.http.starts(), [], "history alone starts nothing")
        h.world.listener(m)  # then its push
        started(h)
        h.world.listener(m)  # the same push again
        time.sleep(0.3)
        self.assertEqual(len(h.http.starts()), 1, "one start, not two")
        row = h.app.store.message("chat_example_team", m.id)
        self.assertEqual(row["from_history"], False, "its first live delivery taken")


class NotTakenTest(unittest.TestCase):
    def test_a_message_the_core_could_not_store_raises_to_the_backend(self):
        h = agent(self)
        real = h.app.store.add_messages

        def broken(*a, **k):
            raise RuntimeError("disk full")

        h.app.store.add_messages = broken
        self.addCleanup(setattr, h.app.store, "add_messages", real)
        m = h.world.make("chat_example_team", "user_example_b", "你好", at=time.time())
        with self.assertRaises(RuntimeError):
            h.world.listener(m)  # the backend sees it was not taken
        h.app.store.add_messages = real
        h.world.listener(m)  # delivered again: stored now
        wait_until(lambda: h.app.store.message("chat_example_team", m.id) is not None, what="stored on the retry")


if __name__ == "__main__":
    unittest.main()
