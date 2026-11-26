"""/agent: which messages start a request, the conversation and its context, the reply tool's binding, the fallback,
continuing a thread, /stop, the timeout and resuming after a restart."""

try:  # first: every temporary directory of this run in one, removed at its end
    import _runtmp  # noqa: F401
except ImportError:  # (started by its path from elsewhere: its directory is not on the path)
    pass
import re
import threading
import time
import unittest

from harness import CID, Harness, HubSideError, fast, wait_until

SUFFIX = "> 🤖 AI 代答，仅供参考"
RUN_ID = re.compile(r"omr_[0-9a-f]{20}")


def agent(test, timing=None, hub_capabilities=None, **cfg):
    # these tests check the end mark (the style before 0.4); DefaultsTest checks today's: a header, a reaction
    config = {"agent": True, "agent_reply_suffix": SUFFIX, "agent_reply_header": " ", "agent_ack_reaction": ""}
    config.update(cfg)
    more = {"hub_capabilities": hub_capabilities} if hub_capabilities is not None else {}
    h = Harness(test, config=config, timing=timing, **more)
    h.sign_in()
    wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
    return h


def ask(h, text, chat="chat_example_team", **kw):
    return h.world.push(chat, "me", text, **kw)


def started(h, n=1):
    wait_until(lambda: len(h.http.starts()) >= n, what="%d conversation(s)" % n)
    return h.http.starts()[n - 1]


def run_of(h, msg):
    return wait_until(lambda: next((r for r in h.app.store.runs_in_chat(msg.chat_id) if r["request_id"] == msg.id), None),
                      what="the run")


def running(h, msg):
    return wait_until(lambda: (lambda r: r if r and r["state"] == "running" else None)(run_of(h, msg)), what="running")


def replies(h):
    return [s for s in h.sent() if s["text"].rstrip().endswith(SUFFIX) or "已停止" in s["text"] or "已经结束了" in s["text"]]


class TriggerTest(unittest.TestCase):
    def test_only_my_fresh_live_prefixed_messages(self):
        h = agent(self)
        h.world.push("chat_example_team", "user_example_a", "/agent 帮我写周报")  # someone else's
        ask(h, "/agent 很久以前的", ago=11 * 60)  # too old
        ask(h, "/agents 不是前缀")
        ask(h, "/agent 这是一条代答\n\n" + SUFFIX)  # one of the Agent's own replies
        ask(h, "/agent 系统消息", type="SYSTEM")
        time.sleep(0.3)
        self.assertEqual(h.http.starts(), [])
        m = ask(h, "@示例同事甲 /agent：帮我看看这个问题")
        body = started(h)["body"]
        self.assertTrue(body["prompt"].endswith("\n帮我看看这个问题"))
        self.assertEqual(run_of(h, m)["request_text"], "帮我看看这个问题")

    def test_history_is_never_a_request(self):
        h = agent(self)
        from openmate_core.backend import BackendError

        h.world.listen_error = BackendError("网络不通", "unreachable")
        h.world.drop()
        wait_until(lambda: h.status()["state"] == "error", what="down")
        missed = h.world.make("chat_example_team", "me", "/agent 断线时发的")
        h.world.listen_error = None
        h.wait_ok()
        wait_until(lambda: h.app.store.message("chat_example_team", missed.id), what="caught up")
        time.sleep(0.2)
        self.assertEqual(h.http.starts(), [])

    def test_bare_prefix_and_handled_once(self):
        h = agent(self)
        m = ask(h, "/agent")
        self.assertTrue(started(h)["body"]["prompt"].endswith("\n/agent"))
        row = h.app.store.message(m.chat_id, m.id)
        h.app.agent.on_message(row)  # delivered again
        time.sleep(0.2)
        self.assertEqual(len(h.http.starts()), 1)

    def test_disabled(self):
        h = Harness(self, config={"agent": False})
        h.sign_in()
        ask(h, "/agent 你好")
        time.sleep(0.3)
        self.assertEqual(h.http.starts(), [])

    def test_prefix_setting(self):
        h = agent(self, agent_prefix="/ai")
        ask(h, "/agent 不是这个前缀")
        time.sleep(0.2)
        self.assertEqual(h.http.starts(), [])
        ask(h, "/AI 用这个")
        started(h)
        for bad in ("/stop", "agent", "/a b", ""):
            hb = Harness(self, config={"agent": True, "agent_prefix": bad})
            self.assertEqual(hb.app.agent.prefix, "/agent", bad)


class SelfMentionTest(unittest.TestCase):
    """「@自己 …」: the user's own live message with a real @ of the user is a request like /agent (Feishu)."""

    def test_my_own_mention_of_me_is_a_request(self):
        h = agent(self)
        m = ask(h, "@示例用户 设置为 write", at_me=True)
        body = started(h)["body"]
        self.assertTrue(body["prompt"].endswith("\n设置为 write"))
        self.assertNotIn("guest", body, "the user's own request: a full conversation, not a guest one")
        self.assertEqual(run_of(h, m)["request_text"], "设置为 write")
        row = h.app.store.message(m.chat_id, m.id)
        h.app.agent.on_message(row)  # delivered again
        time.sleep(0.2)
        self.assertEqual(len(h.http.starts()), 1)

    def test_a_mention_of_me_anywhere_and_a_bare_one(self):
        h = agent(self)
        m = ask(h, "@示例同事甲 这个问题 @示例用户 帮忙看下", at_me=True)
        started(h)
        self.assertEqual(run_of(h, m)["request_text"], "这个问题 @示例用户 帮忙看下")
        bare = ask(h, "@示例用户", at_me=True, chat="chat_example_dm_b")
        started(h, 2)
        self.assertEqual(run_of(h, bare)["request_text"], "")

    def test_only_my_fresh_live_mentions_of_me(self):
        h = agent(self)
        ask(h, "@示例同事甲 帮我看下")  # mine, someone else's @
        ask(h, "@示例用户 很久以前的", at_me=True, ago=11 * 60)  # too old
        ask(h, "@示例用户 这是一条代答\n\n" + SUFFIX, at_me=True)  # one of the Agent's own replies
        h.world.push("chat_example_team", "user_example_a", "@示例用户 帮我看下", at_me=True)  # someone else's: not this
        time.sleep(0.3)
        self.assertEqual(h.http.starts(), [])

    def test_switched_off_or_agent_off(self):
        off = agent(self, agent_self_mention=False)
        self.assertFalse(off.app.agent.self_mention)
        at = ask(off, "@示例用户 设置为 write", at_me=True)
        prefixed = ask(off, "/agent 前缀照常")
        run_of(off, prefixed)
        time.sleep(0.3)
        self.assertEqual([r["request_id"] for r in off.app.store.runs_in_chat(at.chat_id)], [prefixed.id])
        self.assertEqual(len(off.http.starts()), 1)
        h = Harness(self, config={"agent": False})
        h.sign_in()
        ask(h, "@示例用户 设置为 write", at_me=True)
        time.sleep(0.3)
        self.assertEqual(h.http.starts(), [])


class SelfMentionReadingTest(unittest.TestCase):
    """What one live reading says decides an @自己, never a mark another reading left in the store."""

    def test_an_at_me_an_earlier_reading_stored_does_not_make_a_request(self):
        from openmate_core.backend import Message

        h = agent(self)
        kw = dict(id="m-hist", chat_id="chat_example_team", position=9000, sender_id=h.world.me.id, mine=True,
                  at=time.time())
        h.app.ingest([Message(text="@示例用户 先这么写", at_me=True, **kw)], live=False)  # a catch-up read it with the @
        h.app.ingest([Message(text="已自行处理", at_me=False, **kw)], live=True)  # its push: the @ taken out
        time.sleep(0.3)
        self.assertEqual(h.http.starts(), [])
        self.assertTrue(h.app.store.message("chat_example_team", "m-hist")["at_me"], "the stored mark is kept")

    def test_an_edit_heard_first_makes_no_request(self):
        from openmate_core.backend import Message

        h = agent(self)
        h.app.ingest([Message(id="m-edit", chat_id="chat_example_team", position=9001, sender_id=h.world.me.id,
                              mine=True, at=time.time(), text="@示例用户 改成这样", at_me=True, edited=True)],
                     live=True)
        time.sleep(0.3)
        self.assertEqual(h.http.starts(), [])

    def test_the_backend_s_cut_is_the_request(self):
        h = agent(self)
        m = ask(h, "@Jane Doe 总结日报", at_me=True, text_after_mentions="总结日报")
        started(h)
        self.assertEqual(run_of(h, m)["request_text"], "总结日报", "not \"Doe 总结日报\"")
        m2 = ask(h, "@张三/agent 查一下", at_me=True, text_after_mentions="/agent 查一下", chat="chat_example_dm_b")
        started(h, 2)
        self.assertEqual(run_of(h, m2)["request_text"], "查一下", "a prefix after the @ of me: dropped as usual")

    def test_an_at_of_me_with_the_prefix_keeps_the_cut_s_code(self):
        h = agent(self)
        m = ask(h, "@示例用户 /agent 请解释这段 SQL", at_me=True, text_after_mentions="/agent 请解释这段 SQL\nSELECT 1")
        started(h)
        self.assertEqual(run_of(h, m)["request_text"], "请解释这段 SQL\nSELECT 1")

    def test_code_right_after_the_prefix_stays_as_written(self):
        h = agent(self)
        m = ask(h, "@示例用户 /agent", at_me=True, text_after_mentions="/agent\n    ::f();\n    g();")
        started(h)
        self.assertEqual(run_of(h, m)["request_text"], "    ::f();\n    g();")
        m2 = ask(h, "@示例用户 /agent：看看", at_me=True, text_after_mentions="/agent：看看", chat="chat_example_dm_b")
        started(h, 2)
        self.assertEqual(run_of(h, m2)["request_text"], "看看")
        from openmate_core.agent import after_prefix

        self.assertEqual(after_prefix("/agent\r看看", "/agent"), "看看", "a lone CR")
        self.assertEqual(after_prefix("/agent：::f();", "/agent"), "::f();", "one separator, the code's colons stay")
        self.assertEqual(after_prefix("/agent \r\n  x", "/agent"), "  x")
        self.assertIsNone(after_prefix("/agents x", "/agent"))

    def test_a_code_block_first_keeps_its_indents(self):
        h = agent(self)
        cut = "    def a(self):\n        pass\n\n这两个方法该怎么测？"
        m = ask(h, "@示例用户 这两个方法该怎么测？", at_me=True, text_after_mentions=cut)
        started(h)
        self.assertEqual(run_of(h, m)["request_text"], cut)

    def test_commands_read_as_before_whatever_the_cut(self):
        # /agent and /stop are matched as before 0.11.40 (leading @s cut at spaces): a typed "@旧称" after a real @
        h = agent(self)
        m = ask(h, "@同事 @旧称 /agent 查订单", text_after_mentions="@旧称 /agent 查订单")
        started(h)
        self.assertEqual(run_of(h, m)["request_text"], "查订单")

    def test_an_edit_not_known_makes_no_request(self):
        h = agent(self)
        ask(h, "@示例用户 不知道改没改过", at_me=True, edited=None)
        time.sleep(0.3)
        self.assertEqual(h.http.starts(), [])

    def test_a_held_back_line_makes_no_request(self):
        from openmate_core.backend import Message

        h = agent(self)
        kw = dict(id="m-dlp", chat_id="chat_example_team", position=9002, sender_id=h.world.me.id, mine=True,
                  at=time.time())
        h.app.ingest([Message(text="[消息被拦截]", blocked=True, **kw)], live=False)  # a catch-up: held back
        h.app.ingest([Message(text="@示例用户 查一下", at_me=True, text_after_mentions="查一下", **kw)], live=True)
        time.sleep(0.3)
        self.assertEqual(h.http.starts(), [])

    def test_a_stop_after_a_name_with_a_space_still_stops(self):
        h = agent(self)
        ask(h, "@Jane Doe /stop", at_me=True, text_after_mentions="/stop")
        time.sleep(0.3)
        self.assertEqual(h.http.starts(), [], "a stop, not a request")


class ConversationTest(unittest.TestCase):
    def test_start_with_context(self):
        h = agent(self, agent_machine="mac-2", agent_launcher="example-launcher")
        h.http.kb[CID + "-chat_example_team"] = {"chat_id": CID + "-chat_example_team", "chat_name": "示例项目群",
                                                 "kind": "group", "summary": "这个群讨论接口文档",
                                                 "projects": [{"name": "示例接口项目", "summary": "对外接口"}]}
        h.hub.cli = lambda p: {"integration": "lark-cli", "result": {"data": {"items": [
            {"title": "示例评审文档", "url": "https://example.feishu.cn/docx/ExampleDoc001", "summary": "评审要点"}]}}}
        quoted = h.app.store.messages_before("chat_example_team", None, 10)[-1]
        m = ask(h, "/agent 总结一下 https://example.feishu.cn/docx/ExampleDoc001 然后回复", parent_id=quoted["id"])
        req = started(h)
        body = req["body"]
        self.assertEqual(req["auth"], "Bearer pext_test_token")
        self.assertEqual(body["purpose"], "lark.agent")
        self.assertIs(body["report_tasks"], True)
        self.assertEqual(body["machine_id"], "mac-2")
        self.assertEqual(body["launcher"], "example-launcher")
        prompt = body["prompt"]
        run = running(h, m)
        self.assertIn('kind="instructions"', prompt)
        self.assertIn('request 填 "%s"' % run["id"], prompt)
        self.assertIn(CID + "_agent_reply", prompt)
        self.assertIn("飞书群「示例项目群」", prompt)
        self.assertIn("下午三点开周会", prompt)
        self.assertIn("← 请求", prompt)
        self.assertIn('title="引用的消息"', prompt)
        self.assertIn("接口文档这周谁来更新", prompt.split('title="引用的消息"')[1])
        self.assertIn("这个群讨论接口文档", prompt)
        self.assertIn("示例接口项目", prompt)
        self.assertIn('kind="doc" title="示例评审文档" url="https://example.feishu.cn/docx/ExampleDoc001"', prompt)
        self.assertTrue(prompt.endswith("\n总结一下 https://example.feishu.cn/docx/ExampleDoc001 然后回复"))
        self.assertEqual(h.hub.calls[-1][1]["kind"], "docs")

    def test_context_is_capped(self):
        h = agent(self)
        for i in range(45):
            h.world.push("chat_example_chatter", "user_example_a", ("很长的一条 %d " % i) + "字" * 1500)
        ask(h, "/agent 总结", chat="chat_example_chatter")
        prompt = started(h)["body"]["prompt"]
        self.assertLessEqual(len(prompt), 31000)
        self.assertIn("很长的一条 44", prompt)  # newest kept
        self.assertNotIn("很长的一条 3 ", prompt)

    def test_no_kb_or_docs_still_starts(self):
        h = agent(self)
        ask(h, "/agent 看看 https://example.feishu.cn/wiki/ExampleWiki9")
        prompt = started(h)["body"]["prompt"]
        self.assertIn("https://example.feishu.cn/wiki/ExampleWiki9", prompt)
        self.assertIn("lark-cli", prompt)  # says why the document is not in it
        self.assertNotIn("知识库里的会话背景", prompt)


