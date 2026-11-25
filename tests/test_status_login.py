"""status states, the QR sign-in, credentials in the hub's secrets, the live link and history catch-up."""

try:  # first: every temporary directory of this run in one, removed at its end
    import _runtmp  # noqa: F401
except ImportError:  # (started by its path from elsewhere: its directory is not on the path)
    pass
import os
import stat
import time
import unittest

from harness import CID, Harness, HubSideError, fast, secret_json, wait_until
from openmate_core.backend import BackendError


class StatusTest(unittest.TestCase):
    def test_not_signed_in_without_credentials(self):
        h = Harness(self)
        st = h.status()
        self.assertEqual(st["state"], "not_signed_in")
        self.assertTrue(st["error"])
        self.assertTrue(st["guide"])
        self.assertNotIn("account", st)

    def test_ok_only_when_the_link_is_really_up(self):
        h = Harness(self)
        h.world.hold_up = True  # connected, not receiving yet
        p = h.c("login.step", step="")
        p = h.c("login.step", step=p["step"])
        p = h.c("login.step", step=p["step"])  # done after waiting login_wait at most
        self.assertTrue(p["done"])
        st = h.status()
        self.assertEqual(st["state"], "error")
        self.assertIn("正在连接", st["error"])
        self.assertEqual(h.world.history_calls, [], "history before the link was up")
        h.world.hold_up = False
        h.wait_ok()
        wait_until(lambda: h.world.history_calls, what="catch-up after up")
        st = h.status()
        self.assertEqual(st["account"], "示例用户")
        self.assertNotIn("error", st)
        wait_until(lambda: "last_sync" in h.status(), what="last_sync")

    def test_last_event(self):
        h = Harness(self)
        h.sign_in()
        self.assertNotIn("last_event", h.status())
        h.world.push("chat_example_team", "user_example_a", "新消息")
        wait_until(lambda: "last_event" in h.status(), what="last_event")

    def test_expired_credentials(self):
        h = Harness(self)
        h.sign_in()
        h.world.expired = True
        wait_until(lambda: h.status()["state"] == "error", what="expired")
        st = h.status()
        self.assertIn("失效", st["error"])
        self.assertTrue(st["guide"])

    def test_socks_proxy_error_is_explained(self):
        h = Harness(self)
        h.world.listen_error = BackendError("系统代理是 SOCKS，但 LarkAgentX 的 Python 里没有装 python-socks", "proxy")
        p = h.c("login.step", step="")
        for _ in range(3):
            p = h.c("login.step", step=p["step"]) if not p.get("done") else p
        st = wait_until(lambda: (lambda s: s if "python-socks" in (s.get("error") or "") else None)(h.status()),
                        what="the proxy error")
        self.assertEqual(st["state"], "error")
        # it keeps trying, and recovers once fixed
        h.world.listen_error = None
        h.wait_ok()

    def test_a_link_down_says_why_in_the_log(self):
        h = Harness(self)
        h.sign_in()
        secret = "tok_" + "x1y2z3" * 8
        with self.assertLogs("openmate", level="INFO") as logs:
            with h.world.lock:
                h.world.link_notes += [("down", "网络不通"), ("down", "网络不通"),
                                       ("down", "AgentWechat 出错了：\nevent_cursor_expired %s" % secret), ("up", "")]
            wait_until(lambda: any("event_cursor_expired" in line for line in logs.output)
                       and logs.output[-1] == "INFO:openmate:link up", what="the downs and the way back up")
        lines = [line for line in logs.output if ":link " in line]
        self.assertEqual(lines, ["INFO:openmate:link down: 网络不通",  # the same why again: not again
                                 "INFO:openmate:link down: AgentWechat 出错了： event_cursor_expired …",
                                 "INFO:openmate:link up"])
        self.assertNotIn(secret, "\n".join(logs.output))

    def test_unreachable_then_reconnect(self):
        h = Harness(self)
        h.sign_in()
        first = h.world.connections
        h.world.listen_error = BackendError("网络不通", "unreachable")
        h.world.drop()
        st = wait_until(lambda: (lambda s: s if "网络不通" in (s.get("error") or "") else None)(h.status()), what="down")
        self.assertEqual(st["state"], "error")
        self.assertIn("重连", st["error"])
        h.world.listen_error = None
        h.wait_ok()
        self.assertGreater(h.world.connections, first)

    def test_not_configured_backend(self):
        def factory(cfg, data_dir):
            raise BackendError("还没有设置 LarkAgentX 的 Python（larkx_python）", "config")

        h = Harness(self, backend_factory=factory)
        st = h.status()
        self.assertEqual(st["state"], "not_configured")
        self.assertIn("larkx_python", st["error"])
        with self.assertRaises(HubSideError) as e:
            h.c("login.step", step="")
        self.assertIn("larkx_python", e.exception.message)

    def test_a_backend_that_broke_is_an_error_not_a_setting(self):
        def factory(cfg, data_dir):
            raise BackendError("加载 LarkAgentX 时出错，详情见扩展日志", "unknown")

        st = Harness(self, backend_factory=factory).status()
        self.assertEqual((st["state"], st["error"]), ("error", "加载 LarkAgentX 时出错，详情见扩展日志"))
        self.assertNotIn("guide", st)


