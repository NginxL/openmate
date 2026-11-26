"""Markdown /agent replies through the fixture backend, plus a real local PNG render on macOS."""

try:
    import _runtmp  # noqa: F401
except ImportError:
    pass
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zlib

from harness import openmate_feishu, wait_until
from openmate_core import markup, tools
from openmate_core.agent import AgentRunner
from openmate_core.app import ConnectorApp
from openmate_core.fixture import FixtureBackend, tiny_png
from openmate_core.store import Store
from test_wechat_platform import TEAM, openmate_wechat

LONG = "**先说结论**\n\n## 实施步骤\n- 一\n- 二\n- 三"
PLAIN = "先说结论\n\n实施步骤\n• 一\n• 二\n• 三"


class MarkupTest(unittest.TestCase):
    def test_render_receives_a_json_document_with_literal_text_and_styled_runs(self):
        from openmate_core import cards

        text = ('# <script>alert("x")</script>\n## 二\n###### 三\n\n**粗体和`<code>`** 与 '
                '[链接](https://example.org/?a=1&b=2)\n![图](https://example.org/image.png)\n\n'
                '- 一\n- 二\n\n3) 三\n9. 四\n\n> 引用 & <引用>\n> 换行\n\n'
                '```python\n  <tag> & **代码**\n\n\t下一行  \n```')
        mark = '> 🤖 AI · <模型> & "推理"'

        def run(words, bold=False, code=False):
            return {"text": words, "bold": bold, "code": code}

        document = cards.to_document(text, markup.blocks(text), mark)
        with patch.object(cards, "_renderer", return_value=Path("/tmp/renderer")), \
                patch.object(cards, "_run", return_value=0) as invoke:
            cards.render(document, Path("/tmp/reply.png"), Path("/tmp"))
        data = invoke.call_args.args[-1]
        self.assertEqual(json.loads(data), {
            "width": 750, "mark": mark, "blocks": [
                {"kind": "heading", "level": 1, "runs": [run('<script>alert("x")</script>', bold=True)]},
                {"kind": "heading", "level": 2, "runs": [run("二", bold=True)]},
                {"kind": "heading", "level": 3, "runs": [run("三", bold=True)]},
                {"kind": "paragraph", "lines": [[run("粗体和", bold=True), run("<code>", bold=True, code=True),
                                                 run(" 与 "), run("链接 https://example.org/?a=1&b=2")],
                                                [run("![图](https://example.org/image.png)")]]},
                {"kind": "bullets", "items": [{"marker": "•", "runs": [run("一")]},
                                              {"marker": "•", "runs": [run("二")]}]},
                {"kind": "numbers", "items": [{"marker": "3.", "runs": [run("三")]},
                                              {"marker": "4.", "runs": [run("四")]}]},
                {"kind": "quote", "lines": [[run("引用 & <引用>")], [run("换行")]]},
                {"kind": "code", "text": "  <tag> & **代码**\n\n\t下一行  "},
            ],
        })

    def test_plain_text_keeps_code_and_written_list_numbers(self):
        from openmate_core import cards

        text = ("# 标题\n**粗体**、`行内代码`和[链接](http://example.org)\n\n- 一\n+ 二\n* 三\n\n"
                "7) 七\n10. 十\n\n> 第一行\n> 第二行\n\n"
                "```python\n  **原样**\n\n   > x  \n```\n\n_其他_")
        self.assertEqual(cards.plain_text(text, markup.blocks(text)),
                         "标题\n粗体、行内代码和链接 http://example.org\n\n• 一\n• 二\n• 三\n\n"
                         "7) 七\n10. 十\n\n第一行\n第二行\n\n  **原样**\n\n   > x  \n\n_其他_")

    def test_render_thresholds_and_the_markdown_requirement(self):
        from openmate_core import cards

        with tempfile.TemporaryDirectory() as folder, patch.object(cards, "sys", SimpleNamespace(platform="darwin")), \
                patch.object(cards, "render", side_effect=lambda document, path, cache: path.write_bytes(tiny_png())):
            for text, picture in [
                ("**" + "字" * 195 + "**", False),
                ("**" + "字" * 196 + "**", False),
                ("**" + "字" * 197 + "**", True),
                ("- 一\n- 二", False), ("- 一\n- 二\n- 三", True), ("- 一\n- 二\n\n3. 三", True),
                ("**一**\n\n二", False), ("**一**\n\n二\n\n三", True),
                ("# 一", True), ("> 二", True), ("```\n三\n```", True),
                ("字" * 201, False), ("一\n\n二\n\n三", False),
            ]:
                with self.subTest(text=text[:16], length=len(text)), cards.reply(text, folder) as (words, pic, full):
                    self.assertEqual(pic is not None, picture)
                    self.assertEqual(bool(full), picture)
                    if not markup.formatted(text):
                        self.assertEqual(words, text)

    def test_summary_truncates_the_clean_first_paragraph(self):
        from openmate_core import cards

        text = "# 标题\n\n**" + "字" * 90 + "**\n\n# 后续"
        self.assertEqual(cards._summary(text, markup.blocks(text)), "字" * 79 + "…（全文见下图）")

    def test_platform_timeout_and_size_failures_use_plain_text(self):
        from openmate_core import cards, images

        def oversized(document, path, cache):
            with path.open("wb") as output:
                output.truncate(images.IMAGE_MOST + 1)

        with tempfile.TemporaryDirectory() as folder:
            cases = [
                (patch.object(cards, "sys", SimpleNamespace(platform="linux")), "macOS is required"),
                (patch.object(cards, "render", side_effect=oversized), "PNG exceeds the size limit"),
                (patch.object(cards, "_run", side_effect=subprocess.TimeoutExpired("renderer", 20)),
                 "rendering timed out"),
            ]
            with patch.object(cards, "sys", SimpleNamespace(platform="darwin")), \
                    patch.object(cards, "_renderer", return_value=Path(folder) / "renderer"):
                for setup, reason in cases:
                    with self.subTest(reason=reason), setup, self.assertLogs("openmate.cards", "WARNING") as logs:
                        with cards.reply(LONG, folder) as (words, pic, full):
                            self.assertEqual(words, PLAIN)
                            self.assertIsNone(pic)
                            self.assertEqual(full, "")
                        self.assertIn(reason, logs.output[0])


