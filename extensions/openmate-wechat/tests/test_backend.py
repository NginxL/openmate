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
            "at_me": at_me, "at_all": at_all, "parent": parent, "quotable": True, "content": text, "msgsource": ""}


class Base(unittest.TestCase):
    def setUp(self):
        wb.BUSINESS.clear()  # what one test learnt is not the next one's
        wb.PERSONS.clear()
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

    def test_an_older_listens_events_are_ignored(self):
        b = self.backend([dict(msg("old", "1@chatroom", "wxid_me", "/agent 旧账号的", mine=True), gen=99),
                          msg("new", "1@chatroom", "wxid_bob", "hi")])
        b.restore({"state": {"k": "v1"}})
        got, _, _ = self.listen(b, lambda g, l, e: len(g) >= 1)
        time.sleep(0.2)
        self.assertEqual([m.id for m in got], ["new"])
        self.assertIsNone(b.store.seen("old"))
        stops = self.requests("stop")
        self.assertTrue(stops and all(s.get("gen") == 1 for s in stops), "a stop names its own listen")

    def test_at_me_among_at_all_is_not_at_me(self):
        b = self.backend([msg("a1", "1@chatroom", "wxid_bob", "@所有人 @我", at_me=True, at_all=True)])
        b.restore({"state": {"k": "v1"}})
        got, _, _ = self.listen(b, lambda g, l, e: len(g) >= 1)
        self.assertEqual((got[0].at_me, got[0].at_all), (False, True))

    def test_a_message_the_core_could_not_take_comes_again(self):
        b = self.backend([msg("f1", "1@chatroom", "wxid_bob", "hi"), {"event": "cursor", "cursor": "TkVX"}])
        b.restore({"state": {"k": "v1"}})
        b.store.put("cursor", "T0xE")
        calls = []

        def broken(m):
            calls.append(m.id)
            raise RuntimeError("the core failed")

        stop = threading.Event()
        t = threading.Thread(target=lambda: b.listen(broken, lambda s, r: None, stop), daemon=True)
        t.start()
        deadline = time.time() + 5
        while time.time() < deadline and not calls:
            time.sleep(0.02)
        time.sleep(0.3)
        stop.set()
        t.join(5)
        self.assertEqual(b.store.get("cursor"), "T0xE", "the cursor waits for the message")
        self.assertEqual(b.store.seen("f1")[1], False)
        # after a restart the same message comes back as caught-up: the core gets it now
        os.environ["FAKE_EVENTS"] = json.dumps([msg("f1", "1@chatroom", "wxid_bob", "hi", origin="catchup")])
        b._native.close()
        got, _, _ = self.listen(b, lambda g, l, e: len(g) >= 1)
        self.assertEqual([m.id for m in got], ["f1"])
        self.assertEqual(b.store.seen("f1")[1], True)

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


class LoginLostTest(Base):
    def test_a_program_that_lost_the_attempt_gets_a_new_qr(self):
        b = self.backend()
        b.login_start()
        b._native.close()  # the program restarts: it knows no QR attempt
        time.sleep(0.3)
        self.assertEqual(b.login_poll("qr"), "expired")


class ReadyTest(Base):
    def test_a_program_started_for_a_qr_is_given_the_sign_in_before_listening(self):
        b = self.backend([{"event": "link", "up": True}])
        b.restore({"state": {"k": "v1"}})
        b._native.close()
        time.sleep(0.3)
        b.login_start()  # a new program, started without the sign-in
        self.listen(b, lambda g, l, e: l or e)
        ops = [r["op"] for r in self.requests() if r.get("op") in ("restore", "login_start", "listen")]
        self.assertEqual(ops, ["restore", "login_start", "restore", "listen"])


