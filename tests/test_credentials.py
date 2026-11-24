"""Sealed credentials: rotations land in order and the newest wins; logout's delete is the last write, so a rotation
from before it never brings the sign-in back; a new sign-in writes again."""

import _runtmp  # noqa: F401  (first: every temporary directory of this run in one, removed at its end)
import threading
import time
import unittest

from harness import CID, Harness, secret_json, wait_until


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


    def test_a_logout_while_the_sign_in_finishes_voids_it(self):
        from harness import HubSideError

        h = Harness(self)
        b = h.app.backend
        real = b.login_finish
        finishing = threading.Event()

        def slow_finish(state):
            finishing.set()
            time.sleep(0.4)
            return real(state)

        b.login_finish = slow_finish
        p = h.c("login.step", step="", values={})
        out = {}

        def poll():
            q = p
            try:
                for _ in range(5):
                    q = h.c("login.step", step=q["step"])
                    if q.get("done"):
                        break
                out["p"] = q
            except HubSideError as e:
                out["error"] = e.message

        t = threading.Thread(target=poll)
        t.start()
        self.assertTrue(finishing.wait(3))
        h.c("logout")  # while login_finish runs
        t.join(5)
        self.assertIn("作废", out.get("error", ""), out)
        self.assertNotIn(h.cid, h.hub.secrets)
        self.assertFalse(h.app._credentials_rotated_now(creds("late")), "still closed")
        self.assertNotIn(h.cid, h.hub.secrets)

    def test_a_failed_write_is_tried_again(self):
        h = Harness(self)
        h.sign_in()
        h.hub.secrets_fail = True
        h.app._credentials_rotated(creds("r-retry"))
        time.sleep(0.08)
        h.hub.secrets_fail = False
        quiet(h)
        self.assertEqual(token(h), "r-retry")


    def test_a_logout_while_the_account_is_bound_leaves_no_link(self):
        from harness import HubSideError

        h = Harness(self)
        real = h.app._bind_account
        binding = threading.Event()

        def slow_bind(acct):
            binding.set()
            time.sleep(0.4)
            return real(acct)

        h.app._bind_account = slow_bind
        p = h.c("login.step", step="", values={})
        out = {}

        def poll():
            q = p
            try:
                for _ in range(5):
                    q = h.c("login.step", step=q["step"])
                    if q.get("done"):
                        break
                out["p"] = q
            except HubSideError as e:
                out["error"] = e.message

        t = threading.Thread(target=poll)
        t.start()
        self.assertTrue(binding.wait(3))
        h.c("logout")
        t.join(5)
        time.sleep(0.3)
        self.assertIn("作废", out.get("error", ""), out)
        self.assertEqual(h.app.link, "idle")
        self.assertFalse(h.world.connected, "no link after the logout")
        self.assertNotIn(h.cid, h.hub.secrets)

    def test_a_stale_poll_after_a_logout_does_not_reopen(self):
        from harness import HubSideError

        h = Harness(self)
        h.sign_in()
        p = h.c("login.step", step="", values={})  # another sign-in started, then a logout
        h.c("logout")
        with self.assertRaises(HubSideError) as e:
            h.c("login.step", step=p["step"])  # the page polls on
        self.assertIn("退出登录", e.exception.message)
        self.assertFalse(h.app._credentials_rotated_now(creds("late")))
        self.assertNotIn(h.cid, h.hub.secrets)
        h.sign_in()  # the user's own new QR does
        self.assertIn(h.cid, h.hub.secrets)


    def test_a_failed_save_lets_the_adapter_go(self):
        """The adapter took the sign-in, the core could not keep it: the adapter lets it go (nothing is sent as an
        account the core does not know it has)."""
        from harness import HubSideError

        h = Harness(self)
        outs = []
        real = h.app.backend.logout
        h.app.backend.logout = lambda: outs.append(1) or real()
        h.hub.secrets_fail = True
        p = h.c("login.step", step="", values={})
        with self.assertRaises(HubSideError):
            for _ in range(5):
                p = h.c("login.step", step=p["step"])
                if p.get("done"):
                    break
        self.assertEqual(outs, [1])

    def test_a_sign_in_with_nothing_to_scan_reads_an_unread_saved_one_first(self):
        from harness import HubSideError
        from openmate_core.backend import LoginDone
        from openmate_core.fixture import FixtureBackend, FixtureWorld

        calls = []

        class Direct(FixtureBackend):
            signs_in_elsewhere = True

            def login_direct(self):
                calls.append(self.saved_credentials)
                return LoginDone(credentials={"fixture_token": "d", "user": "user_example_me"}, account=self.world.me)

        world = FixtureWorld()
        h = Harness(self, world=world, backend_factory=lambda cfg, d: Direct(world), initialize=False)
        h.hub.secrets_fail = True
        h.initialize()
        wait_until(lambda: h.app.creds == "error", what="the read failed")
        with self.assertRaises(HubSideError) as e:
            h.c("login.step", step="")
        self.assertIn("读取保存的登录信息失败", e.exception.message)
        self.assertEqual(calls, [])  # nothing taken while the saved one is unread
        h.hub.secrets_fail = False
        self.assertTrue(h.c("login.step", step="")["done"])
        self.assertEqual(len(calls), 1)

    def test_an_unread_saved_sign_in_read_again_goes_on_with_it(self):
        from openmate_core.backend import LoginDone
        from openmate_core.fixture import FixtureBackend, FixtureWorld

        calls = []

        class Direct(FixtureBackend):
            signs_in_elsewhere = True

            def login_direct(self):
                calls.append(self.saved_credentials)
                return LoginDone(credentials={"fixture_token": "d", "user": "user_example_me"}, account=self.world.me)

        world = FixtureWorld()
        h = Harness(self, world=world, backend_factory=lambda cfg, d: Direct(world), initialize=False)
        import base64
        import json as _json

        h.hub.secrets[CID] = base64.b64encode(_json.dumps({"fixture_token": "saved", "user": "user_example_me"})
                                              .encode()).decode()
        h.hub.secrets_fail = True
        h.initialize()
        wait_until(lambda: h.app.creds == "error", what="the read failed")
        h.hub.secrets_fail = False
        self.assertTrue(h.c("login.step", step="")["done"])
        self.assertEqual(calls, [{"fixture_token": "saved", "user": "user_example_me"}])

    def test_a_qr_sign_in_is_not_held_by_an_unread_saved_one(self):
        h = Harness(self, initialize=False)
        h.hub.secrets_fail = True
        h.initialize()
        wait_until(lambda: h.app.creds == "error", what="the read failed")
        self.assertEqual(h.c("login.step", step="")["kind"], "qr")

    def test_a_failed_delete_keeps_the_saved_sign_in_for_the_adapter(self):
        from harness import HubSideError

        h = Harness(self)
        h.sign_in()
        self.assertIsNotNone(h.app.backend.saved_credentials)
        self.assertEqual(h.app.backend.core_account, "user_example_me")
        h.hub.secrets_fail = True
        with self.assertRaises(HubSideError):
            h.c("logout")
        self.assertIsNotNone(h.app.backend.saved_credentials)  # still saved: the adapter still checks against it
        h.hub.secrets_fail = False
        h.c("logout")
        self.assertIsNone(h.app.backend.saved_credentials)

    def test_a_failed_save_then_a_new_qr_signs_in(self):
        from harness import HubSideError

        h = Harness(self)
        h.hub.secrets_fail = True
        p = h.c("login.step", step="", values={})
        with self.assertRaises(HubSideError) as e:
            for _ in range(5):
                p = h.c("login.step", step=p["step"])
                if p.get("done"):
                    break
        self.assertIn("保存登录信息失败", e.exception.message)
        h.hub.secrets_fail = False
        for _ in range(6):  # the page polls on: a new QR, scanned
            p = h.c("login.step", step=p.get("step") or "qr")
            if p.get("done"):
                break
        self.assertTrue(p.get("done"), p)
        self.assertIn(h.cid, h.hub.secrets)


if __name__ == "__main__":
    unittest.main()
