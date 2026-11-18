"""The user's switches: a connector turned off in Persona (initialize's active.connectors leaves it out) runs no live
link and no /agent; a member hub (another hub is primary) runs no /agent either."""

import time
import unittest

from harness import Harness, wait_until


def signed_in_then_restarted(test, **over):
    h = Harness(test, config={"agent": True})
    h.sign_in()
    wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
    h2 = h.restart(initialize=False)
    h2.initialize(**over)
    return h2


class SwitchesTest(unittest.TestCase):
    def test_a_connector_turned_off_runs_no_link_and_no_agent(self):
        h = signed_in_then_restarted(self, active={"connectors": []})
        time.sleep(0.4)
        self.assertEqual(h.app.creds, "present", "still signed in")
        self.assertEqual(h.app.link, "idle", "no live link")
        self.assertFalse(h.world.connected)
        self.assertFalse(h.app.agent.enabled)

    def test_on_by_default_and_when_named(self):
        h = signed_in_then_restarted(self)
        wait_until(lambda: h.app.link == "up", what="the link")
        self.assertTrue(h.app.agent.enabled)

    def test_a_member_hub_runs_no_agent(self):
        h = signed_in_then_restarted(self, hub={"build": 202611130000, "extension_points": {"connectors": 2},
                                                "role": "member", "timezone": "Asia/Shanghai", "locale": "zh-Hans"})
        self.assertFalse(h.app.agent.enabled)


if __name__ == "__main__":
    unittest.main()
