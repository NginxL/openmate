"""An /agent reply's picture (<connector>_agent_reply's image): offered only where the backend can send pictures, never
to 自动代答; checked before anything goes out (a PNG or JPEG in a temporary directory, not too big, not a link out of
it); sent right after the answer's text, once, as part of the one reply; its failure reported, never tried again."""

import os
import shutil
import tempfile
import threading
import time
import unittest

from harness import wait_until  # noqa: F401
from test_agent import ask, running
from test_wechat_platform import CID, TEAM, act, page, wechat

from openmate_core import images

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


class Pictures(unittest.TestCase):
    def setUp(self):
        # its real path: the picture reader refuses a link on the way (a TMPDIR that is a link under /tmp)
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="openmate-image-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def picture(self, name="flow.png", data=PNG):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def reply_tool(self, h):
        return [t for t in h.c("tools.list")["tools"] if t["name"] == CID + "_agent_reply"][0]


class OfferTest(Pictures):
    def test_the_tool_and_the_prompt_offer_a_picture_only_where_it_can_go(self):
        h = wechat(self)
        self.assertNotIn("image", self.reply_tool(h)["input_schema"]["properties"])
        msg = ask(h, "/agent 画一下流程")
        running(h, msg)
        self.assertNotIn("image", h.http.starts()[-1]["body"]["prompt"].split("/agent 说明", 1)[-1][:2000])
        h.world.images_ok = True
        tool = self.reply_tool(h)
        self.assertIn("image", tool["input_schema"]["properties"])
        self.assertEqual(tool["input_schema"]["required"], ["request", "text"], "the text stays required")
        self.assertIn("同一次调用", tool["description"])
        msg2 = ask(h, "/agent 再画一下")
        running(h, msg2)
        self.assertIn("填进 image", h.http.starts()[-1]["body"]["prompt"])

    def test_unsupported_here_is_refused_before_the_text(self):
        h = wechat(self)
        run = running(h, ask(h, "/agent 画个图"))
        before = len(h.sent())
        with self.assertRaises(Exception) as e:
            h.tool("agent_reply", request=run["id"], text="流程如下", image=self.picture())
        self.assertIn("不能发图片", str(e.exception))
        self.assertEqual(len(h.sent()), before, "nothing went: the Agent may answer in text")
        h.tool("agent_reply", request=run["id"], text="流程如下（文字版）")
        self.assertEqual(len(h.sent()), before + 1)


class SendTest(Pictures):
    def test_text_then_the_picture_once_and_recorded(self):
        h = wechat(self)
        h.world.images_ok = True
        msg = ask(h, "/agent 画一下上线流程")
        run = running(h, msg)
        path = self.picture()
        out = h.tool("agent_reply", request=run["id"], text="上线流程见图。", image=path)
        self.assertIn("图片也发出了", out)
        self.assertEqual(h.sent()[-1]["text"].strip(), "上线流程见图。")
        self.assertEqual(len(h.world.images), 1)
        sent = h.world.images[0]
        self.assertEqual((sent["chat"], sent["size"]), (TEAM, len(PNG)))
        self.assertNotEqual(sent["path"], path, "a copy the core checked, not the Agent's file")
        self.assertTrue(sent["path"].startswith(os.path.join(h.app.data_dir, "agent-images")))
        wait_until(lambda: any(r["text"].startswith("[图片]") for r in h.hub.sent_records), what="the record")
        rec = [r for r in h.hub.sent_records if r["text"].startswith("[图片]")][0]
        self.assertEqual(rec["text"], "[图片] flow.png")
        self.assertEqual(rec["setting"], "agent")
        n = len(h.sent())
        with self.assertRaises(Exception) as e:
            h.tool("agent_reply", request=run["id"], text="再补一张", image=self.picture("b.png", PNG + b"x"))
        self.assertIn("只回复一次", str(e.exception))
        self.assertEqual((len(h.sent()), len(h.world.images)), (n, 1))

    def test_the_same_answer_again_tells_the_picture_again_without_reading_it(self):
        h = wechat(self)
        h.world.images_ok = True
        run = running(h, ask(h, "/agent 画图"))
        path = self.picture()
        first = h.tool("agent_reply", request=run["id"], text="见图", image=path)
        self.assertIn("图片也发出了", first)
        os.remove(path)  # the Agent's file is gone: an answered run never reads it again
        self.assertEqual(h.tool("agent_reply", request=run["id"], text="见图", image=path), first)
        self.assertEqual(len(h.world.images), 1)
        cur = h.app.store.run_get(run["id"])
        self.assertIn(h.world.images[0]["id"], cur["reply_ids"], "a /stop quoting the picture finds the run")

    def test_a_stop_that_lands_while_the_text_goes_keeps_the_picture(self):
        h = wechat(self)
        h.world.images_ok = True
        run = running(h, ask(h, "/agent 画图"))
        h.world.send_delay = 0.6
        out = []
        t = threading.Thread(target=lambda: out.append(
            h.tool("agent_reply", request=run["id"], text="见图", image=self.picture())), daemon=True)
        t.start()
        time.sleep(0.2)
        agent = h.app.agent
        with agent._lock(run["id"]):  # what /stop does (_stop_run), while the text is on its way
            cur = h.app.store.run_get(run["id"])
            cur["state"] = "stopped"
            h.app.store.run_put(cur)
        t.join(5)
        self.assertEqual(h.sent()[-1]["text"].strip(), "见图", "the text had begun: it went")
        self.assertEqual(h.world.images, [], "the picture had not: it stays")
        self.assertIn("任务已经停止", out[0])

    def test_a_jpeg_goes_too(self):
        h = wechat(self)
        h.world.images_ok = True
        run = running(h, ask(h, "/agent 截个图"))
        self.assertIn("图片也发出了", h.tool("agent_reply", request=run["id"], text="见图", image=self.picture("a.jpg", JPG)))
        self.assertTrue(h.world.images[0]["path"].endswith(".jpg"))

    def test_a_refused_picture_is_told_and_the_answer_stands(self):
        h = wechat(self)
        h.world.images_ok = True
        h.world.image_fail = "rejected"
        run = running(h, ask(h, "/agent 画图"))
        out = h.tool("agent_reply", request=run["id"], text="见图", image=self.picture())
        self.assertIn("图片没发出去", out)
        self.assertIn("不要再回复", out)
        self.assertEqual(h.sent()[-1]["text"].strip(), "见图")
        self.assertEqual(h.world.images, [])
        again = h.tool("agent_reply", request=run["id"], text="见图", image=self.picture())
        self.assertIn("已回复", again, "the same answer asked again gets what it got")
        with self.assertRaises(Exception):
            h.tool("agent_reply", request=run["id"], text="补发图片", image=self.picture())
        self.assertEqual(h.world.images, [], "one reply: the picture is never tried again")

    def test_an_unknown_outcome_is_kept_unknown(self):
        h = wechat(self)
        h.world.images_ok = True
        h.world.image_fail = "uncertain"
        run = running(h, ask(h, "/agent 画图"))
        out = h.tool("agent_reply", request=run["id"], text="见图", image=self.picture())
        self.assertIn("结果不确定", out)
        self.assertNotIn("没发出去", out, "unknown is not \"not sent\"")
        got = h.app.store.send_get(_keys(h, "agent:%s:image:" % run["id"])[0])
        self.assertEqual(got["state"], "uncertain")


