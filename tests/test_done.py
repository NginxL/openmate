"""处理完: the user marks something that needs them as handled; it is left out (the pages, the badge) while it is the
same, and comes back with a newer message (a direct chat) or a newer @me (a group)."""

try:  # first: every temporary directory of this run in one, removed at its end
    import _runtmp  # noqa: F401
except ImportError:  # (started by its path from elsewhere: its directory is not on the path)
    pass
import time
import unittest

from harness import Harness, fast, wait_until


def signed_in(test, config=None):
    h = Harness(test, config=config)
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
        aid = rows[0]["actions"][0]["action"]["id"]
        self.assertTrue(aid.startswith("done2:chat%3Achat_example_dm_a:"), aid)  # with the version the page showed
        self.assertEqual({k: v for k, v in rows[0]["actions"][0].items() if k != "action"},
                         {"title": "处理完", "icon": "check", "swipe": True})
        out = self.act(h, aid)
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

    def test_a_page_drawn_before_a_new_message_does_not_hide_it(self):
        h = signed_in(self)
        rows = [s for s in self.page(h)["sections"] if s.get("title") == "需要我"][0]["items"]
        aid = rows[0]["actions"][0]["action"]["id"]
        h.world.push("chat_example_dm_a", "user_example_a", "又一个问题")  # arrives after the page was drawn
        wait_until(lambda: "又一个问题" in str(h.app.store.messages_before("chat_example_dm_a", None, 3)), what="stored")
        out = self.act(h, aid)
        self.assertIn("新消息", out["toast"])
        self.assertIn("示例同事甲", self.needs(out["view"]), "what the page did not show stays")
        out = self.act(h, "done:chat:chat_example_dm_a")  # a page from before versions: the latest, as before
        self.assertNotIn("示例同事甲", self.needs(out["view"]))

    def test_needs_reach_as_far_back_as_set(self):
        h = signed_in(self, config={"needs_hours": 168})
        h.world.add_chat(type(h.world.chats["chat_example_dm_a"])("chat_example_dm_old", "dm", "示例旧私聊",
                                                                 peer_id="user_example_b"))
        m = h.world.make("chat_example_dm_old", "user_example_b", "四天前问你的事", ago=4 * 86400)
        h.app.ingest([m], live=False)
        self.assertIn("示例旧私聊", self.needs(self.page(h)), "4 days old, within the 7 days set")

    def test_an_at_me_pushed_out_by_chatter_still_needs_me(self):
        h = signed_in(self)
        at = h.world.make("chat_example_chatter", "user_example_a", "@示例用户 这个你看下", at_me=True)
        rest = [h.world.make("chat_example_chatter", "user_example_b", "闲聊第%d条" % i) for i in range(80)]
        h.app.ingest([at] + rest, live=False)
        self.assertIn("示例闲聊群", self.needs(self.page(h)), "80 lines later, the @me is still unanswered")

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