class ReplyTest(unittest.TestCase):
    def ready(self, platform=None, **config):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        app = ConnectorApp(platform or openmate_wechat.platform(), env={})
        app.data_dir = folder.name
        app.store = Store(str(Path(folder.name) / "test.db"))
        self.addCleanup(app.store.close)
        app.backend = FixtureBackend()
        world = app.backend.world
        world.images_ok, world.format_ok = True, False
        app.account, app.creds = world.me, "present"
        app.config = {"agent": True, "agent_reply_header": " ", "auto_reply_header": "🤖 AI 代答", **config}
        app.agent = AgentRunner(app)
        app.hub_call = Mock(return_value={})
        return app

    def run_for(self, app, kind="", sender="me"):
        msg = app.backend.world.make(TEAM, sender, "/agent 总结")
        run = app.agent._new_run(asdict(msg), "总结", time.time())
        run.update(state="running", kind=kind, session_id="test-session")
        if kind == "auto":
            app.store.autoreply_set(TEAM, True)
        elif kind == "doc":
            run.update(doc_url="https://example.org/doc", comment_id="comment")
        app.store.run_put(run)
        return run

    def reply(self, app, run, text=LONG, **args):
        return tools.call_tool(app, {"name": app.cid + "_agent_reply",
                                     "args": {"request": run["id"], "text": text, **args}})["text"]

    def fake_renderer(self, side_effect=None):
        from openmate_core import cards

        platform = patch.object(cards, "sys", SimpleNamespace(platform="darwin"))
        platform.start()
        self.addCleanup(platform.stop)
        renderer = patch.object(cards, "render", side_effect=side_effect or
                                (lambda document, path, cache: path.write_bytes(tiny_png())))
        fake = renderer.start()
        self.addCleanup(renderer.stop)
        return fake

    def test_tool_and_request_explain_markdown_only_with_the_right_capabilities(self):
        app = self.ready()
        world = app.backend.world

        def words():
            tool = next(t for t in tools.read_tools(app) if t["name"].endswith("_agent_reply"))
            return tool["input_schema"]["properties"]["text"]["description"]

        self.assertIn("Markdown", words())
        self.assertIn("短内容按纯文字发", words())
        self.assertIn("有结构的长内容会排成一张图片", app.agent._instructions(self.run_for(app)))
        world.format_ok = True
        self.assertNotIn("Markdown", words())
        world.format_ok = False
        world.images_ok = False
        self.assertNotIn("Markdown", words())

    def test_sends_one_summary_with_the_mark_then_one_image_and_records_both(self):
        app = self.ready(agent_reply_header="> 🤖 AI · {model} · {effort}", agent_mark_effort=True)
        world = app.backend.world
        fake = self.fake_renderer()
        run = self.run_for(app)
        run.update(model="claude-opus-5-5", effort="high")
        app.store.run_put(run)
        mark = "> 🤖 AI · Opus 5.5 · high"
        original = LONG + "\n\n" + mark
        result = self.reply(app, run, text=original)
        self.assertIn("图片也发出了", result)
        self.assertEqual(self.reply(app, run, text=original), result)
        self.assertEqual(fake.call_count, 1)
        document, rendered_path, cache = fake.call_args.args
        self.assertEqual(document["mark"], mark)
        self.assertNotIn("🤖 AI", str(document["blocks"]))
        self.assertEqual(document["blocks"][1]["runs"][0]["text"], "实施步骤")
        self.assertTrue(rendered_path.is_relative_to(Path(app.data_dir).resolve()))
        sent, = world.sent
        self.assertEqual(sent["text"], "先说结论（全文见下图）\n\n" + mark)
        self.assertEqual(sent["reply_to"], run["request_id"])
        image, = world.images
        history = [m.id for m in world.history[TEAM]]
        self.assertLess(history.index(sent["id"]), history.index(image["id"]))
        self.assertFalse(rendered_path.parent.exists(), "the original and checked copy are removed")
        self.assertFalse(Path(image["path"]).exists())
        self.assertEqual(stat.S_IMODE(cache.stat().st_mode), 0o700)
        key = "agent:%s:%s" % (run["id"], hashlib.sha256(original.encode()).hexdigest()[:32])
        self.assertEqual(app.store.send_get(key)["result"]["message_ref"], sent["id"])
        image_key = key + ":image:" + hashlib.sha256(tiny_png()).hexdigest()[:32]
        self.assertEqual(app.store.send_get(image_key)["result"]["message_ref"], image["id"])
        wait_until(lambda: app.hub_call.call_count == 2, what="both sent records")
        self.assertEqual({call.args[1]["message_ref"] for call in app.hub_call.call_args_list},
                         {sent["id"], image["id"]})

    def test_short_reply_sends_clean_text(self):
        app = self.ready()
        self.reply(app, self.run_for(app), text="**通过**：`ok`")
        self.assertEqual(app.backend.world.sent[-1]["text"], "通过：ok")
        self.assertEqual(app.backend.world.images, [])

    def test_render_failure_sends_clean_full_text_and_logs_only_a_reason(self):
        app = self.ready()
        fake = self.fake_renderer(RuntimeError("do not log this"))
        with self.assertLogs("openmate.cards", "WARNING") as logs:
            self.reply(app, self.run_for(app))
        fake.assert_called_once()
        self.assertEqual(len(logs.output), 1)
        self.assertIn("RuntimeError", logs.output[0])
        self.assertNotIn("do not log this", logs.output[0])
        self.assertNotIn("先说结论", logs.output[0])
        self.assertEqual(app.backend.world.sent[-1]["text"], PLAIN)
        self.assertEqual(app.backend.world.images, [])
        self.assertFalse(fake.call_args.args[1].parent.exists())

    def test_fallback_keeps_code_indentation_and_trailing_spaces(self):
        app = self.ready()
        self.fake_renderer(RuntimeError("renderer unavailable"))
        with self.assertLogs("openmate.cards", "WARNING"):
            self.reply(app, self.run_for(app), text="~~~python\n    line()\n    # comment  \n~~~")
        self.assertEqual(app.backend.world.sent[-1]["text"], "    line()\n    # comment  ")

    def test_image_rejection_sends_the_marked_full_text_once(self):
        app = self.ready(agent_reply_header="🤖 AI", agent_mark_position="top")
        world = app.backend.world
        fake = self.fake_renderer()
        run = self.run_for(app, sender="user_example_b")
        world.image_fail = "rejected"
        with patch.object(app.backend, "send_image", wraps=app.backend.send_image) as send_image:
            result = self.reply(app, run)
            self.assertIn("图片没发出去，已补发全文", result)
            self.assertIn("示例服务拒绝了这张图片", result)
            self.assertEqual(self.reply(app, run), result)
            send_image.assert_called_once()
        fake.assert_called_once()
        summary, full = world.sent
        self.assertEqual(summary["text"], "🤖 AI\n先说结论（全文见下图）")
        self.assertEqual(fake.call_args.args[0]["mark"], "🤖 AI")
        self.assertEqual(full["text"], "🤖 AI\n" + PLAIN)
        self.assertEqual(summary["reply_to"], run["request_id"])
        self.assertEqual(full["chat"], summary["chat"])
        self.assertIsNone(full["reply_to"], "the WeChat backend may @ the sender of a quoted message")
        self.assertEqual(full["mentions"], [])
        self.assertEqual(world.images, [])
        key = "agent:%s:%s" % (run["id"], hashlib.sha256(LONG.encode()).hexdigest()[:32])
        self.assertIsNone(app.store.send_get(key + ":image:" + hashlib.sha256(tiny_png()).hexdigest()[:32]))
        self.assertEqual(app.store.send_get(key + ":fulltext")["result"]["message_ref"], full["id"])
        self.assertIn(full["id"], app.store.run_get(run["id"])["reply_ids"])
        wait_until(lambda: app.hub_call.call_count == 2, what="summary and full text records")
        self.assertEqual({call.args[1]["message_ref"] for call in app.hub_call.call_args_list},
                         {summary["id"], full["id"]})
        self.assertFalse(fake.call_args.args[1].parent.exists())

    def test_uncertain_image_does_not_send_full_text(self):
        app = self.ready()
        world = app.backend.world
        fake = self.fake_renderer()
        run = self.run_for(app)
        world.image_fail = "uncertain"
        result = self.reply(app, run)
        self.assertIn("图片的发送结果不确定", result)
        self.assertEqual(self.reply(app, run), result)
        fake.assert_called_once()
        self.assertEqual([s["text"] for s in world.sent], ["先说结论（全文见下图）"])
        key = "agent:%s:%s" % (run["id"], hashlib.sha256(LONG.encode()).hexdigest()[:32])
        image_key = key + ":image:" + hashlib.sha256(tiny_png()).hexdigest()[:32]
        self.assertEqual(app.store.send_get(image_key)["state"], "uncertain")
        self.assertIsNone(app.store.send_get(key + ":fulltext"))

    def test_full_text_rejection_is_not_reported_as_sent(self):
        app = self.ready()
        world = app.backend.world
        self.fake_renderer()
        run = self.run_for(app)
        world.image_fail = "rejected"
        send = app.backend.send

        def reject_full(chat_id, text, *args, **kwargs):
            if text == PLAIN:
                world.send_fail = "rejected"
            return send(chat_id, text, *args, **kwargs)

        with patch.object(app.backend, "send", side_effect=reject_full) as send_text:
            result = self.reply(app, run)
            self.assertIn("补发全文没有发出去", result)
            self.assertNotIn("已补发全文", result)
            self.assertEqual(self.reply(app, run), result)
            self.assertEqual(send_text.call_count, 2)
        self.assertEqual([s["text"] for s in world.sent], ["先说结论（全文见下图）"])

    def test_feishu_does_not_change_even_if_the_backend_can_only_send_plain_text(self):
        app = self.ready(openmate_feishu.platform())
        fake = self.fake_renderer()
        run = self.run_for(app)
        self.assertNotIn("有结构的长内容", app.agent._instructions(run))
        self.reply(app, run)
        self.assertEqual(app.backend.world.sent[-1]["text"], LONG)
        self.assertEqual(app.backend.world.images, [])
        fake.assert_not_called()

    def test_progress_auto_and_explicit_image_do_not_use_cards(self):
        app = self.ready()
        world = app.backend.world
        fake = self.fake_renderer()
        run = self.run_for(app)
        self.assertIn("进度不发", self.reply(app, run, progress=True))
        self.assertEqual(world.sent, [])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder).resolve() / "diagram.png"
            path.write_bytes(tiny_png())
            self.reply(app, run, image=str(path))
        self.assertEqual(world.sent[-1]["text"], LONG)
        self.assertEqual(len(world.images), 1)

        auto = self.run_for(app, kind="auto")
        app.backend.can_mention = False
        self.reply(app, auto)
        self.assertTrue(world.sent[-1]["text"].startswith(LONG))
        self.assertEqual(len(world.images), 1)
        fake.assert_not_called()

    def test_explicit_image_rejection_leaves_the_original_text_as_the_only_message(self):
        app = self.ready()
        world = app.backend.world
        run = self.run_for(app)
        world.image_fail = "rejected"
        path = Path(app.data_dir).resolve() / "diagram.png"
        path.write_bytes(tiny_png())
        result = self.reply(app, run, image=str(path))
        self.assertIn("图片没发出去", result)
        self.assertNotIn("补发全文", result)
        self.assertEqual([s["text"] for s in world.sent], [LONG])
        self.assertEqual(world.images, [])

    def test_document_auto_reply_does_not_use_cards(self):
        platform = openmate_feishu.platform()
        platform.render_cards = True
        app = self.ready(platform, auto_reply_docs=True)
        fake = self.fake_renderer()
        self.reply(app, self.run_for(app, kind="doc"))
        self.assertTrue(app.backend.world.doc_replies[0][2].startswith(LONG))
        fake.assert_not_called()

    def test_a_supported_self_mention_uses_the_same_reply_path(self):
        platform = openmate_wechat.platform()
        platform.self_mention = True  # The fixture can report a self-mention.
        app = self.ready(platform)
        fake = self.fake_renderer()
        world = app.backend.world
        msg = world.make(TEAM, "me", "@示例用户 总结", at_me=True, text_after_mentions="总结")
        with patch.object(app.agent, "_start_run") as start:
            app.agent.on_message({**asdict(msg), "live_at_me": True})
            wait_until(lambda: start.called, what="claimed self-mention")
        run = start.call_args.args[0]
        self.assertEqual(run["request_text"], "总结")
        self.reply(app, run)
        self.assertEqual(world.sent[-1]["text"], "先说结论（全文见下图）")
        self.assertEqual(len(world.images), 1)
        fake.assert_called_once()
        self.assertEqual(fake.call_args.args[0]["mark"], "")