class ProtocolTest(Base):
    def test_a_program_of_another_protocol_is_not_used(self):
        os.environ["FAKE_PROTOCOL"] = "1"
        b = self.backend()
        with self.assertRaises(BackendError) as c:
            b.restore({"state": {"k": "v1"}})
        self.assertIn("版本不对", c.exception.message)

    def test_a_live_message_is_quotable_while_the_core_handles_it(self):
        b = self.backend([msg("r1", "1@chatroom", "wxid_bob", "@我 快答", at_me=True)])
        b.restore({"state": {"k": "v1"}})
        seen_inside = []

        def core(m):  # what /agent or 自动代答 would do at once: answer it
            seen_inside.append((b.store.message(m.id) is not None, [x.id for x in b.history(m.chat_id)]))

        stop = threading.Event()
        t = threading.Thread(target=lambda: b.listen(core, lambda s, r: None, stop), daemon=True)
        t.start()
        deadline = time.time() + 5
        while time.time() < deadline and not seen_inside:
            time.sleep(0.02)
        stop.set()
        t.join(5)
        self.assertEqual(seen_inside, [(True, [])], "kept for the quote, not yet in history")
        self.assertEqual([x.id for x in b.history("1@chatroom")], ["r1"])


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
        self.assertEqual(r["mentions"], [{"id": "wxid_bob", "name": "群里的鲍勃"}], "the group nickname, not a remark")
        q = r["quote"]
        self.assertEqual((q["server_id"], q["sender"], q["outgoing"], q["content"], q["display_name"]),
                         ("q1", "wxid_bob", False, "@我 问题", "群里的鲍勃"))

    def test_answering_my_own_agent_request_ats_nobody(self):
        self.b.send("1@chatroom", "结果", reply_to="q2")
        r = self.requests("send")[-1]
        self.assertEqual(r["quote"]["outgoing"], True)
        self.assertNotIn("mentions", r)

    def test_an_interop_group_answer_quotes_without_an_at(self):
        self.b.store.add_message(msg("q7", "77@im.chatroom", "wxid_bob", "@我 问", at_me=True))
        self.b.send("77@im.chatroom", "答复", reply_to="q7")
        r = self.requests("send")[-1]
        self.assertEqual(r["quote"]["server_id"], "q7")
        self.assertNotIn("mentions", r, "WeChat takes a real @ inside a quote only in …@chatroom groups")

    def test_an_own_send_is_kept_with_its_send_time_and_quotable(self):
        kept = []
        self.b.on_history = kept.extend
        mid = self.b.send("1@chatroom", "记下")
        quoting = self.b.send("1@chatroom", "答复", reply_to="q1")
        deadline = time.time() + 3
        while time.time() < deadline and len(kept) < 2:
            time.sleep(0.05)
        ev = self.b.store.message(mid)[0]
        self.assertEqual((ev["at"], ev["quotable"], ev["content"]), (1700000000, True, "记下"))
        self.assertFalse(self.b.store.message(quoting)[0]["quotable"],
                         "a quote reply went out as WeChat's quote message: never quoted back as plain text")

    def test_keeping_an_own_send_failing_still_reports_it_sent(self):
        def boom(*a, **k):
            raise RuntimeError("disk full")

        self.b._keep_own_send = boom
        self.assertTrue(self.b.send("1@chatroom", "照发"), "it went out: never an error, never sent again")

    def test_a_reply_in_another_chat_quotes_nothing(self):
        self.b.send("2@chatroom", "别处", reply_to="q1")
        r = self.requests("send")[-1]
        self.assertNotIn("quote", r)
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


class AckTest(Base):
    def test_the_ack_line_quotes_the_request_without_an_at(self):
        b = self.backend()
        b.ack_text = "🤖 贾维斯处理中..."
        b.restore({"state": {"k": "v1"}})
        b.store.add_message(msg("a1", "1@chatroom", "wxid_bob", "@我 帮我看看", at_me=True))
        self.assertTrue(b.react("1@chatroom", "a1", "🤖 贾维斯处理中...").startswith("srv_"), "its message id")
        r = self.requests("send")[-1]
        self.assertEqual((r["chat"], r["text"], r["quote"]["server_id"]), ("1@chatroom", "🤖 贾维斯处理中...", "a1"))
        self.assertNotIn("mentions", r, "the answer @s them, the ack does not")
        self.assertEqual(r["quote"]["display_name"], "群里的鲍勃", "the quote shows the group nickname")
        n = len(self.requests("send"))
        self.assertIsNone(b.react("1@chatroom", "a1", "🤖 贾维斯处理中..."), "once per request")
        b.react("1@chatroom", "a1", "DONE")  # a Feishu code: not a WeChat message
        b.unreact("1@chatroom", "a1", "🤖 贾维斯处理中...")  # nothing to take back
        self.assertEqual(len(self.requests("send")), n)
        self.assertEqual(self.requests("recall"), [])

    def test_no_ack_line_set_sends_nothing(self):
        b = self.backend()
        b.restore({"state": {"k": "v1"}})
        b.react("1@chatroom", "a1", "THINKING")
        self.assertEqual(self.requests("send"), [])