class ReplyTest(unittest.TestCase):
    def setUp(self):
        self.h = agent(self)
        self.m = ask(self.h, "/agent 帮我回复一下")
        started(self.h)
        self.run = running(self.h, self.m)
        self.sid = self.run["session_id"]

    def test_reply_tool_is_bound_to_its_request(self):
        h = self.h
        with self.assertRaises(HubSideError) as e:
            h.tool("agent_reply", request="omr_00000000000000000000", text="冒充")
        self.assertIn("没有这个 /agent 请求", e.exception.message)
        text = h.tool("agent_reply", request=self.run["id"], text="接口文档我这周五前更新。")
        self.assertIn("已回复", text)
        sent = h.sent()[-1]
        self.assertEqual(sent["chat"], "chat_example_team")
        self.assertEqual(sent["reply_to"], self.m.id)
        self.assertEqual(sent["root"], self.m.id)
        self.assertEqual(sent["text"], "接口文档我这周五前更新。\n\n" + SUFFIX)
        # the same reply again is not sent twice; the turn's end posts nothing more
        h.tool("agent_reply", request=self.run["id"], text="接口文档我这周五前更新。")
        h.http.add(self.sid, "assistant", "text", "已经回复了")
        h.http.end_turn(self.sid)
        wait_until(lambda: h.app.store.run_get(self.run["id"])["state"] == "done", what="done")
        time.sleep(0.2)
        self.assertEqual(len(replies(h)), 1)
        # a later progress reply (a dispatched task's result) still goes into the thread
        h.tool("agent_reply", request=self.run["id"], text="任务也完成了")
        self.assertEqual(len(replies(h)), 2)

    def test_fallback_posts_the_latest_answer_once(self):
        h = self.h
        h.http.add(self.sid, "assistant", "text", "先查一下")
        h.http.add(self.sid, "assistant", "tool_use", tool_name="Bash")
        h.http.add(self.sid, "user", "tool_result", "ok")
        last = h.http.add(self.sid, "assistant", "text", "最终答案（截断）…", full_size=9000)
        h.http.full[last["id"]] = "最终答案：接口文档周五前更新完。"
        h.http.end_turn(self.sid)
        wait_until(lambda: replies(h), what="the fallback")
        time.sleep(0.3)
        self.assertEqual([s["text"] for s in replies(h)], ["最终答案：接口文档周五前更新完。\n\n" + SUFFIX])
        run = h.app.store.run_get(self.run["id"])
        self.assertEqual((run["state"], run["fallback"]), ("done", "sent"))

    def test_agent_wrote_the_mark_itself(self):
        h = self.h
        h.tool("agent_reply", request=self.run["id"], text="好的\n\n" + SUFFIX)
        self.assertEqual(h.sent()[-1]["text"], "好的\n\n" + SUFFIX)

    def test_turn_without_words_posts_nothing(self):
        h = self.h
        h.http.end_turn(self.sid)
        wait_until(lambda: h.app.store.run_get(self.run["id"])["state"] == "done", what="done")
        time.sleep(0.2)
        self.assertEqual(replies(h), [])

    def test_stopped_in_the_app(self):
        h = self.h
        h.http.add(self.sid, "assistant", "text", "做到一半")
        h.http.add(self.sid, "system", "notice", "已中断")
        h.http.end_turn(self.sid)
        wait_until(lambda: replies(h), what="the notice")
        time.sleep(0.2)
        self.assertEqual([s["text"] for s in replies(h)], ["已在 Persona 里停止\n\n" + SUFFIX])
        with self.assertRaises(HubSideError):
            h.tool("agent_reply", request=self.run["id"], text="还想回")


class SentRecordTest(unittest.TestCase):
    """Every /agent send goes out without a card, so each one that went out is put in the hub's sent-as-user ledger
    (sent.record, allowed by sends_as_user in the manifest); gated writes and the user's own Inbox Send are not."""

    def setUp(self):
        self.h = agent(self)
        self.m = ask(self.h, "/agent 帮我回复一下")
        started(self.h)
        self.run = running(self.h, self.m)
        self.sid = self.run["session_id"]

    def records(self, n):
        return wait_until(lambda: (lambda r: r if len(r) >= n else None)(list(self.h.hub.sent_records)),
                          what="%d sent.record call(s)" % n)

    def test_the_manifest_declares_it(self):
        import json
        import os
        from harness import ROOT

        with open(os.path.join(ROOT, "extensions", "openmate-feishu", "persona-extension.json"), encoding="utf-8") as f:
            conn = json.load(f)["contributes"]["connectors"][0]
        self.assertIs(conn["sends_as_user"], True)
        res = self.h.restart(initialize=False).hub.request("initialize", {
            "protocol": 1, "hub": {}, "extension": {"id": "openmate-feishu", "version": "0.1.0"}, "config": {}})
        self.assertIs(res["contributes"]["connectors"][0]["sends_as_user"], True)

    def test_a_reply_through_the_tool_is_recorded_once(self):
        h = self.h
        h.tool("agent_reply", request=self.run["id"], text="接口文档我这周五前更新。")
        rec = self.records(1)[0]
        sent = h.sent()[-1]
        self.assertEqual(rec["contribution"], CID)
        self.assertEqual(rec["text"], "接口文档我这周五前更新。\n\n" + SUFFIX)
        self.assertEqual(rec["target"], {"chat": "chat_example_team", "thread": self.m.id, "reply_to": self.m.id})
        self.assertEqual(rec["message_ref"], sent["id"])
        self.assertEqual((rec["session_id"], rec["machine_id"]), (self.sid, "mac-example"))
        self.assertEqual(rec["setting"], "agent")
        self.assertEqual(rec["summary"], "/agent 以你的身份回复到飞书群「示例项目群」（5 人）")
        self.assertNotIn("\n", rec["summary"])
        h.tool("agent_reply", request=self.run["id"], text="接口文档我这周五前更新。")  # answered from the journal
        time.sleep(0.3)
        self.assertEqual(len(h.hub.sent_records), 1)

    def test_the_fallback_and_stop_answers_are_recorded(self):
        h = self.h
        h.http.add(self.sid, "assistant", "text", "最终答案")
        h.http.end_turn(self.sid)
        wait_until(lambda: replies(h), what="the fallback")
        stop = ask(h, "/stop", root_id=self.m.id)
        wait_until(lambda: len(replies(h)) == 2, what="the stop answer")
        recs = self.records(2)
        self.assertEqual([r["text"] for r in recs], [x["text"] for x in replies(h)])
        self.assertEqual(recs[0]["text"], "最终答案\n\n" + SUFFIX)
        self.assertEqual(recs[1]["target"], {"chat": "chat_example_team", "thread": self.m.id, "reply_to": stop.id})

    def test_long_text_is_cut_to_what_the_hub_keeps(self):
        h = self.h
        h.tool("agent_reply", request=self.run["id"], text="字" * 5000)
        self.assertEqual(len(self.records(1)[0]["text"]), 4000)

    def test_a_send_that_did_not_surely_go_out_is_not_recorded(self):
        h = self.h
        h.world.send_fail = "uncertain"
        with self.assertRaises(HubSideError):
            h.tool("agent_reply", request=self.run["id"], text="可能发出去了")
        h.world.send_fail = "rejected"
        with self.assertRaises(HubSideError):
            h.tool("agent_reply", request=self.run["id"], text="没发出去")
        time.sleep(0.3)
        self.assertEqual(h.hub.sent_records, [])

    def test_gated_writes_and_inbox_sends_are_the_hubs_to_record(self):
        h = self.h
        h.c("actions.do", name=CID + "_send", request_id="req-1", args={"chat": "chat_example_dm_a", "text": "确认过的"})
        h.c("inbox.reply", thread="chat_example_dm_a", text="我自己发的")
        time.sleep(0.3)
        self.assertEqual(h.hub.sent_records, [])
        self.assertEqual(h.hub.sent_calls, 0)

    def test_a_busy_ledger_is_tried_again(self):
        h = self.h
        answers = [HubSideError("rate_limited", "slow down", {"retry_after": 0.05}), HubSideError("", "ledger write failed")]

        def answer(params):
            if answers:
                raise answers.pop(0)

        h.hub.sent_answer = answer
        h.tool("agent_reply", request=self.run["id"], text="稍后记下")
        self.assertEqual(self.records(1)[0]["text"], "稍后记下\n\n" + SUFFIX)
        self.assertEqual(h.hub.sent_calls, 3)

    def test_a_refusal_is_not_tried_again(self):
        h = self.h

        def answer(params):
            raise HubSideError("permission_denied", "not declared")

        h.hub.sent_answer = answer
        text = h.tool("agent_reply", request=self.run["id"], text="照样发出")
        self.assertIn("已回复", text)  # the send stands
        time.sleep(0.3)
        self.assertEqual(h.hub.sent_calls, 1)
        self.assertEqual(h.sent()[-1]["text"], "照样发出\n\n" + SUFFIX)


class ContinueTest(unittest.TestCase):
    def test_a_request_in_the_thread_goes_on_in_the_same_conversation(self):
        h = agent(self)
        first = ask(h, "/agent 第一个问题")
        started(h)
        run1 = running(h, first)
        sid = run1["session_id"]
        h.http.add(sid, "assistant", "text", "第一个回答")
        h.http.end_turn(sid)
        wait_until(lambda: replies(h), what="first answer")
        second = ask(h, "/agent 接着问", root_id=first.id)
        msgs = wait_until(lambda: h.http.posts("/api/v1/sessions/mac-example/%s/messages" % sid), what="continued")
        self.assertEqual(len(h.http.starts()), 1)
        run2 = running(h, second)
        self.assertNotEqual(run2["id"], run1["id"])
        body = msgs[0]["body"]
        self.assertEqual(body["client_msg_id"], "openmate-" + run2["id"])
        self.assertIn(run2["id"], body["text"])
        self.assertIn("上次请求之后的消息", body["text"])
        time.sleep(0.2)
        self.assertEqual(len(replies(h)), 1, "the first turn's end is not the second's")
        h.http.add(sid, "assistant", "text", "第二个回答")
        h.http.end_turn(sid)
        wait_until(lambda: len(replies(h)) == 2, what="second answer")
        s = replies(h)[-1]
        self.assertEqual((s["text"], s["reply_to"], s["root"]), ("第二个回答\n\n" + SUFFIX, second.id, first.id))
        # the first request's binding still names the first run only
        self.assertEqual(h.app.store.run_get(run1["id"])["request_id"], first.id)


class SupersedeTest(unittest.TestCase):
    def test_a_second_request_while_the_first_runs_gets_the_single_answer(self):
        h = agent(self)
        first = ask(h, "/agent 第一个")
        run1 = running(h, first)
        sid = run1["session_id"]
        second = ask(h, "/agent 再补充一点", root_id=first.id)
        wait_until(lambda: h.http.posts("/api/v1/sessions/mac-example/%s/messages" % sid), what="continued")
        run2 = running(h, second)
        self.assertEqual(h.app.store.run_get(run1["id"])["state"], "done")
        h.http.add(sid, "assistant", "text", "两件事一起答")
        h.http.end_turn(sid)
        wait_until(lambda: replies(h), what="the answer")
        time.sleep(0.3)
        self.assertEqual([(s["text"], s["reply_to"]) for s in replies(h)], [("两件事一起答\n\n" + SUFFIX, second.id)])
        self.assertEqual(h.app.store.run_get(run2["id"])["state"], "done")
        # the first request's tool still answers into its own thread
        h.tool("agent_reply", request=run1["id"], text="第一个的补充")
        self.assertEqual(h.sent()[-1]["reply_to"], first.id)

    def test_an_unmarked_reply_that_looks_like_a_request_is_not_one(self):
        h = agent(self, agent_reply_suffix=" ")
        m = ask(h, "/agent 回一句")
        run = running(h, m)
        h.tool("agent_reply", request=run["id"], text="/agent 这句只是回复里的文字")
        time.sleep(0.3)
        self.assertEqual(len(h.http.starts()), 1)


