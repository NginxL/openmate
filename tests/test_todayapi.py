"""The extension's own HTTP for another extension's 今天 (todayapi): who may call it, what it answers signed out and
signed in, 处理完 for the version the caller saw, its limits, and its socket going away with the program."""

try:  # first: every temporary directory of this run in one, removed at its end
    import _runtmp  # noqa: F401
except ImportError:  # (started by its path from elsewhere: its directory is not on the path)
    pass
import dataclasses
import http.client
import json
import os
import shutil
import socket
import tempfile
import unittest

from harness import Harness, wait_until

import openmate_feishu
from openmate_core import todayapi

FEISHU = {"X-Persona-Role": "extension", "X-Persona-Device": "extension:openmate-feishu"}


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=5)
        self.unix_path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(self.unix_path)
        self.sock = s


class TodayAPITest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(prefix="om-")
        self.addCleanup(shutil.rmtree, d, True)
        self.sock = os.path.join(d, "x.sock")
        pf = dataclasses.replace(openmate_feishu.platform(), today_api_callers=("extension:openmate-feishu",))
        self.h = Harness(self, platform=pf, env={"PERSONA_EXTENSION_SOCKET": self.sock})
        wait_until(lambda: os.path.exists(self.sock), what="the socket")

    def call(self, method, route, body=None, headers=None, raw=None, ctype="application/json"):
        c = UnixHTTP(self.sock)
        h = dict(FEISHU if headers is None else headers)
        data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
        if data is not None and ctype:
            h["Content-Type"] = ctype
        c.request(method, route, body=data, headers=h)
        r = c.getresponse()
        out = r.read()
        c.close()
        return r.status, (json.loads(out) if out else {})

    def signed_in(self):
        self.h.sign_in()
        wait_until(lambda: self.h.app._catchup_done.is_set(), what="catch-up")
        return self.call("GET", "/today")[1]

    def test_the_socket_is_there_once_initialized(self):
        self.assertIsNotNone(self.h.app.today_api, "bound while initialize is answered: the host checks /health right after")

    def test_health_needs_no_caller_and_the_rest_only_the_named_one(self):
        self.assertEqual(self.call("GET", "/health", headers={}), (200, {"ok": True}))
        self.assertEqual(self.call("GET", "/today", headers={})[0], 403)
        self.assertEqual(self.call("GET", "/today", headers={"X-Persona-Role": "full"})[0], 403, "not every device")
        self.assertEqual(self.call("GET", "/today", headers={"X-Persona-Role": "extension",
                                                             "X-Persona-Device": "extension:other"})[0], 403)
        self.assertEqual(oct(os.stat(self.sock).st_mode & 0o777), "0o600")

    def test_signed_out_says_only_that(self):
        status, d = self.call("GET", "/today")
        self.assertEqual(status, 200)
        self.assertEqual(d, {"state": "signed_out", "platform": "飞书", "account": ""})
        self.assertEqual(self.call("GET", "/chat?id=chat_example_dm_a&account=")[0], 409)
        self.assertEqual(self.call("POST", "/done", {"chat": "chat_example_dm_a", "version": "x", "account": ""})[0], 409)

    def test_today_signed_in(self):
        d = self.signed_in()
        self.assertEqual(d["state"], "up")
        self.assertTrue(d["account"])
        kinds = {n["chat"]: n["kind"] for n in d["needs"]}
        self.assertEqual(kinds, {"chat_example_dm_a": "dm", "chat_example_team": "mention"})
        self.assertEqual(d["counts"]["dms"], 1)
        self.assertEqual(d["counts"]["mentions"], 1)
        self.assertTrue(all(n["version"] and n["title"] and n["at"] > 0 for n in d["needs"]))
        self.assertIsInstance(d["groups"], list)

    def test_a_chat_that_needs_the_user_and_no_other(self):
        d = self.signed_in()
        acct = d["account"]
        status, c = self.call("GET", "/chat?id=chat_example_dm_a&account=%s&n=5" % acct)
        self.assertEqual(status, 200)
        self.assertTrue(c["need"])
        self.assertTrue(c["lines"] and all("text" in l and "who" in l for l in c["lines"]))
        self.assertLessEqual(len(c["lines"]), 5)
        self.assertEqual(self.call("GET", "/chat?id=chat_example_dm_a&account=someone-else")[1]["code"], "account_changed")
        self.assertEqual(self.call("GET", "/chat?id=no_such_chat&account=%s" % acct)[0], 404)

    def test_done_for_the_version_seen(self):
        d = self.signed_in()
        acct = d["account"]
        v = {n["chat"]: n["version"] for n in d["needs"]}["chat_example_dm_a"]
        self.assertEqual(self.call("POST", "/done", {"chat": "chat_example_dm_a", "version": v, "account": acct}), (200, {"ok": True}))
        self.assertNotIn("chat_example_dm_a", [n["chat"] for n in self.call("GET", "/today")[1]["needs"]])
        self.assertEqual(self.call("POST", "/done", {"chat": "chat_example_dm_a", "version": v, "account": acct}), (200, {"ok": True}),
                         "the same again: still ok")
        self.h.world.push("chat_example_dm_a", "user_example_a", "又来一条")
        wait_until(lambda: "chat_example_dm_a" in [n["chat"] for n in self.call("GET", "/today")[1]["needs"]], what="back")
        status, e = self.call("POST", "/done", {"chat": "chat_example_dm_a", "version": v, "account": acct})
        self.assertEqual((status, e["code"]), (409, "changed"), "the page saw the message before: the new one stays")
        self.assertIn("chat_example_dm_a", [n["chat"] for n in self.call("GET", "/today")[1]["needs"]])
        self.assertEqual(self.call("POST", "/done", {"chat": "chat_example_dm_a", "version": "", "account": acct})[0], 400)

    def test_a_done_from_before_a_newer_one_was_marked_changes_nothing(self):
        d = self.signed_in()
        acct = d["account"]
        v1 = {n["chat"]: n["version"] for n in d["needs"]}["chat_example_dm_a"]
        self.h.world.push("chat_example_dm_a", "user_example_a", "第二条")
        wait_until(lambda: {n["chat"]: n["version"] for n in self.call("GET", "/today")[1]["needs"]}.get("chat_example_dm_a") != v1,
                   what="the newer one")
        v2 = {n["chat"]: n["version"] for n in self.call("GET", "/today")[1]["needs"]}["chat_example_dm_a"]
        self.assertEqual(self.call("POST", "/done", {"chat": "chat_example_dm_a", "version": v2, "account": acct})[0], 200)
        status, _ = self.call("POST", "/done", {"chat": "chat_example_dm_a", "version": v1, "account": acct})
        self.assertIn(status, (404, 409), "the page that showed the first one is refused")
        self.assertEqual(self.h.app.store.dismissed(["chat:chat_example_dm_a"]), {"chat:chat_example_dm_a": v2},
                         "the newer 处理完 stays")
        self.assertEqual(self.call("GET", "/chat?id=chat_example_dm_a&account=%s" % acct)[0], 404, "处理完: no lines")

    def test_a_mark_written_while_a_done_is_under_way_wins(self):
        d = self.signed_in()
        acct = d["account"]
        v1 = {n["chat"]: n["version"] for n in d["needs"]}["chat_example_dm_a"]
        store = self.h.app.store
        real = todayapi.ChatsToday

        class Meanwhile(real):
            def needs(self, only=None):
                out = real.needs(self, only)
                store.dismiss("chat:chat_example_dm_a", "v2-of-another-writer")  # after the need was read
                return out

        todayapi.ChatsToday = Meanwhile
        self.addCleanup(setattr, todayapi, "ChatsToday", real)
        status, e = self.call("POST", "/done", {"chat": "chat_example_dm_a", "version": v1, "account": acct})
        self.assertEqual((status, e.get("code")), (409, "changed"))
        self.assertEqual(store.dismissed(["chat:chat_example_dm_a"]), {"chat:chat_example_dm_a": "v2-of-another-writer"})

    def test_bad_requests(self):
        self.signed_in()
        self.assertEqual(self.call("POST", "/done", raw=b"{}", ctype="")[0], 415)
        self.assertEqual(self.call("POST", "/done", raw=b"x" * (todayapi.BODY_MOST + 1))[0], 413)
        self.assertEqual(self.call("POST", "/done", raw=b"{not json")[0], 400)
        self.assertEqual(self.call("POST", "/done", raw=b"[1]")[0], 400)
        self.assertEqual(self.call("POST", "/done", raw="\udcff".encode("utf-8", "surrogatepass"))[0], 400)
        self.assertEqual(self.call("GET", "/nope")[0], 404)
        self.assertEqual(self.call("GET", "/health", headers={})[0], 200, "still serving")

    def test_the_socket_goes_with_the_program(self):
        self.h.close()
        wait_until(lambda: not os.path.exists(self.sock), what="the socket removed")


class NoAPITest(unittest.TestCase):
    def test_none_without_callers_or_a_socket(self):
        d = tempfile.mkdtemp(prefix="om-")
        self.addCleanup(shutil.rmtree, d, True)
        sock = os.path.join(d, "x.sock")
        h = Harness(self, env={"PERSONA_EXTENSION_SOCKET": sock})  # Feishu: no callers
        wait_until(lambda: h.app.ready.is_set(), what="started")
        self.assertIsNone(h.app.today_api)
        self.assertFalse(os.path.exists(sock))


if __name__ == "__main__":
    unittest.main()
