"""A chat's picture in Persona's Inbox (InboxThing.avatar, #10923) and on its conversation page: asked of a backend
with has_avatars (Feishu's) in the background, once a day per chat, kept in the store; an Inbox read never waits for
it, and a picture learnt (or changed) tells the hub to read the rows again. A backend without it (WeChat's) is asked
nothing and its rows are as before."""

try:  # first: every temporary directory of this run in one, removed at its end
    import _runtmp  # noqa: F401
except ImportError:  # (started by its path from elsewhere: its directory is not on the path)
    pass
import os
import sys
import time
import unittest

from harness import ROOT, Harness, fast, wait_until
from openmate_core.fixture import FixtureWorld

sys.path.insert(0, os.path.join(ROOT, "extensions", "openmate-wechat"))
import openmate_wechat  # noqa: E402

DM = "chat_example_dm_a"
TEAM = "chat_example_team"
PIC_DM = "https://avatar.example/person-a.png"
PIC_TEAM = "https://avatar.example/team.png"


def things(h):
    return {t["thread"]: t for t in h.c("inbox.collect")["things"]}


def pictured(world):
    world.avatars_ok = True
    world.chats[DM].avatar = PIC_DM
    world.chats[TEAM].avatar = PIC_TEAM
    for c in world.chats.values():
        if c.avatar is None:
            c.avatar = ""  # asked: it has none
    return world


def signed(test, world, **kw):
    h = Harness(test, world=world, **kw)
    h.sign_in()
    wait_until(lambda: h.app._catchup_done.is_set(), what="catch-up")
    return h


