"""/agent: which messages start a request, the conversation and its context, the reply tool's binding, the fallback,
continuing a thread, /stop, the timeout and resuming after a restart."""

import re
import time
import unittest

from harness import CID, Harness, HubSideError, fast, wait_until

SUFFIX = "> 🤖 AI 代答，仅供参考"
RUN_ID = re.compile(r"omr_[0-9a-f]{20}")


def agent(test, timing=None, **cfg):
    # these tests check the end mark (the style before 0.4); DefaultsTest checks today's: a header, a reaction
    config = {"agent": True, "agent_reply_suffix": SUFFIX, "agent_reply_header": " ", "agent_ack_reaction": ""}
    config.update(cfg)
    h = Harness(test, config=config, timing=timing)
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

    def test_after_a_restart_a_queued_request_goes_on_in_the_conversation_started_before_it(self):
        """a's new conversation was on its way (an operation id, no session yet) and b waited behind it: after the
        restart a is asked again under its operation first, and b goes on in the conversation it got, never a second."""
        h = agent(self)
        first = h.world.push("chat_example_team", "me", "第一个的原文")
        second = h.world.push("chat_example_team", "me", "接着问的原文", root_id=first.id)
        now = time.time()
        a = {"id": "omr_" + "5" * 20, "chat_id": "chat_example_team", "request_id": first.id, "thread_key": first.id,
             "root_id": "", "state": "starting", "created": now - 2, "request_text": "第一个的原文", "replies": 0,
             "reply_inflight": 0, "reply_ids": [], "start_op": "op-example-1", "start_prompt": "第一个的原文"}
        b = {"id": "omr_" + "6" * 20, "chat_id": "chat_example_team", "request_id": second.id, "thread_key": first.id,
             "root_id": first.id, "state": "starting", "created": now - 1, "queued": True, "request_text": "接着问的原文",
             "replies": 0, "reply_inflight": 0, "reply_ids": []}
        h.app.store.run_put(a)
        h.app.store.run_put(b)
        h.http.start_pending = 3  # the hub takes a moment to settle a's start
        h2 = h.restart()
        h2.wait_ok()
        wait_until(lambda: h2.app.store.run_get(b["id"])["state"] == "running", what="b going on")
        self.assertEqual(len({s["session_id"] for s in [h2.app.store.run_get(a["id"]), h2.app.store.run_get(b["id"])]}), 1)
        self.assertEqual(len([st for st in h2.http.starts() if st["op"] == "op-example-1"]) >= 1, True)
        self.assertEqual(len({st["op"] for st in h2.http.starts()}), 1, "one conversation")

    def test_the_turns_keep_the_order_asked_whatever_order_they_are_queued_in(self):
        h = agent(self)
        now = time.time()
        runs = [{"id": "omr_%020d" % i, "chat_id": "chat_example_team", "request_id": "m%d" % i, "thread_key": "t",
                 "state": "starting", "created": now + i, "replies": 0, "reply_inflight": 0, "reply_ids": []}
                for i in range(3)]
        for r in runs:
            h.app.store.run_put(r)
        with h.app.agent._mu:
            for r in (runs[2], runs[0], runs[1]):  # a live one heard before a restart took in older ones
                h.app.agent._enqueue(r)
        self.assertEqual(h.app.agent._turns[("chat_example_team", "t")], [r["id"] for r in runs])

    def test_a_request_cut_off_while_it_waited_its_turn_starts_again(self):
        h = agent(self)
        first = ask(h, "/agent 第一个")
        run1 = running(h, first)
        sid = run1["session_id"]
        second = h.world.push("chat_example_team", "me", "接着问的原文", root_id=first.id)
        rid = "omr_" + "4" * 20
        h.app.store.run_put({"id": rid, "chat_id": "chat_example_team", "request_id": second.id, "thread_key": first.id,
                             "root_id": first.id, "state": "starting", "created": time.time(), "queued": True,
                             "request_text": "接着问的原文", "replies": 0, "reply_inflight": 0, "reply_ids": []})
        h2 = h.restart()
        h2.wait_ok()
        msgs = wait_until(lambda: h2.http.posts("/api/v1/sessions/mac-example/%s/messages" % sid), what="started again")
        self.assertIn("接着问的原文", msgs[0]["body"]["text"])
        wait_until(lambda: h2.app.store.run_get(rid)["state"] == "running", what="running")
        self.assertFalse([s for s in h2.sent() if "扩展重启了" in s["text"]], "never told to send it again")

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
        self.assertEqual(sent["text"], "总结如下：一切正常。\n\n> 🤖 AI 回复 · Claude Opus 5.5 · xhigh", "its own mark is not doubled")
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
        self.assertEqual(h.sent()[-1]["text"], "第一条\n\n> 🤖 AI 回复 · Claude Opus 5.5 · xhigh")
        h2 = self.make(agent_reply_suffix="")
        msg = ask(h2, "/agent 一直不知道模型")
        run = running(h2, msg)
        h2.http.model_late = 1000
        h2.tool("agent_reply", request=run["id"], text="先发出去")
        self.assertEqual(h2.sent()[-1]["text"], "先发出去\n\n> 🤖 AI 回复", "after agent_model_wait, without it")
        self.assertFalse(h2.app.store.run_get(run["id"]).get("model"), "not kept empty")
        h2.http.model_late = 0
        h2.tool("agent_reply", request=run["id"], text="第二条")
        self.assertEqual(h2.sent()[-1]["text"], "第二条\n\n> 🤖 AI 回复 · Claude Opus 5.5 · xhigh", "asked again")

    def test_settings_and_a_refused_reaction(self):
        h = self.make(agent_ack_reaction="OnIt", agent_reply_header="—— {model}（{effort}）", agent_reply_suffix=SUFFIX)
        h.world.react_fail = True
        msg = ask(h, "/agent 写个周报")
        run = running(h, msg)  # a refused reaction never holds the request
        h.tool("agent_reply", request=run["id"], text="周报写好了")
        self.assertEqual(h.sent()[-1]["text"], "周报写好了\n\n—— Claude Opus 5.5（xhigh）\n\n" + SUFFIX)
        self.assertEqual(h.world.reactions, [])
        h2 = self.make(agent_ack_reaction="", agent_reply_header="")
        msg = ask(h2, "/agent 不要表情")
        run = running(h2, msg)
        h2.tool("agent_reply", request=run["id"], text="好")
        self.assertEqual((h2.sent()[-1]["text"], h2.world.reactions), ("好", []))

    def test_the_mark_on_top_on_request_and_marks_at_either_end_are_recognised(self):
        h = self.make(agent_mark_position="top")
        msg = ask(h, "/agent 放第一行")
        run = running(h, msg)
        h.tool("agent_reply", request=run["id"], text="答案在第二行\n\n> 🤖 AI 回复 · 别的模型")
        self.assertEqual(h.sent()[-1]["text"], "> 🤖 AI 回复 · Claude Opus 5.5 · xhigh\n答案在第二行", "its own mark at the end is not doubled")
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
        self.assertEqual(fill_header("> 🤖 AI 回复 · {model} · {effort}", "gpt-6-astra", ""), "> 🤖 AI 回复 · GPT-6 Astra")


if __name__ == "__main__":
    unittest.main()
