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
        mention(h)
        run = doc_run(h)
        self.assertEqual((run["kind"], run["doc_title"], run["comment_id"], run["asker"]), ("doc", "示例方案", "c1", "示例同事乙"))
        prompt = h.http.starts()[-1]["body"]["prompt"]
        for part in ("文档代答说明", URL, "评论 id=c1", "lark-cli docs +fetch", "不要改文档"):
            self.assertIn(part, prompt)
        h.tool("agent_reply", request=run["id"], text="> 🤖 AI 代答 · 自己写的\n不用改，口径和上周一致。")
        self.assertEqual(h.world.doc_replies, [(URL, "c1", "🤖 AI 代答 · Claude Opus 5.5 · xhigh\n不用改，口径和上周一致。")])
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


if __name__ == "__main__":
    unittest.main()
