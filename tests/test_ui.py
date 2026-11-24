"""The extension's page and card in the Persona apps (ui.render / ui.action) and the navigation badge."""

import _runtmp  # noqa: F401  (first: every temporary directory of this run in one, removed at its end)
import time
import unittest

from harness import Harness, HubSideError, wait_until


def signed_in(test):
    h = Harness(test)
    h.sign_in()
    wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
    return h


def ui(h, method, **params):
    from openmate_core.ui import check_view

    out = h.hub.request("ui." + method, params)
    view = out if method == "render" else out.get("view")
    if view is not None:
        problems = check_view(view, compact=params.get("contribution", "").endswith("-today"))
        assert not problems, problems
    return out


def items(view):
    return [it for s in view["sections"] for it in s["items"]]


class CheckViewV8Test(unittest.TestCase):
    def test_columns_fields(self):
        from openmate_core.ui import check_view

        ok = {"layout": "columns", "sections": [
            {"title": "需要我", "span": "full", "columns": 2, "icon": "bell", "icon_color": "red",
             "link": {"title": "全部", "action": {"id": "go:chats"}}, "items": [{"type": "text", "text": "x"}]}]}
        self.assertEqual(check_view(ok), [])
        bad = {"layout": "grid", "sections": [
            {"span": "half", "columns": 4, "icon_color": "black", "link": {"title": "", "action": {}}, "items": []}]}
        self.assertEqual(len(check_view(bad)), 5, check_view(bad))


def stats(view):
    grid = [it for it in items(view) if it["type"] == "stat_grid"][0]
    return {s["label"]: s["value"] for s in grid["stats"]}


class ChatsPageTest(unittest.TestCase):
    def test_what_needs_me_and_a_chat_opened_from_it(self):
        h = signed_in(self)
        # the seed's lines are minutes old: right after midnight they are yesterday's, so the chatter group gets one now
        h.world.push("chat_example_chatter", "user_example_c", "早")
        wait_until(lambda: "早" in str(h.app.store.messages_before("chat_example_chatter", None, 3)), what="stored")
        view = ui(h, "render", contribution="chats", state="", ui_version=6, placement="nav_page", size="full")
        self.assertEqual(view["title"], "飞书今天")
        self.assertEqual(view["refresh_after"], 60)
        s = stats(view)
        self.assertEqual((s["需要我"], s["@我"], s["私聊未回"]), ("2", "1", "1"))
        need = [s for s in view["sections"] if s.get("title") == "需要我"][0]["items"]
        self.assertEqual([r["title"] for r in need], ["示例同事甲", "示例项目群"], "newest first")
        self.assertEqual(need[1]["badge"], {"text": "@我", "tone": "caution"})
        self.assertIn("示例同事乙：@示例用户 接口文档这周谁来更新？", need[1]["subtitle"])
        groups = [s for s in view["sections"] if s.get("title") == "今天活跃的群"][0]["items"]
        self.assertIn("示例闲聊群", [r["title"] for r in groups])
        # a row opens its chat inside the page, and back comes back
        opened = ui(h, "action", contribution="chats", action={"id": need[1]["action"]["id"]}, state="", ui_version=6)
        chat = opened["view"]
        self.assertEqual((chat["title"], chat["state"]), ("示例项目群", '{"chat":"chat_example_team"}'))
        self.assertIn("下午三点开周会", [r.get("subtitle") for r in items(chat)])
        self.assertNotIn("示例同事丙 加入了群聊", [r.get("subtitle") for r in items(chat)], "no system lines")
        again = ui(h, "render", contribution="chats", state=chat["state"], ui_version=6)
        self.assertEqual(again["title"], "示例项目群", "a refresh keeps the open chat")
        back = ui(h, "action", contribution="chats", action={"id": "back"}, state=chat["state"], ui_version=6)
        self.assertEqual(back["view"]["title"], "飞书今天")
        # answering ends it
        h.world.push("chat_example_dm_a", "me", "收到，马上看")
        wait_until(lambda: stats(ui(h, "render", contribution="chats", state="", ui_version=6))["私聊未回"] == "0",
                   what="answered")

    def test_the_card_is_compact(self):
        h = signed_in(self)
        from openmate_core.backend import Chat

        for i in range(6):
            cid = "chat_example_dm_x%d" % i
            h.world.add_chat(Chat(cid, "dm", "示例同事%d" % i, peer_id="user_example_c"))
            h.world.push(cid, "user_example_c", "第 %d 件事" % i)
        wait_until(lambda: h.app.store.get_chat("chat_example_dm_x5"), what="the chats")
        card = ui(h, "render", contribution="chats-today", state="", ui_version=6, placement="overview_card", size="compact")
        self.assertLessEqual(len(card["sections"]), 2)
        self.assertLessEqual(len(items(card)), 12)
        self.assertEqual(len(stats(card)), 3)
        rows = [it for it in items(card) if it["type"] == "row"]
        self.assertEqual(len(rows), 3)
        self.assertFalse(any("action" in r for r in rows), "the card opens the page")
        self.assertNotIn("title", card)

    def test_old_apps_get_text_instead_of_new_items(self):
        h = signed_in(self)
        view = ui(h, "render", contribution="chats", state="", ui_version=1)
        self.assertFalse([it for it in items(view) if it["type"] in ("stat_grid", "empty")])
        self.assertIn("需要我 2", items(view)[0]["text"])

    def test_not_signed_in_and_unknown_contribution(self):
        h = Harness(self)
        view = ui(h, "render", contribution="chats", state="", ui_version=6)
        self.assertIn("还没有登录飞书", items(view)[0]["title"])
        with self.assertRaises(HubSideError) as e:
            ui(h, "render", contribution="nope", state="", ui_version=6)
        self.assertEqual(e.exception.code, "no_contribution")

    def test_the_badge_counts_what_needs_me(self):
        h = signed_in(self)

        def badges():
            return [e[2] for e in h.hub.events if e[1] == "ui.badge"]

        wait_until(lambda: badges() and badges()[-1].get("text") == "2", what="the badge")
        self.assertEqual(badges()[-1], {"contribution": "chats", "text": "2", "tone": "caution"})
        n = len(badges())
        h.world.push("chat_example_dm_a", "me", "好的")
        wait_until(lambda: badges()[-1].get("text") == "1", what="one fewer")
        h.world.push("chat_example_team", "me", "我来更新")
        wait_until(lambda: badges()[-1] == {"contribution": "chats", "text": ""}, what="cleared")
        time.sleep(0.3)
        texts = [b.get("text") for b in badges()[n - 1:]]
        self.assertEqual(texts, ["2", "1", ""], "sent only when it changes")


if __name__ == "__main__":
    unittest.main()
