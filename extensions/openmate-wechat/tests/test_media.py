"""Pictures, voice notes, videos, files and forwarded chat records in WeChat messages, against a fake
matewechat-native: what is fetched where, the text the core gets (local paths, or why not), what never leaves this
backend (the CDN keys, the inline bytes), the matewechat_media tool and media(ref)."""

import base64
import json
import os
import sqlite3
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from test_backend import Base  # noqa: E402

from openmate_core.backend import BackendError  # noqa: E402
from openmate_core.wire import RPCError  # noqa: E402
from openmate_wechat import media as wm  # noqa: E402
from openmate_wechat import media_tools  # noqa: E402

KEY = "0123456789abcdef0123456789abcdef"
SILK = b"\x02#!SILK_V3" + b"\x01\x00\x09"


def media_msg(mid, kind, media, chat="1@chatroom", sender="wxid_bob", origin="live", inline=None, mine=False):
    ev = {"event": "message", "origin": origin, "id": mid, "chat": chat, "group": chat.endswith("@chatroom"),
          "sender": sender, "mine": mine, "type": kind, "text": {"IMAGE": "[图片]", "AUDIO": "[语音]", "VIDEO": "[视频]",
                                                                  "CARD": "[聊天记录] 记录"}.get(kind, "[文件] x"),
          "at": int(time.time()), "at_me": False, "at_all": False, "parent": "", "quotable": False, "content": "",
          "msgsource": "", "media": media}
    if inline is not None:
        ev["inline"] = base64.b64encode(inline).decode()
    return ev


def image(mid="p1", mid_url="MID", **kw):
    m = {"kind": "image", "aes_key": KEY, "mid": mid_url, "thumb": "THUMB", "size": 1000, "width": 800, "height": 600}
    m.update(kw)
    return m


