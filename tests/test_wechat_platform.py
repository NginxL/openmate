"""The core as the WeChat extension runs it (the fixture backend under openmate-wechat's Platform): its own page and
connector ids, no document search, no /agent progress (WeChat cannot edit a message), and 自动代答 that answers once.
Also the core fixes made with it: a new account forgets the old one's marked groups, and a group's hourly most holds
when two @s come at once."""

import os
import sys
import threading
import time
import unittest

from harness import ROOT, Harness, wait_until
from test_agent import ask, run_of, running

sys.path.insert(0, os.path.join(ROOT, "extensions", "openmate-wechat"))
import openmate_wechat  # noqa: E402

CID = "matewechat"
TEAM = "chat_example_team"


def wechat(test, **cfg):
    config = {"agent": True, "agent_reply_header": " ", "agent_ack_reaction": "", "agent_done_reaction": "",
              "agent_fail_reaction": ""}
    config.update(cfg)
    h = Harness(test, config=config, platform=openmate_wechat.platform())
    h.sign_in()
    wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
    return h


def page(h, contribution="wechat-chats"):
    return h.hub.request("ui.render", {"contribution": contribution, "state": "", "ui_version": 6})


def act(h, aid, values):
    return h.hub.request("ui.action", {"contribution": "wechat-chats", "action": {"id": aid}, "state": "",
                                       "ui_version": 6, "values": values})


class WeChatPlatformTest(unittest.TestCase):
    def test_its_own_page_and_tools(self):
        h = wechat(self)
        self.assertIn("sections", page(h))
        with self.assertRaises(Exception):
            page(h, "chats")  # Feishu's id is not this extension's
        names = [t["name"] for t in h.c("tools.list")["tools"]]
        self.assertEqual(names, [CID + "_" + n for n in ("status", "chats", "messages", "search", "agent_reply")])
        reply = [t for t in h.c("tools.list")["tools"] if t["name"] == CID + "_agent_reply"][0]
        self.assertNotIn("progress", reply["input_schema"]["properties"], "WeChat cannot edit: no progress")
        self.assertIn("只回复一次最终结果", reply["description"])

    def test_agent_sends_no_progress_and_one_answer(self):
        h = wechat(self)
        msg = ask(h, "/agent 总结一下刚才的讨论")
        run = running(h, msg)
        prompt = h.http.starts()[-1]["body"]["prompt"]
        self.assertIn("只回复一次最终结果", prompt)
        self.assertNotIn("progress=true", prompt)
        before = len(h.sent())
        out = h.tool("agent_reply", request=run["id"], text="还在看", progress=True)
        self.assertIn("不能编辑消息", out)
        self.assertEqual(len(h.sent()), before, "no progress message in the chat")
        h.tool("agent_reply", request=run["id"], text="讨论的结论是周五上线。")
        sent = h.sent()[-1]
        self.assertEqual((sent["chat"], sent["text"].strip()), (TEAM, "讨论的结论是周五上线。"))
        self.assertEqual(sent["reply_to"], msg.id)

    def test_auto_reply_in_a_marked_group_answers_once(self):
        h = wechat(self)
        sec = [s for s in page(h)["sections"] if s.get("title") == "自动代答"][0]
        self.assertIn("auto:" + TEAM, [r["key"] for r in sec["items"]])
        out = act(h, "autoreply", {"auto:" + TEAM: "true"})
        self.assertIn("已开启自动代答", out["toast"])
        msg = h.world.push(TEAM, "user_example_b", "@示例用户 这个接口什么时候能好？", at_me=True)
        run = running(h, msg)
        self.assertEqual(run["kind"], "auto")
        self.assertEqual(h.http.starts()[-1]["body"]["guest"], {"tools": [CID + "_agent_reply"]})
        self.assertIn("不能编辑消息", h.tool("agent_reply", request=run["id"], text="进度", progress=True))
        first = h.tool("agent_reply", request=run["id"], text="周五前上线。")
        n = len(h.sent())
        self.assertEqual(h.tool("agent_reply", request=run["id"], text="周五前上线。"), first,
                         "the same reply asked again gets what it got")
        with self.assertRaises(Exception) as caught:
            h.tool("agent_reply", request=run["id"], text="再补充一句")
        self.assertIn("只回复一次", str(caught.exception))
        self.assertEqual(len(h.sent()), n)

    def test_at_all_alone_and_unmarked_groups_are_not_answered(self):
        h = wechat(self)
        h.world.push(TEAM, "user_example_b", "@所有人 下午开会", at_all=True)
        h.world.push(TEAM, "user_example_b", "@示例用户 在吗", at_me=True)  # the group is not marked
        time.sleep(0.5)
        self.assertEqual([r for r in h.app.store.runs_in_chat(TEAM, 50) if r.get("kind") == "auto"], [])


