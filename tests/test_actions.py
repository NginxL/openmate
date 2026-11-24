"""Read tools (no gate) and the two writes (send, send_dm): their cards, request ids, uncertain sends."""

import _runtmp  # noqa: F401  (first: every temporary directory of this run in one, removed at its end)
import datetime as _dt
import json
import os
import re
import time
import unittest

from harness import CID, ROOT, Harness, HubSideError, wait_until
from openmate_core.backend import Chat


class ToolListTest(unittest.TestCase):
    def test_names_schemas_and_manifest(self):
        h = Harness(self)
        tools = h.c("tools.list")["tools"]
        actions = h.c("actions.list")["tools"]
        names = [t["name"] for t in tools]
        self.assertEqual(names, [CID + "_" + n for n in ("status", "chats", "messages", "search", "docs", "agent_reply", "starkmesh", "media")])
        self.assertEqual([a["name"] for a in actions], [CID + "_send", CID + "_send_dm"])
        for t in tools + actions:
            self.assertTrue(t["description"])
            self.assertEqual(t["input_schema"]["type"], "object")
        send = actions[0]["input_schema"]["properties"]
        self.assertTrue({"chat", "text", "reply_to", "at"} <= set(send), "the example backend can @")
        self.assertIn("at", actions[0]["description"])
        self.assertIn("开一个新话题", actions[0]["description"])
        with open(os.path.join(ROOT, "extensions", "openmate-feishu", "persona-extension.json"), encoding="utf-8") as f:
            conn = json.load(f)["contributes"]["connectors"][0]
        self.assertEqual(conn["agent_tools"], names)  # every read tool and agent_reply; no write
        self.assertEqual(conn["confirm_always"], [CID + "_*"])

    def test_empty_args_answer_or_refuse_in_words(self):
        h = Harness(self)
        h.sign_in()
        for t in h.c("tools.list")["tools"]:
            try:
                res = h.c("tools.call", name=t["name"], args={})
                self.assertTrue(res["text"])
            except HubSideError as e:
                self.assertTrue(e.message)
        for a in h.c("actions.list")["tools"]:
            with self.assertRaises(HubSideError) as e:
                h.c("actions.describe", name=a["name"], args={})
            self.assertTrue(e.exception.message)


class ReadToolsTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self, config={"agent": True})
        self.h.sign_in()
        wait_until(lambda: self.h.app._catchup_done.is_set(), what="catch-up")

    def test_status_tool(self):
        text = self.h.tool("status")
        self.assertIn("已连接", text)
        self.assertIn("示例用户", text)
        self.assertIn("/agent：已开启", text)

    def test_chats_tool(self):
        text = self.h.tool("chats")
        self.assertIn("示例项目群", text)
        self.assertIn("chat=chat_example_team", text)
        self.assertIn("没回（@我）", text)
        self.assertIn("示例闲聊群", self.h.tool("chats", query="闲聊"))
        self.assertNotIn("示例项目群", self.h.tool("chats", query="闲聊"))

    def test_messages_tool(self):
        text = self.h.tool("messages", chat="chat_example_team", limit=2)
        self.assertIn("示例项目群", text)
        self.assertIn("接口文档", text)
        self.assertIn("更早的消息：before=", text)
        before = text.rsplit("before=", 1)[1].strip()
        self.assertIn("下午三点开周会", text)
        older = self.h.tool("messages", chat="chat_example_team", before=before)
        self.assertIn("加入了群聊", older)
        self.assertNotIn("接口文档", older)
        with self.assertRaises(HubSideError):
            self.h.tool("messages", chat="chat_unknown")

    def test_search_tool(self):
        text = self.h.tool("search", query="示例同事")
        self.assertIn("user=user_example_a", text)
        self.assertIn("群：示例项目群", self.h.tool("search", query="项目"))

    def test_docs_tool_through_the_host_cli(self):
        with self.assertRaises(HubSideError) as e:
            self.h.tool("docs", query="评审")
        self.assertIn("lark-cli", e.exception.message)
        asked = []

        def cli(params):
            asked.append(params)
            return {"integration": "lark-cli", "result": {"data": {"data": {"items": [
                {"title": "<em>评审</em>材料", "url": "https://example.feishu.cn/docx/ExampleDoc001", "summary": "周五评审"},
                {"title": "无关", "url": "https://example.feishu.cn/docx/ExampleDoc002"}]}}}}

        self.h.hub.cli = cli
        text = self.h.tool("docs", query="评审", limit=5)
        self.assertEqual(asked[-1], {"operation": "search", "kind": "docs", "query": "评审", "limit": 5})
        self.assertIn("评审材料 https://example.feishu.cn/docx/ExampleDoc001", text)
        self.assertIn("周五评审", text)


class ActionTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self)
        self.h.sign_in()
        wait_until(lambda: self.h.app._catchup_done.is_set(), what="catch-up")

    def test_describe_card_says_who_and_everything(self):
        text = "第一行\n第二行，完整内容"
        team = self.h.app.store.messages_before("chat_example_team", None, 10)[-1]
        d = self.h.c("actions.describe", name=CID + "_send", args={"chat": "chat_example_team", "text": text, "reply_to": team["id"]})
        self.assertEqual(d["action"]["name"], CID + "_send")
        self.assertIn("示例项目群", d["action"]["visible"])
        self.assertIn("以你的身份发到飞书群「示例项目群」（", d["summary"])
        self.assertIn("会话 id chat_example_team", d["summary"].split("\n")[0])
        self.assertIn("回复 示例同事乙", d["summary"])
        self.assertTrue(d["summary"].endswith(text))
        dm = self.h.c("actions.describe", name=CID + "_send_dm", args={"user": "user_example_a", "text": "你好"})
        self.assertIn("示例同事甲", dm["summary"])
        self.assertIn("你好", dm["summary"])
        self.assertEqual(dm["action"]["visible"], "示例同事甲")
        self.assertEqual(self.h.sent(), [], "describe never sends")

    def test_describe_names_a_direct_chat_and_keeps_its_id(self):
        from openmate_core.backend import Chat

        store, me = self.h.app.store, self.h.app.account
        store.upsert_chat(Chat(id="chat_self", kind="dm", name="示例用户", peer_id=me.id))
        store.upsert_chat(Chat(id="chat_unnamed", kind="dm"))
        store.upsert_chat(Chat(id="chat_named", kind="dm", name="示例同事丙", peer_id="user_example_c"))
        cases = (("chat_self", "飞书里你和自己的私聊「示例用户」（会话 id chat_self）", "只有你自己"),
                 ("chat_unnamed", "飞书私聊（对方名字未知，会话 id chat_unnamed）", "这个私聊里的对方（名字未知）"),
                 ("chat_named", "飞书私聊「示例同事丙」（会话 id chat_named）", "示例同事丙"))
        for chat, target, visible in cases:
            d = self.h.c("actions.describe", name=CID + "_send", args={"chat": chat, "text": "你好"})
            self.assertEqual(d["summary"].split("\n")[0], "以你的身份发到" + target, chat)
            self.assertEqual(d["action"]["visible"], visible, chat)
        self.assertEqual(self.h.sent(), [])

    def test_do_needs_a_request_id_and_runs_once(self):
        args = {"chat": "chat_example_team", "text": "我来更新接口文档"}
        with self.assertRaises(HubSideError) as e:
            self.h.c("actions.do", name=CID + "_send", args=args)
        self.assertIn("request_id", e.exception.message)
        first = self.h.c("actions.do", name=CID + "_send", args=args, request_id="req-1")
        again = self.h.c("actions.do", name=CID + "_send", args=args, request_id="req-1")
        self.assertEqual(first, again)
        self.assertTrue(first["message_ref"])
        self.assertIn("已发送", first["text"])
        self.assertEqual(len(self.h.sent()), 1)
        self.h.c("actions.do", name=CID + "_send", args=args, request_id="req-2")
        self.assertEqual(len(self.h.sent()), 2)

    def test_reply_in_a_thread(self):
        root = self.h.world.push("chat_example_team", "user_example_a", "话题开头")
        inner = self.h.world.push("chat_example_team", "user_example_b", "话题里的问题", root_id=root.id)
        wait_until(lambda: self.h.app.store.message("chat_example_team", inner.id), what="stored")
        self.h.c("actions.do", name=CID + "_send", args={"chat": "chat_example_team", "text": "回答", "reply_to": inner.id},
                 request_id="req-t")
        s = self.h.sent()[-1]
        self.assertEqual((s["reply_to"], s["root"]), (inner.id, root.id))

    def test_the_card_says_a_thread_reply_quotes_the_first_message(self):
        root = self.h.world.push("chat_example_team", "user_example_a", "话题开头")
        inner = self.h.world.push("chat_example_team", "user_example_b", "话题里的问题", root_id=root.id)
        wait_until(lambda: self.h.app.store.message("chat_example_team", inner.id), what="stored")
        args = {"chat": "chat_example_team", "text": "回答", "reply_to": inner.id}
        d = self.h.c("actions.describe", name=CID + "_send", args=args)
        self.assertIn("回复 示例同事乙：“话题里的问题”", d["summary"], "a service that quotes the picked one")
        self.h.app.backend.thread_reply_quotes_root = True
        d = self.h.c("actions.describe", name=CID + "_send", args=args)
        self.assertIn("引用的是话题第一条 示例同事甲：“话题开头”", d["summary"])
        self.assertNotIn("回复 示例同事乙：", d["summary"])
        # the first message itself: quoted as it is
        d = self.h.c("actions.describe", name=CID + "_send", args=dict(args, reply_to=root.id))
        self.assertIn("回复 示例同事甲：“话题开头”", d["summary"])

    def test_uncertain_send_is_refused_after(self):
        args = {"chat": "chat_example_team", "text": "可能发出去了"}
        self.h.world.send_fail = "uncertain"
        with self.assertRaises(HubSideError) as e:
            self.h.c("actions.do", name=CID + "_send", args=args, request_id="req-u")
        self.assertIn("不确定", e.exception.message)
        with self.assertRaises(HubSideError) as e:
            self.h.c("actions.do", name=CID + "_send", args=args, request_id="req-u")
        self.assertIn("不会再发", e.exception.message)
        # also after a restart
        h2 = self.h.restart()
        h2.wait_ok()
        with self.assertRaises(HubSideError):
            h2.c("actions.do", name=CID + "_send", args=args, request_id="req-u")
        self.assertEqual(h2.sent(), [])

    def test_cut_off_send_is_uncertain_after_restart(self):
        store = self.h.app.store
        self.assertTrue(store.send_claim("action:req-crash", CID + "_send"))  # as if the program died mid-send
        h2 = self.h.restart()
        h2.wait_ok()
        with self.assertRaises(HubSideError) as e:
            h2.c("actions.do", name=CID + "_send", args={"chat": "chat_example_team", "text": "x"}, request_id="req-crash")
        self.assertIn("不确定", e.exception.message)

    def test_rejected_send_frees_the_request_id(self):
        args = {"chat": "chat_example_team", "text": "被拒了"}
        self.h.world.send_fail = "rejected"
        with self.assertRaises(HubSideError) as e:
            self.h.c("actions.do", name=CID + "_send", args=args, request_id="req-r")
        self.assertIn("没有发出去", e.exception.message)
        self.h.c("actions.do", name=CID + "_send", args=args, request_id="req-r")
        self.assertEqual(len(self.h.sent()), 1)

    def test_send_dm_creates_the_chat_before_claiming(self):
        args = {"user": "user_example_c", "text": "你好，丙"}
        self.h.world.create_fail = True
        with self.assertRaises(HubSideError) as e:
            self.h.c("actions.do", name=CID + "_send_dm", args=args, request_id="req-dm")
        self.assertIn("私聊", e.exception.message)
        self.assertIsNone(self.h.app.store.send_get("action:req-dm"), "a failed chat must not take the request id")
        self.h.world.create_fail = False
        res = self.h.c("actions.do", name=CID + "_send_dm", args=args, request_id="req-dm")
        self.assertIn("已私聊", res["text"])
        self.assertEqual(self.h.world.created_chats, ["chat_example_dm_c"])
        again = self.h.c("actions.do", name=CID + "_send_dm", args=args, request_id="req-dm")
        self.assertEqual(res, again)
        self.assertEqual([s["chat"] for s in self.h.sent()], ["chat_example_dm_c"])

    def test_real_mentions_are_on_the_card_and_go_to_the_backend(self):
        args = {"chat": "chat_example_team", "text": "请 @示例同事甲 看一下，@示例同事乙 也看看",
                "at": ["user_example_a", "user_example_b", "user_example_a"]}
        d = self.h.c("actions.describe", name=CID + "_send", args=args)
        self.assertIn("会 @ 并提醒：示例同事甲（user_example_a）、示例同事乙（user_example_b）", d["summary"])
        self.assertTrue(d["summary"].endswith(args["text"]), "the text as it will read")
        self.h.c("actions.do", name=CID + "_send", args=args, request_id="req-at")
        s = self.h.sent()[-1]
        self.assertEqual([(u, n) for u, n, _ in s["mentions"]],
                         [("user_example_a", "示例同事甲"), ("user_example_b", "示例同事乙")])
        self.assertTrue(all(s["text"].startswith("@" + n, at) for _, n, at in s["mentions"]), "each at its place")
        self.assertEqual(s["text"], args["text"])
        self.h.c("actions.do", name=CID + "_send", args={"chat": "chat_example_team", "text": "没有 @"},
                 request_id="req-no-at")
        self.assertEqual(self.h.sent()[-1]["mentions"], [], "none asked: none passed")

    def test_a_mention_not_written_goes_in_front_on_the_card_and_in_what_is_sent(self):
        args = {"chat": "chat_example_team", "text": "看一下", "at": ["user_example_b", "user_example_a"]}
        d = self.h.c("actions.describe", name=CID + "_send", args=args)
        self.assertTrue(d["summary"].endswith("——\n@示例同事乙 @示例同事甲 看一下"), d["summary"])
        self.h.c("actions.do", name=CID + "_send", args=args, request_id="req-front")
        self.assertEqual(self.h.sent()[-1]["text"], "@示例同事乙 @示例同事甲 看一下", "sent as the card showed it")

    def test_a_request_sent_before_answers_again_without_looking_names_up(self):
        args = {"chat": "chat_example_team", "text": "@示例同事甲 看一下", "at": ["user_example_a"]}
        first = self.h.c("actions.do", name=CID + "_send", args=args, request_id="req-again")
        backend = self.h.app.backend
        real_name, real_store = backend.user_name, self.h.app.store.name
        backend.user_name = lambda *a, **k: (_ for _ in ()).throw(AssertionError("looked up again"))
        self.h.app.store.name = lambda *a, **k: ""
        try:
            self.assertEqual(self.h.c("actions.do", name=CID + "_send", args=args, request_id="req-again"), first)
        finally:
            backend.user_name, self.h.app.store.name = real_name, real_store
        self.assertEqual(len(self.h.sent()), 1)

    def test_mentions_of_oneself_or_of_two_with_one_name_are_refused(self):
        me = self.h.app.account.id
        with self.assertRaises(HubSideError) as e:
            self.h.c("actions.describe", name=CID + "_send", args={"chat": "chat_example_team", "text": "x", "at": [me]})
        self.assertIn("不能有你自己", e.exception.message)
        self.h.app.store.put_name("700100", "同名")
        self.h.app.store.put_name("700101", "同名")
        with self.assertRaises(HubSideError) as e:
            self.h.c("actions.describe", name=CID + "_send",
                     args={"chat": "chat_example_team", "text": "@同名 看", "at": ["700100", "700101"]})
        self.assertIn("重名（同名）", e.exception.message)
        self.assertEqual(self.h.sent(), [])

    def test_a_request_refused_before_is_sent_anew_not_answered_from_the_journal(self):
        args = {"chat": "chat_example_team", "text": "被拒后重发"}
        self.h.world.send_fail = "rejected"
        with self.assertRaises(HubSideError):
            self.h.c("actions.do", name=CID + "_send", args=args, request_id="req-rr")
        res = self.h.c("actions.do", name=CID + "_send", args=args, request_id="req-rr")
        self.assertIn("已发送", res["text"])
        self.assertEqual([s["text"] for s in self.h.sent()], ["被拒后重发"], "really sent, once")

    def test_a_card_that_cannot_be_read_for_a_moment_frees_the_request(self):
        import sqlite3

        store = self.h.app.store
        real = store.get_chat
        calls = []

        def busy(chat_id):
            calls.append(chat_id)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return real(chat_id)

        store.get_chat = busy
        try:
            args = {"chat": "chat_example_team", "text": "稍后再试"}
            with self.assertRaises(HubSideError) as e:
                self.h.c("actions.do", name=CID + "_send", args=args, request_id="req-busy")
            self.assertIn("没有发出", e.exception.message)
            self.assertIsNone(store.send_get("action:req-busy"), "the card is read before the request is claimed")
            res = self.h.c("actions.do", name=CID + "_send", args=args, request_id="req-busy")
        finally:
            store.get_chat = real
        self.assertIn("已发送", res["text"])
        self.assertEqual(len(self.h.sent()), 1, "sent once, on the try after")

    def test_an_uncertain_request_still_says_so_when_its_names_are_gone(self):
        args = {"chat": "chat_example_team", "text": "@示例同事甲 看一下", "at": ["user_example_a"]}
        self.h.world.send_fail = "uncertain"
        with self.assertRaises(HubSideError):
            self.h.c("actions.do", name=CID + "_send", args=args, request_id="req-unc")
        backend = self.h.app.backend
        real_name, real_store = backend.user_name, self.h.app.store.name
        backend.user_name = lambda *a, **k: ""
        self.h.app.store.name = lambda *a, **k: ""
        try:
            with self.assertRaises(HubSideError) as e:
                self.h.c("actions.do", name=CID + "_send", args=args, request_id="req-unc")
        finally:
            backend.user_name, self.h.app.store.name = real_name, real_store
        self.assertIn("不确定", e.exception.message, "the journal speaks first, not the names")

    def test_a_number_id_with_a_leading_zero_is_no_id(self):
        with self.assertRaises(HubSideError) as e:
            self.h.c("actions.describe", name=CID + "_send",
                     args={"chat": "chat_example_team", "text": "x", "at": ["07001"]})
        self.assertIn("不是用户 id", e.exception.message)

    def test_too_many_mentions_are_refused_before_any_name_is_looked_up(self):
        calls = []
        backend = self.h.app.backend
        real = backend.user_name
        backend.user_name = lambda uid, chat=None: calls.append(uid) or ""
        try:
            with self.assertRaises(HubSideError) as e:
                self.h.c("actions.describe", name=CID + "_send",
                         args={"chat": "chat_example_team", "text": "x", "at": ["8%03d" % i for i in range(100)]})
        finally:
            backend.user_name = real
        self.assertIn("最多 @ 20 个人", e.exception.message)
        self.assertEqual(calls, [])

    def test_mentions_that_cannot_be_made_are_refused_before_anything(self):
        base = {"chat": "chat_example_team", "text": "看一下"}
        many = ["7000%02d" % i for i in range(21)]
        for i, uid in enumerate(many):
            self.h.app.store.put_name(uid, "某人%d" % i)
        for at, words in ((["不是id"], "不是用户 id"), (["9999"], "不认识用户 9999"), ("user_example_a x", "不是用户 id"),
                          ({"a": 1}, "列表"), ([{"id": 1}], "不是用户 id"), (many, "最多 @ 20 个人")):
            with self.assertRaises(HubSideError, msg=repr(at)) as e:
                self.h.c("actions.describe", name=CID + "_send", args=dict(base, at=at))
            self.assertIn(words, e.exception.message)
        d = self.h.c("actions.describe", name=CID + "_send", args=dict(base, at=many[:20]))
        self.assertIn("某人19（700019）", d["summary"], "twenty is fine")
        self.h.app.backend.can_mention = False  # a platform without @s: never offered, refused when asked
        try:
            names = [a["input_schema"]["properties"] for a in self.h.c("actions.list")["tools"]]
            self.assertNotIn("at", names[0])
            with self.assertRaises(HubSideError) as e:
                self.h.c("actions.do", name=CID + "_send", args=dict(base, at=["user_example_a"]), request_id="r-x")
            self.assertIn("不能 @人", e.exception.message)
            with self.assertRaises(Exception) as e:
                self.h.app.send_as_user("chat_example_team", "x", mentions=[("user_example_a", "示例同事甲")])
            self.assertIn("不能 @人", str(getattr(e.exception, "message", e.exception)))
        finally:
            self.h.app.backend.can_mention = True
        self.assertEqual(self.h.sent(), [])

    def test_the_card_says_a_new_message_opens_a_topic_in_a_topic_group(self):
        d = self.h.c("actions.describe", name=CID + "_send", args={"chat": "chat_example_team", "text": "新话题"})
        self.assertNotIn("新话题", d["summary"].split("——")[0], "an ordinary group")
        with self.h.world.lock:
            team = self.h.world.chats["chat_example_team"]
            self.h.world.chats["chat_example_team"] = Chat(id=team.id, kind=team.kind, name=team.name, topic=True,
                                                           members=team.members)
        d = self.h.c("actions.describe", name=CID + "_send", args={"chat": "chat_example_team", "text": "新话题"})
        self.assertIn("在这个话题群里开一个新话题", d["summary"], "as the service says now, not as first stored")
        root = self.h.world.push("chat_example_team", "user_example_a", "话题开头")
        wait_until(lambda: self.h.app.store.message("chat_example_team", root.id), what="stored")
        d = self.h.c("actions.describe", name=CID + "_send",
                     args={"chat": "chat_example_team", "text": "回答", "reply_to": root.id})
        self.assertNotIn("开一个新话题", d["summary"], "a reply goes into its topic")
        with self.h.world.lock:
            self.h.world.topic_mode_chats.add("chat_example_team")
        with self.assertRaises(HubSideError) as e:  # a group in topic mode: refused before the phone is asked
            self.h.c("actions.describe", name=CID + "_send", args={"chat": "chat_example_team", "text": "新话题"})
        self.assertIn("只能回复群里已有的话题", e.exception.message)
        d = self.h.c("actions.describe", name=CID + "_send",
                     args={"chat": "chat_example_team", "text": "回答", "reply_to": root.id})
        self.assertIn("回答", d["summary"], "a reply there is fine")
        with self.h.world.lock:
            self.h.world.topic_mode_chats.discard("chat_example_team")
        backend = self.h.app.backend
        real, asked = backend.chat_info, []
        backend.topic_groups = False  # a platform without such groups (WeChat): its chat is never looked up for it
        backend.chat_info = lambda *a: asked.append(a) or real(*a)
        try:
            d = self.h.c("actions.describe", name=CID + "_send", args={"chat": "chat_example_team", "text": "x"})
        finally:
            backend.topic_groups, backend.chat_info = True, real
        self.assertNotIn("开一个新话题", d["summary"])
        self.assertEqual(asked, [])

    def test_bad_args(self):
        with self.assertRaises(HubSideError):
            self.h.c("actions.do", name=CID + "_send", args={"chat": "chat_example_team", "text": "x" * 9000}, request_id="r")
        with self.assertRaises(HubSideError):
            self.h.c("actions.do", name=CID + "_nope", args={}, request_id="r")
        self.assertEqual(self.h.sent(), [])


