"""OPENMATE_LOG raises OpenMate's own loggers only: a library's DEBUG (websockets: handshakes, headers, frames with
the connection's ticket) stays off."""

try:  # first: every temporary directory of this run in one, removed at its end
    import _runtmp  # noqa: F401
except ImportError:  # (started by its path from elsewhere: its directory is not on the path)
    pass
import logging
import unittest

from openmate_core import main


class LoggingTest(unittest.TestCase):
    def setUp(self):
        root, om = logging.getLogger(), logging.getLogger("openmate")
        saved = (root.level, om.level)

        def back():
            root.setLevel(saved[0])
            om.setLevel(saved[1])

        self.addCleanup(back)

    def test_debug_is_openmate_only(self):
        self.assertEqual(main.setup_logging("debug"), logging.DEBUG)
        self.assertTrue(logging.getLogger("openmate.larkx").isEnabledFor(logging.DEBUG))
        self.assertFalse(logging.getLogger("websockets.client").isEnabledFor(logging.DEBUG))
        self.assertTrue(logging.getLogger("websockets.client").isEnabledFor(logging.INFO))

    def test_quieter_and_unknown(self):
        main.setup_logging("WARNING")
        self.assertFalse(logging.getLogger("openmate.app").isEnabledFor(logging.INFO))
        self.assertFalse(logging.getLogger("urllib3").isEnabledFor(logging.INFO))
        self.assertEqual(main.setup_logging("chatty"), logging.INFO, "an unknown level is INFO, not a crash")


class HandlerFilterTest(unittest.TestCase):
    def test_a_library_set_to_debug_is_still_not_written(self):
        import io

        root = logging.getLogger()
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        root.addHandler(h)
        lib = logging.getLogger("websockets.client")
        saved = (root.level, lib.level, logging.getLogger("openmate").level)

        def back():
            root.removeHandler(h)
            root.setLevel(saved[0])
            lib.setLevel(saved[1])
            logging.getLogger("openmate").setLevel(saved[2])

        self.addCleanup(back)
        main.setup_logging("DEBUG")
        lib.setLevel(logging.DEBUG)  # a library (or its user) asked for its own DEBUG
        lib.debug("> GET /ws?ticket=SECRET")
        lib.info("connected")
        logging.getLogger("openmate.larkx").debug("ours")
        out = buf.getvalue()
        self.assertNotIn("SECRET", out)
        self.assertIn("connected", out)
        self.assertIn("ours", out)


if __name__ == "__main__":
    unittest.main()
