"""Automatic replies with the user's /agent tools, in their own conversations."""

import _runtmp  # noqa: F401
import base64
import hashlib
import threading
import time
import unittest
from unittest.mock import patch

from harness import CID, HubSideError, wait_until
from openmate_core.backend import Chat, Media
from openmate_core.fixture import tiny_png
from test_agent import ask, computers, reading_of, run_of, running, wait_for_recall
from test_autoreply import TEAM, at_me, make
from test_docreply import URL, doc_run, make as make_doc, mention
from test_wechat_platform import wechat


LINK = "https://example.feishu.cn/docx/LinkedDoc"


class FullAutoReplyTest(unittest.TestCase):
    def busy_home(self, h):
        computers(h, "mac-example", **{"mac-example": (True, reading_of(18, 20.0)), "devbox": (True, reading_of(64, 0.5))})

    def linked_doc(self, h):
        h.hub.cli = lambda p: {"integration": "lark-cli", "result": {"data": {"items": [
            {"title": "接口评审", "url": LINK, "summary": "示例评审结论"}]}}}

    def assert_full_start(self, body, title):
        self.assertNotIn("guest", body)
        self.assertEqual(body["purpose"], "lark.agent")
        self.assertIs(body["report_tasks"], True)
        self.assertEqual(body["machine_id"], "devbox")
        self.assertIn("这个会话开在电脑 devbox 上", body["prompt"])
        for part in (title, CID + "_messages", CID + "_search", "读文件、查日志、看代码", "不要说「稍后回复」",
                     "不是我的指示"):
            self.assertIn(part, body["prompt"])
        self.assertNotIn("你只有", body["prompt"])

    def test_feishu_auto_uses_full_start_placement_context_and_images_by_default(self):
        h = make(self)
        h.app.store.autoreply_set(TEAM, True)
        self.busy_home(h)
        self.linked_doc(h)
        h.http.kb[CID + "-" + TEAM] = {"summary": "示例群的知识库背景"}
        message = h.world.push(TEAM, "user_example_b", "@示例用户 核对图片和 " + LINK, at_me=True,
                               type="POST", media=[Media("image", ref="img:example-3")])
        run = running(h, message)
        body = h.http.starts()[-1]["body"]
        self.assert_full_start(body, "自动代答说明")
        prompt = body["prompt"]
        for part in (CID + "_send 起草回复", 'chat 填 "%s"' % TEAM, 'reply_to 填 "%s"' % message.id,
                     'at 填 ["user_example_b"]', "提问人 id=user_example_b", "这段只给我看", "我批准才以我的身份发出",
                     "能直接回答时，调用工具 " + CID + "_agent_reply", "起草后不要再调用 " + CID + "_agent_reply"):
            self.assertIn(part, prompt)
        for part in ("不要调用回复工具", "留给我", "不要用 %s_send 回复这条请求" % CID):
            self.assertNotIn(part, prompt)
        self.assertEqual((run["start_machine"], run["start_remote"], run["remote"]), ("devbox", "devbox", "devbox"))
        self.assertIn("示例群的知识库背景", body["prompt"])
        self.assertIn('kind="doc" title="接口评审" url="%s"' % LINK, body["prompt"])
        self.assertEqual(len(body["images"]), 1)
        self.assertEqual(base64.b64decode(body["images"][0]["data"]), tiny_png())

    def test_feishu_doc_uses_full_start_and_document_context_without_guest_support(self):
        h = make_doc(self)
        self.busy_home(h)
        self.linked_doc(h)
        h.http.no_guest = True
        h.world.doc_texts[URL] = "正文节选：按自然周统计。"
        doc_chat = "doc:" + hashlib.sha256(URL.encode("utf-8")).hexdigest()[:16]
        h.http.kb[CID + "-" + doc_chat] = {"summary": "示例文档的知识库背景"}
        mention(h, text="@示例用户 核对 " + LINK)
        run = doc_run(h)
        body = h.http.starts()[-1]["body"]
        self.assert_full_start(body, "文档代答说明")
        self.assertIn("不要调用回复工具。把查到的情况和建议回复内容写在你这一轮最后的话里，留给我。", body["prompt"])
        self.assertIn("不要用 %s_send 回复这条请求" % CID, body["prompt"])
        self.assertIn("正文节选：按自然周统计。", body["prompt"])
        self.assertIn("示例文档的知识库背景", body["prompt"])
        self.assertIn('kind="doc" title="接口评审" url="%s"' % LINK, body["prompt"])
        self.assertIn('request 填 "%s"，text 填回复内容（纯文字）' % run["id"], body["prompt"])
        self.assertEqual(h.world.reactions, [])

    def test_explicit_config_overrides_both_platform_defaults(self):
        for factory, config, guest in ((make, {"auto_reply_full": False}, True), (wechat, {}, True),
                                       (wechat, {"auto_reply_full": True}, False)):
            with self.subTest(platform=factory.__name__, config=config):
                h = factory(self, **config)
                h.app.store.autoreply_set(TEAM, True)
                running(h, at_me(h))
                body = h.http.starts()[-1]["body"]
                self.assertIs(body["report_tasks"], not guest)
                if guest:
                    self.assertEqual(body["guest"], {"tools": [h.cid + "_agent_reply"]})
                    self.assertIn("你只有 %s_agent_reply 这一个工具" % h.cid, body["prompt"])
                else:
                    self.assertNotIn("guest", body)
                    self.assertIn(h.cid + "_messages", body["prompt"])
                    if factory is wechat:
                        self.assertIn("省略 at", body["prompt"])
                if factory is wechat:
                    self.assertEqual(h.world.reactions, [])

    def test_full_auto_continues_only_automatic_replies_in_the_same_thread(self):
        h = make(self)
        h.app.store.autoreply_set(TEAM, True)
        initial = ask(h, "/agent 总结进度")
        personal = running(h, initial)
        first = h.world.push(TEAM, "user_example_b", "@示例用户 进度如何？", at_me=True, root_id=initial.id)
        automatic = running(h, first)
        self.assertNotEqual(automatic["session_id"], personal["session_id"])
        self.assertNotIn("guest", h.http.starts()[-1]["body"])
        mine = running(h, ask(h, "/agent 还有一件事", root_id=initial.id))
        self.assertEqual(mine["session_id"], personal["session_id"])
        second = h.world.push(TEAM, "user_example_b", "@示例用户 文档更新了吗？", at_me=True, root_id=initial.id)
        again = running(h, second)
        self.assertEqual(again["session_id"], automatic["session_id"])
        self.assertEqual(again["continued"], automatic["id"])
        self.assertEqual(len(h.http.starts()), 2)

    def test_switching_modes_does_not_continue_a_conversation_of_the_other_mode(self):
        h = make(self, auto_reply_full=False)
        h.app.store.autoreply_set(TEAM, True)
        first = at_me(h)
        guest = running(h, first)
        h.app.agent.auto_full = True
        second = h.world.push(TEAM, "user_example_b", "@示例用户 再查一下", at_me=True, root_id=first.id)
        full = running(h, second)
        self.assertNotEqual(guest["session_id"], full["session_id"])
        self.assertNotIn("guest", h.http.starts()[-1]["body"])
        h.app.agent.auto_full = False
        third = h.world.push(TEAM, "user_example_b", "@示例用户 还有什么？", at_me=True, root_id=first.id)
        resumed_guest = running(h, third)
        self.assertEqual(resumed_guest["session_id"], guest["session_id"])
        self.assertEqual(len(h.http.starts()), 2)

    def test_a_restart_only_replays_a_start_in_the_same_mode(self):
        h = make(self)
        interrupted = {"kind": "auto", "created": time.time(), "start_op": "example-op", "guest": False}
        self.assertEqual(h.app.agent._resume_how(interrupted), "op")
        h.app.agent.auto_full = False
        self.assertEqual(h.app.agent._resume_how(interrupted), "")
        del interrupted["guest"]  # a start interrupted before this setting was introduced
        self.assertEqual(h.app.agent._resume_how(interrupted), "op")

    def test_full_auto_names_the_media_tool_without_sending_progress_or_fallback(self):
        h = make(self)
        h.app.store.autoreply_set(TEAM, True)
        pic = h.world.push(TEAM, "user_example_a", "", type="IMAGE", media=[Media("image", ref="img:example-3")])
        msg = h.world.push(TEAM, "user_example_b", "@示例用户 这张图是什么？", at_me=True, parent_id=pic.id)
        run = running(h, msg)
        prompt = h.http.starts()[-1]["body"]["prompt"]
        self.assertIn("看图：%s_media message=" % CID + pic.id, prompt)
        self.assertIn("不发进度", h.tool("agent_reply", request=run["id"], text="正在核对", progress=True))
        h.http.add_todo("auto-todo", run["session_id"], [])
        h.http.add(run["session_id"], "assistant", "text", "已核对截图，统计口径是自然周。")
        h.http.end_turn(run["session_id"])
        wait_until(lambda: run_of(h, msg)["state"] == "done", what="the automatic turn ended")
        self.assertFalse(run_of(h, msg).get("awaiting"))
        self.assertEqual(h.sent(), [], "turn text alone is never forwarded to the group")

    def test_full_auto_and_doc_share_the_three_run_limit(self):
        h = make_doc(self)
        h.app.store.autoreply_set(TEAM, True)
        running(h, at_me(h))
        running(h, at_me(h, text="@示例用户 还有一个问题"))
        mention(h)
        doc_run(h)
        blocked = at_me(h, text="@示例用户 再问一个问题")
        wait_until(lambda: h.app.store.message(TEAM, blocked.id), what="the fourth request was stored")
        self.assertEqual(h.app.agent._guest_room(TEAM, "auto", time.time()), "3 already under way")
        self.assertEqual(h.app.agent.auto_used(TEAM), 2)
        self.assertEqual(len(h.http.starts()), 3)
        self.assertTrue(all("guest" not in r["body"] for r in h.http.starts()))


