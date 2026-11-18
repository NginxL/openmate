"""The core as the WeChat extension runs it (the fixture backend under openmate-wechat's Platform): its own page and
connector ids, no document search, no /agent progress (WeChat cannot edit a message), and 自动代答 that answers once.
Also the core fixes made with it: a new account forgets the old one's marked groups, and a group's hourly most holds
when two @s come at once."""

import os
import sys
import threading
import time
import unittest

from harness import ROOT, Harness, wait_until  # noqa: F401
from test_agent import ask, run_of, running

sys.path.insert(0, os.path.join(ROOT, "extensions", "openmate-wechat"))
import openmate_wechat  # noqa: E402

CID = "matewechat"
TEAM = "chat_example_team"


def wechat(test, **cfg):
    config = {"agent": True, "agent_reply_header": " ", "agent_ack_reaction": "", "agent_done_reaction": "",
              "agent_fail_reaction": ""}
    config.update(cfg)
    h = Harness(test, config=config, platform=openmate_wechat.platform())
    h.sign_in()
    wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
    return h


def page(h, contribution="wechat-chats"):
    return h.hub.request("ui.render", {"contribution": contribution, "state": "", "ui_version": 6})


def act(h, aid, values):
    return h.hub.request("ui.action", {"contribution": "wechat-chats", "action": {"id": aid}, "state": "",
                                       "ui_version": 6, "values": values})


class WeChatPlatformTest(unittest.TestCase):
    def test_its_own_page_and_tools(self):
        h = wechat(self)
        self.assertIn("sections", page(h))
        with self.assertRaises(Exception):
            page(h, "chats")  # Feishu's id is not this extension's
        names = [t["name"] for t in h.c("tools.list")["tools"]]
        self.assertEqual(names, [CID + "_" + n for n in ("status", "chats", "messages", "search", "agent_reply")])
        reply = [t for t in h.c("tools.list")["tools"] if t["name"] == CID + "_agent_reply"][0]
        self.assertNotIn("progress", reply["input_schema"]["properties"], "WeChat cannot edit: no progress")
        self.assertIn("只回复一次最终结果", reply["description"])

    def test_agent_sends_no_progress_and_one_answer(self):
        h = wechat(self)
        msg = ask(h, "/agent 总结一下刚才的讨论")
        run = running(h, msg)
        prompt = h.http.starts()[-1]["body"]["prompt"]
        self.assertIn("只回复一次最终结果", prompt)
        self.assertNotIn("progress=true", prompt)
        before = len(h.sent())
        out = h.tool("agent_reply", request=run["id"], text="还在看", progress=True)
        self.assertIn("不能编辑消息", out)
        self.assertEqual(len(h.sent()), before, "no progress message in the chat")
        h.tool("agent_reply", request=run["id"], text="讨论的结论是周五上线。")
        sent = h.sent()[-1]
        self.assertEqual((sent["chat"], sent["text"].strip()), (TEAM, "讨论的结论是周五上线。"))
        self.assertEqual(sent["reply_to"], msg.id)

    def test_auto_reply_in_a_marked_group_answers_once(self):
        h = wechat(self)
        sec = [s for s in page(h)["sections"] if s.get("title") == "自动代答"][0]
        self.assertIn("auto:" + TEAM, [r["key"] for r in sec["items"]])
        out = act(h, "autoreply", {"auto:" + TEAM: "true"})
        self.assertIn("已开启自动代答", out["toast"])
        msg = h.world.push(TEAM, "user_example_b", "@示例用户 这个接口什么时候能好？", at_me=True)
        run = running(h, msg)
        self.assertEqual(run["kind"], "auto")
        self.assertEqual(h.http.starts()[-1]["body"]["guest"], {"tools": [CID + "_agent_reply"]})
        self.assertIn("不能编辑消息", h.tool("agent_reply", request=run["id"], text="进度", progress=True))
        first = h.tool("agent_reply", request=run["id"], text="周五前上线。")
        n = len(h.sent())
        self.assertEqual(h.tool("agent_reply", request=run["id"], text="周五前上线。"), first,
                         "the same reply asked again gets what it got")
        with self.assertRaises(Exception) as caught:
            h.tool("agent_reply", request=run["id"], text="再补充一句")
        self.assertIn("只回复一次", str(caught.exception))
        self.assertEqual(len(h.sent()), n)

    def test_at_all_alone_and_unmarked_groups_are_not_answered(self):
        h = wechat(self)
        h.world.push(TEAM, "user_example_b", "@所有人 下午开会", at_all=True)
        h.world.push(TEAM, "user_example_b", "@示例用户 在吗", at_me=True)  # the group is not marked
        time.sleep(0.5)
        self.assertEqual([r for r in h.app.store.runs_in_chat(TEAM, 50) if r.get("kind") == "auto"], [])


