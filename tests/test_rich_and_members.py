"""A formatted message on the thread page as safe Markdown (format: markdown) while everything else reads its text; a
rich text's (POST's) title line before /agent, and its pictures handed to the conversation with the request; the
members tool (matefeishu_members) and the send tool's @ of a bot it names; WeChat as before."""

try:  # first: every temporary directory of this run in one, removed at its end
    import _runtmp  # noqa: F401
except ImportError:  # (started by its path from elsewhere: its directory is not on the path)
    pass
import base64
import os
import sys
import time
import unittest

from harness import ROOT, HubSideError, wait_until
from test_agent import agent, ask, run_of, started

from openmate_core.backend import Media

sys.path.insert(0, os.path.join(ROOT, "extensions", "openmate-wechat"))
import openmate_wechat  # noqa: E402

TEAM = "chat_example_team"
MD = "**周报**\n\n本周 **完成** 了"


class ThreadPageTest(unittest.TestCase):
    def test_formatted_shows_as_markdown_the_rest_as_text(self):
        h = agent(self, agent=False)
        fm = h.world.push(TEAM, "user_example_a", "周报\n本周 完成 了", type="POST", markdown=MD)
        plain = h.world.push(TEAM, "user_example_b", "普通的一句")
        wait_until(lambda: h.app.store.message(TEAM, plain.id), what="kept")
        msgs = {m["id"]: m for m in h.c("thread.page", thread=TEAM, limit=50)["messages"]}
        self.assertEqual((msgs[fm.id]["text"], msgs[fm.id]["format"]), (MD, "markdown"))
        self.assertEqual(msgs[plain.id]["text"], "普通的一句")
        self.assertNotIn("format", msgs[plain.id])
        # everything else reads the plain text: the tools, the archive
        self.assertIn("本周 完成 了", h.tool("messages", chat=TEAM))
        self.assertNotIn("**", h.tool("messages", chat=TEAM))
        batch = h.c("archive.changes", cursor="", limit=100)
        texts = [m["text"] for b in batch["batches"] for m in b["messages"] if m["id"] == fm.id]
        self.assertEqual(texts, ["周报\n本周 完成 了"])

    def test_an_edit_without_formatting_drops_it(self):
        h = agent(self, agent=False)
        fm = h.world.push(TEAM, "user_example_a", "周报\n本周 完成 了", type="POST", markdown=MD)
        wait_until(lambda: h.app.store.markdowns(TEAM, [fm.id]), what="kept")
        from openmate_core.backend import Message
        again = Message(id=fm.id, chat_id=TEAM, position=fm.position, sender_id=fm.sender_id, type="POST",
                        text="改成了普通文字", at=fm.at)
        h.app.store.add_messages([again], from_history=True)
        self.assertEqual(h.app.store.markdowns(TEAM, [fm.id]), {})


