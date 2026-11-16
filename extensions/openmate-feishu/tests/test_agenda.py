"""The calendar page and card: lark-cli calendar +agenda (read-only), what the page says, the card, opening an event,
and what it says when lark-cli is missing, not signed in or slow."""

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "..", "core"))

from openmate_feishu import agenda  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 11, 16, 10, 0, tzinfo=TZ).timestamp()


def at(day, hh, mm=0):
    return {"datetime": dt.datetime(2026, 11, day, hh, mm, tzinfo=TZ).isoformat(), "timezone": "Asia/Shanghai"}


def ev(eid, title, day, start, end, rsvp="accept", video=False, link=True, organizer="示例同事甲"):
    e = {"event_id": eid, "summary": title, "start_time": at(day, *start), "end_time": at(day, *end),
         "self_rsvp_status": rsvp, "event_organizer": {"display_name": organizer, "user_id": "u1"},
         "free_busy_status": "busy"}
    if video:
        e["vchat"] = {"meeting_url": "https://vc.example.com/j/1", "vc_type": "vc"}
    if link:
        e["app_link"] = "https://applink.example.com/client/calendar/event/detail?id=" + eid
    return e


EVENTS = [
    ev("e1", "站会", 16, (9, 0), (10, 30)),
    ev("e2", "示例评审会", 16, (14, 0), (15, 0), rsvp="needs_action", video=True),
    ev("e3", "交流分享", 16, (20, 30), (21, 30), rsvp="tentative"),
    ev("e4", "已拒绝的会", 16, (16, 0), (17, 0), rsvp="decline"),
    ev("e5", "周会", 17, (10, 0), (11, 0)),
    {"event_id": "e6", "summary": "团建", "start_time": {"date": "2026-11-18"}, "end_time": {"date": "2026-11-19"},
     "self_rsvp_status": "accept"},
]


class Runner:
    """subprocess.run as lark-cli answers it."""

    def __init__(self, answer=None, raise_=None):
        self.answer = answer if answer is not None else {"ok": True, "data": EVENTS}
        self.raise_ = raise_
        self.calls = []

    def __call__(self, args, **kw):
        self.calls.append((args, kw))
        if self.raise_ is not None:
            raise self.raise_
        out = self.answer if isinstance(self.answer, str) else json.dumps(self.answer, ensure_ascii=False)
        return subprocess.CompletedProcess(args, 0, out, "")


def pages(config=None, runner=None, clock=None):
    tmp = tempfile.mkdtemp(prefix="openmate-agenda-")
    fake = os.path.join(tmp, "lark-cli")
    with open(fake, "w") as f:
        f.write("#!/bin/sh\nexit 0\n")
    os.chmod(fake, 0o755)
    cfg = {"lark_cli": fake, "lark_cli_profile": "persona"}
    cfg.update(config or {})
    app = types.SimpleNamespace(config=cfg, tz=TZ)
    runner = runner or Runner()
    clock = clock or (lambda: NOW)
    a = agenda.Agenda(app, clock=clock, cli=agenda.LarkCLI(cfg, run=runner))
    return agenda.CalendarPage(app, a), agenda.CalendarPage(app, a, card=True), runner, fake


def items(view):
    return [it for s in view["sections"] for it in s["items"]]


def section(view, title):
    return [s for s in view["sections"] if s.get("title") == title][0]["items"]


