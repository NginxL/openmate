"""/agent in a thread whose conversation handed its work to a task's Agent: the user's own follow-up goes straight to
that Agent (agents.control), with where it was asked; the thread is told it was handed on; the task's result comes back
the way it always did (the task notice into the conversation, then <connector>_agent_reply), under the follow-up. The
assistant gets it instead when the Agent is gone, the to-do closed, nothing can tell which Agent, or the user wrote
问 Persona / 问助手; someone else's message is never handed on."""

import time
import unittest

from harness import wait_until
from test_agent import agent, ask, running, replies

FORWARDED = "已转给正在做这件事的 Agent"
TEAM = "chat_example_team"


def dispatched(test, **cfg):
    """A /agent request whose conversation started a task (to-do todo_1, its Agent agent-1 on mac-agent) and said so."""
    h = agent(test, **cfg)
    first = ask(h, "/agent 帮我把发版说明整理一下")
    run1 = running(h, first)
    sid = run1["session_id"]
    h.http.add_agent("agent-1")
    h.http.add_todo("todo_1", sid, [("mac-agent", "agent-1")], title="整理发版说明")
    h.http.add(sid, "assistant", "text", "已派出任务：整理发版说明")
    h.http.end_turn(sid)
    wait_until(lambda: len(replies(h)) == 1, what="the first answer")
    wait_until(lambda: h.app.store.run_get(run1["id"])["state"] == "done", what="the first run done")
    return h, first, run1, sid


def follow_up(h, first, text, sender="me", **kw):
    return h.world.push(TEAM, sender, text, root_id=first.id, thread_id=first.id, parent_id=first.id,
                        placement="thread", **kw)


def run_of(h, msg):
    return wait_until(lambda: next((r for r in h.app.store.runs_in_chat(TEAM) if r["request_id"] == msg.id), None),
                      what="the follow-up's run")


def forwarded(test, h, first, sid, text="/agent 顺便把 README 也改一下"):
    """The follow-up goes to the Agent, never to the conversation, and the thread is told so."""
    before = len(h.http.messages_to("mac-agent", "agent-1"))
    msg = follow_up(h, first, text)
    sends = wait_until(lambda: h.http.messages_to("mac-agent", "agent-1")[before:], what="the follow-up at the Agent")
    run = wait_until(lambda: (lambda r: r if r and r["state"] == "forwarded" else None)(run_of(h, msg)), what="forwarded")
    note = wait_until(lambda: [s for s in h.sent() if FORWARDED in s["text"] and s["reply_to"] == msg.id],
                      what="the note in the thread")
    test.assertEqual(len(note), 1)
    test.assertEqual((note[0]["chat"], note[0]["root"]), (TEAM, first.id), "said where the follow-up was asked")
    body = sends[0]["body"]
    test.assertEqual(body["client_msg_id"], "openmate-" + run["id"])
    test.assertIn(text.split(" ", 1)[1], body["text"])
    test.assertIn(msg.id, body["text"], "the message it answers")
    test.assertIn(first.id, body["text"], "the thread it is in")
    test.assertEqual(run["forward"], {"todo_id": "todo_1", "machine_id": "mac-agent", "session_id": "agent-1"})
    test.assertEqual((run["machine_id"], run["session_id"]), ("mac-example", sid), "still the conversation's thread")
    test.assertFalse(h.http.messages_to("mac-example", sid), "the conversation is not asked")
    return msg, run


def went_to_the_assistant(test, h, first, sid, text):
    """The follow-up goes on in the conversation, as before; the Agent gets nothing more."""
    at_agent = len(h.http.messages_to("mac-agent", "agent-1"))
    msg = follow_up(h, first, text)
    sends = wait_until(lambda: h.http.messages_to("mac-example", sid), what="the follow-up in the conversation")
    test.assertIn(text.split(" ", 1)[1], sends[-1]["body"]["text"])
    run = wait_until(lambda: (lambda r: r if r and r["state"] == "running" else None)(run_of(h, msg)), what="running")
    test.assertNotIn("forward", run)
    time.sleep(0.2)
    test.assertEqual(len(h.http.messages_to("mac-agent", "agent-1")), at_agent, "nothing more to the Agent")
    test.assertFalse([s for s in h.sent() if FORWARDED in s["text"] and s["reply_to"] == msg.id])
    return msg, run