class FeishuUnchangedTest(unittest.TestCase):
    """What the shared core keeps doing for Feishu (it can edit): progress, and more than one /agent reply."""

    def test_feishu_agent_may_reply_twice_and_auto_reply_may_send_progress(self):
        h = Harness(self, config={"agent": True, "agent_reply_header": " ", "agent_ack_reaction": ""})
        h.sign_in()
        wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
        msg = ask(h, "/agent 两件事")
        run = running(h, msg)
        h.tool("agent_reply", request=run["id"], text="第一件做完了。")
        h.tool("agent_reply", request=run["id"], text="第二件也做完了。")
        h.app.store.autoreply_set(TEAM, True)
        at = h.world.push(TEAM, "user_example_b", "@示例用户 怎么样了", at_me=True)
        auto = running(h, at)
        self.assertIn("进度", h.tool("agent_reply", request=auto["id"], text="在查", progress=True))

    def test_ai_marks_read_as_before(self):
        from openmate_core.agent import AI_MARKS, ai_marked

        self.assertTrue(ai_marked("> 🤖 AI 代答 · 模型 · high\n周五上线", AI_MARKS))
        self.assertFalse(ai_marked("@张三 请解释\u2005🤖 AI 代答 是什么意思？", AI_MARKS), "Feishu text as in 0.11.0")


class CoreFixesTest(unittest.TestCase):
    def test_a_new_account_forgets_the_marked_groups(self):
        h = wechat(self)
        h.app.store.autoreply_set(TEAM, True)
        h.app.store.set_people_kinds({"user_example_b": "user"})
        h.app.store.wipe_account_data()
        self.assertEqual(h.app.store.autoreply_chats(), [])
        self.assertEqual(h.app.store.people_kinds(["user_example_b"]), {})

    def test_pruning_runs_never_frees_the_quota(self):
        h = wechat(self, auto_reply_daily_most=1)
        h.app.store.autoreply_set(TEAM, True)
        m1 = h.world.push(TEAM, "user_example_b", "@示例用户 一", at_me=True)
        running(h, m1)
        for r in h.app.store.runs_in_chat(TEAM, 50):
            r["state"] = "done"
            h.app.store.run_put(r)
        h.app.store.runs_prune(time.time() + 1, 0)  # every finished run gone
        self.assertEqual([r for r in h.app.store.runs_in_chat(TEAM, 50) if r.get("kind") == "auto"], [])
        self.assertEqual(h.app.agent.auto_used(TEAM), 1)
        h.world.push(TEAM, "user_example_b", "@示例用户 二", at_me=True)
        time.sleep(0.5)
        self.assertEqual(h.app.agent.auto_used(TEAM), 1, "the day's most holds")

    def test_the_hourly_most_holds_for_two_at_once(self):
        h = wechat(self, auto_reply_hourly_most=1)
        h.app.store.autoreply_set(TEAM, True)
        agent = h.app.agent
        # drive _auto_start directly from two threads at the same moment
        go = threading.Barrier(2)
        now = time.time()

        def start(i):
            row = {"id": "race_%d" % i, "chat_id": TEAM, "sender_id": "user_example_b", "sender_name": "乙",
                   "text": "@示例用户 问题 %d" % i, "at": now, "at_me": True, "mine": False, "root_id": "",
                   "thread_id": "", "position": i}
            go.wait()
            agent._auto_start(row, row["text"], now)

        threads = [threading.Thread(target=start, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        autos = [r for r in h.app.store.runs_in_chat(TEAM, 50) if r.get("kind") == "auto"]
        self.assertEqual(len(autos), 1, "one place in the hour, one run")


if __name__ == "__main__":
    unittest.main()
