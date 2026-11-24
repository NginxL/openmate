"""WeChat in 今天: openmate-wechat's chats (its todayapi, through the hub) beside Feishu's, on the card and the page."""

import _runtmp  # noqa: F401  (first: every temporary directory of this run in one, removed at its end)
import json
import os
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "..", "core"))
sys.path.insert(0, HERE)

from openmate_core.hub import HubError  # noqa: E402
from openmate_core.store import Store  # noqa: E402
from openmate_feishu import agenda, layout, today  # noqa: E402

from test_agenda import INPLACE, NOW, TZ, Runner  # noqa: E402
from test_today import CHAT_NEEDS, GROUPS, PERSONA, Chats, Hub, body, items, rows, section, texts  # noqa: E402

BASE = "/api/v1/extensions/openmate-wechat/"
WX = {"state": "up", "platform": "微信", "account": "wxid_me",
      "counts": {"needs": 2, "mentions": 1, "dms": 1, "messages": 88, "chats": 6, "bots": 0, "done": 0},
      "needs": [{"chat": "w1@chatroom", "kind": "mention", "title": "示例家庭群", "sender": "示例亲戚", "preview": "周末吃饭吗",
                 "at": NOW - 100, "version": "m-901"},
                {"chat": "wxid_friend", "kind": "dm", "title": "示例朋友", "sender": "示例朋友", "preview": "在吗",
                 "at": NOW - 4000, "version": "m-902"}],
      "groups": [{"chat": "w1@chatroom", "title": "示例家庭群", "sender": "示例亲戚", "preview": "周末吃饭吗", "at": NOW - 100}]}


class WxHub(Hub):
    """The test hub, plus the calls to openmate-wechat's own HTTP: answers by path, POST bodies kept."""

    def __init__(self, answers):
        super().__init__(answers)
        self.posts = []

    def request(self, method, path, body=None, query=None, timeout=None):
        if method == "POST":
            self.posts.append((path, body))
        if path == BASE + "chat":
            self.calls.append((method, path + "?id=" + str((query or {}).get("id")), timeout))
            if (query or {}).get("account") != "wxid_me":
                raise HubError(409, "微信换了账号", "account_changed")
            a = self.answers.get(path)
            if isinstance(a, Exception):
                raise a
            return a
        return super().request(method, path, body, query, timeout)


def make(wx=WX, needs=None, answers=None, persona=PERSONA):
    hub_answers = dict(persona)
    if wx is not None:
        hub_answers[BASE + "today"] = json.loads(json.dumps(wx)) if isinstance(wx, dict) else wx
    hub_answers.update(answers or {})
    hub = WxHub(hub_answers)
    cfg = {"lark_cli": "/bin/sh", "lark_cli_profile": "persona"}
    d = tempfile.mkdtemp(prefix="openmate-today-wx-")
    notes = []
    app = types.SimpleNamespace(config=cfg, tz=TZ, hub=hub, store=Store(os.path.join(d, "t.db")), me_name=lambda: "我",
                                peer=types.SimpleNamespace(notify=lambda e, p: notes.append((e, p))))
    clock = lambda: NOW  # noqa: E731
    shared = agenda.Agenda(app, clock=clock, cli=agenda.LarkCLI(cfg, run=Runner()), spawn=INPLACE)
    persona_read = today.PersonaToday(app, clock=clock, spawn=INPLACE)
    wechat = today.PeerToday(app, clock=clock, spawn=INPLACE)
    chats = Chats(CHAT_NEEDS if needs is None else needs, GROUPS)
    page = today.TodayPage(app, shared, persona_read, chats, wechat=wechat)
    card = today.TodayPage(app, shared, persona_read, chats, card=True, wechat=wechat)
    return page, card, hub, notes


def tiles(view):
    return [t for s in view["sections"] for it in s["items"] if it.get("type") == "grid" for t in it["tiles"]]