class ForwardTest(unittest.TestCase):
    def test_a_follow_up_goes_to_the_agent_and_its_result_comes_back_under_it(self):
        h, first, run1, sid = dispatched(self)
        msg, run = forwarded(self, h, first, sid)
        self.assertIn("顺便把 README 也改一下", h.http.messages_to("mac-agent", "agent-1")[0]["body"]["text"])
        # the task's result comes back the usual way: the task notice into the conversation, whose reply names the
        # request it knows (the first); it goes under the follow-up it answers
        out = h.tool("agent_reply", request=run1["id"], text="README 也改好了")
        self.assertIn("已回复", out)
        s = h.sent()[-1]
        self.assertEqual((s["reply_to"], s["root"]), (msg.id, first.id))
        self.assertTrue(s["text"].startswith("README 也改好了"))
        self.assertEqual(h.app.store.run_get(run["id"])["replies"], 1)
        # once the follow-up has its answer, a later reply is the first request's again
        h.tool("agent_reply", request=run1["id"], text="另外发版说明也定稿了")
        self.assertEqual(h.sent()[-1]["reply_to"], first.id)

    def test_a_second_follow_up_goes_to_the_same_agent(self):
        h, first, run1, sid = dispatched(self)
        forwarded(self, h, first, sid)
        forwarded(self, h, first, sid, "/agent 还有 CHANGELOG")
        self.assertEqual(len(h.http.messages_to("mac-agent", "agent-1")), 2)
        self.assertEqual(len(h.http.starts()), 1)

    def test_asking_the_assistant_by_name_goes_to_the_assistant(self):
        h, first, run1, sid = dispatched(self)
        forwarded(self, h, first, sid)
        went_to_the_assistant(self, h, first, sid, "/agent 问助手：现在整体进度怎么样")
        h2, first2, _, sid2 = dispatched(self)
        forwarded(self, h2, first2, sid2)
        went_to_the_assistant(self, h2, first2, sid2, "/agent 问 Persona 这个任务还要多久")

    def test_a_closed_todo_goes_to_the_assistant(self):
        h, first, run1, sid = dispatched(self)
        forwarded(self, h, first, sid)
        with h.http.lock:
            h.http.todos[0]["done_at"] = "2026-11-19T12:00:00Z"
        went_to_the_assistant(self, h, first, sid, "/agent 做完了吗")

    def test_an_agent_released_from_the_todo_goes_to_the_assistant(self):
        h, first, run1, sid = dispatched(self)
        forwarded(self, h, first, sid)
        with h.http.lock:
            h.http.todos[0]["agents"][0]["released_at"] = "2026-11-19T12:00:00Z"
        went_to_the_assistant(self, h, first, sid, "/agent 接着做")

    def test_an_agent_gone_offline_goes_to_the_assistant(self):
        h, first, run1, sid = dispatched(self)
        forwarded(self, h, first, sid)
        with h.http.lock:
            h.http.sessions["agent-1"]["offline"] = True
        went_to_the_assistant(self, h, first, sid, "/agent 还在吗")

    def test_someone_elses_message_is_not_handed_on(self):
        h, first, run1, sid = dispatched(self)
        follow_up(h, first, "/agent 顺便帮我也看看", sender="user_example_a")
        time.sleep(0.4)
        self.assertFalse(h.http.messages_to("mac-agent", "agent-1"), "someone else's words never reach the Agent")
        self.assertFalse(h.http.messages_to("mac-example", sid))
        self.assertFalse([s for s in h.sent() if FORWARDED in s["text"]])
        forwarded(self, h, first, sid)  # the user's own, after it, is

    def test_two_open_tasks_go_to_the_assistant(self):
        h, first, run1, sid = dispatched(self)
        forwarded(self, h, first, sid)
        h.http.add_agent("agent-2")
        h.http.add_todo("todo_2", sid, [("mac-agent", "agent-2")], title="另一个任务")
        went_to_the_assistant(self, h, first, sid, "/agent 这个改一下")

    def test_a_lookup_refused_goes_to_the_assistant(self):
        h, first, run1, sid = dispatched(self)
        forwarded(self, h, first, sid)
        h.http.todos_status = 403
        went_to_the_assistant(self, h, first, sid, "/agent 这个改一下")

    def test_switched_off_it_goes_to_the_assistant(self):
        h, first, run1, sid = dispatched(self, agent_forward=False)
        went_to_the_assistant(self, h, first, sid, "/agent 顺便把 README 也改一下")

    def test_a_stop_after_a_follow_up_stops_the_conversation_and_its_task(self):
        h, first, run1, sid = dispatched(self)
        forwarded(self, h, first, sid)
        follow_up(h, first, "/stop")
        stops = wait_until(lambda: h.http.posts("/api/v1/assistant/conversations/%s/stop" % sid), what="the stop")
        self.assertEqual(len(stops), 1)


if __name__ == "__main__":
    unittest.main()