class ServiceDoneTest(unittest.TestCase):
    """The service's own done mark (Feishu's 完成) counts as 处理完 here, until a newer message."""

    page = DoneTest.page
    act = DoneTest.act
    needs = DoneTest.needs

    def done_rows(self, view):
        sec = [s for s in view["sections"] if s.get("title") == "已标记处理完"]
        return [r["title"] for r in sec[0]["items"]] if sec else []

    def ready(self, timing=None):
        """Signed in, the catch-up done and the first question about the needs answered (it hides nothing yet)."""
        h = Harness(self, timing=timing) if timing is not None else Harness(self)
        h.sign_in()
        wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
        wait_until(lambda: h.app.done_rounds >= 1, what="the first question about the needs")
        return h

    def rounds_after(self, h, n, what):
        wait_until(lambda: h.app.done_rounds > n, what=what)

    def test_a_done_mark_heard_live(self):
        h = self.ready()
        self.assertIn("示例同事甲", self.needs(self.page(h)))
        asks = len(h.world.done_asks)
        h.world.mark_done("chat_example_dm_a")
        wait_until(lambda: "示例同事甲" not in self.needs(self.page(h)), what="完成 in Feishu: 处理完 here")
        self.assertEqual(h.world.done_asks[asks:], [["chat_example_dm_a"]], "the push: one question about that chat")
        self.assertEqual(self.done_rows(self.page(h)), ["示例同事甲"])
        h.world.push("chat_example_dm_a", "user_example_a", "还有一件事")
        wait_until(lambda: "示例同事甲" in self.needs(self.page(h)), what="a new message brings it back")

    def test_a_group_marked_done(self):
        h = self.ready()
        h.world.mark_done("chat_example_team")
        wait_until(lambda: "示例项目群" not in self.needs(self.page(h)), what="a group's @me, 完成 in Feishu")

    def test_a_done_mark_made_while_away_is_found_after_the_catch_up(self):
        h = self.ready()
        h.world.drop()
        wait_until(lambda: not h.world.connected, what="the link is down")
        h.world.mark_done("chat_example_dm_a", push=False)
        asks = len(h.world.done_asks)
        wait_until(lambda: len(h.world.done_asks) > asks, what="asked after the catch-up")
        self.assertIn("chat_example_dm_a", h.world.done_asks[-1])
        wait_until(lambda: "示例同事甲" not in self.needs(self.page(h)), what="found 完成: 处理完")

    def test_asked_every_done_check(self):
        h = self.ready(fast(done_check=0.3))
        n, conns = h.app.done_rounds, h.world.connections
        h.world.mark_done("chat_example_dm_a", push=False)
        wait_until(lambda: "示例同事甲" not in self.needs(self.page(h)), what="found 完成 without a push")
        self.assertGreater(h.app.done_rounds, n)
        self.assertEqual(h.world.connections, conns, "no reconnect")

    def test_not_asked_while_the_link_is_down(self):
        h = self.ready(fast(done_check=0.2))
        h.world.hold_up = True
        h.world.drop()
        wait_until(lambda: h.app.link != "up", what="down")
        asks = len(h.world.done_asks)
        time.sleep(0.8)
        self.assertEqual(len(h.world.done_asks), asks, "nothing asked while down")

    def test_a_mark_older_than_the_line_that_made_the_need_is_left_alone(self):
        h = self.ready()
        h.world.mark_done("chat_example_dm_a", when=time.time() - 3600)
        h.world.mark_done("chat_example_team")
        wait_until(lambda: "示例项目群" not in self.needs(self.page(h)), what="the other mark was applied")
        self.assertIn("示例同事甲", self.needs(self.page(h)), "A wrote after that 完成")

    def test_a_later_system_line_does_not_make_a_mark_look_old(self):
        h = self.ready()
        h.world.push("chat_example_dm_a", "user_example_a", "这个问题", ago=600)
        h.world.push("chat_example_dm_a", "user_example_a", "示例同事甲 修改了群名", type="SYSTEM")
        wait_until(lambda: "修改了群名" in str(h.app.store.messages_before("chat_example_dm_a", None, 3)), what="stored")
        h.world.mark_done("chat_example_dm_a", when=time.time() - 300)  # after the question, well before the system line
        wait_until(lambda: "示例同事甲" not in self.needs(self.page(h)), what="the question's 完成 counts")

    def test_a_message_while_the_answer_is_on_its_way_stays(self):
        h = self.ready()
        once = []

        def meanwhile():
            if not once:
                once.append(1)
                h.world.push("chat_example_dm_a", "user_example_a", "又想到一件事")

        h.world.done_hook = meanwhile
        n = h.app.done_rounds
        h.world.mark_done("chat_example_dm_a")
        self.rounds_after(h, n, "the round with the answer")
        self.assertIn("示例同事甲", self.needs(self.page(h)), "the 完成 covered the message before, not this one")

    def test_the_users_restore_while_the_answer_is_on_its_way_holds(self):
        h = self.ready()
        once = []

        def meanwhile():
            if not once:
                once.append(1)
                self.act(h, "done:chat:chat_example_dm_a")
                self.act(h, "undone:chat:chat_example_dm_a")

        h.world.done_hook = meanwhile
        n = h.app.done_rounds
        h.world.mark_done("chat_example_dm_a")
        self.rounds_after(h, n, "the round with the answer")
        h.world.done_hook = None
        self.assertIn("示例同事甲", self.needs(self.page(h)), "恢复 pressed meanwhile is kept")

    def test_a_restore_stays_when_the_reactions_could_not_be_read(self):
        h = self.ready()
        h.world.mark_done("chat_example_dm_a")
        wait_until(lambda: "示例同事甲" not in self.needs(self.page(h)), what="完成: 处理完")
        n = h.app.done_rounds
        self.act(h, "undone:chat:chat_example_dm_a")
        self.rounds_after(h, n, "恢复 asks about the chat")
        key = "chat:chat_example_dm_a"
        h.world.reactions_unasked = True  # the reactions' batches fail from now on
        h.world.mark_inbox("chat_example_dm_a")
        n = h.app.done_rounds
        h.world.drop()
        self.rounds_after(h, n, "a round that saw it not done, its reactions unknown")
        self.assertIn(key, h.app.store.dismissed([key]), "not known not to be answered: the 恢复 mark stays")

    def test_brought_back_stays_back_while_feishu_still_says_done(self):
        h = self.ready()
        h.world.mark_done("chat_example_dm_a")
        wait_until(lambda: "示例同事甲" not in self.needs(self.page(h)), what="完成: 处理完")
        n = h.app.done_rounds
        out = self.act(h, "undone:chat:chat_example_dm_a")
        self.assertIn("示例同事甲", self.needs(out["view"]))
        self.rounds_after(h, n, "恢复 asks about the chat")
        asks = len(h.world.done_asks)
        h.world.drop()  # a reconnect asks again: Feishu still says 完成
        wait_until(lambda: len(h.world.done_asks) > asks, what="asked again")
        n = h.app.done_rounds
        h.world.mark_done("chat_example_dm_a")  # and the same mark heard live once more
        self.rounds_after(h, n, "the push's round")
        self.assertIn("示例同事甲", self.needs(self.page(h)), "恢复 holds against that 完成")
        h.world.mark_inbox("chat_example_dm_a")  # moved back in Feishu: the 恢复 has done its job…
        h.world.drop()
        key = "chat:chat_example_dm_a"
        wait_until(lambda: key not in h.app.store.dismissed([key]), what="a round that saw it not done dropped the 恢复 mark")
        self.assertIn("示例同事甲", self.needs(self.page(h)), "still there until a new 完成")
        h.world.mark_done("chat_example_dm_a")  # …so a new 完成 there counts
        wait_until(lambda: "示例同事甲" not in self.needs(self.page(h)), what="a new 完成 after the 恢复")

    def test_a_first_feishu_done_after_a_restore_here_counts(self):
        h = self.ready()
        self.act(h, "done:chat:chat_example_dm_a")
        n = h.app.done_rounds
        self.act(h, "undone:chat:chat_example_dm_a")  # Feishu never said 完成
        self.rounds_after(h, n, "恢复 asks about the chat")
        h.world.mark_done("chat_example_dm_a")
        wait_until(lambda: "示例同事甲" not in self.needs(self.page(h)), what="the first 完成 in Feishu counts")

    def test_a_restore_from_a_page_drawn_before_a_new_message_covers_nothing(self):
        h = self.ready()
        self.act(h, "done:chat:chat_example_dm_a")
        h.world.push("chat_example_dm_a", "user_example_a", "新的问题")
        wait_until(lambda: "示例同事甲" in self.needs(self.page(h)), what="back with the new message")
        self.act(h, "undone:chat:chat_example_dm_a")  # the 已标记处理完 row of a page drawn before
        h.world.mark_done("chat_example_dm_a")
        wait_until(lambda: "示例同事甲" not in self.needs(self.page(h)), what="完成 of the new message counts")

    def test_another_account_forgets_the_marks(self):
        h = self.ready()
        st = h.app.store
        st.dismiss("chat:chat_example_dm_a", "v1")
        st.dismiss("inbox:item1", "v1")
        st.wipe_account_data()
        self.assertEqual(st.dismissed(["chat:chat_example_dm_a", "inbox:item1"]), {"inbox:item1": "v1"})

    def test_a_restore_as_another_account_forgets_the_one_before(self):
        h = self.ready()
        h.app.store.dismiss("chat:chat_example_team", "v1")
        from openmate_core.backend import Account

        h.world.me = Account("user_example_other", "另一个账号")  # a sign-in that stopped half-way left B's credentials
        h2 = h.restart()
        wait_until(lambda: h2.app.store is not None and h2.app.store.meta_get("account_id") == "user_example_other",
                   what="restored as the other account")
        self.assertEqual(h2.app.store.dismissed(["chat:chat_example_team"]), {}, "the account before's marks are gone")

    def version_of(self, h, chat_id):
        from openmate_core.ui import ChatsToday

        return next(t["_version"] for t in ChatsToday(h.app).needs() if t["_chat"]["id"] == chat_id)

    def test_a_reaction_of_mine_on_the_message_counts(self):
        h = self.ready()
        rows = [r for r in h.app.store.messages_before("chat_example_dm_a", None, 10) if not r.get("mine")]
        v = self.version_of(h, "chat_example_dm_a")
        earlier = [r["id"] for r in rows if r["id"] != v][0]
        n = h.app.done_rounds
        h.world.react_as_me(earlier)  # a reaction on an older line: the newest still asks
        self.rounds_after(h, n, "the push's round")
        self.assertIn("示例同事甲", self.needs(self.page(h)))
        self.assertIn(v, h.world.reactions_asks[-1], "asked about the message that made the need")
        h.world.react_as_me(v)  # DONE on the message itself
        wait_until(lambda: "示例同事甲" not in self.needs(self.page(h)), what="a reaction: answered without words")
        h.world.push("chat_example_dm_a", "user_example_a", "那就这样定了？")
        wait_until(lambda: "示例同事甲" in self.needs(self.page(h)), what="a new message brings it back")

    def test_the_agents_own_thinking_reaction_is_no_answer(self):
        h = self.ready()
        v = self.version_of(h, "chat_example_team")
        n = h.app.done_rounds
        h.world.react_as_me(v, kind="THINKING")  # 自动代答 took it, and may yet fail
        self.rounds_after(h, n, "the push's round")
        self.assertIn("示例项目群", self.needs(self.page(h)))
        h.world.react_as_me(v, kind="CrossMark")
        self.rounds_after(h, h.app.done_rounds, "another round")
        self.assertIn("示例项目群", self.needs(self.page(h)), "❌: it failed, nothing was answered")

    def test_a_restore_holds_while_the_reactions_cannot_be_read(self):
        h = self.ready()
        v = self.version_of(h, "chat_example_dm_a")
        h.world.react_as_me(v)
        wait_until(lambda: "示例同事甲" not in self.needs(self.page(h)), what="a reaction: 处理完")
        n = h.app.done_rounds
        self.act(h, "undone:chat:chat_example_dm_a")
        self.rounds_after(h, n, "恢复 asks about the chat")
        h.world.reactions_fail = True
        n = h.app.done_rounds
        h.app.ask_done(["chat_example_dm_a"])  # Feishu says not 完成; the reactions cannot be read this time
        self.rounds_after(h, n, "a round without the reactions")
        h.world.reactions_fail = False
        n = h.app.done_rounds
        h.app.ask_done(["chat_example_dm_a"])
        self.rounds_after(h, n, "a round with them again")
        self.assertIn("示例同事甲", self.needs(self.page(h)), "恢复 holds against the reaction it came after")

    def test_a_reaction_made_while_away_is_found_after_the_catch_up(self):
        h = self.ready()
        v = self.version_of(h, "chat_example_team")
        h.world.drop()
        wait_until(lambda: not h.world.connected, what="the link is down")
        h.world.react_as_me(v, push=False)
        wait_until(lambda: "示例项目群" not in self.needs(self.page(h)), what="found after the catch-up")

    def test_asking_fails_quietly(self):
        h = Harness(self, timing=fast(done_check=0.2))
        h.world.done_fail = True
        h.sign_in()
        wait_until(lambda: len(h.world.done_asks) >= 2, what="asked, and failed, twice")
        self.assertIn("示例同事甲", self.needs(self.page(h)))
        self.assertEqual(h.app.link, "up")


if __name__ == "__main__":
    unittest.main()
