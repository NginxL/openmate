"""matefeishu_media (Feishu's read tool for a message's pictures and files) and the hint that names it: the fixture
backend under openmate-feishu's Platform. What is fetched where, the size limit, why nothing is there, and what the
models read (matefeishu_messages, /agent's context and instructions); WeChat's lines are left as they were."""

import hashlib
import json
import os
import unittest

from harness import CID, ROOT, Harness, HubSideError, wait_until
from test_agent import agent, ask, started

from openmate_core import views
from openmate_core.backend import BackendError, Media

DM = "chat_example_dm_a"
TEAM = "chat_example_team"
PNG = b"\x89PNG\r\n\x1a\n"


def signed(test):
    h = Harness(test)
    h.sign_in()
    wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
    return h


def picture_row(h):
    return next(r for r in h.app.store.messages_before(DM, None, 50) if r.get("type") == "IMAGE")


def stored(h, chat, msg):
    return wait_until(lambda: h.app.store.message(chat, msg.id), what="the message stored")


def folder_of(h):
    whose = hashlib.sha256(("openmate-feishu:" + h.app.store.meta_get("account_id")).encode()).hexdigest()[:16]
    return os.path.join(h.data_dir, "media", whose)


def counting(h):
    calls = []
    real = h.app.backend.media

    def media(ref, most):
        calls.append((ref, most))
        return real(ref, most)

    h.app.backend.media = media
    return calls


