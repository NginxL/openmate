"""Persona's 渠道 (the channel extension point, docs/extensions/channels.md "Contributing a channel"): both extensions
declare one on their connector; it works only in the chat the user named in 「Persona 渠道会话」 (none by default, and
then nothing is sent). There the user's /agent goes to Persona's channel flow (channel.receive) instead of an /agent
conversation, Persona's answers come back through channel.send where they were asked, and a question Persona put
there is taken back once it was answered somewhere else (Feishu; WeChat cannot take a message back)."""

import json
import os
import sys
import time
import unittest

from harness import CID, ROOT, Harness, HubSideError, fast, wait_until
from openmate_core.backend import Chat
from openmate_core.fixture import FixtureWorld

sys.path.insert(0, os.path.join(ROOT, "extensions", "openmate-wechat"))
import openmate_wechat  # noqa: E402

SOLO = "chat_example_solo"
SOLO_NAME = "示例渠道群"
TEAM = "chat_example_team"
HEADER = "🤖 AI 回复"


def manifest(ext):
    with open(os.path.join(ROOT, "extensions", ext, "persona-extension.json"), encoding="utf-8") as f:
        return json.load(f)


def channel(test, platform=None, timing=None, world=None, **cfg):
    """A signed-in run whose 「Persona 渠道会话」 is the group only the user is in (by its name)."""
    world = world or FixtureWorld()
    world.add_chat(Chat(SOLO, "group", SOLO_NAME, members=1))
    world.make(SOLO, "me", "这个群只有我自己", ago=3000)
    config = {"agent": True, "channel_chat": SOLO_NAME, "agent_ack_reaction": ""}
    config.update(cfg)
    h = Harness(test, config=config, world=world, platform=platform, timing=timing)
    h.sign_in()
    wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
    return h


def say(h, text, chat=SOLO, **kw):
    return h.world.push(chat, "me", text, **kw)


def received(h, n=1):
    wait_until(lambda: len(h.hub.received) >= n, what="%d message(s) handed to Persona" % n)
    return h.hub.received[n - 1]


def ended(h, ix):
    """How the channel's question ended, once it did."""
    return wait_until(lambda: (lambda st: st if st not in ("", "open", "recalling") else None)(
        h.app.store.channel_ask_state(ix, "ask:" + ix)), what="the question ended")


def waiting(ix, kind="permission", **kw):
    """A question the hub lists as waiting (its session, as the hub says it)."""
    return dict({"id": ix, "kind": kind, "machine_id": "mac-example", "session_id": "sess-asks"}, **kw)


def answered_elsewhere(h, ix, status="resolved"):
    """Answered in the app: out of the waiting list, and the history says how it ended."""
    h.http.history.append({"interaction": waiting(ix), "status": status})
    h.http.interactions = [i for i in h.http.interactions if i["id"] != ix]


def line_state(h, mid):
    return wait_until(lambda: (lambda r: r["state"] if r and r["state"] != "pending" else None)(
        h.app.store.channel_msg(SOLO, mid)), what="the line handled")


def send(h, **params):
    params.setdefault("contribution", h.cid)
    return h.hub.request("channel.send", params)


