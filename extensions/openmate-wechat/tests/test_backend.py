"""The WeChat backend against a fake matewechat-native: sign-in, which messages reach the core (live only), the
cursor after its messages, a rotated sign-in sealed before it is used, sends (quote + @ only for someone else's
message, what counts as surely not sent) and the program going away."""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
EXT = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(EXT))
for p in (os.path.join(ROOT, "core"), EXT):
    if p not in sys.path:
        sys.path.insert(0, p)

from openmate_core.backend import BackendError, CredentialsExpired, SendRejected  # noqa: E402
from openmate_wechat import backend as wb  # noqa: E402
from openmate_wechat.native import clean_env  # noqa: E402

FAKE = os.path.join(HERE, "fake_native.py")


def msg(mid, chat, sender, text, origin="live", mine=False, at_me=False, at_all=False, at=None, parent=""):
    return {"event": "message", "origin": origin, "id": mid, "chat": chat, "group": chat.endswith("@chatroom"),
            "sender": sender, "mine": mine, "type": "TEXT", "text": text, "at": at or int(time.time()),
            "at_me": at_me, "at_all": at_all, "parent": parent}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="matewechat-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.log = os.path.join(self.dir, "requests.jsonl")
        self.env_saved = dict(os.environ)
        self.addCleanup(self._restore_env)
        os.environ["FAKE_LOG"] = self.log
        os.environ["FAKE_EVENTS"] = "[]"

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self.env_saved)

    def backend(self, events=None):
        os.environ["FAKE_EVENTS"] = json.dumps(events or [])
        b = wb.WeChatBackend(lambda wait=0: FAKE, os.path.join(self.dir, "data"))
        self.addCleanup(b.close)
        return b

    def requests(self, op=None):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        return [r for r in rows if op is None or r.get("op") == op]

    def listen(self, b, until, timeout=5.0):
        got, links = [], []
        stop = threading.Event()
        err = []

        def run():
            try:
                b.listen(got.append, lambda s, r: links.append(s), stop)
            except Exception as e:  # noqa: BLE001
                err.append(e)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        deadline = time.time() + timeout
        while time.time() < deadline and not until(got, links, err):
            time.sleep(0.02)
        stop.set()
        t.join(5)
        return got, links, err


class SignInTest(Base):
    def test_qr_then_confirmed(self):
        b = self.backend()
        qr = b.login_start()
        self.assertEqual(qr.url, "https://login.weixin.qq.com/l/fake")
        self.assertEqual([b.login_poll(qr.state) for _ in range(3)], ["waiting", "scanned", "confirmed"])
        done = b.login_finish(qr.state)
        self.assertEqual(done.credentials, {"v": 1, "state": {"k": "v1"}})
        self.assertEqual((done.account.id, done.account.name), ("wxid_me", "我"))
        b.login_start()  # a new QR on the same device
        self.assertEqual(self.requests("login_start")[-1]["device"], {"device": 1})

    def test_restore_and_expired(self):
        b = self.backend()
        self.assertEqual(b.restore({"v": 1, "state": {"k": "v1"}}).id, "wxid_me")
        with self.assertRaises(CredentialsExpired):
            b.restore({"v": 1, "state": {"k": "bad"}})
        with self.assertRaises(CredentialsExpired):
            b.restore({})

    def test_another_account_wipes_the_old_ones_messages(self):
        b = self.backend()
        b.restore({"state": {"k": "v1"}})
        b.store.add_message(msg("m1", "1@chatroom", "wxid_bob", "hi"))
        b.restore({"state": {"k": "v2", "who": "wxid_other"}})
        self.assertEqual(b.history("1@chatroom"), [])


