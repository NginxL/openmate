"""The user's switches: a connector turned off in Persona (initialize's active.connectors leaves it out) runs no live
link and no /agent; a member hub (another hub is primary) runs no /agent either."""

import _runtmp  # noqa: F401  (first: every temporary directory of this run in one, removed at its end)
import time
import unittest

from harness import TOKEN, Harness, wait_until


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


    def test_no_connectors_key_is_off(self):
        # Persona leaves the extension point out when none of its contributions is on
        h = signed_in_then_restarted(self, active={"ui": ["wechat-chats"]})
        time.sleep(0.4)
        self.assertEqual(h.app.link, "idle")
        self.assertFalse(h.world.connected)
        self.assertFalse(h.app.agent.enabled)

    def test_signing_in_with_the_connector_off_starts_no_link(self):
        h = Harness(self, initialize=False)
        h.initialize(active={"connectors": []})
        p = h.c("login.step", step="", values={})
        for _ in range(5):
            if p.get("done"):
                break
            p = h.c("login.step", step=p["step"])
        self.assertTrue(p.get("done"))
        self.assertIn(h.cid, h.hub.secrets, "signed in")
        time.sleep(0.3)
        self.assertEqual(h.app.link, "idle")
        self.assertFalse(h.world.connected)

    def test_a_member_hub_answers_nothing_left_running(self):
        from test_agent import ask, replies, running

        h = Harness(self, config={"agent": True, "agent_reply_suffix": "（AI）", "agent_reply_header": " ",
                                  "agent_ack_reaction": ""})
        h.sign_in()
        wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
        m = ask(h, "/agent 角色变了")
        run = running(h, m)
        h2 = h.restart(initialize=False)
        h2.initialize(hub={"build": 202611130000, "extension_points": {"connectors": 2}, "role": "member",
                           "timezone": "Asia/Shanghai", "locale": "zh-Hans", "url": h2.hub_url, "token": TOKEN})
        h2.wait_ok()
        h2.http.add(run["session_id"], "assistant", "text", "答案")
        h2.http.finish(run["session_id"])
        time.sleep(0.6)
        self.assertEqual([x for x in h2.sent() if "答案" in x["text"]], [], "nothing posted from a member hub")
        self.assertEqual(h2.app.store.run_get(run["id"])["state"], "running")


if __name__ == "__main__":
    unittest.main()