class DeclaredTest(unittest.TestCase):
    def test_both_extensions_declare_a_channel_on_their_connector(self):
        for ext, cid in (("openmate-feishu", "matefeishu"), ("openmate-wechat", "matewechat")):
            m = manifest(ext)
            items = m["contributes"].get("channel")
            self.assertIsInstance(items, list, ext + " declares no channel")
            self.assertEqual(len(items), 1)
            ch = items[0]
            self.assertEqual((ch["id"], ch["kind"], ch["connector"], ch["bind"]), (cid, "im", cid, "qr"))
            self.assertTrue(1 <= len(ch["name"]) <= 40)
            caps = ch["capabilities"]
            self.assertEqual(caps["asks"], "text")  # questions and approvals come as text, answered by a reply
            self.assertIs(caps["push"], True)  # questions need it
            self.assertEqual(caps["format"], "plain")  # sent as plain text, as /agent's replies are
            for off in ("images_in", "files_in", "voice_in", "typing", "progress", "threads"):
                self.assertIs(caps[off], False, off)
            self.assertTrue(caps["max_text"] == 0 or caps["max_text"] >= 200)
            prop = m["configuration"]["properties"]["channel_chat"]
            self.assertEqual(prop["type"], "string")
            self.assertNotIn("default", prop)  # no chat by default: nothing goes out until the user names one
            # no new permission: an upgrade asks nothing (the hub's agents.read already reads the waiting questions)
            self.assertIn("agents.read", m["permissions"]["hub"])

    def test_initialize_says_the_channel_and_unknown_calls_are_refused(self):
        h = Harness(self, config={"agent": True}, initialize=False)
        res = h.initialize()
        m = manifest("openmate-feishu")
        self.assertEqual(res["contributes"]["channel"], m["contributes"]["channel"])
        self.assertEqual(h.hub.request("manifest", {})["contributes"]["channel"], m["contributes"]["channel"])
        h.sign_in()
        self.assertEqual(h.hub.request("channel.typing", {"contribution": CID, "on": True}), {})
        with self.assertRaises(HubSideError) as e:
            send(h, contribution="examplechat", ref="r1", text="你好", kind="answer")
        self.assertEqual(e.exception.code, "no_contribution")
        with self.assertRaises(HubSideError) as e:
            send(h, text="没有 ref", kind="answer")
        self.assertEqual(e.exception.code, "invalid_params")
        with self.assertRaises(HubSideError) as e:
            h.hub.request("channel.nothing", {"contribution": CID})
        self.assertEqual(e.exception.code, "unknown_method")


class NoChatTest(unittest.TestCase):
    def test_without_a_channel_chat_nothing_is_sent_and_agent_is_as_before(self):
        h = Harness(self, config={"agent": True, "agent_ack_reaction": ""})
        h.sign_in()
        for kind in ("answer", "ask", "push", "note"):
            with self.assertRaises(HubSideError) as e:
                send(h, ref="r-" + kind, text="一条消息", kind=kind)
            self.assertEqual(e.exception.code, "cannot_send")
        self.assertEqual(h.sent(), [])
        say(h, "/agent 帮我看看", chat=TEAM)
        wait_until(lambda: h.http.starts(), what="an /agent conversation")
        time.sleep(0.2)
        self.assertEqual(h.hub.received, [])

    def test_a_chat_name_nobody_has_or_two_have_is_no_chat(self):
        h = Harness(self, config={"agent": True, "channel_chat": "没有这个群"})
        h.sign_in()
        with self.assertRaises(HubSideError) as e:
            send(h, ref="r1", text="你好", kind="push")
        self.assertEqual(e.exception.code, "cannot_send")
        h2 = channel(self)
        h2.world.add_chat(Chat("chat_example_twin", "group", SOLO_NAME, members=1))
        h2.world.push("chat_example_twin", "me", "同名的另一个群")
        wait_until(lambda: h2.app.store.get_chat("chat_example_twin"), what="the twin stored")
        h2.app.channel._checked = None  # checked again now
        with self.assertRaises(HubSideError) as e:
            send(h2, ref="r2", text="你好", kind="push")
        self.assertEqual(e.exception.code, "cannot_send")

    def test_a_name_another_chat_has_where_the_service_knows_it_is_no_chat(self):
        h = channel(self)
        world = h.world
        world.add_chat(Chat("chat_example_unread", "group", SOLO_NAME, members=30))  # never read here
        self.assertIsNone(h.app.store.get_chat("chat_example_unread"))
        h.app.channel._checked = None
        with self.assertRaises(HubSideError) as e:
            send(h, ref="r1", text="你好", kind="push")
        self.assertEqual(e.exception.code, "cannot_send")
        world.people["user_example_same"] = SOLO_NAME  # a person of that name: their chat would be that name too
        world.chats.pop("chat_example_unread")
        h.app.channel._checked = None
        with self.assertRaises(HubSideError) as e:
            send(h, ref="r2", text="你好", kind="push")
        self.assertEqual(e.exception.code, "cannot_send")
        world.people.pop("user_example_same")
        h.app.channel._checked = None
        self.assertIn("message_id", send(h, ref="r3", text="你好", kind="push"))

    def test_a_chat_left_is_no_chat(self):
        h = channel(self, channel_chat=SOLO)
        h.app.store.chat_gone(SOLO)
        wait_until(lambda: h.app.store.is_gone(SOLO), what="gone")
        h.app.channel._checked = None
        with self.assertRaises(HubSideError) as e:
            send(h, ref="r1", text="你好", kind="push")
        self.assertEqual(e.exception.code, "cannot_send")

    def test_the_connector_switched_off_takes_the_channel_off(self):
        world = FixtureWorld()
        world.add_chat(Chat(SOLO, "group", SOLO_NAME, members=1))
        world.make(SOLO, "me", "这个群只有我自己", ago=3000)
        h = Harness(self, config={"agent": True, "channel_chat": SOLO}, world=world, initialize=False)
        h.initialize(active={"connectors": [], "channel": [CID]})
        with self.assertRaises(HubSideError) as e:
            send(h, ref="r1", text="你好", kind="push")
        self.assertEqual(e.exception.code, "cannot_send")


