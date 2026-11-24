"""Fresh: what a page shows comes at once; a read that takes seconds (lark-cli, the hub across the border) happens behind
it, and a failed read keeps the last one."""

import _runtmp  # noqa: F401  (first: every temporary directory of this run in one, removed at its end)
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
        g = self.make()
        self.src.fail = True  # this new value's own source fails
        g.warm()  # a failed first read is not raised from the background
        self.assertEqual(self.src.n, 1, "it did read, and failed quietly")

    def test_one_warm_loop_per_value_and_it_rests_while_nobody_looks(self):
        from openmate_core import ui as ui_mod

        f = self.make(spawn=None)
        f.ahead = 0
        loops = []
        f.spawn = lambda fn: (loops.append(fn), threading.Thread(target=fn, daemon=True).start())
        stop = threading.Event()
        self.addCleanup(stop.set)
        f.warm(every=0.02, stop=stop)
        f.warm(every=0.02, stop=stop)  # another page asks too
        self.assertEqual(len([x for x in loops if x.__name__ == "run"]), 1, "one loop")
        wait_until(lambda: self.src.n >= 2, what="reads while looked at")
        self.now[0] += ui_mod.WARM_IDLE + 1  # nobody looked for a long while
        time.sleep(0.1)
        n = self.src.n
        time.sleep(0.15)
        self.assertLessEqual(self.src.n, n + 1, "rests")
        f.get()  # someone looks
        wait_until(lambda: self.src.n >= n + 3, what="reads again")

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


class WarmOwnerTest(unittest.TestCase):
    def test_a_stopped_warm_does_not_read_and_a_one_off_keeps_the_loop(self):
        src = Source()
        f = Fresh(src, ahead=0, stale=900, clock=lambda: 1000.0, spawn=lambda fn: threading.Thread(target=fn, daemon=True).start())
        stop = threading.Event()
        stop.set()
        f.warm(every=0.02, stop=stop)
        time.sleep(0.1)
        self.assertEqual(src.n, 0, "stopped already: no read")
        live = threading.Event()
        self.addCleanup(live.set)
        f.warm(every=0.02, stop=live)  # a loop
        wait_until(lambda: src.n >= 1, what="the loop reads")
        f.warm()  # a one-off ends and must not free the loop's place
        time.sleep(0.1)
        self.assertTrue(f._warming)
        spawned = []
        f.spawn = lambda fn: spawned.append(fn)
        f.warm(every=0.02, stop=live)
        self.assertEqual(spawned, [], "no second loop")

    def test_a_draw_during_the_warm_read_counts_as_looking(self):
        from openmate_core import ui as ui_mod

        now = [5000.0]
        gate = threading.Event()

        def read():
            gate.wait(2)
            return "v"

        entered = threading.Event()

        def read_marked():
            entered.set()
            return read()

        f = Fresh(read_marked, ahead=0, stale=900, clock=lambda: now[0],
                  spawn=lambda fn: threading.Thread(target=fn, daemon=True).start())
        stop = threading.Event()
        self.addCleanup(stop.set)
        f.warm(every=60.0, stop=stop)
        self.assertTrue(entered.wait(2), "the warm read is under way")
        now[0] += ui_mod.WARM_IDLE + 10
        from openmate_core.ui import NotYet
        with self.assertRaises(NotYet):
            f.get(wait=0.01)  # someone looks meanwhile (the read is still at the gate)
        looked = f._looked
        gate.set()
        wait_until(lambda: f._kept is not None, what="the warm read is in")
        time.sleep(0.05)
        self.assertEqual(f._looked, looked, "the draw's look is not undone by the warm read")
        self.assertGreaterEqual(looked, now[0])


class BoundedWaitTest(unittest.TestCase):
    def test_a_first_read_is_waited_for_a_bounded_time(self):
        import threading
        from openmate_core.ui import Fresh, NotYet

        gate = threading.Event()
        reads = []

        def read():
            gate.wait(5)
            reads.append(1)
            return "value"

        f = Fresh(read, 30.0, 900.0)
        with self.assertRaises(NotYet):
            f.get(wait=0.1)
        with self.assertRaises(NotYet):
            f.get(wait=0.1)  # the same read still on its way: not a second one
        gate.set()
        for _ in range(50):
            try:
                self.assertEqual(f.get(wait=0.1), "value")
                break
            except NotYet:
                time.sleep(0.05)
        self.assertEqual(len(reads), 1)

    def test_a_failed_first_read_raises_its_error(self):
        from openmate_core.ui import Fresh

        def read():
            raise ValueError("bad")

        f = Fresh(read, 30.0, 900.0)
        with self.assertRaises(ValueError):
            f.get(wait=1.0)


class FreshKeysTest(unittest.TestCase):
    def test_a_failure_is_said_for_a_while_and_another_key_reads_anew(self):
        from openmate_core.ui import Fresh

        now = [1000.0]
        key = ["day1"]
        reads = []

        def read():
            reads.append(key[0])
            if key[0] == "day1":
                raise ValueError("cli failed")
            return "events of " + key[0]

        f = Fresh(read, 30.0, 900.0, clock=lambda: now[0], key=lambda: key[0], spawn=lambda fn: fn())
        with self.assertRaises(ValueError):
            f.get(wait=1.0)
        with self.assertRaises(ValueError):
            f.get(wait=1.0)  # said again, not read again at once
        self.assertEqual(reads, ["day1"])
        key[0] = "day2"  # a new day: its own read, not yesterday's failure
        self.assertEqual(f.get(wait=1.0), "events of day2")
        self.assertEqual(reads, ["day1", "day2"])


if __name__ == "__main__":
    unittest.main()
