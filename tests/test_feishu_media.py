"""matefeishu_media (Feishu's read tool for a message's pictures and files) and the hint that names it: the fixture
backend under openmate-feishu's Platform. What is fetched where, the size limit, why nothing is there, and what the
models read (matefeishu_messages, /agent's context and instructions); WeChat's lines are left as they were."""

try:  # first: every temporary directory of this run in one, removed at its end
    import _runtmp  # noqa: F401
except ImportError:  # (started by its path from elsewhere: its directory is not on the path)
    pass
import base64
import hashlib
import json
import os
import unittest

from harness import CID, ROOT, Harness, HubSideError, wait_until
from test_agent import agent, ask, started

from openmate_core import views
from openmate_core.backend import BackendError, Media
from openmate_core.fixture import tiny_png

DM = "chat_example_dm_a"
TEAM = "chat_example_team"
PNG = b"\x89PNG\r\n\x1a\n"


def signed(test, **kw):
    h = Harness(test, **kw)
    from openmate_feishu import media as fm

    old = fm.BASE
    fm.BASE = os.path.join(h.data_dir, "tmp")  # this test's own temporary directory
    os.makedirs(fm.BASE)
    test.addCleanup(setattr, fm, "BASE", old)
    h.sign_in()
    wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
    return h


def picture_row(h):
    return next(r for r in h.app.store.messages_before(DM, None, 50) if r.get("type") == "IMAGE")


def stored(h, chat, msg):
    return wait_until(lambda: h.app.store.message(chat, msg.id), what="the message stored")


