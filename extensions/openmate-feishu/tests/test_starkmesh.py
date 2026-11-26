"""StarkMesh: the gateway's quota through a read-only scoped admin key; counted in 号, kept fresh behind the page."""

try:  # first: every temporary directory of this run in one, removed at its end
    import _runtmp  # noqa: F401
except ImportError:  # (started by its path from elsewhere: its directory is not on the path)
    pass
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

try:
    from zoneinfo import ZoneInfo

    SH = ZoneInfo("Asia/Shanghai")  # the tz a query names (the hub's user time zone)
except Exception:  # no tz database here
    SH = TZ


def iso(seconds_from_now):
    return dt.datetime.fromtimestamp(NOW + seconds_from_now, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def at(seconds_from_now, now=None, tz=None):
    """A reset as the usage bars say it: a clock time (starkmesh.clock_at), in the query's time zone."""
    return starkmesh.clock_at(NOW + seconds_from_now, NOW if now is None else now, tz or TZ)


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


class UsageTest(unittest.TestCase):
    """usage.query (contributes.usage): the pool as one Claude account for the sidebar's usage bars, from the page's
    own fresh read; within what the manager keeps (usagesource.FromExtension)."""

    def test_the_pool_as_one_claude_account(self):
        page, gw = make()
        res = page.usage({"contribution": "starkmesh", "query_id": "q", "base_url": "http://127.0.0.1:8790",
                          "tz": "Asia/Shanghai", "lang": "zh-Hans"})
        self.assertEqual(len(res["accounts"]), 1, "the pool, not each account: the strip shows the two tightest")
        a = res["accounts"][0]
        self.assertEqual((a["id"], a["provider"], a["name"], a["headline"]),
                         ("starkmesh-pool", "claude", "Claude Code Max", "可用 Claude 号 3/4，5 小时最早 %s 有号重置" % at(6540, tz=SH)))
        self.assertEqual(a["sampled_at"], starkmesh._iso(NOW))
        self.assertNotIn("notice", a)
        # the page's 号 as what is used: 5 小时 1.30 of 3 left, 本周 1.40 of 3, Fable 本周 1.50 of 2
        self.assertEqual([(w["label"], w.get("model"), round(w["used_percent"], 1), w["note"]) for w in a["windows"]],
                         [("5h", None, 56.7, "3 个可用号合计剩 1.30 号，最早 %s 有号重置" % at(6540, tz=SH)),
                          ("7d", None, 53.3, "3 个可用号合计剩 1.40 号，最早 %s 有号重置" % at(144000, tz=SH)),
                          ("7d", "Fable", 25.0, "2 个可用号合计剩 1.50 号，最早 %s 有号重置" % at(144000, tz=SH))])
        self.assertRegex(at(144000, tz=SH), r"^\d\d/\d\d \d\d:\d\d$", "another day says its date")
        # staggered resets: no one end for the pool (a pace check on it would mislead); the headline says the soonest
        self.assertEqual([w.get("resets_at") for w in a["windows"]], [None, None, None])
        together = [dict(ACCOUNTS[0], id=i) for i in (21, 22)]
        snap = starkmesh.Snapshot(together, {"21": {"five_hour": win(40, 3600)}, "22": {"five_hour": win(60, 3630)}}, {}, NOW)
        page.mesh.read = lambda wait=None: snap
        page.mesh.state = (snap, None)  # as a read that came in
        self.assertEqual(page.usage({"lang": "zh-Hans"})["accounts"][0]["windows"][0]["resets_at"], starkmesh._iso(NOW + 3630),
                         "resetting together: one window with the last one's end, so it never ends early")
        page.mesh.read = starkmesh.StarkMesh.read.__get__(page.mesh)
        # what the manager takes: a provider name, window lengths it reads, text within its cuts
        self.assertRegex(a["provider"], r"^[a-z0-9][a-z0-9_-]{0,31}$")
        self.assertTrue(len(a["name"]) <= 80 and len(a["headline"]) <= 200)
        self.assertTrue(all(len(w["note"]) <= 200 and len(w.get("model", "")) <= 40 for w in a["windows"]))
        reads = len(gw.calls)
        page.usage({"lang": "zh-Hans"})
        page.render({"ui_version": 6})
        self.assertEqual(len(gw.calls), reads, "the page and the bars share one read")
        self.assertEqual(sorted({(c[0], c[1].split("?")[0].replace("http://127.0.0.1:18080", "")) for c in gw.calls}),
                         [("GET", "/api/v1/admin/accounts"), ("GET", "/api/v1/admin/dashboard/stats"),
                          ("POST", "/api/v1/admin/accounts/usage/batch")], "nothing the page did not read already")

    def test_in_english_and_japanese(self):
        page, _ = make()
        a = page.usage({"lang": "en"})["accounts"][0]
        self.assertEqual((a["name"], a["headline"], a["windows"][0]["note"], a["windows"][1]["note"]),
                         ("Claude Code Max", "3 of 4 Claude accounts usable; first 5-hour reset at %s" % at(6540),
                          "1.30 of 3 usable accounts left; the first resets at %s" % at(6540),
                          "1.40 of 3 usable accounts left; the first resets at %s" % at(144000)))
        a = page.usage({"lang": "ja"})["accounts"][0]
        self.assertEqual((a["name"], a["headline"], a["windows"][0]["note"]),
                         ("Claude Code Max", "使える Claude アカウント 3/4、5 時間枠の最初のリセットは %s" % at(6540),
                          "使えるアカウント 3 件で残り 1.30 件分、最初のリセットは %s" % at(6540)))

    def test_not_set_up_unreachable_and_nothing_usable(self):
        from openmate_core.wire import RPCError

        page, gw = make(key="")
        a = page.usage({"lang": "zh-Hans"})["accounts"][0]
        self.assertIn("还没有接上 StarkMesh", a["notice"])
        self.assertNotIn("windows", a)
        self.assertEqual(gw.calls, [])
        gw = Gateway()
        gw.fail = urllib.error.URLError("refused")
        page, _ = make(gateway=gw)
        with self.assertRaises(RPCError):  # the manager keeps the last answer, faded
            page.usage({"lang": "zh-Hans"})
        off = [dict(x, status="disabled") for x in ACCOUNTS]
        page, _ = make()
        snap = starkmesh.Snapshot(off, USAGE, {}, NOW)
        page.mesh.read = lambda wait=None: snap
        a = page.usage({"lang": "zh-Hans"})["accounts"][0]
        self.assertEqual((a["headline"], a.get("windows")), ("可用 Claude 号 0/4", None))
        self.assertIn("都用不了", a["notice"])

    def test_a_later_failed_read_is_said_not_hidden(self):
        # the page keeps showing its last read while a background read fails; the bars must not take it as current
        from openmate_core.wire import RPCError

        page, gw = make()
        page.usage({"lang": "zh-Hans"})
        gw.fail = urllib.error.URLError("refused")
        with self.assertRaises(starkmesh.StarkMeshError):
            page.mesh._read()  # the read behind the kept one fails
        with self.assertRaises(RPCError):
            page.usage({"lang": "zh-Hans"})
        gw.fail = None
        page.mesh._read()
        self.assertEqual(len(page.usage({"lang": "zh-Hans"})["accounts"][0]["windows"]), 3, "in again: answered again")

    def test_a_read_that_came_in_since_is_the_answer(self):
        # a failed poll, then one whose own background read comes in: not the read from before the failure
        from openmate_core.wire import RPCError

        t = [NOW]
        gw = Gateway()
        app = types.SimpleNamespace(config={"starkmesh_url": "http://127.0.0.1:18080"}, secrets={"starkmesh_key": "k"},
                                    tz=TZ, cid="matefeishu")
        mesh = starkmesh.StarkMesh(app, api=starkmesh.StarkMeshAPI(app, urlopen=gw), clock=lambda: t[0], spawn=INPLACE)
        page = starkmesh.StarkMeshPage(app, mesh)
        self.assertEqual(page.usage({})["accounts"][0]["sampled_at"], starkmesh._iso(NOW))
        t[0], gw.fail = NOW + 60, urllib.error.URLError("refused")  # past AHEAD: read again behind the kept one
        with self.assertRaises(RPCError):
            page.usage({})
        t[0], gw.fail = NOW + 120, None
        self.assertEqual(page.usage({})["accounts"][0]["sampled_at"], starkmesh._iso(NOW + 120))
        # the read and its failure mark are one state: the newer read with no failure, or the old one with it
        old, new = (starkmesh.Snapshot(ACCOUNTS, USAGE, {}, x) for x in (NOW + 200, NOW + 210))
        mesh.read = lambda wait=None: old
        mesh.state = (new, None)
        self.assertEqual(page.usage({})["accounts"][0]["sampled_at"], starkmesh._iso(NOW + 210))
        mesh.state = (old, NOW + 205)
        with self.assertRaises(RPCError):
            page.usage({})
        mesh.state = (old, NOW + 100)  # failed by a clock that stepped back: the newest read failed all the same
        with self.assertRaises(RPCError):
            page.usage({})
        stepped = starkmesh.Snapshot(ACCOUNTS, USAGE, {}, NOW + 150)  # read in after the clock stepped back
        mesh.state = (stepped, None)
        self.assertEqual(page.usage({})["accounts"][0]["sampled_at"], starkmesh._iso(NOW + 150), "the newest, not the latest clock")

    def test_a_part_of_the_pool_is_not_the_whole(self):
        from openmate_core.wire import RPCError

        page, _ = make()
        snap = starkmesh.Snapshot(ACCOUNTS, USAGE, {}, NOW)
        snap.usage_failed = {"1"}  # a usable account's usage was not read: the numbers are some accounts', not the pool's
        page.mesh.read = lambda wait=None: snap
        with self.assertRaises(RPCError):
            page.usage({"lang": "zh-Hans"})
        snap.usage_failed = {"4", "5"}  # a rate-limited and a disabled one: not in the pool anyway
        self.assertEqual(len(page.usage({"lang": "zh-Hans"})["accounts"][0]["windows"]), 3)
        snap.usage_failed, snap.total = set(), 10  # the list was cut short: said in the headline
        a = page.usage({"lang": "zh-Hans"})["accounts"][0]
        self.assertEqual((a["headline"], a["notice"], a.get("windows")),
                         ("可用 Claude 号 3/4（只读到 5/10 个号）", "号没有读全，额度条不画，详情看中转站页（只读到 5/10 个号）", None),
                         "a part of the pool drawn as a bar would pass for the whole (the strip shows no headline)")
        snap.usage_failed = {"1"}  # partial and a usage missing: said as partial (the bars go), not an error (they'd stay)
        self.assertIn("号没有读全", page.usage({"lang": "zh-Hans"})["accounts"][0]["notice"])

    def test_an_account_the_gateway_could_not_read_is_a_part_too(self):
        # a batch answered 200 but said one usable account's usage failed: not the pool's numbers
        from openmate_core.wire import RPCError

        class OneFails(Gateway):
            def __call__(self, req, timeout=None):
                if req.full_url.endswith("/api/v1/admin/accounts/usage/batch"):
                    self.calls.append((req.get_method(), req.full_url, {}, req.data))
                    usage = {k: v for k, v in USAGE.items() if k != "2"}
                    return io.BytesIO(json.dumps({"code": 0, "data": {"usage": usage, "errors": {"2": "upstream"}}}).encode())
                return super().__call__(req, timeout)

        page, _ = make(gateway=OneFails())
        with self.assertRaises(RPCError):
            page.usage({"lang": "zh-Hans"})
        self.assertEqual(page.mesh.read().usage_failed, {"2"})

    def test_an_unknown_reset_leaves_the_pool_open(self):
        # one account's week resets in 5 minutes, another's is used with no reset said: the bar must not end in 5 minutes
        a, b = (dict(ACCOUNTS[0], id=i, name="c%d" % i) for i in (11, 12))
        snap = starkmesh.Snapshot([a, b], {"11": {"seven_day": win(40, 300)}, "12": {"seven_day": {"utilization": 30}}}, {}, NOW)
        w = snap.window("seven_day")
        self.assertEqual((w["reset"], w["last"]), (300, None))
        page, _ = make()
        page.mesh.read = lambda wait=None: snap
        out = page.usage({"lang": "zh-Hans"})["accounts"][0]["windows"][0]
        self.assertNotIn("resets_at", out)
        self.assertEqual(out["note"], "2 个可用号合计剩 1.30 号，最早 %s 有号重置" % at(300))

    def test_every_page_full_and_no_total_is_a_cut_list(self):
        class Endless(Gateway):
            def __call__(self, req, timeout=None):
                if req.full_url.split("?")[0].endswith("/api/v1/admin/accounts"):
                    self.calls.append((req.get_method(), req.full_url, {}, None))
                    page = int(req.full_url.split("page=")[1].split("&")[0])
                    items = [dict(ACCOUNTS[4], id=page * 1000 + i, name="o%d" % i) for i in range(200)]
                    return io.BytesIO(json.dumps({"code": 0, "data": {"items": items}}).encode())
                return super().__call__(req, timeout)

        page, _ = make(gateway=Endless())
        snap = page.mesh.read()
        self.assertEqual((len(snap.accounts), snap.cut), (2000, True))
        page2, _ = make()
        self.assertIs(page2.mesh.read().cut, False)

    def test_an_api_key_account_has_no_windows_and_is_no_missing_part(self):
        # StarkMesh answers an API-key account's usage with an error ("does not support usage query"): it has none
        class WithAPIKey(Gateway):
            def __call__(self, req, timeout=None):
                path = req.full_url.split("?")[0]
                if path.endswith("/api/v1/admin/accounts"):
                    self.calls.append((req.get_method(), req.full_url, {}, None))
                    items = ACCOUNTS + [dict(ACCOUNTS[0], id=6, name="claude-key", type="apikey")]
                    return io.BytesIO(json.dumps({"code": 0, "data": {"items": items, "total": len(items)}}).encode())
                if path.endswith("/api/v1/admin/accounts/usage/batch"):
                    self.calls.append((req.get_method(), req.full_url, {}, req.data))
                    errors = {"6": "account type apikey does not support usage query"}
                    return io.BytesIO(json.dumps({"code": 0, "data": {"usage": USAGE, "errors": errors}}).encode())
                return super().__call__(req, timeout)

        page, _ = make(gateway=WithAPIKey())
        a = page.usage({"lang": "zh-Hans"})["accounts"][0]
        self.assertEqual(a["headline"], "可用 Claude 号 4/5，5 小时最早 %s 有号重置" % at(6540), "no snapshot is no fault of it")
        self.assertEqual(a["windows"][0]["note"], "3 个可用号合计剩 1.30 号，最早 %s 有号重置" % at(6540), "counted without it")

    def test_a_five_hour_window_ends_with_its_session_window(self):
        # the passive snapshot may leave the 5 小时 resets_at out; the account's session_window_end still says it
        idle = dict(ACCOUNTS[0], id=9, name="claude-idle", session_window_end=iso(-60))
        snap = starkmesh.Snapshot([idle], {"9": {"five_hour": {"utilization": 99}}}, {}, NOW)
        self.assertEqual(snap.window("five_hour")["left"], 1.0)
        page, _ = make()
        self.assertEqual([b["detail"] for b in page.account_bars(idle, snap)], ["剩 100% · 已重置"])
        later = dict(idle, session_window_end=iso(1800))
        snap = starkmesh.Snapshot([later], {"9": {"five_hour": {"utilization": 99}}}, {}, NOW)
        self.assertEqual((round(snap.window("five_hour")["left"], 2), snap.window("five_hour")["last"]), (0.01, 1800))

    def test_as_of_the_query_not_the_read(self):
        # a reset between the read and the query has happened: the window is not handed over already over
        page, _ = make()
        page.mesh.read()
        page.mesh.clock = lambda: NOW + 7000  # claude-a's 5 小时 (6540 s) has started again since, and claude-limited
        w = page.usage({"lang": "zh-Hans"})["accounts"][0]["windows"][0]  # is out of its rate limit (3600 s), renewed
        self.assertEqual((round(w["used_percent"], 6), w.get("resets_at"), w["note"]),
                         (37.5, None, "4 个可用号合计剩 2.50 号，最早 %s 有号重置" % at(7200, NOW + 7000)))

    def test_full_pages_with_a_repeat_are_still_a_cut_list(self):
        class Repeats(Gateway):
            def __call__(self, req, timeout=None):
                if req.full_url.split("?")[0].endswith("/api/v1/admin/accounts"):
                    self.calls.append((req.get_method(), req.full_url, {}, None))
                    page = int(req.full_url.split("page=")[1].split("&")[0])
                    items = [dict(ACCOUNTS[4], id=page * 1000 + i, name="o%d" % i) for i in range(200)]
                    if page == 2:
                        items[0] = dict(items[0], id=1000)  # one of page 1's again
                    return io.BytesIO(json.dumps({"code": 0, "data": {"items": items}}).encode())
                return super().__call__(req, timeout)

        page, _ = make(gateway=Repeats())
        snap = page.mesh.read()
        self.assertEqual((len(snap.accounts), snap.cut), (1999, True))

    def test_a_bedrock_account_has_no_windows_either(self):
        page, _ = make()
        rock = dict(ACCOUNTS[0], id=7, name="claude-rock", type="bedrock")
        snap = starkmesh.Snapshot(ACCOUNTS + [rock], USAGE, {}, NOW)
        snap.usage_failed = {"7"}  # "does not support usage query"
        page.mesh.read = lambda wait=None: snap
        self.assertTrue(page.usage({"lang": "zh-Hans"})["accounts"][0]["headline"].startswith("可用 Claude 号 4/5，"))
        only = starkmesh.Snapshot([rock, dict(rock, id=8, type="apikey"), dict(rock, id=9, type="service_account")], {}, {}, NOW)
        page.mesh.read = lambda wait=None: only
        self.assertIn("不是订阅号", page.usage({"lang": "zh-Hans"})["accounts"][0]["notice"],
                      "not 'once they are used': these never have windows")

    def test_who_can_be_used_is_as_of_the_query(self):
        # claude-limited's rate limit (3600 s) is over by the query: it is usable, and its 5 小时 has started again
        page, _ = make()
        page.mesh.read()
        page.mesh.clock = lambda: NOW + 4000
        a = page.usage({"lang": "zh-Hans"})["accounts"][0]
        self.assertEqual(a["headline"], "可用 Claude 号 4/4，5 小时最早 %s 有号重置" % at(6540, NOW + 4000))
        self.assertEqual(a["windows"][0]["note"].split("，")[0], "4 个可用号合计剩 2.30 号")

    def test_a_bar_missing_some_accounts_is_not_drawn(self):
        # the collapsed strip draws a bar without the headline: one made of some accounts could look less used
        a, b = (dict(ACCOUNTS[0], id=i, name="c%d" % i) for i in (11, 12))
        full = {"five_hour": win(90, 600), "seven_day": win(50, 9000)}
        snap = starkmesh.Snapshot([a, b], {"11": full, "12": {"five_hour": win(10, 600)}}, {}, NOW)
        page, _ = make()
        page.mesh.read = lambda wait=None: snap
        acct = page.usage({"lang": "zh-Hans"})["accounts"][0]
        self.assertEqual([w["label"] for w in acct["windows"]], ["5h"], "the weekly bar would be one account's")
        self.assertEqual(acct["headline"], "可用 Claude 号 2/2，5 小时最早 %s 有号重置，每周有 1 个号没有快照，不画" % at(600))
        self.assertTrue(page.usage({"lang": "en"})["accounts"][0]["headline"].endswith("; no weekly bar: 1 without a snapshot"))
        snap = starkmesh.Snapshot([a, b], {"11": {"five_hour": win(90, 600)}, "12": {"five_hour": win(10, 600)}}, {}, NOW)
        page.mesh.read = lambda wait=None: snap  # neither has a weekly number: said as well
        self.assertTrue(page.usage({"lang": "zh-Hans"})["accounts"][0]["headline"].endswith("，每周有 2 个号没有快照，不画"))
        snap = starkmesh.Snapshot([a, b], {"11": full}, {}, NOW)  # nothing of the second: no bar at all
        page.mesh.read = lambda wait=None: snap
        acct = page.usage({"lang": "zh-Hans"})["accounts"][0]
        self.assertEqual((acct.get("windows"), acct["headline"]),
                         (None, "可用 Claude 号 2/2，5 小时有 1 个号没有快照，不画，每周有 1 个号没有快照，不画"))
        self.assertEqual(acct["notice"], "有号还没有用量快照，额度条先不画，5 小时有 1 个号没有快照，不画，每周有 1 个号没有快照，不画")

    def test_one_end_only_when_every_counted_account_has_it(self):
        # one account 80% used resetting in an hour, one already started again: no shared end (pace would mislead)
        a, b = (dict(ACCOUNTS[0], id=i, name="c%d" % i) for i in (11, 12))
        snap = starkmesh.Snapshot([a, b], {"11": {"five_hour": win(80, 3600)}, "12": {"five_hour": win(70, -60)}}, {}, NOW)
        w = snap.window("five_hour")
        self.assertEqual((w["of"], w["dated"], w["last"]), (2, False, 3600))
        page, _ = make()
        page.mesh.read = lambda wait=None: snap
        self.assertNotIn("resets_at", page.usage({"lang": "zh-Hans"})["accounts"][0]["windows"][0])

    def test_fable_held_back_is_out_of_the_fable_bar(self):
        # StarkMesh keeps the account but sends it no Fable (credits required): it is not Fable capacity
        held = dict(ACCOUNTS[0], extra={"model_rate_limits": {"claude-fable-5": {"rate_limit_reset_at": iso(86400),
                                                                                  "reason": "anthropic_fable_credits_required"}}})
        snap = starkmesh.Snapshot([held, ACCOUNTS[1]], USAGE, {}, NOW)
        page, _ = make()
        page.mesh.read = lambda wait=None: snap
        fable = [w for w in page.usage({"lang": "zh-Hans"})["accounts"][0]["windows"] if w.get("model") == "Fable"]
        self.assertEqual((round(fable[0]["used_percent"], 6), fable[0]["note"].split("，")[0]), (50.0, "1 个可用号合计剩 0.50 号"))
        self.assertTrue(starkmesh.fable_blocked(held, NOW))
        self.assertFalse(starkmesh.fable_blocked(held, NOW + 90000), "until its time")
        self.assertFalse(starkmesh.fable_blocked(ACCOUNTS[0], NOW))

    def test_a_number_sampled_after_the_reset_is_kept(self):
        # StarkMesh may update a window's number without its reset time: 80% sampled 30 s ago, the reset 2 min ago
        a = dict(ACCOUNTS[0], id=31)
        u = {"updated_at": iso(-30), "five_hour": win(10, 3600), "seven_day": {"utilization": 80, "resets_at": iso(-120)}}
        snap = starkmesh.Snapshot([a], {"31": u}, {}, NOW)
        w = snap.window("seven_day")
        self.assertEqual((round(w["left"], 2), w["last"], w["dated"]), (0.2, None, False), "kept, never less used")
        page, _ = make()
        self.assertEqual(page.account_bars(a, snap)[1]["detail"], "剩 20% · 重置时间未知")
        same = dict(u, updated_at=iso(-120))  # sampled in the reset's second (whole seconds): kept too
        self.assertEqual(round(starkmesh.Snapshot([a], {"31": same}, {}, NOW).window("seven_day")["left"], 2), 0.2)
        frac = {"updated_at": iso(-120), "seven_day": {"utilization": 80,  # the reset half a second into that second
                                                       "resets_at": iso(-120).replace("Z", ".500000Z")}}
        self.assertEqual(round(starkmesh.Snapshot([a], {"31": frac}, {}, NOW).window("seven_day")["left"], 2), 0.2)
        older = dict(u, updated_at=iso(-600))  # sampled before the reset: started again
        snap = starkmesh.Snapshot([a], {"31": older}, {}, NOW)
        self.assertEqual(snap.window("seven_day")["left"], 1.0)
        self.assertEqual(page.account_bars(a, snap)[1]["detail"], "剩 100% · 已重置")
        # sampled after its reset, but six hours ago: the 5 小时 period it was in has ended as well
        idle = {"updated_at": iso(-6 * 3600), "five_hour": {"utilization": 80, "resets_at": iso(-7 * 3600)}}
        self.assertEqual(starkmesh.Snapshot([a], {"31": idle}, {}, NOW).window("five_hour")["left"], 1.0)
        # no reset time at all, sampled eight days ago: that weekly period has ended too
        old = {"updated_at": iso(-8 * 86400), "seven_day": {"utilization": 99}}
        self.assertEqual(starkmesh.Snapshot([a], {"31": old}, {}, NOW).window("seven_day")["left"], 1.0)
        recent = {"updated_at": iso(-86400), "seven_day": {"utilization": 99}}
        self.assertAlmostEqual(starkmesh.Snapshot([a], {"31": recent}, {}, NOW).window("seven_day")["left"], 0.01)
        # a 5 小时 sampled exactly 5 h ago (whole seconds): the window may still have most of a second to go
        edge = {"updated_at": iso(-5 * 3600), "five_hour": {"utilization": 80, "resets_at": iso(-6 * 3600)}}
        self.assertAlmostEqual(starkmesh.Snapshot([a], {"31": edge}, {}, NOW).window("five_hour")["left"], 0.2)

    def test_the_used_share_is_not_rounded(self):
        # 20 accounts at 95% and one at 96%: 4.95% left, below the web's 5% line; rounded, it would sit on it
        accts = [dict(ACCOUNTS[0], id=100 + i) for i in range(21)]
        usage = {str(100 + i): {"five_hour": win(95 if i else 96, 3600)} for i in range(21)}
        page, _ = make()
        page.mesh.read = lambda wait=None: starkmesh.Snapshot(accts, usage, {}, NOW)
        used = page.usage({"lang": "zh-Hans"})["accounts"][0]["windows"][0]["used_percent"]
        self.assertGreater(used, 95.0)
        self.assertAlmostEqual(used, 95.047619, places=5)

    def test_a_window_without_an_end_is_kept_until_surely_over(self):
        # no end said: a fresh 12% may be this window's (kept); once the sample's own window must be over, it is
        a = dict(ACCOUNTS[0], id=41)
        fresh = {"updated_at": iso(-60), "five_hour": {"utilization": 12, "remaining_seconds": 0}}
        snap = starkmesh.Snapshot([a], {"41": fresh}, {}, NOW)
        self.assertAlmostEqual(snap.window("five_hour")["left"], 0.88)
        page, _ = make()
        self.assertEqual([b["detail"] for b in page.account_bars(a, snap)], ["剩 88% · 重置时间未知"])
        stale = dict(fresh, updated_at=iso(-5 * 3600 - 2))
        snap = starkmesh.Snapshot([a], {"41": stale}, {}, NOW)
        self.assertEqual(snap.window("five_hour")["left"], 1.0)
        self.assertEqual([b["detail"] for b in page.account_bars(a, snap)], ["剩 100% · 未开始"])

    def test_the_gateways_times_against_the_querys_clock(self):
        # read by a clock 2 minutes fast, set right since: a reset 1 minute from now has not happened
        a = dict(ACCOUNTS[0], id=51)
        snap = starkmesh.Snapshot([a], {"51": {"five_hour": win(80, 60)}}, {}, NOW + 120)
        page, _ = make()
        page.mesh.read = lambda wait=None: snap
        page.mesh.state = (snap, None)
        w = page.usage({"lang": "zh-Hans"})["accounts"][0]["windows"][0]
        self.assertEqual((round(w["used_percent"], 6), w.get("resets_at")), (80.0, starkmesh._iso(NOW + 60)))

    def test_an_end_is_never_said_early(self):
        self.assertEqual(starkmesh._iso(NOW + 0.1, up=True), starkmesh._iso(NOW + 1))
        self.assertEqual(starkmesh._iso(NOW + 0.9), starkmesh._iso(NOW))

    def test_no_bars_left_says_which_went(self):
        held = dict(ACCOUNTS[0], extra={"model_rate_limits": {"claude-fable-5": {"rate_limit_reset_at": iso(86400)}}})
        snap = starkmesh.Snapshot([held], {"1": {"seven_day_fable": win(100, 3600)}}, {}, NOW)
        page, _ = make()
        page.mesh.read = lambda wait=None: snap
        a = page.usage({"lang": "zh-Hans"})["accounts"][0]
        self.assertNotIn("windows", a)
        self.assertEqual(a["notice"], "有号还没有用量快照，额度条先不画，5 小时有 1 个号没有快照，不画，每周有 1 个号没有快照，不画")
        meta = starkmesh.Snapshot([ACCOUNTS[0]], {"1": {"claude_subscription": {"five_hour": None}}}, {}, NOW)
        page.mesh.read = lambda wait=None: meta  # an account's subscription details are no quota window
        self.assertTrue(page.usage({"lang": "zh-Hans"})["accounts"][0]["notice"].startswith("有号还没有用量快照"))

    def test_any_fraction_of_a_second(self):
        # Python before 3.11 reads only 3 or 6 fractional digits; the gateway may write 5 (or 9)
        base = starkmesh._ts("2026-11-16T10:30:26Z")
        for text in ("2026-11-16T10:30:26.12345Z", "2026-11-16T10:30:26.123456789+00:00", "2026-11-16T10:30:26.1Z"):
            self.assertAlmostEqual(starkmesh._ts(text), base, delta=1, msg=text)
        self.assertIsNone(starkmesh._ts("not a time"))

    def test_an_expired_credential_is_out_of_the_pool(self):
        # StarkMesh refuses it at once, before its pause worker turns schedulable off: not in the bars
        gone = dict(ACCOUNTS[1], auto_pause_on_expired=True, expires_at=int(NOW) - 1)
        snap = starkmesh.Snapshot([ACCOUNTS[0], gone], USAGE, {}, NOW)
        page, _ = make()
        page.mesh.read = lambda wait=None: snap
        a = page.usage({"lang": "zh-Hans"})["accounts"][0]
        self.assertTrue(a["headline"].startswith("可用 Claude 号 1/2"), a["headline"])
        self.assertAlmostEqual(a["windows"][0]["used_percent"], 20.0, places=6, msg="claude-a's 20% alone")
        self.assertTrue(starkmesh.usable(gone, NOW), "the page's own rule is unchanged")
        self.assertFalse(starkmesh.expired(dict(gone, auto_pause_on_expired=False), NOW))
        self.assertFalse(starkmesh.expired(dict(gone, expires_at=int(NOW) + 60), NOW))
        self.assertTrue(starkmesh.expired(dict(gone, expires_at=iso(-1)), NOW), "expires_at as a time too")
        # the read-only key's answer has no auto_pause_on_expired: not inferred (one with it off is still scheduled)
        scoped = {k: v for k, v in gone.items() if k != "auto_pause_on_expired"}
        self.assertFalse(starkmesh.expired(dict(scoped, expires_at=iso(-1)), NOW))

    def test_a_gateway_repeating_a_full_first_page_may_hold_more(self):
        class Ignores(Gateway):
            def __call__(self, req, timeout=None):
                if req.full_url.split("?")[0].endswith("/api/v1/admin/accounts"):
                    self.calls.append((req.get_method(), req.full_url, {}, None))
                    items = [dict(ACCOUNTS[4], id=i, name="o%d" % i) for i in range(200)]  # page ignored, no total
                    return io.BytesIO(json.dumps({"code": 0, "data": {"items": items}}).encode())
                return super().__call__(req, timeout)

        page, _ = make(gateway=Ignores())
        snap = page.mesh.read()
        self.assertEqual((len(snap.accounts), snap.cut), (200, True))

    def test_a_list_cut_short_is_said_in_the_notice_too(self):
        page, _ = make()
        off = [dict(x, status="disabled") for x in ACCOUNTS]
        snap = starkmesh.Snapshot(off, USAGE, {}, NOW)
        snap.cut = True  # every page full and no total: there may be more
        page.mesh.read = lambda wait=None: snap
        a = page.usage({"lang": "zh-Hans"})["accounts"][0]
        self.assertEqual(a["headline"], "可用 Claude 号 0/4（只读了前 5 个号）")
        self.assertTrue(a["notice"].endswith("（只读了前 5 个号）"), a["notice"])

    def test_a_window_past_its_reset_has_started_again(self):
        idle = {"id": 9, "name": "claude-idle", "platform": "anthropic", "type": "oauth", "status": "active", "schedulable": True}
        snap = starkmesh.Snapshot([idle], {"9": {"five_hour": {"utilization": 99, "resets_at": iso(-60), "remaining_seconds": 0}}},
                                  {}, NOW)
        self.assertEqual(snap.window("five_hour"), {"left": 1.0, "of": 1, "ratio": 1.0, "reset": None, "last": None, "dated": False})
        page, _ = make()
        page.mesh.read = lambda wait=None: snap
        w = page.usage({"lang": "zh-Hans"})["accounts"][0]["windows"][0]
        self.assertEqual((round(w["used_percent"], 6), "resets_at" in w), (0.0, False))
        self.assertEqual([b["detail"] for b in page.account_bars(idle, snap)], ["剩 100% · 已重置"],
                         "the page's own bar says the same as the pool")


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


class ManyAccountsTest(unittest.TestCase):
    def test_75_accounts_stay_within_a_page_and_every_page_is_read(self):
        many = [dict(ACCOUNTS[0], id=100 + i, name="claude-%03d" % i) for i in range(250)]
        usage = {str(100 + i): {"five_hour": win(20, 600), "seven_day": win(10, 6000), "seven_day_fable": win(5, 6000)}
                 for i in range(250)}
        asked = []

        def opener(req, timeout=None):
            u = req.full_url
            asked.append(u)
            if "/admin/accounts/usage" in u:
                data = {"usage": usage, "errors": {}}
            elif "/admin/accounts" in u:
                q = dict(x.split("=") for x in u.split("?", 1)[1].split("&"))
                page, size = int(q["page"]), int(q["page_size"])
                data = {"items": many[(page - 1) * size: page * size], "total": len(many)}
            else:
                data = STATS
            return io.BytesIO(json.dumps({"code": 0, "data": data}).encode())

        page, _ = make(gateway=opener)
        view = page.render({"ui_version": 9})
        self.assertEqual(len(page.mesh.read().accounts), 250, "every page, not the first 200 taken as all")
        self.assertFalse(check_view(view), check_view(view))
        acc = [s for s in view["sections"] if str(s.get("title", "")).startswith("账号")][0]
        self.assertIn("还有 210 个账号", acc.get("footer", ""))


class PageSizeTest(unittest.TestCase):
    def test_pages_as_starkmesh_gives_them(self):
        # StarkMesh answers a page_size over 100 with 20: ask for 100, so 5 pages hold 500 accounts, not 100
        many = [dict(ACCOUNTS[0], id=2000 + i, name="claude-p%03d" % i) for i in range(250)]

        def opener(req, timeout=None):
            u = req.full_url
            if "/admin/accounts/usage" in u:
                ids = json.loads(req.data.decode())["account_ids"]
                data = {"usage": {str(i): {"five_hour": win(10, 600)} for i in ids}, "errors": {}}
            elif "/admin/accounts" in u:
                q = dict(x.split("=") for x in u.split("?", 1)[1].split("&"))
                p, size = int(q["page"]), int(q["page_size"])
                size = size if 1 <= size <= 100 else 20
                data = {"items": many[(p - 1) * size: p * size], "total": len(many)}
            else:
                data = STATS
            return io.BytesIO(json.dumps({"code": 0, "data": data}).encode())

        page, _ = make(gateway=opener)
        snap = page.mesh.read()
        self.assertEqual(len(snap.accounts), 250)
        self.assertEqual(len(page.usage({"lang": "zh-Hans"})["accounts"][0]["windows"]), 1)


class OddGatewayTest(unittest.TestCase):
    def test_a_gateway_that_ignores_page_lists_each_account_once(self):
        same = [dict(ACCOUNTS[0], id=500 + i, name="claude-x%03d" % i) for i in range(200)]
        asked = []

        def opener(req, timeout=None):
            u = req.full_url
            if "/admin/accounts/usage" in u:
                ids = json.loads(req.data.decode())["account_ids"]
                asked.append(len(ids))
                data = {"usage": {str(i): {"five_hour": win(0, 600)} for i in ids}, "errors": {}}
            elif "/admin/accounts" in u:
                data = {"items": same}  # every page the same, no total
            else:
                data = STATS
            return io.BytesIO(json.dumps({"code": 0, "data": data}).encode())

        page, _ = make(gateway=opener)
        view = page.render({"ui_version": 9})
        self.assertFalse(check_view(view), check_view(view))
        self.assertEqual(len(page.mesh.read().accounts), 200)

    def test_the_usage_of_every_account_in_batches(self):
        many = [dict(ACCOUNTS[0], id=700 + i, name="claude-y%03d" % i) for i in range(250)]
        asked = []

        def opener(req, timeout=None):
            u = req.full_url
            if "/admin/accounts/usage" in u:
                ids = json.loads(req.data.decode())["account_ids"]
                asked.append(len(ids))
                data = {"usage": {str(i): {"five_hour": win(0, 600)} for i in ids}, "errors": {}}  # only those asked
            elif "/admin/accounts" in u:
                q = dict(x.split("=") for x in u.split("?", 1)[1].split("&"))
                p, size = int(q["page"]), int(q["page_size"])
                data = {"items": many[(p - 1) * size: p * size], "total": len(many)}
            else:
                data = STATS
            return io.BytesIO(json.dumps({"code": 0, "data": data}).encode())

        page, _ = make(gateway=opener)
        snap = page.mesh.read()
        self.assertEqual(asked, [100, 100, 50], "the read-only key may ask 100 at a time")
        self.assertEqual(len(snap.usage), 250)


class UsageBatchFailsTest(unittest.TestCase):
    def test_a_failed_usage_batch_keeps_the_rest(self):
        many = [dict(ACCOUNTS[0], id=900 + i, name="claude-z%03d" % i) for i in range(250)]

        def opener(req, timeout=None):
            u = req.full_url
            if "/admin/accounts/usage" in u:
                ids = json.loads(req.data.decode())["account_ids"]
                if len(ids) < 100:
                    raise urllib.error.HTTPError(u, 503, "busy", {}, None)
                data = {"usage": {str(i): {"five_hour": win(0, 600)} for i in ids}, "errors": {}}
            elif "/admin/accounts" in u:
                q = dict(x.split("=") for x in u.split("?", 1)[1].split("&"))
                p, size = int(q["page"]), int(q["page_size"])
                data = {"items": many[(p - 1) * size: p * size], "total": len(many)}
            else:
                data = STATS
            return io.BytesIO(json.dumps({"code": 0, "data": data}).encode())

        page, _ = make(gateway=opener)
        snap = page.mesh.read()
        self.assertEqual((len(snap.accounts), len(snap.usage), snap.usage_missed), (250, 200, 50))
        self.assertTrue(snap.stats, "the stats are still read")


class UsageAllBatchesFailTest(unittest.TestCase):
    def test_no_batch_in_is_a_failed_read(self):
        # 150 accounts in two batches, both refused: not an empty pool's usage ("正常"), a read that failed
        many = [dict(ACCOUNTS[0], id=3000 + i, name="claude-q%03d" % i) for i in range(150)]

        def opener(req, timeout=None):
            u = req.full_url
            if "/admin/accounts/usage" in u:
                raise urllib.error.HTTPError(u, 503, "busy", {}, None)
            if "/admin/accounts" in u:
                return io.BytesIO(json.dumps({"code": 0, "data": {"items": many, "total": len(many)}}).encode())
            return io.BytesIO(json.dumps({"code": 0, "data": STATS}).encode())

        page, _ = make(gateway=opener)
        with self.assertRaises(starkmesh.StarkMeshError):
            page.mesh._read()
        self.assertTrue(page.brief({}).startswith("StarkMesh 读不到："))


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