class ReceiveTest(unittest.TestCase):
    def test_agent_in_the_channel_chat_goes_to_persona(self):
        h = channel(self)
        m = say(h, "/agent 帮我查一下明天的会")
        got = received(h)
        self.assertEqual(got["contribution"], CID)
        self.assertEqual(got["message"], {"id": m.id, "text": "帮我查一下明天的会"})
        h.app.agent.on_message(h.app.store.message(SOLO, m.id))  # delivered again: handed over once
        time.sleep(0.2)
        self.assertEqual(h.hub.receive_calls, 1)
        self.assertEqual(h.http.starts(), [], "no /agent conversation of its own")

    def test_other_chats_keep_the_agent_path(self):
        h = channel(self)
        say(h, "/agent 帮我看看", chat=TEAM)
        wait_until(lambda: h.http.starts(), what="an /agent conversation")
        time.sleep(0.2)
        self.assertEqual(h.hub.received, [])

    def test_every_line_of_the_users_own_goes_to_persona(self):
        h = channel(self)
        h.world.push(SOLO, "user_example_a", "/agent 别人发的")
        say(h, "/agent")  # nothing to hand over
        say(h, "/agent 这是一条代答\n\n> " + HEADER)  # an AI's own line
        m = say(h, "明天提醒我交周报")  # no prefix needed: the chat is the user's conversation with Persona
        self.assertEqual(received(h)["message"], {"id": m.id, "text": "明天提醒我交周报"})
        time.sleep(0.3)
        self.assertEqual(len(h.hub.received), 1)
        self.assertEqual(h.http.starts(), [])

    def test_a_line_waits_while_the_chat_cannot_be_checked(self):
        world = FixtureWorld()
        world.search_fail = True  # the service away: the name cannot be checked
        h = channel(self, world=world)
        with self.assertRaises(HubSideError) as e:
            send(h, ref="r1", text="你好", kind="push")
        self.assertNotEqual(e.exception.code, "cannot_send", "the hub tries again")
        m = say(h, "/agent 你好")
        time.sleep(0.3)
        self.assertEqual(h.hub.received, [])
        self.assertEqual(h.http.starts(), [], "never the /agent way meanwhile")
        world.search_fail = False
        h.app.channel._checked = None
        h.app.channel._queued.set()
        self.assertEqual(received(h)["message"]["id"], m.id)

    def test_a_chat_renamed_is_no_chat_at_once(self):
        h = channel(self)
        send(h, ref="r1", text="你好", kind="push")
        h.app.store._tx(lambda db: db.execute("UPDATE chats SET name = '改过的名字' WHERE id = ?", (SOLO,)))
        with self.assertRaises(HubSideError) as e:
            send(h, ref="r2", text="你好", kind="push")
        self.assertEqual(e.exception.code, "cannot_send")

    def test_the_channel_works_with_agent_off(self):
        h = channel(self, agent=False)
        m = say(h, "/agent 你好")
        self.assertEqual(received(h)["message"]["id"], m.id)

    def test_stop_words_are_the_channels_stop(self):
        h = channel(self)
        say(h, "/停")
        self.assertEqual(received(h)["message"]["text"], "/stop")
        say(h, "/stop")
        self.assertEqual(received(h, 2)["message"]["text"], "/stop")

    def test_a_reply_quoting_a_channel_message_is_handed_over_with_the_quote(self):
        h = channel(self)
        m = say(h, "/agent 帮我查一下明天的会")
        received(h)
        res = send(h, ref="run1:0", text="明天 10 点有评审会。", kind="answer", reply_to=m.id)
        follow = say(h, "改到下午可以吗", parent_id=res["message_id"])
        got = received(h, 2)["message"]
        self.assertEqual(got["id"], follow.id)
        self.assertEqual(got["text"], "改到下午可以吗")
        self.assertIn("明天 10 点有评审会。", got["quote"])

    def test_a_follow_up_in_a_thread_a_request_was_asked_in(self):
        h = channel(self)
        root = h.world.push(SOLO, "user_example_a", "这周的事")
        say(h, "/agent 总结一下这个话题", root_id=root.id, parent_id=root.id, placement="thread")
        received(h)
        follow = say(h, "再列一下负责人", root_id=root.id, parent_id=root.id, placement="thread")
        self.assertEqual(received(h, 2)["message"], {"id": follow.id, "text": "再列一下负责人"})

    def test_a_stop_quoting_a_channel_answer_is_the_channels_even_in_an_old_requests_thread(self):
        h = channel(self)
        root = h.world.push(SOLO, "user_example_a", "这周的事")
        old = h.app.agent._new_run({"chat_id": SOLO, "id": root.id}, "以前的请求", time.time())
        old["state"] = "running"
        h.app.store.run_put(old)  # an /agent request asked in this thread before the channel
        m = say(h, "/agent 总结一下这个话题", root_id=root.id, parent_id=root.id, placement="thread")
        received(h)
        answer = send(h, ref="run1:0", text="总结如下。", kind="answer", reply_to=m.id)["message_id"]
        say(h, "/stop", root_id=root.id, parent_id=answer, placement="thread")
        self.assertEqual(received(h, 2)["message"]["text"], "/stop")
        time.sleep(0.2)
        self.assertEqual(h.app.store.run_get(old["id"])["state"], "running", "the old request is not touched")

    def test_lines_are_handed_over_in_order_and_after_a_restart(self):
        # tried again after 0.3 s, then not before 30 s: the restart comes while the line still waits
        h = channel(self, timing=fast(channel_poll=0.05, channel_retry=(0.3, 30.0), channel_unseen=0.3))
        fails = [1]

        def flaky(p):
            if fails[0]:
                fails[0] -= 1
                raise HubSideError("", "the assistant is away")
        h.hub.receive_answer = flaky
        first = say(h, "/agent 跑一下测试")
        say(h, "/stop")
        wait_until(lambda: len(h.hub.received) == 2, what="both")
        self.assertEqual([r["message"]["id"] for r in h.hub.received][0], first.id, "the stop never overtakes it")
        self.assertEqual(h.hub.received[1]["message"]["text"], "/stop")
        h.hub.receive_answer = lambda p: (_ for _ in ()).throw(HubSideError("", "the assistant is away"))
        cut = say(h, "/agent 重启前发的")
        wait_until(lambda: h.hub.receive_calls >= 5, what="tried twice")
        h2 = h.restart()
        h2.wait_ok()
        got = wait_until(lambda: [r for r in h2.hub.received if r["message"]["id"] == cut.id], what="handed over")
        self.assertEqual(got[0]["message"]["text"], "重启前发的")

    def test_a_refused_hand_over_is_said_in_the_chat(self):
        h = channel(self)
        h.hub.receive_answer = lambda p: (_ for _ in ()).throw(HubSideError("", "the assistant is away"))
        m = say(h, "/agent 你好")
        note = wait_until(lambda: [s for s in h.sent() if s["chat"] == SOLO and "没能交给 Persona" in s["text"]],
                          what="the note")[0]
        self.assertEqual(note["reply_to"], m.id)
        self.assertIn(HEADER, note["text"])
        self.assertGreaterEqual(h.hub.receive_calls, 2, "tried again before giving up")


