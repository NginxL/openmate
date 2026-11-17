"""The "Agent 进度" page and card: the hub's sessions sorted by what they ask of the user."""

import datetime as dt
import json
import types
import unittest
from zoneinfo import ZoneInfo

import harness  # noqa: F401  (puts the core on the path)
from openmate_core import agents
from openmate_core.hub import HubError
from openmate_core.ui import check_view

TZ = ZoneInfo("Asia/Shanghai")
NOW = dt.datetime(2026, 11, 17, 15, 0, tzinfo=TZ).timestamp()
INPLACE = lambda fn: fn()  # noqa: E731


def at(minutes_ago: float) -> str:
    return dt.datetime.fromtimestamp(NOW - minutes_ago * 60, dt.timezone.utc).isoformat().replace("+00:00", "Z")


SESSIONS = [
    {"session_id": "s-wait", "title": "修复登录", "space_name": "persona", "state": "needs_you", "state_since": at(25),
     "updated_at": at(25), "activity": "在问：要不要合并？", "pending_interactions": 1},
    {"session_id": "s-card", "title": "整理周报", "space_name": "日常", "state": "idle", "state_since": at(5), "updated_at": at(5),
     "pending_interactions": 2},  # a card waiting counts as waiting for the user, whatever the state says
    {"session_id": "s-run", "title": "写测试", "space_name": "openmate", "state": "working", "state_since": at(12),
     "updated_at": at(1), "activity": "在跑测试", "context_tokens": 280000, "context_window": 1000000,
     "model": "claude-opus-5-5", "effort": "xhigh", "branch": "feat/agents", "last_text": "测试都过了，正在整理改动"},
    {"session_id": "s-quiet", "title": "迁移数据", "space_name": "starkmesh", "state": "working", "state_since": at(90),
     "updated_at": at(45)},
    {"session_id": "s-loop", "title": "等 CI", "space_name": "starkmesh", "state": "working", "state_since": at(90),
     "updated_at": at(60), "waiting": True},  # a background wait is quiet on purpose
    {"session_id": "s-new", "title": "早报", "state": "unread", "state_since": at(3), "updated_at": at(3),
     "cwd": "/Users/x/proj/日报", "last_text": "**结论**：今天先开会"},
    {"session_id": "s-done", "title": "改文案", "space_name": "openmate", "state": "idle", "updated_at": at(200),
     "state_since": at(200), "last_text": "改好了"},
    {"session_id": "s-old", "title": "上周的", "space_name": "openmate", "state": "offline", "updated_at": "2026-11-10T01:00:00Z",
     "state_since": "2026-11-10T01:00:00Z"},
    {"session_id": "s-assistant", "title": "主对话", "state": "working", "assistant": True},
]


class Hub:
    available = True

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def request(self, method, path, body=None, query=None, timeout=None):
        self.calls.append((method, path, query))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def make(answer=None):
    hub = Hub({"sessions": list(reversed(SESSIONS))} if answer is None else answer)  # the order is the page's, not the hub's
    app = types.SimpleNamespace(tz=TZ, hub=hub)
    read = agents.AgentsRead(app, clock=lambda: NOW, spawn=INPLACE)
    return agents.AgentsPage(app, read), agents.AgentsPage(app, read, card=True), hub


def draw(page, **params):
    params.setdefault("ui_version", 6)
    view = page.render(params)
    problems = check_view(view, compact=page.card)
    assert not problems, problems
    return view


def section(view, start):
    return [s for s in view["sections"] if str(s.get("title") or "").startswith(start)][0]


def keys(sec):
    return [it["key"].split(":", 1)[1] for it in sec["items"] if it["type"] == "row"]


