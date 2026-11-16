"""Fresh: what a page shows comes at once; a read that takes seconds (lark-cli, the hub across the border) happens behind
it, and a failed read keeps the last one."""

import threading
import time
import unittest

from harness import wait_until  # noqa: F401  (puts core on the path)
from openmate_core.ui import UI, Fresh


def inplace(fn):
    fn()


class Source:
    def __init__(self):
        self.n = 0
        self.fail = False

    def __call__(self):
        self.n += 1
        if self.fail:
            raise RuntimeError("down")
        return "v%d" % self.n


class FreshTest(unittest.TestCase):
    def make(self, spawn=inplace):
        self.now = [1000.0]
        self.key = ["day1"]
        self.src = Source()
        return Fresh(self.src, ahead=30, stale=900, clock=lambda: self.now[0], key=lambda: self.key[0], spawn=spawn)

    def test_a_first_read_waits_then_the_last_one_comes_at_once(self):
        f = self.make()
        self.assertEqual(f.get(), "v1")
        self.now[0] += 29
        self.assertEqual((f.get(), self.src.n), ("v1", 1), "young: no read")
        self.now[0] += 2
        self.assertEqual(f.get(), "v1", "older than ahead: the last one, and a read behind it")
        self.assertEqual((self.src.n, f.get()), (2, "v2"))

    def test_a_failed_read_keeps_the_last_one_until_stale(self):
        f = self.make()
        f.get()
        self.src.fail = True
        self.now[0] += 60
        self.assertEqual(f.get(), "v1")
        self.assertEqual(f.get(), "v1", "kept after the failed read behind it")
        self.now[0] += 900
        with self.assertRaises(RuntimeError, msg="stale: a draw waits, and says it failed"):
            f.get()

    def test_a_new_day_waits(self):
        f = self.make()
        f.get()
        self.key[0] = "day2"
        self.assertEqual((f.get(), self.src.n), ("v2", 2))

    def test_nothing_to_show_raises(self):
        f = self.make()
        self.src.fail = True
        with self.assertRaises(RuntimeError):
            f.get()

    def test_the_read_behind_does_not_hold_the_draw(self):
        gate = threading.Event()
        started = threading.Event()
        f = self.make(spawn=None)  # real threads
        f.get()
        slow = self.src

        def read():
            started.set()
            gate.wait(5)
            return slow()

        f.read = read
        self.now[0] += 60
        self.assertEqual(f.get(), "v1")
        self.assertTrue(started.wait(5))
        self.assertEqual(f.get(), "v1", "while the read behind is still going")
        gate.set()
        wait_until(lambda: f.get() == "v2", what="the read behind")
        self.assertEqual(self.src.n, 2, "one read behind, not one per draw")

    def test_warm_reads_before_anyone_looks(self):
        f = self.make()
        f.warm()
        self.assertEqual(self.src.n, 1)
        self.assertEqual((f.get(), self.src.n), ("v1", 1))
        self.src.fail = True
        self.make().warm()  # a failed first read is not raised from the background

    def test_kept_warm_until_stopped(self):
        f = self.make(spawn=None)
        stop = threading.Event()
        f.ahead = 0  # each get reads again (behind, in place below)
        f.spawn = lambda fn: threading.Thread(target=fn, daemon=True).start()
        f.warm(every=0.02, stop=stop)
        wait_until(lambda: self.src.n >= 3, what="read again and again")
        stop.set()
        time.sleep(0.1)
        n = self.src.n
        time.sleep(0.1)
        self.assertLessEqual(self.src.n, n + 1, "stopped")

    def test_ui_warms_each_page_once(self):
        calls = []

        class P:
            def render(self, params):
                return {}

            def warm(self):
                calls.append(self)

        p, q = P(), P()
        ui = UI.__new__(UI)
        ui.pages = {"a": p, "a-card": p, "b": q, "c": object()}
        ui.warm()
        self.assertEqual(calls, [p, q])


if __name__ == "__main__":
    unittest.main()
