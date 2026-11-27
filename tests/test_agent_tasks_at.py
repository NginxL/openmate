"""/agent after it hands out work (Platform.agent_task_wait): a turn that ends with only progress while its
conversation's to-do is still open, or its to-dos cannot be read, keeps the request open (🤔, no last words posted)
until the answer comes or the work is over; and the reply tool's `at` (Platform.verify_at): real @s of the chat's
members only, written in the text. WeChat as before."""

try:  # first: every temporary directory of this run in one, removed at its end
    import _runtmp  # noqa: F401
except ImportError:  # (started by its path from elsewhere: its directory is not on the path)
    pass
import time
import unittest

from harness import Harness, HubSideError, fast, wait_until
from test_agent import ask, running
from test_wechat_platform import wechat

TEAM = "chat_example_team"


def make(test, **cfg):
    config = {"agent": True}
    config.update(cfg)
    h = Harness(test, config=config, timing=fast(agent_task_recheck=0.3))
    h.sign_in()
    wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
    return h


class TaskWaitTest(unittest.TestCase):
    def dispatched(self, h, text="/agent 派个任务", progress=True):
        msg = ask(h, text)
        run = running(h, msg)
        h.http.add_todo("todo_1", run["session_id"], [("mac-agent", "agent_s1")])
        if progress:
            h.tool("agent_reply", request=run["id"], text="已派给 Agent，做完告诉你", progress=True)
        h.http.add(run["session_id"], "assistant", "text", "已经派出去了")
        h.http.end_turn(run["session_id"])
        wait_until(lambda: h.app.store.run_get(run["id"])["state"] == "done", what="the turn's end")
        return msg, run

    def test_open_work_waits_for_the_answer(self):
        h = make(self)
        msg, run = self.dispatched(h)
        self.assertEqual(h.app.store.run_get(run["id"])["awaiting"], "open")
        time.sleep(0.8)
        self.assertEqual(len(h.sent()), 1, "only the progress message: its last words are not the answer")
        self.assertNotIn((TEAM, msg.id, "DONE"), h.world.reactions)
        h.tool("agent_reply", request=run["id"], text="做完了：结果在这里")
        wait_until(lambda: (TEAM, msg.id, "DONE") in h.world.reactions, what="✅ with the answer")
        self.assertTrue(h.sent()[-1]["text"].startswith("做完了：结果在这里"))
        wait_until(lambda: "✅ 已完成" in h.sent()[0]["text"], what="the progress message closed")
        self.assertFalse(h.app.store.run_get(run["id"]).get("awaiting"))

    def test_its_last_words_go_as_progress_when_it_sent_none(self):
        h = make(self)
        msg, run = self.dispatched(h, progress=False)
        sent = wait_until(lambda: h.sent(), what="the progress")
        self.assertIn("⏳ 进行中", sent[0]["text"])
        self.assertIn("已经派出去了", sent[0]["text"])
        self.assertEqual(h.app.store.run_get(run["id"])["replies"], 0, "no answer")

    def test_unknown_to_dos_wait_too(self):
        h = make(self)
        h.http.todos_status = 503
        msg, run = self.dispatched(h)
        self.assertEqual(h.app.store.run_get(run["id"])["awaiting"], "unknown")
        self.assertNotIn((TEAM, msg.id, "DONE"), h.world.reactions)

    def test_once_the_work_is_over_it_ends_without_words(self):
        h = make(self)
        msg, run = self.dispatched(h)
        with h.http.lock:
            h.http.todos[0]["done_at"] = "2026-11-27T12:00:00Z"
        wait_until(lambda: (TEAM, msg.id, "DONE") in h.world.reactions, what="✅", timeout=10)
        self.assertEqual(len(h.sent()), 1, "nothing said")

    def test_an_answer_before_the_turn_ends_ends_it_as_before(self):
        h = make(self)
        msg = ask(h, "/agent 直接答")
        run = running(h, msg)
        h.http.add_todo("todo_2", run["session_id"], [("mac-agent", "agent_s2")])
        h.tool("agent_reply", request=run["id"], text="答案：可以")
        h.http.end_turn(run["session_id"])
        wait_until(lambda: (TEAM, msg.id, "DONE") in h.world.reactions, what="✅")
        self.assertFalse(h.app.store.run_get(run["id"]).get("awaiting"))

    def test_no_work_handed_out_ends_as_before(self):
        h = make(self)
        msg = ask(h, "/agent 普通问题")
        run = running(h, msg)
        h.http.add(run["session_id"], "assistant", "text", "答案是 42")
        h.http.end_turn(run["session_id"])
        wait_until(lambda: (TEAM, msg.id, "DONE") in h.world.reactions, what="✅")
        self.assertTrue(h.sent()[-1]["text"].startswith("答案是 42"))

    def test_the_instructions_say_so(self):
        h = make(self)
        ask(h, "/agent 看看说明")
        body = wait_until(lambda: h.http.starts(), what="started")[0]["body"]
        self.assertIn("这个请求会一直挂着等结果", body["prompt"])
        self.assertIn("不要用它假装 @ 了谁", body["prompt"])

    def test_wechat_ends_as_before(self):
        h = wechat(self)
        msg = ask(h, "/agent 派个任务")
        run = running(h, msg)
        h.http.add_todo("todo_3", run["session_id"], [("mac-agent", "agent_s3")])
        h.http.add(run["session_id"], "assistant", "text", "已经派出去了")
        h.http.end_turn(run["session_id"])
        wait_until(lambda: h.app.store.run_get(run["id"])["state"] == "done", what="done as before")
        wait_until(lambda: any("已经派出去了" in s["text"] for s in h.sent()), what="its answer, as before")
        self.assertFalse(h.app.store.run_get(run["id"]).get("awaiting"))
        body = h.http.starts()[0]["body"]
        self.assertNotIn("一直挂着等结果", body["prompt"])
        self.assertNotIn("假装 @", body["prompt"])


