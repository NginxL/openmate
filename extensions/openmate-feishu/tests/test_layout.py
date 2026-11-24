"""The 今天 page as the user arranges it: 调整布局 (order, hidden, whole row or half), the status tiles, the Agent
block from the Agent progress read, the shortcuts and the bookmarks."""

import _runtmp  # noqa: F401  (first: every temporary directory of this run in one, removed at its end)
import datetime as dt
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "..", "core"))

from openmate_core import agents  # noqa: E402
from openmate_core.ui import check_view  # noqa: E402
from openmate_feishu import layout, today  # noqa: E402

from test_agenda import INPLACE, NOW  # noqa: E402
from test_today import PERSONA, body, make  # noqa: E402


def act(page, aid, state=""):
    out = page.action({"action": {"id": aid}, "ui_version": 6, "state": state})
    if "view" in out:
        assert not check_view(out["view"]), check_view(out["view"])
    return out


def titles(view):
    return [s["title"] for s in body(view)]


class LayoutTest(unittest.TestCase):
    def test_edit_mode_moves_hides_and_widens(self):
        page, _, _ = make()
        out = act(page, "layout:edit")
        edit = out["view"]
        self.assertEqual(json.loads(edit["state"]), {"edit": 1})
        rows = [it for it in edit["sections"][0]["items"] if it["type"] == "row"]
        self.assertEqual([r["title"] for r in rows], [b[1] for b in layout.BLOCKS if b[0] != "wechat"], "微信今天 only with WeChat")
        first = [a["title"] for a in rows[0]["actions"]]
        self.assertEqual(first, ["下移", "隐藏", "改成半行"], "the first can't go up; status is a whole row")
        self.assertIn("hub 升级后生效", rows[2]["subtitle"], "a half row before UI version 8 says when it counts")
        state = edit["state"]
        self.assertEqual(act(page, "layout:down:feishu", state)["toast"], "已下移“飞书今天”")
        self.assertEqual(act(page, "layout:hide:calendar", state)["toast"], "已隐藏“日程”")
        self.assertEqual(act(page, "layout:wide:agents", state)["toast"], "“Agent”改成整行")
        self.assertNotIn("toast", act(page, "layout:up:status", state), "nothing to do: no toast")
        view = act(page, "layout:done", state)["view"]
        self.assertEqual(view["state"], "")
        self.assertEqual(titles(view), ["需要我 · 5", "正在跑的 Agent · 2", "飞书今天", "待办 · 1", "快捷入口"])
        v8 = page.render({"ui_version": 8})
        spans = {s.get("title"): s.get("span") for s in body(v8)}
        self.assertEqual(spans["正在跑的 Agent · 2"], "full")
        self.assertIsNone(spans["飞书今天"])
        # kept: a new page (another draw) has the same arrangement, and 恢复默认 puts it back
        self.assertEqual(titles(page.render({"ui_version": 6})), titles(view))
        self.assertEqual(act(page, "layout:show:calendar", state)["toast"], "已显示“日程”")
        act(page, "layout:reset", state)
        self.assertEqual(titles(page.render({"ui_version": 6})),
                         ["需要我 · 5", "日程", "飞书今天", "正在跑的 Agent · 2", "待办 · 1", "快捷入口"])

    def test_a_stored_layout_meets_a_newer_version(self):
        page, _, _ = make()
        older = ["todos"] + [b[0] for b in layout.BLOCKS if b[0] not in ("todos", "agents")]  # a version without "agents"
        page.app.store.meta_set(layout.KEY, json.dumps({"order": older[:3] + ["gone", "needs"] + older[3:], "hidden": ["gone", "status"],
                                                        "wide": {"todos": True, "needs": "yes"}}))
        lay = page.layout()
        self.assertEqual(lay.order, ["todos", "status", "needs", "calendar", "feishu", "wechat", "agents", "mesh", "auto", "shortcuts", "bookmarks"],
                         "a block the stored order lacks goes after the one before it by default; unknown and repeated ones go")
        self.assertEqual(lay.hidden, ["status"])
        self.assertEqual(lay.wide, {"todos": True})
        page.app.store.meta_set(layout.KEY, "not json")
        self.assertEqual(page.layout().order, layout.Layout.default().order)

    def test_old_apps_edit_with_buttons(self):
        page, _, _ = make()
        edit = page.render({"ui_version": 5, "state": json.dumps({"edit": 1})})
        self.assertFalse(check_view(edit))
        buttons = [it["action"]["id"] for it in edit["sections"][0]["items"] if it["type"] == "button"]
        self.assertIn("layout:down:status", buttons)
        self.assertFalse(any("actions" in it for it in edit["sections"][0]["items"]))