class StopTest(unittest.TestCase):
    def setUp(self):
        self.h = agent(self)
        self.m = ask(self.h, "/agent 写个长报告")
        started(self.h)
        self.run = running(self.h, self.m)
        self.sid = self.run["session_id"]

    def test_stop_in_the_thread(self):
        h = self.h
        stop = ask(h, "/stop", root_id=self.m.id)
        wait_until(lambda: replies(h), what="stopped")
        s = replies(h)[-1]
        self.assertEqual(s["text"], "已停止\n\n" + SUFFIX)
        self.assertEqual(s["reply_to"], stop.id)
        self.assertEqual(h.http.posts("/api/v1/assistant/conversations/%s/stop" % self.sid)[0]["body"], {})
        self.assertEqual(h.app.store.run_get(self.run["id"])["state"], "stopped")
        with self.assertRaises(HubSideError) as e:
            h.tool("agent_reply", request=self.run["id"], text="还想回")
        self.assertIn("停止", e.exception.message)
        h.http.add(self.sid, "assistant", "text", "半截话")
        h.http.end_turn(self.sid)
        ask(h, "@示例同事甲 /停", root_id=self.m.id)
        wait_until(lambda: len(replies(h)) == 2, what="the second stop")
        time.sleep(0.2)
        self.assertEqual([x["text"] for x in replies(h)], ["已停止\n\n" + SUFFIX, "已经结束了\n\n" + SUFFIX])

    def test_stop_by_quoting_lists_the_tasks(self):
        h = self.h
        h.http.sessions[self.sid]["tasks"] = [{"id": "todo_1", "title": "写周报", "agents": 1}]
        ask(h, "/停止", parent_id=self.m.id)
        wait_until(lambda: replies(h), what="stopped")
        self.assertEqual(replies(h)[-1]["text"], "已停止：写周报\n\n" + SUFFIX)

    def test_stop_after_it_finished(self):
        h = self.h
        h.http.add(self.sid, "assistant", "text", "完成了")
        h.http.end_turn(self.sid)
        wait_until(lambda: replies(h), what="answer")
        h.http.sessions[self.sid]["working"] = False
        ask(h, "/stop", root_id=self.m.id)
        wait_until(lambda: len(replies(h)) == 2, what="stop answer")
        self.assertEqual(replies(h)[-1]["text"], "已经结束了\n\n" + SUFFIX)

    def test_older_hub_without_stop_interrupts(self):
        h = self.h
        h.http.stop_status[self.sid] = 404
        ask(h, "/stop", root_id=self.m.id)
        wait_until(lambda: replies(h), what="stopped")
        self.assertTrue(h.http.posts("/api/v1/sessions/mac-example/%s/interrupt" % self.sid))
        self.assertEqual(replies(h)[-1]["text"], "已停止\n\n" + SUFFIX)

    def test_a_bare_stop_in_a_private_chat_stops_its_latest_request(self):
        h = self.h
        m = ask(h, "/agent 私聊里的长任务", chat="chat_example_dm_a")
        run = running(h, m)
        stop = ask(h, "/stop", chat="chat_example_dm_a")
        wait_until(lambda: len(replies(h)) == 1, what="stopped")
        s = replies(h)[0]
        self.assertEqual((s["chat"], s["text"], s["reply_to"]), ("chat_example_dm_a", "已停止\n\n" + SUFFIX, stop.id))
        self.assertEqual(h.app.store.run_get(run["id"])["state"], "stopped")
        self.assertEqual(h.app.store.run_get(self.run["id"])["state"], "running", "the group's request goes on")
        ask(h, "/停", chat="chat_example_dm_a")
        wait_until(lambda: len(replies(h)) == 2, what="the second stop")
        self.assertEqual(replies(h)[1]["text"], "已经结束了\n\n" + SUFFIX)

    def test_stop_says_when_background_work_goes_on(self):
        h = self.h
        with h.http.lock:
            h.http.sessions[self.sid].update(working=False, background=True)  # the turn is over, a command waits
        ask(h, "/stop", root_id=self.m.id)
        wait_until(lambda: replies(h), what="the answer")
        text = replies(h)[0]["text"]
        self.assertIn("后台任务在跑", text)
        self.assertIn("不会再回复到这里", text)
        self.assertEqual(h.app.store.run_get(self.run["id"])["state"], "stopped")
        with self.assertRaises(HubSideError):
            h.tool("agent_reply", request=self.run["id"], text="后台任务醒了")

    def test_stop_while_working_with_background_work(self):
        h = self.h
        with h.http.lock:
            h.http.sessions[self.sid]["background"] = True
        ask(h, "/stop", root_id=self.m.id)
        wait_until(lambda: replies(h), what="the answer")
        self.assertTrue(replies(h)[0]["text"].startswith("已停止。它在 Persona 里还有后台任务在跑"))

    def test_a_bare_stop_in_a_private_chat_without_requests_says_nothing(self):
        h = self.h
        ask(h, "/stop", chat="chat_example_dm_a")
        time.sleep(0.3)
        self.assertEqual(replies(h), [])

    def test_a_second_stop_of_a_stopped_request_leaves_the_resumed_one_alone(self):
        """Stopped, then asked again in the thread: a /stop quoting the old request again reaches nothing at the hub
        (it was stopped already), so it stops nothing here either."""
        h = agent(self)
        first = ask(h, "/agent 第一个")
        run1 = running(h, first)
        sid = run1["session_id"]
        ask(h, "/stop", root_id=first.id, parent_id=first.id)
        wait_until(lambda: h.app.store.run_get(run1["id"])["state"] == "stopped", what="stopped")
        second = ask(h, "/agent 接着来", root_id=first.id)
        run2 = running(h, second)
        ask(h, "/stop", root_id=first.id, parent_id=first.id)
        wait_until(lambda: [s for s in h.sent() if "已经结束了" in s["text"]], what="the answer to the second stop")
        self.assertEqual(h.app.store.run_get(run2["id"])["state"], "running")
        self.assertEqual(len(h.http.posts("/api/v1/assistant/conversations/%s/stop" % sid)), 1)
        self.assertIn("已回复", h.tool("agent_reply", request=run2["id"], text="接着来的回答"), "it still answers")

    def test_a_request_sent_after_a_stop_of_the_one_before_still_answers(self):
        """b gathers its context while the user stops a; b reaches the conversation after the stop (which asks it
        again): b's own answer goes out, as it always did."""
        h = agent(self)
        first = ask(h, "/agent 第一个")
        run1 = running(h, first)
        sid = run1["session_id"]
        gather = h.app.agent.build_prompt
        h.app.agent.build_prompt = lambda *a, **kw: (time.sleep(0.6), gather(*a, **kw))[1]
        second = ask(h, "/agent 接着来", root_id=first.id)
        time.sleep(0.2)
        ask(h, "/stop", root_id=first.id, parent_id=first.id)
        wait_until(lambda: h.http.posts("/api/v1/assistant/conversations/%s/stop" % sid), what="the stop")
        run2 = running(h, second)
        self.assertIn("已回复", h.tool("agent_reply", request=run2["id"], text="接着来的回答"))

    def test_a_request_whose_first_try_reached_nothing_counts_from_the_try_that_did(self):
        h = agent(self)
        first = ask(h, "/agent 第一个")
        run1 = running(h, first)
        sid = run1["session_id"]

        def offline_while_stopped():  # the user stops the first meanwhile; this try reaches nothing
            ask(h, "/stop", root_id=first.id, parent_id=first.id)
            wait_until(lambda: h.app.store.run_get(run1["id"])["state"] == "stopped", what="the first stopped")
            return 503, {"error": "offline", "code": "manager_offline"}

        h.http.send_hook[sid] = offline_while_stopped
        second = ask(h, "/agent 接着来", root_id=first.id)
        run2 = running(h, second)
        self.assertIn("已回复", h.tool("agent_reply", request=run2["id"], text="接着来的回答"))

    def test_only_my_stop_counts(self):
        h = self.h
        h.world.push("chat_example_team", "user_example_a", "/stop", root_id=self.m.id)
        ask(h, "/stop")  # mine, but in no request's thread
        time.sleep(0.3)
        self.assertEqual(replies(h), [])
        self.assertEqual(h.app.store.run_get(self.run["id"])["state"], "running")



class ReceiptsAndProgressTest(unittest.TestCase):
    """How a request shows in the chat: 🤔 swapped for ✅ / ❌ (a /stop takes it away), progress edited in one message."""

    def make(self, timing=None, **cfg):
        config = {"agent": True}
        config.update(cfg)
        h = Harness(self, config=config, timing=timing)
        h.sign_in()
        wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
        return h

    def test_the_reaction_says_how_it_ended(self):
        h = self.make()
        msg = ask(h, "/agent 查一下")
        run = running(h, msg)
        wait_until(lambda: h.world.reactions, what="the 🤔")
        h.tool("agent_reply", request=run["id"], text="查到了")
        self.assertEqual(h.world.reactions, [("chat_example_team", msg.id, "THINKING")], "the answer alone does not end it")
        h.http.end_turn(run["session_id"])
        wait_until(lambda: h.world.reactions == [("chat_example_team", msg.id, "DONE")], what="✅ instead of 🤔")
        h2 = self.make(timing=fast(agent_timeout=0.5))
        slow = ask(h2, "/agent 慢慢来")
        running(h2, slow)
        wait_until(lambda: h2.world.reactions == [("chat_example_team", slow.id, "CrossMark")], timeout=5, what="❌ on timeout")
        h3 = self.make(agent_done_reaction="", agent_fail_reaction="")
        stop = ask(h3, "/agent 停下")
        running(h3, stop)
        wait_until(lambda: h3.world.reactions, what="the 🤔")
        ask(h3, "/stop", root_id=stop.id)
        wait_until(lambda: h3.world.reactions == [] and h3.app.store.run_get(run_of(h3, stop)["id"])["state"] == "stopped",
                   what="a /stop takes the 🤔 away")

    def test_a_refused_reaction_never_holds_the_run(self):
        h = self.make()
        h.world.react_fail = True
        msg = ask(h, "/agent 没有表情也行")
        run = running(h, msg)
        h.tool("agent_reply", request=run["id"], text="好的")
        h.http.end_turn(run["session_id"])
        wait_until(lambda: h.app.store.run_get(run["id"])["state"] == "done", what="done")
        self.assertEqual(h.world.reactions, [])
        self.assertFalse(h.app.store.run_get(run["id"]).get("ack_end"), "no 🤔 went on: nothing to swap")

    def test_progress_is_one_message_edited_in_place(self):
        h = self.make()
        msg = ask(h, "/agent 分三步做")
        run = running(h, msg)
        out = h.tool("agent_reply", request=run["id"], text="第一步：读代码", progress=True)
        self.assertIn("之后的进度会原地更新这条", out)
        first = h.sent()[-1]
        self.assertEqual((first["reply_to"], first["root"]), (msg.id, msg.id))
        self.assertIn("⏳ 进行中", first["text"])
        self.assertTrue(h.app.agent.is_agent_text(first["text"]), "never read as a request")
        out = h.tool("agent_reply", request=run["id"], text="第二步：跑测试", progress=True)
        self.assertIn("原地编辑", out)
        self.assertEqual(len(h.sent()), 1, "edited, not sent again")
        self.assertEqual(h.world.edits[-1][1], first["id"])
        self.assertIn("第二步：跑测试\n\n> 🤖 AI 回复", h.sent()[0]["text"])
        h.tool("agent_reply", request=run["id"], text="做完了：三步都过了")
        self.assertEqual(len(h.sent()), 2, "the answer is a new message, so it notifies")
        self.assertTrue(h.sent()[1]["text"].startswith("做完了：三步都过了"), "the answer first, the mark after")
        self.assertIn("✅ 已完成", h.sent()[0]["text"], "the progress message says it is over")
        self.assertIn("第二步：跑测试", h.sent()[0]["text"], "with its last progress")
        self.assertEqual(h.app.store.run_get(run["id"])["replies"], 1, "progress is not the answer")

    def test_an_edit_refused_sends_a_new_progress_message(self):
        h = self.make()
        msg = ask(h, "/agent 改不了就重发")
        run = running(h, msg)
        h.tool("agent_reply", request=run["id"], text="进度一", progress=True)
        h.world.edit_fail = True
        h.tool("agent_reply", request=run["id"], text="进度二", progress=True)
        self.assertEqual(len(h.sent()), 2)
        self.assertEqual(h.app.store.run_get(run["id"])["progress_id"], h.sent()[1]["id"], "later edits go to the new one")
        h.tool("agent_reply", request=run["id"], text="进度二", progress=True)
        self.assertEqual(len(h.sent()), 2, "the same progress again is not sent twice")

    def test_a_turn_with_only_progress_still_posts_its_answer(self):
        h = self.make()
        msg = ask(h, "/agent 只发了进度")
        run = running(h, msg)
        h.tool("agent_reply", request=run["id"], text="正在查", progress=True)
        h.http.add(run["session_id"], "assistant", "text", "查完了，结论是可以")
        h.http.end_turn(run["session_id"])
        wait_until(lambda: len(h.sent()) == 2, what="the fallback answer")
        self.assertTrue(h.sent()[1]["text"].startswith("查完了，结论是可以"))
        wait_until(lambda: "✅ 已完成" in h.sent()[0]["text"], what="the progress message closed")
        self.assertEqual(h.world.reactions, [("chat_example_team", msg.id, "DONE")])

class PlacementTest(unittest.TestCase):
    """A /agent reply goes back where it was asked, as the backend read the request (Message.placement): only in a
    thread, in a thread and also in the main stream, or in the main stream; its progress message too, edited in place
    wherever it is."""

    def make(self, **cfg):
        config = {"agent": True}
        config.update(cfg)
        h = Harness(self, config=config)
        h.sign_in()
        wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
        return h

    def thread(self, h):
        """A root in the team group with a reply under it (the request quotes that reply)."""
        root = h.world.push("chat_example_team", "user_example_a", "[图片]", type="IMAGE")
        under = h.world.push("chat_example_team", "user_example_b", "还有货啊", root_id=root.id, thread_id=root.id,
                             parent_id=root.id, placement="thread")
        return root, under

    def where(self, sent):
        return sent["reply_to"], sent["root"], sent.get("to_chat", False)

    def progress_answer_and_where(self, h, msg):
        """Progress twice (one message, edited in place), then the answer: where each went."""
        run = running(h, msg)
        h.tool("agent_reply", request=run["id"], text="第一步：看图", progress=True)
        first = h.sent()[-1]
        out = h.tool("agent_reply", request=run["id"], text="第二步：查规格", progress=True)
        self.assertIn("原地编辑", out)
        self.assertEqual(len(h.sent()), 1, "edited, not sent again")
        self.assertEqual(h.world.edits[-1][:2], ("chat_example_team", first["id"]))
        self.assertIn("第二步：查规格", h.sent()[0]["text"])
        h.tool("agent_reply", request=run["id"], text="是一台裸金属实例")
        self.assertEqual(len(h.sent()), 2)
        self.assertIn("✅ 已完成", h.sent()[0]["text"], "the progress message, edited where it is")
        self.assertEqual(h.world.edits[-1][1], first["id"])
        return run, [self.where(x) for x in h.sent()]

    def test_only_in_the_thread_goes_back_to_the_thread(self):
        h = self.make()
        root, under = self.thread(h)
        msg = ask(h, "/agent 这个是什么货", root_id=root.id, thread_id=root.id, parent_id=under.id, placement="thread")
        run, places = self.progress_answer_and_where(h, msg)
        self.assertEqual(places, [(msg.id, root.id, False)] * 2)
        self.assertEqual(run["placement"], "thread")
        self.assertIn("回复会以我的身份发到这个话题里，", started(h)["body"]["prompt"])

    def test_in_the_thread_and_the_chat_goes_back_to_both(self):
        h = self.make()
        root, under = self.thread(h)
        msg = ask(h, "/agent 这个是什么货", root_id=root.id, thread_id=root.id, parent_id=under.id,
                  placement="thread_chat")
        run, places = self.progress_answer_and_where(h, msg)
        self.assertEqual(places, [(msg.id, root.id, True)] * 2, "into the thread, also to the chat, quoting the request")
        self.assertEqual(run["placement"], "thread_chat")
        self.assertIn("回复会以我的身份发到这个话题里，同时也发到群里", started(h)["body"]["prompt"])

    def test_in_the_main_stream_quotes_it_there(self):
        h = self.make()
        root, under = self.thread(h)
        msg = ask(h, "/agent 这个是什么货", parent_id=under.id, placement="chat")  # a quote in the main stream
        run, places = self.progress_answer_and_where(h, msg)
        self.assertEqual(places, [(msg.id, None, False)] * 2, "no thread opened on the request: a quote in the chat")
        self.assertIn("回复会以我的身份引用这条请求发到群里", started(h)["body"]["prompt"])
        plain = ask(h, "/agent 再问一句", placement="chat")
        run2 = running(h, plain)
        h.tool("agent_reply", request=run2["id"], text="好的")
        self.assertEqual(self.where(h.sent()[-1]), (plain.id, None, False))

    def test_the_run_s_own_notices_go_there_too(self):
        h = self.make(agent_reply_header=" ", agent_reply_suffix=SUFFIX, agent_ack_reaction="")
        root, under = self.thread(h)
        msg = ask(h, "/agent 停下", root_id=root.id, thread_id=root.id, parent_id=under.id, placement="thread_chat")
        run = running(h, msg)
        h.http.add(run["session_id"], "assistant", "text", "做到一半")
        h.http.add(run["session_id"], "system", "notice", "已中断")
        h.http.end_turn(run["session_id"])
        notice = wait_until(lambda: next((x for x in h.sent() if "已在 Persona 里停止" in x["text"]), None), what="notice")
        self.assertEqual(self.where(notice), (msg.id, root.id, True))
        main = ask(h, "/agent 主界面", placement="chat")
        run2 = running(h, main)
        h.http.add(run2["session_id"], "assistant", "text", "主界面的答案")
        h.http.end_turn(run2["session_id"])
        answer = wait_until(lambda: next((x for x in h.sent() if "主界面的答案" in x["text"]), None), what="fallback")
        self.assertEqual(self.where(answer), (main.id, None, False), "the fallback answer too")

    def test_a_stop_is_answered_where_it_was_said(self):
        h = self.make(agent_reply_header=" ", agent_reply_suffix=SUFFIX, agent_ack_reaction="")
        root, under = self.thread(h)
        msg = ask(h, "/agent 慢慢做", root_id=root.id, thread_id=root.id, parent_id=under.id, placement="thread_chat")
        running(h, msg)
        stop = ask(h, "/stop", root_id=root.id, thread_id=root.id, parent_id=msg.id, placement="thread_chat")
        said = wait_until(lambda: next((x for x in h.sent() if "已停止" in x["text"]), None), what="the /stop answer")
        self.assertEqual(self.where(said), (stop.id, root.id, True))

    def test_a_stop_quoting_the_progress_message_in_the_main_stream(self):
        h = self.make(agent_reply_header=" ", agent_reply_suffix=SUFFIX, agent_ack_reaction="")
        msg = ask(h, "/agent 慢慢做", placement="chat")
        run = running(h, msg)
        h.tool("agent_reply", request=run["id"], text="第一步", progress=True)
        progress = h.sent()[-1]
        ask(h, "/stop", parent_id=progress["id"], placement="chat")  # no thread to find it by: the quote does
        wait_until(lambda: h.app.store.run_get(run["id"])["state"] == "stopped", what="stopped")

    def test_a_stop_quoting_the_timeout_notice_in_the_main_stream(self):
        h = Harness(self, config={"agent": True}, timing=fast(agent_timeout=0.5))
        h.sign_in()
        wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
        msg = ask(h, "/agent 很慢", placement="chat")
        run = running(h, msg)
        notice = wait_until(lambda: next((x for x in h.sent() if "不再等待" in x["text"]), None), timeout=5, what="notice")
        self.assertEqual(self.where(notice), (msg.id, None, False))
        wait_until(lambda: h.app.store.run_get(run["id"]).get("notice_id") == notice["id"], what="kept")
        self.assertEqual(h.app.agent._target({"chat_id": msg.chat_id, "parent_id": notice["id"]})["id"], run["id"])
        newer = ask(h, "/agent 同话题的新请求", root_id=msg.id, thread_id=msg.id, parent_id=notice["id"], placement="thread")
        later = running(h, newer)
        stop_in_thread = {"chat_id": msg.chat_id, "parent_id": notice["id"], "root_id": msg.id}
        self.assertEqual(h.app.agent._target(stop_in_thread)["id"], later["id"], "in a thread: its newest run, as before")

    def test_a_copy_is_never_a_command(self):
        h = self.make(agent_reply_header=" ", agent_reply_suffix=SUFFIX, agent_ack_reaction="", agent_auto=True)
        root, under = self.thread(h)
        ask(h, "/agent 副本先到", placement="chat", same_as="msg_example_source")  # the main stream's copy
        msg = ask(h, "/agent 副本先到", root_id=root.id, thread_id=root.id, parent_id=under.id,
                  placement="thread_chat", id="msg_example_source")
        run = running(h, msg)
        self.assertEqual(run["placement"], "thread_chat", "the original is the request, wherever the copy came")
        ask(h, "/stop", placement="chat", same_as="msg_example_stop")
        ask(h, "/stop", root_id=root.id, thread_id=root.id, parent_id=msg.id, placement="thread_chat",
            id="msg_example_stop")
        wait_until(lambda: h.app.store.run_get(run["id"])["state"] == "stopped", what="the original /stop stops it")
        time.sleep(0.2)
        self.assertEqual(len(h.http.starts()), 1)

    def test_a_placement_comes_with_its_own_reading(self):
        from openmate_core.backend import Message

        h = self.make()
        m = Message(id="msg_example_x", chat_id="chat_example_team", position=999, mine=True, text="x")
        h.app.store.add_messages([m], from_history=True)  # an older reading: not in a thread
        live = Message(id="msg_example_x", chat_id="chat_example_team", position=999, mine=True, text="x",
                       root_id="msg_example_r", placement="thread_chat", same_as="msg_example_s")
        (row,) = h.app.store.add_messages([live], from_history=False)
        self.assertEqual((row["placement"], row["root_id"]), ("thread_chat", "msg_example_r"), "the live reading wins")
        self.assertEqual(row["same_as"], "msg_example_s")
        self.assertEqual(h.app.store.message("chat_example_team", "msg_example_x")["root_id"], "", "the store's as it was")
        h.app.store.add_messages([Message(id="msg_example_y", chat_id="chat_example_team", position=1000, mine=True,
                                          text="y", parent_id="msg_example_q")], from_history=True)
        (row,) = h.app.store.add_messages([Message(id="msg_example_y", chat_id="chat_example_team", position=1000,
                                                   mine=True, text="y", placement="chat")], from_history=False)
        self.assertEqual(row["parent_id"], "msg_example_q", "a live reading naming no quote keeps the stored one")
        self.assertNotIn("placement", h.app.store.message("chat_example_team", "msg_example_x"), "never stored")

    def test_unknown_placement_keeps_the_old_way(self):
        h = self.make()
        msg = ask(h, "/agent 不知道在哪")  # a backend that reads no placement (WeChat, a direct chat)
        run = running(h, msg)
        self.assertEqual(run["placement"], "")
        h.tool("agent_reply", request=run["id"], text="好的")
        self.assertEqual(self.where(h.sent()[-1]), (msg.id, msg.id, False))
        self.assertIn("回复会以我的身份发到这个话题里，", started(h)["body"]["prompt"])


