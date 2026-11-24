"""Persona's 渠道 (the channel extension point, docs/extensions/channels.md "Contributing a channel"): both extensions
declare one on their connector; it works only in the chat the user named in 「Persona 渠道会话」 (none by default, and
then nothing is sent). There the user's /agent goes to Persona's channel flow (channel.receive) instead of an /agent
conversation, Persona's answers come back through channel.send where they were asked, and a question Persona put
there is taken back once it was answered somewhere else (Feishu; WeChat cannot take a message back)."""

try:  # first: every temporary directory of this run in one, removed at its end
    import _runtmp  # noqa: F401
except ImportError:  # (started by its path from elsewhere: its directory is not on the path)
    pass
import base64
import json
import os
import sys
import time
import unittest

from harness import CID, ROOT, Harness, HubSideError, fast, wait_until
from openmate_core.backend import Chat
from openmate_core.fixture import FixtureWorld, tiny_png

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
    """A signed-in run whose 「Persona 渠道会话」 is the group only the user is in (by its id)."""
    world = world or FixtureWorld()
    world.add_chat(Chat(SOLO, "group", SOLO_NAME, members=1))
    world.make(SOLO, "me", "这个群只有我自己", ago=3000)
    config = {"agent": True, "channel_chat": SOLO, "agent_ack_reaction": ""}
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


def png_b64(size=2):
    return base64.b64encode(tiny_png(size=size)).decode("ascii")


def pictured_channel(test, **kw):
    """The 渠道 on a backend that sends pictures where a text would go (Feishu's)."""
    world = FixtureWorld()
    world.images_ok = world.image_threads = True
    return channel(test, world=world, **kw)


