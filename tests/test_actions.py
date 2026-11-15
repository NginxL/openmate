"""Read tools (no gate) and the two writes (send, send_dm): their cards, request ids, uncertain sends."""

import json
import os
import unittest

from harness import CID, ROOT, Harness, HubSideError, wait_until


class ToolListTest(unittest.TestCase):
    def test_names_schemas_and_manifest(self):
        h = Harness(self)
        tools = h.c("tools.list")["tools"]
        actions = h.c("actions.list")["tools"]
        names = [t["name"] for t in tools]
        self.assertEqual(names, [CID + "_" + n for n in ("status", "chats", "messages", "search", "docs", "agent_reply")])
        self.assertEqual([a["name"] for a in actions], [CID + "_send", CID + "_send_dm"])
        for t in tools + actions:
            self.assertTrue(t["description"])
            self.assertEqual(t["input_schema"]["type"], "object")
        send = actions[0]["input_schema"]["properties"]
        self.assertTrue({"chat", "text", "reply_to"} <= set(send))
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
        self.assertIn("以你的身份发到飞书群「示例项目群」", d["summary"])
        self.assertIn("回复 示例同事乙", d["summary"])
        self.assertTrue(d["summary"].endswith(text))
        dm = self.h.c("actions.describe", name=CID + "_send_dm", args={"user": "user_example_a", "text": "你好"})
        self.assertIn("示例同事甲", dm["summary"])
        self.assertIn("你好", dm["summary"])
        self.assertEqual(dm["action"]["visible"], "示例同事甲")
        self.assertEqual(self.h.sent(), [], "describe never sends")

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

    def test_bad_args(self):
        with self.assertRaises(HubSideError):
            self.h.c("actions.do", name=CID + "_send", args={"chat": "chat_example_team", "text": "x" * 9000}, request_id="r")
        with self.assertRaises(HubSideError):
            self.h.c("actions.do", name=CID + "_nope", args={}, request_id="r")
        self.assertEqual(self.h.sent(), [])


if __name__ == "__main__":
    unittest.main()
