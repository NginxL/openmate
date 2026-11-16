"""The changed-event debouncer and the store's own guarantees."""

import os
import shutil
import stat
import tempfile
import time
import unittest

import harness  # noqa: F401  (puts the core on the path)
from harness import Harness, fast, wait_until
from openmate_core.backend import Message
from openmate_core.events import Changed
from openmate_core.store import Store


class ChangedTest(unittest.TestCase):
    def test_at_most_one_per_interval_and_none_lost(self):
        got = []
        c = Changed(lambda s: got.append((time.monotonic(), s)), interval=0.2)
        self.addCleanup(c.stop)
        t0 = time.monotonic()
        for _ in range(30):
            c.mark("inbox")
            c.mark("chats")
            time.sleep(0.01)
        last_mark = time.monotonic()
        time.sleep(0.5)
        for scope in ("inbox", "chats"):
            times = [t for t, s in got if s == scope]
            self.assertGreaterEqual(len(times), 2)
            self.assertLessEqual(len(times), int((last_mark - t0) / 0.2) + 2)
            for a, b in zip(times, times[1:]):
                self.assertGreaterEqual(b - a, 0.19)
            self.assertGreaterEqual(times[-1], last_mark - 0.21, "the last change was dropped")

    def test_end_to_end_rate(self):
        h = Harness(self, timing=fast(debounce=0.3))
        h.sign_in()
        time.sleep(0.4)
        n0 = len(h.hub.changed("inbox"))
        for i in range(20):
            h.world.push("chat_example_chatter", "user_example_a", "刷屏 %d" % i)
        time.sleep(0.8)
        events = h.hub.changed("inbox")[n0:]
        self.assertGreaterEqual(len(events), 1)
        self.assertLessEqual(len(events), 4)
        for a, b in zip(events, events[1:]):
            self.assertGreaterEqual(b[0] - a[0], 0.28)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="openmate-store-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = Store(os.path.join(self.dir, "sub", "openmate.db"))
        self.addCleanup(self.store.close)

    def test_mode_and_dedupe(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.store.path).st_mode), 0o600)
        m = Message(id="m1", chat_id="c1", position=1, sender_id="u", text="一", at=time.time())
        self.assertEqual(len(self.store.add_messages([m], from_history=False)), 1)
        m2 = Message(id="m1", chat_id="c1", position=1, sender_id="u", text="改过的", at=time.time())
        self.assertEqual(self.store.add_messages([m2], from_history=True), [])
        self.assertEqual(self.store.message("c1", "m1")["text"], "改过的")  # not new, but its text is renewed (an edit)
        # the same id in another chat is another message
        self.assertEqual(len(self.store.add_messages([Message(id="m1", chat_id="c2", position=1)], False)), 1)

    def test_pages_by_position(self):
        now = time.time()
        msgs = [Message(id="m%d" % i, chat_id="c", position=i, text=str(i), at=now + i) for i in (5, 1, 3, 2, 4)]
        self.store.add_messages(msgs, False)
        self.assertEqual([r["position"] for r in self.store.messages_before("c", None, 3)], [3, 4, 5])
        self.assertEqual([r["position"] for r in self.store.messages_before("c", 3, 10)], [1, 2])
        rows = self.store.archive_rows(0, 10)
        self.assertEqual([r["id"] for r in rows], ["m5", "m1", "m3", "m2", "m4"])  # the archive goes by arrival

    def test_send_journal(self):
        self.assertTrue(self.store.send_claim("k", "x"))
        self.assertFalse(self.store.send_claim("k", "x"))
        self.store.send_release("k")
        self.assertTrue(self.store.send_claim("k", "x"))
        self.store.send_finish("k", "done", {"message_ref": "m"})
        self.store.send_release("k")  # a finished one is never released
        self.assertEqual(self.store.send_get("k")["result"], {"message_ref": "m"})
        self.assertTrue(self.store.send_claim("j", "x"))
        self.assertEqual(self.store.sends_interrupted(), 1)
        self.assertEqual(self.store.send_get("j")["state"], "uncertain")

    def test_runs_and_seen(self):
        run = {"id": "r1", "chat_id": "c", "request_id": "m", "thread_key": "m", "state": "running",
               "created": time.time(), "reply_ids": ["a"], "last_text": {"id": "i", "text": "t"}}
        self.store.run_put(run)
        got = self.store.run_get("r1")
        self.assertEqual(got["reply_ids"], ["a"])
        self.assertEqual(got["last_text"]["text"], "t")
        self.assertEqual([r["id"] for r in self.store.runs_in_state(["running"])], ["r1"])
        self.assertTrue(self.store.seen_add("x"))
        self.assertFalse(self.store.seen_add("x"))


if __name__ == "__main__":
    unittest.main()
