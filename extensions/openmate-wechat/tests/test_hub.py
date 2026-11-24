"""The WeChat backend through an AgentWechat hub (hub.py) against a fake hub: signing in there, what reaches the core
(live only; our own echo never; the user's desktop typing as their own), sends (key, quote, @, outcomes), reads made
in the desktop app, a gap the event log no longer covers, media, groups left, and the account signed out there."""

import _runtmp  # noqa: F401  (first: every temporary directory of this run in one, removed at its end)
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
for p in (os.path.join(ROOT, "core"), EXT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from fake_hub import FakeHub  # noqa: E402
from openmate_core.backend import BackendError, SendRejected  # noqa: E402
from openmate_wechat import backend as wb  # noqa: E402
from openmate_wechat import hub as wh  # noqa: E402

GROUP = "123@chatroom"


def wait_until(cond, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


class Base(unittest.TestCase):
    def setUp(self):
        wb.BUSINESS.clear()
        wb.PERSONS.clear()
        self.dir = tempfile.mkdtemp(prefix="matewechat-hub-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.hub = FakeHub()
        self.addCleanup(self.hub.close)
        self.history, self.done, self.gone = [], [], []
        self._saved = (wb.GROUPS_SPACING, wb.GROUP_NAME_PAUSE)
        wb.GROUPS_SPACING, wb.GROUP_NAME_PAUSE = 0.0, 0.0
        self.addCleanup(self._restore)

    def _restore(self):
        wb.GROUPS_SPACING, wb.GROUP_NAME_PAUSE = self._saved

    def backend(self, token=None, url=None, data="data"):
        b = wb.WeChatBackend(None, os.path.join(self.dir, data), hub_url=url or self.hub.url)
        b.secrets = {"hub_token": self.hub.token if token is None else token}
        b.on_history = self.history.extend
        b.on_chats_done = self.done.append
        b.on_chat_gone = self.gone.append
        self.addCleanup(b.close)
        return b

    def signed_in(self):
        b = self.backend()
        b.login_direct()
        return b

    def listen(self, b):
        got, links = [], []
        stop = threading.Event()
        err = []

        def run():
            try:
                b.listen(got.append, lambda s, r: links.append((s, r)), stop)
            except Exception as e:  # noqa: BLE001
                err.append(e)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.addCleanup(t.join, 5)
        self.addCleanup(stop.set)
        self.assertTrue(wait_until(lambda: ("up", "") in links), (links, err))
        return got, links, stop, err


class SignInTest(Base):
    def test_signed_in_there_is_signed_in_here(self):
        b = self.backend()
        done = b.login_direct()
        self.assertEqual((done.account.id, done.account.name), ("wxid_me", "我自己"))
        self.assertEqual(done.credentials, {"v": 1, "hub": True, "account": "wxid_me"})
        # nothing to scan, nothing of WeChat's sign-in kept here
        self.assertFalse(any("login" in r[1] for r in self.hub.requests))

    def test_not_signed_in_there_says_where_to(self):
        self.hub.authenticated = False
        with self.assertRaises(BackendError) as e:
            self.backend().login_direct()
        self.assertIn("AgentWechat 桌面端扫码", e.exception.message)
        self.assertNotEqual(e.exception.kind, "expired")

    def test_restore_waits_for_the_hub_instead_of_expiring(self):
        self.hub.authenticated = False
        b = self.backend()
        with self.assertRaises(BackendError) as e:
            b.restore({"v": 1, "state": {"old": "qr sign-in"}})
        self.assertNotIsInstance(e.exception, wb.CredentialsExpired)
        self.hub.authenticated = True
        self.assertEqual(b.restore({"v": 1, "state": {"old": "qr sign-in"}}).id, "wxid_me")

    def test_token_missing_or_refused_is_a_setting(self):
        with self.assertRaises(BackendError) as e:
            self.backend(token="").login_direct()
        self.assertEqual(e.exception.kind, "config")
        with self.assertRaises(BackendError) as e:
            self.backend(token="wrong-token-0123456789").login_direct()
        self.assertEqual(e.exception.kind, "config")
        self.assertIn("不认这个访问密钥", e.exception.message)
        with self.assertRaises(BackendError) as e:
            self.backend(token="bad\ntoken-0123456789").login_direct()  # never put into a header (nor a log)
        self.assertEqual(e.exception.kind, "config")
        self.assertNotIn("token-0123456789", e.exception.message)

    def test_plain_http_only_to_this_computer(self):
        for url in ("http://10.0.0.5:6174", "http://hub.example:6174", "https://u:p@hub.example",
                    "http://127.0.0.1:6174/?x=1"):
            with self.assertRaises(BackendError) as e:
                self.backend(url=url).login_direct()
            self.assertEqual(e.exception.kind, "config", url)
            self.assertNotIn("u:p", e.exception.message)
        self.assertEqual(self.hub.requests, [])  # the token went nowhere

    def test_another_account_there_is_not_taken_by_itself(self):
        b = self.signed_in()
        self.hub.me = "wxid_someone_else"
        b2 = self.backend()  # a restart: the saved sign-in is restored
        with self.assertRaises(BackendError) as e:
            b2.restore({"v": 1, "hub": True, "account": "wxid_me"})
        self.assertIn("另一个微信号", e.exception.message)
        self.assertEqual(b2.store.get("account"), "wxid_me")  # nothing of the account here wiped
        with self.assertRaises(SendRejected):
            b2.send("wxid_friend", "不能以另一个号发出去")
        self.assertEqual([r for r in self.hub.sent("/sends") if r[0] == "POST"], [])
        n = len(self.hub.requests)
        self.assertEqual(b2.user_name("wxid_friend"), "")  # nothing read as the other account either
        self.assertFalse(any("/contacts/" in r[1] for r in self.hub.requests[n:]))
        # the local store rebuilt: the account sealed in the credentials still counts, a QR sign-in's too
        b3 = self.backend(data="fresh")
        with self.assertRaises(BackendError):
            b3.restore({"v": 1, "hub": True, "account": "wxid_me"})
        self.assertEqual(b3.store.get("account"), "")
        b4 = self.backend(data="fresh2")
        with self.assertRaises(BackendError):
            b4.restore({"v": 1, "state": {"own_username": "wxid_me"}})
        with self.assertRaises(BackendError) as e:
            b4.login_direct()  # the saved sign-in names the account served here: never another one
        self.assertIn("另一个微信号", e.exception.message)
        with self.assertRaises(BackendError):
            b.login_direct()  # nor after signing out here: the extension serves one account
        self.assertEqual(b.store.get("account"), "wxid_me")

    def test_another_account_signed_in_there_meanwhile_is_neither_read_nor_sent_as(self):
        b = self.signed_in()
        got, links, stop, err = self.listen(b)
        self.hub.authenticated = False
        self.hub.push("account.updated", {"accountId": self.hub.account, "authentication": "logged_out"})
        self.assertTrue(wait_until(lambda: any(s == "down" for s, r in links)))
        self.hub.me = "wxid_other_person"  # someone else's WeChat signed in to the hub
        self.hub.authenticated = True
        self.assertTrue(wait_until(lambda: any("另一个微信号" in r for s, r in links), timeout=8), links)
        self.hub.message("wxid_x", "/agent 别人的号")
        with self.assertRaises(SendRejected):
            b.send("wxid_friend", "不能用别人的号发")
        self.assertEqual([r for r in self.hub.sent("/sends") if r[0] == "POST"], [])
        time.sleep(0.5)
        self.assertEqual(got, [])
        self.hub.me = "wxid_me"  # back: it goes on
        n = len(links)
        self.assertTrue(wait_until(lambda: ("up", "") in links[n:], timeout=10), links)
        self.hub.message("wxid_friend", "回来了")
        self.assertTrue(wait_until(lambda: any(g.text == "回来了" for g in got), timeout=8))

    def test_the_cores_saved_sign_in_counts_before_any_restore(self):
        self.hub.me = "wxid_someone_else"
        b = self.backend(data="rebuilt")  # the local store rebuilt; the core still holds the account's sign-in
        b.saved_credentials = {"v": 1, "hub": True, "account": "wxid_me"}
        with self.assertRaises(BackendError) as e:
            b.login_direct()
        self.assertIn("另一个微信号", e.exception.message)
        self.assertEqual(b.store.get("account"), "")

    def test_a_restore_checks_the_cores_store_account_too(self):
        self.hub.me = "wxid_someone_else"
        b = self.backend(data="mixed")  # the core's store is the account's; the rest names the hub's other one
        b.core_account = "wxid_me"
        b.store.put("account", "wxid_someone_else")
        with self.assertRaises(BackendError) as e:
            b.restore({"v": 1, "hub": True, "account": "wxid_someone_else"})
        self.assertIn("另一个微信号", e.exception.message)
        with self.assertRaises(SendRejected):
            b.send("wxid_friend", "不该发出")
        self.assertEqual([r for r in self.hub.sent("/sends") if r[0] == "POST"], [])

    def test_a_store_of_another_account_than_the_cores_is_not_read(self):
        b = self.backend(data="cut")
        b.store.put("account", "wxid_other")
        b.store.add_message({"id": "1", "chat": "wxid_friend", "sender": "wxid_friend", "text": "别人的", "at": 1})
        b.core_account = "wxid_me"
        self.assertEqual(b.history("wxid_friend"), [])
        self.assertEqual(b.recent_chats(10), [])
        self.assertEqual(b.search("wxid"), [])
        b.store.add_groups(["9@chatroom"])
        b.store.name_chat("wxid_friend", "备注名", 0)
        self.assertEqual(b.group_candidates(10), [])
        self.assertEqual(b.chat_info("wxid_friend").name, "")
        self.assertEqual(b.user_name("wxid_friend"), "")
        self.assertEqual(b.chat_done_states(["wxid_friend"]), {})
        with self.assertRaises(BackendError):
            b.fetch_media("1")
        with self.assertRaises(BackendError):
            b.media("wx:1", 1 << 20)
        with self.assertRaises(BackendError):
            b.people_kinds  # noqa: B018 (exists)
            b.ask_kinds(["wxid_friend"])
        self.assertEqual(b.people_kinds(["wxid_friend"]), {})
        b._link(restore=False)  # a sign-in's own requests still go (a QR scanned again settles it)
        b.core_account = ""  # the core's store rebuilt; its saved sign-in still names the account
        b.saved_credentials = {"v": 1, "hub": True, "account": "wxid_me"}
        self.assertEqual(b.history("wxid_friend"), [])
        self.assertEqual(b.chat_info("wxid_friend").name, "")
        b.saved_credentials = None
        b.core_account = "wxid_other"
        self.assertEqual([m.text for m in b.history("wxid_friend")], ["别人的"])

    def test_a_token_for_several_accounts_is_refused(self):
        self.hub.accounts_extra = [dict(self.hub.account_obj(), id="account-2")]
        with self.assertRaises(BackendError) as e:
            self.backend().login_direct()
        self.assertIn("--accounts", e.exception.message)

    def test_no_hub_no_direct_sign_in(self):
        b = wb.WeChatBackend(lambda wait=0: "/nonexistent", os.path.join(self.dir, "plain"))
        self.addCleanup(b.close)
        self.assertIsNone(b.login_direct())


class LiveTest(Base):
    def test_a_live_message_reaches_the_core_and_the_cursor_follows(self):
        b = self.signed_in()
        got, links, stop, _ = self.listen(b)
        m = self.hub.message("wxid_friend", "你好")
        self.assertTrue(wait_until(lambda: got))
        msg = got[0]
        self.assertEqual((msg.id, msg.chat_id, msg.sender_id, msg.text, msg.mine), (m["serverId"], "wxid_friend",
                                                                                    "wxid_friend", "你好", False))
        self.assertTrue(wait_until(lambda: b.store.get("hub_cursor") == "account-1:%d" % self.hub.cursor()))
        self.assertEqual(b.store.get("cursor"), "")  # the program's own sync key is not touched
        # quoting it later needs the hub's own id: kept with it
        self.assertEqual(b.store.message(m["serverId"])[0]["hub_id"], m["id"])

    def test_what_came_while_not_listening_is_history_however_fresh(self):
        b = self.signed_in()
        self.hub.push("noise", {})
        b.store.put("hub_cursor", "account-1:%d" % self.hub.cursor())
        self.hub.message("wxid_friend", "/agent 刚才离线时发的", direction="outgoing", origin="live")
        self.hub.message(GROUP, "离线时 @ 你", sender="wxid_a", mentions_self=True)
        got, links, stop, _ = self.listen(b)
        self.assertTrue(wait_until(lambda: len(self.history) == 2))
        self.hub.message("wxid_friend", "连上之后的")
        self.assertTrue(wait_until(lambda: got))
        self.assertEqual([g.text for g in got], ["连上之后的"])

    def test_what_comes_while_the_stream_is_down_is_history(self):
        b = self.signed_in()
        got, *_ = self.listen(b)
        self.hub.stream_delay = 1.5
        n = self.hub.streams
        self.hub.drop += 1  # down
        time.sleep(0.3)
        self.hub.message("wxid_friend", "/agent 断开时发的")
        self.assertTrue(wait_until(lambda: any(h.text == "/agent 断开时发的" for h in self.history), timeout=8))
        self.assertGreater(self.hub.streams, n)
        self.hub.message("wxid_friend", "连上之后")
        self.assertTrue(wait_until(lambda: got))
        self.assertEqual([g.text for g in got], ["连上之后"])

    def test_a_hiccup_keeps_a_live_message_live(self):
        b = self.signed_in()
        got, *_ = self.listen(b)
        self.hub.fail_messages = 1  # its details fail once: the stream reconnects
        self.hub.message(GROUP, "@ 你一下", sender="wxid_a", mentions_self=True)
        self.assertTrue(wait_until(lambda: got, timeout=8))
        self.assertTrue(got[0].at_me)

    def test_first_connect_brings_the_recent_chats_as_history(self):
        self.hub.message("wxid_friend", "登录前的一句", commit=False)
        b = self.signed_in()
        got, *_ = self.listen(b)
        self.assertTrue(wait_until(lambda: any(h.text == "登录前的一句" for h in self.history)))
        self.assertEqual(got, [])

    def test_a_chat_too_big_to_page_does_not_stop_the_catch_up(self):
        b = self.signed_in()
        self.hub.message("wxid_big", "大会话", commit=False)
        self.hub.message("wxid_friend", "普通会话", commit=False)
        self.hub.big_chats.add("wxid_big")
        for _ in range(3):
            self.hub.push("noise", {})
        self.hub.oldest = 3
        b.store.put("hub_cursor", "account-1:1")
        got, *_ = self.listen(b)
        self.assertTrue(wait_until(lambda: any(h.text == "普通会话" for h in self.history)))
        self.hub.message("wxid_friend", "之后的")
        self.assertTrue(wait_until(lambda: got))

    def test_a_wecom_groups_member_from_the_envelope(self):
        b = self.signed_in()
        got, *_ = self.listen(b)
        wecom = "789@im.chatroom"
        self.hub.message(wecom, "wxid_a:\n@我 看下", sender=wecom, mentions_self=True)
        self.assertTrue(wait_until(lambda: got))
        self.assertEqual((got[0].sender_id, got[0].text), ("wxid_a", "@我 看下"))

    def test_a_cursor_of_another_hub_or_ahead_of_the_log_starts_now(self):
        b = self.signed_in()
        b.store.put("hub_cursor", "account-9:5")
        got, *_ = self.listen(b)
        self.hub.message("wxid_friend", "新的")
        self.assertTrue(wait_until(lambda: got))
        b2 = self.backend(data="data2")
        b2.store.put("account", "wxid_me")
        b2.store.put("hub_cursor", "account-1:99999")  # a new hub for the same account: its log is shorter
        b2.restore({"v": 1, "hub": True})
        got2, *_ = self.listen(b2)
        self.hub.message("wxid_friend", "再来一条")
        self.assertTrue(wait_until(lambda: got2))

    def test_first_listen_starts_now(self):
        self.hub.message("wxid_friend", "登录前的", commit=True)
        b = self.signed_in()
        got, links, stop, _ = self.listen(b)
        self.hub.message("wxid_friend", "之后的")
        self.assertTrue(wait_until(lambda: got))
        self.assertEqual([g.text for g in got], ["之后的"])

    def test_group_at_me_from_the_hub_mentions(self):
        b = self.signed_in()
        got, *_ = self.listen(b)
        self.hub.message(GROUP, "看下这个", sender="wxid_a", mentions_self=True)
        self.hub.message(GROUP, "随便聊", sender="wxid_b")
        self.assertTrue(wait_until(lambda: len(got) == 2))
        self.assertEqual([(g.sender_id, g.at_me) for g in got], [("wxid_a", True), ("wxid_b", False)])

    def test_typed_in_the_desktop_app_is_the_users_own(self):
        b = self.signed_in()
        got, *_ = self.listen(b)
        self.hub.message("wxid_friend", "/agent 帮我查下", direction="outgoing", origin="local_send")
        self.assertTrue(wait_until(lambda: got))
        self.assertTrue(got[0].mine)
        self.assertEqual(got[0].text, "/agent 帮我查下")

    def test_typed_on_the_phone_is_the_users_own(self):
        b = self.signed_in()
        got, *_ = self.listen(b)
        self.hub.message("wxid_friend", "/agent 手机上", direction="outgoing", origin="live")
        self.assertTrue(wait_until(lambda: got))
        self.assertTrue(got[0].mine)

    def test_profile_import_is_not_acted_on(self):
        b = self.signed_in()
        got, *_ = self.listen(b)
        self.hub.message("wxid_friend", "旧资料", origin="profile_import")
        self.hub.message("wxid_friend", "新的")
        self.assertTrue(wait_until(lambda: got))
        time.sleep(0.2)
        self.assertEqual([g.text for g in got], ["新的"])

    def test_a_gap_the_log_no_longer_has_is_caught_up_as_history(self):
        b = self.signed_in()
        self.hub.message("wxid_friend", "漏掉的", commit=False)
        for _ in range(3):
            self.hub.push("noise", {})
        self.hub.oldest = 3  # events up to 2 are gone: a cursor at 1 is too old
        b.store.put("hub_cursor", "account-1:1")
        got, *_ = self.listen(b)
        self.assertTrue(wait_until(lambda: any(h.text == "漏掉的" for h in self.history)))
        self.assertEqual(got, [])
        self.hub.message("wxid_friend", "接着来的")
        self.assertTrue(wait_until(lambda: got))
        self.assertEqual(got[0].text, "接着来的")

    def test_signed_out_there_is_down_with_the_reason_then_back(self):
        b = self.signed_in()
        got, links, stop, err = self.listen(b)
        self.hub.authenticated = False
        self.hub.push("account.updated", {"accountId": self.hub.account, "authentication": "logged_out"})
        self.assertTrue(wait_until(lambda: any(s == "down" and "桌面端扫码" in r for s, r in links)), links)
        self.hub.authenticated = True
        n = len(links)
        self.assertTrue(wait_until(lambda: ("up", "") in links[n:], timeout=8), links)
        # past the sign-out event: the next message comes (the event is not read again and again)
        self.hub.message("wxid_friend", "重新登录后")
        self.assertTrue(wait_until(lambda: any(g.text == "重新登录后" for g in got), timeout=8))
        self.assertEqual(err, [])


class SendTest(Base):
    def test_send_quote_and_mention_and_our_echo_is_not_acted_on(self):
        b = self.signed_in()
        got, *_ = self.listen(b)
        q = self.hub.message(GROUP, "谁知道？", sender="wxid_a", mentions_self=True)
        self.assertTrue(wait_until(lambda: got))
        self.hub.members[GROUP] = [{"id": "wxid_a", "username": "wxid_a", "roomDisplayName": "小A",
                                    "nickname": "A"}]
        self.hub.groups[GROUP] = {"id": GROUP, "roomId": GROUP, "name": "项目群", "isInGroup": True}
        self.hub.contacts["wxid_a"] = {"id": "wxid_a", "username": "wxid_a", "nickname": "A", "remark": None}
        mid = b.send(GROUP, "我知道", reply_to=q["serverId"])
        post = [r for r in self.hub.sent("/sends") if r[0] == "POST"][0]
        self.assertTrue(post[3].get("Idempotency-Key", "").startswith("om-"))
        self.assertEqual(post[2]["threadId"], GROUP)
        self.assertEqual(post[2]["mentions"], {"userIds": ["wxid_a"]})
        self.assertEqual(post[2]["quote"], {"messageId": q["id"], "serverId": q["serverId"]})
        sent = [m for m in self.hub.messages.values() if m["direction"] == "outgoing"][0]
        self.assertEqual(mid, sent["serverId"])
        time.sleep(0.5)  # the echo came and went
        self.assertEqual(len(got), 1)

    def test_an_echo_before_the_send_answered_is_still_ours(self):
        self.hub.echo_first = True
        b = self.signed_in()
        got, *_ = self.listen(b)
        b.send("wxid_friend", "/agent 不是命令，是回复")
        time.sleep(0.6)
        self.assertEqual(got, [])

    def test_our_message_synced_back_first_as_live_is_still_ours(self):
        self.hub.echo_origin = "live"
        b = self.signed_in()
        got, *_ = self.listen(b)
        b.send("wxid_friend", "/agent 这是回复，不是命令")
        time.sleep(0.6)
        self.assertEqual(got, [])

    def test_a_lost_answer_not_found_yet_is_unknown_never_not_sent(self):
        self.hub.send_outcome = "lost_answer"
        self.hub.lookup_missing = True
        b = self.signed_in()
        with self.assertRaises(BackendError) as e:
            b.send("wxid_friend", "回复一下")
        self.assertNotIsInstance(e.exception, SendRejected)  # the core never sends it again
        self.assertEqual(len([r for r in self.hub.sent("/sends") if r[0] == "POST"]), 1)

    def test_the_same_text_twice_both_ours(self):
        b = self.signed_in()
        got, *_ = self.listen(b)
        b.send("wxid_friend", "收到")
        b.send("wxid_friend", "收到")
        time.sleep(0.6)
        self.assertEqual(got, [])
        self.hub.message("wxid_friend", "收到", direction="outgoing", origin="local_send")  # now the user's
        self.assertTrue(wait_until(lambda: got))
        self.assertTrue(got[0].mine)

    def test_a_group_quote_never_shows_a_private_remark(self):
        b = self.signed_in()
        got, *_ = self.listen(b)
        q1 = self.hub.message(GROUP, "问题一", sender="wxid_a", mentions_self=True)
        q2 = self.hub.message(GROUP, "问题二", sender="wxid_b", mentions_self=True)
        self.assertTrue(wait_until(lambda: len(got) == 2))
        self.hub.contacts["wxid_a"] = {"id": "wxid_a", "username": "wxid_a", "nickname": "A", "remark": "老板"}
        self.hub.members[GROUP] = [{"id": "wxid_a", "username": "wxid_a", "roomDisplayName": "小A", "nickname": "A"},
                                   {"id": "wxid_b", "username": "wxid_b", "roomDisplayName": "小B", "nickname": "B"}]
        self.hub.contacts["wxid_b"] = {"id": "wxid_b", "username": "wxid_b", "nickname": "B", "remark": None}
        q3 = self.hub.message(GROUP, "问题三", sender="wxid_c", mentions_self=True)  # not a contact (a former one?)
        self.assertTrue(wait_until(lambda: len(got) == 3))
        b.send(GROUP, "答一", reply_to=q1["serverId"])
        b.send(GROUP, "答二", reply_to=q2["serverId"])
        b.send(GROUP, "答三", reply_to=q3["serverId"])
        posts = [r[2] for r in self.hub.sent("/sends") if r[0] == "POST"]
        self.assertNotIn("quote", posts[0])  # the user's remark for A would be shown to the group
        self.assertEqual(posts[0]["mentions"], {"userIds": ["wxid_a"]})
        self.assertEqual(posts[1]["quote"]["serverId"], q2["serverId"])  # B has none: quoted
        self.assertNotIn("quote", posts[2])  # not known to have none
        kept = b.store.message(b.history(GROUP)[-3].id)[0]
        self.assertEqual(kept.get("parent"), "")  # the local copy says what went out: no quote

    def test_a_direct_quote_never_shows_a_private_remark_either(self):
        b = self.signed_in()
        got, *_ = self.listen(b)
        q = self.hub.message("wxid_friend", "在吗")
        self.assertTrue(wait_until(lambda: got))
        self.hub.contacts["wxid_friend"] = {"id": "wxid_friend", "username": "wxid_friend", "nickname": "F",
                                            "remark": "张总"}
        b.send("wxid_friend", "在", reply_to=q["serverId"])
        post = [r[2] for r in self.hub.sent("/sends") if r[0] == "POST"][0]
        self.assertNotIn("quote", post)

    def test_after_signing_out_here_nothing_goes_out(self):
        b = self.signed_in()
        b.logout()
        with self.assertRaises(SendRejected):
            b.send("wxid_friend", "退出后不该发")
        self.assertEqual([r for r in self.hub.sent("/sends") if r[0] == "POST"], [])

    def test_a_send_with_no_known_outcome_is_told_by_its_own_record(self):
        """Its outcome was not known when it was made (the answer lost, the record not in yet); its echo is told by
        the hub's record of that send (its key), and the user's own lines in that chat stay theirs."""
        self.hub.send_outcome = "lost_answer"
        self.hub.lookup_missing_once = True
        b = self.signed_in()
        got, *_ = self.listen(b)
        with self.assertRaises(BackendError):
            b.send("wxid_friend", "/agent 扩展自己的话")  # unknown: never sent again
        self.assertTrue(wait_until(lambda: b.store.keys("hub_unsure:") == []), "its echo told: the record went")
        self.hub.message("wxid_friend", "/agent 我自己打的", direction="outgoing", origin="local_send")
        self.assertTrue(wait_until(lambda: got))
        time.sleep(0.3)
        self.assertEqual([g.text for g in got], ["/agent 我自己打的"])

    def test_a_record_that_cannot_say_which_message_is_ours(self):
        self.hub.send_outcome = "needs_review"  # it may have gone out; the hub keeps no ids for it
        b = self.signed_in()
        got, *_ = self.listen(b)
        with self.assertRaises(BackendError):
            b.send("wxid_friend", "/agent 扩展的")
        self.hub.message("wxid_friend", "/agent 扩展的", direction="outgoing", origin="live")  # WeChat syncs it back
        self.hub.message("wxid_other", "/agent 我的", direction="outgoing", origin="local_send")
        self.assertTrue(wait_until(lambda: got))
        time.sleep(0.3)
        self.assertEqual([g.text for g in got], ["/agent 我的"])

    def test_two_unsure_sends_one_failed_the_others_echo_is_ours(self):
        b = self.signed_in()
        link = b._native
        self.hub.sends["s1"] = {"id": "s1", "_key": "k1", "state": "failed", "serverId": None, "errorCode": "x"}
        self.hub.sends["s2"] = {"id": "s2", "_key": "k2", "state": "accepted", "serverId": "777",
                                "localMessageId": "msg:x"}
        link.remember_pending("wxid_friend", time.time(), "k1")
        link.remember_pending("wxid_friend", time.time(), "k2")
        self.assertTrue(link._own("wxid_friend", "msg:x", "777"))
        self.assertFalse(link._own("wxid_friend", "msg:y", "778"))  # the user's own, once both are settled

    def test_one_whose_echo_comes_after_a_restart_is_still_ours(self):
        self.hub.send_outcome = "silent"  # taken, not gone out yet
        saved = wh.SEND_WAIT
        wh.SEND_WAIT = 1.0
        self.addCleanup(setattr, wh, "SEND_WAIT", saved)
        b = self.signed_in()
        with self.assertRaises(BackendError):
            b.send("wxid_z", "/agent 第二条")
        self.assertEqual(len(b.store.keys("hub_unsure:")), 1)
        b.close()  # the extension restarts; meanwhile the hub sends it
        s = list(self.hub.sends.values())[-1]
        m = self.hub.message("wxid_z", "/agent 第二条", direction="outgoing", origin="local_send", commit=False)
        s.update(state="accepted", serverId=m["serverId"], localMessageId=m["id"])
        b2 = self.backend()
        b2.restore({"v": 1, "hub": True, "account": "wxid_me"})
        got2, *_ = self.listen(b2)
        self.hub.push("message.committed", {"messageId": m["id"], "threadId": "wxid_z", "origin": "local_send",
                                            "direction": "outgoing"})
        self.hub.message("wxid_other", "/agent 重启后", direction="outgoing", origin="local_send")
        self.assertTrue(wait_until(lambda: got2))
        time.sleep(0.3)
        self.assertEqual([g.text for g in got2], ["/agent 重启后"])
        self.assertEqual(b2.store.keys("hub_unsure:"), [])

    def test_a_lost_answer_is_looked_up_never_sent_again(self):
        self.hub.send_outcome = "lost_answer"
        b = self.signed_in()
        got, *_ = self.listen(b)
        mid = b.send("wxid_friend", "回复一下")
        posts = [r for r in self.hub.sent("/sends") if r[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertTrue(any("by-idempotency-key" in r[1] for r in self.hub.requests))
        self.assertTrue(mid)
        time.sleep(0.6)
        self.assertEqual(got, [])  # its echo is ours

    def test_the_same_words_typed_later_are_the_users(self):
        b = self.signed_in()
        got, *_ = self.listen(b)
        b.send("wxid_friend", "好的")
        time.sleep(0.6)
        self.hub.message("wxid_friend", "好的", direction="outgoing", origin="local_send")
        self.assertTrue(wait_until(lambda: got))
        self.assertTrue(got[0].mine)

    def test_wecom_groups_get_no_structured_at_and_old_messages_are_quoted(self):
        b = self.signed_in()
        got, *_ = self.listen(b)
        wecom = "456@im.chatroom"
        self.hub.message(wecom, "", sender="wxid_a", wire=3)
        old = self.hub.message("wxid_friend", "以前的消息", commit=False)
        b.store.add_message({"id": old["serverId"], "chat": "wxid_friend", "sender": "wxid_friend", "text": "以前的消息",
                             "at": 1, "quotable": True, "content": "以前的消息"})  # kept before the hub: no hub id
        self.assertTrue(wait_until(lambda: got))
        self.hub.contacts["wxid_friend"] = {"id": "wxid_friend", "username": "wxid_friend", "nickname": "F",
                                            "remark": None}
        b.send(wecom, "收到", reply_to=got[0].id)
        b.send("wxid_friend", "回你", reply_to=old["serverId"])
        posts = [r[2] for r in self.hub.sent("/sends") if r[0] == "POST"]
        self.assertNotIn("mentions", posts[0])
        self.assertEqual(posts[1]["quote"], {"messageId": old["id"], "serverId": old["serverId"]})

    def test_an_answer_to_someone_who_left_goes_without_the_at(self):
        b = self.signed_in()
        got, *_ = self.listen(b)
        q = self.hub.message(GROUP, "问一下", sender="wxid_gone", mentions_self=True)
        self.assertTrue(wait_until(lambda: got))
        self.hub.gone_members = {"wxid_gone"}
        mid = b.send(GROUP, "答复", reply_to=q["serverId"])
        self.assertTrue(mid)
        posts = [r[2] for r in self.hub.sent("/sends") if r[0] == "POST"]
        self.assertNotIn("mentions", posts[-1])
        time.sleep(0.5)
        self.assertEqual(len(got), 1)  # its echo is ours

    def test_outcomes(self):
        b = self.signed_in()
        self.hub.send_outcome = "failed"
        with self.assertRaises(SendRejected):
            b.send("wxid_friend", "a")
        self.hub.send_outcome = "refuse"
        with self.assertRaises(SendRejected):
            b.send("wxid_friend", "b")
        self.hub.send_outcome = "needs_review"
        with self.assertRaises(BackendError) as e:
            b.send("wxid_friend", "c")
        self.assertNotIsInstance(e.exception, SendRejected)
        self.assertIn("不会自动重发", e.exception.message)


class ReadsGroupsMediaTest(Base):
    def test_a_chat_read_on_the_phone_or_in_the_desktop_app_is_done(self):
        b = self.signed_in()
        got, *_ = self.listen(b)
        self.hub.message("wxid_friend", "在吗")
        self.hub.message("wxid_other", "在吗")
        self.assertTrue(wait_until(lambda: len(got) == 2))
        time.sleep(1.1)  # read a second later (the same second cannot tell which came first)
        self.hub.read("wxid_friend")  # on the phone
        self.hub.read("wxid_other", origin=None)  # in the desktop app (no origin)
        self.assertTrue(wait_until(lambda: len(self.done) == 2))
        states = b.chat_done_states(["wxid_friend", "wxid_other"])
        self.assertIsNotNone(states.get("wxid_friend"))
        self.assertIsNotNone(states.get("wxid_other"))

    def test_a_recalled_picture_is_not_served(self):
        b = self.signed_in()
        got, *_ = self.listen(b)
        m = self.hub.message("wxid_friend", "", wire=3)
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
        self.hub.blobs["asset_r"] = png
        self.hub.assets[m["id"]] = [{"id": "asset_r", "kind": "image", "variant": "original", "state": "pending",
                                     "contentUrl": None, "byteLength": None, "retryable": True}]
        self.assertTrue(wait_until(lambda: got))
        ev, st, _ = b.fetch_media(m["serverId"], wait=10)
        self.assertEqual(st["state"], "done")
        path = st["files"]["image"]
        self.hub.recall(m)
        self.assertTrue(wait_until(lambda: not os.path.exists(path)))
        with self.assertRaises(BackendError):
            b.media("wx:" + m["serverId"], 1 << 20)

    def test_a_recall_seen_only_in_a_catch_up_drops_the_media(self):
        b = self.signed_in()
        got, *_ = self.listen(b)
        m = self.hub.message("wxid_friend", "", wire=3)
        self.hub.blobs["asset_z"] = b"\x89PNG\r\n\x1a\n" + b"0" * 64
        self.hub.assets[m["id"]] = [{"id": "asset_z", "kind": "image", "variant": "original", "state": "pending",
                                     "contentUrl": None, "byteLength": None, "retryable": True}]
        self.assertTrue(wait_until(lambda: got))
        ev, st, _ = b.fetch_media(m["serverId"], wait=10)
        path = st["files"]["image"]
        m.update(type=10000, body="[Message recalled]", recall={"observedAt": "x"})  # recalled while away
        out = b._native.message_event(m, "catchup")
        self.assertTrue(out.get("update"))
        b._on_event(out, lambda msg: None)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(b.store.message(m["serverId"])[0]["type"], "SYSTEM")

    def test_a_message_the_core_did_not_take_comes_again(self):
        b = self.signed_in()
        stop = threading.Event()
        self.addCleanup(stop.set)
        calls = []

        def on_message(msg):
            calls.append(msg.text)
            if len(calls) == 1:
                raise RuntimeError("the core could not take it now")

        errors = []

        def run():
            while not stop.is_set():
                try:
                    b.listen(on_message, lambda s, r: None, stop)
                except BackendError as e:
                    errors.append(e.message)
                    time.sleep(0.2)

        threading.Thread(target=run, daemon=True).start()
        self.assertTrue(wait_until(lambda: self.hub.streams >= 1))
        time.sleep(0.3)
        self.hub.message("wxid_friend", "/agent 再来一次")
        self.assertTrue(wait_until(lambda: len(calls) >= 2, timeout=10), (calls, errors))
        self.assertEqual(calls[:2], ["/agent 再来一次", "/agent 再来一次"])

    def test_a_read_through_the_hub_covers_its_second(self):
        b = self.signed_in()
        got, *_ = self.listen(b)
        m = self.hub.message("wxid_friend", "在吗")
        self.assertTrue(wait_until(lambda: got))
        self.hub.push("thread.read-state", {"threadId": "wxid_friend", "readScope": "thread",
                                            "throughTime": wh.seconds(m["createdAt"])})
        self.assertTrue(wait_until(lambda: self.done))
        self.assertIsNotNone(b.chat_done_states(["wxid_friend"]).get("wxid_friend"))

    def test_a_recall_renews_the_kept_text(self):
        b = self.signed_in()
        got, *_ = self.listen(b)
        m = self.hub.message("wxid_friend", "说错了")
        self.assertTrue(wait_until(lambda: got))
        self.hub.recall(m)
        self.assertTrue(wait_until(lambda: any(h.text == "[撤回了一条消息]" for h in self.history)))
        self.assertEqual(b.store.message(m["serverId"])[0]["text"], "[撤回了一条消息]")
        self.assertEqual(len(got), 1)

    def test_a_group_left_on_the_phone_is_gone(self):
        b = self.signed_in()
        got, *_ = self.listen(b)
        self.hub.groups[GROUP] = {"id": GROUP, "roomId": GROUP, "name": "项目群", "isInGroup": False}
        self.hub.push("group.updated", {"groupId": GROUP, "origin": "live", "source": "remote_sync"})
        self.assertTrue(wait_until(lambda: self.gone == [GROUP]))

    def test_groups_and_one_left(self):
        b = self.signed_in()
        self.hub.groups = {GROUP: {"id": GROUP, "roomId": GROUP, "name": "项目群", "isInGroup": True},
                           "9@chatroom": {"id": "9@chatroom", "roomId": "9@chatroom", "name": "旧群",
                                          "isInGroup": False}}
        b._refresh_groups(full=True)
        # the list names them; the member check (each group's own isInGroup) finds the one left
        self.assertTrue(wait_until(lambda: self.gone == ["9@chatroom"]))
        self.assertTrue(wait_until(lambda: [c.name for c in b.group_candidates(10)] == ["项目群"]))
        self.assertTrue(any("group-sync" in r[1] for r in self.hub.requests))

    def test_a_live_lookup_names_without_saying_what_it_is(self):
        b = self.signed_in()
        self.hub.lookup["stranger_1"] = "陌生人"
        self.assertEqual(b.user_name("stranger_1"), "陌生人")
        self.assertNotIn("stranger_1", wb.PERSONS)
        self.assertNotIn("stranger_1", wb.BUSINESS)

    def test_kinds_from_the_hub(self):
        b = self.signed_in()
        self.hub.contacts["qqtech"] = {"id": "qqtech", "username": "qqtech", "nickname": "腾讯科技",
                                       "classification": {"category": "business"}}
        self.hub.contacts["alice_x"] = {"id": "alice_x", "username": "alice_x", "nickname": "Alice",
                                        "classification": {"category": "unknown"}}
        b.store.add_message({"id": "1", "chat": "qqtech", "at": 1})
        b.store.add_message({"id": "2", "chat": "alice_x", "at": 1})
        kinds = b.people_kinds(["qqtech", "alice_x"])
        self.assertEqual(kinds.get("qqtech"), "bot")
        self.assertIn("qqtech", wb.BUSINESS)
        self.assertNotIn("alice_x", wb.PERSONS)  # the hub could not say: still undecided

    def test_a_picture_is_fetched_from_the_hub(self):
        b = self.signed_in()
        got, *_ = self.listen(b)
        m = self.hub.message("wxid_friend", "", wire=3)
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
        self.hub.blobs["asset_orig"] = png
        self.hub.blobs["asset_thumb"] = b"\xff\xd8\xff" + b"t" * 16
        thumb_url = "/v1/accounts/account-1/media/asset_thumb/content"
        # the thumbnail is there already (the hub fetches it by itself); the original only when asked for
        self.hub.assets[m["id"]] = [
            {"id": "asset_orig", "kind": "image", "variant": "original", "state": "pending", "contentUrl": None,
             "byteLength": None, "retryable": True},
            {"id": "asset_thumb", "kind": "image", "variant": "thumbnail", "state": "available",
             "contentUrl": thumb_url, "byteLength": 19, "retryable": True}]
        self.assertTrue(wait_until(lambda: got))
        ev, st, text = b.fetch_media(m["serverId"], wait=10)
        self.assertEqual(st["state"], "done", st)
        with open(st["files"]["image"], "rb") as f:
            self.assertEqual(f.read(), png)
        self.assertIn("本地文件", text)


class MediaFailuresTest(Base):
    def image(self, b, got):
        m = self.hub.message("wxid_friend", "", wire=3)
        self.hub.assets[m["id"]] = [{"id": "asset_o", "kind": "image", "variant": "original", "state": "pending",
                                     "contentUrl": None, "byteLength": None, "retryable": True}]
        self.assertTrue(wait_until(lambda: got))
        return m

    def test_a_failed_job_ends_the_fetch_at_once(self):
        self.hub.job_fail = "media_integrity_failed"
        b = self.signed_in()
        got, *_ = self.listen(b)
        m = self.image(b, got)
        t0 = time.time()
        ev, st, text = b.fetch_media(m["serverId"], wait=30)
        self.assertLess(time.time() - t0, 10)
        self.assertIn("media_integrity_failed", text)

    def test_a_cut_download_is_not_kept(self):
        b = self.signed_in()
        got, *_ = self.listen(b)
        m = self.image(b, got)
        self.hub.blobs["asset_o"] = b"\x89PNG\r\n\x1a\n" + b"0" * 64
        self.hub.cut["asset_o"] = 10
        ev, st, text = b.fetch_media(m["serverId"], wait=30)
        self.assertNotIn("image", (st or {}).get("files") or {})
        self.assertIn("没能下载", text)


class MessageShapesTest(unittest.TestCase):
    """The hub's message shapes (v2.7.1) as the program's message events."""

    def link(self):
        link = wh.HubLink(wh.HubClient("http://127.0.0.1:9", "test-token-0123456789"))
        link.account, link.me = "account-1", "wxid_me"
        link._file_media = lambda hub_id, name: {"kind": "file", "url": "hub:%s:original" % hub_id,
                                                 "aes_key": "hub", "name": "报告.pdf", "size": 12}
        return link

    def msg(self, **kw):
        m = {"id": "msg_x", "threadId": GROUP, "serverId": "77", "senderId": "wxid_a", "direction": "incoming",
             "type": 1, "appMessageType": None, "body": "hi", "createdAt": "2026-11-18T10:00:00Z", "link": None,
             "quote": None, "mentions": {"state": "known", "self": False, "all": False, "userIds": [],
                                         "source": None}}
        m.update(kw)
        return m

    def test_shapes(self):
        L = self.link()
        ev = L.message_event(self.msg(), "live")
        self.assertEqual((ev["id"], ev["hub_id"], ev["sender"], ev["text"], ev["at"], ev["quotable"]),
                         ("77", "msg_x", "wxid_a", "hi", 1794996000, True))
        f = L.message_event(self.msg(type=49, appMessageType=6, body=None), "live")
        self.assertEqual((f["type"], f["text"], f["media"]["url"]), ("FILE", "[文件] 报告.pdf", "hub:msg_x:original"))
        r = L.message_event(self.msg(type=49, appMessageType=19, body="[Chat History] 群聊记录\n小A: 你好"), "live")
        self.assertEqual(r["text"], "[聊天记录] 群聊记录\n小A: 你好")
        q = L.message_event(self.msg(type=49, appMessageType=57, body="同意",
                                     quote={"serverId": "55", "senderId": "wxid_b", "embeddedText": "提案"}), "live")
        self.assertEqual((q["type"], q["text"], q["parent"], q["quoted_text"], q["quotable"]),
                         ("TEXT", "同意", "55", "提案", False))
        img = L.message_event(self.msg(type=3, body=None), "live")
        self.assertEqual((img["text"], img["media"]["mid"], img["media"]["thumb"]),
                         ("[图片]", "hub:msg_x:original", "hub:msg_x:thumbnail"))
        rc = L.message_event(self.msg(type=10000, body="[Message recalled]", recall={"observedAt": "x"}), "live")
        self.assertEqual((rc["type"], rc["text"]), ("SYSTEM", "[撤回了一条消息]"))
        me = L.message_event(self.msg(direction="outgoing", senderId="wxid_me"), "live")
        self.assertTrue(me["mine"])
        self.assertEqual(me["sender"], "wxid_me")
        at = L.message_event(self.msg(mentions={"state": "known", "self": True, "all": False, "userIds": ["wxid_me"],
                                                "source": "msgsource.atuserlist"}), "live")
        self.assertTrue(at["at_me"])

    def test_an_error_frame_ends_the_stream(self):
        import http.server

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b": connected\n\nid: 1\ndata: {\"cursor\":\"1\",\"kind\":\"x\",\"data\":{}}\n\n"
                                 b"event: error\ndata: {\"error\":{\"code\":\"event_cursor_expired\"}}\n\n")

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        self.addCleanup(srv.server_close)
        c = wh.HubClient("http://127.0.0.1:%d" % srv.server_address[1], "test-token-0123456789")
        got = []
        with self.assertRaises(wh.HubDown):
            for item in c.events("/s", threading.Event()):
                got.append(item)
        self.assertEqual([g[0] for g in got], [wh.OPEN, "1"])


if __name__ == "__main__":
    unittest.main()
