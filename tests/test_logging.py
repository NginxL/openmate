"""OPENMATE_LOG raises OpenMate's own loggers only: a library's DEBUG (websockets: handshakes, headers, frames with
the connection's ticket) stays off."""

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


if __name__ == "__main__":
    unittest.main()