class WeChatTodayTest(unittest.TestCase):
    def test_the_card_has_a_wechat_tile_and_its_needs(self):
        _, card, _, _ = make()
        view = card.render({"ui_version": 6})
        items(view, compact=True)
        grids = [it for it in view["sections"][0]["items"] if it["type"] == "grid"]
        self.assertEqual([len(g["tiles"]) for g in grids], [3, 2], "four tiles at most in a card's grid")
        ts = {t["title"]: t for t in tiles(view)}
        self.assertEqual((ts["微信"]["value"], ts["微信"]["subtitle"], ts["微信"]["badge"]["text"]),
                         ("2", "@我 1 · 私聊 1 · 今天 88 条", "@我 1"))
        self.assertEqual(ts["需要我"]["value"], "7")
        self.assertIn("微信 2", ts["需要我"]["subtitle"])
        first = view["sections"][1]["items"]
        self.assertEqual([r["title"] for r in first], ["修复登录问题", "示例告警", "提交评审材料"], "Persona's come first")

    def test_the_page_merges_feishu_and_wechat_by_time(self):
        page, _, _, _ = make()
        view = page.render({"ui_version": 6})
        items(view)
        needs = section(view, "需要我 · 7")
        self.assertIn("微信 2", texts(needs)[0])
        chat_rows = [r for r in rows(needs) if r.get("badge", {}).get("text") in ("@我", "私聊", "微信 @我", "微信私聊")]
        self.assertEqual([(r["title"], r["badge"]["text"]) for r in chat_rows],
                         [("示例家庭群", "微信 @我"), ("示例项目群", "@我"), ("示例朋友", "微信私聊"), ("示例同事甲", "私聊")])
        wx_row = chat_rows[0]
        self.assertEqual(wx_row["action"], {"id": "wx:w1%40chatroom:wxid_me"})
        self.assertEqual(wx_row["actions"][0]["action"], {"id": "done:wx:w1%40chatroom:m-901:wxid_me"})
        self.assertEqual(wx_row["icon_color"], "green")
        wechat = section(view, "微信今天")
        self.assertEqual(texts(wechat), ["@我 1 · 私聊 1 · 今天收到 88 条，6 个会话"])
        self.assertEqual([r["title"] for r in rows(wechat)], ["示例家庭群"])
        self.assertEqual([t["title"] for t in view["sections"][1]["items"][0]["tiles"]], ["需要我", "日程", "飞书", "微信", "Agent"])

    def test_without_wechat_nothing_of_it(self):
        page, card, _, _ = make(wx=HubError(404, "no such extension", "not_found"))
        view = page.render({"ui_version": 6})
        items(view)
        self.assertNotIn("微信", [t["title"] for t in tiles(view)])
        self.assertNotIn("微信今天", [s.get("title") for s in body(view)])
        self.assertNotIn("微信", texts(section(view, "需要我 · 5"))[0])
        c = card.render({"ui_version": 6})
        items(c, compact=True)
        self.assertEqual([len(it["tiles"]) for it in c["sections"][0]["items"]], [4])
        edit = page.render({"ui_version": 6, "state": json.dumps({"edit": 1})})
        self.assertNotIn("微信今天", [r["title"] for r in rows(edit["sections"][0])], "not offered in 调整布局")

    def test_denied_is_left_out_too(self):
        page, _, _, _ = make(wx=HubError(403, "missing scope", "permission_denied"))
        self.assertNotIn("微信", [t["title"] for t in tiles(page.render({"ui_version": 6}))])

    def test_signed_out(self):
        page, _, _, _ = make(wx={"state": "signed_out", "platform": "微信", "account": ""})
        view = page.render({"ui_version": 6})
        items(view)
        ts = {t["title"]: t for t in tiles(view)}
        self.assertEqual((ts["微信"]["value"], ts["微信"]["badge"]["text"]), ("—", "未登录"))
        self.assertIn("微信 —", ts["需要我"]["subtitle"])
        self.assertEqual(rows(section(view, "微信今天"))[0]["title"] if rows(section(view, "微信今天")) else
                         section(view, "微信今天")["items"][0]["title"], "还没有登录微信：在 微信 页扫码登录")

    def test_unreachable_is_not_all_clear(self):
        quiet = {k: ({"sessions": []} if "sessions" in k else {"items": []} if "inbox" in k else {"todos": []}) for k in PERSONA}
        page, card, _, _ = make(wx=HubError(502, "the manager could not reach the hub", "hub_unreachable"), needs=[],
                                persona=quiet)
        view = page.render({"ui_version": 6})
        items(view)
        ts = {t["title"]: t for t in tiles(view)}
        self.assertEqual(ts["需要我"]["badge"]["text"], "微信读不到", "not 清空 while WeChat is unknown")
        self.assertIn("暂时读不到：微信（稍后自动重试）", [t for s in view["sections"] for t in texts(s)])
        c = card.render({"ui_version": 6})
        self.assertEqual(c["sections"][1]["items"][0]["title"], "微信暂时读不到，其他的没有要你处理的事")

    def test_done_writes_back_the_version_seen(self):
        page, _, hub, notes = make()
        hub.answers[BASE + "done"] = {"ok": True}
        view = page.render({"ui_version": 6})
        after = json.loads(json.dumps(WX))
        after["needs"] = after["needs"][1:]
        after["counts"].update(needs=1, mentions=0)
        hub.answers[BASE + "today"] = after
        out = page.action({"ui_version": 6, "action": {"id": "done:wx:w1%40chatroom:m-901:wxid_me"}, "state": ""})
        self.assertEqual(hub.posts, [(BASE + "done", {"chat": "w1@chatroom", "version": "m-901", "account": "wxid_me"})])
        self.assertEqual(out["toast"], "已标记处理完，有新消息会再出现")
        self.assertNotIn("示例家庭群", [r["title"] for r in rows(section(out["view"], "需要我 · 6"))], "read again")
        self.assertEqual(sorted(p["contribution"] for e, p in notes if e == "ui.invalidate"), sorted([today.CARD, today.PAGE]))
        del view

    def test_done_on_a_need_that_changed(self):
        page, _, hub, _ = make()
        hub.answers[BASE + "done"] = HubError(409, "有新消息，已刷新", "changed")
        out = page.action({"ui_version": 6, "action": {"id": "done:wx:w1%40chatroom:m-901:wxid_me"}, "state": ""})
        self.assertEqual(out["toast"], "有新消息，已刷新")
        self.assertIn("view", out)
        hub.answers[BASE + "done"] = HubError(502, "down", "hub_unreachable")
        self.assertEqual(page.action({"ui_version": 6, "action": {"id": "done:wx:w1%40chatroom:m-901:wxid_me"}, "state": ""})["toast"],
                         "微信扩展暂时连不上，稍后再试")
        hub.answers[BASE + "done"] = HubError(409, "微信换了账号", "account_changed")
        out = page.action({"ui_version": 6, "action": {"id": "done:wx:w1%40chatroom:m-901:wxid_old"}, "state": ""})
        self.assertEqual(out["toast"], "微信换了账号，已刷新")
        self.assertEqual(hub.posts[-1][1]["account"], "wxid_old", "the account the page was drawn for, not today's")

    def test_a_wechat_chat_opens_here(self):
        page, _, hub, _ = make()
        out = page.action({"ui_version": 6, "action": {"id": "wx:w1%40chatroom:wxid_me"}, "state": ""})
        self.assertEqual(out["push"]["ui"], today.PAGE)
        self.assertEqual(json.loads(out["push"]["state"]), {"wx": "w1@chatroom", "acct": "wxid_me"})
        self.assertEqual(out["push"]["title"], "示例家庭群")
        hub.answers[BASE + "chat"] = {"chat": "w1@chatroom", "title": "示例家庭群", "kind": "mention", "version": "m-901",
                                      "need": True, "lines": [{"who": "示例亲戚", "text": "周末吃饭吗", "at": NOW - 100, "mine": False}]}
        view = page.render({"ui_version": 6, "state": out["push"]["state"]})
        items(view)
        self.assertEqual(view["title"], "示例家庭群")
        self.assertEqual(json.loads(view["state"]), {"wx": "w1@chatroom", "acct": "wxid_me"})
        self.assertEqual([(r["title"], r.get("subtitle")) for r in rows(view["sections"][0])], [("示例亲戚", "周末吃饭吗")])
        done = rows(view["sections"][1])[0]
        self.assertEqual(done["action"], {"id": "done:wx:w1%40chatroom:m-901:wxid_me"})
        self.assertIn(("GET", BASE + "chat?id=w1@chatroom", 8.0), hub.calls)
        hub.answers[BASE + "done"] = {"ok": True}
        back = page.action({"ui_version": 6, "action": done["action"], "state": view["state"]})
        self.assertEqual((back["toast"], back.get("pop")), ("已标记处理完，有新消息会再出现", True))
        hub.answers[BASE + "done"] = HubError(409, "有新消息，已刷新", "changed")
        again = page.action({"ui_version": 6, "action": done["action"], "state": view["state"]})
        self.assertEqual(again["toast"], "有新消息，已刷新")
        self.assertEqual(again["view"]["title"], "示例家庭群", "the chat drawn again, not a toast alone")
        hub.answers[BASE + "chat"] = HubError(404, "gone", "not_found")
        gone = page.render({"ui_version": 6, "state": out["push"]["state"]})
        self.assertEqual(gone["sections"][0]["items"][0]["title"], "这个会话现在没有要处理的了")
        other = page.render({"ui_version": 6, "state": json.dumps({"wx": "w1@chatroom", "acct": "wxid_old"})})
        self.assertEqual((other["title"], other["sections"][0]["items"][0]["title"]), ("微信", "微信换了账号，这一件不在了"),
                         "a page drawn for the account before shows nothing of this one's")

    def test_more_than_twelve_needs_open_here(self):
        many = json.loads(json.dumps(WX))
        many["needs"] = [{"chat": "w%d" % i, "kind": "dm", "title": "朋友%d" % i, "sender": "朋友%d" % i, "preview": "在吗",
                          "at": NOW - 60 * i, "version": "v%d" % i} for i in range(15)]
        many["counts"].update(needs=15, mentions=0, dms=15)
        page, _, _, _ = make(wx=many)
        view = page.render({"ui_version": 6})
        needs = [s for s in body(view) if s["title"].startswith("需要我")][0]
        more = [r for r in rows(needs) if r["title"].startswith("展开全部")]
        self.assertEqual(more[0]["action"], {"id": "needs:all"})
        out = page.action({"ui_version": 6, "action": {"id": "needs:all"}, "state": ""})
        items(out["view"])
        needs = [s for s in body(out["view"]) if s["title"].startswith("需要我")][0]
        self.assertEqual(len([r for r in rows(needs) if r.get("badge", {}).get("text") == "微信私聊"]), 15)
        self.assertEqual(json.loads(out["view"]["state"]), {"all": 1})
        ts = {t["title"]: t for t in tiles(out["view"])}
        self.assertEqual(ts["微信"]["value"], "15")
        self.assertIn("微信 15", ts["需要我"]["subtitle"], "the tile and the list agree")

    def test_a_malformed_need_is_left_out_not_the_page(self):
        bad = json.loads(json.dumps(WX))
        bad["needs"] += [{"chat": "w9", "version": "v9"}, {"chat": "w8", "kind": "dm", "version": "v8", "at": "soon"}, "x"]
        page, _, _, _ = make(wx=bad)
        view = page.render({"ui_version": 6})
        items(view)
        titles = [r["title"] for r in rows(section(view, "需要我 · 8"))]
        self.assertNotIn("w9", titles)
        self.assertIn("微信", [t["title"] for t in tiles(view)])

    def test_the_page_says_wechat_is_unknown_too(self):
        quiet = {k: ({"sessions": []} if "sessions" in k else {"items": []} if "inbox" in k else {"todos": []}) for k in PERSONA}
        page, _, _, _ = make(wx=HubError(502, "down", "hub_unreachable"), needs=[], persona=quiet)
        needs = [s for s in body(page.render({"ui_version": 6})) if s["title"].startswith("需要我")][0]
        self.assertEqual(needs["items"][1]["title"], "微信暂时读不到，其他的没有要你处理的事")

    def test_a_layout_keeps_its_order_while_wechat_is_away(self):
        page, _, _, _ = make(wx=HubError(404, "no such extension", "not_found"))
        st = json.dumps({"edit": 1})
        page.action({"ui_version": 6, "action": {"id": "layout:down:feishu"}, "state": st})
        page.action({"ui_version": 6, "action": {"id": "layout:up:feishu"}, "state": st})
        self.assertEqual(page.layout().order[3:6], ["feishu", "wechat", "agents"], "back as it was, 微信今天 in its place")

    def test_one_read_for_the_page_and_the_card(self):
        page, card, hub, _ = make()
        page.render({"ui_version": 6})
        card.render({"ui_version": 6})
        self.assertEqual(len([c for c in hub.calls if c[1] == BASE + "today"]), 1)

    def test_layout_block_and_manifest(self):
        self.assertEqual([b[0] for b in layout.BLOCKS].index("wechat"), [b[0] for b in layout.BLOCKS].index("feishu") + 1)
        m = json.load(open(os.path.join(os.path.dirname(HERE), "persona-extension.json"), encoding="utf-8"))
        self.assertIn("extensions.call:openmate-wechat", m["permissions"]["hub"])


if __name__ == "__main__":
    unittest.main()
