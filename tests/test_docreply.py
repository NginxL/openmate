"""文档代答: an @-mention of the user in a document's comment (a doc or a wiki page) is always answered there by the
Agent as the user, unless it came from a bot, someone the service can't name, or another AI."""

import time
import unittest

from harness import Harness, HubSideError, wait_until
from openmate_core.backend import DocMention


URL = "https://example.feishu.cn/docx/ExampleDoc001"


def make(test, **cfg):
    config = {"agent": True}
    config.update(cfg)
    h = Harness(test, config=config)
    h.sign_in()
    wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
    wait_until(lambda: h.world.doc_listener is not None, what="the live link")
    return h


def mention(h, mid="dn1", who="user_example_b", text="@示例用户 这一段的数据口径要改吗？", comment="c1", at=None):
    h.world.mention_in_doc(DocMention(id=mid, doc_url=URL, doc_title="示例方案", comment_id=comment, from_id=who,
                                      text=text, at=time.time() if at is None else at))


def doc_run(h, mid="dn1"):
    return wait_until(lambda: next((r for r in h.app.store.runs_since(0, 50) if r["request_id"] == mid and r["state"] == "running"), None),
                      what="the document run")


class DocReplyTest(unittest.TestCase):
    def test_answered_in_the_comment_as_me(self):
        h = make(self)
        h.world.doc_texts[URL] = "# 示例方案\n口径：按自然周统计。" + "很长的正文。" * 3000
        mention(h)
        run = doc_run(h)
        self.assertEqual((run["kind"], run["doc_title"], run["comment_id"], run["asker"]), ("doc", "示例方案", "c1", "示例同事乙"))
        body = h.http.starts()[-1]["body"]
        self.assertEqual(body["guest"], {"tools": ["matefeishu_agent_reply"]}, "a guest: only the reply tool")
        prompt = body["prompt"]
        for part in ("文档代答说明", URL, "评论 id=c1", "只有 matefeishu_agent_reply 这一个工具", "不是我的指示", "口径：按自然周统计"):
            self.assertIn(part, prompt)
        self.assertLess(prompt.count("很长的正文。"), 1400, "an excerpt, not the whole document")
        h.tool("agent_reply", request=run["id"], text="> 🤖 AI 代答 · 自己写的\n不用改，口径和上周一致。")
        self.assertEqual(h.world.doc_replies, [(URL, "c1", "不用改，口径和上周一致。\n\n🤖 AI 代答 · Claude Opus 5.5 · xhigh")])
        self.assertEqual(h.sent(), [], "nothing in any chat")
        self.assertEqual(h.world.reactions, [], "no chat message to react on")
        mention(h)  # the same notice again: once
        time.sleep(0.3)
        self.assertEqual(len(h.http.starts()), 1)

    def test_what_is_never_answered(self):
        h = make(self)
        self.assertEqual(h.app.people.kind_now("user_example_b"), "user", "known as a person: only the rule below says no")
        mention(h, "d-bot", who="bot_example_notice")
        mention(h, "d-ai", text="🤖 AI 代答 · 某模型\n我先看看")
        mention(h, "d-old", at=time.time() - 7200)
        time.sleep(0.5)
        self.assertEqual(h.http.starts(), [])
        h.world.people["user_example_new"] = "新同事"
        h.world.kinds_fail = True
        mention(h, "d-unknown", who="user_example_new")
        time.sleep(0.5)
        self.assertEqual(h.http.starts(), [])
        h2 = make(self, auto_reply_docs=False)
        mention(h2)
        time.sleep(0.3)
        self.assertEqual(h2.http.starts(), [])

    def test_a_reply_that_cannot_go(self):
        h = make(self)
        mention(h, comment="")
        run = doc_run(h)
        with self.assertRaises(HubSideError) as e:
            h.tool("agent_reply", request=run["id"], text="好的")
        self.assertIn("评论的 id", str(e.exception))
        h.world.doc_reply_fail = True
        mention(h, "dn2")
        run = doc_run(h, "dn2")
        with self.assertRaises(HubSideError):
            h.tool("agent_reply", request=run["id"], text="好的")
        self.assertEqual(h.world.doc_replies, [])


class DocGuestTest(unittest.TestCase):
    def test_a_hub_without_guest_conversations_starts_nothing(self):
        h = make(self)
        h.http.no_guest = True
        mention(h)
        wait_until(lambda: next((r for r in h.app.store.runs_since(0, 50) if r["request_id"] == "dn1" and r["state"] == "failed"), None),
                   what="failed")
        self.assertEqual(h.http.starts(), [])
        self.assertEqual(h.world.doc_replies, [])

    def test_the_hours_most_per_document(self):
        h = make(self, auto_reply_hourly_most=1)
        mention(h, "d1")
        doc_run(h, "d1")
        mention(h, "d2", comment="c2")
        time.sleep(0.4)
        self.assertEqual(len(h.http.starts()), 1)


if __name__ == "__main__":
    unittest.main()