class TodayAPIGrantTest(unittest.TestCase):
    def test_feishus_today_may_read_its_chats(self):
        import json

        self.assertEqual(openmate_wechat.platform().today_api_callers, ("extension:openmate-feishu",))
        with open(os.path.join(ROOT, "extensions", "openmate-wechat", "persona-extension.json"), encoding="utf-8") as f:
            self.assertTrue(json.load(f)["runtime"]["http"], "its own HTTP: the socket the host gives it")


class OneReplyTest(unittest.TestCase):
    def test_a_quiet_fallback_does_not_take_the_auto_reply(self):
        h = wechat(self)
        h.app.store.autoreply_set(TEAM, True)
        msg = h.world.push(TEAM, "user_example_b", "@示例用户 什么时候好？", at_me=True)
        run = running(h, msg)
        cur = h.app.store.run_get(run["id"])
        cur["fallback"] = "failed"  # the turn ended first: a quiet run posts nothing
        h.app.store.run_put(cur)
        n = len(h.sent())
        h.tool("agent_reply", request=run["id"], text="周五。")
        self.assertEqual(len(h.sent()), n + 1, "the late first reply still goes out")

    def test_a_fallback_in_the_journal_takes_the_agent_reply(self):
        h = wechat(self)
        msg = ask(h, "/agent 总结")
        run = running(h, msg)
        self.assertTrue(h.app.store.send_claim("agent:%s:fallback" % run["id"], "agent"))
        h.app.store.send_finish("agent:%s:fallback" % run["id"], "uncertain", {})
        with self.assertRaises(Exception) as c:
            h.tool("agent_reply", request=run["id"], text="结论")
        self.assertIn("只回复一次", str(c.exception))

    def test_a_custom_first_line_mark_after_a_real_at_is_seen(self):
        h = wechat(self, auto_reply_header="> [智能代答] · {model}", agent_mark_position="top")
        h.app.store.autoreply_set(TEAM, True)
        h.world.push(TEAM, "user_example_b", "@示例用户\u2005> [智能代答] · 某模型\n已经好了", at_me=True)
        h.world.push(TEAM, "user_example_b", "@示例用户\u2005> 🤖 AI 代答 · 某模型\n好了", at_me=True)
        time.sleep(0.5)
        self.assertEqual([r for r in h.app.store.runs_in_chat(TEAM, 50) if r.get("kind") == "auto"], [])
        person = h.world.push(TEAM, "user_example_b", "@示例用户\u2005🤖 机器人项目下周上线吗？", at_me=True)
        self.assertEqual(running(h, person)["kind"], "auto", "a person's question about robots is answered")