class HistoryToCoreTest(Base):
    def test_caught_up_and_own_sends_reach_the_core_as_history(self):
        b = self.backend([msg("c9", "wxid_new", "wxid_new", "断线时发的", origin="catchup"), {"sleep": 0.3}])
        kept = []
        b.on_history = kept.extend
        b.restore({"state": {"k": "v1"}})
        got, _, _ = self.listen(b, lambda g, l, e: kept)
        self.assertEqual(got, [], "never live")
        self.assertEqual([m.id for m in kept], ["c9"])
        b.store.add_message(msg("q5", "1@chatroom", "wxid_bob", "@我 问", at_me=True))
        mid = b.send("1@chatroom", "答", reply_to="q5")
        deadline = time.time() + 3
        while time.time() < deadline and len(kept) < 2:
            time.sleep(0.05)
        own = kept[-1]
        self.assertEqual((own.id, own.mine, own.text, own.parent_id), (mid, True, "@群里的鲍勃\u2005答", "q5"))

    def test_kinds_learnt_from_wechat(self):
        os.environ["FAKE_BUSINESS"] = "cmb4008205555"
        os.environ["FAKE_NO_KIND"] = "wxid_carl,wxid_dan"  # the fake reads its environment when it starts
        b = self.backend()
        b.restore({"state": {"k": "v1"}})
        b._lookup_names(["cmb4008205555", "wxid_bob"])
        self.assertEqual(wb.kind_of("cmb4008205555"), "bot")
        import openmate_wechat

        self.assertFalse(openmate_wechat.archive_chat("cmb4008205555", None, {}), "a verified service account")
        self.assertTrue(openmate_wechat.archive_chat("wxid_bob", None, {}))
        self.assertTrue(openmate_wechat.archive_chat("wxid_unknown", None, {}), "a person's id, not known as anything else")
        wb.PERSONS.add("wxid_dan")
        b._lookup_names(["wxid_carl", "wxid_dan"])
        self.assertNotIn("wxid_carl", wb.PERSONS, "no flags: not taken for a person")
        self.assertIn("wxid_dan", wb.PERSONS, "no flags this time: what was learnt stays")
        self.assertTrue(wb.is_group("123@im.chatroom") and wb.is_group("9@qy_g"))
        with self.assertRaises(BackendError):
            b.create_chat("9@qy_g")


class KindsLaterTest(Base):
    def test_people_kinds_asks_wechat_about_undescribed_ids_and_tells_the_core_later(self):
        os.environ["FAKE_BUSINESS"] = "qqtech"
        b = self.backend()
        told = []
        b.on_people_kinds = told.append
        b.restore({"state": {"k": "v1"}})
        oim = "25984984210915466@openim"
        self.assertEqual(b.people_kinds(["qqtech", "wxid_bob", oim]), {"qqtech": "bot", "wxid_bob": "user", oim: "user"},
                         "腾讯科技 has a person-like id: WeChat says it is an official account")
        asked = [i for r in self.requests("contacts") for i in r.get("ids", [])]
        self.assertNotIn(oim, asked, "a WeCom account is a person's: nothing to ask")
        self.assertIn({"qqtech": "bot"}, told)
        n = len(self.requests("contacts"))
        b.people_kinds(["qqtech", "wxid_bob"])
        self.assertEqual(len(self.requests("contacts")), n, "described once: not asked again")
        b.close()
        b2 = self.backend()
        told2 = []
        b2.on_people_kinds = told2.append
        b2.restore({"state": {"k": "v1"}})
        self.assertIn({"qqtech": "bot"}, told2, "what was learnt before is told again at start (the core may say user)")

    def test_a_person_wechat_will_not_name_stays_nameless_and_is_not_asked_each_time(self):
        os.environ["FAKE_UNKNOWN"] = "wxid_ghost"
        b = self.backend()
        b.restore({"state": {"k": "v1"}})
        b.store.add_message(msg("g1", "wxid_ghost", "wxid_ghost", "你好"))
        self.assertEqual(b.chat_info("wxid_ghost").name, "", "never the id as a name: the core asks again later")
        n = len(self.requests("contacts"))
        b.chat_info("wxid_ghost")
        self.assertEqual(len(self.requests("contacts")), n, "not again before NAME_RETRY")


