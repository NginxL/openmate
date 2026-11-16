"""处理完: the user marks something that needs them as handled; it is left out (the pages, the badge) while it is the
same, and comes back with a newer message (a direct chat) or a newer @me (a group)."""

import time
import unittest

from harness import Harness, wait_until


def signed_in(test):
    h = Harness(test)
    h.sign_in()
    wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
    return h


class DoneTest(unittest.TestCase):
    def page(self, h, state=""):
        return h.hub.request("ui.render", {"contribution": "chats", "state": state, "ui_version": 6})

    def act(self, h, aid, state=""):
        return h.hub.request("ui.action", {"contribution": "chats", "action": {"id": aid}, "state": state, "ui_version": 6})

    def needs(self, view):
        sec = [s for s in view["sections"] if s.get("title") == "需要我"]
        return [r["title"] for r in sec[0]["items"] if r.get("type") == "row"] if sec else []

    def badges(self, h):
        return [e[2] for e in h.hub.events if e[1] == "ui.badge"]

    def test_marked_done_until_a_new_message(self):
        h = signed_in(self)
        view = self.page(h)
        rows = [s for s in view["sections"] if s.get("title") == "需要我"][0]["items"]
        self.assertEqual(rows[0]["actions"], [{"title": "处理完", "icon": "check", "swipe": True,
                                               "action": {"id": "done:chat:chat_example_dm_a"}}])
        out = self.act(h, "done:chat:chat_example_dm_a")
        self.assertIn("已标记处理完", out["toast"])
        self.assertEqual(self.needs(out["view"]), ["示例项目群"])
        done = [s for s in out["view"]["sections"] if s.get("title") == "已标记处理完"][0]
        self.assertEqual(done["items"][0]["title"], "示例同事甲")
        self.assertEqual(done["items"][0]["actions"][0]["action"], {"id": "undone:chat:chat_example_dm_a"})
        wait_until(lambda: self.badges(h) and self.badges(h)[-1].get("text") == "1", what="the badge counts one fewer")
        head = out["view"]["sections"][0]["items"][0]["stats"][0]
        self.assertEqual(head["value"], "1")
        # A writes again: it needs me again
        h.world.push("chat_example_dm_a", "user_example_a", "还在吗？")
        wait_until(lambda: "示例同事甲" in self.needs(self.page(h)), what="back with a new message")
        wait_until(lambda: self.badges(h)[-1].get("text") == "2", what="the badge again")

    def test_a_group_comes_back_only_with_a_new_at_me(self):
        h = signed_in(self)
        self.act(h, "done:chat:chat_example_team")
        self.assertNotIn("示例项目群", self.needs(self.page(h)))
        h.world.push("chat_example_team", "user_example_c", "我先去开会了")
        wait_until(lambda: "我先去开会了" in str(h.app.store.messages_before("chat_example_team", None, 3)), what="stored")
        self.assertNotIn("示例项目群", self.needs(self.page(h)), "any line of the group does not bring it back")
        h.world.push("chat_example_team", "user_example_c", "@示例用户 这个你来定吧", at_me=True)
        wait_until(lambda: "示例项目群" in self.needs(self.page(h)), what="a new @me brings it back")

    def test_restore_and_the_chat_pages_button(self):
        h = signed_in(self)
        state = '{"chat":"chat_example_dm_a"}'
        chat = self.page(h, state)
        buttons = [it for it in chat["sections"][0]["items"] if it["type"] == "button"]
        self.assertEqual([b["title"] for b in buttons], ["返回", "处理完"])
        out = self.act(h, buttons[1]["action"]["id"], state)
        self.assertEqual(out["view"]["title"], "示例同事甲", "stays on the chat")
        self.assertEqual([it["title"] for it in out["view"]["sections"][0]["items"]], ["返回"], "nothing to mark now")
        out = self.act(h, "undone:chat:chat_example_dm_a")
        self.assertEqual(out["toast"], "已恢复")
        self.assertIn("示例同事甲", self.needs(out["view"]))
        self.assertFalse([s for s in out["view"]["sections"] if s.get("title") == "已标记处理完"])
        self.assertIn("没有要处理", self.act(h, "done:chat:chat_example_dm_b")["toast"], "I spoke last there")
        time.sleep(0.1)

    def test_old_apps_get_no_row_menu(self):
        h = signed_in(self)
        view = h.hub.request("ui.render", {"contribution": "chats", "state": "", "ui_version": 5})
        self.assertFalse(any("actions" in it for s in view["sections"] for it in s["items"]))


if __name__ == "__main__":
    unittest.main()