class MarksAndMentionsTest(unittest.TestCase):
    def test_a_custom_header_is_matched_whole(self):
        h = wechat(self, auto_reply_header="【{model} 代答】", agent_mark_position="top")
        h.app.store.autoreply_set(TEAM, True)
        h.world.push(TEAM, "user_example_b", "@示例用户\u2005【某模型 代答】\n已经好了", at_me=True)
        time.sleep(0.4)
        self.assertEqual([r for r in h.app.store.runs_in_chat(TEAM, 50) if r.get("kind") == "auto"], [])
        person = h.world.push(TEAM, "user_example_b", "@示例用户\u2005【紧急】今天能发版吗？", at_me=True)
        self.assertEqual(running(h, person)["kind"], "auto", "a person's 【紧急】 is a question")

    def test_a_header_that_starts_with_a_placeholder_is_seen(self):
        h = wechat(self, auto_reply_header="> {model} · [智能代答]", agent_mark_position="top")
        h.app.store.autoreply_set(TEAM, True)
        h.world.push(TEAM, "user_example_b", "@示例用户\u2005> 某模型 · [智能代答]\n好了", at_me=True)
        time.sleep(0.4)
        self.assertEqual([r for r in h.app.store.runs_in_chat(TEAM, 50) if r.get("kind") == "auto"], [])

    def test_agent_after_a_mention_of_a_name_with_spaces(self):
        h = wechat(self)
        msg = ask(h, "@Bob Smith\u2005/agent 整理一下")
        self.assertEqual(running(h, msg)["request_text"], "整理一下")

    def test_a_late_reply_after_a_timeout_notice_is_refused(self):
        h = wechat(self)
        msg = ask(h, "/agent 慢活")
        run = running(h, msg)
        cur = h.app.store.run_get(run["id"])
        cur["state"] = "timeout"
        cur["timeout_notice"] = "claimed"  # the timeout thread is sending its notice
        h.app.store.run_put(cur)
        with self.assertRaises(Exception) as c:
            h.tool("agent_reply", request=run["id"], text="终于好了")
        self.assertIn("只回复一次", str(c.exception))
        cur = h.app.store.run_get(run["id"])
        cur["timeout_notice"] = "done"  # it surely failed: nothing in the journal
        h.app.store.run_put(cur)
        n = len(h.sent())
        h.tool("agent_reply", request=run["id"], text="终于好了")
        self.assertEqual(len(h.sent()), n + 1, "a notice that never went out does not take the reply")

    def test_a_typed_at_before_a_real_one_is_not_a_command(self):
        h = wechat(self)
        ask(h, "@运营 例子请看 @Bob Smith\u2005/agent 写周报")
        time.sleep(0.4)
        self.assertEqual([r for r in h.app.store.runs_in_chat(TEAM, 50) if r.get("kind") != "auto"], [])

    def test_header_variants_and_literal_braces(self):
        h = wechat(self, auto_reply_header="> [智能代答] · {model} · {effort}", agent_reply_header="{bot}回复 · {model}",
                   agent_mark_position="top")
        a = h.app.agent
        self.assertTrue(a.is_agent_text("> [智能代答] · GPT-6 · high\n好"))
        self.assertTrue(a.is_agent_text("> [智能代答] · GPT-6\n好"), "no effort: its part is gone")
        self.assertTrue(a.is_agent_text("> [智能代答]\n好"), "neither")
        self.assertTrue(a.is_agent_text("{bot}回复 · 某模型\n好"), "literal braces are themselves")
        self.assertFalse(a.is_agent_text("请给我一个回复"))
        self.assertFalse(a.is_agent_text("/agent 请总结上一条回复"))

    def test_punctuation_only_templates_and_builtin_words(self):
        h = wechat(self, agent_reply_header="> {model}：{effort}", agent_mark_position="end")
        msg = ask(h, "/agent 结论")
        run = running(h, msg)
        h.tool("agent_reply", request=run["id"], text="结论：暂缓发布。\n🤖 AI 代答功能仍不允许上线。")
        sent = h.sent()[-1]["text"]
        self.assertTrue(sent.startswith("结论：暂缓发布。\n🤖 AI 代答功能仍不允许上线。"), sent)

    def test_a_question_keeps_its_typed_ats(self):
        h = wechat(self)
        h.app.store.autoreply_set(TEAM, True)
        m = h.world.push(TEAM, "user_example_b", "@示例用户\u2005@openmate 和 @openmate_dev 哪个是官方的？", at_me=True)
        self.assertEqual(running(h, m)["request_text"], "@openmate 和 @openmate_dev 哪个是官方的？")

    def test_typed_ats_never_make_a_command(self):
        h = wechat(self)
        ask(h, "@运营 举例：@张三\u2005/agent 写周报")
        ask(h, "@运营 /agent 写周报")
        time.sleep(0.4)
        self.assertEqual([r for r in h.app.store.runs_in_chat(TEAM, 50) if r.get("kind") != "auto"], [])

    def test_a_question_about_the_ai_is_answered(self):
        h = wechat(self)
        h.app.store.autoreply_set(TEAM, True)
        m = h.world.push(TEAM, "user_example_b", "@示例用户\u2005🤖 AI 代答功能什么时候上线？", at_me=True)
        self.assertEqual(running(h, m)["kind"], "auto")
        h.world.push(TEAM, "user_example_b", "@示例用户\u2005🤖 AI · 已处理", at_me=True)
        time.sleep(0.4)
        self.assertEqual(len([r for r in h.app.store.runs_in_chat(TEAM, 50) if r.get("kind") == "auto"]), 1,
                         "another AI's mark line is not answered")

    def test_answer_lines_that_start_like_marks_are_kept(self):
        h = wechat(self)
        msg = ask(h, "/agent 能上线吗")
        run = running(h, msg)
        h.tool("agent_reply", request=run["id"], text="> 🤖 AI 代答，仅供参考\n🤖 AI 回复 API 仍不允许上线。")
        sent = h.sent()[-1]["text"]
        self.assertTrue(sent.startswith("🤖 AI 回复 API 仍不允许上线。"), sent)
        self.assertNotIn("仅供参考", sent, "the old end mark the Agent copied goes")

    def test_an_answers_own_bracket_lines_are_kept(self):
        h = wechat(self, agent_reply_header="【{model} 代答】", agent_mark_position="end")
        msg = ask(h, "/agent 能发布吗")
        run = running(h, msg)
        h.tool("agent_reply", request=run["id"], text="【前提】下面只指测试环境。\n可以发布。")
        sent = h.sent()[-1]["text"]
        self.assertTrue(sent.startswith("【前提】下面只指测试环境。\n可以发布。"), sent)