class LoginTest(unittest.TestCase):
    def test_qr_steps_and_credentials_in_secrets(self):
        h = Harness(self)
        p = h.c("login.step", step="", values={})
        self.assertEqual(p["kind"], "qr")
        self.assertEqual(p["step"], "qr")
        self.assertEqual(p["interval"], 2)
        self.assertTrue(p["url"].startswith("https://"))
        self.assertIn("扫码", p["hint"])
        self.assertNotIn("qr", {k for k in p if k == "qr"}, "the program never draws the bitmap")
        p2 = h.c("login.step", step="qr")
        self.assertEqual(p2["url"], p["url"])
        self.assertIn("确认", p2["hint"])
        done = h.c("login.step", step="qr")
        self.assertEqual(done, {"done": True, "account": "示例用户"})
        self.assertIn(CID, h.hub.secrets)
        self.assertEqual(secret_json(h.hub.secrets[CID])["user"], "user_example_me")
        h.wait_ok()
        # no credentials anywhere in the data directory
        for dirpath, _, files in os.walk(h.data_dir):
            for f in files:
                with open(os.path.join(dirpath, f), "rb") as fh:
                    self.assertNotIn(b"fixture_token", fh.read())

    def test_expired_qr_gets_a_new_one(self):
        h = Harness(self)
        p = h.c("login.step", step="")
        h.world.qr_script = ["waiting", "expired"]
        w = h.c("login.step", step="qr")
        self.assertEqual(w["url"], p["url"])
        e = h.c("login.step", step="qr")
        self.assertEqual(e["step"], "qr")
        self.assertNotEqual(e["url"], p["url"])
        self.assertIn("过期", e["hint"])
        h.c("login.step", step="qr")
        self.assertTrue(h.c("login.step", step="qr")["done"])

    def test_empty_step_starts_over(self):
        h = Harness(self)
        a = h.c("login.step", step="")
        b = h.c("login.step", step="")
        self.assertNotEqual(a["url"], b["url"])

    def test_restore_after_restart_and_logout(self):
        h = Harness(self)
        h.sign_in()
        h2 = h.restart()
        h2.wait_ok()
        self.assertEqual(h2.status()["account"], "示例用户")
        self.assertEqual(h2.c("logout"), {})
        self.assertNotIn(CID, h2.hub.secrets)
        st = h2.status()
        self.assertEqual(st["state"], "not_signed_in")
        h3 = h2.restart()
        self.assertEqual(h3.status()["state"], "not_signed_in")

    def test_another_account_forgets_the_previous_chats(self):
        h = Harness(self)
        h.sign_in()
        wait_until(lambda: h.c("inbox.collect")["things"], what="things")
        h.c("logout")
        h.world.me = type(h.world.me)("user_example_other", "另一个示例用户")
        h.world.history.clear()
        h.sign_in()
        self.assertEqual(h.c("inbox.collect")["things"], [])

    def test_store_is_private(self):
        h = Harness(self)
        h.sign_in()
        path = os.path.join(h.data_dir, "openmate.db")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


