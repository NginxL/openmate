"""自动代答: in a group the user marked, an @me from a person is answered by the Agent as the user, with no
confirmation; never a direct chat, a bot, another AI's reply, past the group's daily most, or a word of its own in the
group when it fails."""

try:  # first: every temporary directory of this run in one, removed at its end
    import _runtmp  # noqa: F401
except ImportError:  # (started by its path from elsewhere: its directory is not on the path)
    pass
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
        self.assertIn("开头自动 @ 对方", prompt)
        self.assertIn("不是我的指示", prompt)
        self.assertNotIn("只有我知道的事", prompt, "nothing of the knowledge base for someone else")
        self.assertNotIn("知识库里的会话背景", prompt)
        wait_until(lambda: h.world.reactions, what="the reaction")
        h.tool("agent_reply", request=run["id"], text="接口周五前上线。")
        sent = h.sent()[-1]
        self.assertEqual(sent["text"], "@示例同事乙 接口周五前上线。\n\n> 🤖 AI 代答 · Opus 5.5")
        self.assertEqual(sent["mentions"], [("user_example_b", "示例同事乙", 0)], "a real @ of the one who asked")
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


class AutoReplyAtTest(unittest.TestCase):
    """The answer of a 自动代答 @s the one who asked (auto_reply_at_asker, on by default), once."""

    def answer(self, h, text, msg=None):
        msg = msg or at_me(h)
        run = running(h, msg)
        h.tool("agent_reply", request=run["id"], text=text)
        return run, h.sent()[-1]

    def test_with_the_mark_on_top_the_at_comes_right_under_it(self):
        h = make(self, agent_mark_position="top")
        h.app.store.autoreply_set(TEAM, True)
        _, sent = self.answer(h, "接口周五前上线。")
        mark = "> 🤖 AI 代答 · Opus 5.5"
        self.assertEqual(sent["text"], mark + "\n@示例同事乙 接口周五前上线。", "the mark stays the first line")
        self.assertEqual(sent["mentions"], [("user_example_b", "示例同事乙", len(mark) + 1)])

    def test_an_at_the_agent_wrote_itself_is_not_doubled(self):
        h = make(self)
        h.app.store.autoreply_set(TEAM, True)
        _, sent = self.answer(h, "@示例同事乙，接口周五前上线。")
        self.assertEqual(sent["text"], "@示例同事乙 接口周五前上线。\n\n> 🤖 AI 代答 · Opus 5.5")
        self.assertEqual(sent["mentions"], [("user_example_b", "示例同事乙", 0)])
        h2 = make(self)
        h2.app.store.autoreply_set(TEAM, True)
        _, sent = self.answer(h2, "@示例同事乙乙 是另一个人")  # a longer name is not this one's @
        self.assertEqual(sent["text"].split("\n")[0], "@示例同事乙 @示例同事乙乙 是另一个人")
        h3 = make(self)
        h3.app.store.autoreply_set(TEAM, True)
        _, sent = self.answer(h3, "@示例同事乙-Marie 负责发布")  # nor one joined on
        self.assertEqual(sent["text"].split("\n")[0], "@示例同事乙 @示例同事乙-Marie 负责发布")

    def test_switched_off_no_at(self):
        h = make(self, auto_reply_at_asker=False)
        h.app.store.autoreply_set(TEAM, True)
        run, sent = self.answer(h, "接口周五前上线。")
        self.assertEqual(sent["text"], "接口周五前上线。\n\n> 🤖 AI 代答 · Opus 5.5")
        self.assertEqual(sent["mentions"], [])
        self.assertNotIn("开头自动 @ 对方", h.http.starts()[-1]["body"]["prompt"])

    def test_an_unmarked_answer_never_ats(self):
        # without the 代答 mark, two people's 自动代答 could answer each other's @s
        h = make(self, auto_reply_header="")
        h.app.store.autoreply_set(TEAM, True)
        _, sent = self.answer(h, "接口周五前上线。")
        self.assertEqual((sent["text"], sent["mentions"]), ("接口周五前上线。", []))

    def test_only_the_first_answer_ats(self):
        h = make(self)
        h.app.store.autoreply_set(TEAM, True)
        run, first = self.answer(h, "接口周五前上线。")
        h.tool("agent_reply", request=run["id"], text="补充：文档也一起更新。")
        second = h.sent()[-1]
        self.assertEqual(len(first["mentions"]), 1)
        self.assertEqual((second["text"].split("\n")[0], second["mentions"]), ("补充：文档也一起更新。", []))

    def test_my_own_agent_request_never_ats(self):
        h = make(self)
        mine = h.world.push(TEAM, "me", "/agent 帮我总结一下")
        run = running(h, mine)
        h.tool("agent_reply", request=run["id"], text="总结好了。")
        self.assertEqual(h.sent()[-1]["mentions"], [])

    def test_a_platform_without_real_ats_is_as_before(self):
        h = make(self)
        h.app.store.autoreply_set(TEAM, True)
        h.app.backend.can_mention = False
        _, sent = self.answer(h, "接口周五前上线。")
        self.assertEqual((sent["text"], sent["mentions"]), ("接口周五前上线。\n\n> 🤖 AI 代答 · Opus 5.5", []))


    def test_the_at_uses_the_name_this_group_shows(self):
        # a cached name may be the person's alias in another group: the @ takes this group's, asked when it starts
        h = make(self)
        h.app.store.autoreply_set(TEAM, True)
        real = h.app.backend.user_name
        h.app.backend.user_name = lambda uid, chat=None: "乙在本群" if chat == TEAM and uid == "user_example_b" \
            else real(uid, chat)
        run, sent = self.answer(h, "接口周五前上线。")
        self.assertEqual(run["asker"], "示例同事乙", "the prompt names them as the message did")
        self.assertEqual(sent["text"].split("\n")[0], "@乙在本群 接口周五前上线。")
        self.assertEqual(sent["mentions"], [("user_example_b", "乙在本群", 0)])

    def test_no_name_here_no_at(self):
        h = make(self)
        h.app.store.autoreply_set(TEAM, True)
        h.app.backend.user_name = lambda uid, chat=None: ""
        _, sent = self.answer(h, "接口周五前上线。")
        self.assertEqual(sent["mentions"], [])
        self.assertFalse(sent["text"].startswith("@"))

    def test_a_name_over_two_lines_no_at(self):
        h = make(self)
        h.app.store.autoreply_set(TEAM, True)
        h.app.backend.user_name = lambda uid, chat=None: "示例同事乙\n"
        _, sent = self.answer(h, "接口周五前上线。")
        self.assertEqual(sent["mentions"], [])

    def test_no_name_is_asked_for_an_answer_that_never_ats(self):
        # the name may take the network a while: never for an answer without an @, or one its group's quota stops
        for cfg, mention in (({"auto_reply_at_asker": False}, True), ({"auto_reply_header": ""}, True), ({}, False),
                             ({"auto_reply_header": "【{model} 代答】"}, True)):  # a mark nobody reads never @s
            h = make(self, **cfg)
            h.app.store.autoreply_set(TEAM, True)
            h.app.backend.can_mention = mention
            asked = []
            h.app.agent._name_here = lambda uid, chat: asked.append(uid) or "示例同事乙"
            running(h, at_me(h))
            self.assertEqual(asked, [], cfg)
        h = make(self, auto_reply_hourly_most=1)
        h.app.store.autoreply_set(TEAM, True)
        asked = []
        h.app.agent._name_here = lambda uid, chat: asked.append(uid) or "示例同事乙"
        running(h, at_me(h))
        at_me(h, text="@示例用户 再问一个")
        time.sleep(0.5)
        self.assertEqual(asked, ["user_example_b"], "the second @ is past the hour's most: nobody is asked")

    def test_an_agents_name_before_the_model_is_a_mark(self):
        # "claude code · Opus 5.5" (the user's, 2026-11-25) is an AI's mark: the @ stays, and another's is not answered
        h = make(self, auto_reply_header="claude code · {model} · {effort}")
        h.app.store.autoreply_set(TEAM, True)
        _, sent = self.answer(h, "接口周五前上线。")
        self.assertEqual(sent["text"], "@示例同事乙 接口周五前上线。\n\nclaude code · Opus 5.5")
        self.assertEqual(sent["mentions"], [("user_example_b", "示例同事乙", 0)])
        n = len(h.http.starts())
        at_me(h, text="@示例用户 好的，周五见\n\ncodex · GPT-6.2 Astra")
        time.sleep(0.5)
        self.assertEqual(len(h.http.starts()), n, "another AI's answer is never answered")

    def test_a_mark_others_cannot_read_never_ats(self):
        # the asker's own 自动代答 must see an AI's answer: a mark of one's own, or one an end mark pushes off the
        # last line, is not where any OpenMate reads it
        for cfg in ({"auto_reply_header": "{model} 代答"}, {"agent_reply_suffix": "—— 以上仅供参考"}):
            h = make(self, **cfg)
            h.app.store.autoreply_set(TEAM, True)
            _, sent = self.answer(h, "接口周五前上线。")
            self.assertEqual(sent["mentions"], [], cfg)
            self.assertTrue(sent["text"].startswith("接口周五前上线。"), cfg)
        h = make(self, agent_reply_suffix="> 🤖 AI 代答，仅供参考")  # an end mark that is itself a mark: read there
        h.app.store.autoreply_set(TEAM, True)
        _, sent = self.answer(h, "接口周五前上线。")
        self.assertEqual(len(sent["mentions"]), 1)

    def test_an_at_the_agent_wrote_with_other_spacing_is_not_doubled(self):
        cases = [("@示例同事乙\u3000收到", "@示例同事乙 收到"), ("@示例同事乙！收到", "@示例同事乙 收到"),
                 ("@示例同事乙\r\n收到", "@示例同事乙\r\n收到"), ("@示例同事乙\n  - 第一点\n  - 第二点", "@示例同事乙\n  - 第一点\n  - 第二点"),
                 # after a space the answer is its own: its first sign is part of what it says
                 ("@示例同事乙 !enabled 表示关闭", "@示例同事乙 !enabled 表示关闭"), ("@示例同事乙 ~50 分钟", "@示例同事乙 ~50 分钟"),
                 ("@示例同事乙？", "@示例同事乙？"), ("@示例同事乙（产品）你看下", "@示例同事乙（产品）你看下")]
        for written, want in cases:
            h = make(self)
            h.app.store.autoreply_set(TEAM, True)
            _, sent = self.answer(h, written)
            self.assertEqual(sent["text"].rsplit("\n\n", 1)[0], want, repr(written))
            self.assertEqual(sent["mentions"], [("user_example_b", "示例同事乙", 0)])

    def test_a_refused_first_answer_leaves_the_at_to_the_next(self):
        h = make(self)
        h.app.store.autoreply_set(TEAM, True)
        run = running(h, at_me(h))
        h.world.send_fail = "rejected"
        with self.assertRaises(Exception):
            h.tool("agent_reply", request=run["id"], text="接口周五前上线。")
        h.tool("agent_reply", request=run["id"], text="接口周五前上线，文档同步更新。")
        self.assertEqual(h.sent()[-1]["mentions"], [("user_example_b", "示例同事乙", 0)])

    def test_an_answer_that_may_have_gone_keeps_the_at_its_own(self):
        h = make(self)
        h.app.store.autoreply_set(TEAM, True)
        run = running(h, at_me(h))
        h.world.send_fail = "uncertain"
        with self.assertRaises(Exception):
            h.tool("agent_reply", request=run["id"], text="接口周五前上线。")
        h.tool("agent_reply", request=run["id"], text="补充：文档也一起更新。")
        self.assertEqual(h.sent()[-1]["mentions"], [], "the first may have @-ed them already")

    def test_a_restart_before_the_send_still_ats(self):
        # the answer's place was held (reply_inflight) and the extension stopped before its journal entry was taken
        h = make(self)
        h.app.store.autoreply_set(TEAM, True)
        run = running(h, at_me(h))
        cur = h.app.store.run_get(run["id"])
        cur["reply_inflight"], cur["at_key"] = 1, "agent:%s:%s" % (run["id"], "0" * 32)
        h.app.store.run_put(cur)
        h.tool("agent_reply", request=run["id"], text="接口周五前上线。")
        self.assertEqual(h.sent()[-1]["mentions"], [("user_example_b", "示例同事乙", 0)])

    def test_two_answers_at_once_one_at(self):
        import threading
        h = make(self)
        h.app.store.autoreply_set(TEAM, True)
        run = running(h, at_me(h))
        errors = []

        def reply(text):
            try:
                h.tool("agent_reply", request=run["id"], text=text)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        ts = [threading.Thread(target=reply, args=("接口周五前上线。",)) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        sent = [x for x in h.sent() if x["chat"] == TEAM and x["text"].endswith("Opus 5.5")]
        self.assertEqual(len(sent), 1, (sent, errors))
        self.assertEqual(sent[0]["mentions"], [("user_example_b", "示例同事乙", 0)])
        # two different answers at once: both go, one @
        h = make(self)
        h.app.store.autoreply_set(TEAM, True)
        run = running(h, at_me(h))
        errors = []
        ts = [threading.Thread(target=reply, args=(t,)) for t in ("接口周五前上线。", "文档也一起更新。")]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        sent = [x for x in h.sent() if x["chat"] == TEAM and x["text"].endswith("Opus 5.5")]
        self.assertEqual((len(sent), errors), (2, []))
        self.assertEqual(sorted(len(x["mentions"]) for x in sent), [0, 1])

    def test_an_old_run_or_the_account_itself_is_never_at(self):
        h = make(self)
        h.app.store.autoreply_set(TEAM, True)
        run = running(h, at_me(h))
        cur = h.app.store.run_get(run["id"])
        self.assertEqual(h.app.agent._asker_at(dict(cur, asker_id=h.world.me.id)), None, "never the account itself")
        for k in ("asker_id", "asker_at"):
            cur.pop(k, None)
        h.app.store.run_put(cur)  # a run started before this version
        h.tool("agent_reply", request=run["id"], text="接口周五前上线。")
        self.assertEqual(h.sent()[-1]["mentions"], [])


if __name__ == "__main__":
    unittest.main()