class AutoDraftWithdrawalTest(unittest.TestCase):
    def auto(self, h, **where):
        h.app.store.autoreply_set(TEAM, True)
        msg = h.world.push(TEAM, "user_example_b", "@示例用户 能承诺周五上线吗？", at_me=True, **where)
        return msg, running(h, msg)

    def user_mentions(self, h, text, mentions, chat=TEAM, **where):
        message = h.world.make(chat, "me", text, **where)
        message.mention_ids = mentions
        h.app.ingest([message], live=True)

    def assert_stopped(self, h, msg):
        wait_until(lambda: run_of(h, msg)["state"] == "stopped", what="stopped")
        wait_until(lambda: h.http.sessions.get(run_of(h, msg)["session_id"], {}).get("interrupted"), what="interrupted")
        wait_until(lambda: run_of(h, msg).get("ack_end") == "stopped", what="acknowledgement withdrawn")
        self.assertEqual(run_of(h, msg)["stop_reason"], "user_reply")
        self.assertEqual(h.sent(), [])

    def assert_running(self, h, msg):
        time.sleep(0.15)  # a stop, if taken, is handed off to its own thread
        self.assertEqual(run_of(h, msg)["state"], "running")
        self.assertEqual(h.http.posts("/interrupt"), [])

    def test_user_quote_interrupts_auto_and_withdraws_its_reaction(self):
        h = make(self)
        msg, _ = self.auto(h)
        ask(h, "我确认周五可以上线。", parent_id=msg.id)
        self.assert_stopped(h, msg)

    def test_real_mention_of_asker_interrupts_auto(self):
        h = make(self)
        msg, _ = self.auto(h)
        self.user_mentions(h, "我确认周五可以上线。", ["user_example_b"])
        self.assert_stopped(h, msg)

    def test_reply_in_the_same_topic_interrupts_auto(self):
        h = make(self)
        h.app.store.upsert_chat(Chat(TEAM, "group", "示例项目群", topic=True))
        root = h.world.push(TEAM, "user_example_a", "发布话题", placement="thread_chat")
        msg, _ = self.auto(h, root_id=root.id, thread_id=root.id, placement="thread")
        ask(h, "我确认周五可以上线。", root_id=root.id, thread_id=root.id, placement="thread")
        self.assert_stopped(h, msg)

    def test_unrelated_messages_leave_auto_running(self):
        h = make(self)
        root = h.world.push(TEAM, "user_example_a", "普通群里的线程")
        msg, _ = self.auto(h, root_id=root.id)
        ask(h, "@示例同事乙 周会要开始了。")  # plain text is not an @
        self.user_mentions(h, "记得参加周会。", ["user_example_a"], root_id=root.id)
        self.user_mentions(h, "周五可以上线。", ["user_example_b"], chat="chat_example_chatter", parent_id=msg.id)
        h.world.push(TEAM, "user_example_a", "帮你回了。", parent_id=msg.id)
        self.assert_running(h, msg)

    def test_approved_send_echo_does_not_interrupt_auto(self):
        h = make(self)
        msg, _ = self.auto(h)
        args = {"chat": TEAM, "reply_to": msg.id, "at": ["user_example_b"], "text": "周五可以上线。"}
        h.c("actions.describe", name=CID + "_send", args=args)
        self.assertEqual(h.sent(), [])
        approved = h.c("actions.do", name=CID + "_send", request_id="approved-draft", args=args)
        sent = h.sent()[-1]
        self.assertEqual(sent["reply_to"], msg.id)
        self.assertEqual(sent["mentions"], [("user_example_b", "示例同事乙", 0)])
        row = h.app.store.message(TEAM, approved["message_ref"])
        self.assertTrue(row["mine"])
        self.assert_running(h, msg)  # the fixture echoes inside send, before actions.do has the ID
        with h.app.agent._mu:
            h.app.agent._own.clear()
        h.app.agent.on_message(row)  # the journal also recognizes a later echo after losing the cache
        self.assert_running(h, msg)

    def test_agent_reply_echo_does_not_interrupt_auto(self):
        h = make(self)
        msg, run = self.auto(h)
        h.tool("agent_reply", request=run["id"], text="发布窗口是周五。")
        self.assert_running(h, msg)

    def test_pending_card_keeps_auto_running_when_session_status_says_done(self):
        h = make(self)
        msg, run = self.auto(h)
        session_row = h.app.hub.session_row
        checked = threading.Event()

        def row(mid, sid):
            if sid != run["session_id"]:
                return session_row(mid, sid)
            checked.set()
            return {"status": "done", "state": "needs_you", "pending_interactions": 1}

        with patch.object(h.app.hub, "session_row", row):
            h.http.add(run["session_id"], "assistant", "text", "你需要确认周五的发布承诺。")
            self.assertTrue(checked.wait(5))
            self.assert_running(h, msg)
            ask(h, "我来处理。", parent_id=msg.id)
            self.assert_stopped(h, msg)

    def test_recall_withdraws_a_pending_draft(self):
        h = make(self)
        h.app.backend.reports_recalls = True
        msg, run = self.auto(h)
        sid = run["session_id"]
        args = {"chat": TEAM, "reply_to": msg.id, "text": "周五可以上线。"}
        h.c("actions.describe", name=CID + "_send", args=args)
        h.http.sessions[sid]["pending_interactions"] = 1
        h.http.end_turn(sid)
        wait_until(lambda: run_of(h, msg)["state"] == "done", what="turn ended with a pending draft")
        h.app.agent.on_recalled(msg.chat_id, msg.id, h.app.account_epoch())
        wait_until(lambda: h.http.posts("/api/v1/assistant/conversations/%s/stop" % sid), what="the conversation stopped")
        self.assertEqual(run_of(h, msg)["state"], "stopped")
        self.assertEqual(run_of(h, msg)["stop_reason"], "recalled")
        self.assertFalse(h.http.sessions[sid].get("pending_interactions"), "interrupt withdrew the draft")
        with self.assertRaisesRegex(HubSideError, "已撤回"):
            h.c("actions.do", name=CID + "_send", request_id="late-approval", args=args)
        self.assertEqual(h.sent(), [])

    def test_a_recalled_auto_request_gets_no_confirmation_card(self):
        h = make(self)
        h.app.backend.reports_recalls = True
        msg, run = self.auto(h)
        args = {"chat": TEAM, "reply_to": msg.id, "text": "周五可以上线。"}
        with patch.object(h.app.backend, "message_gone", return_value=True) as lookup:
            with self.assertRaisesRegex(HubSideError, "已撤回"):
                h.c("actions.describe", name=CID + "_send", args=args)
            lookup.assert_called_once_with(TEAM, msg.id)
        wait_until(lambda: h.http.posts("/%s/stop" % run["session_id"]), what="the conversation stopped")
        self.assertEqual(run_of(h, msg)["stop_reason"], "recalled")
        self.assertEqual(h.sent(), [])

    def test_recalling_an_ended_auto_request_leaves_a_newer_draft_alone(self):
        h = make(self)
        root = h.world.push(TEAM, "user_example_a", "周五的安排")
        msg, first = self.auto(h, root_id=root.id)
        sid = first["session_id"]
        h.http.end_turn(sid)
        wait_until(lambda: run_of(h, msg)["state"] == "done", what="first turn ended")
        newer, second = self.auto(h, root_id=root.id)
        self.assertEqual(second["session_id"], sid)
        h.http.sessions[sid]["pending_interactions"] = 1
        h.app.agent.on_recalled(TEAM, msg.id, h.app.account_epoch())
        wait_for_recall()
        self.assertEqual(run_of(h, msg)["state"], "done")
        self.assertEqual(run_of(h, newer)["state"], "running")
        self.assertEqual(h.http.sessions[sid]["pending_interactions"], 1)
        self.assertEqual(h.http.posts("/stop"), [])

    def test_recall_during_sender_lookup_prevents_auto_start(self):
        h = make(self)
        h.app.store.autoreply_set(TEAM, True)
        looking_up, release = threading.Event(), threading.Event()
        threads = []

        def kind(*_):
            threads.append(threading.current_thread())
            looking_up.set()
            release.wait(5)
            return "user"

        with patch.object(h.app.people, "kind_now", side_effect=kind):
            msg = at_me(h)
            try:
                self.assertTrue(looking_up.wait(5))
                h.app.agent.on_recalled(TEAM, msg.id, h.app.account_epoch())
            finally:
                release.set()
            threads[0].join(5)
            self.assertFalse(threads[0].is_alive())
        self.assertEqual(h.app.store.run_count(), 0)
        self.assertEqual(h.http.starts(), [])

    def test_ended_auto_guest_and_personal_runs_are_untouched(self):
        h = make(self)
        msg, run = self.auto(h)
        h.http.end_turn(run["session_id"])
        wait_until(lambda: run_of(h, msg)["state"] == "done", what="ended")
        ask(h, "我来处理。", parent_id=msg.id)
        self.assertEqual(run_of(h, msg)["state"], "done")
        personal = ask(h, "/agent 查一下发布安排")
        running(h, personal)
        ask(h, "我来处理。", parent_id=personal.id)
        self.assert_running(h, personal)
        h.app.agent.auto_full = False
        guest, _ = self.auto(h)
        h.app.agent.auto_full = True  # an existing guest stays a guest when the setting changes
        ask(h, "我来处理。", parent_id=guest.id)
        self.assert_running(h, guest)
        self.assertEqual(run_of(h, msg)["state"], "done")

    def test_user_reply_during_start_interrupts_the_session_when_its_id_arrives(self):
        h = make(self)
        h.app.store.autoreply_set(TEAM, True)
        entered, released = threading.Event(), threading.Event()
        start = h.app.hub.start_conversation

        def paused_start(*args, **kwargs):
            session = start(*args, **kwargs)
            entered.set()
            released.wait(5)
            return session

        with patch.object(h.app.hub, "start_conversation", paused_start):
            msg = at_me(h)
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(run_of(h, msg)["state"], "starting")
                ask(h, "我来处理。", parent_id=msg.id)
                wait_until(lambda: run_of(h, msg)["state"] == "stopped", what="stopped before the start returned")
                self.assertEqual(h.http.posts("/interrupt"), [])
            finally:
                released.set()
        self.assert_stopped(h, msg)