class SendTest(unittest.TestCase):
    def test_the_answer_goes_back_where_it_was_asked_once_per_ref(self):
        h = channel(self)
        m = say(h, "/agent 帮我查一下明天的会")
        received(h)
        res = send(h, ref="run1:0", text="明天 10 点有评审会。", kind="answer", reply_to=m.id)
        out = [s for s in h.sent() if s["chat"] == SOLO]
        self.assertEqual(len(out), 1)
        self.assertEqual(res["message_id"], out[0]["id"])
        self.assertEqual((out[0]["reply_to"], out[0]["root"]), (m.id, None))  # quoting it in the chat
        self.assertTrue(out[0]["text"].startswith("明天 10 点有评审会。"))
        self.assertIn(HEADER, out[0]["text"])
        self.assertEqual(send(h, ref="run1:0", text="明天 10 点有评审会。", kind="answer", reply_to=m.id), res)
        rec = wait_until(lambda: [r for r in h.hub.sent_records if r.get("setting") == "channel"], what="the ledger")
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["target"], {"chat": SOLO, "reply_to": m.id})
        time.sleep(0.2)
        self.assertEqual(len([s for s in h.sent() if s["chat"] == SOLO]), 1, "sent once")
        self.assertEqual(len(h.hub.received), 1, "its echo is no request")

    def test_a_request_in_a_thread_is_answered_in_the_thread(self):
        h = channel(self)
        root = h.world.push(SOLO, "user_example_a", "这周的事")
        m = say(h, "/agent 总结一下这个话题", root_id=root.id, parent_id=root.id, placement="thread")
        received(h)
        send(h, ref="run2:0", text="总结如下。", kind="answer", reply_to=m.id)
        out = [s for s in h.sent() if s["chat"] == SOLO][-1]
        self.assertEqual((out["reply_to"], out["root"], out["to_chat"]), (m.id, root.id, False))

    def test_without_a_request_it_goes_into_the_chat(self):
        h = channel(self)
        send(h, ref="push1", text="后台任务做完了。", kind="push")
        out = [s for s in h.sent() if s["chat"] == SOLO]
        self.assertEqual([(s["reply_to"], s["root"]) for s in out], [(None, None)])

    def test_an_answer_to_a_line_not_from_this_chat_or_account_is_not_sent(self):
        h = channel(self)
        other = say(h, "/agent 别的群里问的", chat=TEAM)  # the /agent path's, not the channel's
        m = say(h, "/agent 帮我查一下")
        received(h)
        for reply_to in ("msg_example_nowhere", other.id):
            with self.assertRaises(HubSideError) as e:
                send(h, ref="x-" + reply_to, text="回答", kind="answer", reply_to=reply_to)
            self.assertEqual(e.exception.code, "cannot_send")
        h.app.store.wipe_account_data("user_example_other")  # another account signed in
        with self.assertRaises(HubSideError) as e:
            send(h, ref="x2", text="回答", kind="answer", reply_to=m.id)
        self.assertEqual(e.exception.code, "cannot_send")
        self.assertEqual([s for s in h.sent() if "回答" in s["text"]], [])

    def test_not_signed_in_cannot_send(self):
        world = FixtureWorld()
        world.add_chat(Chat(SOLO, "group", SOLO_NAME, members=1))
        h = Harness(self, config={"agent": True, "channel_chat": SOLO}, world=world)
        with self.assertRaises(HubSideError) as e:
            send(h, ref="r1", text="你好", kind="push")
        self.assertEqual(e.exception.code, "cannot_send")


