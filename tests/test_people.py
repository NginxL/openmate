"""Who is a program: a bot's direct messages and @-mentions are notices, not things that need the user. Asked of the
service in the background (Backend.people_kinds), kept in the store; a draw never waits for it."""

import _runtmp  # noqa: F401  (first: every temporary directory of this run in one, removed at its end)
import os
import tempfile
import time
import types
import unittest

from harness import Harness, wait_until
from openmate_core.backend import Backend, Chat
from openmate_core.people import BATCH, RETRY, RETRY_ERROR, People
from openmate_core.store import Store


def inplace(fn):
    fn()


class Asking(Backend):
    """Only people_kinds matters here."""

    def __init__(self, kinds=None, fail=False):
        self.kinds = kinds or {}
        self.fail = fail
        self.calls = []

    def people_kinds(self, user_ids):
        self.calls.append(list(user_ids))
        if self.fail:
            raise RuntimeError("down")
        return {u: self.kinds[u] for u in user_ids if u in self.kinds}

    login_start = login_poll = login_finish = restore = logout = listen = history = recent_chats = None
    search = chat_info = user_name = media = send = create_chat = None


class PeopleTest(unittest.TestCase):
    def make(self, backend, signed_in=True):
        d = tempfile.mkdtemp(prefix="openmate-people-")
        store = Store(os.path.join(d, "t.db"))
        self.addCleanup(store.close)
        self.changed = []
        app = types.SimpleNamespace(store=store, backend=backend, signed_in=signed_in,
                                    mark_changed=lambda *s: self.changed.append(s))
        self.now = [100.0]
        return People(app, spawn=inplace, clock=lambda: self.now[0]), app

    def test_the_store_keeps_what_the_service_said(self):
        _, app = self.make(Asking())
        app.store.set_people_kinds({"b1": "bot", "u1": "user", "x": "robot", "": "bot"})
        self.assertEqual(app.store.people_kinds(["b1", "u1", "x", "nobody"]), {"b1": "bot", "u1": "user"})
        app.store.set_people_kinds({"u1": "bot"})
        self.assertEqual(app.store.people_kinds(["u1"]), {"u1": "bot"})

    def test_unknown_ones_are_asked_about_behind_and_the_pages_drawn_again(self):
        backend = Asking({"b1": "bot", "u1": "user"})
        people, app = self.make(backend)
        self.assertEqual(people.bots(["b1", "u1", ""]), set(), "not known yet: nobody is left out")
        self.assertEqual(backend.calls, [["b1", "u1"]])
        self.assertEqual(self.changed, [("inbox",)], "the badge and the pages count again")
        self.assertEqual(people.bots(["b1", "u1"]), {"b1"})
        self.assertEqual(len(backend.calls), 1, "known now: not asked again")

    def test_at_most_a_batch_at_a_time(self):
        backend = Asking({"p%03d" % i: "user" for i in range(BATCH + 5)})
        people, _ = self.make(backend)
        people.bots(["p%03d" % i for i in range(BATCH + 5)])
        self.assertEqual([len(c) for c in backend.calls], [BATCH, 5])

    def test_a_failure_is_asked_again_soon_an_unknown_answer_later(self):
        backend = Asking({"u1": "user"}, fail=True)  # e.g. right after the start, before the sign-in is restored
        people, app = self.make(backend)
        people.bots(["u1", "mystery"])
        people.bots(["u1", "mystery"])
        self.assertEqual(len(backend.calls), 1, "not again at once")
        self.assertEqual(self.changed, [])
        backend.fail = False
        self.now[0] += RETRY_ERROR
        people.bots(["u1", "mystery"])
        self.assertEqual(backend.calls[-1], ["mystery", "u1"], "soon after a failed read")
        self.assertEqual(app.store.people_kinds(["u1", "mystery"]), {"u1": "user"})
        self.now[0] += RETRY_ERROR
        people.bots(["mystery"])
        self.assertEqual(len(backend.calls), 2, "the service could not tell: not again before RETRY")
        self.now[0] += RETRY
        people.bots(["mystery"])
        self.assertEqual(backend.calls[-1], ["mystery"])

    def test_not_signed_in_or_a_service_that_cannot_tell(self):
        backend = Asking({"b1": "bot"})
        people, app = self.make(backend, signed_in=False)
        people.bots(["b1"])
        self.assertEqual(backend.calls, [])
        app.signed_in = True
        people.bots(["b1"])
        self.assertEqual(backend.calls, [["b1"]], "kept for the next draw")

        class Plain(Asking):
            people_kinds = Backend.people_kinds

        people, _ = self.make(Plain())
        self.assertEqual(people.bots(["b1"]), set())


class BotsInTheChatsTest(unittest.TestCase):
    def test_a_bots_messages_are_notices(self):
        h = Harness(self)
        h.sign_in()
        wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
        w = h.world
        w.add_chat(Chat("chat_example_dm_bot", "dm", "示例通知机器人", peer_id="bot_example_notice"))
        w.push("chat_example_dm_bot", "bot_example_notice", "构建成功：示例服务 #812")
        w.push("chat_example_chatter", "bot_example_notice", "@示例用户 会议纪要已生成", at_me=True)

        def badges():
            return [e[2] for e in h.hub.events if e[1] == "ui.badge"]

        def page():
            return h.hub.request("ui.render", {"contribution": "chats", "state": "", "ui_version": 6})

        # the seed already makes the badge "2": wait for the page to have seen the bot's lines and told them apart
        def needs_now():
            v = page()
            head = v["sections"][0]["items"][0]["stats"][0]
            return v if head.get("detail") == "机器人通知 2 条不算在内" else None

        view = wait_until(needs_now, timeout=15, what="the bot's two told apart")
        need = [s for s in view["sections"] if s.get("title") == "需要我"][0]["items"]
        self.assertEqual([r["title"] for r in need], ["示例同事甲", "示例项目群"])
        wait_until(lambda: badges() and badges()[-1].get("text") == "2", timeout=15, what="the badge without them")
        head = view["sections"][0]["items"][0]["stats"][0]
        self.assertEqual((head["value"], head["detail"]), ("2", "机器人通知 2 条不算在内"))
        asked = [u for call in w.kinds_calls for u in call]
        self.assertIn("bot_example_notice", asked)
        self.assertEqual(len(asked), len(set(asked)), "each asked once")
        # a person in a group @-mentioning me still counts, even where a bot did too; the row says the person's @me,
        # not the chat's last line
        w.push("chat_example_chatter", "user_example_c", "@示例用户 你怎么看？", at_me=True)
        wait_until(lambda: badges()[-1].get("text") == "3", timeout=15, what="a person's @me counts")
        w.push("chat_example_chatter", "bot_example_notice", "今日排行榜已更新")
        wait_until(lambda: "今日排行榜" in str(h.app.store.messages_before("chat_example_chatter", None, 5)), what="stored")
        need = [s for s in page()["sections"] if s.get("title") == "需要我"][0]["items"]
        chatter = [r for r in need if r["title"] == "示例闲聊群"][0]
        self.assertEqual(chatter["subtitle"], "示例同事丙：@示例用户 你怎么看？")
        time.sleep(0.1)


if __name__ == "__main__":
    unittest.main()
