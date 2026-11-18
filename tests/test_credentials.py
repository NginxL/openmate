"""Sealed credentials: rotations land in order and the newest wins; logout's delete is the last write, so a rotation
from before it never brings the sign-in back; a new sign-in writes again."""

import threading
import time
import unittest

from harness import Harness, secret_json, wait_until


def token(h):
    data = h.hub.secrets.get(h.cid)
    return None if data is None else secret_json(data).get("fixture_token")


def creds(name):
    return {"fixture_token": name, "user": "user_example_me"}


def quiet(h):
    wait_until(lambda: h.app._creds_writer is None, what="the credentials writer")


class CredentialsTest(unittest.TestCase):
    def test_the_newest_rotation_wins(self):
        h = Harness(self)
        h.sign_in()
        slow = {"r1": 0.3, "r2": 0.15}
        h.hub.save_hook = lambda c: time.sleep(slow.get(c.get("fixture_token"), 0))
        for name in ("r1", "r2", "r3"):
            h.app._credentials_rotated(creds(name))
        quiet(h)
        self.assertEqual(token(h), "r3")
        self.assertEqual(h.app._credentials["fixture_token"], "r3")

    def test_logout_is_the_last_write(self):
        h = Harness(self)
        h.sign_in()
        started = threading.Event()

        def slow_save(c):
            started.set()
            time.sleep(0.4)

        h.hub.save_hook = slow_save
        h.app._credentials_rotated(creds("r1"))
        self.assertTrue(started.wait(2))
        self.assertEqual(h.c("logout"), {})
        quiet(h)
        time.sleep(0.2)
        self.assertNotIn(h.cid, h.hub.secrets, "a rotation from before the logout brought the sign-in back")
        # after the logout, nothing the adapter hands over is written
        h.hub.save_hook = None
        h.app._credentials_rotated(creds("late"))
        self.assertFalse(h.app._credentials_rotated_now(creds("late-sync")))
        quiet(h)
        self.assertNotIn(h.cid, h.hub.secrets)

    def test_a_rotation_from_before_a_sign_in_is_not_written(self):
        h = Harness(self)
        h.sign_in()
        old = h.app._rotation(creds("old"))
        h.c("logout")
        h.sign_in()
        new = token(h)
        self.assertFalse(h.app._seal_rotation(*old))
        self.assertEqual(token(h), new)

    def test_a_new_sign_in_writes_rotations_again(self):
        h = Harness(self)
        h.sign_in()
        h.c("logout")
        h.sign_in()
        self.assertTrue(h.app._credentials_rotated_now(creds("r-sync")))
        self.assertEqual(token(h), "r-sync")
        h.app._credentials_rotated(creds("r-bg"))
        quiet(h)
        self.assertEqual(token(h), "r-bg")

    def test_rotations_during_a_qr_sign_in_are_written(self):
        # WeChat seals the accepted sign-in (a rotation) before login_finish answers: after a logout, a new QR opens it
        h = Harness(self)
        h.sign_in()
        h.c("logout")
        h.c("login.step", step="", values={})
        self.assertTrue(h.app._credentials_rotated_now(creds("accepted")))
        self.assertEqual(token(h), "accepted")


if __name__ == "__main__":
    unittest.main()