class FilteredMessagesTest(unittest.TestCase):
    """matefeishu_messages with since / until / query: the reads the assistant used to script with lark-cli loops."""

    def setUp(self):
        from openmate_core.fixture import FixtureWorld

        self.world = FixtureWorld()
        self.world.add_chat(Chat("chat_long", "group", "长历史群", members=9))
        self.base = time.time() - 1000 * 60
        for i in range(1000):  # one a minute; only the newest reach the store at catch-up, the rest is history
            self.world.make("chat_long", "user_example_a", ("周报 第%d条" if i % 100 == 7 else "闲聊 第%d条") % i, at=self.base + i * 60)
        self.h = Harness(self, world=self.world)
        self.h.sign_in()
        wait_until(lambda: self.h.app._catchup_done.is_set(), what="catch-up")

    def stamp(self, i: int) -> str:  # in the hub's time zone, as the tool reads it (not the machine's: CI runs in UTC)
        return _dt.datetime.fromtimestamp(self.base + i * 60, self.h.app.tz).strftime("%Y-%m-%d %H:%M")

    def test_query_keeps_only_matching_messages(self):
        text = self.h.tool("messages", chat="chat_example_team", query="周会")
        self.assertIn("下午三点开周会", text)
        self.assertNotIn("接口文档", text)
        self.assertIn("含“周会”", text)
        self.assertIn("示例同事甲", self.h.tool("messages", chat="chat_example_team", query="示例同事甲"))  # the sender counts
        self.assertIn("没有符合的消息", self.h.tool("messages", chat="chat_example_team", query="周会 不存在的词"))

    def test_a_window_reads_back_into_history(self):
        text = self.h.tool("messages", chat="chat_long", since=self.stamp(100), until=self.stamp(104), limit=50)
        got = [int(x) for x in re.findall(r"第(\d+)条", text)]
        self.assertEqual(got, [100, 101, 102, 103, 104])  # oldest first, both ends kept, nothing outside
        self.assertNotIn("before=", text)  # the window was read to its start

    def test_query_over_history_pages_by_before(self):
        text = self.h.tool("messages", chat="chat_long", query="周报", limit=3)
        self.assertEqual(re.findall(r"周报 第(\d+)条", text), ["707", "807", "907"])
        before = re.search(r"更早的符合消息：before=(\d+)", text).group(1)
        older = self.h.tool("messages", chat="chat_long", query="周报", limit=50, before=before)
        self.assertEqual(re.findall(r"周报 第(\d+)条", older), ["7", "107", "207", "307", "407", "507", "607"])
        self.assertNotIn("before=", older)

    def test_a_long_look_hands_back_where_to_go_on(self):
        from openmate_core import tools

        old = tools.SCAN_MOST
        tools.SCAN_MOST = 250
        try:
            text = self.h.tool("messages", chat="chat_long", query="不存在的词")
        finally:
            tools.SCAN_MOST = old
        self.assertIn("没有符合的消息", text)
        self.assertRegex(text, r"还没看完，接着往前：before=\d+")

    def test_a_date_until_is_the_whole_day_and_bad_times_say_so(self):
        day = _dt.datetime.fromtimestamp(self.base + 999 * 60, self.h.app.tz).strftime("%Y-%m-%d")
        self.assertIn("闲聊 第999条", self.h.tool("messages", chat="chat_long", until=day, limit=1))
        with self.assertRaises(HubSideError) as e:
            self.h.tool("messages", chat="chat_long", since="上周三")
        self.assertIn("时间看不懂", e.exception.message)


if __name__ == "__main__":
    unittest.main()