def folder_of(h):
    whose = hashlib.sha256(("openmate-feishu:" + h.app.store.meta_get("account_id")).encode()).hexdigest()[:16]
    ours = hashlib.sha256(os.path.abspath(h.data_dir).encode()).hexdigest()[:8]
    return os.path.join(h.data_dir, "tmp", "openmate-feishu-media-%d-%s" % (os.getuid(), ours), whose)


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

    def test_a_picture_is_fetched_into_a_private_temporary_directory(self):
        h = signed(self)
        row = picture_row(h)
        calls = counting(h)
        os.makedirs(os.path.dirname(folder_of(h)), 0o755)  # made before, open to others: closed again
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
        self.assertEqual(oct(os.stat(folder_of(h)).st_mode & 0o777), oct(0o700))
        self.assertEqual(oct(os.stat(os.path.dirname(folder_of(h))).st_mode & 0o777), oct(0o700))
        self.assertNotIn(os.path.join(h.data_dir, "media"), text, "not the extension's data (the agent sandbox closes it)")
        self.assertEqual(calls, [("img:example-1", 25 << 20)], "through media(ref, max_bytes), 25 MB at most")
        # every call fetches again, as the account signed in now; the chat may be named
        self.assertIn(path, h.tool("media", message=row["id"], chat=DM))
        self.assertEqual(len(calls), 2)

    def test_the_picture_itself_goes_to_the_model_when_the_hub_takes_pictures(self):
        h = signed(self, hub_capabilities=["inbox_group", "tool_images"])
        row = picture_row(h)
        out = h.c("tools.call", name=CID + "_media", args={"message": row["id"]})
        self.assertEqual(len(out["images"]), 1)
        img = out["images"][0]
        self.assertEqual(img["mime"], "image/png")
        self.assertEqual(base64.b64decode(img["data"]), tiny_png(), "the picture's own bytes (within the limits)")
        self.assertEqual(img["caption"], "消息 id=%s 的图片" % row["id"])
        self.assertIn("这张图片已经直接附在这条结果里（第 1 张）", out["text"])
        self.assertIn("本地文件：", out["text"], "the file is still there to read")
        self.assertNotIn("img:example-1", json.dumps(out), "the ref (its resource key) is not printed")

    def test_without_the_hubs_word_the_text_alone_as_before(self):
        for caps in (None, ["inbox_group"]):
            h = signed(self, hub_capabilities=caps)
            out = h.c("tools.call", name=CID + "_media", args={"message": picture_row(h)["id"]})
            self.assertNotIn("images", out)
            self.assertIn("可以直接读这个文件看图", out["text"])
            self.assertNotIn("直接附在", out["text"])

    def test_a_picture_the_model_cannot_take_is_said_and_its_file_stays(self):
        h = signed(self, hub_capabilities=["tool_images"])
        from openmate_core import modelimages

        for name in ("_pillow", "_sips"):
            self.addCleanup(setattr, modelimages, name, getattr(modelimages, name))
            setattr(modelimages, name, lambda data, side: None)  # no way to draw it smaller here
        h.app.backend.media = lambda ref, most: ("image/gif", b"GIF89a" + b"\0" * 32)
        out = h.c("tools.call", name=CID + "_media", args={"message": picture_row(h)["id"]})
        self.assertNotIn("images", out)
        self.assertIn("这张图片没法转成模型能看的格式，可以直接读这个文件看图", out["text"])

    def test_each_account_writes_its_own_and_fetches_as_itself(self):
        h = signed(self)
        row = picture_row(h)
        calls = counting(h)
        first = h.tool("media", message=row["id"])
        h.app.store.meta_set("account_id", "user_example_other")  # another account's store (core: _bind_account)
        second = h.tool("media", message=row["id"])
        self.assertEqual(len(calls), 2, "fetched again, as this account")
        self.assertNotEqual(first.split("本地文件：")[1].split("（")[0], second.split("本地文件：")[1].split("（")[0])
        first_path = first.split("本地文件：")[1].split("（")[0]
        self.assertFalse(os.path.exists(os.path.dirname(first_path)), "the account before's files are gone")
        h.app.store.meta_set("account_id", "")
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message=row["id"])
        self.assertIn("正在恢复登录", e.exception.message, "whose files these are must be known")

    def test_the_account_before_is_tidied_even_by_a_failed_call_and_not_another_instance(self):
        h = signed(self)
        row = picture_row(h)
        h.tool("media", message=row["id"])
        before = folder_of(h)
        os.makedirs(os.path.join(before, "sub"))
        os.chmod(os.path.join(before, "sub"), 0o000)
        os.chmod(before, 0o000)  # its files cannot even be listed as it is: made usable first
        other = os.path.join(os.path.dirname(os.path.dirname(before)), "openmate-feishu-media-%d-0123abcd" % os.getuid(),
                             "0123456789abcdef")
        os.makedirs(other)  # another instance of the extension (another data directory): never touched
        with open(os.path.join(other, "1.png"), "wb") as f:
            f.write(PNG)
        os.makedirs(os.path.join(os.path.dirname(before), "0123456789abcdef\n"))  # not an account's name
        h.app.store.meta_set("account_id", "user_example_other")
        with self.assertRaises(HubSideError):
            h.tool("media", message="id=123")  # not even a message id
        self.assertFalse(os.path.exists(before), "tidied though nothing was fetched")
        self.assertTrue(os.path.exists(os.path.join(other, "1.png")))
        self.assertTrue(os.path.isdir(os.path.join(os.path.dirname(before), "0123456789abcdef\n")))

    def test_the_account_before_goes_even_when_this_ones_folder_cannot_be_made(self):
        h = signed(self)
        row = picture_row(h)
        h.tool("media", message=row["id"])
        before = folder_of(h)
        h.app.store.meta_set("account_id", "user_example_other")
        from openmate_feishu import media as fm

        real = fm.os.mkdir

        def full(path, mode=0o777):
            if path == folder_of(h):
                raise OSError(28, "No space left on device")
            return real(path, mode)

        fm.os.mkdir = full
        try:
            with self.assertRaises(HubSideError) as e:
                h.tool("media", message=row["id"])
        finally:
            fm.os.mkdir = real
        self.assertIn("存不到本机", e.exception.message)
        self.assertFalse(os.path.exists(before))

    def test_a_folder_is_made_this_users_own_and_old_files_go(self):
        h = signed(self)
        row = picture_row(h)
        top = os.path.dirname(folder_of(h))
        os.makedirs(folder_of(h))
        os.chmod(top, 0o500)  # this user's, but not writable: made usable again
        stale = os.path.join(folder_of(h), "7000000000000000009.png")
        with open(stale, "wb") as f:
            f.write(PNG)
        os.utime(stale, (1, 1))
        h.tool("media", message=row["id"])
        self.assertEqual(oct(os.stat(top).st_mode & 0o777), oct(0o700))
        self.assertFalse(os.path.exists(stale), "older than a day: gone")
        self.assertTrue(os.path.exists(os.path.join(folder_of(h), row["id"] + ".png")))

    def test_a_failed_write_or_an_empty_answer_leaves_the_file_before(self):
        h = signed(self)
        row = picture_row(h)
        path = os.path.join(folder_of(h), row["id"] + ".png")
        h.tool("media", message=row["id"])
        h.app.backend.media = lambda ref, most: ("image/png", b"")
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message=row["id"])
        self.assertIn("图片下载不了：飞书给回来的是空的", e.exception.message)
        from openmate_feishu import media as fm

        h.app.backend.media = lambda ref, most: ("image/png", PNG + b"new")
        real = fm.os.replace

        def broken(a, b):
            raise OSError(28, "No space left on device")

        fm.os.replace = broken
        try:
            with self.assertRaises(HubSideError) as e:
                h.tool("media", message=row["id"])
        finally:
            fm.os.replace = real
        self.assertIn("存不到本机", e.exception.message)
        with open(path, "rb") as f:
            self.assertFalse(f.read().endswith(b"new"), "the file before is whole")
        self.assertEqual([n for n in os.listdir(folder_of(h)) if n.startswith(".part-")], [], "no partial left")

    def test_never_written_through_a_link(self):
        h = signed(self)
        row = picture_row(h)
        elsewhere = os.path.join(h.data_dir, "elsewhere")
        os.makedirs(elsewhere)
        os.makedirs(os.path.dirname(folder_of(h)))
        kept = os.path.join(elsewhere, "old-but-not-ours.png")
        with open(kept, "wb") as f:
            f.write(PNG)
        os.utime(kept, (1, 1))
        os.symlink(elsewhere, folder_of(h))
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message=row["id"])
        self.assertIn("存不到本机", e.exception.message)
        self.assertIn("不是一个目录", e.exception.message)
        self.assertEqual(os.listdir(elsewhere), ["old-but-not-ours.png"], "nothing written, nothing removed there")
        os.remove(kept)
        # a link where the file goes is replaced by the file itself, not followed
        os.remove(folder_of(h))
        os.makedirs(folder_of(h))
        outside = os.path.join(elsewhere, "outside.png")
        with open(outside, "wb") as f:
            f.write(b"not this")
        os.symlink(outside, os.path.join(folder_of(h), row["id"] + ".png"))
        h.tool("media", message=row["id"])
        self.assertFalse(os.path.islink(os.path.join(folder_of(h), row["id"] + ".png")))
        with open(outside, "rb") as f:
            self.assertEqual(f.read(), b"not this")

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

    def test_a_generic_answer_takes_the_type_the_message_gave(self):
        h = signed(self)
        m = h.world.push(TEAM, "user_example_a", "", type="FILE",
                         media=[Media("file", ref="file:report", name="report", mime="application/pdf")])
        stored(h, TEAM, m)
        h.app.backend.media = lambda ref, most: ("application/octet-stream", b"%PDF-1.4 example")
        text = h.tool("media", message=m.id)
        self.assertIn(os.path.join(folder_of(h), m.id + ".pdf"), text)
        self.assertIn("application/pdf", text)

    def test_more_than_the_limit_is_not_fetched(self):
        h = signed(self)
        m = h.world.push(TEAM, "user_example_a", "", type="FILE",
                         media=[Media("file", ref="big:%d" % ((25 << 20) + 1), name="大文件.zip")])
        stored(h, TEAM, m)
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message=m.id)
        self.assertIn("超过 25 MB", e.exception.message)
        folder = folder_of(h)
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
        self.assertNotIn("接口文档", e.exception.message, "what a message says is never told back")
        undecoded = h.world.push(TEAM, "user_example_a", "", type="IMAGE")  # Feishu said IMAGE, its content not read
        stored(h, TEAM, undecoded)
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message=undecoded.id)
        self.assertIn("没有留下下载信息", e.exception.message)
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
            h.tool("media", message="img_v3_example_key")
        self.assertIn("本地没有这条飞书消息", e.exception.message)
        self.assertNotIn("img_v3", e.exception.message, "a resource key given by mistake is not echoed")
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message="")
        self.assertIn("缺少消息 id", e.exception.message)
        store, h.app.store, h.app.setup_error = h.app.store, None, "本地数据库打不开"
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message="1")
        self.assertIn("飞书扩展有问题：本地数据库打不开", e.exception.message)
        h.app.store, h.app.setup_error = store, ""
        blocked = h.world.push(TEAM, "user_example_a", "", type="IMAGE", blocked=True,
                               media=[Media("image", ref="img:blocked")])
        stored(h, TEAM, blocked)
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message=blocked.id)
        self.assertIn("安全策略", e.exception.message)
        h.app.store._tx(lambda db: db.execute("UPDATE messages SET blocked = 1, text = '旧正文' WHERE id = ?",
                                              (text_msg["id"],)))  # kept before it was blocked
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message=text_msg["id"])
        self.assertIn("安全策略", e.exception.message)
        self.assertNotIn("旧正文", e.exception.message)

    def test_expired_and_not_signed_in(self):
        h = signed(self)
        row = picture_row(h)

        def gone(ref, most):
            raise BackendError("附件不在了（可能已被撤回或过期）", "not_found")

        h.app.backend.media = gone
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message=row["id"])
        self.assertIn("图片下载不了：附件不在了（可能已被撤回或过期）", e.exception.message)
        h.app.creds = "none"
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message=row["id"])
        self.assertIn("飞书还没登录", e.exception.message)

    def test_a_page_instead_of_the_file_is_not_a_file(self):
        h = signed(self)
        m = h.world.push(TEAM, "user_example_a", "", type="FILE",
                         media=[Media("file", ref="file:report", name="report.pdf", mime="application/pdf")])
        stored(h, TEAM, m)
        h.app.backend.media = lambda ref, most: ("text/html", b"<!DOCTYPE html><html>sign in</html>")
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message=m.id)
        self.assertIn("回答的是一个网页", e.exception.message)
        for kind, kind_type in (("voice", "AUDIO"), ("video", "VIDEO")):  # no type recorded, only the kind
            v = h.world.push(TEAM, "user_example_a", "", type=kind_type, media=[Media(kind, ref="file:" + kind)])
            stored(h, TEAM, v)
            with self.assertRaises(HubSideError) as e:
                h.tool("media", message=v.id)
            self.assertIn("回答的是一个网页", e.exception.message, kind)
        page = h.world.push(TEAM, "user_example_a", "", type="FILE",
                            media=[Media("file", ref="file:page", name="a.html", mime="text/html")])
        stored(h, TEAM, page)
        self.assertIn(page.id + ".html", h.tool("media", message=page.id), "an HTML file sent as one is kept")
        tpl = h.world.push(TEAM, "user_example_a", "", type="FILE",
                           media=[Media("file", ref="file:tpl", name="page.html.j2", mime="text/plain")])
        stored(h, TEAM, tpl)
        h.app.backend.media = lambda ref, most: ("text/plain", b"<!DOCTYPE html><html>{{ x }}</html>")
        self.assertIn(tpl.id + ".j2", h.tool("media", message=tpl.id), "what a file holds is not guessed at")

    def test_why_it_cannot_fetch_now(self):
        h = signed(self)
        row = picture_row(h)
        h.app.link = "connecting"  # credentials kept, the session not back yet

        def restoring(ref, most):
            raise BackendError("还没有登录飞书", "expired")

        h.app.backend.media = restoring
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message=row["id"])
        self.assertIn("正在恢复登录", e.exception.message)
        h.app.creds = "loading"
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message=row["id"])
        self.assertIn("正在恢复登录", e.exception.message)
        h.app.link, h.app.link_reason = "error", "连不上代理：请检查代理设置"
        h.app.creds = "present"
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message=row["id"])
        self.assertIn("飞书连不上：连不上代理", e.exception.message)
        h.app.link, h.app.creds = "connecting", "loading"
        h.app.setup_error = "LarkAgentX 的 Python 不对"
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message=row["id"])
        self.assertIn("飞书扩展有问题，下载不了：LarkAgentX 的 Python 不对", e.exception.message)
        h.app.setup_error, h.app.creds = "", "none"
        with self.assertRaises(HubSideError) as e:
            h.tool("media", message=row["id"])
        self.assertIn("扫码登录", e.exception.message)

    def test_an_expired_sign_in_is_said(self):
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