class TimeoutRestartTest(unittest.TestCase):
    def test_timeout(self):
        h = agent(self, timing=fast(agent_timeout=0.5))
        m = ask(h, "/agent 慢慢来")
        run = running(h, m)
        wait_until(lambda: replies(h), timeout=5, what="timeout message")
        time.sleep(0.2)
        texts = [s["text"] for s in replies(h)]
        self.assertEqual(len(texts), 1)
        self.assertIn("不再等待", texts[0])
        self.assertEqual(h.app.store.run_get(run["id"])["state"], "timeout")

    def test_resume_after_restart(self):
        h = agent(self)
        m = ask(h, "/agent 重启后接上")
        run = running(h, m)
        sid = run["session_id"]
        h2 = h.restart()
        h2.wait_ok()
        h2.http.add(sid, "assistant", "text", "重启后的回答")
        h2.http.end_turn(sid)
        wait_until(lambda: replies(h2), what="answer after restart")
        time.sleep(0.3)
        self.assertEqual([s["text"] for s in replies(h2)], ["重启后的回答\n\n" + SUFFIX])
        self.assertEqual(len(h2.http.starts()), 1)

    def test_a_start_cut_off_by_a_restart_is_not_started_twice(self):
        h = agent(self)
        m = h.world.push("chat_example_team", "me", "普通消息")
        h.app.store.run_put({"id": "omr_" + "0" * 20, "chat_id": "chat_example_team", "request_id": m.id,
                             "thread_key": m.id, "state": "starting", "created": time.time(), "root_id": "",
                             "replies": 0, "reply_inflight": 0, "reply_ids": []})
        h2 = h.restart()
        h2.wait_ok()
        wait_until(lambda: replies(h2), what="the notice")
        self.assertIn("重新发送", replies(h2)[0]["text"])
        self.assertEqual(h2.http.starts(), [])
        self.assertEqual(h2.app.store.run_get("omr_" + "0" * 20)["state"], "failed")


