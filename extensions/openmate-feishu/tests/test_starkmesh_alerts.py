"""The installed Feishu pages share a StarkMesh read with the background notifier."""

import json
import tempfile
import threading
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from test_starkmesh import INPLACE, NOW, TZ, at, items, starkmesh
import openmate_feishu
from openmate_core.hub import HubError


HOUR = 3600
LOW = ["sm-low-5h", "sm-low-7d", "sm-low-fable"]


def account(key=1, **fields):
    return dict({"id": key, "name": "Claude %d" % key, "platform": "anthropic", "status": "active",
                 "type": "oauth", "schedulable": True}, **fields)


class Hub:
    def __init__(self):
        self.requests = []
        self.error = None
        self.result = {"sent": True}

    def request(self, method, path, body, **kwargs):
        self.requests.append((method, path, body))
        if self.error is not None:
            raise self.error
        return self.result


class StarkMeshAlertsTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.app = types.SimpleNamespace(
            config={"starkmesh_url": "http://127.0.0.1:18080", "starkmesh_alerts": True, "starkmesh_alert_left": 10},
            secrets={"starkmesh_key": "test-key"}, tz=TZ, cid="matefeishu", closing=threading.Event(),
            hub=Hub(), data_dir=folder.name)
        self.addCleanup(self.app.closing.set)
        self.path = Path(folder.name) / "starkmesh-alerts.json"
        self.now = NOW
        self.reads = 0
        self.accounts = [account()]
        self.used = (0, 0, 0)
        self.resets_in = (12 * HOUR, 40 * HOUR, 40 * HOUR)
        self.omit = set()
        self.cut = False
        self.failed = set()
        self.error = None
        self.restart()

    def restart(self):
        self.mesh = starkmesh.StarkMesh(self.app, clock=lambda: self.now, spawn=INPLACE)
        self.mesh._read_once = self.read
        with patch.object(starkmesh, "StarkMesh", return_value=self.mesh):
            self.pages = openmate_feishu._agenda_pages(self.app)
        self.page = self.pages["starkmesh"]

    def read(self):
        self.reads += 1
        if self.error is not None:
            raise self.error
        usage = {
            str(a["id"]): {
                key: {"utilization": used, "resets_at": starkmesh._iso(self.now + reset)}
                for (key, _), used, reset in zip(starkmesh.WINDOWS, self.used, self.resets_in) if key not in self.omit
            }
            for a in self.accounts
        }
        snap = starkmesh.Snapshot(self.accounts, usage, {}, self.now)
        snap.cut = self.cut
        snap.usage_failed = self.failed
        return snap

    def check(self, elapsed=0):
        self.now += elapsed
        alerts = getattr(self.page, "alerts", None)
        if alerts is not None:
            alerts.check()

    def keys(self):
        return [body["id"] for _, _, body in self.app.hub.requests]

    def test_switch_off_still_preloads_the_configured_mesh(self):
        self.app.config["starkmesh_alerts"] = False
        warmups = []
        self.mesh._fresh.spawn = warmups.append
        self.page.warm()
        self.pages["starkmesh-card"].warm()
        self.assertEqual(len(warmups), 1)
        with patch.object(self.app.closing, "wait", return_value=True) as wait:
            warmups[0]()
        wait.assert_called_once_with(300.0)
        self.assertIsNone(self.page.alerts._thread)
        self.assertEqual(self.reads, 1)
        self.assertTrue(self.page.usage({})["accounts"][0]["windows"])
        self.check()
        self.assertEqual((self.reads, self.keys()), (1, []))
        self.assertFalse(self.path.exists())

    def test_missing_setup_starts_neither_preload_nor_alerts(self):
        self.app.secrets.clear()
        warmups = []
        self.mesh._fresh.spawn = warmups.append
        self.page.warm()
        self.check()
        self.assertEqual(warmups, [])
        self.assertIsNone(self.page.alerts._thread)
        self.assertEqual((self.reads, self.keys()), (0, []))
        self.assertIn("后台告警已开，尚未检查", items(self.page.render({}))[-1]["text"])

    def test_startup_creates_one_daemon_and_closing_interrupts_wait(self):
        alerts = self.page.alerts
        self.assertIsNone(alerts._thread)
        warmups = []
        self.mesh._fresh.spawn = warmups.append
        closing = self.app.closing
        waiting = threading.Event()
        wait = closing.wait

        def wait_for_close(timeout):
            waiting.set()
            return wait(timeout)

        with patch.object(closing, "wait", side_effect=wait_for_close):
            self.page.warm()
            thread = alerts._thread
            self.assertIsNotNone(thread)
            try:
                self.assertTrue(thread.daemon)
                self.assertTrue(waiting.wait(2))
                self.pages["starkmesh-card"].warm()
                self.assertIs(alerts._thread, thread)
                self.assertEqual(len(warmups), 1)
            finally:
                closing.set()
                thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.reads, 1)

    def test_foreground_and_alerts_share_recent_read(self):
        self.page.usage({})
        self.check(10)
        self.assertEqual(self.reads, 1)
        self.check(300)
        self.page.usage({})
        self.assertEqual(self.reads, 2)

    def test_low_windows_notify_once_and_rearm_at_hysteresis(self):
        self.used = (90, 92, 95)
        self.check()
        self.assertEqual(self.keys(), LOW)
        self.check(300)
        self.assertEqual(self.keys(), LOW)
        self.used = (88, 88, 88)
        self.check(300)
        self.assertIn("上次检查 " + at(600), items(self.page.render({}))[-1]["text"])
        self.assertIn("5 小时额度低", items(self.page.render({}))[-1]["text"])
        self.used = (85, 85, 85)
        self.check(300)
        self.assertNotIn("额度低", items(self.page.render({}))[-1]["text"])
        self.used = (90, 92, 95)
        self.check(6 * HOUR)
        self.assertEqual(self.keys(), LOW + LOW)

    def test_low_windows_skip_partial_or_failed_reads_and_check_complete_windows(self):
        self.used = (99, 99, 99)
        self.cut = True
        self.check()
        self.assertEqual(self.keys(), [])
        self.cut = False
        self.failed = {"1"}
        self.check(300)
        self.assertEqual(self.keys(), [])
        self.failed.clear()
        self.omit = {"seven_day"}
        self.check(300)
        self.assertEqual(self.keys(), [LOW[0], LOW[2]])
        self.omit = {"seven_day_fable"}
        self.check(300)
        self.assertEqual(self.keys(), [LOW[0], LOW[2], LOW[1]])

    def test_window_reset_rearms_even_if_the_next_sample_is_already_low(self):
        self.used = (99, 0, 0)
        self.resets_in = (4 * HOUR, 40 * HOUR, 40 * HOUR)
        self.check()
        self.check(6 * HOUR)
        self.assertEqual(self.keys(), LOW[:1] * 2)

    def test_threshold_is_read_each_round(self):
        self.used = (87, 0, 0)
        self.check()
        self.app.config["starkmesh_alert_left"] = 13
        self.check(300)
        self.assertEqual(self.keys(), LOW[:1])

    def test_account_baseline_and_persistent_transitions_are_aggregated(self):
        self.accounts = [account(key) for key in range(1, 10)] + [account(10, status="error")]
        self.check()
        self.assertEqual(self.keys(), [])
        deadline = starkmesh._iso(self.now + HOUR)
        self.accounts = [account(1, status="error"), account(2, auto_pause_on_expired=True, expires_at=self.now + 60),
                         account(3, rate_limit_reset_at=deadline), account(4, overload_until=deadline),
                         account(5, temp_unschedulable_until=deadline), account(6, schedulable=False),
                         account(7, status="disabled", auto_pause_on_expired=True, expires_at=self.now - 60),
                         account(8, expires_at=self.now - 60), account(9), account(10, status="error")]
        self.check(300)
        self.assertEqual(self.keys(), ["sm-acct"])
        body = self.app.hub.requests[0][2]["body"]
        self.assertEqual(body, "2 个 Claude 号出错或已过期：Claude 1、Claude 2")
        self.accounts[2] = account(3, status="error")  # already unavailable in the preceding round
        self.check(6 * HOUR)
        self.assertEqual(self.keys(), ["sm-acct"])
        self.accounts[0] = account(1)
        self.check(300)
        self.accounts[0] = account(1, status="error")
        self.check(300)
        self.assertEqual(self.keys(), ["sm-acct"] * 2)

    def test_account_cooldown_sends_only_new_errors_after_one_hour(self):
        self.accounts = [account(1, name="账号 A"), account(2, name="账号 B"), account(3)]
        self.check()
        self.accounts[0]["status"] = "error"
        self.check(300)
        self.assertEqual(self.keys(), ["sm-acct"])
        self.assertEqual(self.app.hub.requests[0][2]["body"], "1 个 Claude 号出错或已过期：账号 A")

        self.accounts[1]["status"] = "error"
        self.check(300)
        self.check(HOUR - 301)
        self.assertEqual(self.keys(), ["sm-acct"])
        self.check(1)
        self.assertEqual(self.keys(), ["sm-acct"] * 2)
        self.assertEqual(self.app.hub.requests[1][2]["body"], "1 个 Claude 号出错或已过期：账号 B")

    def test_none_usable_is_p0_and_rearms_after_recovery(self):
        self.accounts = [account(status="disabled")]
        self.check()
        self.assertEqual(self.keys(), ["sm-none"])
        self.assertEqual(self.app.hub.requests[0][2]["push_level"], "P0")
        self.check(6 * HOUR)
        self.assertEqual(self.keys(), ["sm-none"])
        self.accounts = [account()]
        self.check(300)
        self.accounts = [account(temp_unschedulable_until=starkmesh._iso(self.now + HOUR))]
        self.check(300)
        self.assertEqual(self.keys(), ["sm-none"] * 2)

    def test_partial_account_list_does_not_establish_all_unavailable(self):
        self.cut = True
        self.accounts = [account(status="error")]
        self.check()
        self.assertEqual(self.keys(), [])
        self.cut = False
        self.check(300)
        self.assertEqual(self.keys(), ["sm-none"])

    def test_failure_for_fifteen_minutes_then_success_rearms(self):
        self.error = starkmesh.StarkMeshError("gateway unavailable", "offline")
        self.check()
        self.check(14 * 60)
        self.assertEqual(self.keys(), [])
        self.check(60)
        self.assertEqual(self.keys(), ["sm-down"])
        self.assertIn("读不到 StarkMesh", items(self.page.render({}))[-1]["text"])
        self.check(6 * HOUR)
        self.assertEqual(self.keys(), ["sm-down"])
        self.error = None
        self.cut = True  # an incomplete success still ends the outage
        self.check(300)
        self.error = starkmesh.StarkMeshError("gateway unavailable", "offline")
        self.check(300)
        self.assertEqual(self.keys(), ["sm-down"])
        self.check(15 * 60)
        self.assertEqual(self.keys(), ["sm-down"] * 2)

    def test_foreground_recovery_breaks_consecutive_failures(self):
        self.error = starkmesh.StarkMeshError("offline", "offline")
        self.check()
        self.now += 300
        self.error = None
        self.page.usage({})
        self.error = starkmesh.StarkMeshError("offline", "offline")
        self.check(10 * 60)
        self.assertEqual(self.keys(), [])
        self.check(15 * 60)
        self.assertEqual(self.keys(), ["sm-down"])

    def test_http_auth_failures_notify_immediately(self):
        for code in (401, 403):
            with self.subTest(code=code):
                self.restart()
                self.mesh._read_once = starkmesh.StarkMesh._read_once.__get__(self.mesh)

                def rejected(req, timeout):
                    raise urllib.error.HTTPError(req.full_url, code, "denied", {}, None)

                self.mesh.api._open = rejected
                self.check()
                self.assertEqual(self.keys(), ["sm-down"])
                self.assertIn("只读管理密钥", self.app.hub.requests[-1][2]["body"])
                self.app.hub.requests.clear()
                self.path.unlink()

    def test_pending_notification_retries_and_logs_outcomes_without_details(self):
        self.used = (99, 0, 0)
        self.app.hub.error = HubError(403, "private notification text")
        with self.assertLogs("openmate.starkmesh.alerts", "INFO") as records:
            self.check()
            self.check(300)
            self.app.hub.error = HubError(0, "private gateway detail", "not_connected")
            self.check(300)
            self.app.hub.error = None
            self.app.hub.result = {"sent": False, "reason": "no_device"}
            self.check(300)
            self.app.hub.result = {"sent": True}
            self.check(300)
            self.check(300)
        self.assertEqual(self.keys(), LOW[:1] * 5)
        self.assertEqual(records.output, [
            "WARNING:openmate.starkmesh.alerts:sm-low-5h: 403",
            "WARNING:openmate.starkmesh.alerts:sm-low-5h: not_connected",
            "WARNING:openmate.starkmesh.alerts:sm-low-5h: no_device",
            "INFO:openmate.starkmesh.alerts:sent sm-low-5h",
        ])

    def test_cooldown_limits_retrigger_within_six_hours(self):
        self.used = (99, 0, 0)
        self.check()
        self.used = (0, 0, 0)
        self.check(300)
        self.used = (99, 0, 0)
        self.check(300)
        self.check(6 * HOUR - 601)
        self.assertEqual(self.keys(), LOW[:1])
        self.check(31)
        self.assertEqual(self.keys(), LOW[:1] * 2)

    def test_rolling_hour_cap_counts_all_six_keys(self):
        from openmate_feishu import starkmesh_alerts

        alerts = self.page.alerts
        state = alerts._state
        state["last_sent"].update((key, NOW - 600) for key in starkmesh_alerts.LABELS)
        with patch.object(starkmesh_alerts, "KEY_COOLDOWN", 0):
            self.assertFalse(alerts._due(LOW[0], NOW))
            self.assertTrue(alerts._due(LOW[0], NOW + 3000))

    def test_restart_keeps_sent_episodes_cooldowns_and_account_baseline_by_id(self):
        self.used = (99, 0, 0)
        self.accounts = [account(), account(2)]
        self.check()
        self.restart()
        self.accounts[0] = account(status="error", name="Claude secret name")
        self.check(300)
        self.assertEqual(self.keys(), [LOW[0], "sm-acct"])
        saved = self.path.read_text(encoding="utf-8")
        self.assertNotIn("Claude", saved)
        self.assertEqual(json.loads(saved)["accounts"], {"1": "sent", "2": "usable"})
        self.restart()
        self.check(6 * HOUR)
        self.assertEqual(self.keys(), [LOW[0], "sm-acct"])

    def test_payload_limits_and_footer_does_not_change_the_card(self):
        self.accounts = [account(), account(2)]
        self.check()
        self.accounts[0] = account(status="error", name="🌋" * 1500)
        self.used = (99, 0, 0)
        self.check(300)
        for method, path, body in self.app.hub.requests:
            self.assertEqual((method, path), ("POST", "/api/v1/notify"))
            self.assertEqual(set(body), {"title", "body", "id", "ui", "push_level"})
            self.assertTrue(body["title"].startswith("StarkMesh"))
            self.assertEqual((body["ui"], body["push_level"]), ("starkmesh", "P1"))
            self.assertLessEqual(len(body["title"]), 120)
            self.assertLessEqual(len(body["body"]), 1000)
            self.assertLessEqual(len(body["id"].encode("utf-8")), 56)
        self.assertEqual(self.keys(), ["sm-acct", LOW[0]])
        self.assertTrue(self.app.hub.requests[0][2]["body"].endswith("…"))
        self.assertIn(at(300 + 12 * HOUR, now=self.now), self.app.hub.requests[1][2]["body"])
        before = self.pages["starkmesh-card"].render({})
        self.app.config["starkmesh_alerts"] = False
        self.check()
        self.assertNotIn("后台告警", items(self.page.render({}))[-1]["text"])
        self.assertEqual(self.pages["starkmesh-card"].render({}), before)
