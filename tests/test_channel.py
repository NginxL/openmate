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

from harness import CID, ROOT, Harness, HubSideError, wait_until
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


def channel(test, platform=None, **cfg):
    """A signed-in run whose 「Persona 渠道会话」 is the group only the user is in (by its name)."""
    world = FixtureWorld()
    world.add_chat(Chat(SOLO, "group", SOLO_NAME, members=1))
    world.make(SOLO, "me", "这个群只有我自己", ago=3000)
    config = {"agent": True, "channel_chat": SOLO_NAME, "agent_ack_reaction": ""}
    config.update(cfg)
    h = Harness(test, config=config, world=world, platform=platform)
    h.sign_in()
    wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
    return h


def say(h, text, chat=SOLO, **kw):
    return h.world.push(chat, "me", text, **kw)


def received(h, n=1):
    wait_until(lambda: len(h.hub.received) >= n, what="%d message(s) handed to Persona" % n)
    return h.hub.received[n - 1]


def ended(h, ix, mid):
    """How the channel's question ended, once it did."""
    return wait_until(lambda: (lambda st: st if st not in ("", "open", "recalling") else None)(
        h.app.store.channel_ask_state(ix, mid)), what="the question ended")


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
        with self.assertRaises(HubSideError) as e:
            send(h2, ref="r2", text="你好", kind="push")
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

    def test_only_the_users_own_lines_that_are_for_persona(self):
        h = channel(self)
        h.world.push(SOLO, "user_example_a", "/agent 别人发的")
        say(h, "随手记一句，不是给 Persona 的")
        say(h, "/agent")  # nothing to hand over
        say(h, "/agent 这是一条代答\n\n> " + HEADER)  # an AI's own line
        time.sleep(0.3)
        self.assertEqual(h.hub.received, [])
        self.assertEqual(h.http.starts(), [])

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
        root = say(h, "这周的事")
        m = say(h, "/agent 总结一下这个话题", root_id=root.id, parent_id=root.id, placement="thread")
        received(h)
        send(h, ref="run2:0", text="总结如下。", kind="answer", reply_to=m.id)
        out = [s for s in h.sent() if s["chat"] == SOLO][-1]
        self.assertEqual((out["reply_to"], out["root"], out["to_chat"]), (m.id, root.id, False))

    def test_without_a_known_request_it_goes_into_the_chat(self):
        h = channel(self)
        send(h, ref="push1", text="后台任务做完了。", kind="push")
        send(h, ref="x1", text="未知请求的回答", kind="answer", reply_to="msg_example_nowhere")
        out = [s for s in h.sent() if s["chat"] == SOLO]
        self.assertEqual([(s["reply_to"], s["root"]) for s in out], [(None, None), (None, None)])

    def test_not_signed_in_cannot_send(self):
        world = FixtureWorld()
        world.add_chat(Chat(SOLO, "group", SOLO_NAME, members=1))
        h = Harness(self, config={"agent": True, "channel_chat": SOLO}, world=world)
        with self.assertRaises(HubSideError) as e:
            send(h, ref="r1", text="你好", kind="push")
        self.assertEqual(e.exception.code, "cannot_send")


class AskTest(unittest.TestCase):
    def ask(self, h, ix="ix_example_1", reply_to=None):
        h.http.interactions = [{"id": ix, "kind": "permission"}]
        params = dict(ref="ask:" + ix, text="Agent 想运行 make test。回复「允许」或「拒绝」。", kind="ask")
        if reply_to:
            params["reply_to"] = reply_to
        res = send(h, **params)
        n = h.http.interactions_asked()
        wait_until(lambda: h.http.interactions_asked() > n, what="the waiting questions read")
        return res["message_id"]

    def test_a_question_answered_elsewhere_is_taken_back(self):
        h = channel(self)
        m = say(h, "/agent 跑一下测试")
        received(h)
        mid = self.ask(h, reply_to=m.id)
        out = [s for s in h.sent() if s["id"] == mid][0]
        self.assertEqual(out["reply_to"], m.id)  # under the request it is about
        time.sleep(0.2)
        self.assertEqual(h.world.recalls, [], "still waiting: kept")
        h.http.interactions = []  # answered in the app
        wait_until(lambda: h.world.recalls, what="taken back")
        self.assertEqual(h.world.recalls, [(SOLO, mid)])
        self.assertEqual(ended(h, "ix_example_1", mid), "recalled")
        time.sleep(0.2)
        self.assertEqual(h.world.recalls, [(SOLO, mid)], "once")

    def test_a_question_answered_here_stays(self):
        h = channel(self)
        mid = self.ask(h)
        a = say(h, "允许")  # a reply while it waits: its answer
        self.assertEqual(received(h)["message"], {"id": a.id, "text": "允许"})
        h.http.interactions = []  # Persona resolved it with that answer
        self.assertEqual(ended(h, "ix_example_1", mid), "answered_here")
        time.sleep(0.2)
        self.assertEqual(h.world.recalls, [])
        self.assertTrue(any(s["id"] == mid for s in h.sent()))
        say(h, "这句不是答案了")
        time.sleep(0.3)
        self.assertEqual(len(h.hub.received), 1, "nothing waits any more: an ordinary line")

    def test_a_question_never_seen_waiting_is_never_taken_back(self):
        h = channel(self)
        h.http.interactions = []  # one the hub does not show extensions
        mid = send(h, ref="ask:ix_example_hidden", text="请在 App 里填写表单。", kind="ask")["message_id"]
        self.assertEqual(ended(h, "ix_example_hidden", mid), "unseen")
        time.sleep(0.2)
        self.assertEqual(h.world.recalls, [])

    def test_a_question_not_taken_back_is_marked_ended(self):
        h = channel(self)
        h.world.recall_fail = True
        mid = self.ask(h)
        h.http.interactions = []
        edit = wait_until(lambda: [e for e in h.world.edits if e[1] == mid], what="marked ended")[0]
        self.assertIn("已经在别处处理", edit[2])
        self.assertIn(HEADER, edit[2])

    def test_the_hub_unreadable_takes_nothing_back(self):
        h = channel(self)
        self.ask(h)
        h.http.interactions_status = 503
        h.http.interactions = []
        n = h.http.interactions_asked()
        wait_until(lambda: h.http.interactions_asked() > n + 2, what="a few reads")
        self.assertEqual(h.world.recalls, [])

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
        h.http.interactions = []
        self.assertEqual(ended(h, "ix_example_1", mid), "ended")
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
