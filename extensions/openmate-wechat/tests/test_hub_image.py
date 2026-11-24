"""A picture through AgentWechat (WeChatBackend.send_image): staged on the hub, then sent as an image under its own
key; its echo is ours (history, never live); a refusal before or after staging is "not sent", needs_review or a lost
answer is unknown (never sent again); another account there refuses it before anything is uploaded; the program alone
(no hub) cannot send pictures."""

try:  # first: every temporary directory of this run in one, removed at its end
    import _runtmp  # noqa: F401
except ImportError:  # (started by its path from elsewhere: its directory is not on the path)
    pass
import json
import os
import time

from test_hub import GROUP, Base, wait_until
from openmate_core.backend import BackendError, SendRejected  # noqa: E402
from openmate_wechat import backend as wb  # noqa: E402
from openmate_wechat import hub as wh  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4


class ImageTest(Base):
    def picture(self, data=PNG, name="flow.png"):
        p = os.path.join(self.dir, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def posts(self, path):
        return [r for r in self.hub.sent(path) if r[0] == "POST"]

    def unsure(self, b):
        return [k for k in b.store._db_keys() if k.startswith("hub_unsure:")] if hasattr(b.store, "_db_keys") else \
            [r[0] for r in b.store._db.execute("SELECT key FROM kv WHERE key LIKE 'hub_unsure:%'").fetchall()]

    def test_staged_then_sent_and_its_echo_is_ours(self):
        b = self.signed_in()
        self.assertTrue(b.can_send_images)
        got, *_ = self.listen(b)
        mid = b.send_image(GROUP, self.picture())
        up = self.posts("/uploads")
        self.assertEqual(len(up), 1)
        staged = list(self.hub.uploads.values())[0]
        self.assertEqual((staged["name"], staged["mime"], staged["data"]), ("flow.png", "image/png", PNG))
        send = self.posts("/sends")
        self.assertEqual(len(send), 1)
        self.assertEqual(send[0][2], {"threadId": GROUP, "kind": "image", "sourceId": list(self.hub.uploads)[0]})
        self.assertTrue(send[0][3].get("Idempotency-Key", "").startswith("om-"))
        echo = [m for m in self.hub.messages.values() if m["direction"] == "outgoing"][0]
        self.assertEqual(mid, echo["serverId"])
        time.sleep(0.6)
        self.assertEqual(got, [], "our own picture never reaches the core as live")
        kept = self.unsure(b)
        self.assertEqual(len(kept), 1)
        self.assertEqual(json.loads(b.store.get(kept[0]))["server"], mid, "known by its id, on disk too")

    def test_an_echo_before_the_answer_is_still_ours(self):
        self.hub.echo_first = True
        b = self.signed_in()
        got, *_ = self.listen(b)
        b.send_image("wxid_friend", self.picture())
        time.sleep(0.6)
        self.assertEqual(got, [])

    def test_a_lost_answer_is_found_by_its_key_and_its_echo_is_ours(self):
        self.hub.send_outcome = "lost_answer"
        b = self.signed_in()
        got, *_ = self.listen(b)
        mid = b.send_image("wxid_friend", self.picture())
        echo = [m for m in self.hub.messages.values() if m["direction"] == "outgoing"][0]
        self.assertEqual(mid, echo["serverId"], "the job found by its key names WeChat's id (serverMessageId)")
        self.assertEqual(len(self.posts("/sends")), 1, "never sent again")
        time.sleep(0.4)
        self.assertEqual(got, [])

    def test_a_refused_upload_sends_nothing(self):
        self.hub.upload_fail = "upload_too_large"
        b = self.signed_in()
        with self.assertRaises(SendRejected):
            b.send_image(GROUP, self.picture())
        self.assertEqual(self.posts("/sends"), [])
        self.assertEqual(self.unsure(b), [])

    def test_failed_is_not_sent_and_needs_review_is_unknown(self):
        b = self.signed_in()
        self.hub.send_outcome = "failed"
        with self.assertRaises(SendRejected):
            b.send_image(GROUP, self.picture())
        self.assertEqual(self.unsure(b), [])
        self.hub.send_outcome = "needs_review"
        with self.assertRaises(BackendError) as e:
            b.send_image(GROUP, self.picture())
        self.assertNotIsInstance(e.exception, SendRejected, "the core never sends it again")
        self.assertEqual(len(self.unsure(b)), 1, "its echo, if any, is still asked about by its key")

    def test_a_job_that_never_ends_is_unknown(self):
        self.hub.send_outcome = "silent"
        saved = wh.IMAGE_WAIT
        wh.IMAGE_WAIT = 1.0
        self.addCleanup(setattr, wh, "IMAGE_WAIT", saved)
        b = self.signed_in()
        with self.assertRaises(BackendError) as e:
            b.send_image(GROUP, self.picture())
        self.assertNotIsInstance(e.exception, SendRejected)

    def test_another_account_there_refuses_before_anything_is_uploaded(self):
        b = self.signed_in()
        self.hub.me = "wxid_someone_else"
        with self.assertRaises(SendRejected):
            b.send_image(GROUP, self.picture())
        self.assertEqual(self.posts("/uploads"), [])
        self.assertEqual(self.posts("/sends"), [])

    def test_signed_out_there_refuses_before_anything_is_uploaded(self):
        b = self.signed_in()
        self.hub.authenticated = False
        with self.assertRaises(SendRejected):
            b.send_image(GROUP, self.picture())
        self.assertEqual(self.posts("/uploads"), [])

    def test_not_a_png_or_jpeg_is_refused_here(self):
        b = self.signed_in()
        with self.assertRaises(SendRejected):
            b.send_image(GROUP, self.picture(b"GIF89a....", "a.gif"))
        self.assertEqual(self.posts("/uploads"), [])

    def test_the_program_alone_cannot_send_pictures(self):
        b = wb.WeChatBackend(None, os.path.join(self.dir, "native"))
        self.addCleanup(b.close)
        self.assertFalse(b.can_send_images)
        with self.assertRaises(SendRejected):
            b.send_image(GROUP, self.picture())


class ImageEchoAfterRestartTest(Base):
    def test_a_sent_pictures_echo_after_a_restart_is_ours(self):
        self.hub.echo_delay = 1.5
        b = self.signed_in()
        p = os.path.join(self.dir, "flow.png")
        with open(p, "wb") as f:
            f.write(PNG)
        b.send_image("wxid_friend", p)
        b.close()  # gone before the echo came
        b2 = self.signed_in()
        got, *_ = self.listen(b2)
        self.assertTrue(wait_until(lambda: any(m["direction"] == "outgoing" for m in self.hub.messages.values())))
        time.sleep(2.0)
        self.assertEqual(got, [], "told by the id kept on disk")

    def test_an_old_sent_pictures_record_is_dropped(self):
        b = self.signed_in()
        b.store.put("hub_unsure:om-old", json.dumps({"chat": "wxid_friend", "at": time.time() - 3 * 86400,
                                                     "server": "123"}))
        b.close()
        b2 = self.signed_in()
        self.assertFalse(b2.store.get("hub_unsure:om-old"))

    def test_an_unknown_picture_send_kept_on_disk_judges_its_echo_after_a_restart(self):
        self.hub.send_outcome = "needs_review"
        b = self.signed_in()
        p = os.path.join(self.dir, "flow.png")
        with open(p, "wb") as f:
            f.write(PNG)
        with self.assertRaises(BackendError):
            b.send_image("wxid_friend", p)
        key = [r for r in self.hub.sent("/sends") if r[0] == "POST"][0][3]["Idempotency-Key"]
        b.close()
        # the hub learns later that it went out after all: its echo, and the job by its key, name the same message
        job = [s for s in self.hub.sends.values() if s["_key"] == key][0]
        m = self.hub.message("wxid_friend", "", direction="outgoing", origin="local_send", wire=3, commit=False)
        job.update(state="accepted", serverMessageId=m["serverId"])
        b2 = self.signed_in()
        got, *_ = self.listen(b2)
        self.hub.push("message.committed", {"messageId": m["id"], "threadId": "wxid_friend", "origin": "local_send",
                                            "direction": "outgoing"})
        time.sleep(0.8)
        self.assertEqual(got, [], "ours, told by its key after the restart")
        self.assertTrue(wait_until(lambda: not [r for r in b2.store._db.execute(
            "SELECT key FROM kv WHERE key LIKE 'hub_unsure:%'").fetchall()]), "settled: the record is gone")