@unittest.skipUnless(sys.platform == "darwin", "requires macOS")
class MacRenderTest(unittest.TestCase):
    def test_renders_a_valid_png_at_twice_the_card_width(self):
        from openmate_core import cards

        with tempfile.TemporaryDirectory() as folder:
            if not Path(cards.XCRUN).is_file() or cards._run([cards.XCRUN, "--no-cache", "--find", "swiftc"],
                                                            Path(folder), 20):
                self.skipTest("xcrun swiftc is unavailable")
            cache = Path(folder) / "agent-cards"
            cache.mkdir(mode=0o700)
            with tempfile.TemporaryDirectory(dir=cache) as work:
                path = Path(work) / "reply.png"
                sample = LONG + "\n\n> 引用\n> 换行\n\n```\n  code()\n    next()\n```\n\n内联 `<code>`"
                cards.render(cards.to_document(sample, markup.blocks(sample), "🤖 AI"), path, cache)
                png = path.read_bytes()
                self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
                self.assertEqual(png[12:16], b"IHDR")
                width, height = struct.unpack_from(">II", png, 16)
                self.assertEqual(width, 1500)
                self.assertGreater(height, 128)
                pos, data = 8, bytearray()
                while pos < len(png):
                    size, kind = struct.unpack_from(">I4s", png, pos)
                    end = pos + 8 + size
                    self.assertEqual(zlib.crc32(png[pos + 4:end]), struct.unpack_from(">I", png, end)[0])
                    if kind == b"IDAT":
                        data.extend(png[pos + 8:end])
                    pos = end + 4
                self.assertEqual(kind, b"IEND")
                self.assertTrue(zlib.decompress(data))
            self.assertFalse(path.exists())
            binary, = cache.iterdir()
            self.assertTrue(binary.name.startswith("render-"))


if __name__ == "__main__":
    unittest.main()