class AgendaTest(unittest.TestCase):
    def test_the_command_is_a_read_of_the_next_days(self):
        page, _, runner, fake = pages()
        page.render({"ui_version": 6})
        args, kw = runner.calls[0]
        self.assertEqual(args[:7], [fake, "--profile", "persona", "calendar", "+agenda", "--as", "user"])
        self.assertEqual(args[7:9], ["--start", "2026-11-16T00:00:00+08:00"])
        self.assertEqual(args[9:11], ["--end", "2026-11-23T00:00:00+08:00"])
        self.assertEqual(args[11:], ["--format", "json"])
        self.assertTrue(kw["env"]["PATH"].startswith(os.path.dirname(fake) + os.pathsep), "its own dir first (env node)")
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        _, _, runner2, fake2 = pages(config={"lark_cli_profile": ""})
        pages_default = agenda.CalendarPage(types.SimpleNamespace(config={}, tz=TZ), agenda.Agenda(
            types.SimpleNamespace(config={"lark_cli": fake2}, tz=TZ), clock=lambda: NOW,
            cli=agenda.LarkCLI({"lark_cli": fake2}, run=runner2)))
        pages_default.render({"ui_version": 6})
        self.assertNotIn("--profile", runner2.calls[0][0])

    def test_the_page(self):
        page, _, _, _ = pages()
        view = page.render({"ui_version": 6})
        self.assertEqual(view["title"], "日程")
        grid = items(view)[0]["stats"]
        self.assertEqual([(s["label"], s["value"]) for s in grid],
                         [("今天", "3 个"), ("下一个", "14:00"), ("现在", "会议中"), ("待回复", "1")])
        self.assertEqual(grid[2]["detail"], "10:30 结束")
        self.assertEqual(grid[1]["detail"], "示例评审会")
        pending = section(view, "待回复的邀请")
        self.assertEqual([(r["title"], r["value"]) for r in pending], [("示例评审会", "11-16 周一 14:00–15:00")])
        self.assertEqual(pending[0]["badge"], {"text": "待回复", "tone": "caution"})
        today = section(view, "今天")
        self.assertEqual([r["title"] for r in today], ["站会", "示例评审会", "交流分享"], "not the declined one")
        self.assertEqual(today[1]["subtitle"], "组织者 示例同事甲 · 视频会议")
        self.assertEqual(today[2]["badge"]["text"], "暂定")
        self.assertNotIn("badge", today[0])
        self.assertEqual(today[0]["action"], {"id": "open:e1"})
        later = section(view, "接下来 6 天")
        self.assertEqual([(r["title"], r["value"]) for r in later], [("周会", "11-17 周二 10:00–11:00"), ("团建", "11-18 周三 全天")])

    def test_free_until_the_next_and_none_left(self):
        page, _, _, _ = pages(clock=lambda: dt.datetime(2026, 11, 16, 11, 0, tzinfo=TZ).timestamp())
        grid = items(page.render({"ui_version": 6}))[0]["stats"]
        self.assertEqual((grid[2]["label"], grid[2]["value"]), ("空闲", "到 14:00"))
        self.assertEqual(grid[0]["detail"], "已过 1 个")
        page, card, _, _ = pages(clock=lambda: dt.datetime(2026, 11, 16, 22, 0, tzinfo=TZ).timestamp())
        grid = items(page.render({"ui_version": 6}))[0]["stats"]
        self.assertEqual((grid[1]["value"], grid[1]["detail"], grid[2]["value"]), ("—", "今天没有了", "今天都空"))
        self.assertEqual(items(card.render({"ui_version": 6}))[1]["title"], "今天没有日程了")

    def test_the_card(self):
        _, card, _, _ = pages()
        view = card.render({"ui_version": 6, "size": "compact"})
        self.assertLessEqual(len(view["sections"]), 2)
        self.assertLessEqual(len(items(view)), 12)
        self.assertNotIn("title", view)
        self.assertEqual([s["label"] for s in items(view)[0]["stats"]], ["今天", "下一个", "现在"])
        rows = [it for it in items(view) if it["type"] == "row"]
        self.assertEqual([r["title"] for r in rows], ["站会", "示例评审会", "交流分享"], "the one on now and the next")
        self.assertFalse(any("action" in r for r in rows))

    def test_opening_an_event(self):
        page, _, _, _ = pages()
        self.assertEqual(page.action({"action": {"id": "open:e2"}}),
                         {"open_url": "https://applink.example.com/client/calendar/event/detail?id=e2"})
        runner = Runner({"ok": True, "data": [ev("x", "无链接", 16, (12, 0), (13, 0), link=False)]})
        page, _, _, _ = pages(runner=runner)
        self.assertIn("没有可以打开的链接", page.action({"action": {"id": "open:x"}})["toast"])
        self.assertIn("不在", page.action({"action": {"id": "open:gone"}})["toast"])

    def test_not_signed_in_missing_and_slow(self):
        runner = Runner({"ok": False, "error": {"type": "authorization", "subtype": "missing_scope", "message": "secret-ish"}})
        page, card, _, _ = pages(runner=runner)
        view = page.render({"ui_version": 6})
        detail = items(view)[0]["detail"]
        self.assertIn("lark-cli --profile persona auth login --scope calendar:calendar.event:read", detail)
        self.assertNotIn("secret-ish", detail, "never lark-cli's own words")
        self.assertEqual(items(card.render({"ui_version": 6}))[0]["title"], "日程读不到")
        page, _, _, _ = pages(config={"lark_cli": "/nonexistent/lark-cli"})
        self.assertIn("没找到 lark-cli", items(page.render({"ui_version": 6}))[0]["detail"])
        page, _, _, _ = pages(runner=Runner(raise_=subprocess.TimeoutExpired("lark-cli", 25)))
        self.assertIn("超时", items(page.render({"ui_version": 6}))[0]["detail"])
        page, _, _, _ = pages(runner=Runner("not json"))
        self.assertIn("看不懂", items(page.render({"ui_version": 6}))[0]["detail"])
        old = items(page.render({"ui_version": 1}))[0]
        self.assertEqual(old["type"], "text", "old apps get text")

    def test_read_once_a_minute(self):
        now = [NOW]
        page, card, runner, _ = pages(clock=lambda: now[0])
        page.render({"ui_version": 6})
        card.render({"ui_version": 6})
        self.assertEqual(len(runner.calls), 1, "the page and the card share one read")
        now[0] += 61
        page.render({"ui_version": 6})
        self.assertEqual(len(runner.calls), 2)


if __name__ == "__main__":
    unittest.main()
