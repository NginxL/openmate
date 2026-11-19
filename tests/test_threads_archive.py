"""thread.page / thread.media and archive.changes / archive.media."""

import base64
import unittest

from harness import CID, Harness, HubSideError, wait_until
from openmate_core.backend import Chat, Media


class ThreadTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self, config={"history_chats": 5})
        self.h.world.add_chat(Chat("chat_example_big", "group", "示例大群", members=300))
        for i in range(122):
            self.h.world.make("chat_example_big", "user_example_a" if i % 2 else "user_example_b", "历史消息 %d" % i,
                              ago=3000 - i * 10)
        self.h.sign_in()
        wait_until(lambda: self.h.app._catchup_done.is_set(), what="catch-up")

    def test_newest_page_oldest_first(self):
        page = self.h.c("thread.page", thread="chat_example_team", limit=10)
        self.assertEqual(page["chat"], {"id": "chat_example_team", "name": "示例项目群", "kind": "group",
                                        "reply": CID + "_send", "members": 5})
        ats = [m["at"] for m in page["messages"]]
        self.assertEqual(ats, sorted(ats))
        last = page["messages"][-1]
        self.assertEqual(last["text"], "@示例用户 接口文档这周谁来更新？")
        self.assertEqual(last["sender"], {"name": "示例同事乙", "id": "user_example_b"})
        system = [m for m in page["messages"] if m.get("event")]
        self.assertEqual(len(system), 1)
        self.assertNotIn("before", page)  # the start of the chat

    def test_paging_back_reads_older_from_the_service(self):
        stored = self.h.app.store.messages_before("chat_example_big", None, 1000)
        self.assertLess(len(stored), 120)  # the initial history read only the newest
        seen, before, pages = [], None, 0
        while True:
            args = {"thread": "chat_example_big", "limit": 50}
            if before:
                args["before"] = before
            page = self.h.c("thread.page", **args)
            self.assertLessEqual(len(page["messages"]), 50)
            ids = [m["id"] for m in page["messages"]]
            self.assertFalse(set(ids) & set(seen), "a message on two pages")
            if seen:
                self.assertLessEqual(page["messages"][-1]["at"], self.first_at)
            seen += ids
            self.first_at = page["messages"][0]["at"] if page["messages"] else None
            pages += 1
            before = page.get("before")
            if not before:
                break
            self.assertLess(pages, 10)
        self.assertEqual(len(seen), 122)
        self.assertTrue(any(b is not None for _, b, _ in self.h.world.history_calls), "older pages came from history()")

    def test_limit_is_held_to_50(self):
        page = self.h.c("thread.page", thread="chat_example_big", limit=500)
        self.assertEqual(len(page["messages"]), 50)

    def test_media_and_quotes(self):
        page = self.h.c("thread.page", thread="chat_example_dm_a", limit=20)
        pic = page["messages"][-1]
        self.assertEqual(pic["media"], [{"kind": "image", "ref": "img:example-1", "mime": "image/png", "width": 2, "height": 2}])
        media = self.h.c("thread.media", ref="img:example-1")
        self.assertEqual(media["mime"], "image/png")
        self.assertTrue(base64.b64decode(media["data"]).startswith(b"\x89PNG"))
        mine = [m for m in page["messages"] if m.get("mine")]
        self.assertEqual(mine[0]["sender"]["name"], "示例用户")
        q = self.h.world.push("chat_example_dm_a", "user_example_a", "就是这个", parent_id=page["messages"][0]["id"])
        page = wait_until(lambda: (lambda p: p if p["messages"][-1]["id"] == q.id else None)(
            self.h.c("thread.page", thread="chat_example_dm_a", limit=20)), what="the quote")
        self.assertEqual(page["messages"][-1]["reply_to"]["text"], "周五评审的材料准备好了吗？")
        self.assertEqual(page["messages"][-1]["reply_to"]["from"], "示例同事甲")

    def test_media_limits(self):
        with self.assertRaises(HubSideError) as e:
            self.h.c("thread.media", ref="big:%d" % (5 << 20))
        self.assertIn("4 MB", e.exception.message)
        got = self.h.c("archive.media", ref="big:%d" % (5 << 20))
        self.assertEqual(len(base64.b64decode(got["data"])), 5 << 20)
        with self.assertRaises(HubSideError):
            self.h.c("archive.media", ref="big:%d" % (21 << 20))

    def test_unknown_thread(self):
        with self.assertRaises(HubSideError):
            self.h.c("thread.page", thread="chat_nope", limit=10)