class BlocksTest(unittest.TestCase):
    def test_status_tiles_have_starkmesh_when_set(self):
        from test_starkmesh import make as make_mesh

        page, card, _ = make()
        page.mesh, _ = make_mesh()
        tiles = page.render({"ui_version": 6})["sections"][1]["items"][0]["tiles"]
        self.assertEqual([t["title"] for t in tiles], ["需要我", "日程", "飞书", "Agent", "中转站"])
        self.assertEqual((tiles[-1]["value"], tiles[-1]["badge"]), ("3/4", {"text": "偏紧", "tone": "caution"}))
        self.assertEqual(len(card.render({"ui_version": 6})["sections"][0]["items"][0]["tiles"]), 4, "the Overview card stays four")

    def test_the_agent_block_sorts_by_the_progress_read(self):
        answers = dict(PERSONA)
        stamp = lambda m: dt.datetime.fromtimestamp(NOW - m * 60, dt.timezone.utc).isoformat()  # noqa: E731
        answers["/api/v1/sessions"] = {"sessions": [
            {"session_id": "a1", "title": "长任务", "space_name": "示例仓库", "state": "working", "state_since": stamp(90), "updated_at": stamp(50)},
            {"session_id": "a2", "title": "写文档", "space_name": "示例仓库", "state": "working", "state_since": stamp(5), "updated_at": stamp(1)},
            {"session_id": "a3", "title": "早上的", "space_name": "示例仓库", "state": "idle", "state_since": stamp(60), "updated_at": stamp(60)}]}
        page, _, _ = make(answers=answers)
        page.agents = agents.AgentsRead(page.app, clock=lambda: NOW, spawn=INPLACE)
        view = page.render({"ui_version": 6})
        sec = [s for s in body(view) if s["title"].startswith("Agent")][0]
        self.assertEqual(sec["title"], "Agent · 2")
        self.assertEqual(sec["items"][0]["text"], "可能卡住 1 · 在跑 1 · 今天做过 1")
        rows = [it for it in sec["items"] if it["type"] == "row"]
        self.assertEqual([(r["title"], r["action"]["id"]) for r in rows[:2]], [("长任务", "agent:a1"), ("写文档", "agent:a2")])
        self.assertEqual(rows[-1]["action"], {"id": "go:agents"})
        pushed = act(page, "agent:a1")["push"]
        self.assertEqual((pushed["ui"], json.loads(pushed["state"])), (agents.PAGE, {"session": "a1"}))
        self.assertEqual(act(page, "go:agents")["push"]["ui"], agents.PAGE)

    def test_shortcuts_and_bookmarks(self):
        page, _, _ = make()
        view = page.render({"ui_version": 6})
        grid = [s for s in body(view) if s["title"] == "快捷入口"][0]["items"][0]
        self.assertEqual([(t["title"], t["action"]["id"]) for t in grid["tiles"]],
                         [("飞书", "go:chats"), ("日程", "go:calendar"), ("Agent 进度", "go:agents")], "no StarkMesh page: no tile")
        self.assertNotIn("书签", titles(view), "no bookmarks: no block")
        page.app.config["bookmarks"] = ["示例仓库 | https://example.com/repo | 代码和脚本", "坏的 | http://example.com", "监控|https://example.com/grafana"]
        view = page.render({"ui_version": 6})
        marks = [s for s in body(view) if s["title"] == "书签"][0]["items"]
        self.assertEqual([(r["title"], r["subtitle"], r["action"]["id"]) for r in marks],
                         [("示例仓库", "代码和脚本", "url:0"), ("监控", "https://example.com/grafana", "url:1")])
        self.assertEqual(act(page, "url:1"), {"open_url": "https://example.com/grafana"})
        self.assertIn("不在了", act(page, "url:7")["toast"])
        self.assertEqual(today.bookmarks({"bookmarks": ["x | https://a.example"] * 40})[29:], [("x", "https://a.example", "")])


if __name__ == "__main__":
    unittest.main()