class NetworkTest(unittest.TestCase):
    """The hub is reached over a network that drops now and then (the manager's relay answers 503 manager_offline
    while its bus is down, 502 hub_unreachable when a request breaks on the way)."""

    def test_a_request_waits_while_the_manager_is_offline(self):
        h = agent(self)
        h.http.offline = 4
        m = ask(h, "/agent 网络断了一会儿")
        run = running(h, m)
        self.assertEqual(len(h.http.starts()), 1)
        self.assertNotIn("ref", run)
        self.assertEqual(replies(h), [])
        h.tool("agent_reply", request=run["id"], text="回来了")
        self.assertEqual([s["text"] for s in replies(h)], ["回来了\n\n" + SUFFIX])

    def test_it_gives_up_when_the_hub_stays_away(self):
        h = agent(self, timing=fast(agent_hub_wait=0.3))
        h.http.offline = 10 ** 6
        m = ask(h, "/agent 一直断着")
        wait_until(lambda: replies(h), what="the explanation")
        text = replies(h)[0]["text"]
        self.assertIn("网络断了", text)
        self.assertIn("重新发送", text)
        self.assertEqual(h.http.starts(), [])
        self.assertEqual(run_of(h, m)["state"], "failed")

    def test_a_start_whose_answer_was_lost_is_answered_by_the_same_operation(self):
        h = agent(self)
        h.http.lost_starts = 1
        m = ask(h, "/agent 回答丢了")
        run = running(h, m)
        starts = h.http.starts()
        self.assertEqual(len(starts), 2)
        self.assertTrue(starts[0]["op"] and starts[0]["op"] == starts[1]["op"], "one operation id")
        self.assertEqual(starts[0]["body"], starts[1]["body"], "the very same request")
        self.assertNotIn("ref", run)
        self.assertEqual(run["session_id"], "sess-1", "the conversation the first try opened")
        self.assertEqual(len(h.http.sessions), 1, "one conversation, never a second")
        h.tool("agent_reply", request=run["id"], text="唯一会话的回答")
        self.assertEqual([s["text"] for s in replies(h)], ["唯一会话的回答\n\n" + SUFFIX])

    def test_a_start_still_under_way_is_waited_for(self):
        h = agent(self)
        h.http.start_pending = 2
        m = ask(h, "/agent 还在处理")
        run = running(h, m)
        self.assertEqual(len({s["op"] for s in h.http.starts()}), 1)
        self.assertEqual(len(h.http.sessions), 1)
        self.assertEqual(run["session_id"], "sess-1")

    def test_a_start_nobody_knows_about_is_not_sent_again(self):
        h = agent(self)
        h.http.start_unknown = True
        m = ask(h, "/agent 不知道有没有开始")
        wait_until(lambda: replies(h), what="the explanation")
        self.assertIn("在 Persona 里看看", replies(h)[0]["text"])
        self.assertEqual(len(h.http.starts()), 1, "never asked again on its own")
        self.assertEqual(run_of(h, m)["state"], "failed")

    def test_lost_starts_are_tried_three_times(self):
        h = agent(self)
        h.http.lost_starts = 10
        m = ask(h, "/agent 一直丢")
        wait_until(lambda: replies(h), what="the explanation")
        self.assertIn("试了 3 次", replies(h)[0]["text"])
        self.assertIn("在 Persona 里看看", replies(h)[0]["text"])
        self.assertEqual(len(h.http.starts()), 3)
        self.assertEqual(len({s["op"] for s in h.http.starts()}), 1, "the same operation each time")
        self.assertEqual(len(h.http.sessions), 1, "one conversation at most")
        run = run_of(h, m)
        self.assertEqual((run["state"], run.get("ref")), ("failed", None), "never a new ref")

    def test_a_continuation_whose_answer_was_lost_is_sent_once(self):
        h = agent(self)
        first = ask(h, "/agent 第一个问题")
        run1 = running(h, first)
        sid = run1["session_id"]
        h.http.add(sid, "assistant", "text", "第一个回答")
        h.http.end_turn(sid)
        wait_until(lambda: replies(h), what="first answer")
        h.http.lost_sends = 1
        second = ask(h, "/agent 接着问", root_id=first.id)
        run2 = running(h, second)
        posts = h.http.posts("/api/v1/sessions/mac-example/%s/messages" % sid)
        self.assertEqual(len(posts), 2)
        self.assertEqual({p["body"]["client_msg_id"] for p in posts}, {"openmate-" + run2["id"]})
        typed = [it for it in h.http.sessions[sid]["items"] if it.get("client_msg_id")]
        self.assertEqual(len(typed), 1)
        self.assertEqual(len(h.http.starts()), 1)
        self.assertNotIn("ref", run2)

    def test_a_start_waits_for_the_machine_to_come_back(self):
        h = agent(self)
        h.http.machine_offline = 3
        m = ask(h, "/agent 电脑刚醒")
        run = running(h, m)
        self.assertEqual(len(h.http.starts()), 4)
        self.assertEqual(len(h.http.sessions), 1)
        self.assertNotIn("ref", run)
        self.assertEqual(replies(h), [])
        # the hub keeps "nothing ran" as an operation's receipt: each try after one goes under a new operation id
        ops = [s["op"] for s in h.http.starts()]
        self.assertEqual(len(set(ops)), 4)
        self.assertEqual(run["start_op"], ops[-1])
        self.assertEqual({s["body"]["prompt"] for s in h.http.starts()}, {h.http.starts()[0]["body"]["prompt"]})

    def _stop_at_new_id(self, h, change: bool):
        """Stop the agent at its first "nothing ran": after the new id is kept (change) or before. The id kept."""
        agent_ = h.app.agent
        real = agent_._new_start_op
        kept = []

        def once(run, refused, epoch):
            out = real(run, refused, epoch) if change else None
            kept.append(out[1] if out else refused)
            agent_._stop.set()  # as at a shutdown: the start loop asks nothing more
            return None

        agent_._new_start_op = once
        return kept

    def test_a_restart_after_the_new_id_was_kept_asks_under_it(self):
        h = agent(self, timing=fast(agent_hub_wait=60.0))
        h.http.machine_offline = 1
        kept = self._stop_at_new_id(h, change=True)
        m = ask(h, "/agent 电脑离开了一下又重启")
        wait_until(lambda: kept, what="the new id")
        refused = h.http.starts()[0]["op"]
        self.assertNotEqual(kept[0], refused)
        before = len(h.http.starts())
        h2 = h.restart()
        h2.wait_ok()
        run = running(h2, m)
        self.assertEqual(h2.http.starts()[before]["op"], kept[0], "the restart asks under the kept id")
        self.assertEqual(run["start_op"], kept[0])
        self.assertEqual(len(h2.http.sessions), 1)

    def test_a_restart_before_the_new_id_was_kept_gets_the_refusal_then_goes_on(self):
        h = agent(self, timing=fast(agent_hub_wait=60.0))
        h.http.machine_offline = 1
        kept = self._stop_at_new_id(h, change=False)
        m = ask(h, "/agent 换号前就重启了")
        wait_until(lambda: kept, what="the refusal")
        refused = kept[0]
        before = len(h.http.starts())
        h2 = h.restart()
        h2.wait_ok()
        run = running(h2, m)
        self.assertEqual(h2.http.starts()[before]["op"], refused, "asked again: the hub's kept refusal")
        self.assertNotEqual(run["start_op"], refused)
        self.assertEqual(len(h2.http.sessions), 1)

    def test_two_starts_of_one_run_change_its_id_once(self):
        # the live line's start and the restart's resume may both get the same "nothing ran": one new id, not two
        h = agent(self, timing=fast(agent_hub_wait=60.0))
        h.http.offline = 10 ** 6  # it waits, under its first id
        m = ask(h, "/agent 两个线程同时换号")
        refused = wait_until(lambda: run_of(h, m).get("start_op"), what="its operation id")
        h.app.agent._stop.set()
        time.sleep(0.2)
        run = run_of(h, m)
        first = h.app.agent._new_start_op(run, refused, None)
        self.assertNotEqual(first[1], refused)
        # the other start, with the same refusal: it leaves the run to the first (never marks it failed after)
        self.assertIsNone(h.app.agent._new_start_op(run, refused, None))
        self.assertEqual(run_of(h, m)["start_op"], first[1])
        cur = run_of(h, m)
        cur["state"] = "running"  # started meanwhile (by the first): nothing more to ask
        h.app.store.run_put(cur)
        self.assertIsNone(h.app.agent._new_start_op(cur, first[1], None))

    def test_the_new_id_gets_its_own_tries(self):
        from openmate_core.hub import HubError

        h = agent(self, timing=fast(agent_hub_wait=60.0, agent_start_tries=2))
        real = h.app.agent.hub.start_conversation
        ops = []
        script = [HubError(502, "回包丢了", "hub_unreachable"),  # A: may have reached it (a try counted)
                  HubError(503, "离线", "node_unreachable", retryable=True, delivered="none"),  # A: nothing ran
                  HubError(502, "回包丢了", "hub_unreachable")]  # B: may have reached it

        def start(*a, **kw):
            ops.append(kw.get("op_id"))
            if script:
                raise script.pop(0)
            return real(*a, **kw)

        h.app.agent.hub.start_conversation = start
        m = ask(h, "/agent 换号后的重试次数")
        run = running(h, m)
        self.assertEqual(len(ops), 4)
        self.assertEqual(ops[0], ops[1])
        self.assertNotEqual(ops[1], ops[2], "a new id after the hub's nothing ran")
        self.assertEqual(ops[2], ops[3], "B asked again, not given up on A's count")
        self.assertEqual(run["start_op"], ops[3])
        self.assertEqual(replies(h), [])

    def test_one_start_loop_per_run(self):
        h = agent(self, timing=fast(agent_hub_wait=60.0))
        h.http.offline = 10 ** 6  # the live line's start waits for the manager
        m = ask(h, "/agent 只有一个启动")
        wait_until(lambda: run_of(h, m).get("start_op"), what="its operation id")
        other = threading.Thread(target=h.app.agent._resume_start, args=(run_of(h, m),), daemon=True)
        other.start()  # as the restart's resume would, for a run made right as the app started
        other.join(2)
        self.assertFalse(other.is_alive(), "the second start of the run leaves it to the first")
        h.http.offline = 0
        running(h, m)
        self.assertEqual(len(h.http.sessions), 1)

    def test_a_resume_that_comes_after_the_start_ended_does_nothing(self):
        h = agent(self)
        m = ask(h, "/agent 恢复线程来晚了")
        snapshot = dict(run_of(h, m), state="starting")  # as a resume read it, before the live start ended
        running(h, m)
        before = len(h.http.starts())
        h.app.agent._resume_start(snapshot)
        self.assertEqual(len(h.http.starts()), before, "not asked again")
        self.assertEqual(run_of(h, m)["state"], "running")

    def test_a_request_resumed_after_its_wait_keeps_its_id(self):
        # one a restart resumes long after it was asked: never a new conversation under a new id (overnight)
        h = agent(self, timing=fast(agent_hub_wait=60.0))
        h.http.machine_offline = 1
        kept = self._stop_at_new_id(h, change=False)
        m = ask(h, "/agent 隔夜才恢复")
        wait_until(lambda: kept, what="the refusal")
        old = run_of(h, m)  # asked a day ago (run_put keeps created: the column is set here)
        h.app.store._tx(lambda db: db.execute("UPDATE runs SET created = ? WHERE id = ?", (time.time() - 86400, old["id"])))
        refused = kept[0]
        h2 = h.restart(timing=fast(agent_hub_wait=0.5))
        h2.wait_ok()
        wait_until(lambda: run_of(h2, m)["state"] == "failed", what="it ends")
        self.assertEqual({s["op"] for s in h2.http.starts()}, {refused}, "only ever the old id")
        self.assertEqual(h2.http.sessions, {})

    def test_a_stop_during_the_wait_leaves_the_old_id_for_an_overnight_resume(self):
        h = agent(self, timing=fast(agent_hub_wait=60.0, agent_hub_probe=30.0))
        h.http.machine_offline = 1
        m = ask(h, "/agent 等待中退出，隔天恢复")
        wait_until(lambda: len(h.http.starts()) == 1, what="the refusal")
        time.sleep(0.3)  # it waits before asking again
        refused = h.http.starts()[0]["op"]
        self.assertEqual(run_of(h, m)["start_op"], refused, "the new id is taken only right before the next ask")
        rid = run_of(h, m)["id"]
        h.app.store._tx(lambda db: db.execute("UPDATE runs SET created = ? WHERE id = ?", (time.time() - 86400, rid)))
        h2 = h.restart(timing=fast(agent_hub_wait=0.5))
        h2.wait_ok()
        wait_until(lambda: run_of(h2, m)["state"] == "failed", what="it ends")
        self.assertEqual({s["op"] for s in h2.http.starts()}, {refused})
        self.assertEqual(h2.http.sessions, {})

    def test_the_restart_scan_leaves_this_start_s_own_runs(self):
        # the live line may make a run before the scan of the runs a restart cut off: it is not one of them
        h = agent(self, timing=fast(agent_hub_wait=60.0))
        h.http.offline = 10 ** 6
        m = ask(h, "/agent 刚启动就来的")
        run = run_of(h, m)
        run.pop("start_op", None)  # as before its start took an id
        run.pop("start_prompt", None)
        h.app.store.run_put(run)
        h.app.agent._resume_cut_off()
        self.assertEqual(run_of(h, m)["state"], "starting", "not taken for a request a restart cut off")
        self.assertEqual([x for x in h.sent() if "扩展重启了" in x["text"]], [])

    def test_a_restart_after_the_clock_went_back_still_resumes(self):
        # the runs to resume are the ones starting when the runner was made, whatever their times say
        h = agent(self, timing=fast(agent_hub_wait=60.0))
        h.http.offline = 10 ** 6
        m = ask(h, "/agent 时钟往回调了")
        wait_until(lambda: run_of(h, m).get("start_op"), what="its operation id")
        rid = run_of(h, m)["id"]
        h.app.store._tx(lambda db: db.execute("UPDATE runs SET created = ? WHERE id = ?", (time.time() + 200, rid)))
        h.http.offline = 0
        h2 = h.restart()
        h2.wait_ok()
        running(h2, m)
        self.assertEqual(len(h2.http.sessions), 1)

    def test_no_new_id_for_a_request_dated_ahead_of_the_clock(self):
        # the clock went back since it was asked: its wait cannot be told, so it keeps its id (as main)
        h = agent(self, timing=fast(agent_hub_wait=60.0, agent_hub_probe=30.0))
        h.http.machine_offline = 1
        m = ask(h, "/agent 时钟回拨后的拒绝")
        wait_until(lambda: len(h.http.starts()) == 1, what="the refusal")
        refused = h.http.starts()[0]["op"]
        rid = run_of(h, m)["id"]
        h.app.store._tx(lambda db: db.execute("UPDATE runs SET created = ? WHERE id = ?", (time.time() + 900, rid)))
        h2 = h.restart(timing=fast(agent_hub_wait=0.5))
        h2.wait_ok()
        wait_until(lambda: run_of(h2, m)["state"] == "failed", what="it ends")
        self.assertEqual({s["op"] for s in h2.http.starts()}, {refused})
        self.assertEqual(h2.http.sessions, {})

    def test_without_the_snapshot_the_scan_goes_by_the_start_time(self):
        from openmate_core.agent import AgentRunner

        h = agent(self, timing=fast(agent_hub_wait=60.0))
        h.http.offline = 10 ** 6
        real = h.app.store.runs_in_state
        h.app.store.runs_in_state = lambda states: (_ for _ in ()).throw(RuntimeError("busy"))
        runner = AgentRunner(h.app)  # its snapshot of the starting runs could not be read
        h.app.store.runs_in_state = real
        self.assertIsNone(runner._cut_off)
        m = ask(h, "/agent 快照没读到")
        run = run_of(h, m)
        run.pop("start_op", None)
        run.pop("start_prompt", None)
        h.app.store.run_put(run)
        runner._resume_cut_off()
        self.assertEqual(run_of(h, m)["state"], "starting", "made after the runner: not one a restart cut off")

    def test_a_refusal_after_the_store_closed_changes_nothing(self):
        h = agent(self, timing=fast(agent_hub_wait=60.0))
        h.http.offline = 10 ** 6
        m = ask(h, "/agent 关掉以后才回来的拒绝")
        wait_until(lambda: run_of(h, m).get("start_op"), what="its operation id")
        run = run_of(h, m)
        h.close()
        self.assertIsNone(h.app.agent._new_start_op(run, run["start_op"], None))

    def test_it_gives_up_when_the_machine_stays_offline(self):
        h = agent(self, timing=fast(agent_hub_wait=0.3))
        h.http.machine_offline = 10 ** 6
        m = ask(h, "/agent 电脑一直离线")
        wait_until(lambda: replies(h), what="the explanation")
        time.sleep(0.2)
        texts = [x["text"] for x in replies(h)]
        self.assertEqual(len(texts), 1)
        self.assertIn("示例电脑离线了", texts[0])
        self.assertIn("请稍后重新发送", texts[0])
        self.assertEqual(h.http.sessions, {})
        self.assertEqual(run_of(h, m)["state"], "failed")

    def test_a_start_the_machine_may_have_run_is_asked_again_as_the_same_operation(self):
        h = agent(self)
        h.http.lost_starts, h.http.lost_code = 1, "node_unreachable"
        m = ask(h, "/agent 电脑那边断了")
        run = running(h, m)
        self.assertEqual(len(h.http.starts()), 2)
        self.assertEqual(len({s["op"] for s in h.http.starts()}), 1)
        self.assertNotIn("ref", run)
        self.assertEqual(len(h.http.sessions), 1)

    def test_a_restart_while_it_waits_asks_the_hub_again_under_its_operation(self):
        h = agent(self, timing=fast(agent_hub_wait=60.0))
        h.http.offline = 10 ** 6
        m = ask(h, "/agent 等着等着重启了")
        op = wait_until(lambda: run_of(h, m).get("start_op"), what="its operation id")
        time.sleep(0.2)
        h.http.offline = 0
        h2 = h.restart()
        h2.wait_ok()
        run = running(h2, m)
        self.assertEqual(run["start_op"], op)
        self.assertEqual(len(h2.http.sessions), 1)
        self.assertEqual({r.get("op") for r in h2.http.starts()}, {op}, "only ever this operation")
        self.assertEqual([x for x in h2.sent() if "重新发送" in x["text"]], [])

    def test_a_restart_after_the_hub_started_it_gets_that_conversation(self):
        h = agent(self, timing=fast(agent_hub_wait=60.0, agent_start_tries=10 ** 6))
        h.http.lost_starts = 10 ** 6  # it starts at the hub, every answer is lost on the way back
        m = ask(h, "/agent 回执丢了又重启")
        wait_until(lambda: len(h.http.starts()) >= 2, what="asked again")
        self.assertEqual(run_of(h, m)["state"], "starting")
        h.http.lost_starts = 0
        h2 = h.restart()
        h2.wait_ok()
        run = running(h2, m)
        self.assertEqual(len(h2.http.sessions), 1, "the conversation the hub started, not a second")
        self.assertIn(run["session_id"], h2.http.sessions)

    def test_a_restart_with_no_operation_still_asks_to_send_it_again(self):
        # a run an older version left starting (no operation id): never started blindly, the user is told
        h = agent(self, timing=fast(agent_hub_wait=60.0))
        h.http.offline = 10 ** 6
        m = ask(h, "/agent 旧版本留下的")
        wait_until(lambda: run_of(h, m).get("start_op"), what="its operation id")
        h.app.agent._stop.set()  # the start gives up; then it looks as an older version left it
        time.sleep(0.2)
        run = run_of(h, m)
        run.pop("start_op", None)
        run.pop("start_prompt", None)
        run["state"] = "starting"
        h.app.store.run_put(run)
        h.http.offline = 0
        h2 = h.restart()
        h2.wait_ok()
        wait_until(lambda: [x for x in h2.sent() if "重新发送" in x["text"]], what="the notice")
        self.assertEqual(h2.http.starts(), [])
        self.assertEqual(run_of(h2, m)["state"], "failed")

    def test_a_turn_without_its_end_marker_ends_when_the_session_is_done(self):
        h = agent(self)
        m = ask(h, "/agent 短问题")
        run = running(h, m)
        sid = run["session_id"]
        h.http.add(sid, "assistant", "text", "短回答")
        time.sleep(0.4)  # quiet, but still working: not the end
        self.assertEqual(replies(h), [])
        self.assertEqual(h.app.store.run_get(run["id"])["state"], "running")
        self.assertTrue(h.http.session_rows())
        h.http.finish(sid)
        wait_until(lambda: h.app.store.run_get(run["id"])["state"] == "done", what="done")
        wait_until(lambda: replies(h), what="the answer posted")
        time.sleep(0.2)
        self.assertEqual([x["text"] for x in replies(h)], ["短回答\n\n" + SUFFIX])
        asked = len(h.http.session_rows())
        time.sleep(0.3)
        self.assertEqual(len(h.http.session_rows()), asked, "a finished run is not polled any more")

    def test_a_run_that_replied_through_the_tool_ends_by_its_status(self):
        h = agent(self)
        m = ask(h, "/agent 只用工具回复")
        run = running(h, m)
        h.tool("agent_reply", request=run["id"], text="工具回复")
        h.http.finish(run["session_id"])  # nothing new in the transcript that the poll would read as an answer
        wait_until(lambda: h.app.store.run_get(run["id"])["state"] == "done", what="done")
        time.sleep(0.2)
        self.assertEqual([x["text"] for x in replies(h)], ["工具回复\n\n" + SUFFIX])

    def test_an_offline_session_ends_a_run_that_replied_but_not_one_that_did_not(self):
        h = agent(self)
        m1 = ask(h, "/agent 回复过")
        r1 = running(h, m1)
        m2 = ask(h, "/agent 还没回复", chat="chat_example_dm_a")
        r2 = running(h, m2)
        h.tool("agent_reply", request=r1["id"], text="回复了")
        h.http.add(r2["session_id"], "assistant", "text", "写到一半")
        with h.http.lock:
            for r in (r1, r2):
                h.http.sessions[r["session_id"]]["offline"] = True
        wait_until(lambda: h.app.store.run_get(r1["id"])["state"] == "done", what="the replied run done")
        time.sleep(0.4)
        self.assertEqual(h.app.store.run_get(r2["id"])["state"], "running", "a dropped machine's half answer waits")
        self.assertEqual([x["text"] for x in replies(h)], ["回复了\n\n" + SUFFIX])
        with h.http.lock:
            h.http.sessions[r2["session_id"]]["offline"] = False
            h.http.sessions[r2["session_id"]]["working"] = False
        wait_until(lambda: h.app.store.run_get(r2["id"])["state"] == "done", what="the second run done")
        h.tool("agent_reply", request=r1["id"], text="后来的进展")  # a finished run still takes replies
        self.assertEqual(len(replies(h)), 3)

    def test_a_done_status_before_the_answer_is_not_the_end(self):
        h = agent(self)
        m = ask(h, "/agent 还没开口")
        run = running(h, m)
        h.http.finish(run["session_id"])  # the list says done, but nothing answered our request yet
        time.sleep(0.4)
        self.assertEqual(h.app.store.run_get(run["id"])["state"], "running")
        self.assertEqual(h.http.session_rows(), [])

    def test_a_replied_turn_ends_by_its_status_with_nothing_more_posted(self):
        h = agent(self)
        m = ask(h, "/agent 用工具回复")
        run = running(h, m)
        sid = run["session_id"]
        h.tool("agent_reply", request=run["id"], text="工具里的回复")
        h.http.add(sid, "assistant", "tool_use", tool_name="matefeishu_agent_reply")
        h.http.add(sid, "assistant", "text", "已经回复到飞书了")
        h.http.finish(sid)
        wait_until(lambda: h.app.store.run_get(run["id"])["state"] == "done", what="done")
        time.sleep(0.2)
        self.assertEqual([x["text"] for x in replies(h)], ["工具里的回复\n\n" + SUFFIX])

    def test_stop_while_it_waits_for_the_hub(self):
        h = agent(self)
        h.http.offline = 10 ** 6
        m = ask(h, "/agent 等网络")
        run_of(h, m)
        ask(h, "/stop", root_id=m.id)
        wait_until(lambda: replies(h), what="stopped")
        self.assertEqual(replies(h)[0]["text"], "已停止\n\n" + SUFFIX)
        h.http.offline = 0
        time.sleep(0.3)
        self.assertEqual(h.http.starts(), [])
        self.assertEqual(run_of(h, m)["state"], "stopped")
        self.assertEqual(len(replies(h)), 1)

    def test_stop_that_cannot_reach_the_hub_still_stops_the_replies(self):
        h = agent(self)
        m = ask(h, "/agent 停不下来")
        run = running(h, m)
        h.http.offline = 10 ** 6
        ask(h, "/stop", root_id=m.id)
        wait_until(lambda: replies(h), what="the answer")
        self.assertIn("已停止回复", replies(h)[0]["text"])
        with self.assertRaises(HubSideError):
            h.tool("agent_reply", request=run["id"], text="还想回")

    def test_the_hubs_refusals_are_not_retried_and_words_come_in_chinese(self):
        h = agent(self)
        h.http.start_fail = 400
        ask(h, "/agent 你好")
        wait_until(lambda: replies(h), what="the explanation")
        self.assertEqual(len(h.http.starts()), 1)
        self.assertEqual(h.http.starts()[0].get("lang"), "zh-CN")

    def test_errors_tell_what_may_have_been_sent(self):
        import socket
        from openmate_core.hub import HubAPI, HubError

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()  # nothing listens there
        with self.assertRaises(HubError) as e:
            HubAPI("http://127.0.0.1:%d" % port, "t").health(timeout=2)
        self.assertTrue(e.exception.unsent)
        self.assertTrue(e.exception.unreachable)
        for code, unsent, unreachable in (("manager_offline", True, True), ("hub_unreachable", False, True),
                                          ("network", False, True), ("", False, False), ("bad_request", False, False)):
            err = HubError(502, "x", code)
            self.assertEqual((err.unsent, err.unreachable, err.transient), (unsent, unreachable, unreachable), code)
        # the hub's own answers about the machine
        offline = HubError(503, "离线", "node_unreachable", retryable=True, delivered="none")
        self.assertEqual((offline.unsent, offline.unreachable, offline.transient), (True, False, True))
        broke = HubError(502, "断了", "node_unreachable")
        self.assertEqual((broke.unsent, broke.transient), (False, True))
        away = HubError(503, "电脑的 manager 没连上", "assistant_offline")  # the docs: no receipt, try the same again
        self.assertEqual((away.unsent, away.transient), (True, True))
        missing = HubError(409, "没装 Claude Code", "claude_missing", retryable=False, delivered="none")
        self.assertEqual((missing.unsent, missing.transient), (False, False))