class AgentsPageTest(unittest.TestCase):
    def test_groups_by_what_each_asks_of_the_user(self):
        page, _, hub = make()
        view = draw(page)
        self.assertEqual(view["title"], "Agent 进度")
        self.assertEqual(keys(section(view, "等你")), ["s-wait", "s-card"])  # the longest waiting first
        self.assertEqual(keys(section(view, "可能卡住")), ["s-quiet"])
        self.assertEqual(keys(section(view, "在跑")), ["s-loop", "s-run"])
        self.assertEqual(keys(section(view, "做完没看")), ["s-new"])
        self.assertEqual(keys(section(view, "今天做过")), ["s-done"])
        flat = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("上周的", flat)  # older than today and not waiting: not on the page
        self.assertNotIn("主对话", flat)  # the hub's own assistant is not an Agent here
        _, path, query = hub.calls[0]
        self.assertEqual(path, "/api/v1/sessions")
        self.assertEqual(query["roots"], 1)
        other, _, _ = make({"sessions": SESSIONS})  # the hub's order either way: the same page
        self.assertEqual(draw(other), view)

    def test_a_row_says_project_activity_time_and_context(self):
        page, _, _ = make()
        view = draw(page)
        run = [it for it in section(view, "在跑")["items"] if it.get("key") == "working:s-run"][0]
        self.assertEqual(run["subtitle"], "openmate · 在跑测试 · 已经 12 分钟")
        self.assertEqual(run["value"], "上下文 28%")
        loop = [it for it in section(view, "在跑")["items"] if it.get("key") == "working:s-loop"][0]
        self.assertIn("后台等待", loop["subtitle"])
        quiet = section(view, "可能卡住")["items"][0]
        self.assertIn("45 分钟没有动静", quiet["subtitle"])
        self.assertEqual(quiet["badge"]["tone"], "caution")
        new = section(view, "做完没看")["items"][0]
        self.assertTrue(new["subtitle"].startswith("日报 · **结论**：今天先开会"))  # no space name: the folder's

    def test_tiles_count_and_the_projects_section(self):
        page, _, _ = make()
        view = draw(page)
        grid = view["sections"][0]["items"][0]
        self.assertEqual([(t["title"], t["value"]) for t in grid["tiles"]],
                         [("等你", "2"), ("可能卡住", "1"), ("在跑", "2"), ("做完没看", "1"), ("今天做过", "1")])
        self.assertEqual(grid["tiles"][0]["badge"]["tone"], "danger")
        projects = section(view, "按项目")["items"]
        self.assertEqual(projects[0]["title"], "starkmesh")  # one stuck: first
        self.assertEqual(projects[0]["subtitle"], "可能卡住 1 · 在跑 1")

    def test_a_row_opens_its_details(self):
        page, _, _ = make()
        pushed = page.action({"action": {"id": "agent:s-run"}, "ui_version": 6})["push"]
        self.assertEqual(pushed["ui"], agents.PAGE)
        self.assertEqual(pushed["title"], "写测试")
        detail = draw(page, state=pushed["state"])
        self.assertEqual(detail["title"], "写测试")
        facts = {s["label"]: s["value"] for s in detail["sections"][0]["items"][0]["stats"]}
        self.assertEqual(facts["模型"], "Claude Opus 5.5 · xhigh")
        self.assertEqual(facts["上下文"], "28%")
        flat = json.dumps(detail, ensure_ascii=False)
        self.assertIn("feat/agents", flat)
        self.assertIn("测试都过了，正在整理改动", flat)
        back = page.action({"action": {"id": "back"}, "state": pushed["state"], "ui_version": 6})["view"]
        self.assertEqual(back["title"], "Agent 进度")
        gone = draw(page, state=json.dumps({"session": "nope"}))
        self.assertIn("不在最近的列表里", json.dumps(gone, ensure_ascii=False))

    def test_the_card_puts_waiting_first_and_only_shows(self):
        _, card, _ = make()
        view = draw(card)
        self.assertEqual([t["title"] for t in view["sections"][0]["items"][0]["tiles"]], ["等你", "可能卡住", "在跑", "做完没看"])
        rows = [it for it in view["sections"][1]["items"] if it["type"] == "row"]
        self.assertEqual([r["key"] for r in rows], ["needs:s-wait", "needs:s-card", "stuck:s-quiet"])
        self.assertFalse(any("action" in r for r in rows))
        quiet, _, _ = make({"sessions": []})
        self.assertEqual(draw(agents.AgentsPage(quiet.app, quiet.agents, card=True))["sections"][1]["items"][0]["type"], "empty")

    def test_an_unreachable_hub_says_so_and_old_clients_get_plain_parts(self):
        page, card, _ = make(HubError(502, "请求超时"))
        self.assertIn("请求超时", json.dumps(draw(page), ensure_ascii=False))
        self.assertIn("请求超时", json.dumps(draw(card), ensure_ascii=False))
        page, _, _ = make()
        v2 = draw(page, ui_version=2)
        self.assertEqual(v2["sections"][0]["items"][0]["type"], "stat_grid")
        self.assertFalse(any("icon" in it for s in v2["sections"] for it in s["items"] if it["type"] == "row"))

    def test_span_and_times(self):
        self.assertEqual(agents.span(30), "不到 1 分钟")
        self.assertEqual(agents.span(80 * 60), "1 小时 20 分")
        self.assertEqual(agents.span(3 * 86400), "3 天")
        self.assertEqual(agents.ts("0001-01-01T00:00:00Z"), 0.0)
        self.assertEqual(agents.ts("2026-11-17T06:24:06.49Z"), dt.datetime(2026, 11, 17, 6, 24, 6, 490000, dt.timezone.utc).timestamp())
        self.assertEqual(agents.ts("2026-11-17T06:24:06.123456789+08:00"),
                         dt.datetime(2026, 11, 16, 22, 24, 6, 123456, dt.timezone.utc).timestamp(), "Go's nine digits")
        self.assertEqual(agents.ts("2026-11-17T06:24:06Z"), dt.datetime(2026, 11, 17, 6, 24, 6, tzinfo=dt.timezone.utc).timestamp())


if __name__ == "__main__":
    unittest.main()