class AskTest(unittest.TestCase):
    def ask(self, h, ix="ix_example_1", reply_to=None, item=None):
        h.http.interactions = [item or waiting(ix)]
        params = dict(ref="ask:" + ix, text="Agent 想运行 make test。回复「允许」或「拒绝」。", kind="ask")
        if reply_to:
            params["reply_to"] = reply_to
        res = send(h, **params)
        wait_until(lambda: (h.app.store.channel_ask(ix, "ask:" + ix) or {}).get("seen"), what="seen waiting")
        return res.get("message_id", "")

    def test_a_question_answered_elsewhere_is_taken_back(self):
        h = channel(self)
        m = say(h, "/agent 跑一下测试")
        received(h)
        mid = self.ask(h, reply_to=m.id)
        out = [s for s in h.sent() if s["id"] == mid][0]
        self.assertEqual(out["reply_to"], m.id)  # under the request it is about
        time.sleep(0.2)
        self.assertEqual(h.world.recalls, [], "still waiting: kept")
        answered_elsewhere(h, "ix_example_1")
        self.assertEqual(ended(h, "ix_example_1"), "recalled")
        self.assertEqual(h.world.recalls, [(SOLO, mid)])
        time.sleep(0.2)
        self.assertEqual(h.world.recalls, [(SOLO, mid)], "once")

    def test_missing_from_the_list_is_not_enough_the_history_must_say_it_ended(self):
        h = channel(self)
        self.ask(h)
        h.http.interactions = []  # a list rebuilt after a restart, the history says nothing yet
        n = h.http.interactions_asked()
        wait_until(lambda: h.http.interactions_asked() > n + 3, what="a few rounds")
        self.assertEqual(h.world.recalls, [])
        self.assertEqual(h.app.store.channel_ask_state("ix_example_1", "ask:ix_example_1"), "open")
        h.http.history.append({"interaction": waiting("ix_example_1"), "status": "pending"})
        n = h.http.interactions_asked()
        wait_until(lambda: h.http.interactions_asked() > n + 3, what="a few rounds")
        self.assertEqual(h.world.recalls, [])
        h.http.history = [{"interaction": waiting("ix_example_1"), "status": "expired"}]
        self.assertEqual(ended(h, "ix_example_1"), "recalled")

    def test_a_question_answered_here_stays(self):
        h = channel(self)
        mid = self.ask(h)
        a = say(h, "允许")  # a reply while it waits: its answer
        self.assertEqual(received(h)["message"], {"id": a.id, "text": "允许"})
        answered_elsewhere(h, "ix_example_1")  # Persona resolved it with that answer
        self.assertEqual(ended(h, "ix_example_1"), "answered_here")
        time.sleep(0.2)
        self.assertEqual(h.world.recalls, [])
        self.assertTrue(any(s["id"] == mid for s in h.sent()))
        line = say(h, "这句不是答案了")
        self.assertEqual(received(h, 2)["message"]["id"], line.id, "Persona's to read as it will")

    def test_a_line_said_before_a_new_question_is_not_its_answer(self):
        h = channel(self, timing=fast(channel_poll=0.05, channel_retry=(0.5,), channel_unseen=0.3))
        self.ask(h)
        tries = [1]

        def flaky(p):
            if tries[0]:
                tries[0] -= 1
                raise HubSideError("", "the assistant is away")
        h.hub.receive_answer = flaky
        line = say(h, "允许")  # said for the first question
        wait_until(lambda: h.hub.receive_calls >= 1, what="tried once")
        h.http.interactions.append(waiting("ix_example_2"))
        send(h, ref="ask:ix_example_2", text="Agent 想删除 build 目录。回复「允许」或「拒绝」。", kind="ask")
        self.assertEqual(line_state(h, line.id), "dropped")
        self.assertEqual(h.hub.received, [], "never read as the second question's answer")
        note = wait_until(lambda: [x for x in h.sent() if "新问题" in x["text"]], what="the note")[0]
        self.assertEqual(note["reply_to"], line.id)

    def test_a_refused_answer_answers_nothing(self):
        h = channel(self)
        self.ask(h)
        h.hub.receive_answer = lambda p: (_ for _ in ()).throw(HubSideError("invalid_params", "refused"))
        line = say(h, "允许")
        self.assertEqual(line_state(h, line.id), "failed")  # it answered nothing: taken back all the same
        h.hub.receive_answer = None
        answered_elsewhere(h, "ix_example_1")
        self.assertEqual(ended(h, "ix_example_1"), "recalled")

    def test_new_with_text_may_answer_a_bare_command_does_not(self):
        h = channel(self)
        self.ask(h)
        say(h, "/agent /status")
        received(h)
        self.assertEqual(h.app.store.channel_ask("ix_example_1", "ask:ix_example_1")["here"], "")
        line = say(h, "/agent /new 允许")
        self.assertEqual(received(h, 2)["message"]["text"], "/new 允许")
        self.assertEqual(line_state(h, line.id), "handed")
        self.assertEqual(h.app.store.channel_ask("ix_example_1", "ask:ix_example_1")["here"], line.id)
        answered_elsewhere(h, "ix_example_1")
        self.assertEqual(ended(h, "ix_example_1"), "answered_here")
        self.assertEqual(h.world.recalls, [])

    def test_a_question_never_seen_waiting_is_never_taken_back(self):
        h = channel(self)
        h.http.interactions = []  # one the hub does not show extensions
        send(h, ref="ask:ix_example_hidden", text="请在 App 里填写表单。", kind="ask")
        self.assertEqual(ended(h, "ix_example_hidden"), "unseen")
        time.sleep(0.2)
        self.assertEqual(h.world.recalls, [])

    def test_a_question_not_taken_back_is_marked_ended(self):
        h = channel(self)
        h.world.recall_fail = True
        mid = self.ask(h)
        answered_elsewhere(h, "ix_example_1")
        self.assertEqual(ended(h, "ix_example_1"), "edited")
        edit = [e for e in h.world.edits if e[1] == mid][0]
        self.assertIn("已经在别处处理", edit[2])
        self.assertIn(HEADER, edit[2])

    def test_the_hub_unreadable_takes_nothing_back(self):
        h = channel(self)
        self.ask(h)
        h.http.interactions_status = 503
        answered_elsewhere(h, "ix_example_1")
        n = h.http.interactions_asked()
        wait_until(lambda: h.http.interactions_asked() > n + 2, what="a few reads")
        self.assertEqual(h.world.recalls, [])

    def test_a_former_channel_chats_question_is_left_alone(self):
        h = channel(self)
        self.ask(h)
        h.app.channel.setting = "另一个群"  # the setting changed (the extension starts again with it)
        h.app.channel._checked = None
        answered_elsewhere(h, "ix_example_1")
        self.assertEqual(ended(h, "ix_example_1"), "dropped")
        self.assertEqual((h.world.recalls, h.world.edits), ([], []))

    def test_a_question_cut_off_while_taken_back_is_taken_back_after_a_restart(self):
        h = channel(self)
        mid = self.ask(h)
        h.app.store.channel_ask_set("ix_example_1", "ask:ix_example_1", state="recalling")
        h2 = h.restart()
        h2.wait_ok()
        answered_elsewhere(h2, "ix_example_1")
        self.assertEqual(ended(h2, "ix_example_1"), "recalled")
        self.assertEqual(h2.world.recalls, [(SOLO, mid)])

    def test_a_question_written_down_late_is_followed_on_the_same_ref(self):
        h = channel(self)
        mid = self.ask(h)
        with h.app.store._lock:
            h.app.store._tx(lambda db: db.execute("DELETE FROM channel_asks"))  # cut off before it was written down
        again = send(h, ref="ask:ix_example_1", text="Agent 想运行 make test。回复「允许」或「拒绝」。", kind="ask")
        self.assertEqual(again["message_id"], mid)
        self.assertEqual(h.app.store.channel_ask("ix_example_1", "ask:ix_example_1")["message_id"], mid)
        self.assertEqual(len([s for s in h.sent() if s["id"] == mid]), 1)

    def test_a_question_sent_without_a_message_id_still_takes_its_answer(self):
        h = channel(self)
        backend = h.app.backend
        real = backend.send
        backend.send = lambda *a, **k: (real(*a, **k), "")[1]  # WeChat may not say the new message's id
        self.assertEqual(self.ask(h), "")
        a = say(h, "允许")
        self.assertEqual(received(h)["message"]["id"], a.id)
        self.assertEqual(line_state(h, a.id), "handed")
        self.assertEqual(h.app.store.channel_ask("ix_example_1", "ask:ix_example_1")["here"], a.id)
        answered_elsewhere(h, "ix_example_1")
        self.assertEqual(ended(h, "ix_example_1"), "answered_here")

    def test_the_history_is_read_page_after_page(self):
        h = channel(self)
        self.ask(h)
        h.http.interactions = []
        h.http.history = [{"interaction": waiting("ix_example_1"), "status": "resolved"}] + [
            {"interaction": waiting("ix_newer_%d" % i), "status": "resolved"} for i in range(450)]  # newer ones first
        self.assertEqual(ended(h, "ix_example_1"), "recalled")

    def test_two_calls_for_one_ref_at_once_send_it_once(self):
        import threading
        h = channel(self)
        h.world.send_delay = 0.5
        out = []
        ts = [threading.Thread(target=lambda: out.append(send(h, ref="slow1", text="慢慢发", kind="push")))
              for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0], out[1])
        self.assertEqual(len([x for x in h.sent() if "慢慢发" in x["text"]]), 1)

    def test_a_ref_first_tried_under_another_account_is_not_sent(self):
        h = channel(self)
        h.world.send_fail = "rejected"
        with self.assertRaises(HubSideError) as e:
            send(h, ref="push9", text="换号前的通知", kind="push")
        self.assertNotEqual(e.exception.code, "cannot_send")  # surely not sent: the hub tries again
        with h.app.lock:
            h.app._account_epoch += 1  # another account signed in meanwhile
        with self.assertRaises(HubSideError) as e:
            send(h, ref="push9", text="换号前的通知", kind="push")
        self.assertEqual(e.exception.code, "cannot_send")
        self.assertEqual([x for x in h.sent() if "换号前" in x["text"]], [])

    def test_a_replay_writes_nothing_down_for_another_account(self):
        h = channel(self)
        mid = self.ask(h)
        h.app.store._tx(lambda db: db.execute("DELETE FROM channel_asks"))
        h.app._owner = "user_example_other"  # the store is another account's now
        again = send(h, ref="ask:ix_example_1", text="Agent 想运行 make test。回复「允许」或「拒绝」。", kind="ask")
        self.assertEqual(again["message_id"], mid)  # the same answer
        self.assertIsNone(h.app.store.channel_ask("ix_example_1", "ask:ix_example_1"))

    def test_wechat_answers_and_asks_but_takes_nothing_back(self):
        h = channel(self, platform=openmate_wechat.platform(), agent_reply_header=" ")
        from openmate_wechat.backend import WeChatBackend
        self.assertFalse(WeChatBackend.can_recall)
        h.world.recall_ok = False  # as WeChat's own backend: nothing can be taken back
        self.assertEqual(h.cid, "matewechat")
        m = say(h, "/agent 你好")
        self.assertEqual(received(h)["contribution"], "matewechat")
        send(h, ref="run1:0", text="你好！", kind="answer", reply_to=m.id)
        mid = self.ask(h, reply_to=m.id)
        answered_elsewhere(h, "ix_example_1")
        self.assertEqual(ended(h, "ix_example_1"), "ended")
        time.sleep(0.2)
        self.assertEqual(h.world.recalls, [])
        self.assertEqual([e for e in h.world.edits if e[1] == mid], [])

    def test_another_account_forgets_the_channels_messages(self):
        h = channel(self)
        m = say(h, "/agent 跑一下测试")
        received(h)
        self.ask(h, reply_to=m.id)
        store = h.app.store
        self.assertTrue(store.channel_asks_open(SOLO))
        store.wipe_account_data("user_example_other")
        self.assertEqual(store.channel_asks_open(SOLO), [])
        self.assertIsNone(store.channel_msg(SOLO, m.id))


if __name__ == "__main__":
    unittest.main()
