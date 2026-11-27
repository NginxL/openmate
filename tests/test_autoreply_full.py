"""Automatic replies with the user's /agent tools, in their own conversations."""

import _runtmp  # noqa: F401
import base64
import hashlib
import time
import unittest

from harness import CID, wait_until
from openmate_core.backend import Media
from openmate_core.fixture import tiny_png
from test_agent import ask, computers, reading_of, run_of, running
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
                     "不要调用回复工具", "留给我", "不是我的指示"):
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
        h.http.add(run["session_id"], "assistant", "text", "需要你确认。建议回复：按自然周统计。")
        h.http.end_turn(run["session_id"])
        wait_until(lambda: run_of(h, msg)["state"] == "done", what="the automatic turn ended")
        self.assertFalse(run_of(h, msg).get("awaiting"))
        self.assertEqual(h.sent(), [], "the suggestion stays in Persona")

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