class PicturesOutTest(unittest.TestCase):
    """images_out (#11986): the pictures of an answer follow its text, where it was asked, each once."""

    def test_feishu_declares_images_out_and_wechat_does_not(self):
        caps = {ext: manifest(ext)["contributes"]["channel"][0]["capabilities"]
                for ext in ("openmate-feishu", "openmate-wechat")}
        self.assertIs(caps["openmate-feishu"].get("images_out"), True)
        self.assertNotIn("images_out", caps["openmate-wechat"], "WeChat's channel is as it was")

    def test_after_the_text_where_it_was_asked_each_once(self):
        h = pictured_channel(self)
        m = say(h, "/agent 画个流程图")
        received(h)
        params = dict(ref="run1:0", text="流程图如下。", kind="answer", reply_to=m.id, images=[
            {"mime": "image/png", "data": png_b64(2)}, {"mime": "image/png", "data": png_b64(3)}])
        res = send(h, **params)
        text = [s for s in h.sent() if s["chat"] == SOLO]
        self.assertEqual(len(text), 1)
        self.assertEqual(res["message_id"], text[0]["id"], "the text's id")
        pics = list(h.world.images)
        self.assertEqual([(p["chat"], p["reply_to"], p["root"]) for p in pics], [(SOLO, m.id, None)] * 2,
                         "where the text went")
        self.assertEqual(send(h, **params), res)
        self.assertEqual((len(h.world.images), len([s for s in h.sent() if s["chat"] == SOLO])), (2, 1), "each once")
        rec = wait_until(lambda: [r for r in h.hub.sent_records if r.get("setting") == "channel"] if len(
            [r for r in h.hub.sent_records if r.get("setting") == "channel"]) >= 3 else None, what="the ledger")
        self.assertEqual(sorted(r["text"] for r in rec)[:2], ["[图片]", "[图片]"])
        time.sleep(0.2)
        self.assertEqual(len(h.hub.received), 1, "the pictures' echoes are no request")

    def test_in_a_thread_and_pictures_alone(self):
        h = pictured_channel(self)
        root = h.world.push(SOLO, "user_example_a", "这周的事")
        m = say(h, "/agent 截个图", root_id=root.id, parent_id=root.id, placement="thread")
        received(h)
        res = send(h, ref="run2:1", text="", kind="answer", reply_to=m.id, images=[{"mime": "image/png", "data": png_b64()}])
        self.assertEqual([(p["reply_to"], p["root"]) for p in h.world.images], [(m.id, root.id)])
        self.assertEqual(res["message_id"], h.world.images[0]["id"])
        self.assertEqual([s for s in h.sent() if s["chat"] == SOLO and s["text"].strip()], [], "no empty text sent")
        push = send(h, ref="push1", text="", kind="push", images=[{"mime": "image/png", "data": png_b64()}])
        self.assertEqual((h.world.images[-1]["reply_to"], h.world.images[-1]["root"]), (None, None), "into the chat")
        self.assertEqual(push["message_id"], h.world.images[-1]["id"])

    def test_a_refused_picture_goes_again_with_the_ref_and_the_text_does_not(self):
        h = pictured_channel(self)
        m = say(h, "/agent 画个图")
        received(h)
        params = dict(ref="run3:0", text="图在下面。", kind="answer", reply_to=m.id,
                      images=[{"mime": "image/png", "data": png_b64()}])
        h.world.image_fail = "rejected"
        with self.assertRaises(HubSideError):
            send(h, **params)
        self.assertEqual((len(h.world.images), len([s for s in h.sent() if s["chat"] == SOLO])), (0, 1))
        send(h, **params)
        self.assertEqual((len(h.world.images), len([s for s in h.sent() if s["chat"] == SOLO])), (1, 1),
                         "the picture this time, the text not again")
        h.world.image_fail = "uncertain"
        params.update(ref="run3:1", images=[{"mime": "image/png", "data": png_b64(4)}])
        with self.assertRaises(HubSideError):
            send(h, **params)
        n = len(h.world.images)
        with self.assertRaises(HubSideError):
            send(h, **params)  # an outcome nobody knows: never sent again
        self.assertEqual(len(h.world.images), n)

    def test_what_is_no_png_or_jpeg_is_left_out_and_without_images_out_nothing_changes(self):
        h = pictured_channel(self)
        send(h, ref="p1", text="看图", kind="push", images=[{"mime": "image/gif", "data": base64.b64encode(
            b"GIF89a" + b"\0" * 20).decode()}, {"mime": "image/png", "data": "不是 base64"}, {"data": png_b64()}])
        self.assertEqual(len(h.world.images), 1, "only the PNG")
        h.world.images_ok = False  # a backend that cannot send pictures now: the text alone
        send(h, ref="p2", text="又一张", kind="push", images=[{"mime": "image/png", "data": png_b64()}])
        self.assertEqual(len(h.world.images), 1)
        self.assertEqual(send(h, ref="p3", text="", kind="push", images=[{"mime": "image/png", "data": png_b64()}]), {})
        w = channel(self, platform=openmate_wechat.platform(), agent_reply_header=" ")
        w.world.images_ok = True
        send(w, ref="p4", text="微信", kind="push", images=[{"mime": "image/png", "data": png_b64()}])
        self.assertEqual(w.world.images, [], "WeChat's channel takes no pictures (no images_out)")
        self.assertEqual(len([s for s in w.sent() if s["text"].startswith("微信")]), 1)


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

    def test_a_chat_not_stored_or_a_name_is_no_chat(self):
        for setting in ("chat_example_nowhere", SOLO_NAME):
            h = channel(self, channel_chat=setting)
            with self.assertRaises(HubSideError) as e:
                send(h, ref="r1", text="你好", kind="push")
            self.assertEqual(e.exception.code, "cannot_send")
            say(h, "/agent 你好")
            wait_until(lambda: h.http.starts(), what="the /agent way")
            self.assertEqual(h.hub.received, [])

    def test_a_chat_left_is_no_chat(self):
        h = channel(self, channel_chat=SOLO)
        h.app.store.chat_gone(SOLO)
        wait_until(lambda: h.app.store.is_gone(SOLO), what="gone")
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

    def test_a_ref_whose_outcome_is_unknown_is_never_sent_again(self):
        h = channel(self)
        h.world.send_fail = "uncertain"
        with self.assertRaises(HubSideError):
            send(h, ref="u1", text="不知道发没发出去", kind="push")
        h.app.channel.setting = "没有这个群"  # whatever changed since, the journal answers first
        with self.assertRaises(HubSideError) as e:
            send(h, ref="u1", text="不知道发没发出去", kind="push")
        self.assertNotEqual(e.exception.code, "cannot_send")
        self.assertIn("不确定", e.exception.message)

    def test_every_piece_of_a_question_is_one_accounts(self):
        h = channel(self)
        h.http.interactions = [waiting("ix_example_long")]
        send(h, ref="ask:ix_example_long#0", text="很长的问题，第一段。", kind="ask")
        h.app._owner = "user_example_other"  # another account by now
        with self.assertRaises(HubSideError) as e:
            send(h, ref="ask:ix_example_long#1", text="第二段。", kind="ask")
        self.assertEqual(e.exception.code, "cannot_send")

    def test_a_ref_is_bound_to_its_account_for_good(self):
        h = channel(self)
        h.world.send_fail = "rejected"
        with self.assertRaises(HubSideError):
            send(h, ref="b1", text="换号前的通知", kind="push")
        h.app._owner = "user_example_other"  # another account's store (a restart keeps the binding: it is stored)
        with self.assertRaises(HubSideError) as e:
            send(h, ref="b1", text="换号前的通知", kind="push")
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

    def test_the_line_goes_as_said_and_an_agent_stop_is_the_channels_stop(self):
        h = channel(self)
        m = say(h, "@示例同事甲 这个先别动")
        self.assertEqual(received(h)["message"]["text"], "@示例同事甲 这个先别动")
        say(h, "/agent /停止")
        self.assertEqual(received(h, 2)["message"]["text"], "/stop")
        self.assertTrue(m.id)

    def test_what_this_program_sent_or_another_ai_said_is_not_the_users(self):
        h = channel(self)
        h.app.send_as_user(SOLO, "通过工具以你的身份发的一句")  # a tool's send or an inbox reply: its echo comes back
        say(h, "🤖 AI · Assistant：这是别的 AI 写的")
        time.sleep(0.4)
        self.assertEqual(h.hub.received, [])
        m = say(h, "这句是我说的")
        self.assertEqual(received(h)["message"]["id"], m.id)

    def test_a_users_line_like_a_tools_send_is_still_theirs(self):
        h = channel(self)
        h.app.send_as_user(SOLO, "收到")  # a tool's send: its echo is not the user's
        time.sleep(0.2)
        self.assertEqual(h.hub.received, [])
        m = say(h, "收到")  # the user's own, later: theirs
        self.assertEqual(received(h)["message"]["id"], m.id)

    def test_an_agent_stop_of_a_request_from_before_stops_that_request(self):
        h = channel(self)
        root = h.world.push(SOLO, "user_example_a", "这周的事")
        old = h.app.agent._new_run({"chat_id": SOLO, "id": root.id}, "以前的请求", time.time())
        old["state"] = "running"
        h.app.store.run_put(old)  # an /agent request asked in this thread before the channel
        say(h, "/agent /停止", root_id=root.id, parent_id=root.id, placement="thread")
        wait_until(lambda: h.app.store.run_get(old["id"])["state"] == "stopped", what="the old request stopped")
        time.sleep(0.2)
        self.assertEqual(h.hub.received, [])
        self.assertEqual(h.http.starts(), [], "no new /agent request for it")

    def test_a_picture_goes_as_unsupported_and_a_bare_quote_answers_nothing(self):
        h = channel(self)
        from openmate_core.backend import Media
        pic = say(h, "[图片]", type="IMAGE", media=[Media("image", ref="img:example-9", mime="image/png")])
        self.assertEqual(received(h)["message"], {"id": pic.id, "unsupported": ["图片（这个渠道还转交不了，请在 App 里发）"]})

    def test_a_picture_without_words_and_a_sticker_go_as_unsupported(self):
        h = channel(self)
        pic = say(h, "", type="IMAGE")  # Feishu: a picture has no text at all
        self.assertEqual(received(h)["message"], {"id": pic.id, "unsupported": ["图片（这个渠道还转交不了，请在 App 里发）"]})
        st = say(h, "[表情]", type="STICKER")  # WeChat: a sticker's words stand for it, with no media
        self.assertEqual(received(h, 2)["message"], {"id": st.id, "unsupported": ["表情（这个渠道还转交不了，请在 App 里发）"]})

    def test_an_agent_reply_elsewhere_does_not_swallow_the_users_words_here(self):
        h = channel(self)
        h.app.agent._remember_post("收到")  # an /agent reply in another chat said just that
        m = say(h, "收到")
        self.assertEqual(received(h)["message"]["id"], m.id)

    def test_a_line_heard_before_a_sign_out_is_not_taken_after_it(self):
        h = channel(self)
        m = h.world.make(SOLO, "me", "/stop")  # heard (and stored) before the sign-out, its turn comes after
        row = {"chat_id": SOLO, "id": m.id, "text": "/stop", "mine": True, "at": time.time(), "received": time.time()}
        time.sleep(0.01)
        h.app.channel.forget_pending()
        self.assertTrue(h.app.channel.take(row, "/stop"))
        self.assertIsNone(h.app.store.channel_msg(SOLO, m.id))

    def test_a_channel_push_does_not_swallow_the_users_same_words(self):
        h = channel(self, agent_reply_header=" ")  # no AI mark: only the id tells the echo
        send(h, ref="push1", text="收到", kind="push")
        time.sleep(0.2)
        self.assertEqual(h.hub.received, [], "its echo")
        m = say(h, "收到")
        self.assertEqual(received(h)["message"]["id"], m.id)

    def test_signing_out_lets_the_lines_not_handed_over_go(self):
        h = channel(self, timing=fast(channel_poll=0.05, channel_retry=(30.0,), channel_unseen=0.3))
        h.hub.receive_answer = lambda p: (_ for _ in ()).throw(HubSideError("", "the assistant is away"))
        m = say(h, "/stop")
        wait_until(lambda: h.hub.receive_calls >= 1, what="tried")
        h.c("logout")
        self.assertEqual(h.app.store.channel_msg(SOLO, m.id)["state"], "expired")
        h.hub.receive_answer = None
        h.app.channel._queued.set()
        time.sleep(0.3)
        self.assertEqual(h.hub.received, [])

    def test_a_refused_hand_over_is_said_in_the_chat(self):
        h = channel(self)
        h.hub.receive_answer = lambda p: (_ for _ in ()).throw(HubSideError("permission_denied", "not a channel"))
        m = say(h, "/agent 你好")
        note = wait_until(lambda: [s for s in h.sent() if s["chat"] == SOLO and "没能交给 Persona" in s["text"]],
                          what="the note")[0]
        self.assertEqual(note["reply_to"], m.id)
        self.assertIn(HEADER, note["text"])

    def test_a_lost_answer_is_tried_again_with_the_same_id_till_too_old(self):
        h = channel(self)
        fails = [3]

        def flaky(p):
            if fails[0]:
                fails[0] -= 1
                raise HubSideError("timeout", "no answer")
        h.hub.receive_answer = flaky
        m = say(h, "/agent 你好")
        self.assertEqual(received(h)["message"]["id"], m.id)
        self.assertEqual(h.hub.receive_calls, 4)
        h2 = channel(self, timing=fast(channel_poll=0.05, channel_retry=(0.05,), channel_unseen=0.3, agent_fresh=1.0))
        h2.hub.receive_answer = lambda p: (_ for _ in ()).throw(HubSideError("timeout", "no answer"))
        m2 = say(h2, "/agent 你好")
        note = wait_until(lambda: [s for s in h2.sent() if "很久都没能确认" in s["text"]], timeout=10, what="the note")[0]
        self.assertEqual(note["reply_to"], m2.id)
        self.assertEqual(h2.app.store.channel_msg(SOLO, m2.id)["state"], "expired")
        self.assertIn("message_id", send(h2, ref="late1", text="迟到的回答", kind="answer", reply_to=m2.id),
                      "Persona may have had it after all: its answer is taken")

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

    def test_an_answer_only_to_a_line_handed_over_not_to_any_line_of_the_users(self):
        """larkx 0.2.1 (nwverse/persona-extensions#533): an answer goes only under a request that was handed to
        Persona, never under any other line of the user's in the chat: not the answers sent there before, not the
        echo of a tool's send, not a line another AI wrote, not a line not handed over."""
        h = channel(self)
        m = say(h, "/agent 帮我查一下")
        received(h)
        first = send(h, ref="r0", text="查到了。", kind="answer", reply_to=m.id)["message_id"]
        tool = h.app.send_as_user(SOLO, "通过工具以你的身份发的一句")
        ai = say(h, "🤖 AI · Assistant：这是别的 AI 写的")
        time.sleep(0.3)
        self.assertEqual(len(h.hub.received), 1, "neither of those was handed over")
        for i, reply_to in enumerate((first, tool, ai.id)):
            with self.assertRaises(HubSideError) as e:
                send(h, ref="g%d" % i, text="回答", kind="answer", reply_to=reply_to)
            self.assertEqual(e.exception.code, "cannot_send", reply_to)
        self.assertEqual([s for s in h.sent() if s["text"].startswith("回答")], [])

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
        note = wait_until(lambda: [x for x in h.sent() if "又提了一个问题" in x["text"]], what="the note")[0]
        self.assertEqual(note["reply_to"], line.id)

    def test_an_answer_handed_over_is_not_undone_by_a_later_refused_line(self):
        h = channel(self)
        self.ask(h)
        first = say(h, "允许")
        self.assertEqual(line_state(h, first.id), "handed")
        h.hub.receive_answer = lambda p: (_ for _ in ()).throw(HubSideError("invalid_params", "refused"))
        second = say(h, "再说一句")
        self.assertEqual(line_state(h, second.id), "failed")
        answered_elsewhere(h, "ix_example_1")
        self.assertEqual(ended(h, "ix_example_1"), "answered_here")
        self.assertEqual(h.world.recalls, [])

    def test_a_question_waits_for_a_line_being_handed_over(self):
        import threading
        h = channel(self)
        started = threading.Event()

        def slow(p):
            started.set()
            time.sleep(0.6)
        h.hub.receive_answer = slow
        say(h, "/agent 你好")
        self.assertTrue(started.wait(5))
        t0 = time.monotonic()
        h.http.interactions = [waiting("ix_example_1")]
        send(h, ref="ask:ix_example_1", text="Agent 想运行 make test。回复「允许」或「拒绝」。", kind="ask")
        self.assertGreater(time.monotonic() - t0, 0.3, "sent after the hand-over, not between its check and it")

    def test_an_answer_quoting_an_older_question_is_not_given_to_a_newer_one(self):
        h = channel(self)
        old = self.ask(h, "ix_example_old")
        time.sleep(0.02)
        h.http.interactions.append(waiting("ix_example_new"))
        send(h, ref="ask:ix_example_new", text="Agent 想删除 build 目录。回复「允许」或「拒绝」。", kind="ask")
        line = say(h, "允许", parent_id=old)  # for the older one, heard after the newer one came
        self.assertEqual(line_state(h, line.id), "dropped")
        self.assertEqual(h.hub.received, [])

    def test_a_second_piece_of_the_same_question_is_no_new_question(self):
        h = channel(self, timing=fast(channel_poll=0.05, channel_retry=(0.4,), channel_unseen=0.3))
        h.http.interactions = [waiting("ix_example_long")]
        send(h, ref="ask:ix_example_long#0", text="很长的问题，第一段。", kind="ask")
        tries = [1]

        def flaky(p):
            if tries[0]:
                tries[0] -= 1
                raise HubSideError("timeout", "no answer")
        h.hub.receive_answer = flaky
        line = say(h, "允许")
        wait_until(lambda: h.hub.receive_calls >= 1, what="tried once")
        send(h, ref="ask:ix_example_long#1", text="第二段：回复「允许」或「拒绝」。", kind="ask")
        self.assertEqual(line_state(h, line.id), "handed")

    def test_a_question_cut_off_before_it_was_written_down_is_followed_after_a_restart(self):
        h = channel(self)
        mid = self.ask(h)
        h.app.store._tx(lambda db: db.execute("DELETE FROM channel_asks"))
        h2 = h.restart()
        h2.wait_ok()
        self.assertEqual(h2.app.store.channel_ask("ix_example_1", "ask:ix_example_1")["message_id"], mid)

    def test_a_question_not_taken_back_for_now_is_tried_again(self):
        h = channel(self)
        h.world.recall_fail = True
        h.world.edit_fail = True  # the service away for a while
        mid = self.ask(h)
        answered_elsewhere(h, "ix_example_1")
        wait_until(lambda: h.app.store.channel_ask("ix_example_1", "ask:ix_example_1")["tries"] >= 1, what="tried")
        h.world.recall_fail = False
        h.world.edit_fail = False
        self.assertEqual(ended(h, "ix_example_1"), "recalled")
        self.assertEqual(h.world.recalls, [(SOLO, mid)])

    def test_a_quote_alone_answers_nothing(self):
        h = channel(self)
        self.ask(h)
        quoted = h.app.store.messages_before(SOLO, None, 5)[0]
        line = say(h, "/agent", parent_id=quoted["id"])
        self.assertEqual(line_state(h, line.id), "handed")
        self.assertEqual(h.app.store.channel_ask("ix_example_1", "ask:ix_example_1")["here"], "")

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
        before = time.time()
        again = send(h, ref="ask:ix_example_1", text="Agent 想运行 make test。回复「允许」或「拒绝」。", kind="ask")
        self.assertEqual(again["message_id"], mid)
        row = h.app.store.channel_ask("ix_example_1", "ask:ix_example_1")
        self.assertEqual(row["message_id"], mid)
        self.assertLess(row["at"], before, "when it was sent, not when it was written down again")
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
            {"interaction": waiting("ix_newer_%d" % i), "status": "resolved"} for i in range(2500)]  # 13 pages newer
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