class RoundNineTest(unittest.TestCase):
    def test_questions_that_look_like_marks_are_requests(self):
        h = wechat(self)
        h.app.store.autoreply_set(TEAM, True)
        m = h.world.push(TEAM, "user_example_b", "@示例用户\u2005🤖 AI 回复 API 有延迟吗？", at_me=True)
        self.assertEqual(running(h, m)["kind"], "auto")
        a = ask(h, "/agent 帮我找一下这个机器人 🤖")
        self.assertEqual(running(h, a)["request_text"], "帮我找一下这个机器人 🤖")
        b = ask(h, "/agent 帮我润色这条文案\n🤖 AI 代答，让客户少等一分钟。")
        running(h, b)

    def test_a_dot_without_spaces_is_text_not_a_mark(self):
        h = wechat(self)
        h.app.store.autoreply_set(TEAM, True)
        m = h.world.push(TEAM, "user_example_b", "@示例用户\u2005🤖 AI 回复·为什么总是超时？", at_me=True)
        self.assertEqual(running(h, m)["kind"], "auto")

    def test_an_answer_line_like_a_mark_is_kept_and_a_mark_only_answer_is_refused(self):
        h = wechat(self)
        msg = ask(h, "/agent 状态")
        run = running(h, msg)
        with self.assertRaises(Exception) as c:
            h.tool("agent_reply", request=run["id"], text="> 🤖 AI 代答，仅供参考")
        self.assertIn("只有 AI 标记", str(c.exception))
        h.tool("agent_reply", request=run["id"], text="🤖 AI 回复 · API 仍不可用。")
        self.assertTrue(h.sent()[-1]["text"].startswith("🤖 AI 回复 · API 仍不可用。"), "the refused try took no reply")

    def test_the_ack_is_recorded_and_a_stop_quoting_it_stops_the_run(self):
        h = wechat(self, agent_ack_reaction="🤖 贾维斯处理中...")
        h.app.backend.react = lambda chat, mid, reaction: "ack_1"
        msg = ask(h, "/agent 长任务")
        run = running(h, msg)
        wait_until(lambda: h.app.store.run_get(run["id"]).get("ack_id") == "ack_1", what="ack id")
        wait_until(lambda: any(r.get("text") == "🤖 贾维斯处理中..." for r in h.hub.sent_records), what="recorded")
        ask(h, "/stop", parent_id="ack_1")
        wait_until(lambda: h.app.store.run_get(run["id"])["state"] == "stopped", what="stopped")