class DirectLoginTest(unittest.TestCase):
    """An adapter signed in where it connects (WeChat through AgentWechat): 登录 needs no QR."""

    def harness(self, fail: str = ""):
        from openmate_core.backend import LoginDone
        from openmate_core.fixture import FixtureBackend, FixtureWorld

        calls = []

        class Direct(FixtureBackend):
            def login_direct(self):
                calls.append(1)
                if fail:
                    raise BackendError(fail, "unreachable")
                self.signed_in = self.world.me
                return LoginDone(credentials={"fixture_token": "direct", "user": self.world.me.id},
                                 account=self.world.me)

        world = FixtureWorld()
        return calls, Harness(self, world=world, backend_factory=lambda cfg, d: Direct(world))

    def test_done_at_once_and_sealed(self):
        calls, h = self.harness()
        done = h.c("login.step", step="", values={})
        self.assertEqual(done, {"done": True, "account": "示例用户"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(secret_json(h.hub.secrets[CID])["fixture_token"], "direct")
        h.wait_ok()
        # signed out, then in again: once more without a QR
        self.assertEqual(h.c("logout"), {})
        self.assertEqual(h.status()["state"], "not_signed_in")
        self.assertTrue(h.c("login.step", step="")["done"])
        self.assertEqual(len(calls), 2)
        h.wait_ok()

    def test_a_reason_it_cannot_is_shown(self):
        calls, h = self.harness(fail="AgentWechat 里的微信还没登录")
        with self.assertRaises(HubSideError) as e:
            h.c("login.step", step="")
        self.assertIn("AgentWechat 里的微信还没登录", e.exception.message)
        self.assertNotIn(CID, h.hub.secrets)
        self.assertEqual(h.status()["state"], "not_signed_in")

    def test_a_qr_backend_still_gets_its_qr(self):
        h = Harness(self)
        self.assertEqual(h.c("login.step", step="")["kind"], "qr")


class CatchUpTest(unittest.TestCase):
    def test_initial_history_once_bounded_by_history_chats(self):
        h = Harness(self, config={"history_chats": 2})
        h.sign_in()
        wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
        chats = {c for c, _, _ in h.world.history_calls}
        self.assertEqual(len(chats), 2)
        stored = lambda app: {r["chat_id"] for r in app.store._all("SELECT DISTINCT chat_id FROM messages", ())}
        self.assertEqual(stored(h.app), chats)
        # a restart does not read the initial history again: the other chats the service lists keep only what came
        # since the last catch-up (nothing here)
        h.world.history_calls.clear()
        h2 = h.restart()
        h2.wait_ok()
        wait_until(lambda: h2.app.last_sync, what="catch-up")
        self.assertNotIn(h2.app.timing.initial_messages, {n for _, _, n in h.world.history_calls})
        self.assertEqual(stored(h2.app), chats)

    def test_no_initial_history_when_zero(self):
        h = Harness(self, config={"history_chats": 0})
        h.sign_in()
        time.sleep(0.2)
        self.assertEqual(h.world.history_calls, [])
        self.assertEqual(h.c("inbox.collect")["things"], [])

    def test_messages_missed_while_down_come_back_as_history(self):
        h = Harness(self)
        h.sign_in()
        h.world.listen_error = BackendError("网络不通", "unreachable")
        h.world.drop()
        wait_until(lambda: h.status()["state"] == "error", what="down")
        missed = h.world.make("chat_example_team", "user_example_a", "@示例用户 断线时发的", at_me=True)
        h.world.listen_error = None
        h.wait_ok()
        row = wait_until(lambda: h.app.store.message("chat_example_team", missed.id), what="the missed message")
        self.assertTrue(row["from_history"])
        thing = [t for t in h.c("inbox.collect")["things"] if t["thread"] == "chat_example_team"][0]
        self.assertEqual(thing["last"], missed.id)
        self.assertEqual(thing["kind"], "mention")

    def test_a_later_copy_renews_the_text_but_is_not_new(self):
        h = Harness(self)
        h.sign_in()
        wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
        m = h.world.make("chat_example_team", "user_example_a", "旧的读法", type="CARD")
        store = h.app.store
        self.assertEqual(len(store.add_messages([m], True)), 1)
        m.text = "新的读法"
        self.assertEqual(store.add_messages([m], True), [], "not a new message")
        self.assertEqual(store.message("chat_example_team", m.id)["text"], "新的读法")
        m.text = ""
        store.add_messages([m], True)
        self.assertEqual(store.message("chat_example_team", m.id)["text"], "新的读法", "an empty reading keeps the text")

    def test_a_newer_reading_reads_such_chats_once_more(self):
        h = Harness(self, config={"history_chats": 2})
        h.sign_in()
        wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
        store = h.app.store
        self.assertEqual(store.meta_get("render_version"), "1", "a first history is read the current way")
        # a chat outside the catch-up (quiet for 5 days) with a card read the old way
        old = h.world.make("chat_example_old", "user_example_a", "卡片标题", type="CARD", ago=5 * 86400)
        store.add_messages([old], True)
        h.world.history["chat_example_old"][-1].text = "卡片标题\n卡片正文"
        backend = h.app.backend
        backend.render_version, backend.rerender_types = "2", ("CARD",)

        def catch_up():
            h.app._catchup_done.clear()
            h.app._request_catch_up(h.app._gen)
            wait_until(lambda: h.app._catchup_done.is_set(), what="a catch-up")

        h.world.history_calls.clear()
        catch_up()
        self.assertIn(("chat_example_old", None, h.app.timing.rerender_messages), h.world.history_calls)
        self.assertEqual(store.message("chat_example_old", old.id)["text"], "卡片标题\n卡片正文")
        self.assertEqual(store.meta_get("render_version"), "2")
        h.world.history_calls.clear()
        catch_up()
        self.assertNotIn("chat_example_old", {c for c, _, _ in h.world.history_calls}, "once")

    def test_messages_stored_with_no_text_are_read_again_where_they_are(self):
        h = Harness(self)
        h.sign_in()
        wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
        store = h.app.store
        old = h.world.make("chat_example_chatter", "user_example_a", "", ago=3600)  # read as nothing back then
        store.add_messages([old], True)
        for i in range(120):  # it is far below the newest
            store.add_messages([h.world.make("chat_example_chatter", "user_example_b", "闲聊%d" % i)], True)
        self.assertEqual(store.message("chat_example_chatter", old.id)["text"], "")
        old.text = "示例文章 https://example.com/a"  # a newer reading reads it
        backend = h.app.backend
        backend.render_version, backend.rerender_types, backend.rerender_empty = "9", (), ("TEXT",)
        h.app._catchup_done.clear()
        h.app._request_catch_up(h.app._gen)
        wait_until(lambda: h.app._catchup_done.is_set(), what="a catch-up")
        self.assertEqual(store.message("chat_example_chatter", old.id)["text"], "示例文章 https://example.com/a")
        self.assertEqual(store.meta_get("render_version"), "9")

    def test_an_empty_text_is_read_again_at_a_catch_up_and_archived_once_it_reads(self):
        h = Harness(self)
        h.sign_in()
        wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
        store, backend = h.app.store, h.app.backend
        backend.rerender_empty = ("TEXT",)
        backend.render_version = store.meta_get("render_version") or "1"
        store.meta_set("render_version", backend.render_version)
        held = h.world.make("chat_example_team", "user_example_a", "", ago=600)  # held for review: no text yet
        blocked = h.world.make("chat_example_team", "user_example_b", "[拦下了]", ago=500)
        blocked.blocked = True  # the service keeps it from this client: its text is only the note
        store.add_messages([held, blocked], True)
        cursor = ""
        for _ in range(50):  # what the knowledge base reads meanwhile: neither of them
            page = h.c("archive.changes", cursor=cursor)
            self.assertNotIn("[拦下了]", str(page))
            cursor = page.get("next") or cursor
            if not page.get("more"):
                break
        held.text = "审核通过的正文"  # the service lets it through
        h.app._catchup_done.clear()
        h.app._request_catch_up(h.app._gen)
        wait_until(lambda: h.app._catchup_done.is_set(), what="a catch-up")
        self.assertEqual(store.message("chat_example_team", held.id)["text"], "审核通过的正文")
        self.assertIn("审核通过的正文", str(h.c("archive.changes", cursor=cursor)), "its first text in the archive")

    def test_nothing_is_stored_for_an_account_signed_out_while_reading(self):
        h = Harness(self)
        h.sign_in()
        wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
        app = h.app
        m = h.world.make("chat_example_new_one", "user_example_a", "读的时候还是 A")
        real = app.backend.chat_info

        def switched(chat_id):  # the read waits on the service while another account takes the store
            with app.lock:
                app._account_epoch += 1
            return real(chat_id) if chat_id in h.world.chats else type(h.world.chats["chat_example_team"])(chat_id, "group", "x")

        app.backend.chat_info = switched
        self.addCleanup(setattr, app.backend, "chat_info", real)
        self.assertEqual(app.ingest([m], live=False), [])
        self.assertIsNone(app.store.message("chat_example_new_one", m.id))

    def test_a_text_that_comes_back_reaches_the_archive_again(self):
        h = Harness(self)
        h.sign_in()
        wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
        store = h.app.store
        blank = h.world.make("chat_example_team", "user_example_a", "", ago=600)
        store.add_messages([blank], True)
        cursor = max(r["seq"] for r in store.archive_rows(0, 100000))  # the archive read past it, with no text
        blank.text = "示例文章 https://example.com/a"
        store.add_messages([blank], True)
        again = [r for r in store.archive_rows(cursor, 100) if r["id"] == blank.id]
        self.assertEqual([r["text"] for r in again], ["示例文章 https://example.com/a"], "handed over again, with its text")
        self.assertEqual(len([r for r in store.archive_rows(0, 100000) if r["id"] == blank.id]), 1, "still one row")

    def test_a_link_up_during_a_catch_up_gets_its_own(self):
        h = Harness(self)
        h.sign_in()
        wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
        n = len(h.world.history_calls)
        h.app._catchup_done.clear()
        gen = h.app._gen
        with h.app.lock:
            h.app._catchup_running = True  # as if one were running now
        h.app._request_catch_up(gen)  # a second link up meanwhile: remembered, not dropped
        self.assertEqual(len(h.world.history_calls), n)
        with h.app.lock:
            h.app._catchup_running = False
        h.app._request_catch_up(gen)
        wait_until(lambda: h.app._catchup_done.is_set(), what="the second catch-up")
        self.assertGreater(len(h.world.history_calls), n)

    def test_backoff_resets_after_a_stable_link(self):
        h = Harness(self, timing=fast(backoff_min=0.05, backoff_max=5.0, stable_after=0.2))
        h.sign_in()
        for _ in range(4):  # quick drops double the backoff
            h.world.drop()
            time.sleep(0.02)
        time.sleep(0.5)  # stays up longer than stable_after
        h.wait_ok()
        n = h.world.connections
        t0 = time.monotonic()
        h.world.drop()
        wait_until(lambda: h.world.connections > n, timeout=3, what="reconnect")
        self.assertLess(time.monotonic() - t0, 1.0, "the backoff was not reset by the stable link")


if __name__ == "__main__":
    unittest.main()
