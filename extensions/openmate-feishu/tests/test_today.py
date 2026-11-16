"""The 今天 page and card: Persona's Agents, inbox and to-dos through the hub API, Feishu's chats and calendar, one page."""

import datetime as dt
import os
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "..", "core"))

from openmate_core.hub import HubError  # noqa: E402
from openmate_core.store import Store  # noqa: E402
from openmate_core.ui import check_view  # noqa: E402
from openmate_feishu import agenda, today  # noqa: E402

from test_agenda import INPLACE, NOW, TZ, Runner, ev  # noqa: E402


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
    def __init__(self, needs, groups=None):
        self._needs = list(needs)
        self._groups = groups or []
        self.done = []

    def needs(self):
        return self._needs

    def dismiss(self, chat_id):
        hit = [t for t in self._needs if t["_chat"]["id"] == chat_id]
        self._needs = [t for t in self._needs if t not in hit]
        self.done += hit
        return bool(hit)

    bots = 0

    def counts(self, needs):
        return {"mentions": sum(1 for t in needs if t["kind"] == "mention"), "dms": sum(1 for t in needs if t["kind"] == "dm"),
                "messages": 120, "chats": 9, "bots": self.bots}

    def active_groups(self):
        return self._groups


GROUPS = [({"id": "g1", "kind": "group", "name": "示例项目群"}, {"id": "m1", "sender_name": "示例同事乙", "text": "今天发版", "type": "TEXT", "at": NOW - 600}),
          ({"id": "g2", "kind": "group", "name": "示例值班群"}, {"id": "m2", "sender_name": "示例同事丙", "text": "已处理", "type": "TEXT", "at": NOW - 1200})]


PERSONA = {
    "/api/v1/sessions?state=needs_you": {"sessions": [{"session_id": "s1", "title": "修复登录问题", "activity": "在问：要不要合并？", "agent": "claude",
                                                       "state_since": "2026-11-16T01:00:00Z"}]},
    "/api/v1/sessions?state=working": {"sessions": [{"session_id": "s2", "title": "整理周报", "activity": "读取文档", "agent": "codex"},
                                                     {"session_id": "s3", "space_name": "示例仓库", "agent": "claude"}]},
    "/api/v1/inbox": {"items": [{"id": "i1", "title": "示例告警", "summary": "服务异常", "tier": "action", "updated_at": "2026-11-16T01:00:00Z"},
                                {"id": "i2", "title": "周报提醒", "tier": "fyi"}]},
    "/api/v1/todos": {"todos": [{"id": "t1", "title": "提交评审材料", "due_date": "2026-11-16", "ask": {"verdict": "x"}},
                                {"id": "t2", "title": "下周的事", "due_date": "2026-11-20"},
                                {"id": "t3", "title": "已完成", "done_at": "2026-11-15T10:00:00Z"}]},
}
CHAT_NEEDS = [{"kind": "mention", "title": "示例项目群", "sender": "示例同事乙", "preview": "接口文档谁来更新？", "thread": "c1",
               "_chat": {"id": "c1"}, "_at": NOW - 300},
              {"kind": "dm", "title": "示例同事甲", "sender": "示例同事甲", "preview": "在吗", "thread": "c2", "_chat": {"id": "c2"}}]


def make(answers=None, needs=None, runner=None, clock=None, groups=None):
    clock = clock or (lambda: NOW)
    cfg = {"lark_cli": "/bin/sh", "lark_cli_profile": "persona"}
    hub = Hub(answers if answers is not None else PERSONA)
    d = tempfile.mkdtemp(prefix="openmate-today-")
    app = types.SimpleNamespace(config=cfg, tz=TZ, hub=hub, store=Store(os.path.join(d, "t.db")), me_name=lambda: "我")
    shared = agenda.Agenda(app, clock=clock, cli=agenda.LarkCLI(cfg, run=runner or Runner()), spawn=INPLACE)
    persona = today.PersonaToday(app, clock=clock, spawn=INPLACE)
    chats = Chats(CHAT_NEEDS if needs is None else needs, GROUPS if groups is None else groups)
    return (today.TodayPage(app, shared, persona, chats), today.TodayPage(app, shared, persona, chats, card=True), hub)