class CutOffAnswerTest(unittest.TestCase):
    def test_an_answer_cut_off_midway_is_a_network_failure(self):
        import http.server
        import threading

        from openmate_core.hub import HubAPI, HubError

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.write(b'{"session":')  # and the connection ends

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        with self.assertRaises(HubError) as e:
            HubAPI("http://127.0.0.1:%d" % srv.server_address[1], "t").request("POST", "/x", {"a": 1})
        self.assertTrue(e.exception.transient)
        self.assertFalse(e.exception.unsent, "it may have run")


    def test_an_error_answer_cut_off_midway_may_have_run(self):
        import http.server
        import threading

        from openmate_core.hub import HubAPI, HubError

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(502)
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.write(b'{"code":"assistant_in')

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        with self.assertRaises(HubError) as e:
            HubAPI("http://127.0.0.1:%d" % srv.server_address[1], "t").request("POST", "/x", {"a": 1})
        self.assertTrue(e.exception.transient)
        self.assertFalse(e.exception.unsent)


class OffHereTest(unittest.TestCase):
    def test_the_reply_tool_sends_nothing_once_off_here(self):
        h = agent(self)
        m = ask(h, "/agent 关了以后")
        run = running(h, m)
        h.app.agent.enabled = False  # the connector off, or now a member hub
        with self.assertRaises(HubSideError) as e:
            h.tool("agent_reply", request=run["id"], text="答案")
        self.assertIn("不再以你的身份回复", e.exception.message)
        self.assertEqual(replies(h), [])

    def test_a_start_too_old_to_replay_is_not_asked_again(self):
        h = agent(self, timing=fast(agent_hub_wait=60.0))
        h.http.offline = 10 ** 6
        m = ask(h, "/agent 很久以前的")
        wait_until(lambda: run_of(h, m).get("start_op"), what="its operation id")
        h.app.agent._stop.set()
        time.sleep(0.2)
        run = run_of(h, m)
        run["state"] = "starting"
        h.app.store.run_put(run)
        old = time.time() - 26 * 86400
        h.app.store._tx(lambda db: db.execute("UPDATE runs SET created = ? WHERE id = ?", (old, run["id"])))
        h.http.offline = 0
        h2 = h.restart()
        h2.wait_ok()
        wait_until(lambda: [x for x in h2.sent() if "Persona 里看看" in x["text"]], what="the notice")
        self.assertEqual(h2.http.starts(), [], "the hub may have forgotten its receipt: never sent blind")


class SettingsAndFailuresTest(unittest.TestCase):
    def test_suffix_settings(self):
        from openmate_core.agent import parse_suffix

        self.assertEqual(parse_suffix(None), "", "no end mark unless one is set")
        self.assertEqual(parse_suffix(""), "")
        self.assertEqual(parse_suffix(" "), "")
        self.assertEqual(parse_suffix(SUFFIX), SUFFIX)
        self.assertEqual(parse_suffix("\\n—— AI"), "\n—— AI")
        h = agent(self, agent_reply_suffix=" ")
        m = ask(h, "/agent 不要标记")
        run = running(h, m)
        h.tool("agent_reply", request=run["id"], text="纯回复")
        self.assertEqual(h.sent()[-1]["text"], "纯回复")
        # without a mark the Agent's reply must still not count as a request
        time.sleep(0.2)
        self.assertEqual(len(h.http.starts()), 1)

    def test_newline_suffix(self):
        h = agent(self, agent_reply_suffix="\\n—— AI 回复")
        m = ask(h, "/agent 换行标记")
        run = running(h, m)
        h.tool("agent_reply", request=run["id"], text="内容")
        self.assertEqual(h.sent()[-1]["text"], "内容\n—— AI 回复")

    def test_without_a_hub_token(self):
        h = Harness(self, config={"agent": True}, hub_url="")
        h.sign_in()
        m = ask(h, "/agent 你好")
        said = wait_until(lambda: [x for x in h.sent() if "Persona hub" in x["text"]], what="the explanation")
        self.assertTrue(said[0]["text"].endswith("\n\n> 🤖 AI 回复"), "the mark, without a model: no session yet")
        self.assertEqual(run_of(h, m)["state"], "failed")

    def test_start_refused(self):
        h = agent(self)
        h.http.start_fail = 400
        ask(h, "/agent 你好")
        wait_until(lambda: replies(h), what="the explanation")
        self.assertIn("没能启动 Agent", replies(h)[0]["text"])

    def test_runs_are_kept_three_days_and_at_most_200(self):
        h = agent(self)
        h.app.agent.stop()  # these rows are made up: the poll loop must not time the running one out meanwhile
        store = h.app.store
        now = time.time()
        for i in range(210):
            store.run_put({"id": "r%03d" % i, "chat_id": "c", "request_id": "m%d" % i, "thread_key": "m%d" % i,
                           "state": "done", "created": now - (5 * 86400 if i < 5 else i)})
        store.run_put({"id": "active", "chat_id": "c", "request_id": "x", "thread_key": "x", "state": "running",
                       "created": now - 4 * 86400})
        store.runs_prune(now - 3 * 86400, 200)
        self.assertIsNone(store.run_get("r000"))
        self.assertIsNotNone(store.run_get("active"), "a running request is never pruned")
        self.assertLessEqual(store.run_count(), 201)
        self.assertIsNotNone(store.run_get("r005"))


class DefaultsTest(unittest.TestCase):
    """Today's defaults: a 🤔 on the request when it is taken, and the answer under a header naming the model."""

    def make(self, **cfg):
        config = {"agent": True}
        config.update(cfg)
        h = Harness(self, config=config)
        h.sign_in()
        wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
        return h

    def test_taken_with_a_reaction_and_answered_under_the_models_name(self):
        h = self.make()
        msg = ask(h, "/agent 帮我总结一下")
        wait_until(lambda: h.world.reactions, what="the reaction")
        self.assertEqual(h.world.reactions, [("chat_example_team", msg.id, "THINKING")])
        run = running(h, msg)
        h.tool("agent_reply", request=run["id"], text="> 🤖 AI 回复 · 别的模型\n总结如下：一切正常。")
        sent = h.sent()[-1]
        self.assertEqual(sent["text"], "总结如下：一切正常。\n\n> 🤖 AI 回复 · Opus 5.5", "its own mark is not doubled")
        self.assertEqual((sent["reply_to"], sent["root"]), (msg.id, msg.id), "quoting the request")
        self.assertEqual(h.world.reactions, [("chat_example_team", msg.id, "THINKING")], "once")
        self.assertTrue(h.app.agent.is_agent_text(sent["text"]))
        # a reply starting with the header never starts another request
        h.world.push("chat_example_team", "me", "> 🤖 AI 回复 · Claude Opus 5.5\n/agent 再来一次")
        time.sleep(0.3)
        self.assertEqual(len(h.http.starts()), 1)

    def test_a_model_listed_late_is_waited_for_and_never_kept_empty(self):
        h = self.make()
        msg = ask(h, "/agent 新会话")
        run = running(h, msg)
        h.http.model_late = 2  # the hub lists the new session twice before it knows its model
        h.tool("agent_reply", request=run["id"], text="第一条")
        self.assertEqual(h.sent()[-1]["text"], "第一条\n\n> 🤖 AI 回复 · Opus 5.5")
        h2 = self.make(agent_reply_suffix="")
        msg = ask(h2, "/agent 一直不知道模型")
        run = running(h2, msg)
        h2.http.model_late = 1000
        h2.tool("agent_reply", request=run["id"], text="先发出去")
        self.assertEqual(h2.sent()[-1]["text"], "先发出去\n\n> 🤖 AI 回复", "after agent_model_wait, without it")
        self.assertFalse(h2.app.store.run_get(run["id"]).get("model"), "not kept empty")
        h2.http.model_late = 0
        h2.tool("agent_reply", request=run["id"], text="第二条")
        self.assertEqual(h2.sent()[-1]["text"], "第二条\n\n> 🤖 AI 回复 · Opus 5.5", "asked again")

    def test_settings_and_a_refused_reaction(self):
        h = self.make(agent_ack_reaction="OnIt", agent_reply_header="—— {model}（{effort}）", agent_reply_suffix=SUFFIX,
                      agent_mark_effort=True)
        h.world.react_fail = True
        msg = ask(h, "/agent 写个周报")
        run = running(h, msg)  # a refused reaction never holds the request
        h.tool("agent_reply", request=run["id"], text="周报写好了")
        self.assertEqual(h.sent()[-1]["text"], "周报写好了\n\n—— Opus 5.5（xhigh）\n\n" + SUFFIX)
        self.assertEqual(h.world.reactions, [])
        # without agent_mark_effort a mark names the model alone
        for header, want in (("> 🤖 AI 回复 · {model} · {effort}", "> 🤖 AI 回复 · Opus 5.5"),):
            h3 = self.make(agent_reply_header=header)
            run = running(h3, ask(h3, "/agent 只写模型"))
            h3.tool("agent_reply", request=run["id"], text="好了")
            self.assertEqual(h3.sent()[-1]["text"], "好了\n\n" + want, header)
        h4 = self.make(agent_mark_effort=True)
        run = running(h4, ask(h4, "/agent 也写强度"))
        h4.tool("agent_reply", request=run["id"], text="好了")
        self.assertEqual(h4.sent()[-1]["text"], "好了\n\n> 🤖 AI 回复 · Opus 5.5 · xhigh")
        h2 = self.make(agent_ack_reaction="", agent_reply_header="")
        msg = ask(h2, "/agent 不要表情")
        run = running(h2, msg)
        h2.tool("agent_reply", request=run["id"], text="好")
        self.assertEqual((h2.sent()[-1]["text"], h2.world.reactions), ("好", []))

    def test_a_mark_of_plain_words_is_read_as_a_whole_line(self):
        # "claude code · Opus 5.5" (the user's, 2026-11-25): their own lines that merely start with those words are theirs
        h = self.make(agent_reply_header="claude code · {model} · {effort}")
        run = running(h, ask(h, "/agent 看下"))
        h.tool("agent_reply", request=run["id"], text="claude code · Opus 5.5\n答案")
        sent = h.sent()[-1]["text"]
        self.assertEqual(sent, "答案\n\nclaude code · Opus 5.5", "the mark the Agent copied is not doubled")
        self.assertTrue(h.app.agent.is_agent_text(sent))
        h.tool("agent_reply", request=run["id"], text="配置改好了\nclaude code 的设置在 settings.json")
        self.assertEqual(h.sent()[-1]["text"], "配置改好了\nclaude code 的设置在 settings.json\n\nclaude code · Opus 5.5",
                         "an answer's own last line is kept")
        a = h.app.agent
        self.assertFalse(a.is_agent_text("/agent 帮我看下\nclaude code 报错了"))
        self.assertFalse(a.is_agent_text("claude code 怎么配置？"))
        self.assertTrue(a.is_agent_text("> 🤖 AI 回复 · 别的模型\n答案"), "built-in marks as always")
        n = len(h.http.starts())
        running(h, ask(h, "/agent 帮我看下\nclaude code 报错了"))
        self.assertEqual(len(h.http.starts()), n + 1, "a request of the user's, not an AI's")
        self.assertFalse(a.is_agent_text("/agent 帮我安装下面这个工具\nclaude code"), "the words alone are the user's")
        self.assertFalse(a.is_agent_text("/agent 看看这条报错\ncodex · 登录失败"), "a person's line, not a mark")
        self.assertFalse(a.is_agent_text("/agent 看看这条报错\nclaude code · 登录失败"))
        self.assertFalse(a.is_agent_text("/agent 帮我排查\ncodex · HTTP 401"), "an error, not a model")
        self.assertTrue(a.is_agent_text("答案\ncodex · o3"), "o3 is a model")
        self.assertFalse(a.is_agent_text("/agent 帮我排查\ncodex · o3 returned HTTP 401"), "an error's words")
        from openmate_core.agent import mark_model
        for m in ("qwen3-32b", "o4-mini", "gpt-6-2-astra", "claude-sonnet-5-5[1m]", "deepseek-v4-1", "gemini-4-neon"):
            self.assertTrue(a.is_agent_text("答案\ncodex · " + mark_model(m)), m)
        h.tool("agent_reply", request=run["id"], text="装好的工具：\ncodex · 已安装\ncursor · 未安装")
        self.assertEqual(h.sent()[-1]["text"], "装好的工具：\ncodex · 已安装\ncursor · 未安装\n\nclaude code · Opus 5.5")
        h.tool("agent_reply", request=run["id"], text="排查结果：\ncodex · HTTP 401")
        self.assertEqual(h.sent()[-1]["text"], "排查结果：\ncodex · HTTP 401\n\nclaude code · Opus 5.5")
        h.tool("agent_reply", request=run["id"], text="claude code")  # asked for the tool's name alone
        self.assertEqual(h.sent()[-1]["text"], "claude code\n\nclaude code · Opus 5.5")
        self.assertTrue(a.is_agent_text("答案\n\ncodex · GPT-6.2 Astra"), "an Agent's name before the model is an AI's")

    def test_the_mark_names_the_agent_that_answers(self):
        # {agent}: the Agent the session runs on, as the hub lists it (the user's, 2026-11-25)
        h = self.make(agent_reply_header="{agent} · {model} · {effort}")
        run = running(h, ask(h, "/agent 你好"))
        h.tool("agent_reply", request=run["id"], text="你好")
        sent = h.sent()[-1]["text"]
        self.assertEqual(sent, "你好\n\nclaude code · Opus 5.5")
        self.assertTrue(h.app.agent.is_agent_text(sent), "its own echo is an AI's")
        h.tool("agent_reply", request=run["id"], text="claude code · Opus 5.5\n再补一句")
        self.assertEqual(h.sent()[-1]["text"], "再补一句\n\nclaude code · Opus 5.5", "a copied mark is not doubled")
        sid = run["session_id"]
        run2 = running(h, ask(h, "/agent 换个 Agent"))
        h.http.sessions[run2["session_id"]].update(agent="codex", model="gpt-6-2-astra")
        h.tool("agent_reply", request=run2["id"], text="好的")
        self.assertEqual(h.sent()[-1]["text"], "好的\n\ncodex · GPT-6.2 Astra")
        run3 = running(h, ask(h, "/agent 不认识的 Agent"))
        h.http.sessions[run3["session_id"]].update(agent="someday")
        h.tool("agent_reply", request=run3["id"], text="好的")
        sent = h.sent()[-1]["text"]
        self.assertEqual(sent, "好的\n\n🤖 AI · Opus 5.5", "an Agent nobody knows: an AI's mark all the same")
        self.assertTrue(h.app.agent.is_agent_text(sent))
        # a run from before {agent} (its model kept, no agent): asked once
        cur = h.app.store.run_get(run["id"])
        cur.pop("agent", None)
        h.app.store.run_put(cur)
        h.http.sessions[sid].update(agent="codex")
        h.tool("agent_reply", request=run["id"], text="又一句")
        self.assertEqual(h.sent()[-1]["text"], "又一句\n\ncodex · Opus 5.5")

    def test_an_agent_or_a_model_said_late(self):
        h = self.make(agent_reply_header="{agent} · {model} · {effort}", agent_reply_suffix="")
        run = running(h, ask(h, "/agent 你好"))
        h.http.sessions[run["session_id"]].update(agent="")  # the hub has not said the Agent yet
        h.tool("agent_reply", request=run["id"], text="第一句")
        self.assertEqual(h.sent()[-1]["text"], "第一句\n\n🤖 AI · Opus 5.5")
        h.http.sessions[run["session_id"]].update(agent="claude")  # now it has: asked again, not kept empty
        h.tool("agent_reply", request=run["id"], text="第二句")
        self.assertEqual(h.sent()[-1]["text"], "第二句\n\nclaude code · Opus 5.5")
        h2 = self.make(agent_reply_header="{agent} · {model} · {effort}", agent_reply_suffix="")
        run = running(h2, ask(h2, "/agent 模型一直不来"))
        h2.http.model_late = 1000
        h2.tool("agent_reply", request=run["id"], text="先发出去")
        self.assertEqual(h2.sent()[-1]["text"], "先发出去\n\n🤖 AI", "no model: a built-in mark alone, not the Agent's name")
        self.assertEqual(h2.app.store.run_get(run["id"]).get("agent"), "claude", "the Agent kept on its own")
        with self.assertRaises(Exception) as c:
            h2.tool("agent_reply", request=run["id"], text="🤖 AI")
        self.assertIn("只有 AI 标记", str(c.exception))
        self.assertFalse(h2.app.agent.is_agent_text("/agent 请排查这条报错\npi · GPT-6.2 HTTP 401"))
        self.assertFalse(h2.app.agent.is_agent_text("/agent 请排查这条报错\ncodex · GPT-6.2 HTTP 401"))
        self.assertFalse(h2.app.agent.is_agent_text("/agent 帮我排查\nopencode · MiniMax M2.5 401 Unauthorized"))
        self.assertTrue(h2.app.agent.is_agent_text("答案\nopencode · Qwen3 32B AWQ"), "a model's own suffix")
        self.assertTrue(h2.app.agent.is_agent_text("答案\nopencode · MiniMax M2.5"))

    def test_feishu_marks_of_punctuation_or_a_leading_model(self):
        h = self.make(agent_reply_header="—— {model}（{effort}）", agent_mark_effort=True)
        run = running(h, ask(h, "/agent 写个周报"))
        h.tool("agent_reply", request=run["id"], text="周报写好了\n\n—— Opus 5.5（xhigh）")
        self.assertEqual(h.sent()[-1]["text"], "周报写好了\n\n—— Opus 5.5（xhigh）", "the copied mark is not doubled")
        with self.assertRaises(Exception) as c:
            h.tool("agent_reply", request=run["id"], text="—— Opus 5.5（xhigh）")
        self.assertIn("只有 AI 标记", str(c.exception))
        h2 = self.make(agent_reply_header="{model} 助手")
        a = h2.app.agent
        self.assertFalse(a.is_agent_text("/agent 推荐一个适合编程的 AI 助手"))
        run = running(h2, ask(h2, "/agent 推荐一个适合编程的 AI 助手"))
        h2.tool("agent_reply", request=run["id"], text="这是一款开源的 AI 助手")
        self.assertEqual(h2.sent()[-1]["text"], "这是一款开源的 AI 助手\n\nOpus 5.5 助手")
        h3 = self.make(auto_reply_header="{model}")  # the 自动代答's mark is no /agent answer's
        run = running(h3, ask(h3, "/agent 你是什么模型？只回模型名"))
        h3.tool("agent_reply", request=run["id"], text="Opus 5.5")
        self.assertTrue(h3.sent()[-1]["text"].startswith("Opus 5.5\n\n"))

    def test_the_mark_on_top_on_request_and_marks_at_either_end_are_recognised(self):
        h = self.make(agent_mark_position="top")
        msg = ask(h, "/agent 放第一行")
        run = running(h, msg)
        h.tool("agent_reply", request=run["id"], text="答案在第二行\n\n> 🤖 AI 回复 · 别的模型")
        self.assertEqual(h.sent()[-1]["text"], "> 🤖 AI 回复 · Opus 5.5\n答案在第二行", "its own mark at the end is not doubled")
        h2 = self.make(agent_mark_position="sideways")
        self.assertEqual(h2.app.agent.mark_position, "end", "an unknown value is the default")
        agent_ = h2.app.agent
        for text in ("答案\n\n> 🤖 AI 回复 · Claude Opus 5.5", "> 🤖 AI 回复 · Claude Opus 5.5\n答案", "答案\n🤖 AI 代答 · 某模型\n"):
            self.assertTrue(agent_.is_agent_text(text), text)
        self.assertFalse(agent_.is_agent_text("我觉得 🤖 AI 回复 这种标记挺好\n就这样"), "a mark mid-line is not one")
        # another AI's reply with its mark at the end never starts a request here
        h2.world.push("chat_example_team", "me", "/agent 再帮我看看\n\n> 🤖 AI 回复 · 别人的模型")
        time.sleep(0.3)
        self.assertEqual(len(h2.http.starts()), 0)

    def test_labels(self):
        from openmate_core.agent import fill_header, model_label

        self.assertEqual([model_label(m) for m in ("claude-opus-5-5", "gpt-6-1-sol", "gpt-6-astra", "claude-sonnet-5-5[1m]",
                                                    "claude-haiku-5-5-20261001", "gemini-3-pro", "")],
                         ["Claude Opus 5.5", "GPT-6.1 Sol", "GPT-6 Astra", "Claude Sonnet 5.5 1M", "Claude Haiku 5.5",
                          "Gemini 3 Pro", ""])
        self.assertEqual(fill_header("> 🤖 AI 回复 · {model} · {effort}", "", ""), "> 🤖 AI 回复", "unknown parts go")
        self.assertEqual([fill_header("· {model}", m, "") for m in ("claude-opus-5-5", "gemini-4-neon", "gpt-6-1-sol")],
                         ["· Opus 5.5", "· Gemini 4 Neon", "· GPT-6.1 Sol"], "a Claude model by its own name in a mark")
        self.assertEqual(fill_header("> 🤖 AI 回复 · {model} · {effort}", "gpt-6-astra", ""), "> 🤖 AI 回复 · GPT-6 Astra")