class AvatarTest(unittest.TestCase):
    def test_learnt_in_the_background_then_on_the_rows_and_the_page(self):
        world = pictured(FixtureWorld())
        h = signed(self, world)
        first = things(h)
        before = len(h.hub.changed("inbox"))
        self.assertTrue(first)
        wait_until(lambda: len(h.hub.changed("inbox")) > before, what="the Inbox told to read again")
        t = things(h)
        self.assertEqual((t[DM]["avatar"], t[TEAM]["avatar"]), (PIC_DM, PIC_TEAM))
        self.assertNotIn("avatar", t["chat_example_chatter"], "none: the row draws the initial")
        self.assertEqual(h.app.store.avatars(["chat_example_chatter"])["chat_example_chatter"][0], "")
        self.assertEqual(h.c("thread.page", thread=DM)["chat"]["avatar"], PIC_DM)
        self.assertNotIn("avatar", h.c("thread.page", thread="chat_example_chatter")["chat"])

    def test_asked_once_a_day_and_again_after_a_failed_ask_only_an_hour_later(self):
        world = pictured(FixtureWorld())
        h = signed(self, world, timing=fast(avatar_retry=0.3))
        things(h)
        wait_until(lambda: (h.app.store.avatars([DM]) or None), what="the picture kept")
        wait_until(lambda: h.app._avatars_by is None, what="the round over")
        asked = list(world.info_asked)
        for _ in range(3):
            things(h)
        time.sleep(0.4)  # past avatar_retry: a known picture is still not asked again within the day
        things(h)
        time.sleep(0.1)
        self.assertEqual(world.info_asked, asked, "a picture known today is not asked again")
        value, checked = h.app.store.avatars([DM])[DM]
        with h.app.store._lock:  # a day old
            h.app.store._db.execute("UPDATE avatars SET checked = ? WHERE chat_id = ?", (checked - 86401, DM))
        world.chats[DM].avatar = "https://avatar.example/person-a-new.png"
        n = len(h.hub.changed("inbox"))
        things(h)
        wait_until(lambda: h.app.store.avatars([DM])[DM][0].endswith("-new.png"), what="the new picture")
        wait_until(lambda: len(h.hub.changed("inbox")) > n, what="the Inbox told")
        self.assertEqual(things(h)[DM]["avatar"], "https://avatar.example/person-a-new.png")

    def test_a_failed_ask_is_tried_again_later_and_never_holds_the_inbox(self):
        world = pictured(FixtureWorld())
        h = signed(self, world, timing=fast(avatar_retry=0.3))
        real = h.app.backend.chat_info
        from openmate_core.backend import BackendError

        def away(chat_id):
            raise BackendError("暂时连不上", "unreachable")

        h.app.backend.chat_info = away
        with h.app.store._lock:  # (the catch-up's reads of new chats learnt them already)
            h.app.store._db.execute("DELETE FROM avatars")
        t0 = time.monotonic()
        self.assertNotIn("avatar", things(h)[DM])
        self.assertLess(time.monotonic() - t0, 2.0)
        wait_until(lambda: h.app._avatars_by is None, what="the round over")
        self.assertEqual(h.app.store.avatars([DM]), {}, "nothing known")
        h.app.backend.chat_info = real
        things(h)
        time.sleep(0.1)
        self.assertEqual(h.app.store.avatars([DM]), {}, "not asked again at once")
        time.sleep(0.35)
        things(h)
        wait_until(lambda: h.app.store.avatars([DM]).get(DM, ("", 0))[0] == PIC_DM, what="asked again later")

    def test_asking_for_a_picture_changes_nothing_else_of_the_chat(self):
        world = pictured(FixtureWorld())
        h = signed(self, world)
        team = h.app.store.get_chat(TEAM)
        with h.app.store._lock:  # muted, as a list of chats says (chat_info does not say it)
            h.app.store._db.execute("UPDATE chats SET muted = 1, name = '列表里的名字' WHERE id = ?", (TEAM,))
            h.app.store._db.execute("DELETE FROM avatars")
        things(h)
        wait_until(lambda: h.app.store.avatars([TEAM]).get(TEAM, ("", 0))[0] == PIC_TEAM, what="the picture")
        after = h.app.store.get_chat(TEAM)
        self.assertEqual((after["muted"], after["name"], after["kind"]), (True, "列表里的名字", team["kind"]))

    def test_a_page_opened_asks_for_its_picture_too(self):
        world = pictured(FixtureWorld())
        h = signed(self, world)
        with h.app.store._lock:  # (a chat outside the Inbox's days: no row asked it)
            h.app.store._db.execute("DELETE FROM avatars")
        self.assertNotIn("avatar", h.c("thread.page", thread=DM)["chat"])
        wait_until(lambda: h.app.store.avatars([DM]).get(DM, ("", 0))[0] == PIC_DM, what="asked in the background")
        self.assertEqual(h.c("thread.page", thread=DM)["chat"]["avatar"], PIC_DM)

    def test_another_accounts_sign_in_forgets_them(self):
        world = pictured(FixtureWorld())
        h = signed(self, world)
        things(h)
        wait_until(lambda: h.app.store.avatars([DM]), what="the picture kept")
        h.app.store.wipe_account_data("user_example_other")
        self.assertEqual(h.app.store.avatars([DM, TEAM]), {})

    def test_a_backend_without_pictures_is_asked_nothing_and_its_rows_are_as_before(self):
        world = FixtureWorld()
        world.chats[DM].avatar = PIC_DM  # (a backend that does not say has_avatars: never read)
        h = signed(self, world)
        asked = list(world.info_asked)
        t = things(h)
        time.sleep(0.2)
        self.assertTrue(t)
        self.assertFalse(any("avatar" in th for th in t.values()))
        self.assertEqual(world.info_asked, asked)
        self.assertNotIn("avatar", h.c("thread.page", thread=DM)["chat"])

    def test_wechat_rows_carry_no_picture(self):
        world = FixtureWorld()
        h = signed(self, world, platform=openmate_wechat.platform())
        t = things(h)
        self.assertTrue(t)
        self.assertFalse(any("avatar" in th for th in t.values()))
        self.assertEqual(h.app.store.avatars(t), {})


if __name__ == "__main__":
    unittest.main()