class ReadTest(Base):
    def test_a_chat_read_on_the_phone_after_the_last_message_is_done(self):
        now = int(time.time())
        b = self.backend([msg("r1", "wxid_bob", "wxid_bob", "在吗", at=now - 60),
                          {"event": "read", "chat": "wxid_bob", "at": now - 30}])
        hints = []
        b.on_chats_done = hints.append
        b.restore({"state": {"k": "v1"}})
        self.listen(b, lambda g, l, e: hints)
        self.assertEqual(hints, [{"wxid_bob": float(now - 30)}])
        self.assertEqual(b.chat_done_states(["wxid_bob", "wxid_ann"]), {"wxid_bob": float(now - 30)},
                         "no read seen: left out, never None")
        b.store.add_message(msg("r2", "wxid_bob", "wxid_bob", "还有一件事", at=now))
        self.assertEqual(b.chat_done_states(["wxid_bob"]), {"wxid_bob": None}, "a new message brings it back")
        b._on_read("wxid_bob", now - 100)
        self.assertEqual(b.store.get("read:wxid_bob"), str(now - 30), "an older read never moves it back")
        b._on_read("wxid_bob", now)
        self.assertEqual(b.chat_done_states(["wxid_bob"]), {"wxid_bob": None},
                         "read in the very second of the last message: can't tell which came first, still open")

    def test_old_direct_chats_get_their_kind_in_the_background(self):
        os.environ["FAKE_BUSINESS"] = "cmb4008205555"
        os.environ["FAKE_GROUPS"] = "[]"
        b = self.backend()
        b.restore({"state": {"k": "v1"}})
        for cid in ("wxid_old1", "cmb4008205555", "wxid_old2"):
            b.store.add_message(msg("m-" + cid, cid, cid, "旧消息"))
        b._groups_work(True)
        asked = [i for r in self.requests("contacts") for i in r.get("ids", [])]
        self.assertEqual(sorted(asked), ["cmb4008205555", "wxid_old1", "wxid_old2"])
        self.assertTrue(all(len(r.get("ids", [])) <= 2 for r in self.requests("contacts")), "two per request")
        self.assertEqual(wb.kind_of("cmb4008205555"), "bot")
        self.assertIn("wxid_old1", wb.PERSONS)
        b._groups_work(True)
        self.assertEqual(len([i for r in self.requests("contacts") for i in r.get("ids", [])]), 3, "asked once")