class MediaTest(Base):
    def run_events(self, events, until, voice_cmd=""):
        os.environ["MATEWECHAT_FAKE_EVENTS"] = json.dumps(events)
        from openmate_wechat import backend as wb

        b = wb.WeChatBackend(lambda wait=0: os.path.join(HERE, "fake_native.py"), os.path.join(self.dir, "data"),
                             voice_cmd=voice_cmd)
        self.addCleanup(b.close)
        kept = []
        b.on_history = kept.extend
        b.restore({"state": {"k": "v1"}})
        got, _, err = self.listen(b, lambda g, l, e: until(g, kept), timeout=10)
        self.assertEqual(err, [])
        return b, got, kept

    @staticmethod
    def texts(kept, mid):
        return [m.text for m in kept if m.id == mid]

    def test_a_live_picture_is_fetched_and_its_path_reaches_the_core(self):
        b, got, kept = self.run_events([media_msg("p1", "IMAGE", image())],
                                       lambda g, k: any("本地文件" in t for t in self.texts(k, "p1")))
        self.assertEqual([m.id for m in got], ["p1"])
        self.assertEqual(got[0].text, "[图片]", "handed over at once, before the fetch")
        self.assertEqual([x.ref for x in got[0].media], ["wx:p1"])
        text = self.texts(kept, "p1")[-1]
        path = os.path.join(self.dir, "data", "media", "p1.jpg")
        self.assertEqual(text, "[图片] 本地文件：" + path)
        with open(path, "rb") as f:
            self.assertTrue(f.read().startswith(b"\xff\xd8\xff"))
        asked = self.requests("media")[0]
        self.assertEqual((asked["quality"], asked["file_id"], asked["aes_key"]), ("standard", "MID", KEY))
        self.assertNotIn(KEY, text)
        self.assertEqual(b.history("1@chatroom")[0].text, text, "the conversation's history reads the same")
        # media(ref): the picture's bytes, for the conversation page and the knowledge base
        mime, data = b.media("wx:p1", 1 << 20)
        self.assertEqual(mime, "image/jpeg")
        self.assertTrue(data.startswith(b"\xff\xd8"))
        with self.assertRaises(BackendError) as c:
            b.media("wx:p1", 4)
        self.assertEqual(c.exception.kind, "too_large")

    def test_wechats_own_format_falls_back_to_the_thumbnail(self):
        b, _, kept = self.run_events([media_msg("p2", "IMAGE", image(mid_url="WXGF"))],
                                     lambda g, k: any("缩略图" in t for t in self.texts(k, "p2")))
        text = self.texts(kept, "p2")[-1]
        self.assertIn("本地文件（只有缩略图）：" + os.path.join(self.dir, "data", "media", "p2-thumb.jpg"), text)
        self.assertIn("wxgf", text)
        self.assertEqual([r["quality"] for r in self.requests("media")], ["standard", "thumbnail"])

    def test_an_expired_picture_says_so_and_keeps_the_inline_thumbnail(self):
        jpeg = b"\xff\xd8\xff\xe0inline"
        b, _, kept = self.run_events(
            [media_msg("p3", "IMAGE", image(mid_url="EXPIRED", thumb="EXPIRED"), inline=jpeg)],
            lambda g, k: any("过期" in t for t in self.texts(k, "p3")))
        text = self.texts(kept, "p3")[-1]
        self.assertIn("只有缩略图", text)
        self.assertIn("p3-inline.jpg", text)
        self.assertIn("原图没下载到：文件已过期", text)
        # the inline bytes and the keys stay out of the core; the inline bytes are not in the database either
        raw = sqlite3.connect(os.path.join(self.dir, "data", "wechat.db")).execute(
            "SELECT body FROM messages WHERE id='p3'").fetchone()[0]
        self.assertNotIn("inline", json.loads(raw))
        self.assertNotIn(base64.b64encode(jpeg).decode(), raw)

    def test_nothing_fetched_leaves_the_placeholder_with_the_reason(self):
        b, _, kept = self.run_events([media_msg("p4", "IMAGE", image(mid_url="EXPIRED", thumb="EXPIRED"))],
                                     lambda g, k: any("没能下载" in t for t in self.texts(k, "p4")))
        self.assertEqual(self.texts(kept, "p4")[-1], "[图片]（没能下载：文件已过期，微信服务器上取不到了）")
        with self.assertRaises(BackendError) as c:
            b.media("wx:p4", 1 << 20)
        self.assertEqual(c.exception.kind, "not_found")

    def test_an_old_program_is_named_as_the_reason(self):
        os.environ["MATEWECHAT_FAKE_NO_MEDIA"] = "1"
        _, _, kept = self.run_events([media_msg("p5", "IMAGE", image())],
                                     lambda g, k: any("没能下载" in t for t in self.texts(k, "p5")))
        self.assertIn("0.1.10", self.texts(kept, "p5")[-1])

    def test_a_voice_note_becomes_a_wav_and_its_words(self):
        script = os.path.join(self.dir, "stt.py")
        with open(script, "w") as f:
            f.write("import sys\nassert sys.argv[-1].endswith('.wav')\nprint('今晚八点开团')\n")
        voice = {"kind": "voice", "length": 13, "ms": 3000}
        _, _, kept = self.run_events([media_msg("v1", "AUDIO", voice, inline=SILK)],
                                     lambda g, k: any("转文字" in t for t in self.texts(k, "v1")),
                                     voice_cmd="%s -I %s" % (sys.executable, script))
        wav = os.path.join(self.dir, "data", "media", "v1.wav")
        self.assertEqual(self.texts(kept, "v1")[-1], "[语音 3 秒] 音频：%s；转文字：今晚八点开团" % wav)
        asked = self.requests("voice")[0]
        self.assertTrue(asked["silk"].endswith("v1.silk"), "the SILK that came in the message, no download")
        self.assertFalse(os.path.exists(asked["silk"]), "the SILK goes once it is WAV")

    def test_a_voice_note_without_inline_data_is_downloaded_and_says_no_words_without_a_command(self):
        _, _, kept = self.run_events([media_msg("v2", "AUDIO", {"kind": "voice", "length": 900, "ms": 1400})],
                                     lambda g, k: any("音频" in t for t in self.texts(k, "v2")))
        self.assertIn("没有转文字", self.texts(kept, "v2")[-1])
        asked = self.requests("voice")[0]
        self.assertEqual((asked["server_id"], asked["length"]), ("v2", 900))

    def test_a_video_gives_its_cover_and_the_video_within_the_limit(self):
        small = {"kind": "video", "aes_key": KEY, "url": "VID", "thumb": "TH", "size": 2048, "seconds": 9}
        big = dict(small, size=wm.MOST + 1)
        _, _, kept = self.run_events([media_msg("d1", "VIDEO", small), media_msg("d2", "VIDEO", big)],
                                     lambda g, k: self.texts(k, "d1") and self.texts(k, "d2"))
        d1, d2 = self.texts(kept, "d1")[-1], self.texts(kept, "d2")[-1]
        root = os.path.join(self.dir, "data", "media")
        self.assertTrue(d1.startswith("[视频 9 秒] 封面：%s；视频：%s" % (os.path.join(root, "d1-cover.jpg"),
                                                                    os.path.join(root, "d1.mp4"))), d1)
        self.assertIn("封面：" + os.path.join(root, "d2-cover.jpg"), d2)
        self.assertIn("超过 25 MB 上限，没有下载", d2)
        self.assertNotIn("VID", [r["file_id"] for r in self.requests("media") if r["path"].endswith("d2.mp4")])

    def test_a_file_keeps_its_name_and_a_too_big_one_says_so(self):
        f1 = {"kind": "file", "name": "../攻略.pdf", "ext": "pdf", "size": 12, "url": "F", "aes_key": KEY}
        f2 = dict(f1, size=wm.MOST + 1)
        _, _, kept = self.run_events([media_msg("f1", "FILE", f1), media_msg("f2", "FILE", f2)],
                                     lambda g, k: self.texts(k, "f1") and self.texts(k, "f2"))
        path = os.path.join(self.dir, "data", "media", "f1", "_攻略.pdf")
        self.assertEqual(self.texts(kept, "f1")[-1], "[文件] ../攻略.pdf（12 字节） 本地文件：" + path)
        self.assertTrue(os.path.isfile(path))
        self.assertIn("超过 25 MB 上限", self.texts(kept, "f2")[-1])

    def test_a_forwarded_chat_record_is_text_at_once(self):
        record = {"kind": "record", "title": "群聊的聊天记录", "items": [
            {"type": "1", "who": "Bob", "when": "2026-11-17 20:01", "text": "今晚打本"},
            {"type": "2", "who": "Ann"},
            {"type": "8", "who": "Ann", "title": "攻略.pdf"}]}
        _, got, _ = self.run_events([media_msg("r1", "CARD", record)], lambda g, k: g)
        self.assertEqual(got[0].text, "[聊天记录] 群聊的聊天记录（3 条）：\nBob（2026-11-17 20:01）：今晚打本\n"
                                      "Ann：[图片]\nAnn：[文件] 攻略.pdf")
        self.assertEqual(got[0].media, [])
        self.assertEqual(self.requests("media"), [])

    def test_caught_up_media_is_fetched_too(self):
        _, got, kept = self.run_events([media_msg("p6", "IMAGE", image(), origin="catchup")],
                                       lambda g, k: any("本地文件" in t for t in self.texts(k, "p6")))
        self.assertEqual(got, [], "history only")
        self.assertEqual(self.texts(kept, "p6")[0], "[图片]")

    def test_the_media_tool(self):
        b, _, _ = self.run_events([media_msg("p7", "IMAGE", image())],
                                  lambda g, k: any("本地文件" in t for t in self.texts(k, "p7")))
        b.store.add_message({"event": "message", "id": "old1", "chat": "1@chatroom", "type": "IMAGE", "text": "[图片]",
                             "at": 1, "sender": "wxid_bob"})

        class App:
            backend = b
            signed_in = True

        schema, run = media_tools(App())["matewechat_media"]
        self.assertEqual(schema["input_schema"]["required"], ["message"])
        self.assertIn("本地文件：" + os.path.join(self.dir, "data", "media", "p7.jpg"), run({"message": "p7"}))
        n = len(self.requests("media"))
        run({"message": "p7", "again": True})
        self.assertEqual(len(self.requests("media")), n + 1, "again fetches again")
        self.assertIn("没有留下下载信息", run({"message": "old1"}))
        with self.assertRaises(RPCError):
            run({"message": "nope"})

    def test_local_copies_age_out(self):
        b, _, kept = self.run_events([media_msg("p8", "IMAGE", image())],
                                     lambda g, k: any("本地文件" in t for t in self.texts(k, "p8")))
        path = os.path.join(self.dir, "data", "media", "p8.jpg")
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(b.fetcher.prune(now=time.time() + (wm.MEDIA_KEEP_DAYS + 1) * 86400), ["p8"])
        self.assertFalse(os.path.exists(path))
        self.assertIn("本地副本已按 30 天清理", b.history("1@chatroom")[0].text)


    def test_a_video_ref_gives_the_video_and_its_cover_has_its_own(self):
        small = {"kind": "video", "aes_key": KEY, "url": "VID", "thumb": "TH", "size": 2048, "seconds": 9}
        b, got, _ = self.run_events([media_msg("v1", "VIDEO", small)],
                                    lambda g, k: any("视频：" in t for t in self.texts(k, "v1")))
        self.assertEqual([x.ref for x in got[0].media], ["wx:v1", "wx:v1:cover"])
        self.assertEqual(b.media("wx:v1", 1 << 20)[0], "video/mp4", "the video's ref: the video, always")
        self.assertEqual(b.media("wx:v1:cover", 1 << 20)[0], "image/jpeg")

    def test_a_file_is_checked_against_the_size_and_md5_it_came_with(self):
        f = {"kind": "file", "name": "a.pdf", "ext": "pdf", "size": 13, "url": "F", "aes_key": KEY,
             "md5": "0123456789abcdef0123456789abcdef"}
        self.run_events([media_msg("f9", "FILE", f)], lambda g, k: self.texts(k, "f9"))
        asked = [r for r in self.requests("media") if r.get("quality") == "file"][0]
        self.assertEqual((asked["size"], asked["md5"]), (13, "0123456789abcdef0123456789abcdef"))

    def test_another_account_keeps_nothing_of_a_fetch_running_then(self):
        b, _, kept = self.run_events([media_msg("p5", "IMAGE", image())],
                                     lambda g, k: any("本地文件" in t for t in self.texts(k, "p5")))
        f = b.fetcher
        real = f.fetch

        def slow(mid, media):
            out = real(mid, media)
            f.reset()  # another account signs in while this one runs
            return out

        f.fetch = slow
        f.want("p6", image(), force=True)
        f.wait("p6", 5)
        time.sleep(0.2)
        self.assertNotEqual((f.state("p6") or {}).get("state"), "done", "no state written for the old account")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "data", "media", "p6.jpg")), "no file kept")

    def test_a_fetch_again_that_fails_keeps_the_copy_from_before(self):
        b, _, kept = self.run_events([media_msg("p7", "IMAGE", image())],
                                     lambda g, k: any("本地文件" in t for t in self.texts(k, "p7")))
        f = b.fetcher
        before = f.state("p7")["files"]
        f.fetch = lambda mid, media: ({}, "没能下载：网络")
        f.want("p7", image(), force=True)
        f.wait("p7", 5)
        st = f.state("p7")
        self.assertEqual((st["state"], st["files"]), ("done", before))

    def test_beyond_the_total_the_oldest_copies_go(self):
        b, _, kept = self.run_events([media_msg("q1", "IMAGE", image()), media_msg("q2", "IMAGE", image())],
                                     lambda g, k: all(any("本地文件" in t for t in self.texts(k, m)) for m in ("q1", "q2")))
        f = b.fetcher
        f._room_at = 0.0
        size = os.path.getsize(os.path.join(self.dir, "data", "media", "q1.jpg"))
        gone = f.make_room(most=size + 1)  # room for one: the older one goes
        self.assertEqual(len(gone), 1)
        self.assertEqual(f.state(gone[0])["state"], "gone")


    def test_a_video_fetched_again_with_only_its_cover_keeps_the_video(self):
        small = {"kind": "video", "aes_key": KEY, "url": "VID", "thumb": "TH", "size": 2048, "seconds": 9}
        b, _, _ = self.run_events([media_msg("v2", "VIDEO", small)],
                                  lambda g, k: any("视频：" in t for t in self.texts(k, "v2")))
        f = b.fetcher
        old = f.state("v2")["files"]
        f.fetch = lambda mid, media: ({"cover": old["cover"]}, "视频没能下载：网络")
        f.want("v2", small, force=True)
        f.wait("v2", 5)
        self.assertEqual(f.state("v2")["files"].get("video"), old["video"], "the video from before stays")
        self.assertEqual(b.media("wx:v2", 1 << 20)[0], "video/mp4")

    def test_room_is_never_made_from_a_message_being_fetched(self):
        b, _, _ = self.run_events([media_msg("r1", "IMAGE", image()), media_msg("r2", "IMAGE", image())],
                                  lambda g, k: all(any("本地文件" in t for t in self.texts(k, m)) for m in ("r1", "r2")))
        f = b.fetcher
        with f._cv:
            f._busy.update({"r1", "r2"})
        f._room_at = 0.0
        self.assertEqual(f.make_room(most=1), [], "both busy: nothing removed")
        with f._cv:
            f._busy.difference_update({"r1", "r2"})
        f._room_at = 0.0
        self.assertEqual(sorted(f.make_room(most=1)), ["r1", "r2"])


class RecordTextTest(unittest.TestCase):
    def test_long_records_are_cut(self):
        items = [{"type": "1", "who": "A", "text": "x" * 10} for _ in range(wm.RECORD_LINES + 5)]
        text = wm.record_text({"title": "t", "items": items})
        self.assertIn("……还有 5 条", text)
        self.assertLessEqual(len(text), wm.RECORD_CHARS)

    def test_sniff_and_names(self):
        self.assertEqual(wm.sniff(b"\x89PNG\r\n\x1a\n...."), ("png", "image/png"))
        self.assertEqual(wm.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 "), ("webp", "image/webp"))
        self.assertEqual(wm.sniff(b"wxgf"), ("", ""))
        self.assertEqual(wm.safe_name("../../etc/passwd", "f"), "_.._etc_passwd")
        self.assertEqual(wm.safe_name("  ", "f"), "f")


if __name__ == "__main__":
    unittest.main()
