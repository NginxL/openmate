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


def make(key="sk-admin-readonly", gateway=None, url=True):
    gateway = gateway or Gateway()
    app = types.SimpleNamespace(config={"starkmesh_url": "http://127.0.0.1:18080"} if url else {},
                                secrets={"starkmesh_key": key} if key else {}, tz=TZ, cid="matefeishu")
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
        rows = [it for it in accounts["items"] if it["type"] == "row"]
        self.assertEqual([(r["title"], r["badge"]["text"]) for r in rows],
                         [("openai-x", "出错"), ("claude-limited", "限流中"), ("claude-a", "可用"), ("claude-b", "可用"), ("claude-c", "可用")])
        self.assertIn("token expired", rows[0]["subtitle"])
        self.assertNotIn("value", rows[2], "the bars say it")
        # each account: its windows' bars under it, what is LEFT and when it resets
        at = [i for i, it in enumerate(accounts["items"]) if it.get("title") == "claude-a"][0]
        bars = accounts["items"][at + 1:at + 4]
        reset = dt.datetime.fromtimestamp(NOW + 6540, TZ).strftime("%m/%d %H:%M")
        self.assertEqual([(b["label"], b["detail"], b["tone"]) for b in bars],
                         [("5 小时", "剩 80% · " + reset, "positive"),
                          ("7D", "剩 90% · " + dt.datetime.fromtimestamp(NOW + 144000, TZ).strftime("%m/%d %H:%M"), "positive"),
                          ("Fable 7D", "剩 100% · " + dt.datetime.fromtimestamp(NOW + 144000, TZ).strftime("%m/%d %H:%M"), "positive")])
        self.assertAlmostEqual(bars[0]["ratio"], 0.8)
        c = [i for i, it in enumerate(accounts["items"]) if it.get("title") == "claude-c"][0]
        self.assertEqual([(b["label"], b["detail"].split(" · ")[0], b["tone"]) for b in accounts["items"][c + 1:c + 3]],
                         [("5 小时", "剩 0%", "danger"), ("7D", "剩 10%", "danger")], "over 100% used is 0% left")
        limited = [i for i, it in enumerate(accounts["items"]) if it.get("title") == "claude-limited"][0]
        self.assertEqual(accounts["items"][limited + 1]["detail"][:4], "剩 0%")
        idle = {"id": 9, "name": "claude-idle", "platform": "anthropic", "type": "oauth", "status": "active", "schedulable": True}
        snap = starkmesh.Snapshot([idle], {"9": {"five_hour": {"utilization": 0}, "seven_day": {"utilization": 40}}}, {}, NOW)
        self.assertEqual([b["detail"] for b in page.account_bars(idle, snap)], ["剩 100% · 未开始", "剩 60% · 重置时间未知"])

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
        for kw in ({"key": ""}, {"url": False}):
            page, gw = make(**kw)
            self.assertIn("还没有接上 StarkMesh", str(page.render({"ui_version": 6})))
            self.assertIsNone(page.card(6, True, True), "no block on 今天 without an address and a key")
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

    def test_rings_from_ui_version_9(self):
        page, _ = make()
        v9 = page.render({"ui_version": 9})
        items(v9)
        quota = v9["sections"][1]
        tiles = quota["items"][0]["tiles"]
        self.assertEqual([(t["title"], t["value"], t["gauge"]) for t in tiles],
                         [("5 小时", "1.30", {"ratio": 0.4333, "unit": "号", "color": "amber"}),
                          ("本周", "1.40", {"ratio": 0.4667, "unit": "号", "color": "amber"}),
                          ("Fable 本周", "1.50", {"ratio": 0.75, "unit": "号", "color": "green"})])
        self.assertTrue(all("icon" not in t and "spark" not in t for t in tiles))
        self.assertEqual([it["type"] for it in quota["items"]], ["grid"], "the rings say it: no bars")
        self.assertEqual(quota["footer"], "最近一次重置在 1 小时 49 分后")
        v8 = page.render({"ui_version": 8})
        self.assertFalse(any("gauge" in t for t in v8["sections"][1]["items"][0]["tiles"]), "before 9: tiles and bars")
        self.assertIn("progress", [it["type"] for it in v8["sections"][1]["items"]])
        card = page.card(9, True, True)
        self.assertTrue(all("gauge" in t for t in card["items"][1]["tiles"]), "the 今天 block's rings too")
        from openmate_core.ui import check_view
        bad = {"sections": [{"items": [{"type": "grid", "tiles": [{"title": "x", "gauge": {"ratio": 1.5}, "spark": {"points": [1]}}]}]}]}
        self.assertTrue(check_view(bad))

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