def _keys(h, prefix):
    rows = h.app.store._db.execute("SELECT key FROM sends WHERE key LIKE ?", (prefix + "%",)).fetchall()
    return [r[0] for r in rows]


class CheckTest(Pictures):
    def refused(self, h, run, image, words):
        n, m = len(h.sent()), len(h.world.images)
        with self.assertRaises(Exception) as e:
            h.tool("agent_reply", request=run["id"], text="见图", image=image)
        self.assertIn(words, str(e.exception))
        self.assertEqual((len(h.sent()), len(h.world.images)), (n, m), "nothing goes when the picture is wrong")

    def test_checked_before_anything_goes(self):
        h = wechat(self)
        h.world.images_ok = True
        saved_roots = images.roots
        images.roots = lambda: [(self.tmp, os.path.realpath(self.tmp))]
        self.addCleanup(setattr, images, "roots", saved_roots)
        run = running(h, ask(h, "/agent 画图"))
        self.refused(h, run, "flow.png", "绝对路径")
        self.refused(h, run, os.path.join(self.tmp, "missing.png"), "找不到")
        self.refused(h, run, self.picture("a.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>"), "PNG 或 JPG")
        self.refused(h, run, self.picture("empty.png", b""), "空的")
        away = tempfile.mkdtemp(prefix="openmate-image-away-", dir=h.app.data_dir)
        outside = os.path.join(away, "secret.png")
        with open(outside, "wb") as f:
            f.write(PNG)
        self.refused(h, run, outside, "临时目录")
        self.refused(h, run, os.path.join(self.tmp, "..", os.path.basename(self.tmp) + "-x", "a.png"), "临时目录")
        link = os.path.join(self.tmp, "link.png")
        os.symlink(outside, link)
        self.refused(h, run, link, "符号链接")
        os.symlink(away, os.path.join(self.tmp, "sub"))  # a directory on the way that leads out
        self.refused(h, run, os.path.join(self.tmp, "sub", "secret.png"), "符号链接")
        os.mkfifo(os.path.join(self.tmp, "pipe.png"))  # never holds the reply
        self.refused(h, run, os.path.join(self.tmp, "pipe.png"), "普通文件")
        saved = images.IMAGE_MOST
        images.IMAGE_MOST = 32
        try:
            self.refused(h, run, self.picture("big.png"), "太大")
        finally:
            images.IMAGE_MOST = saved
        os.mkdir(os.path.join(self.tmp, "dir.png"))
        self.refused(h, run, os.path.join(self.tmp, "dir.png"), "普通文件")
        nested = os.path.join(self.tmp, "a", "b")
        os.makedirs(nested)
        p = os.path.join(nested, "deep.png")
        with open(p, "wb") as f:
            f.write(PNG)
        # after all that, the one reply is still there to give (a picture a few directories down is fine)
        self.assertIn("图片也发出了", h.tool("agent_reply", request=run["id"], text="见图", image=p))

    def test_auto_reply_never_sends_a_picture(self):
        h = wechat(self)
        h.world.images_ok = True
        act(h, "autoreply", {"auto:" + TEAM: "true"})
        self.assertIn("sections", page(h))
        msg = h.world.push(TEAM, "user_example_b", "@示例用户 流程是什么？", at_me=True)
        run = running(h, msg)
        self.assertEqual(run["kind"], "auto")
        self.assertNotIn("填进 image", h.http.starts()[-1]["body"]["prompt"])
        self.refused(h, run, self.picture(), "不能发图片")
        h.tool("agent_reply", request=run["id"], text="周五上线。")
        self.assertEqual(h.world.images, [])


if __name__ == "__main__":
    unittest.main()