class MediaToolTest(unittest.TestCase):
    def test_listed_as_a_read_tool_for_the_agent_too(self):
        h = signed(self)
        tools = {t["name"]: t for t in h.c("tools.list")["tools"]}
        media = tools[CID + "_media"]
        self.assertEqual(media["input_schema"]["required"], ["message"])
        self.assertIn("只读", media["description"])
        self.assertIn("不会发任何消息", media["description"])
        self.assertNotIn(CID + "_media", [t["name"] for t in h.c("actions.list")["tools"]], "not a write")
        with open(os.path.join(ROOT, "extensions", "openmate-feishu", "persona-extension.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertIn(CID + "_media", manifest["contributes"]["connectors"][0]["agent_tools"], "the Agent gets it too")

    def test_a_picture_is_fetched_into_the_data_directory(self):
        h = signed(self)
        row = picture_row(h)
        calls = counting(h)
        text = h.tool("media", message=row["id"])
        path = os.path.join(folder_of(h), row["id"] + ".png")
        self.assertIn("图片 本地文件：" + path, text)
        self.assertIn("image/png", text)
        self.assertIn("2×2", text)
        self.assertIn("可以直接读这个文件看图", text)
        self.assertIn("id=" + row["id"], text)
        self.assertNotIn("img:example-1", text, "the ref (its resource key) is not printed")
        with open(path, "rb") as f:
            self.assertTrue(f.read().startswith(PNG))
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), oct(0o600))
        self.assertEqual(calls, [("img:example-1", 25 << 20)], "through media(ref, max_bytes), 25 MB at most")
        # the kept copy, unless again; the chat may be named
        self.assertIn(path, h.tool("media", message=row["id"], chat=DM))
        self.assertEqual(len(calls), 1)
        self.assertIn(path, h.tool("media", message=row["id"], again=True))
        self.assertEqual(len(calls), 2)
        self.assertIn(path, h.tool("media", id=row["id"]), "the hint's own word for it works too")
        self.assertEqual(len(calls), 2)

    def test_another_account_never_gets_the_copies_kept(self):
        h = signed(self)
        row = picture_row(h)
        calls = counting(h)
        first = h.tool("media", message=row["id"])
        h.app.store.meta_set("account_id", "user_example_other")  # another account's store (core: _bind_account)
        second = h.tool("media", message=row["id"])
        self.assertEqual(len(calls), 2, "fetched again, as this account")
        self.assertNotEqual(first.split("本地文件：")[1].split("（")[0], second.split("本地文件：")[1].split("（")[0])
        h.app.store.meta_set("account_id", "")
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message=row["id"])
        self.assertIn("飞书还没登录", e.exception.message, "whose copies these are must be known")

    def test_again_keeps_the_old_copy_until_the_new_one_is_in(self):
        h = signed(self)
        row = picture_row(h)
        path = os.path.join(folder_of(h), row["id"] + ".png")
        h.tool("media", message=row["id"])

        def down(ref, most):
            raise BackendError("连不上飞书", "unreachable")

        h.app.backend.media = down
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message=row["id"], again=True)
        self.assertIn("图片下载不了：连不上飞书", e.exception.message)
        self.assertTrue(os.path.isfile(path), "the copy kept before is still there")
        self.assertIn(path, h.tool("media", message=row["id"]))

    def test_a_kept_copy_is_told_by_its_bytes_and_never_a_link(self):
        h = signed(self)
        m = h.world.push(TEAM, "user_example_a", "", type="FILE",
                         media=[Media("file", ref="file:memo", name="memo.jpg", mime="application/pdf")])
        stored(h, TEAM, m)
        h.app.backend.media = lambda ref, most: ("application/pdf", b"%PDF-1.4 example")
        first = h.tool("media", message=m.id)
        again = h.tool("media", message=m.id)  # the kept copy: named .jpg, but not a picture
        for text in (first, again):
            self.assertIn("application/pdf", text)
            self.assertNotIn("看图", text)
        # a link put where a copy would be is not handed out
        n = h.world.push(TEAM, "user_example_a", "", type="FILE", media=[Media("file", ref="file:spec", name="a.txt")])
        stored(h, TEAM, n)
        folder = folder_of(h)
        outside = os.path.join(h.data_dir, "outside.txt")
        with open(outside, "w") as f:
            f.write("not this")
        os.symlink(outside, os.path.join(folder, n.id + ".txt"))
        h.app.backend.media = lambda ref, most: ("text/plain", b"the real one")
        text = h.tool("media", message=n.id)
        with open(os.path.join(folder, n.id + ".txt")) as f:
            self.assertEqual(f.read(), "the real one")
        self.assertFalse(os.path.islink(os.path.join(folder, n.id + ".txt")))
        self.assertIn(n.id + ".txt", text)

    def test_a_file_keeps_its_extension_and_name(self):
        h = signed(self)
        m = h.world.push(TEAM, "user_example_a", "", type="FILE", media=[Media("file", ref="file:spec", name="接口说明.txt")])
        stored(h, TEAM, m)
        text = h.tool("media", message=m.id)
        path = os.path.join(folder_of(h), m.id + ".txt")
        self.assertIn("文件「接口说明.txt」 本地文件：" + path, text)
        self.assertNotIn("看图", text)
        with open(path, encoding="utf-8") as f:
            self.assertIn("示例文件", f.read())

    def test_more_than_the_limit_is_not_fetched(self):
        h = signed(self)
        m = h.world.push(TEAM, "user_example_a", "", type="FILE",
                         media=[Media("file", ref="big:%d" % ((25 << 20) + 1), name="大文件.zip")])
        stored(h, TEAM, m)
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message=m.id)
        self.assertIn("超过 25 MB", e.exception.message)
        folder = os.path.join(h.data_dir, "media")
        self.assertFalse(os.path.isdir(folder) and os.listdir(folder), "nothing kept")
        # a backend that hands back more than it was allowed is held to the limit too
        h.app.backend.media = lambda ref, most: ("application/zip", b"\0" * (most + 1))
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message=m.id)
        self.assertIn("超过 25 MB", e.exception.message)

    def test_why_there_is_nothing(self):
        h = signed(self)
        text_msg = h.app.store.messages_before(TEAM, None, 50)[-1]
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message=text_msg["id"])
        self.assertIn("没有图片或文件", e.exception.message)
        no_ref = h.world.push(TEAM, "user_example_a", "", type="IMAGE", media=[Media("image")])
        stored(h, TEAM, no_ref)
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message=no_ref.id)
        self.assertIn("没有留下下载信息", e.exception.message)
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message="7000000000000000001")
        self.assertIn("本地没有这条飞书消息", e.exception.message)
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message="../../etc/passwd")
        self.assertIn("消息 id 不对", e.exception.message)
        self.assertNotIn("passwd", e.exception.message, "what was given is not echoed (a ref has its key in it)")
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message="")
        self.assertIn("缺少消息 id", e.exception.message)
        blocked = h.world.push(TEAM, "user_example_a", "", type="IMAGE", blocked=True,
                               media=[Media("image", ref="img:blocked")])
        stored(h, TEAM, blocked)
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message=blocked.id)
        self.assertIn("安全策略", e.exception.message)

    def test_expired_and_not_signed_in(self):
        h = signed(self)
        row = picture_row(h)

        def gone(ref, most):
            raise BackendError("附件不在了（可能已被撤回或过期）", "not_found")

        h.app.backend.media = gone
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message=row["id"])
        self.assertIn("图片下载不了：附件不在了（可能已被撤回或过期）", e.exception.message)
        h.app.creds = "absent"
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message=row["id"])
        self.assertIn("飞书还没登录", e.exception.message)

    def test_an_expired_sign_in_is_said_even_with_a_kept_copy(self):
        h = signed(self)
        row = picture_row(h)
        h.tool("media", message=row["id"])
        h.app.link = "expired"
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message=row["id"])
        self.assertIn("登录已失效", e.exception.message)


