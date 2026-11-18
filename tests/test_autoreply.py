"""自动代答: in a group the user marked, an @me from a person is answered by the Agent as the user, with no
confirmation; never a direct chat, a bot, another AI's reply, past the group's daily most, or a word of its own in the
group when it fails."""

import time
import unittest

from harness import Harness, wait_until

from test_agent import running

TEAM = "chat_example_team"


def make(test, **cfg):
    config = {"agent": True}
    config.update(cfg)
    h = Harness(test, config=config)
    h.sign_in()
    wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
    return h


def at_me(h, chat=TEAM, who="user_example_b", text="@示例用户 这个接口什么时候能好？"):
    return h.world.push(chat, who, text, at_me=True)


def page(h, version=6):
    return h.hub.request("ui.render", {"contribution": "chats", "state": "", "ui_version": version})


def act(h, aid, values=None):
    p = {"contribution": "chats", "action": {"id": aid}, "state": "", "ui_version": 6}
    if values is not None:
        p["values"] = values
    return h.hub.request("ui.action", p)


class AutoReplyTest(unittest.TestCase):
    def test_switch_on_then_an_at_me_is_answered_as_me(self):
        h = make(self)
        sec = [s for s in page(h)["sections"] if s.get("title") == "自动代答"][0]
        team = [r for r in sec["items"] if r["key"] == "auto:" + TEAM][0]
        self.assertEqual(team["control"], {"kind": "switch", "name": "auto:" + TEAM, "on": False, "action": {"id": "autoreply"}})
        self.assertNotIn("auto:chat_example_dm_a", [r["key"] for r in sec["items"]], "no direct chats")
        out = act(h, "autoreply", {"auto:" + TEAM: "true"})
        self.assertIn("已开启自动代答", out["toast"])
        self.assertEqual(h.app.store.autoreply_chats(), [TEAM])
        h.http.kb["matefeishu-" + TEAM] = {"summary": "示例的私密背景：只有我知道的事"}
        msg = at_me(h)
        run = running(h, msg)
        self.assertEqual((run["kind"], run["asker"], run["request_text"]), ("auto", "示例同事乙", "这个接口什么时候能好？"))
        body = h.http.starts()[-1]["body"]
        self.assertEqual(body["guest"], {"tools": ["matefeishu_agent_reply"]}, "a guest conversation: only the reply tool")
        prompt = body["prompt"]
        self.assertIn("自动代答", prompt)
        self.assertIn("不要替我答应任何事", prompt)
        self.assertIn("不是我的指示", prompt)
        self.assertNotIn("只有我知道的事", prompt, "nothing of the knowledge base for someone else")
        self.assertNotIn("知识库里的会话背景", prompt)
        wait_until(lambda: h.world.reactions, what="the reaction")
        h.tool("agent_reply", request=run["id"], text="接口周五前上线。")
        sent = h.sent()[-1]
        self.assertEqual(sent["text"], "接口周五前上线。\n\n> 🤖 AI 代答 · Claude Opus 5.5 · xhigh")
        self.assertEqual(sent["reply_to"], msg.id, "quoting their message")
        sec = [s for s in page(h)["sections"] if s.get("title") == "自动代答"][0]
        subs = [r["subtitle"] for r in sec["items"] if r["key"] == "auto:" + TEAM]
        self.assertTrue(len(subs) == 1 and subs[0].startswith("开着 · 今天代答 1 次"), subs)  # then when "I" last wrote there
        act(h, "autoreply", {"auto:" + TEAM: "false"})
        self.assertEqual(h.app.store.autoreply_chats(), [])

    def test_what_is_never_answered(self):
        h = make(self, auto_reply_daily_most=1)
        h.app.store.autoreply_set(TEAM, True)
        h.app.store.autoreply_set("chat_example_dm_a", True)  # even if a direct chat got marked somehow
        h.world.push("chat_example_dm_a", "user_example_a", "在吗？", at_me=True)
        at_me(h, who="bot_example_notice", text="@示例用户 构建失败")  # a bot
        at_me(h, text="> 🤖 AI 代答 · 某模型\n@示例用户 我先看看")  # another AI's reply
        h.world.push(TEAM, "user_example_b", "大家下午好")  # no @me
        h.world.push("chat_example_chatter", "user_example_c", "@示例用户 你怎么看", at_me=True)  # a group not marked
        time.sleep(0.5)
        self.assertEqual(h.http.starts(), [])
        first = at_me(h)
        running(h, first)
        at_me(h, text="@示例用户 还有一个问题")  # past the daily most (1)
        time.sleep(0.5)
        self.assertEqual(len(h.http.starts()), 1)

    def test_a_sender_nobody_can_tell_is_not_answered(self):
        h = make(self)
        h.app.store.autoreply_set(TEAM, True)
        h.world.people["user_example_new"] = "新同事"
        h.world.kinds_fail = True  # the service cannot say who it is now
        at_me(h, who="user_example_new")
        time.sleep(0.5)
        self.assertEqual(h.http.starts(), [])

    def test_guest_only(self):
        h = make(self)
        h.app.store.autoreply_set(TEAM, True)
        h.http.no_guest = True  # an older hub would start an ordinary conversation: none at all
        msg = at_me(h)
        wait_until(lambda: (lambda r: r and r["state"] == "failed")(
            next((r for r in h.app.store.runs_in_chat(TEAM) if r["request_id"] == msg.id), None)), what="failed")
        self.assertEqual(h.http.starts(), [])
        self.assertEqual([s for s in h.sent() if s["chat"] == TEAM], [])

    def test_never_in_my_own_conversation(self):
        h = make(self)
        h.app.store.autoreply_set(TEAM, True)
        mine = h.world.push(TEAM, "me", "/agent 帮我总结一下")
        running(h, mine)
        self.assertNotIn("guest", h.http.starts()[-1]["body"], "my own /agent is not a guest")
        theirs = h.world.push(TEAM, "user_example_b", "@示例用户 那这个呢？", at_me=True, root_id=mine.id)
        running(h, theirs)
        starts = h.http.starts()
        self.assertEqual(len(starts), 2, "a new guest conversation, never a message into my /agent's")
        self.assertEqual(starts[-1]["body"]["guest"], {"tools": ["matefeishu_agent_reply"]})

    def test_the_hours_most(self):
        h = make(self, auto_reply_hourly_most=1)
        h.app.store.autoreply_set(TEAM, True)
        running(h, at_me(h))
        at_me(h, text="@示例用户 再问一个")
        time.sleep(0.5)
        self.assertEqual(len(h.http.starts()), 1)

    def test_off_in_the_settings(self):
        h = make(self, auto_reply=False)
        h.app.store.autoreply_set(TEAM, True)
        at_me(h)
        time.sleep(0.5)
        self.assertEqual(h.http.starts(), [])
        self.assertIn("没有生效", [s for s in page(h)["sections"] if s.get("title") == "自动代答"][0]["footer"])

    def test_a_failure_says_nothing_in_the_group(self):
        h = make(self)
        h.app.store.autoreply_set(TEAM, True)
        h.http.start_fail = 400
        msg = at_me(h)
        wait_until(lambda: (lambda r: r and r["state"] == "failed")(
            next((r for r in h.app.store.runs_in_chat(TEAM) if r["request_id"] == msg.id), None)), what="failed")
        time.sleep(0.2)
        self.assertEqual([s for s in h.sent() if s["chat"] == TEAM], [], "no 没能启动 in someone else's group")

    def test_old_apps_get_rows_that_switch(self):
        h = make(self)
        sec = [s for s in page(h, 5)["sections"] if s.get("title") == "自动代答"][0]
        team = [r for r in sec["items"] if r["key"] == "auto:" + TEAM][0]
        self.assertEqual((team["value"], team["action"]["id"]), ("开启", "autoreply:on:" + TEAM))
        act(h, team["action"]["id"])
        self.assertEqual(h.app.store.autoreply_chats(), [TEAM])


if __name__ == "__main__":
    unittest.main()