def items(view, compact=False):
    assert not check_view(view, compact=compact), check_view(view, compact=compact)
    return [it for s in view["sections"] for it in s["items"]]


def section(view, title):
    return [s for s in view["sections"] if s.get("title") == title][0]


def texts(sec):
    return [it["text"] for it in sec["items"] if it["type"] == "text"]


def rows(sec):
    return [it for it in sec["items"] if it["type"] == "row"]


class TodayTest(unittest.TestCase):
    def test_the_page_is_cards(self):
        page, _, hub = make()
        view = page.render({"ui_version": 6})
        items(view)
        self.assertEqual(view["title"], "今天 · 11月16日 周一")
        self.assertEqual([s.get("title") for s in view["sections"]],
                         ["需要我 · 5", "日程", "飞书今天", "正在跑的 Agent · 2", "待办 · 1"])
        needs = view["sections"][0]
        self.assertEqual(texts(needs), ["飞书 2 · 收件箱 1 · Agent 等你 1 · 待办等你拍板 1"])
        self.assertEqual([(r["title"], r["badge"]["text"], r["icon"]) for r in rows(needs)[:-1]],
                         [("修复登录问题", "Agent 等你", "robot"), ("示例告警", "收件箱", "mail"), ("提交评审材料", "等你拍板", "check"),
                          ("示例项目群", "@我", "chat"), ("示例同事甲", "私聊", "chat")])
        self.assertEqual(rows(needs)[3]["action"], {"id": "chat:c1"})
        self.assertEqual(rows(needs)[3]["value"], "09:55")
        self.assertEqual((rows(needs)[-1]["title"], rows(needs)[-1]["action"]), ("在“飞书今天”里看全部私聊和 @我 ›", {"id": "go:chats"}))
        cal = view["sections"][1]
        self.assertEqual(texts(cal), ["今天 3 个 · 会议中，10:30 结束 · 2 个邀请没回复"])
        self.assertEqual([r["title"] for r in rows(cal)], ["站会", "示例评审会", "交流分享", "本周还有 3 个 ›"])
        self.assertEqual((rows(cal)[0]["badge"], rows(cal)[0]["icon_color"]), ({"text": "进行中", "tone": "positive"}, "green"))
        self.assertEqual((rows(cal)[-1]["value"], rows(cal)[-1]["action"]), ("待回复 2", {"id": "go:calendar"}))
        feishu = view["sections"][2]
        self.assertEqual(texts(feishu), ["@我 1 · 私聊 1 · 今天收到 120 条，9 个会话"])
        self.assertEqual([(r["title"], r.get("subtitle")) for r in rows(feishu)],
                         [("示例项目群", "示例同事乙：今天发版"), ("示例值班群", "示例同事丙：已处理"), ("打开飞书今天 ›", None)])
        agents = view["sections"][3]
        self.assertEqual(texts(agents), ["在跑 2 · 等你 1（在“需要我”里）"])
        self.assertEqual([(r["title"], r["value"]) for r in rows(agents)], [("整理周报", "codex"), ("示例仓库", "claude")])
        todos = view["sections"][4]
        self.assertEqual(texts(todos), ["未完成 2 · 今天到期 1"])
        self.assertEqual([r["title"] for r in rows(todos)], ["下周的事"], "the one waiting for a decision is in 需要我")
        self.assertTrue(all(t == 8.0 for _, _, t in hub.calls), "short waits")

    def test_ui_version_8_lays_the_cards_out_two_by_two(self):
        page, _, _ = make()
        view = page.render({"ui_version": 8})
        items(view)
        self.assertEqual(view["layout"], "columns")
        needs, cal, feishu, agents, todos = view["sections"]
        self.assertEqual((needs.get("span"), needs.get("columns")), ("full", 2), "需要我 across, its rows two by two")
        self.assertEqual([s.get("span") for s in (cal, feishu, agents, todos)], [None] * 4, "the rest two to a row")
        self.assertEqual([(s["icon"], s["icon_color"]) for s in view["sections"]],
                         [("bell", "red"), ("calendar", "blue"), ("chat", "indigo"), ("robot", "teal"), ("check", "green")])
        self.assertEqual(needs["link"], {"title": "全部", "action": {"id": "go:chats"}})
        self.assertEqual(cal["link"], {"title": "日历", "action": {"id": "go:calendar"}})
        self.assertEqual(feishu["link"], {"title": "飞书", "action": {"id": "go:chats"}})
        self.assertNotIn("link", agents)
        self.assertNotIn("go:chats", str(needs["items"]) + str(feishu["items"]), "the links are on the cards, not rows")
        self.assertEqual(rows(cal)[-1]["title"], "本周还有 3 个 ›", "the week's rest stays at the bottom")
        self.assertEqual(texts(needs), ["飞书 2 · 收件箱 1 · Agent 等你 1 · 待办等你拍板 1"])
        # nothing waits: no link on 需要我, a gray bell
        page, _, _ = make(answers={"/api/v1/todos": {"todos": []}}, needs=[], groups=[])
        quiet = page.render({"ui_version": 8})["sections"][0]
        self.assertEqual((quiet["icon_color"], "link" in quiet), ("gray", False))

    def test_before_8_no_columns_fields(self):
        page, _, _ = make()
        for v in (6, 7):
            view = page.render({"ui_version": v})
            self.assertNotIn("layout", view)
            self.assertFalse(any(k in s for s in view["sections"] for k in ("span", "icon", "link", "columns")))

    def test_marked_done(self):
        now = [NOW]
        answers = {k: (dict(v) if isinstance(v, dict) else v) for k, v in PERSONA.items()}
        page, card, _ = make(answers=answers, clock=lambda: now[0])
        view = page.render({"ui_version": 6})
        needs = rows(view["sections"][0])
        self.assertEqual([r["actions"][0]["action"]["id"] for r in needs[:-1]],
                         ["done:wait:s1", "done:inbox:i1", "done:ask:t1", "done:chat:c1", "done:chat:c2"])
        self.assertTrue(all(r["actions"][0]["title"] == "处理完" for r in needs[:-1]))
        self.assertFalse(any("actions" in r for r in card.render({"ui_version": 6})["sections"][1]["items"]))
        self.assertFalse(any("actions" in r for r in rows(page.render({"ui_version": 5})["sections"][0])), "a row menu is 6")
        for aid in ("done:inbox:i1", "done:wait:s1", "done:ask:t1", "done:chat:c1"):
            out = page.action({"action": {"id": aid}, "ui_version": 6})
            self.assertIn("已标记处理完", out["toast"])
        view = out["view"]
        self.assertEqual(view["sections"][0]["title"], "需要我 · 1")
        self.assertEqual([r["title"] for r in rows(view["sections"][0])][:1], ["示例同事甲"])
        self.assertEqual(texts(view["sections"][0]), ["飞书 1 · 收件箱 0 · Agent 等你 0 · 已处理 4"])
        self.assertIn("没有要处理", page.action({"action": {"id": "done:inbox:nope"}, "ui_version": 6})["toast"])
        # the inbox item is updated, the Agent asks again: they come back; the others stay handled
        answers["/api/v1/inbox"] = {"items": [dict(PERSONA["/api/v1/inbox"]["items"][0], updated_at="2026-11-16T03:00:00Z")]}
        answers["/api/v1/sessions?state=needs_you"] = {"sessions": [dict(PERSONA["/api/v1/sessions?state=needs_you"]["sessions"][0],
                                                                        state_since="2026-11-16T03:00:00Z")]}
        now[0] += 700  # past STALE: a fresh read
        view = page.render({"ui_version": 6})
        self.assertEqual([r["title"] for r in rows(view["sections"][0])][:2], ["修复登录问题", "示例告警"])
        self.assertIn("已处理 2", texts(view["sections"][0])[0])

    def test_what_ai_answered_today(self):
        page, _, _ = make()
        store = page.app.store
        base = {"request_id": "m1", "thread_key": "m1", "root_id": "", "created": NOW - 600, "replies": 1,
                "request_text": "接口什么时候好？", "asker": "示例同事乙"}
        store.run_put(dict(base, id="omr_a", chat_id="g1", state="done", kind="auto"))
        store.run_put(dict(base, id="omr_b", chat_id="doc:tok", state="failed", kind="doc", replies=0, created=NOW - 300,
                           doc_title="示例方案", doc_url="https://example.feishu.cn/docx/tok", request_text="这里要改吗？"))
        store.run_put(dict(base, id="omr_c", chat_id="g1", state="done", request_id="m9"))  # an /agent run: not here
        view = page.render({"ui_version": 6})
        sec = section(view, "AI 代答 · 2")
        self.assertEqual(texts(sec), ["今天代答 1 次，没有代答 1 次"])
        self.assertEqual([(r["title"], r["badge"]["text"]) for r in rows(sec)], [("示例方案", "没有代答"), ("g1", "已代答")], "a chat the store lacks: its id")
        self.assertEqual(rows(sec)[0]["subtitle"], "示例同事乙：这里要改吗？")
        self.assertEqual(page.action({"action": {"id": rows(sec)[0]["action"]["id"]}}), {"open_url": "https://example.feishu.cn/docx/tok"})
        v8 = page.render({"ui_version": 8})
        card = [s for s in v8["sections"] if s.get("title", "").startswith("AI 代答")][0]
        self.assertEqual((card["icon"], card["link"]["title"]), ("robot", "设置"))

    def test_the_starkmesh_block(self):
        import test_starkmesh

        page, _, _ = make()
        mesh_page, _ = test_starkmesh.make()
        page.mesh = mesh_page
        view = page.render({"ui_version": 6})
        items(view)
        sec = section(view, "中转站")
        self.assertEqual(texts(sec), ["可用号 3/4 · 5 小时剩 1.30 号 · 本周剩 1.40 号"])
        self.assertEqual(page.action({"action": {"id": "go:starkmesh"}}), {"push": {"ui": "starkmesh", "title": "中转站 · StarkMesh"}})
        v8 = page.render({"ui_version": 8})
        card = [s for s in v8["sections"] if s.get("title") == "中转站"][0]
        self.assertEqual((card["icon"], card["link"]), ("gauge", {"title": "打开", "action": {"id": "go:starkmesh"}}))
        page.mesh, _ = test_starkmesh.make(key="")
        self.assertNotIn("中转站", [s.get("title") for s in page.render({"ui_version": 6})["sections"]], "no key: no block")

    def test_bot_notices_are_counted_apart(self):
        page, _, _ = make()
        page.chats.bots = 7
        feishu = section(page.render({"ui_version": 6}), "飞书今天")
        self.assertEqual(texts(feishu), ["@我 1 · 私聊 1 · 今天收到 120 条，9 个会话 · 机器人通知 7"])

    def test_links_open_pages_on_top(self):
        page, _, _ = make()
        self.assertEqual(page.action({"action": {"id": "go:calendar"}}), {"push": {"ui": "calendar", "title": "日程"}})
        self.assertEqual(page.action({"action": {"id": "go:chats"}}), {"push": {"ui": "chats", "title": "飞书今天"}})
        self.assertEqual(page.action({"action": {"id": "chat:g1"}}),
                         {"push": {"ui": "chats", "state": '{"chat":"g1"}', "title": "会话"}})
        self.assertEqual(page.action({"action": {"id": "open:e2"}}),
                         {"open_url": "https://applink.example.com/client/calendar/event/detail?id=e2"})
        self.assertIn("不在", page.action({"action": {"id": "open:gone"}})["toast"])

    def test_the_card(self):
        _, card, _ = make()
        view = card.render({"ui_version": 6, "size": "compact"})
        self.assertEqual(len(items(view, compact=True)), 4)
        grid = view["sections"][0]["items"][0]
        self.assertEqual([t["title"] for t in grid["tiles"]], ["需要我", "日程", "飞书", "Agent"])
        self.assertEqual(grid["tiles"][0]["value"], "5")
        self.assertNotIn("title", view)
        card_rows = view["sections"][1]["items"]
        self.assertEqual([r["title"] for r in card_rows], ["修复登录问题", "示例告警", "提交评审材料"])
        self.assertFalse(any("action" in r for r in card_rows), "a card only shows")

    def test_a_part_missing_leaves_the_rest(self):
        answers = dict(PERSONA)
        answers["/api/v1/inbox"] = HubError(502, "the manager could not reach the hub", "hub_unreachable")
        answers["/api/v1/todos"] = HubError(403, "forbidden", "forbidden")
        page, _, _ = make(answers=answers)
        view = page.render({"ui_version": 6})
        items(view)
        self.assertEqual(view["sections"][0]["title"], "需要我 · 3")
        note = view["sections"][-1]["items"][0]
        self.assertEqual(note["text"], "暂时读不到：Persona 收件箱、待办（稍后自动重试）")
        self.assertNotIn("manager", note["text"])
        self.assertNotIn("待办 · 0", [s.get("title") for s in view["sections"]])
        page, card, _ = make(runner=Runner({"ok": False, "error": {"type": "authorization"}}))
        view = page.render({"ui_version": 6})
        self.assertEqual(section(view, "日程")["items"][0]["title"], "日程读不到")
        tiles = card.render({"ui_version": 6})["sections"][0]["items"][0]["tiles"]
        self.assertEqual((tiles[1]["value"], tiles[1]["subtitle"]), ("—", "读不到"))

    def test_nothing_to_do(self):
        page, card, _ = make(answers={"/api/v1/todos": {"todos": []}}, needs=[], groups=[])
        view = page.render({"ui_version": 6})
        items(view)
        self.assertEqual(section(view, "需要我")["items"][1]["title"], "没有要你处理的事")
        self.assertEqual(section(view, "正在跑的 Agent")["items"][1]["title"], "现在没有在跑的 Agent")
        self.assertEqual(section(view, "飞书今天")["items"][1]["title"], "今天群里还没有新消息")
        self.assertNotIn("待办", [s.get("title", "")[:2] for s in view["sections"]], "no to-do card without to-dos")
        self.assertNotIn("go:chats", str(section(view, "需要我")), "no link to chats when none needs the user")
        self.assertEqual(card.render({"ui_version": 6})["sections"][1]["items"][0]["title"], "没有要你处理的事")

    def test_persona_read_once_a_minute(self):
        now = [NOW]
        page, card, hub = make(clock=lambda: now[0])
        page.render({"ui_version": 6})
        card.render({"ui_version": 6})
        self.assertEqual(len(hub.calls), 4, "the page and the card share one read")
        now[0] += 61
        page.render({"ui_version": 6})
        self.assertEqual(len(hub.calls), 8)

    def test_a_part_missing_later_keeps_the_last_whole_read(self):
        now = [NOW]
        answers = dict(PERSONA)
        page, _, hub = make(answers=answers, clock=lambda: now[0])
        page.render({"ui_version": 6})
        answers["/api/v1/inbox"] = HubError(502, "the manager could not reach the hub", "hub_unreachable")
        now[0] += 61
        view = page.render({"ui_version": 6})
        self.assertEqual(view["sections"][0]["title"], "需要我 · 5", "the last whole read")
        self.assertNotIn("暂时读不到", str(view))
        now[0] += 600
        self.assertIn("暂时读不到：Persona 收件箱", str(page.render({"ui_version": 6})))

    def test_old_apps(self):
        page, card, _ = make()
        v5 = page.render({"ui_version": 5})
        items(v5)
        self.assertFalse(any("icon" in it for s in v5["sections"] for it in s["items"]), "no row icons before 6")
        self.assertEqual(card.render({"ui_version": 5})["sections"][0]["items"][0]["type"], "stat_grid")
        v2 = page.render({"ui_version": 2})
        self.assertFalse(any("action" in it and it["action"]["id"].startswith(("go:", "chat:"))
                             for s in v2["sections"] for it in s["items"]), "no pages on top before 3")
        self.assertEqual(card.render({"ui_version": 1})["sections"][0]["items"][0]["type"], "text")

    def test_a_slow_draw_says_where_the_time_went(self):
        page, _, _ = make()
        with self.assertLogs("openmate.today", "INFO") as logs:
            page.persona.read = lambda: (__import__("time").sleep(0.35), dict(PERSONA_PARTS))[1]
            page.render({"ui_version": 6})
        self.assertIn("today took", logs.output[0])
        self.assertIn("Persona 3", logs.output[0])


PERSONA_PARTS = {"waiting": [], "working": [], "inbox": [], "todos": [], "missing": []}


if __name__ == "__main__":
    unittest.main()
