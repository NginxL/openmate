"""The 今天 page and card: Persona's Agents, inbox and to-dos through the hub API, Feishu's chats and calendar, one page."""

import datetime as dt
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "..", "core"))

from openmate_core.hub import HubError  # noqa: E402
from openmate_core.ui import check_view  # noqa: E402
from openmate_feishu import agenda, today  # noqa: E402

from test_agenda import NOW, TZ, Runner, ev  # noqa: E402


class Hub:
    available = True

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def request(self, method, path, body=None, query=None, timeout=None):
        key = path + ("?state=" + query["state"] if query and "state" in query else "")
        self.calls.append((method, key, timeout))
        a = self.answers.get(key)
        if isinstance(a, Exception):
            raise a
        return a if a is not None else {}


class Chats:
    def __init__(self, needs):
        self._needs = needs

    def needs(self):
        return self._needs


PERSONA = {
    "/api/v1/sessions?state=needs_you": {"sessions": [{"session_id": "s1", "title": "修复登录问题", "activity": "在问：要不要合并？", "agent": "claude"}]},
    "/api/v1/sessions?state=working": {"sessions": [{"session_id": "s2", "title": "整理周报", "activity": "读取文档", "agent": "codex"},
                                                     {"session_id": "s3", "space_name": "示例仓库", "agent": "claude"}]},
    "/api/v1/inbox": {"items": [{"id": "i1", "title": "示例告警", "summary": "服务异常", "tier": "action"},
                                {"id": "i2", "title": "周报提醒", "tier": "fyi"}]},
    "/api/v1/todos": {"todos": [{"id": "t1", "title": "提交评审材料", "due_date": "2026-11-16", "ask": {"verdict": "x"}},
                                {"id": "t2", "title": "下周的事", "due_date": "2026-11-20"},
                                {"id": "t3", "title": "已完成", "done_at": "2026-11-15T10:00:00Z"}]},
}
CHAT_NEEDS = [{"kind": "mention", "title": "示例项目群", "sender": "示例同事乙", "preview": "接口文档谁来更新？", "thread": "c1"},
              {"kind": "dm", "title": "示例同事甲", "sender": "示例同事甲", "preview": "在吗", "thread": "c2"}]


def make(answers=None, needs=None, runner=None, clock=None):
    clock = clock or (lambda: NOW)
    cfg = {"lark_cli": "/bin/sh", "lark_cli_profile": "persona"}
    hub = Hub(answers if answers is not None else PERSONA)
    app = types.SimpleNamespace(config=cfg, tz=TZ, hub=hub)
    shared = agenda.Agenda(app, clock=clock, cli=agenda.LarkCLI(cfg, run=runner or Runner()))
    persona = today.PersonaToday(app, clock=clock)
    chats = Chats(CHAT_NEEDS if needs is None else needs)
    return (today.TodayPage(app, shared, persona, chats), today.TodayPage(app, shared, persona, chats, card=True), hub)


def items(view, compact=False):
    assert not check_view(view, compact=compact), check_view(view, compact=compact)
    return [it for s in view["sections"] for it in s["items"]]


def section(view, title):
    return [s for s in view["sections"] if s.get("title") == title][0]


class TodayTest(unittest.TestCase):
    def test_the_page(self):
        page, _, hub = make()
        view = page.render({"ui_version": 6})
        self.assertEqual(view["title"], "今天 · 11月16日 周一")
        grid = items(view)[0]["stats"]
        self.assertEqual([(s["label"], s["value"]) for s in grid], [("需要我", "4"), ("日程", "3 个"), ("待办", "2"), ("Agent", "2 在跑")])
        self.assertEqual(grid[0]["detail"], "Agent 1 · 收件箱 1 · 飞书 2")
        self.assertEqual(grid[1]["detail"], "会议中")
        self.assertEqual(grid[2]["detail"], "今天到期 1")
        self.assertEqual(grid[3]["detail"], "等你 1")
        needs = section(view, "需要我")["items"]
        self.assertEqual([(r["title"], r["badge"]["text"]) for r in needs],
                         [("修复登录问题", "Agent 等你"), ("示例告警", "收件箱"), ("示例项目群", "@我"), ("示例同事甲", "私聊")])
        cal = section(view, "日程")
        self.assertEqual([r["title"] for r in cal["items"]], ["站会", "示例评审会", "交流分享"])
        self.assertEqual(cal["items"][0]["badge"], {"text": "进行中", "tone": "positive"})
        self.assertEqual(cal["footer"], "还有 2 个邀请没回复，在“日程”页里看")
        work = section(view, "在推进的事")["items"]
        self.assertEqual([r["title"] for r in work], ["提交评审材料", "下周的事", "整理周报", "示例仓库"])
        self.assertEqual(work[0]["badge"]["text"], "等你拍板")
        self.assertIn("到期 11-16", work[0]["subtitle"])
        self.assertTrue(all(t == 8.0 for _, _, t in hub.calls), "short waits")

    def test_the_card(self):
        _, card, _ = make()
        view = card.render({"ui_version": 6, "size": "compact"})
        self.assertEqual(len(items(view, compact=True)), 4)
        self.assertNotIn("title", view)
        self.assertEqual([r["title"] for r in view["sections"][1]["items"]], ["修复登录问题", "示例告警", "示例项目群"])

    def test_a_part_missing_leaves_the_rest(self):
        answers = dict(PERSONA)
        answers["/api/v1/inbox"] = HubError(502, "the manager could not reach the hub", "hub_unreachable")
        answers["/api/v1/todos"] = HubError(403, "forbidden", "forbidden")
        page, _, _ = make(answers=answers)
        view = page.render({"ui_version": 6})
        self.assertEqual(items(view)[0]["stats"][0]["value"], "3")
        note = view["sections"][-1]["items"][0]
        self.assertEqual(note["text"], "暂时读不到：Persona 收件箱、待办（稍后自动重试）")
        self.assertNotIn("manager", note["text"])
        page, _, _ = make(runner=Runner({"ok": False, "error": {"type": "authorization"}}))
        view = page.render({"ui_version": 6})
        self.assertEqual(items(view)[0]["stats"][1]["value"], "—")
        self.assertEqual(section(view, "日程")["items"][0]["title"], "日程读不到")

    def test_nothing_to_do(self):
        page, card, _ = make(answers={"/api/v1/todos": {"todos": []}}, needs=[])
        view = page.render({"ui_version": 6})
        self.assertEqual(section(view, "需要我")["items"][0]["title"], "没有要你处理的事")
        self.assertEqual(section(view, "在推进的事")["items"][0]["title"], "没有进行中的待办和 Agent")
        self.assertEqual(card.render({"ui_version": 6})["sections"][1]["items"][0]["title"], "没有要你处理的事")

    def test_persona_read_once_a_minute_and_events_open(self):
        now = [NOW]
        page, card, hub = make(clock=lambda: now[0])
        page.render({"ui_version": 6})
        card.render({"ui_version": 6})
        self.assertEqual(len(hub.calls), 4, "the page and the card share one read")
        now[0] += 61
        page.render({"ui_version": 6})
        self.assertEqual(len(hub.calls), 8)
        self.assertEqual(page.action({"action": {"id": "open:e2"}}),
                         {"open_url": "https://applink.example.com/client/calendar/event/detail?id=e2"})

    def test_old_apps(self):
        page, _, _ = make()
        view = page.render({"ui_version": 1})
        self.assertEqual(view["sections"][0]["items"][0]["type"], "text")


if __name__ == "__main__":
    unittest.main()