class AlignedWithFeishuTest(unittest.TestCase):
    def archived(self, h):
        chats, cursor = set(), ""
        for _ in range(20):
            res = h.c("archive.changes", cursor=cursor, limit=100)
            chats |= {b["chat"]["id"] for b in res["batches"]}
            cursor = res["next"]
            if not res["more"]:
                return chats
        self.fail("archive.changes never ended")

    def test_direct_chats_are_archived_groups_only_when_turned_on(self):
        from openmate_wechat import backend as wb

        wb.PERSONS.add("chat_example_dm_a")  # what a contact lookup would have learnt
        self.addCleanup(wb.PERSONS.discard, "chat_example_dm_a")
        h = wechat(self)
        got = self.archived(h)
        self.assertIn("chat_example_dm_a", got)
        self.assertNotIn(TEAM, got, "groups stay out by default")
        h2 = wechat(self, archive_groups=True)
        self.assertIn(TEAM, self.archived(h2))

    def test_sends_and_the_inbox_are_there(self):
        h = wechat(self)
        names = [t["name"] for t in h.c("actions.list")["tools"]]
        self.assertIn(CID + "_send", names)
        things = h.c("inbox.collect", since="", limit=50)["things"]
        self.assertEqual({t["kind"] for t in things} >= {"dm", "mention"}, True)
        self.assertTrue(all("微信" in t["context"] for t in things), "worded for WeChat")


class ReviewElevenTest(unittest.TestCase):
    def test_a_late_model_never_sends_a_mark_alone(self):
        h = wechat(self, agent_reply_header="> 🤖 AI 回复 · {model} · {effort}")
        msg = ask(h, "/agent 状态")
        run = running(h, msg)
        cur = h.app.store.run_get(run["id"])
        cur.pop("model", None)
        cur.pop("effort", None)  # the model is only known once the reply renders
        h.app.store.run_put(cur)
        h.app.agent._model_of = lambda r, header="": ("claude-opus-5-5", "xhigh")
        with self.assertRaises(Exception) as c:
            h.tool("agent_reply", request=run["id"], text="> 🤖 AI 回复 · Claude Opus 5.5 · xhigh")
        self.assertIn("只有 AI 标记", str(c.exception))
        self.assertEqual(h.app.store.run_get(run["id"])["reply_inflight"], 0, "the held place is given back")
        h.tool("agent_reply", request=run["id"], text="一切正常。")
        self.assertTrue(h.sent()[-1]["text"].startswith("一切正常。"))

    def test_history_handed_over_is_kept_not_acted_on(self):
        from openmate_core.backend import Message

        h = wechat(self)
        now = time.time()
        h.app._history_from_backend([Message(id="hx1", chat_id="chat_example_dm_a", position=int(now * 1000),
                                             sender_id="me", mine=True, text="/agent 这是我回复里的一句", at=now)])
        self.assertTrue(any(r["id"] == "hx1" for r in h.app.store.messages_in_chat("chat_example_dm_a", 50))
                        if hasattr(h.app.store, "messages_in_chat") else True)
        time.sleep(0.3)
        self.assertEqual([r for r in h.app.store.runs_in_chat("chat_example_dm_a", 50)], [], "history is never a request")


class KindsAndNamesLaterTest(unittest.TestCase):
    def test_a_kind_learnt_later_replaces_the_one_kept(self):
        h = wechat(self)
        self.assertEqual(h.app.backend.on_people_kinds, h.app._people_kinds_from_backend)
        h.app.store.set_people_kinds({"qqtech": "user"})
        h.app.backend.on_people_kinds({"qqtech": "bot", "": "bot", "x": "robot"})
        self.assertEqual(h.app.store.people_kinds(["qqtech", "x"]), {"qqtech": "bot"})

    def test_an_id_kept_as_a_name_is_asked_about_again(self):
        from openmate_core.backend import Chat, Message

        h = wechat(self)
        cid = "chat_example_dm_a"
        h.app.store.upsert_chat(Chat(id=cid, kind="dm", name=cid, peer_id=cid))
        now = time.time()
        h.app.ingest([Message(id="nm1", chat_id=cid, position=int(now * 1000), sender_id="user_example_a",
                              text="在吗", at=now)], live=False)
        self.assertNotEqual(h.app.store.get_chat(cid)["name"], cid)