class HintTest(unittest.TestCase):
    def test_messages_say_how_to_see_a_picture(self):
        h = signed(self)
        row = picture_row(h)
        text = h.tool("messages", chat=DM)
        self.assertIn("[图片]（看图：%s_media message=%s）" % (CID, row["id"]), text)
        plain = [line for line in text.splitlines() if "周五评审" in line][0]
        self.assertNotIn("看图", plain)

    def test_a_file_says_download(self):
        h = signed(self)
        m = h.world.push(TEAM, "user_example_a", "", type="FILE", media=[Media("file", ref="file:x", name="a.pdf")])
        stored(h, TEAM, m)
        self.assertIn("[文件] a.pdf（下载文件：%s_media message=%s）" % (CID, m.id), h.tool("messages", chat=TEAM))

    def test_agent_context_and_instructions_name_the_tool(self):
        h = agent(self)
        pic = h.world.push(TEAM, "user_example_a", "", type="IMAGE", media=[Media("image", ref="img:example-2")])
        stored(h, TEAM, pic)
        ask(h, "/agent 这个是什么", root_id=pic.id, parent_id=pic.id)
        prompt = started(h)["body"]["prompt"]
        hint = "[图片]（看图：%s_media message=%s）" % (CID, pic.id)
        self.assertIn(hint, prompt.split('title="话题里的消息"')[1].split("</persona-context>")[0])
        self.assertIn(hint, prompt.split('title="引用的消息"')[1].split("</persona-context>")[0])
        instructions = prompt.split('title="/agent 说明"')[1].split("</")[0]
        self.assertIn("消息里的图片和文件用 %s_media 看" % CID, instructions)
        self.assertIn("不要用 lark-cli", instructions)

    def test_an_auto_reply_has_only_its_reply_tool_and_no_hint(self):
        h = agent(self)
        h.hub.request("ui.action", {"contribution": "chats", "action": {"id": "autoreply"}, "state": "",
                                    "ui_version": 6, "values": {"auto:" + TEAM: "true"}})
        pic = h.world.push(TEAM, "user_example_a", "", type="IMAGE", media=[Media("image", ref="img:example-3")])
        stored(h, TEAM, pic)
        h.world.push(TEAM, "user_example_b", "@示例用户 这张图是什么？", at_me=True, parent_id=pic.id)
        body = started(h)["body"]
        self.assertEqual(body["guest"], {"tools": [CID + "_agent_reply"]})
        self.assertIn("[图片]", body["prompt"])
        self.assertNotIn(CID + "_media", body["prompt"], "a guest cannot call it: no hint")

    def test_the_assistant_is_told_too(self):
        path = os.path.join(ROOT, "extensions", "openmate-feishu", "assistant", "instructions.md")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("看飞书消息里的图片和文件用 `matefeishu_media`，不要用 lark-cli", text)

    def test_without_a_media_tool_lines_are_as_before(self):
        row = {"id": "m1", "type": "IMAGE", "text": "", "at": 0, "sender_name": "甲",
               "media": [{"kind": "image", "ref": "wx:m1"}]}
        self.assertTrue(views.log_line(row, None).endswith(": [图片]"), "WeChat names no tool: unchanged")
        self.assertTrue(views.log_line(row, None, media_tool="x_media").endswith("[图片]（看图：x_media message=m1）"))
        self.assertTrue(views.log_line(dict(row, blocked=True), None, media_tool="x_media").endswith("[图片]"))


if __name__ == "__main__":
    unittest.main()