class ArchiveTest(unittest.TestCase):
    def follow(self, h, cursor="", limit=100):
        batches = []
        for _ in range(50):
            res = h.c("archive.changes", cursor=cursor, limit=limit)
            self.assertLessEqual(len(res["batches"]), limit)
            if res["batches"] or res["more"]:
                self.assertNotEqual(res["next"], cursor)
            batches += res["batches"]
            cursor = res["next"]
            if not res["more"]:
                return batches, cursor
        self.fail("archive.changes never ended")

    def test_cursor_from_the_start_then_only_new(self):
        h = Harness(self)
        h.sign_in()
        wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
        batches, cursor = self.follow(h, limit=2)
        delivered = [(b["chat"]["id"], m["id"]) for b in batches for m in b["messages"]]
        self.assertEqual(len(delivered), len(set(delivered)), "a message delivered twice")
        self.assertTrue(cursor)
        texts = [m["text"] for b in batches for m in b["messages"]]
        self.assertNotIn("示例同事丙 加入了群聊", texts, "system lines stay out")
        self.assertIn("[图片]", texts)
        # the same cursor again: nothing, and the cursor stays
        res = h.c("archive.changes", cursor=cursor, limit=100)
        self.assertEqual(res, {"batches": [], "next": cursor, "more": False})
        # new messages come after it
        m = h.world.push("chat_example_team", "user_example_a", "新进展", media=[Media("file", ref="file:spec", name="spec.txt")])
        wait_until(lambda: h.app.store.message("chat_example_team", m.id), what="stored")
        res = h.c("archive.changes", cursor=cursor, limit=100)
        self.assertEqual(len(res["batches"]), 1)
        b = res["batches"][0]
        self.assertEqual(b["chat"]["id"], "chat_example_team")
        self.assertEqual(b["chat"]["name"], "示例项目群")
        self.assertEqual(b["chat"]["kind"], "group")
        msg = b["messages"][0]
        self.assertEqual(msg["id"], m.id)
        self.assertEqual(msg["media"], ["file:spec"])
        self.assertEqual(msg["sender_handles"], [{"kind": "lark", "value": "user_example_a"}])
        self.assertEqual(msg["sender_name"], "示例同事甲")
        self.assertTrue(msg["at"].endswith("Z"))

    def test_sticker_pictures_stay_out(self):
        """A sticker reaches the knowledge base as its "[表情]" line, with no picture to download; a picture still has one."""
        h = Harness(self)
        h.sign_in()
        wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
        _, cursor = self.follow(h)
        st = h.world.push("chat_example_team", "user_example_a", "", type="STICKER",
                          media=[Media("sticker", ref="sticker:wave", width=240, height=240)])
        im = h.world.push("chat_example_team", "user_example_a", "", type="IMAGE",
                          media=[Media("image", ref="image:shot", width=800, height=600)])
        wait_until(lambda: h.app.store.message("chat_example_team", im.id), what="stored")
        res = h.c("archive.changes", cursor=cursor, limit=100)
        msgs = {m["id"]: m for b in res["batches"] for m in b["messages"]}
        self.assertEqual(msgs[st.id]["text"], "[表情]")
        self.assertNotIn("media", msgs[st.id])
        self.assertEqual(msgs[im.id]["media"], ["image:shot"])

    def test_batch_fields(self):
        h = Harness(self)
        h.sign_in()
        wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
        batches, _ = self.follow(h)
        by_chat = {}
        for b in batches:
            by_chat.setdefault(b["chat"]["id"], []).extend(b["messages"])
        team = by_chat["chat_example_team"]
        mention = [m for m in team if "接口文档" in m["text"]][0]
        self.assertTrue(mention["mentioned"])
        mine = [m for m in by_chat["chat_example_dm_b"] if m.get("mine")][0]
        self.assertEqual(mine["sender_name"], "示例用户")
        for msgs in by_chat.values():
            ats = [m["at"] for m in msgs]
            self.assertEqual(ats, sorted(ats))
            for m in msgs:
                self.assertTrue(m["id"] and m["at"] and (m["text"].strip() or m.get("media")))

    def test_bad_cursor(self):
        h = Harness(self)
        with self.assertRaises(HubSideError):
            h.c("archive.changes", cursor="not-ours", limit=10)


if __name__ == "__main__":
    unittest.main()