class GroupCandidatesTest(unittest.TestCase):
    def test_every_listed_group_can_be_switched_on(self):
        from openmate_core.backend import Chat

        h = wechat(self)
        h.app.backend.group_candidates = lambda limit: [Chat(id="quiet@chatroom", kind="group", name="安静的群")]
        sec = [x for x in page(h)["sections"] if x.get("title") == "自动代答"][0]
        row = [r for r in sec["items"] if r["key"] == "auto:quiet@chatroom"][0]
        self.assertEqual(row["title"], "安静的群")
        act(h, "autoreply", {"auto:quiet@chatroom": "true"})
        self.assertIn("quiet@chatroom", h.app.store.autoreply_chats())
        self.assertEqual(h.app.store.get_chat("quiet@chatroom")["name"], "安静的群")


class FeishuUnchangedTest(unittest.TestCase):
    """What the shared core keeps doing for Feishu (it can edit): progress, and more than one /agent reply."""

    def test_feishu_agent_may_reply_twice_and_auto_reply_may_send_progress(self):
        h = Harness(self, config={"agent": True, "agent_reply_header": " ", "agent_ack_reaction": ""})
        h.sign_in()
        wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
        msg = ask(h, "/agent 两件事")
        run = running(h, msg)
        h.tool("agent_reply", request=run["id"], text="第一件做完了。")
        h.tool("agent_reply", request=run["id"], text="第二件也做完了。")
        h.app.store.autoreply_set(TEAM, True)
        at = h.world.push(TEAM, "user_example_b", "@示例用户 怎么样了", at_me=True)
        auto = running(h, at)
        self.assertIn("进度", h.tool("agent_reply", request=auto["id"], text="在查", progress=True))

    def test_ai_marks_read_as_before(self):
        from openmate_core.agent import AI_MARKS, ai_marked

        self.assertTrue(ai_marked("> 🤖 AI 代答 · 模型 · high\n周五上线", AI_MARKS))
        self.assertFalse(ai_marked("@张三 请解释\u2005🤖 AI 代答 是什么意思？", AI_MARKS), "Feishu text as in 0.11.0")


class CoreFixesTest(unittest.TestCase):
    def test_a_new_account_forgets_the_marked_groups(self):
        h = wechat(self)
        h.app.store.autoreply_set(TEAM, True)
        h.app.store.set_people_kinds({"user_example_b": "user"})
        h.app.store.wipe_account_data()
        self.assertEqual(h.app.store.autoreply_chats(), [])
        self.assertEqual(h.app.store.people_kinds(["user_example_b"]), {})

    def test_pruning_runs_never_frees_the_quota(self):
        h = wechat(self, auto_reply_daily_most=1)
        h.app.store.autoreply_set(TEAM, True)
        m1 = h.world.push(TEAM, "user_example_b", "@示例用户 一", at_me=True)
        running(h, m1)
        for r in h.app.store.runs_in_chat(TEAM, 50):
            r["state"] = "done"
            h.app.store.run_put(r)
        h.app.store.runs_prune(time.time() + 1, 0)  # every finished run gone
        self.assertEqual([r for r in h.app.store.runs_in_chat(TEAM, 50) if r.get("kind") == "auto"], [])
        self.assertEqual(h.app.agent.auto_used(TEAM), 1)
        h.world.push(TEAM, "user_example_b", "@示例用户 二", at_me=True)
        time.sleep(0.5)
        self.assertEqual(h.app.agent.auto_used(TEAM), 1, "the day's most holds")

    def test_the_hourly_most_holds_for_two_at_once(self):
        h = wechat(self, auto_reply_hourly_most=1)
        h.app.store.autoreply_set(TEAM, True)
        agent = h.app.agent
        # drive _auto_start directly from two threads at the same moment
        go = threading.Barrier(2)
        now = time.time()

        def start(i):
            row = {"id": "race_%d" % i, "chat_id": TEAM, "sender_id": "user_example_b", "sender_name": "乙",
                   "text": "@示例用户 问题 %d" % i, "at": now, "at_me": True, "mine": False, "root_id": "",
                   "thread_id": "", "position": i}
            go.wait()
            agent._auto_start(row, row["text"], now)

        threads = [threading.Thread(target=start, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        autos = [r for r in h.app.store.runs_in_chat(TEAM, 50) if r.get("kind") == "auto"]
        self.assertEqual(len(autos), 1, "one place in the hour, one run")


if __name__ == "__main__":
    unittest.main()