GiB = 1 << 30


def reading_of(cores, load, free_gib=32.0, total_gib=64.0, fresh=True):
    """A computer's metrics as the hub gives them (GET /api/v1/machines/{id}/metrics)."""
    total = int(total_gib * GiB)
    latest = {"t": time.time() if fresh else time.time() - 3600, "load": load, "mem_used": total - int(free_gib * GiB),
              "mem_total": total}
    return {"online": True, "system": {"cores": cores}, "latest": latest}


def computers(h, home, **machines):
    """The hub's computers: home is the assistant's; each machine is (online, metrics or None)."""
    h.http.assistant_mid = home
    h.http.machine_list = [{"id": mid, "online": online, "claude_version": "2.1.330"} for mid, (online, _) in machines.items()]
    h.http.metrics = {mid: m for mid, (_, m) in machines.items() if m is not None}


def placement_asks(h):
    with h.http.lock:
        return [r for r in h.http.requests if r["method"] == "GET" and r["path"] == "/api/v1/machines"]


class LoadPlacementTest(unittest.TestCase):
    """No computer set: a new /agent starts on the assistant's computer unless it is busy, then on the least loaded idle
    other one; a follow-up stays with its conversation, and a new conversation in a thread goes back to its computer."""

    @property
    def BUSY(self):  # 1.11 per core (over the 1.0 line), read just now
        return reading_of(18, 20.0)

    def test_an_idle_home_starts_as_before(self):
        h = agent(self)
        computers(h, "mac-example", **{"mac-example": (True, reading_of(18, 3.0)), "devbox": (True, reading_of(64, 0.5))})
        m = ask(h, "/agent 平常的一件事")
        body = started(h)["body"]
        self.assertNotIn("machine_id", body, "home is not busy: the start names no computer, as before")
        self.assertEqual(running(h, m)["machine_id"], "mac-example")
        self.assertEqual(len(placement_asks(h)), 1)

    def test_a_busy_home_goes_to_the_least_loaded_idle_computer(self):
        h = agent(self)
        computers(h, "mac-example", **{
            "mac-example": (True, self.BUSY),
            "devbox-busy": (True, reading_of(64, 70.0)),          # 1.09 per core: busy too
            "devbox-short": (True, reading_of(64, 1.0, free_gib=1.0)),  # idle, but too little memory
            "devbox-away": (False, reading_of(64, 0.1)),          # offline
            "devbox-quiet": (True, reading_of(64, 0.1, fresh=False)),  # no fresh reading
            "devbox-small": (True, reading_of(8, 2.0)),           # 0.25 per core
            "devbox-big": (True, reading_of(64, 6.4)),            # 0.10 per core: the least loaded
        })
        m = ask(h, "/agent 一件重活")
        self.assertEqual(started(h)["body"].get("machine_id"), "devbox-big")
        run = running(h, m)
        self.assertEqual((run["machine_id"], run["start_machine"]), ("devbox-big", "devbox-big"))

    def test_too_little_memory_at_home_is_busy(self):
        h = agent(self)
        computers(h, "mac-example", **{"mac-example": (True, reading_of(18, 1.0, free_gib=1.5)),
                                       "devbox": (True, reading_of(64, 0.5))})
        ask(h, "/agent 内存不够的时候")
        self.assertEqual(started(h)["body"].get("machine_id"), "devbox")

    def test_a_home_at_one_per_core_or_under_stays(self):
        # the line is Persona's own (assistant.admission load_per_core 1.0): 0.89 or exactly 1.0 per core is not busy
        for load in (16.0, 18.0):
            h = agent(self)
            computers(h, "mac-example", **{"mac-example": (True, reading_of(18, load)),
                                           "devbox": (True, reading_of(64, 0.5))})
            ask(h, "/agent 不算忙")
            self.assertNotIn("machine_id", started(h)["body"], "%.2f per core is not over 1.0" % (load / 18))

    def test_eight_sessions_working_at_home_is_busy(self):
        # the third line, Persona's max_claude_procs: a quiet load does not hide eight sessions working a turn
        h = agent(self)
        computers(h, "mac-example", **{"mac-example": (True, reading_of(18, 3.0)), "devbox": (True, reading_of(64, 0.5))})
        h.http.working = {"mac-example": 8}
        ask(h, "/agent 会话太多")
        self.assertEqual(started(h)["body"].get("machine_id"), "devbox")
        h2 = agent(self)
        computers(h2, "mac-example", **{"mac-example": (True, reading_of(18, 3.0)), "devbox": (True, reading_of(64, 0.5))})
        h2.http.working = {"mac-example": 7, "devbox": 1}  # sessions done but unseen are listed too: never counted
        ask(h2, "/agent 还行")
        self.assertNotIn("machine_id", started(h2)["body"], "seven working is under the line")

    def test_a_computer_with_eight_sessions_working_is_not_chosen(self):
        h = agent(self)
        computers(h, "mac-example", **{"mac-example": (True, self.BUSY),
                                       "devbox-big": (True, reading_of(64, 6.4)),     # the least loaded, but full
                                       "devbox-small": (True, reading_of(8, 2.0))})
        h.http.working = {"devbox-big": 8}
        ask(h, "/agent 换一台")
        self.assertEqual(started(h)["body"].get("machine_id"), "devbox-small")

    def test_without_the_session_list_the_load_lines_alone(self):
        # an older hub or no agents.read: the working count is unread, never taken as busy
        h = agent(self)
        computers(h, "mac-example", **{"mac-example": (True, reading_of(18, 3.0)), "devbox": (True, reading_of(64, 0.5))})
        h.http.working = None
        ask(h, "/agent 读不到会话")
        self.assertNotIn("machine_id", started(h)["body"])
        h2 = agent(self)
        computers(h2, "mac-example", **{"mac-example": (True, self.BUSY), "devbox": (True, reading_of(64, 0.5))})
        h2.http.working = None
        ask(h2, "/agent 读不到会话但很忙")
        self.assertEqual(started(h2)["body"].get("machine_id"), "devbox")

    def test_home_stays_when_every_other_computer_is_busy_or_unread(self):
        h = agent(self)
        computers(h, "mac-example", **{"mac-example": (True, self.BUSY), "devbox": (True, reading_of(8, 9.0)),
                                       "devbox-2": (True, None)})
        ask(h, "/agent 都忙")
        self.assertNotIn("machine_id", started(h)["body"])

    def test_without_readings_or_the_permission_it_starts_as_before(self):
        h = agent(self)  # no placement routes: an older hub, or machines.read not granted (404)
        ask(h, "/agent 读不到")
        self.assertNotIn("machine_id", started(h)["body"])
        h2 = agent(self)
        computers(h2, "mac-example", **{"mac-example": (True, None), "devbox": (True, reading_of(64, 0.5))})
        ask(h2, "/agent 家里没有读数")
        self.assertNotIn("machine_id", started(h2)["body"], "home without a reading is never left")

    def test_a_follow_up_stays_with_its_conversation(self):
        h = agent(self)
        computers(h, "mac-example", **{"mac-example": (True, self.BUSY), "devbox": (True, reading_of(64, 0.5))})
        first = ask(h, "/agent 第一个问题")
        run1 = running(h, first)
        self.assertEqual(run1["machine_id"], "devbox")
        sid = run1["session_id"]
        h.http.add(sid, "assistant", "text", "第一个回答")
        h.http.end_turn(sid)
        wait_until(lambda: replies(h), what="first answer")
        computers(h, "mac-example", **{"mac-example": (True, reading_of(18, 1.0)), "devbox": (True, self.BUSY)})
        ask(h, "/agent 接着问", root_id=first.id)
        wait_until(lambda: h.http.posts("/api/v1/sessions/devbox/%s/messages" % sid), what="continued where it runs")
        self.assertEqual(len(h.http.starts()), 1)
        self.assertEqual(len(placement_asks(h)), 1, "a follow-up is never placed")

    def test_a_new_conversation_in_a_thread_goes_back_to_its_computer(self):
        h = agent(self, timing=fast(agent_keep=0.4))
        computers(h, "mac-example", **{"mac-example": (True, self.BUSY), "devbox": (True, reading_of(64, 0.5))})
        first = ask(h, "/agent 第一个问题")
        run1 = running(h, first)
        h.http.add(run1["session_id"], "assistant", "text", "第一个回答")
        h.http.end_turn(run1["session_id"])
        wait_until(lambda: replies(h), what="first answer")
        time.sleep(0.5)  # past agent_keep: the thread's next request opens a new conversation
        computers(h, "mac-example", **{"mac-example": (True, reading_of(18, 1.0)), "devbox": (True, reading_of(64, 0.5))})
        ask(h, "/agent 过了一阵又来问", root_id=first.id)
        second = started(h, 2)["body"]
        self.assertEqual(second.get("machine_id"), "devbox", "the thread's files and context are on that computer")
        # an offline thread computer: placed anew (home is idle now)
        h.http.machine_list = [{"id": "mac-example", "online": True}, {"id": "devbox", "online": False}]
        h.http.metrics["devbox"] = dict(reading_of(64, 0.5), online=False)
        time.sleep(0.5)
        ask(h, "/agent 那台电脑关了", root_id=first.id)
        self.assertNotIn("machine_id", started(h, 3)["body"])

    def test_two_requests_of_a_thread_start_on_one_computer(self):
        h = agent(self)
        computers(h, "mac-example", **{"mac-example": (True, self.BUSY), "devbox": (True, reading_of(64, 0.5)),
                                       "devbox-2": (True, reading_of(64, 3.0))})
        h.http.start_pending = 50  # the first start's answer keeps saying "under way"
        first = ask(h, "/agent 第一个")
        wait_until(lambda: h.http.starts(), what="the first start asked")
        computers(h, "mac-example", **{"mac-example": (True, self.BUSY), "devbox": (True, reading_of(64, 30.0)),
                                       "devbox-2": (True, reading_of(64, 0.5))})
        ask(h, "/agent 第二个", root_id=first.id, parent_id=first.id)
        wait_until(lambda: len({r["op"] for r in h.http.starts()}) >= 2, what="the second start asked")
        self.assertEqual({r["body"].get("machine_id") for r in h.http.starts()}, {"devbox"},
                         "the thread's computer while its first is still starting")

    def test_a_thread_does_not_follow_its_starting_request_to_an_away_computer(self):
        h = agent(self)
        computers(h, "mac-example", **{"mac-example": (True, self.BUSY), "devbox": (True, reading_of(64, 0.5))})
        h.http.start_pending = 50  # the first start's answer keeps saying "under way"
        first = ask(h, "/agent 第一个")
        wait_until(lambda: h.http.starts(), what="the first start asked")
        computers(h, "mac-example", **{"mac-example": (True, reading_of(18, 1.0)), "devbox": (False, None)})
        ask(h, "/agent 第二个", root_id=first.id, parent_id=first.id)
        wait_until(lambda: len({r["op"] for r in h.http.starts()}) >= 2, what="the second start asked")
        second = [r for r in h.http.starts() if r["op"] != h.http.starts()[0]["op"]][0]
        self.assertNotIn("machine_id", second["body"], "devbox is away: the second goes home")

    def test_a_failed_request_does_not_hold_the_thread_to_its_computer(self):
        h = agent(self)
        computers(h, "mac-example", **{"mac-example": (True, self.BUSY), "devbox": (True, reading_of(64, 0.5))})
        h.http.refuse_on["devbox"] = (403, "permission_denied", "")
        first = ask(h, "/agent 第一个")
        wait_until(lambda: run_of(h, first)["state"] == "failed", what="failed")
        computers(h, "mac-example", **{"mac-example": (True, reading_of(18, 1.0)), "devbox": (True, reading_of(64, 0.5))})
        ask(h, "/agent 再来一次", root_id=first.id, parent_id=first.id)
        self.assertNotIn("machine_id", started(h, 2)["body"], "home is idle: the refusal holds nothing")

    def test_a_launcher_set_by_hand_stays_home(self):
        h = agent(self, agent_launcher="example-launcher")
        computers(h, "mac-example", **{"mac-example": (True, self.BUSY), "devbox": (True, reading_of(64, 0.5))})
        ask(h, "/agent 指定了启动器")
        body = started(h)["body"]
        self.assertNotIn("machine_id", body)
        self.assertEqual(body["launcher"], "example-launcher")
        self.assertEqual(placement_asks(h), [])

    def test_home_by_its_current_id_is_never_another_computer(self):
        h = agent(self)
        computers(h, "mac-old-id", **{"mac-example": (True, reading_of(18, 0.1))})  # read idle by its current id
        h.http.metrics["mac-old-id"] = dict(self.BUSY, machine_id="mac-example")  # busy through its old one
        ask(h, "/agent 只有一台电脑")
        self.assertNotIn("machine_id", started(h)["body"])

    def test_a_set_computer_is_never_placed(self):
        h = agent(self, agent_machine="mac-2")
        computers(h, "mac-example", **{"mac-example": (True, self.BUSY), "devbox": (True, reading_of(64, 0.5))})
        ask(h, "/agent 指定了电脑")
        self.assertEqual(started(h)["body"]["machine_id"], "mac-2")
        self.assertEqual(placement_asks(h), [])

    def test_balance_off_starts_as_before(self):
        h = agent(self, agent_balance=False)
        computers(h, "mac-example", **{"mac-example": (True, self.BUSY), "devbox": (True, reading_of(64, 0.5))})
        ask(h, "/agent 不分流")
        self.assertNotIn("machine_id", started(h)["body"])
        self.assertEqual(placement_asks(h), [])

    def test_readings_a_computer_does_not_send(self):
        h = agent(self)
        windows = reading_of(16, 0.0, free_gib=1.0)
        windows["latest"].pop("load")  # Windows sends no load: its memory alone makes it busy
        mac_unread = reading_of(64, 0.1)
        mac_unread["latest"]["mem_used"] = 0  # a Mac's total before its first used-memory reading: not chosen
        computers(h, "mac-example", **{"mac-example": (True, windows), "devbox-unread": (True, mac_unread),
                                       "devbox": (True, reading_of(8, 1.0))})
        ask(h, "/agent 内存紧张的 Windows")
        self.assertEqual(started(h)["body"].get("machine_id"), "devbox")

    def test_a_computer_the_hub_says_takes_no_conversation_is_never_chosen(self):
        h = agent(self)
        computers(h, "mac-example", **{"mac-example": (True, self.BUSY), "devbox-work": (True, reading_of(64, 0.1)),
                                       "devbox-pending": (True, reading_of(64, 0.1)), "devbox-old": (True, reading_of(64, 0.1)),
                                       "devbox": (True, reading_of(64, 3.2))})
        computers(h, "mac-example", **{"mac-example": (True, self.BUSY), "devbox-work": (True, reading_of(64, 0.1)),
                                       "devbox-pending": (True, reading_of(64, 0.1)), "devbox-old": (True, reading_of(64, 0.1)),
                                       "devbox-bare": (True, reading_of(64, 0.1)), "devbox": (True, reading_of(64, 3.2))})
        for m in h.http.machine_list:
            m.update({"devbox-work": {"trust": "work"}, "devbox-pending": {"trust": "pending"},
                      "devbox-old": {"node_incompatible": True}, "devbox-bare": {"claude_version": ""}}.get(m["id"], {}))
        ask(h, "/agent 挑能用的")
        self.assertEqual(started(h)["body"].get("machine_id"), "devbox")

    def test_a_thread_whose_computer_was_renamed_goes_to_its_current_id(self):
        h = agent(self, timing=fast(agent_keep=0.4))
        computers(h, "mac-example", **{"mac-example": (True, self.BUSY), "devbox-old-id": (True, reading_of(64, 0.5))})
        first = ask(h, "/agent 第一个问题")
        run1 = running(h, first)
        h.http.add(run1["session_id"], "assistant", "text", "第一个回答")
        h.http.end_turn(run1["session_id"])
        wait_until(lambda: replies(h), what="first answer")
        time.sleep(0.5)
        # merged: listed by its new id; the old one still answers, for the new id
        computers(h, "mac-example", **{"mac-example": (True, reading_of(18, 1.0)), "devbox": (True, reading_of(64, 0.5))})
        h.http.metrics["devbox-old-id"] = dict(reading_of(64, 0.5), machine_id="devbox")
        ask(h, "/agent 又来问", root_id=first.id)
        self.assertEqual(started(h, 2)["body"].get("machine_id"), "devbox")

    def test_a_thread_on_home_by_its_new_id_stays_home(self):
        h = agent(self, timing=fast(agent_keep=0.4))
        computers(h, "mac-example", **{"mac-example": (True, reading_of(18, 1.0)), "devbox": (True, reading_of(64, 0.5))})
        first = ask(h, "/agent 第一个问题")
        run1 = running(h, first)  # started with no machine: the hub answers home's current id, mac-example
        h.http.add(run1["session_id"], "assistant", "text", "第一个回答")
        h.http.end_turn(run1["session_id"])
        wait_until(lambda: replies(h), what="first answer")
        time.sleep(0.5)
        h.http.assistant_mid = "mac-old-id"  # the assistant's setting names home by an id kept after a merge
        h.http.metrics["mac-old-id"] = dict(self.BUSY, machine_id="mac-example")  # busy: only the thread keeps it home
        ask(h, "/agent 又来问", root_id=first.id)
        self.assertNotIn("machine_id", started(h, 2)["body"], "home: as a start without placement, never 'remote'")

    def test_home_by_an_id_the_list_does_not_show(self):
        h = agent(self)
        computers(h, "mac-old-id", **{"mac-example": (True, self.BUSY), "devbox": (True, reading_of(64, 0.5))})
        h.http.metrics["mac-old-id"] = self.BUSY  # merged entries: the hub still reads the computer by its old id
        ask(h, "/agent 旧 id")
        self.assertEqual(started(h)["body"].get("machine_id"), "devbox")

    def test_a_start_that_fails_there_is_never_started_again_elsewhere(self):
        h = agent(self)
        computers(h, "mac-example", **{"mac-example": (True, self.BUSY), "devbox": (True, reading_of(64, 0.5))})
        h.http.refuse_on["devbox"] = (403, "permission_denied", "none")  # (an answer may be lost: never run twice)
        m = ask(h, "/agent 那台电脑不收")
        wait_until(lambda: run_of(h, m)["state"] == "failed", what="failed")
        self.assertEqual({r["body"].get("machine_id") for r in h.http.starts()}, {"devbox"})

    def test_a_conversation_on_another_computer_is_told_its_limits(self):
        h = agent(self)
        computers(h, "mac-example", **{"mac-example": (True, self.BUSY), "devbox": (True, reading_of(64, 0.5))})
        first = ask(h, "/agent 在别的电脑上")
        run = running(h, first)
        prompt = started(h)["body"]["prompt"]
        self.assertIn("这个会话开在电脑 devbox 上", prompt)
        self.assertEqual(run["remote"], "devbox")
        h.http.add(run["session_id"], "assistant", "text", "答了")
        h.http.end_turn(run["session_id"])
        wait_until(lambda: replies(h), what="answer")
        second = ask(h, "/agent 接着问", root_id=first.id)
        self.assertEqual(running(h, second)["remote"], "devbox", "a follow-up's conversation is still on devbox")
        sent = h.http.posts("/api/v1/sessions/devbox/%s/messages" % run["session_id"])[-1]["body"]["text"]
        self.assertIn("这个会话开在电脑 devbox 上", sent)
        self.assertNotIn("看图：", sent, "no hint of a download it cannot read")
        self.assertNotIn("matefeishu_media", sent)

    def test_a_try_again_keeps_the_computer_it_chose(self):
        h = agent(self)
        computers(h, "mac-example", **{"mac-example": (True, self.BUSY), "devbox": (True, reading_of(64, 0.5)),
                                       "devbox-2": (True, reading_of(64, 30.0))})
        h.http.lost_starts = 1  # the first answer is lost: the same operation is asked again
        m = ask(h, "/agent 网络抖了一下")
        wait_until(lambda: len(h.http.starts()) >= 2, what="asked again")
        computers(h, "mac-example", **{"mac-example": (True, self.BUSY), "devbox": (True, self.BUSY),
                                       "devbox-2": (True, reading_of(64, 0.5))})
        running(h, m)
        bodies = [r["body"] for r in h.http.starts()]
        self.assertEqual({b.get("machine_id") for b in bodies}, {"devbox"}, "one operation, one computer")
        self.assertEqual(len(placement_asks(h)), 1)