class QuotaToolTest(unittest.TestCase):
    """matefeishu_starkmesh: the assistant's read of the quota, from the page's own fresh read."""

    def test_the_quota_in_words(self):
        page, gw = make()
        tools = page.tools()
        self.assertEqual(list(tools), ["matefeishu_starkmesh"])
        schema, call = tools["matefeishu_starkmesh"]
        self.assertEqual((schema["name"], schema["input_schema"]["type"]), ("matefeishu_starkmesh", "object"))
        lines = call({}).split("\n")
        self.assertTrue(lines[0].startswith("StarkMesh 额度：偏紧（数据读于 "), lines[0])  # 1.3 of 3 left in 5 小时
        self.assertEqual(lines[1], "可用 Claude 号 3/4")
        self.assertEqual(lines[2], "5 小时：剩 1.30 号（3 个可用号满额 3 号），1 小时 49 分后重置")
        self.assertEqual(lines[3], "本周：剩 1.40 号（3 个可用号满额 3 号），1 天 16 小时后重置")
        self.assertEqual(lines[4], "Fable 本周：剩 1.50 号（2 个可用号满额 2 号），1 天 16 小时后重置")
        self.assertEqual(lines[5], "不可用：claude-limited，限流中，1 小时 0 分后恢复")
        self.assertEqual(lines[6], "今天：请求 1.2万，Token 9876.5万，费用 $12.50")
        self.assertEqual(len(lines), 7, "the OpenAI account is not a Claude one")
        reads = len(gw.calls)
        call({})
        self.assertEqual(len(gw.calls), reads, "a second ask is answered from the fresh read")

    def test_not_set_up_or_unreachable_is_said(self):
        page, gw = make(key="")
        self.assertIn("还没有接上 StarkMesh", page.tools()["matefeishu_starkmesh"][1]({}))
        self.assertEqual(gw.calls, [])
        gw = Gateway()
        gw.fail = urllib.error.URLError("refused")
        page, _ = make(gateway=gw)
        self.assertTrue(page.brief({}).startswith("StarkMesh 读不到："))


class OverviewCardTest(unittest.TestCase):
    def test_the_card_has_a_pill_then_each_window(self):
        page, _ = make()
        card = starkmesh.StarkMeshCard(page)
        view = card.render({"ui_version": 6})
        assert not check_view(view, compact=True), check_view(view, compact=True)
        head, grid = view["sections"][0]["items"]
        self.assertEqual(head["title"], "可用号 3/4")
        self.assertEqual(head["badge"], {"text": "偏紧", "tone": "caution"})  # the 5 小时 window: 1.30 of 4 left
        self.assertEqual([t["title"] for t in grid["tiles"]], ["5 小时", "本周", "Fable 本周"])
        rings = card.render({"ui_version": 9})["sections"][0]["items"][1]["tiles"]
        self.assertIn("gauge", rings[0])
        bars = card.render({"ui_version": 5})["sections"][0]["items"]
        self.assertEqual([it["type"] for it in bars], ["row", "progress", "progress", "progress"])
        self.assertNotIn("icon", bars[0])

    def test_without_a_key_it_says_how(self):
        page, _ = make(key="")
        view = starkmesh.StarkMeshCard(page).render({"ui_version": 6})
        self.assertEqual(view["sections"][0]["items"][0]["title"], "还没有接上 StarkMesh")

    def test_the_pill_follows_what_is_left(self):
        s = types.SimpleNamespace(ok_claude=[1], window=lambda key: {"ratio": 0.6} if key == "five_hour" else None)
        self.assertEqual(starkmesh.standing(s), ("充足", "positive"))
        s.window = lambda key: {"ratio": 0.1}
        self.assertEqual(starkmesh.standing(s), ("快用完", "danger"))
        s.window, s.ok_claude = (lambda key: None), []
        self.assertEqual(starkmesh.standing(s), ("没有可用号", "danger"))