class ListenTest(Base):
    def test_only_live_messages_reach_the_core(self):
        events = [
            {"event": "link", "up": True},
            msg("c1", "1@chatroom", "wxid_bob", "while away", origin="catchup"),
            msg("e1", "1@chatroom", "wxid_me", "our reply", origin="local_send", mine=True),
            msg("l1", "1@chatroom", "wxid_bob", "@我 在吗", at_me=True),
            msg("l2", "1@chatroom", "wxid_me", "/agent 总结", mine=True),
            msg("l1", "1@chatroom", "wxid_bob", "@我 在吗", at_me=True),  # the same message again
            {"event": "cursor", "cursor": "Y3Vyc29y"},
        ]
        b = self.backend(events)
        b.restore({"state": {"k": "v1"}})
        got, links, err = self.listen(b, lambda g, l, e: len(g) >= 2 and b.store.get("cursor"))
        self.assertEqual(err, [])
        self.assertEqual([m.id for m in got], ["l1", "l2"])
        self.assertTrue(got[0].at_me and not got[0].mine and got[0].sender_id == "wxid_bob")
        self.assertTrue(got[1].mine and got[1].text == "/agent 总结")
        self.assertEqual(links[:1], ["up"])
        self.assertEqual(b.store.get("cursor"), "Y3Vyc29y")
        self.assertEqual([m.id for m in b.history("1@chatroom")], ["c1", "e1", "l1", "l2"], "all kept for history")
        self.assertIn("stop", [r["op"] for r in self.requests()])

    def test_cursor_saved_from_listen_on_next_start(self):
        b = self.backend([{"event": "cursor", "cursor": "QUJD"}])
        b.restore({"state": {"k": "v1"}})
        self.listen(b, lambda g, l, e: b.store.get("cursor") == "QUJD")
        b.listen  # noqa: B018
        self.listen(b, lambda g, l, e: len(self.requests("listen")) >= 2, timeout=2)
        self.assertEqual(self.requests("listen")[-1]["cursor"], "QUJD")

    def test_auth_lost_raises_expired(self):
        b = self.backend([{"event": "auth_lost", "message": "x"}])
        b.restore({"state": {"k": "v1"}})
        _, _, err = self.listen(b, lambda g, l, e: e)
        self.assertIsInstance(err[0], CredentialsExpired)

    def test_rotated_sign_in_is_sealed_then_acked(self):
        b = self.backend([{"event": "persist", "seq": 1, "state": {"k": "v2"}},
                          {"event": "persist", "seq": 2, "state": {"k": "v3"}}])
        sealed = []
        b.on_credentials_sync = lambda c: sealed.append(c) or c["state"]["k"] != "v3"
        b.restore({"state": {"k": "v1"}})
        self.listen(b, lambda g, l, e: len(self.requests("persist_ack")) >= 2)
        acks = self.requests("persist_ack")
        self.assertEqual([(a["seq"], a["ok"]) for a in acks], [(1, True), (2, False)])
        self.assertEqual(sealed, [{"v": 1, "state": {"k": "v2"}}, {"v": 1, "state": {"k": "v3"}}])
        self.assertEqual(b._state, {"k": "v2"}, "a sign-in that could not be sealed is not kept")

    def test_program_exit_ends_listen(self):
        b = self.backend([{"event": "link", "up": True}, {"event": "exit"}])
        b.restore({"state": {"k": "v1"}})
        _, _, err = self.listen(b, lambda g, l, e: e)
        self.assertIsInstance(err[0], BackendError)
        self.assertEqual(err[0].kind, "unreachable")
        # the next listen starts the program again and gives it the sign-in
        os.environ["FAKE_EVENTS"] = "[]"
        self.listen(b, lambda g, l, e: len(self.requests("restore")) >= 2, timeout=3)
        self.assertGreaterEqual(len(self.requests("restore")), 2)


class SendTest(Base):
    def setUp(self):
        super().setUp()
        self.b = self.backend()
        self.b.restore({"state": {"k": "v1"}})
        self.b.store.add_message(msg("q1", "1@chatroom", "wxid_bob", "@我 问题", at_me=True))
        self.b.store.add_message(msg("q2", "1@chatroom", "wxid_me", "/agent 帮我", mine=True))

    def test_answering_someone_quotes_and_ats_them(self):
        self.assertEqual(self.b.send("1@chatroom", "答复", reply_to="q1"), "srv_%d" % self.requests("send")[-1]["id"])
        r = self.requests("send")[-1]
        self.assertEqual(r["reply_to"], "q1")
        self.assertEqual(r["mentions"], [{"id": "wxid_bob", "name": "群里的鲍勃"}], "the group nickname, not a remark")
        self.assertEqual(r["reply_name"], "群里的鲍勃")

    def test_answering_my_own_agent_request_ats_nobody(self):
        self.b.send("1@chatroom", "结果", reply_to="q2")
        r = self.requests("send")[-1]
        self.assertEqual(r["reply_to"], "q2")
        self.assertNotIn("mentions", r)

    def test_outcomes(self):
        with self.assertRaises(SendRejected):
            self.b.send("1@chatroom", "REJECT")
        with self.assertRaises(SendRejected):
            self.b.send("1@chatroom", "NOTSENT")
        with self.assertRaises(BackendError) as c:
            self.b.send("1@chatroom", "UNCERTAIN")
        self.assertNotIsInstance(c.exception, SendRejected, "maybe sent: never sent again")
        with self.assertRaises(BackendError) as c:
            self.b.send("1@chatroom", "DIE")
        self.assertNotIsInstance(c.exception, SendRejected)


class NamesTest(Base):
    def test_names_and_kinds(self):
        b = self.backend()
        b.restore({"state": {"k": "v1"}})
        self.assertEqual(b.user_name("wxid_bob", "1@chatroom"), "群里的鲍勃")
        self.assertEqual(b.user_name("wxid_bob"), "备注b")
        self.assertEqual(b.chat_info("wxid_bob").name, "备注b")
        self.assertEqual(b.chat_info("1@chatroom").members, 2)
        self.assertEqual(b.people_kinds(["wxid_bob", "gh_abc", "abc@im.bot", "filehelper", "x@openim", "1@chatroom"]),
                         {"wxid_bob": "user", "gh_abc": "bot", "abc@im.bot": "bot", "filehelper": "bot",
                          "x@openim": "user"})

    def test_proxy_variables_never_reach_the_program(self):
        env = clean_env({"PATH": "/bin", "https_proxy": "http://127.0.0.1:7897", "ALL_PROXY": "socks5://x"})
        self.assertEqual(env, {"PATH": "/bin"})


if __name__ == "__main__":
    unittest.main()