class GroupsTest(Base):
    def setUp(self):
        super().setUp()
        self._spacing, wb.GROUPS_SPACING, wb.GROUP_NAME_PAUSE = wb.GROUPS_SPACING, 0.0, 0.0
        self.addCleanup(setattr, wb, "GROUPS_SPACING", self._spacing)

    def wait(self, cond, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline and not cond():
            time.sleep(0.05)
        return cond()

    def test_every_contacts_group_is_listed_and_named(self):
        b = self.backend([{"event": "link", "up": True}, {"sleep": 1.5}])
        b.restore({"state": {"k": "v1"}})
        self.listen(b, lambda g, l, e: l)
        self.assertTrue(self.wait(lambda: {c.name for c in b.group_candidates(50)} == {"群g1", "群g2"}))
        self.assertTrue(all(c.kind == "group" for c in b.group_candidates(50)))

    def test_a_contact_change_reads_the_groups_again(self):
        os.environ["FAKE_GROUPS"] = '["g1@chatroom"]'
        b = self.backend([{"event": "link", "up": True}, {"sleep": 0.6}, {"event": "contacts"}, {"sleep": 1.5}])
        b.restore({"state": {"k": "v1"}})
        stop = threading.Event()
        t = threading.Thread(target=lambda: b.listen(lambda m: None, lambda s, r: None, stop), daemon=True)
        t.start()
        self.wait(lambda: len(self.requests("groups")) >= 1)
        os.environ["FAKE_GROUPS"] = '["g1@chatroom", "g9@chatroom"]'  # read by the next program call only via env:
        self.wait(lambda: len(self.requests("groups")) >= 2, 4)
        stop.set()
        t.join(5)
        self.assertGreaterEqual(len(self.requests("groups")), 2, "the contact change made it read the list again")

    def test_a_group_first_seen_in_a_message_is_listed_and_named(self):
        os.environ["FAKE_GROUPS"] = "[]"
        b = self.backend([{"event": "link", "up": True}, msg("n1", "new7@chatroom", "wxid_bob", "大家好")])
        b.restore({"state": {"k": "v1"}})
        self.listen(b, lambda g, l, e: g)
        self.assertTrue(self.wait(lambda: "群new7" in {c.name for c in b.group_candidates(50)}))

    def test_another_account_reads_its_groups_afresh(self):
        b = self.backend([{"event": "link", "up": True}, {"sleep": 1.0}])
        b.restore({"state": {"k": "v1"}})
        self.listen(b, lambda g, l, e: l)
        self.assertTrue(self.wait(lambda: b.store.get("groups_at")))
        b.restore({"state": {"k": "v2", "who": "wxid_other"}})
        self.assertEqual(b.store.get("groups_at"), "", "the old account's scan does not count for the new one")
        self.assertEqual(b.group_candidates(50), [])

    def test_an_incomplete_scan_is_not_counted(self):
        os.environ["FAKE_GROUPS_COMPLETE"] = "0"
        b = self.backend([{"event": "link", "up": True}, {"sleep": 1.0}])
        b.restore({"state": {"k": "v1"}})
        self.listen(b, lambda g, l, e: l)
        self.assertTrue(self.wait(lambda: len(b.group_candidates(50)) == 2))
        self.assertEqual(b.store.get("groups_at"), "", "read again at the next chance")

    def test_unnamed_groups_have_no_stored_stand_in(self):
        b = self.backend()
        b.restore({"state": {"k": "v1"}})
        b.store.add_groups(["q1@chatroom"])
        self.assertEqual([(c.id, c.name) for c in b.group_candidates(10)], [("q1@chatroom", "")])

    def test_a_quote_travels_with_the_message(self):
        ev = dict(msg("qq", "1@chatroom", "wxid_bob", "@我 请确认是谁负责这个？", at_me=True), parent="77",
                  quoted_sender="小李", quoted_text="需求负责人是郑元")
        b = self.backend([ev])
        b.restore({"state": {"k": "v1"}})
        got, _, _ = self.listen(b, lambda g, l, e: g)
        self.assertEqual(got[0].text, "@我 请确认是谁负责这个？", "the message as written (commands, marks)")
        self.assertEqual(got[0].parent_id, "77")

    def test_no_lookup_for_an_empty_id(self):
        b = self.backend()
        b.restore({"state": {"k": "v1"}})
        self.assertEqual(b.user_name(""), "")
        self.assertEqual(b._lookup_names(["", ""]), {})
        self.assertEqual(self.requests("contacts"), [])


class NamesTest(Base):
    def test_names_and_kinds(self):
        b = self.backend()
        b.restore({"state": {"k": "v1"}})
        self.assertEqual(b.user_name("wxid_bob", "1@chatroom"), "群里的鲍勃")
        self.assertEqual(b.user_name("wxid_bob"), "备注b")
        self.assertEqual(b.chat_info("wxid_bob").name, "备注b")
        self.assertEqual(b.chat_info("1@chatroom").members, 3, "the full member count, not the roster page")
        self.assertEqual(b.people_kinds(["wxid_bob", "gh_abc", "abc@im.bot", "filehelper", "x@openim", "1@chatroom",
                                         "masssendapp", "y@kefu.openim"]),
                         {"wxid_bob": "user", "gh_abc": "bot", "abc@im.bot": "bot", "filehelper": "bot",
                          "x@openim": "user", "masssendapp": "bot", "y@kefu.openim": "bot"})

    def test_the_mark_view_drops_real_mentions_only(self):
        import openmate_wechat

        v = openmate_wechat.mark_view
        self.assertEqual(v("@Bob Smith\u2005> 🤖 AI 代答 · m · h\n周五"), "> 🤖 AI 代答 · m · h\n周五")
        self.assertEqual(v("@鲍勃\u2005@Ann\u2005[智能代答] x"), "[智能代答] x")
        self.assertEqual(v("@" + "长" * 300 + "\u2005> 🤖 AI 代答"), "> 🤖 AI 代答")
        self.assertEqual(v("@鲍勃 你好"), "@鲍勃 你好", "a typed @ is not a real one")
        self.assertEqual(v("@运营 例子 @Bob Smith\u2005/agent x"), "@运营 例子 @Bob Smith\u2005/agent x",
                         "never past a typed @")
        self.assertEqual(v("@Alice@Acme Chen\u2005/agent 整理"), "@Alice@Acme Chen\u2005/agent 整理",
                         "a name with @ cannot be told from a typed @: kept (a missed /agent is safe)")
        self.assertEqual(v("@运营 请群里负责 API@张三\u2005/agent 写周报"), "@运营 请群里负责 API@张三\u2005/agent 写周报")
        self.assertEqual(v("@运营 举例：@张三\u2005/agent 写周报"), "@运营 举例：@张三\u2005/agent 写周报")

    def test_proxy_variables_never_reach_the_program(self):
        env = clean_env({"PATH": "/bin", "https_proxy": "http://127.0.0.1:7897", "ALL_PROXY": "socks5://x"})
        self.assertEqual(env, {"PATH": "/bin"})


if __name__ == "__main__":
    unittest.main()
