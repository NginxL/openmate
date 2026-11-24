"""/agent in a thread whose conversation handed its work to a task's Agent: the user's own follow-up goes straight to
that Agent (agents.control), with where it was asked; the thread is told it was handed on; the task's result comes back
the way it always did (the task notice into the conversation, then <connector>_agent_reply), under the follow-up. The
assistant gets it instead when the Agent is gone, the to-do closed, nothing can tell which Agent, or the user wrote
问 Persona / 问助手; someone else's message is never handed on."""

try:  # first: every temporary directory of this run in one, removed at its end
    import _runtmp  # noqa: F401
except ImportError:  # (started by its path from elsewhere: its directory is not on the path)
    pass
import dataclasses
import time
import unittest

from harness import HubSideError, wait_until
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
    asked = len(h.http.messages_to("mac-example", sid))
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
    test.assertEqual(len(h.http.messages_to("mac-example", sid)), asked, "the conversation is not asked")
    wait_until(lambda: h.app.store.run_get(run["id"]).get("noted"), what="the note kept with the run")
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
        # the same reply asked again is the same reply: not sent again, under neither request
        n = len(h.sent())
        h.tool("agent_reply", request=run1["id"], text="README 也改好了")
        self.assertEqual(len(h.sent()), n, "a reply asked again is never sent twice")
        # once the follow-up has its answer, a later reply is the first request's again
        h.tool("agent_reply", request=run1["id"], text="另外发版说明也定稿了")
        self.assertEqual(h.sent()[-1]["reply_to"], first.id)

    def test_the_result_answers_the_follow_up_whatever_was_asked_in_between(self):
        h, first, run1, sid = dispatched(self)
        msg, run = forwarded(self, h, first, sid)
        ask_msg, ask_run = went_to_the_assistant(self, h, first, sid, "/agent 问助手：现在整体进度怎么样")
        h.tool("agent_reply", request=ask_run["id"], text="整体进度过半")  # its own turn: its own answer
        self.assertEqual(h.sent()[-1]["reply_to"], ask_msg.id)
        h.http.end_turn(sid)
        wait_until(lambda: h.app.store.run_get(ask_run["id"])["state"] == "done", what="that turn's end")
        # the task's notice, a turn later: the conversation names the request it knows last; it answers the follow-up
        h.tool("agent_reply", request=ask_run["id"], text="README 也改好了")
        self.assertEqual(h.sent()[-1]["reply_to"], msg.id)

    def test_a_turn_still_running_is_answered_once(self):
        """The conversation is still at its first turn (it handed out the task and goes on); a follow-up goes to the
        Agent, a later one to the assistant: the first turn is the later one's to end, and is answered once."""
        h = agent(self)
        first = ask(h, "/agent 帮我把发版说明整理一下")
        run1 = running(h, first)
        sid = run1["session_id"]
        h.http.add_agent("agent-1")
        h.http.add_todo("todo_1", sid, [("mac-agent", "agent-1")], title="整理发版说明")
        forwarded(self, h, first, sid)
        ask_msg, ask_run = went_to_the_assistant(self, h, first, sid, "/agent 问助手 顺便看看进度")
        self.assertEqual(h.app.store.run_get(run1["id"])["superseded_by"], ask_run["id"])
        h.http.add(sid, "assistant", "text", "都安排好了")
        h.http.end_turn(sid)
        wait_until(lambda: [s for s in replies(h) if s["text"].startswith("都安排好了")], what="the answer")
        time.sleep(0.4)
        self.assertEqual([s["reply_to"] for s in replies(h) if s["text"].startswith("都安排好了")], [ask_msg.id],
                         "one answer, under the request asked last")

    def test_a_stop_while_the_agent_is_looked_up_stops_the_conversation(self):
        h, first, run1, sid = dispatched(self)
        forwarded(self, h, first, sid)
        h.http.todos_delay = 1.0
        msg = follow_up(h, first, "/agent 再改一处")
        wait_until(lambda: [r for r in h.http.requests if r["path"] == "/api/v1/todos"][1:], what="the lookup")
        follow_up(h, first, "/stop")
        wait_until(lambda: h.http.posts("/api/v1/assistant/conversations/%s/stop" % sid), what="the stop")
        wait_until(lambda: h.app.store.run_get(run_of(h, msg)["id"])["state"] == "stopped", what="stopped")
        time.sleep(1.5)  # the lookup ends after the stop
        self.assertEqual(len(h.http.messages_to("mac-agent", "agent-1")), 1, "the stopped follow-up never went")
        self.assertFalse(h.http.messages_to("mac-example", sid), "nor did it wake the stopped conversation")

    def test_a_reply_after_the_conversation_was_stopped_is_refused(self):
        h, first, run1, sid = dispatched(self)
        msg, run = forwarded(self, h, first, sid)
        follow_up(h, first, "/stop")
        wait_until(lambda: h.http.posts("/api/v1/assistant/conversations/%s/stop" % sid), what="the stop")
        wait_until(lambda: [r for r in h.app.store.runs_in_chat(TEAM) if r["state"] == "stopped"], what="stopped")
        with self.assertRaises(HubSideError) as e:
            h.tool("agent_reply", request=run1["id"], text="README 也改好了")
        self.assertIn("已经停止", e.exception.message)

    def test_a_stop_in_persona_asks_the_conversation(self):
        """Stopped in the app (the hub keeps it stopped, nothing here knows): the conversation is asked, which is what
        lets its task notices through again; the next follow-up goes to the Agent again."""
        h, first, run1, sid = dispatched(self)
        with h.http.lock:
            h.http.sessions[sid]["stopped"] = True
        went_to_the_assistant(self, h, first, sid, "/agent 继续做吧")
        h.http.end_turn(sid)
        wait_until(lambda: [r for r in h.app.store.runs_in_chat(TEAM) if r["state"] == "running"] == [], what="turn over")
        forwarded(self, h, first, sid, "/agent 再顺便改个错别字")

    def test_a_request_while_one_is_looked_up_takes_neither_away(self):
        h, first, run1, sid = dispatched(self)
        h.http.todos_delay = 1.0
        b = follow_up(h, first, "/agent 顺便把 README 也改一下")
        wait_until(lambda: [r for r in h.http.requests if r["path"] == "/api/v1/todos"], what="the lookup")
        c = follow_up(h, first, "/agent 问助手 进度如何")
        wait_until(lambda: h.http.messages_to("mac-example", sid), what="the question in the conversation")
        wait_until(lambda: h.http.messages_to("mac-agent", "agent-1"), what="the follow-up at the Agent")
        wait_until(lambda: run_of(h, b).get("noted"), what="the follow-up handed on, the thread told")
        self.assertEqual(run_of(h, b)["state"], "forwarded")
        wait_until(lambda: run_of(h, c)["state"] == "running", what="c running (sent, then written)")

    def test_held_for_approval_is_handed_on(self):
        h, first, run1, sid = dispatched(self)
        h.http.send_refused["agent-1"] = (409, "peer_held", "unknown")
        msg, run = forwarded(self, h, first, sid)
        self.assertEqual(len([it for it in h.http.sessions["agent-1"]["items"] if it.get("client_msg_id")]), 1)
        note = [x for x in h.sent() if FORWARDED in x["text"]][-1]["text"]
        self.assertIn("电脑上有个确认还没处理", note, "the user is told it waits at the Agent's computer")

    def test_taken_while_its_computer_holds_a_dialog_says_so(self):
        h, first, run1, sid = dispatched(self)
        h.http.send_hook["agent-1"] = lambda: (200, {"ok": True, "waits_on_computer": True})
        forwarded(self, h, first, sid)
        self.assertIn("电脑上有个确认还没处理", [x for x in h.sent() if FORWARDED in x["text"]][-1]["text"])

    def test_a_stop_quoting_a_follow_up_being_looked_up_stops_it_alone(self):
        """Nothing of it went anywhere yet: the stop is its own, as for any request not sent; the conversation and
        its task go on."""
        h, first, run1, sid = dispatched(self)
        h.http.todos_delay = 1.0
        b = follow_up(h, first, "/agent 顺便把 README 也改一下")
        wait_until(lambda: [r for r in h.http.requests if r["path"] == "/api/v1/todos"], what="the lookup")
        h.world.push(TEAM, "me", "/stop", root_id=first.id, thread_id=first.id, parent_id=b.id, placement="thread")
        wait_until(lambda: run_of(h, b)["state"] == "stopped", what="b stopped")
        time.sleep(1.5)
        self.assertFalse(h.http.posts("/api/v1/assistant/conversations/%s/stop" % sid), "the conversation goes on")
        self.assertFalse(h.http.messages_to("mac-agent", "agent-1"))
        self.assertFalse(h.http.messages_to("mac-example", sid))
        self.assertIn("已回复", h.tool("agent_reply", request=run1["id"], text="发版说明整理好了"), "the first still answers")

    def test_the_agent_the_task_went_to_not_a_helper_it_started(self):
        h = agent(self)
        first = ask(h, "/agent 帮我把发版说明整理一下")
        run1 = running(h, first)
        sid = run1["session_id"]
        h.http.add_agent("agent-1")
        h.http.add_agent("agent-2")
        t = h.http.add_todo("todo_1", sid, [("mac-agent", "agent-2"), ("mac-agent", "agent-1")])
        t["agents"][1]["source"] = "manual"  # the task's own Agent; agent-2 is a checker it started (spawned), listed first
        h.http.add(sid, "assistant", "text", "已派出任务")
        h.http.end_turn(sid)
        wait_until(lambda: h.app.store.run_get(run1["id"])["state"] == "done", what="the first run done")
        forwarded(self, h, first, sid)
        self.assertFalse(h.http.messages_to("mac-agent", "agent-2"), "never the helper")

    def test_an_agent_moved_before_anything_went_out_asks_the_conversation(self):
        h, first, run1, sid = dispatched(self)

        def moved():  # the manager is offline (nothing sent); meanwhile the Agent goes to another to-do
            h.http.todos[0]["agents"][0]["released_at"] = "2026-11-19T12:00:00Z"
            h.http.todos.insert(0, {"id": "todo_x", "title": "别的", "origin": "elsewhere", "done_at": None,
                                    "agents": [{"machine_id": "mac-agent", "session_id": "agent-1"}]})
            return 503, {"error": "offline", "code": "manager_offline"}

        h.http.send_hook["agent-1"] = moved
        msg = follow_up(h, first, "/agent 顺便把 README 也改一下")
        wait_until(lambda: h.http.messages_to("mac-example", sid), what="the follow-up in the conversation")
        self.assertEqual(len(h.http.messages_to("mac-agent", "agent-1")), 1, "never tried again at the moved Agent")
        self.assertFalse(h.http.sessions["agent-1"]["items"])
        self.assertNotIn("forward", run_of(h, msg))

    def test_a_stop_in_persona_before_a_retry_stops_the_follow_up(self):
        h, first, run1, sid = dispatched(self)

        def stopped_meanwhile():  # nothing sent (the manager is offline); meanwhile it is stopped in the app
            h.http.sessions[sid]["stopped"] = True
            return 503, {"error": "offline", "code": "manager_offline"}

        h.http.send_hook["agent-1"] = stopped_meanwhile
        msg = follow_up(h, first, "/agent 顺便把 README 也改一下")
        wait_until(lambda: run_of(h, msg)["state"] == "stopped", what="stopped with its conversation")
        time.sleep(0.3)
        self.assertFalse(h.http.messages_to("mac-example", sid), "the stopped conversation is not woken")
        self.assertFalse(h.http.sessions["agent-1"]["items"])

    def test_a_result_before_the_send_answered_goes_under_the_follow_up(self):
        h, first, run1, sid = dispatched(self)
        out = {}

        def early():  # the Agent works fast: its result comes back through the conversation before our send returns
            out["text"] = h.tool("agent_reply", request=run1["id"], text="README 也改好了")
            return None

        h.http.send_hook["agent-1"] = early
        msg, run = forwarded(self, h, first, sid)
        self.assertIn("已回复", out["text"])
        s = [x for x in h.sent() if x["text"].startswith("README 也改好了")]
        self.assertEqual([x["reply_to"] for x in s], [msg.id])

    def test_a_reply_asked_again_never_answers_another_follow_up(self):
        h, first, run1, sid = dispatched(self)
        b, run_b = forwarded(self, h, first, sid, "/agent 改 README")
        c, run_c = forwarded(self, h, first, sid, "/agent 改 CHANGELOG")
        h.tool("agent_reply", request=run1["id"], text="CHANGELOG 改好了")
        self.assertEqual(h.sent()[-1]["reply_to"], c.id)
        h.tool("agent_reply", request=run1["id"], text="CHANGELOG 改好了")
        self.assertEqual(h.app.store.run_get(run_b["id"]).get("replies") or 0, 0, "b is still waiting")
        h.tool("agent_reply", request=run1["id"], text="README 改好了")
        self.assertEqual(h.sent()[-1]["reply_to"], b.id)

    def test_requests_reach_the_conversation_in_the_order_asked(self):
        """b is looked up (no Agent to hand it to: the to-do is closed) while c asks the conversation: c waits for b,
        so the conversation reads them as they were asked, and the turn's one answer is c's, posted once."""
        h, first, run1, sid = dispatched(self)
        with h.http.lock:
            h.http.todos[0]["done_at"] = "2026-11-19T12:00:00Z"
        h.http.todos_delay = 1.0
        b = follow_up(h, first, "/agent 做个英文版")
        wait_until(lambda: [r for r in h.http.requests if r["path"] == "/api/v1/todos"], what="the lookup")
        c = follow_up(h, first, "/agent 问助手，改为中文版")
        wait_until(lambda: len(h.http.messages_to("mac-example", sid)) == 2, what="both in the conversation")
        texts = [m["body"]["text"] for m in h.http.messages_to("mac-example", sid)]
        self.assertTrue(texts[0].endswith("做个英文版") and texts[1].endswith("问助手，改为中文版"), "as asked")
        wait_until(lambda: run_of(h, c)["state"] == "running", what="c running")
        self.assertEqual([r["id"] for r in h.app.store.runs_in_chat(TEAM) if r["state"] == "running"], [run_of(h, c)["id"]])
        h.http.add(sid, "assistant", "text", "两件事一起答")
        h.http.end_turn(sid)
        wait_until(lambda: [s for s in replies(h) if s["text"].startswith("两件事一起答")], what="the answer")
        time.sleep(0.4)
        self.assertEqual([s["reply_to"] for s in replies(h) if s["text"].startswith("两件事一起答")], [c.id])

    def test_a_request_waiting_its_turn_is_stopped_with_its_conversation(self):
        """b is looked up, c waits its turn behind it; a /stop of the thread's first request stops the conversation:
        c, asked before it, never wakes it again."""
        h, first, run1, sid = dispatched(self)
        with h.http.lock:
            h.http.todos[0]["done_at"] = "2026-11-19T12:00:00Z"  # b finds no Agent: it would ask the conversation
        h.http.todos_delay = 1.0
        b = follow_up(h, first, "/agent 做个英文版")
        wait_until(lambda: [r for r in h.http.requests if r["path"] == "/api/v1/todos"], what="the lookup")
        c = follow_up(h, first, "/agent 问助手 进度如何")
        run_of(h, c)
        follow_up(h, first, "/stop")
        wait_until(lambda: h.http.posts("/api/v1/assistant/conversations/%s/stop" % sid), what="the stop")
        wait_until(lambda: run_of(h, b)["state"] == "stopped" and run_of(h, c)["state"] == "stopped", what="both stopped")
        time.sleep(1.5)
        self.assertFalse(h.http.messages_to("mac-example", sid), "the stopped conversation is never asked again")

    def test_a_stop_quoting_a_request_still_waiting_stops_that_request(self):
        """c waits while b is handed on; a /stop quoting c stops c (nothing of it went anywhere), as a /stop of a
        request not started yet always did; b goes on to the Agent."""
        h, first, run1, sid = dispatched(self)
        h.http.todos_delay = 1.0
        b = follow_up(h, first, "/agent 顺便把 README 也改一下")
        wait_until(lambda: [r for r in h.http.requests if r["path"] == "/api/v1/todos"], what="the lookup")
        c = follow_up(h, first, "/agent 问助手 进度如何")
        run_of(h, c)
        h.world.push(TEAM, "me", "/stop", root_id=first.id, thread_id=first.id, parent_id=c.id, placement="thread")
        wait_until(lambda: run_of(h, c)["state"] == "stopped", what="c stopped")
        wait_until(lambda: run_of(h, b).get("noted"), what="b handed on")
        time.sleep(0.3)
        self.assertEqual(run_of(h, b)["state"], "forwarded")
        self.assertFalse(h.http.messages_to("mac-example", sid), "c never reached the conversation")

    def test_a_stop_of_the_first_request_stops_a_follow_up_waiting_for_its_result(self):
        h, first, run1, sid = dispatched(self)
        msg, run = forwarded(self, h, first, sid)
        follow_up(h, first, "/stop")  # quoting the first request
        wait_until(lambda: h.app.store.run_get(run["id"])["state"] == "stopped", what="the follow-up stopped too")
        ask_msg, ask_run = went_to_the_assistant(self, h, first, sid, "/agent 问助手 重新来吧")
        h.http.end_turn(sid)
        wait_until(lambda: h.app.store.run_get(ask_run["id"])["state"] == "done", what="that turn's end")
        h.tool("agent_reply", request=ask_run["id"], text="重新做好了")
        self.assertEqual(h.sent()[-1]["reply_to"], ask_msg.id, "never under the stopped follow-up")

    def test_a_follow_up_whose_known_request_is_gone_asks_the_conversation(self):
        h, first, run1, sid = dispatched(self)
        msg, run = forwarded(self, h, first, sid)
        h.tool("agent_reply", request=run1["id"], text="README 也改好了")  # the follow-up has its answer
        h.app.store._tx(lambda db: db.execute("DELETE FROM runs WHERE id = ?", (run1["id"],)))  # pruned
        went_to_the_assistant(self, h, first, sid, "/agent 再改一处")

    def test_a_restart_never_hands_on_into_a_conversation_stopped_meanwhile(self):
        h, first, run1, sid = dispatched(self)
        msg = follow_up(h, first, "这条是追问的原文")
        rid = "omr_" + "3" * 20
        h.app.store.run_put({"id": rid, "chat_id": TEAM, "request_id": msg.id, "thread_key": first.id,
                             "root_id": first.id, "placement": "thread", "state": "starting", "created": time.time(),
                             "request_text": "这条是追问的原文", "replies": 0, "reply_inflight": 0, "reply_ids": [],
                             "machine_id": "mac-example", "session_id": sid, "continued": run1["id"],
                             "asked": run1["id"], "forward_pending": {"todo_id": "todo_1", "machine_id": "mac-agent",
                                                                      "session_id": "agent-1"}})
        with h.http.lock:
            h.http.sessions[sid]["stopped"] = True  # stopped in Persona while the extension was down
        h2 = h.restart()
        h2.wait_ok()
        wait_until(lambda: h2.app.store.run_get(rid)["state"] == "failed", what="said unsure")
        self.assertFalse(h2.http.messages_to("mac-agent", "agent-1"), "not sent again")
        self.assertIn("Agent 会话", h2.app.store.run_get(rid)["error"])
        wait_until(lambda: [s for s in h2.sent() if "Agent 会话" in s["text"] and s["reply_to"] == msg.id],
                   what="the user told, where the follow-up was asked")

    def test_a_stop_of_the_follow_up_stops_the_turn_still_running(self):
        h = agent(self)
        first = ask(h, "/agent 帮我把发版说明整理一下")
        run1 = running(h, first)
        sid = run1["session_id"]
        h.http.add_agent("agent-1")
        h.http.add_todo("todo_1", sid, [("mac-agent", "agent-1")], title="整理发版说明")
        msg, run = forwarded(self, h, first, sid)
        h.world.push(TEAM, "me", "/stop", root_id=first.id, thread_id=first.id, parent_id=msg.id, placement="thread")
        wait_until(lambda: h.app.store.run_get(run1["id"])["state"] == "stopped", what="the first turn stopped too")
        with self.assertRaises(HubSideError):
            h.tool("agent_reply", request=run1["id"], text="还是想说一句")
        h.http.add(sid, "assistant", "text", "半截的回答")
        h.http.end_turn(sid)
        time.sleep(0.4)
        self.assertFalse([s for s in h.sent() if s["text"].startswith("半截的回答")], "nothing posted after the stop")

    def test_a_restart_while_the_agent_is_looked_up_looks_again(self):
        h, first, run1, sid = dispatched(self)
        msg = follow_up(h, first, "这条是追问的原文")
        rid = "omr_" + "2" * 20
        h.app.store.run_put({"id": rid, "chat_id": TEAM, "request_id": msg.id, "thread_key": first.id,
                             "root_id": first.id, "placement": "thread", "state": "starting", "created": time.time(),
                             "request_text": "这条是追问的原文", "replies": 0, "reply_inflight": 0, "reply_ids": [],
                             "machine_id": "mac-example", "session_id": sid, "continued": run1["id"],
                             "asked": run1["id"], "forwarding": True})
        h2 = h.restart()
        h2.wait_ok()
        wait_until(lambda: h2.app.store.run_get(rid)["state"] == "forwarded", what="handed on after the restart")
        wait_until(lambda: h2.app.store.run_get(rid).get("noted"), what="the note")
        self.assertIn("这条是追问的原文", h2.http.messages_to("mac-agent", "agent-1")[-1]["body"]["text"])

    def test_a_conversation_no_more_reporting_is_asked(self):
        """Persona keeps a reporting conversation two weeks: past that, its task notices go to the main conversation."""
        h, first, run1, sid = dispatched(self)
        forwarded(self, h, first, sid)
        with h.http.lock:
            h.http.sessions[sid]["reporter"] = False
        went_to_the_assistant(self, h, first, sid, "/agent 接着做")

    def test_a_follow_up_waiting_for_its_answer_is_kept_past_the_most_kept(self):
        h, first, run1, sid = dispatched(self)
        msg, run = forwarded(self, h, first, sid)
        h.app.timing = dataclasses.replace(h.app.timing, agent_keep_most=0)  # every finished run is past the most
        h.app.agent._prune(force=True)
        self.assertIsNotNone(h.app.store.run_get(run["id"]), "the follow-up still waits for its answer")
        self.assertIsNotNone(h.app.store.run_get(run1["id"]), "and the request the conversation knows")
        h.tool("agent_reply", request=run1["id"], text="README 也改好了")
        self.assertEqual(h.sent()[-1]["reply_to"], msg.id)
        h.app.agent._prune(force=True)
        self.assertIsNone(h.app.store.run_get(run["id"]), "answered: kept no more")

    def test_a_note_that_did_not_go_out_is_tried_again(self):
        h, first, run1, sid = dispatched(self)
        h.world.send_fail = "rejected"  # the chat refuses the next send (the note)
        msg = follow_up(h, first, "/agent 顺便把 README 也改一下")
        run = wait_until(lambda: (lambda r: r if r and r["state"] == "forwarded" and r.get("note_tried") else None)(
            run_of(h, msg)), what="the note tried")
        self.assertFalse(run.get("noted"))
        self.assertFalse([s for s in h.sent() if FORWARDED in s["text"]])
        old = h.app.store.run_get(run["id"])
        old["note_tried"] = time.time() - 120
        h.app.store.run_put(old)
        wait_until(lambda: [s for s in h.sent() if FORWARDED in s["text"] and s["reply_to"] == msg.id], what="the note")
        wait_until(lambda: h.app.store.run_get(run["id"]).get("noted"), what="kept as told")

    def test_a_stopped_conversation_is_asked_again(self):
        """Stopped, the hub drops the conversation's task notices until it is asked again: a result handed on now
        would never come back, so the next follow-up goes to the conversation."""
        h, first, run1, sid = dispatched(self)
        forwarded(self, h, first, sid)
        follow_up(h, first, "/stop")
        wait_until(lambda: h.http.posts("/api/v1/assistant/conversations/%s/stop" % sid), what="the stop")
        went_to_the_assistant(self, h, first, sid, "/agent 继续做吧")

    def test_an_answer_lost_on_the_way_is_sent_again_once(self):
        h, first, run1, sid = dispatched(self)
        h.http.lost_sends, h.http.lost_send_status = 1, (500, "internal")  # it went out; the answer did not come
        msg, run = forwarded(self, h, first, sid)
        posts = h.http.messages_to("mac-agent", "agent-1")
        self.assertEqual(len(posts), 2)
        self.assertEqual({p["body"]["client_msg_id"] for p in posts}, {"openmate-" + run["id"]})
        self.assertEqual(len([it for it in h.http.sessions["agent-1"]["items"] if it.get("client_msg_id")]), 1)

    def test_a_refused_send_goes_to_the_assistant(self):
        h, first, run1, sid = dispatched(self)
        forwarded(self, h, first, sid)
        h.http.send_refused["agent-1"] = 409
        before = len(h.http.messages_to("mac-agent", "agent-1"))
        msg = follow_up(h, first, "/agent 这个也改")
        wait_until(lambda: h.http.messages_to("mac-example", sid), what="the follow-up in the conversation")
        self.assertEqual(len(h.http.messages_to("mac-agent", "agent-1")), before + 1, "tried once, refused")
        self.assertNotIn("forward", run_of(h, msg))

    def test_a_follow_up_cut_off_by_a_restart_is_handed_on_once(self):
        h, first, run1, sid = dispatched(self)
        forwarded(self, h, first, sid)
        msg = follow_up(h, first, "这条是追问的原文")  # stored; the run below stands for its /agent cut off mid-send
        rid = "omr_" + "1" * 20
        target = {"todo_id": "todo_1", "machine_id": "mac-agent", "session_id": "agent-1"}
        h.app.store.run_put({"id": rid, "chat_id": TEAM, "request_id": msg.id, "thread_key": first.id,
                             "root_id": first.id, "placement": "thread", "state": "starting", "created": time.time(),
                             "request_text": "这条是追问的原文", "replies": 0, "reply_inflight": 0, "reply_ids": [],
                             "machine_id": "mac-example", "session_id": sid, "continued": run1["id"],
                             "asked": run1["id"], "forward_pending": target})
        h.http.add("agent-1", "user", "text", "已经送到的那次", client_msg_id="openmate-" + rid)  # it went out
        h2 = h.restart()
        h2.wait_ok()
        wait_until(lambda: h2.app.store.run_get(rid)["state"] == "forwarded", what="handed on after the restart")
        wait_until(lambda: [s for s in h2.sent() if FORWARDED in s["text"] and s["reply_to"] == msg.id], what="the note")
        self.assertEqual(len([it for it in h2.http.sessions["agent-1"]["items"]
                              if it.get("client_msg_id") == "openmate-" + rid]), 1, "never twice")
        self.assertEqual(h2.app.store.run_get(rid)["forward"], target)
        wait_until(lambda: h2.app.store.run_get(rid).get("noted"), what="the note kept with the run")

    def test_the_request_the_conversation_knows_is_kept_while_a_follow_up_waits(self):
        h, first, run1, sid = dispatched(self)
        msg, run = forwarded(self, h, first, sid)
        h.app.store._tx(lambda db: db.execute("UPDATE runs SET created = ? WHERE id = ?",  # older than what is kept
                                              (time.time() - h.app.timing.agent_keep - 60, run1["id"])))
        h.app.agent._prune(force=True)
        self.assertIsNotNone(h.app.store.run_get(run1["id"]), "a follow-up still waits on it")

        h.tool("agent_reply", request=run1["id"], text="README 也改好了")
        self.assertEqual(h.sent()[-1]["reply_to"], msg.id)

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
        n = len([r for r in h.http.requests if r["path"] == "/api/v1/assistant/reporters"])
        went_to_the_assistant(self, h, first, sid, "/agent 做完了吗")
        self.assertEqual(len([r for r in h.http.requests if r["path"] == "/api/v1/assistant/reporters"]), n,
                         "no to-do to hand on to: nothing more is asked")

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

    def test_an_open_task_without_an_agent_makes_two(self):
        h, first, run1, sid = dispatched(self)
        forwarded(self, h, first, sid)
        h.http.add_todo("todo_2", sid, [], title="还没开始的任务")
        went_to_the_assistant(self, h, first, sid, "/agent 这个改一下")

    def test_an_agent_no_longer_on_the_todo_goes_to_the_assistant(self):
        """The to-dos read before the Agent was released, its session read after: the session is on no open to-do."""
        h, first, run1, sid = dispatched(self)
        forwarded(self, h, first, sid)
        h.http.add_todo("todo_x", "another-conversation", [("mac-agent", "agent-1")])  # moved to another to-do
        with h.http.lock:
            h.http.todos.insert(0, h.http.todos.pop())  # the session's row now names that one
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