class RedirectTest(unittest.TestCase):
    """The key goes to the address the user wrote only: a redirect elsewhere is not followed (an HTTP error)."""

    def serve(self, answer):
        import http.server
        import threading

        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                seen.append((self.path, self.headers.get("x-api-key")))
                code, headers, body = answer(self.path)
                self.send_response(code)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return "http://127.0.0.1:%d" % srv.server_address[1], seen

    def api(self, url):
        app = types.SimpleNamespace(config={"starkmesh_url": url}, secrets={"starkmesh_key": "sk-test-only"})
        return starkmesh.StarkMeshAPI(app)

    def test_a_redirect_elsewhere_keeps_the_key(self):
        ok = json.dumps({"code": 0, "data": {"x": 1}}).encode()
        other, seen_other = self.serve(lambda path: (200, {}, ok))
        here, seen = self.serve(lambda path: (302, {"Location": other + "/stolen"}, b""))
        with self.assertRaises(starkmesh.StarkMeshError) as e:
            self.api(here).call("GET", "/api/v1/admin/dashboard/stats")
        self.assertIn("302", e.exception.message)
        self.assertEqual(seen, [("/api/v1/admin/dashboard/stats", "sk-test-only")])
        self.assertEqual(seen_other, [], "nothing reached the other address")

    def test_a_redirect_on_the_same_address_keeps_working(self):
        ok = json.dumps({"code": 0, "data": {"x": 1}}).encode()
        here, seen = self.serve(lambda path: (200, {}, ok) if path == "/new" else (301, {"Location": "/new"}, b""))
        self.assertEqual(self.api(here).call("GET", "/old"), {"x": 1})
        self.assertEqual(seen, [("/old", "sk-test-only"), ("/new", "sk-test-only")])


class RedirectPostTest(unittest.TestCase):
    def test_a_303_after_a_post_carries_the_key_and_no_body(self):
        import http.server
        import threading

        seen = []
        ok = json.dumps({"code": 0, "data": {"x": 2}}).encode()

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                seen.append(("POST", self.path, self.headers.get("x-api-key"), self.headers.get("Content-Length")))
                self.send_response(303)
                self.send_header("Location", "/done")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                seen.append(("GET", self.path, self.headers.get("x-api-key"), self.headers.get("Content-Length")))
                self.send_response(200)
                self.send_header("Content-Length", str(len(ok)))
                self.end_headers()
                self.wfile.write(ok)

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        url = "http://127.0.0.1:%d" % srv.server_address[1]
        app = types.SimpleNamespace(config={"starkmesh_url": url}, secrets={"starkmesh_key": "sk-test-only"})
        self.assertEqual(starkmesh.StarkMeshAPI(app).call("POST", "/batch", body={"ids": [1]}), {"x": 2})
        self.assertEqual(seen[1], ("GET", "/done", "sk-test-only", None), "no body, so no Content-Length")

    def test_default_ports_are_the_same_place(self):
        o = starkmesh._origin
        self.assertEqual(o("https://gw.example.com/a"), o("https://gw.example.com:443/b"))
        self.assertEqual(o("http://GW.example.com/a"), o("http://gw.example.com:80/b"))
        self.assertNotEqual(o("http://gw.example.com/a"), o("https://gw.example.com/a"))
        self.assertNotEqual(o("http://gw.example.com/a"), o("http://gw.example.com:8080/a"))


if __name__ == "__main__":
    unittest.main()
