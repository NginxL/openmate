"""/agent: which messages start a request, the conversation and its context, the reply tool's binding, the fallback,
continuing a thread, /stop, the timeout and resuming after a restart."""

import re
import time
import unittest

from harness import CID, Harness, HubSideError, fast, wait_until

SUFFIX = "> 🤖 AI 代答，仅供参考"
RUN_ID = re.compile(r"omr_[0-9a-f]{20}")


def agent(test, timing=None, **cfg):
    config = {"agent": True}
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

    def test_only_my_stop_counts(self):
        h = self.h
        h.world.push("chat_example_team", "user_example_a", "/stop", root_id=self.m.id)
        ask(h, "/stop")  # mine, but in no request's thread
        time.sleep(0.3)
        self.assertEqual(replies(h), [])
        self.assertEqual(h.app.store.run_get(self.run["id"])["state"], "running")


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


class SettingsAndFailuresTest(unittest.TestCase):
    def test_suffix_settings(self):
        from openmate_core.agent import parse_suffix

        self.assertEqual(parse_suffix(None), SUFFIX)
        self.assertEqual(parse_suffix(""), SUFFIX)
        self.assertEqual(parse_suffix(" "), "")
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
        wait_until(lambda: replies(h), what="the explanation")
        self.assertIn("Persona hub", replies(h)[0]["text"])
        self.assertEqual(run_of(h, m)["state"], "failed")

    def test_start_refused(self):
        h = agent(self)
        h.http.start_fail = 400
        ask(h, "/agent 你好")
        wait_until(lambda: replies(h), what="the explanation")
        self.assertIn("没能启动 Agent", replies(h)[0]["text"])

    def test_runs_are_kept_three_days_and_at_most_200(self):
        h = agent(self)
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


if __name__ == "__main__":
    unittest.main()
