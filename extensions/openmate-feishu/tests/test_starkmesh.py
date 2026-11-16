"""StarkMesh: the gateway's quota through a read-only scoped admin key; counted in 号, kept fresh behind the page."""

import datetime as dt
import io
import json
import os
import sys
import types
import unittest
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "..", "core"))

from openmate_core.ui import check_view  # noqa: E402
from openmate_feishu import starkmesh  # noqa: E402

from test_agenda import INPLACE, NOW, TZ  # noqa: E402


def iso(seconds_from_now):
    return dt.datetime.fromtimestamp(NOW + seconds_from_now, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def win(util, reset_in):
    return {"utilization": util, "resets_at": iso(reset_in), "remaining_seconds": reset_in}


ACCOUNTS = [
    {"id": 1, "name": "claude-a", "platform": "anthropic", "type": "oauth", "status": "active", "schedulable": True},
    {"id": 2, "name": "claude-b", "platform": "anthropic", "type": "oauth", "status": "active", "schedulable": True},
    {"id": 3, "name": "claude-c", "platform": "anthropic", "type": "setup-token", "status": "active", "schedulable": True},
    {"id": 4, "name": "claude-limited", "platform": "anthropic", "type": "oauth", "status": "active", "schedulable": True,
     "rate_limit_reset_at": iso(3600)},
    {"id": 5, "name": "openai-x", "platform": "openai", "type": "oauth", "status": "error", "schedulable": True,
     "error_message": "token expired"},
]
USAGE = {
    "1": {"five_hour": win(20, 6540), "seven_day": win(10, 144000), "seven_day_fable": win(0, 144000)},
    "2": {"five_hour": win(50, 7200), "seven_day": win(60, 150000), "seven_day_fable": win(50, 150000)},
    "3": {"five_hour": win(120, 9000), "seven_day": win(90, 160000)},
    "4": {"five_hour": win(100, 3600), "seven_day": win(100, 3600)},
}
STATS = {"today_requests": 12345, "today_tokens": 98765432, "today_actual_cost": 12.5, "rpm": 42}


class Gateway:
    def __init__(self):
        self.calls = []
        self.fail = None  # an exception for every call
        self.stats_status = 200

    def __call__(self, req, timeout=None):
        self.calls.append((req.get_method(), req.full_url, dict(req.header_items()), req.data))
        if self.fail is not None:
            raise self.fail
        path = req.full_url.split("?")[0].replace("http://127.0.0.1:18080", "")
        if path == "/api/v1/admin/accounts":
            data = {"items": ACCOUNTS, "total": len(ACCOUNTS)}
        elif path == "/api/v1/admin/accounts/usage/batch":
            data = {"usage": USAGE, "errors": {}}
        elif path == "/api/v1/admin/dashboard/stats":
            if self.stats_status != 200:
                raise urllib.error.HTTPError(req.full_url, self.stats_status, "no", {}, None)
            data = STATS
        else:
            raise urllib.error.HTTPError(req.full_url, 404, "no", {}, None)
        return io.BytesIO(json.dumps({"code": 0, "message": "success", "data": data}).encode("utf-8"))


def make(key="sk-admin-readonly", gateway=None):
    gateway = gateway or Gateway()
    app = types.SimpleNamespace(config={}, secrets={"starkmesh_key": key} if key else {}, tz=TZ)
    mesh = starkmesh.StarkMesh(app, api=starkmesh.StarkMeshAPI(app, urlopen=gateway), clock=lambda: NOW, spawn=INPLACE)
    return starkmesh.StarkMeshPage(app, mesh), gateway


def items(view):
    assert not check_view(view), check_view(view)
    return [it for s in view["sections"] for it in s["items"]]


class StarkMeshTest(unittest.TestCase):
    def test_the_dashboard(self):
        page, gw = make()
        view = page.render({"ui_version": 6})
        items(view)
        heads = {t["title"]: t for t in view["sections"][0]["items"][0]["tiles"]}
        self.assertEqual((heads["服务"]["value"], heads["服务"]["color"]), ("3/5", "amber"), "an account in error")
        self.assertEqual(heads["可用号"]["value"], "3/4")
        self.assertEqual((heads["今日请求"]["value"], heads["今日请求"]["subtitle"]), ("1.2万", "RPM 42"))
        self.assertEqual((heads["今日 Token"]["value"], heads["今日 Token"]["subtitle"]), ("9876.5万", "费用 $12.50"))
        quota = view["sections"][1]
        tiles = quota["items"][0]["tiles"]
        self.assertEqual([(t["title"], t["value"], t["color"]) for t in tiles],
                         [("5 小时", "1.30 号", "amber"), ("本周", "1.40 号", "amber"), ("Fable 本周", "1.50 号", "green")])
        self.assertEqual(tiles[0]["subtitle"], "1 小时 49 分后重置")
        bars = [it for it in quota["items"] if it["type"] == "progress"]
        self.assertEqual([(b["label"], b["tone"]) for b in bars],
                         [("5 小时 · 剩 1.30 号", "caution"), ("本周 · 剩 1.40 号", "caution"), ("Fable 本周 · 剩 1.50 号", "positive")])
        self.assertAlmostEqual(bars[2]["ratio"], 0.75)
        self.assertEqual(quota["footer"], "最近一次重置在 1 小时 49 分后")
        accounts = view["sections"][2]
        self.assertEqual([(r["title"], r["badge"]["text"]) for r in accounts["items"]],
                         [("openai-x", "出错"), ("claude-limited", "限流中"), ("claude-a", "可用"), ("claude-b", "可用"), ("claude-c", "可用")])
        self.assertEqual(accounts["items"][2]["value"], "5h 20% · 周 10%")
        self.assertIn("token expired", accounts["items"][0]["subtitle"])

    def test_read_only_with_the_key(self):
        page, gw = make()
        page.render({"ui_version": 6})
        self.assertEqual([(m, u.split("?")[0].replace("http://127.0.0.1:18080", "")) for m, u, _, _ in gw.calls],
                         [("GET", "/api/v1/admin/accounts"), ("POST", "/api/v1/admin/accounts/usage/batch"),
                          ("GET", "/api/v1/admin/dashboard/stats")])
        self.assertTrue(all(h.get("X-api-key") == "sk-admin-readonly" for _, _, h, _ in gw.calls))
        body = json.loads(gw.calls[1][3])
        self.assertEqual(body, {"account_ids": [1, 2, 3, 4], "force": False, "refresh_claude": False},
                         "the snapshots it has, never an active refresh upstream")
        page.render({"ui_version": 6})
        self.assertEqual(len(gw.calls), 3, "kept fresh behind: one read")

    def test_no_key_a_wrong_key_and_no_gateway(self):
        page, gw = make(key="")
        self.assertIn("还没有接上 StarkMesh", str(page.render({"ui_version": 6})))
        self.assertIsNone(page.card(6, True, True), "no block on 今天 without a key")
        self.assertEqual(gw.calls, [])
        gw = Gateway()
        gw.fail = urllib.error.HTTPError("u", 401, "no", {}, None)
        page, _ = make(gateway=gw)
        view = page.render({"ui_version": 6})
        self.assertIn("不认这个密钥", items(view)[0]["detail"])
        self.assertNotIn("sk-admin", json.dumps(view, ensure_ascii=False), "never the key")
        gw = Gateway()
        gw.fail = urllib.error.URLError("refused")
        page, _ = make(gateway=gw)
        self.assertIn("连不上 StarkMesh", items(page.render({"ui_version": 6}))[0]["detail"])

    def test_a_key_without_dashboard_read_still_shows_the_quota(self):
        gw = Gateway()
        gw.stats_status = 403
        page, _ = make(gateway=gw)
        view = page.render({"ui_version": 6})
        heads = {t["title"]: t for t in view["sections"][0]["items"][0]["tiles"]}
        self.assertEqual(heads["今日请求"]["value"], "—")
        self.assertEqual(view["sections"][1]["items"][0]["tiles"][0]["value"], "1.30 号")

    def test_versions(self):
        page, _ = make()
        v8 = page.render({"ui_version": 8})
        items(v8)
        self.assertEqual((v8["layout"], v8["sections"][0]["span"], v8["sections"][1]["icon"]), ("columns", "full", "gauge"))
        v5 = page.render({"ui_version": 5})
        items(v5)
        self.assertEqual(v5["sections"][0]["items"][0]["type"], "stat_grid")
        self.assertEqual([it["type"] for it in v5["sections"][1]["items"]], ["progress"] * 3, "bars before tiles")

    def test_the_block_on_today(self):
        page, _ = make()
        card = page.card(6, True, True)
        self.assertEqual(card["items"][0]["text"], "可用号 3/4 · 5 小时剩 1.30 号 · 本周剩 1.40 号")
        self.assertEqual([t["title"] for t in card["items"][1]["tiles"]], ["5 小时", "本周", "Fable 本周"])
        self.assertEqual(card["items"][-1]["action"], {"id": "go:starkmesh"})
        self.assertNotIn("go:starkmesh", str(page.card(8, True, True)), "the card's own link on 8")


if __name__ == "__main__":
    unittest.main()