class PlacementReadingTest(unittest.TestCase):
    def test_reading(self):
        from openmate_core import placement

        now = time.time()
        self.assertEqual(placement.reading(reading_of(8, 2.0, free_gib=4.0, total_gib=16.0), now), (0.25, 4 * GiB))
        self.assertIsNone(placement.reading(reading_of(8, 2.0, fresh=False), now), "a stale reading is none")
        self.assertIsNone(placement.reading(dict(reading_of(8, 2.0), online=False), now))
        self.assertIsNone(placement.reading({"online": True, "system": {"cores": 8}}, now), "no latest sample")
        self.assertIsNone(placement.reading({"online": True, "system": {"cores": 0}, "latest": {"load": 1}}, now))
        no_load = reading_of(8, 2.0, free_gib=3.0, total_gib=16.0)
        no_load["latest"].pop("load")
        self.assertEqual(placement.reading(no_load, now), (None, 3 * GiB))
        no_used = reading_of(8, 2.0)
        no_used["latest"]["mem_used"] = 0
        self.assertEqual(placement.reading(no_used, now), (0.25, None))
        self.assertTrue(placement.busy((1.01, 8 * GiB)))
        self.assertTrue(placement.busy((0.1, GiB)))
        self.assertTrue(placement.busy((None, GiB)))
        self.assertFalse(placement.busy((1.0, 2 * GiB)), "the line is Persona's load_per_core 1.0: over it, not at it")
        self.assertFalse(placement.busy((0.9, 8 * GiB)))
        self.assertFalse(placement.busy((0.5, None)))
        self.assertTrue(placement.busy((0.1, 8 * GiB), 8), "eight sessions working a turn: Persona's max_claude_procs")
        self.assertFalse(placement.busy((0.1, 8 * GiB), 7))
        self.assertFalse(placement.busy((0.1, 8 * GiB), None), "an unread count is no count")

    def test_a_slow_hub_is_not_waited_out(self):
        from openmate_core import placement

        class Slow:
            def __init__(self):
                self.t = 0.0
                self.asked = []

            def clock(self):
                return self.t

            def assistant_machine(self, timeout):
                return "home"

            def machines(self, timeout):
                return [{"id": m, "online": True, "claude_version": "2.1.330"} for m in ("home", "a", "b", "c")]

            def machine_metrics(self, mid, timeout):
                self.asked.append(mid)
                self.t += timeout  # each answer takes all it may
                return reading_of(18, 20.0) if mid == "home" else reading_of(64, 0.5)

        hub = Slow()
        machine, why = placement.choose(hub, clock=hub.clock)
        self.assertLessEqual(hub.t, placement.BUDGET)
        self.assertGreater(len(hub.asked), 1, "home and at least one other were read")
        self.assertLess(len(hub.asked), 4, "the budget cuts the readings short")
        self.assertIn(machine, ("", "a", "b"))


if __name__ == "__main__":
    unittest.main()