class AtTest(unittest.TestCase):
    def setUp(self):
        self.h = make(self)
        self.h.world.members[TEAM] = [
            {"id": "8001", "name": "构建机器人", "real_name": "构建机器人", "kind": "bot"},
            {"id": "user_example_b", "name": "示例同事乙", "real_name": "示例同事乙", "kind": "user"}]
        self.msg = ask(self.h, "/agent 让机器人重跑")
        self.run = running(self.h, self.msg)

    def test_a_member_by_id_and_name_is_really_at(self):
        out = self.h.tool("agent_reply", request=self.run["id"], text="已让 @构建机器人 重跑",
                          at=[{"id": "8001", "name": "构建机器人"}])
        self.assertIn("已回复", out)
        sent = self.h.sent()[-1]
        self.assertEqual(sent["mentions"], [("8001", "构建机器人", 3)])
        self.assertTrue(sent["text"].startswith("已让 @构建机器人 重跑"))

    def test_what_is_refused(self):
        h, rid = self.h, self.run["id"]
        cases = [
            ([{"id": "9999", "name": "谁"}], "@谁 你好"),  # not a member
            ([{"id": "8001", "name": "别的名字"}], "@别的名字 你好"),  # not the name shown there
            ([{"id": "8001", "name": "构建机器人"}], "没写名字"),  # not written in the text
            ("8001", "@构建机器人 你好"),  # not the shape
        ]
        for at, text in cases:
            with self.assertRaises(HubSideError, msg=str(at)):
                h.tool("agent_reply", request=rid, text=text, at=at)
        with self.assertRaises(HubSideError):
            h.tool("agent_reply", request=rid, text="@构建机器人 进度", progress=True, at=[{"id": "8001", "name": "构建机器人"}])
        self.assertEqual(h.sent(), [], "nothing went out")

    def test_the_same_words_with_at_are_another_reply(self):
        self.h.tool("agent_reply", request=self.run["id"], text="请 @构建机器人 重跑")
        self.h.tool("agent_reply", request=self.run["id"], text="请 @构建机器人 重跑", at=[{"id": "8001", "name": "构建机器人"}])
        self.assertEqual([s["mentions"] for s in self.h.sent()], [[], [("8001", "构建机器人", 2)]])

    def test_wechat_leaves_at_alone(self):
        w = wechat(self)
        msg = ask(w, "/agent 你好")
        run = running(w, msg)
        out = w.tool("agent_reply", request=run["id"], text="好的", at=[{"id": "x", "name": "y"}])
        self.assertIn("已回复", out)

    def test_no_at_no_mentions(self):
        self.h.tool("agent_reply", request=self.run["id"], text="打了 @构建机器人 但没给 at")
        self.assertEqual(self.h.sent()[-1]["mentions"], [], "a typed @name stays words")

    def test_the_tool_offers_at_here_not_on_wechat(self):
        props = {t["name"]: t for t in self.h.c("tools.list")["tools"]}["matefeishu_agent_reply"]["input_schema"]["properties"]
        self.assertIn("at", props)
        w = wechat(self)
        props = {t["name"]: t for t in w.c("tools.list")["tools"]}["matewechat_agent_reply"]["input_schema"]["properties"]
        self.assertNotIn("at", props)


if __name__ == "__main__":
    unittest.main()