class PostAgentTest(unittest.TestCase):
    def test_a_title_line_before_the_prefix(self):
        h = agent(self)
        m = ask(h, "周报\n/agent 帮我看下", type="POST", title="周报")
        body = started(h)["body"]
        self.assertTrue(body["prompt"].endswith("\n帮我看下"), body["prompt"][-80:])
        self.assertEqual(run_of(h, m)["request_text"], "帮我看下")

    def test_the_title_is_matched_as_written_and_code_counts(self):
        h = agent(self)
        ask(h, "周报 \n/agent 帮我看下", type="POST", title="周报 ")  # a title with a space at its end
        wait_until(lambda: h.http.starts(), what="started")
        h2 = agent(self)
        # the code block before the paragraph shows first (the text leaves code out): no request
        ask(h2, "周报\n/agent 帮我看下", type="POST", title="周报",
            text_after_mentions="周报\nprint(1)\n/agent 帮我看下")
        time.sleep(0.4)
        self.assertEqual(h2.http.starts(), [])

    def test_its_pictures_are_kept_for_a_resume(self):
        h = agent(self)
        pics = [Media("image", ref="img:%d" % i) for i in range(2)]
        m = ask(h, "/agent 看图", type="POST", media=pics)
        run = wait_until(lambda: next((r for r in h.app.store.runs_in_chat(TEAM) if r["request_id"] == m.id), None),
                         what="the run")
        first = h.app.agent._request_images(run, h.app.store.message(TEAM, m.id) | {"type": "POST"})
        again = h.app.agent._request_images(run, {"type": "POST", "media": [{"kind": "image", "ref": "fail:not_found"}]})
        self.assertEqual(first, again, "the same pictures, however a second fetch would go")

    def test_no_title_no_second_line(self):
        h = agent(self)
        ask(h, "先说一句\n/agent 帮我看下", type="POST")  # a second paragraph is no request without a title
        time.sleep(0.4)
        self.assertEqual(h.http.starts(), [])

    def test_its_pictures_go_with_it_at_most_four(self):
        h = agent(self)
        pics = [Media("image", ref="img:%d" % i) for i in range(6)] + [Media("image", ref="fail:not_found")]
        ask(h, "/agent 看看这些图", type="POST", media=[pics[-1]] + pics)
        body = started(h)["body"]
        imgs = body.get("images") or []
        self.assertEqual(len(imgs), 4)  # one that could not be fetched left out, then the first four
        self.assertEqual(imgs[0]["mime"], "image/png")
        self.assertTrue(base64.b64decode(imgs[0]["data"]).startswith(b"\x89PNG"))

    def test_a_text_request_carries_no_images(self):
        h = agent(self)
        ask(h, "/agent 帮我看下")
        self.assertNotIn("images", started(h)["body"])


class MembersTest(unittest.TestCase):
    def setUp(self):
        self.h = agent(self, agent=False)
        self.h.world.members[TEAM] = [
            {"id": "8001", "name": "构建机器人", "real_name": "构建机器人", "kind": "bot"},
            {"id": "user_example_b", "name": "小乙", "real_name": "示例同事乙", "kind": "user"}]

    def test_lists_people_and_bots_with_ids(self):
        out = self.h.tool("members", chat=TEAM)
        self.assertIn("2 个成员，其中机器人 1 个", out)
        self.assertIn("- 构建机器人（机器人，id=8001）", out)
        self.assertIn("- 小乙（成员，id=user_example_b，本名 示例同事乙）", out)
        self.assertIn("构建机器人", self.h.tool("members", chat=TEAM, query="机器"))
        self.assertNotIn("小乙", self.h.tool("members", chat=TEAM, query="机器"))
        with self.assertRaises(HubSideError):
            self.h.tool("members", chat="chat_unknown")

    def test_the_send_tool_ats_a_bot_the_list_names(self):
        a = {"chat": TEAM, "text": "@构建机器人 重新跑一下", "at": ["8001"]}
        card = self.h.c("actions.describe", name="matefeishu_send", args=a)
        self.assertIn("构建机器人", card["summary"])
        self.h.c("actions.do", name="matefeishu_send", args=a, request_id="r-bot")
        self.assertEqual(self.h.sent()[-1]["mentions"], [("8001", "构建机器人", 0)])

    def test_verify(self):
        from openmate_feishu import members

        self.assertEqual(members.verify(self.h.app, TEAM, [{"id": "8001", "name": "构建机器人"}]), [("8001", "构建机器人")])
        self.assertEqual(members.verify(self.h.app, TEAM, [{"id": "user_example_b", "name": "示例同事乙"}]),
                         [("user_example_b", "示例同事乙")])  # their own name too
        for wrong in ([{"id": "9999", "name": "谁"}], [{"id": "8001", "name": "别的名字"}]):
            with self.assertRaises(members.MembersError):
                members.verify(self.h.app, TEAM, wrong)


class WeChatTest(unittest.TestCase):
    def test_no_members_tool_no_markdown(self):
        from harness import Harness

        h = Harness(self, platform=openmate_wechat.platform())
        h.sign_in()
        self.assertNotIn("matewechat_members", [t["name"] for t in h.c("tools.list")["tools"]])
        with self.assertRaises(HubSideError):
            h.c("tools.call", name="matewechat_members", args={"chat": TEAM})

if __name__ == "__main__":
    unittest.main()
