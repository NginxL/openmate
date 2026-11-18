"""自动代答's groups: the marked ones and the 50 the user wrote in last on the chats page, then 找群, a search over every
group the service lists, where any of them can be switched on."""

import json
import time
import unittest

from harness import wait_until

from openmate_core.backend import Chat
from openmate_core.ui import AUTOREPLY_MOST, SEARCH_MOST, check_view

from test_autoreply import TEAM, act, make


def view(h, state="", version=6):
    return h.hub.request("ui.render", {"contribution": "chats", "state": state, "ui_version": version})


def auto_rows(v):
    sec = [s for s in v["sections"] if s.get("title") == "自动代答"][0]
    return [r for r in sec["items"] if r.get("type") == "row"]


class GroupsTest(unittest.TestCase):
    def setUp(self):
        self.h = make(self)
        w = self.h.world
        now = time.time()
        for i in range(60):  # groups the user wrote in, group 0 the longest ago
            cid = "spoke_%02d" % i
            w.add_chat(Chat(id=cid, kind="group", name="发过言的群%02d" % i, members=5))
            w.push(cid, "user_example_b", "大家好", at=now - 7200 + i)
            w.push(cid, "me", "收到", at=now - 3600 + i * 10)
        listed = [Chat(id="listed_%03d" % i, kind="group", name="候选群%03d" % i) for i in range(120)]
        self.h.app.backend.group_candidates = lambda limit: listed[:limit]
        wait_until(lambda: len(self.h.app.store.groups_i_spoke_in(100)) == 60, what="the user's messages stored")

    def test_the_marked_then_the_fifty_i_wrote_in_last(self):
        h = self.h
        act(h, "autoreply", {"auto:" + TEAM: "true"})
        v = view(h)
        self.assertFalse(check_view(v))
        rows = auto_rows(v)
        self.assertEqual(rows[0]["key"], "auto:" + TEAM, "the marked one first")
        groups = [r for r in rows[1:] if r["key"].startswith("auto:")]
        self.assertEqual(len(groups), AUTOREPLY_MOST)
        self.assertEqual([r["title"] for r in groups[:3]], ["发过言的群59", "发过言的群58", "发过言的群57"],
                         "the one I wrote in last first")
        self.assertIn("我最近", groups[0]["subtitle"])
        find = rows[-1]
        self.assertTrue(find["title"].startswith("找群：搜索全部"))
        self.assertEqual(find["action"]["push"]["ui"], "chats")
        self.assertEqual(json.loads(find["action"]["push"]["state"]), {"groups": 1})

    def test_find_a_group_by_name_and_switch_it_on(self):
        h = self.h
        v = view(h, json.dumps({"groups": 1}))
        self.assertFalse(check_view(v))
        self.assertEqual(v["search"]["action"], {"id": "groups:search"})
        out = h.hub.request("ui.action", {"contribution": "chats", "action": {"id": "groups:search"}, "values": {"q": "候选群11"},
                                          "state": v["state"], "ui_version": 6})["view"]
        self.assertFalse(check_view(out))
        self.assertEqual(json.loads(out["state"]), {"groups": 1, "q": "候选群11"})
        self.assertEqual([r["title"] for r in auto_rows(out)], ["候选群11%d" % i for i in range(10)])
        res = h.hub.request("ui.action", {"contribution": "chats", "action": {"id": "autoreply"},
                                          "values": {"auto:listed_115": "true"}, "state": out["state"], "ui_version": 6})
        self.assertIn("已开启", res["toast"])
        self.assertEqual(json.loads(res["view"]["state"]), {"groups": 1, "q": "候选群11"}, "still the search")
        self.assertIn("listed_115", h.app.store.autoreply_chats(), "a group with no message here yet, switched on")

    def test_many_matches_are_capped(self):
        h = self.h
        out = h.hub.request("ui.action", {"contribution": "chats", "action": {"id": "groups:search"}, "values": {"q": "候选群"},
                                          "state": json.dumps({"groups": 1}), "ui_version": 6})["view"]
        self.assertEqual(len(auto_rows(out)), SEARCH_MOST)
        self.assertIn("换个更具体的词", out["sections"][0]["items"][0]["text"])
        none = h.hub.request("ui.action", {"contribution": "chats", "action": {"id": "groups:search"}, "values": {"q": "不存在"},
                                           "state": json.dumps({"groups": 1}), "ui_version": 6})["view"]
        self.assertEqual(none["sections"][0]["items"][1]["title"], "没有名字里带“不存在”的群")

    def test_thousands_of_groups_still_draw(self):
        many = [Chat(id="many_%04d" % i, kind="group", name="群%04d" % i) for i in range(4000)]
        self.h.app.backend.group_candidates = lambda limit: many[:limit]
        for version in (2, 6, 8):
            v = view(self.h, version=version)
            self.assertEqual(check_view(v), [], "within the hub's 300 items (UI version %d)" % version)
        self.assertIn("4062", auto_rows(view(self.h))[-1]["title"], "找群 says how many there are")

    def test_old_apps_open_it_without_push(self):
        rows = auto_rows(view(self.h, version=2))
        self.assertEqual(rows[-1]["action"], {"id": "groups:open"})
        out = act(self.h, "groups:open")
        self.assertEqual(out["view"]["title"], "自动代答 · 找群")


if __name__ == "__main__":
    unittest.main()
