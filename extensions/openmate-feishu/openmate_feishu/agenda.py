"""The Feishu calendar in the Persona apps: a page in the main navigation and a card on Overview with today's and the
next days' events, read through lark-cli with the user's own sign-in. Read-only: OpenMate runs only
`lark-cli calendar +agenda`, never anything that writes, and keeps no credentials (lark-cli keeps them in ~/.lark-cli).

Settings: lark_cli (its path; else found on PATH or in a login shell, where an npm install usually is) and
lark_cli_profile (lark-cli's --profile: the app the user signed in to for Persona; empty is lark-cli's default).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import shlex
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from openmate_core.ui import Page, day_start, empty, row, stat

log = logging.getLogger("openmate.calendar")

PAGE = "calendar"
CARD = "calendar-today"
DAYS = 7
CACHE = 60.0
TIMEOUT = 25
REFRESH = 120
LATER_MOST = 30
WEEKDAYS = "一二三四五六日"
RSVP = {"needs_action": ("待回复", "caution"), "tentative": ("暂定", "caution"), "accept": ("已接受", "neutral"),
        "decline": ("已拒绝", "neutral")}


class CalendarError(Exception):
    def __init__(self, message: str, kind: str = "failed"):
        super().__init__(message)
        self.message = message
        self.kind = kind


class LarkCLI:
    """lark-cli as the user installed it: `calendar +agenda` only."""

    def __init__(self, config: dict, run: Callable = subprocess.run):
        self.setting = str(config.get("lark_cli") or "").strip()
        self.profile = str(config.get("lark_cli_profile") or "").strip()
        self._run = run
        self._bin: Optional[str] = None

    def bin(self) -> str:
        if self._bin is not None:
            return self._bin
        name = self.setting or "lark-cli"
        path = ""
        if os.path.isabs(name):
            path = name if os.access(name, os.X_OK) else ""
        else:
            path = shutil.which(name) or ""
            if not path:  # an npm install lives in a version manager's directory that only a login shell has
                try:
                    out = self._run([os.environ.get("SHELL") or "/bin/zsh", "-lc", "command -v " + shlex.quote(name)],
                                    capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL).stdout or ""
                except (OSError, subprocess.SubprocessError):
                    out = ""
                lines = [x.strip() for x in out.splitlines() if x.strip()]
                path = lines[-1] if lines and os.path.isabs(lines[-1]) else ""
        self._bin = path
        return path

    def agenda(self, start: _dt.datetime, end: _dt.datetime) -> List[dict]:
        b = self.bin()
        if not b:
            self._bin = None  # installed later: look again next time
            raise CalendarError("没找到 lark-cli：先在这台电脑上安装飞书 lark-cli，或在扩展设置里填它的路径（lark_cli）", "missing")
        args = [b] + (["--profile", self.profile] if self.profile else []) + [
            "calendar", "+agenda", "--as", "user", "--start", start.isoformat(), "--end", end.isoformat(), "--format", "json"]
        env = dict(os.environ)
        env["PATH"] = os.path.dirname(b) + os.pathsep + env.get("PATH", "")  # an npm shim runs `env node`
        try:
            p = self._run(args, capture_output=True, text=True, timeout=TIMEOUT, env=env, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise CalendarError("lark-cli 读日程超时了，稍后会再试", "timeout")
        except OSError:
            raise CalendarError("lark-cli 启动不了", "missing")
        try:
            d = json.loads(p.stdout or "{}")
        except ValueError:
            raise CalendarError("lark-cli 的回答看不懂（版本不对？）", "failed")
        if not isinstance(d, dict):
            raise CalendarError("lark-cli 的回答看不懂（版本不对？）", "failed")
        if not d.get("ok"):
            err = d.get("error") if isinstance(d.get("error"), dict) else {}
            kind = "%s %s" % (err.get("type") or "", err.get("subtype") or "")
            if "authorization" in kind or "scope" in kind or "login" in kind or "token" in kind:
                login = "lark-cli%s auth login --scope calendar:calendar.event:read" % (
                    " --profile " + self.profile if self.profile else "")
                raise CalendarError("还没授权读日程：在这台电脑上运行 %s，按提示在飞书里确认" % login, "auth")
            raise CalendarError("lark-cli 读日程失败（%s）" % (kind.strip() or "原因不明"), "failed")
        data = d.get("data")
        if isinstance(data, dict):
            data = data.get("items") or data.get("events") or []
        return [e for e in (data or []) if isinstance(e, dict)]


def event_time(t, tz) -> Tuple[Optional[_dt.datetime], bool]:
    """(the moment, all-day) of a start_time / end_time: {datetime, timezone}, {date}, or a timestamp."""
    if not isinstance(t, dict):
        return None, False
    if t.get("datetime"):
        try:
            d = _dt.datetime.fromisoformat(str(t["datetime"]).replace("Z", "+00:00"))
            return (d if d.tzinfo else d.replace(tzinfo=tz)).astimezone(tz), False
        except ValueError:
            return None, False
    if t.get("date"):
        try:
            return _dt.datetime.combine(_dt.date.fromisoformat(str(t["date"])), _dt.time(), tz), True
        except ValueError:
            return None, True
    if t.get("timestamp"):
        try:
            return _dt.datetime.fromtimestamp(int(t["timestamp"]), tz), False
        except (TypeError, ValueError, OverflowError, OSError):
            return None, False
    return None, False


class Event:
    def __init__(self, e: dict, tz):
        self.id = str(e.get("event_id") or "")
        self.title = str(e.get("summary") or "").strip() or "（无标题）"
        self.start, self.all_day = event_time(e.get("start_time"), tz)
        self.end, _ = event_time(e.get("end_time"), tz)
        self.rsvp = str(e.get("self_rsvp_status") or "")
        org = e.get("event_organizer") if isinstance(e.get("event_organizer"), dict) else {}
        self.organizer = str(org.get("display_name") or "")
        vchat = e.get("vchat") if isinstance(e.get("vchat"), dict) else {}
        self.video = bool(vchat.get("meeting_url"))
        link = str(e.get("app_link") or "")
        self.link = link if link.startswith("https://") else ""

    def span(self, with_day: bool = False) -> str:
        if self.start is None:
            return ""
        day = "%s 周%s " % (self.start.strftime("%m-%d"), WEEKDAYS[self.start.weekday()]) if with_day else ""
        if self.all_day:
            return day + "全天"
        end = self.end.strftime("%H:%M") if self.end else ""
        return day + self.start.strftime("%H:%M") + ("–" + end if end else "")

    def detail(self) -> str:
        parts = []
        if self.organizer:
            parts.append("组织者 " + self.organizer)
        if self.video:
            parts.append("视频会议")
        return " · ".join(parts)

    def badge(self) -> Optional[dict]:
        if self.rsvp in ("needs_action", "tentative"):
            text, tone = RSVP[self.rsvp]
            return {"text": text, "tone": tone}
        return None


class Agenda:
    """The next DAYS days of events, read at most once a CACHE."""

    def __init__(self, app, clock: Callable[[], float] = time.time, cli: Optional[LarkCLI] = None):
        self.app = app
        self.clock = clock
        self.cli = cli or LarkCLI(app.config)
        self._mu = threading.Lock()
        self._cache: Optional[Tuple[float, float, List[dict]]] = None  # (read at, for the day starting, events)

    def events(self) -> List[Event]:
        tz = self.app.tz
        now = self.clock()
        start = day_start(tz, now)
        with self._mu:
            hit = self._cache
            if hit is None or hit[1] != start or now - hit[0] >= CACHE:
                begin = _dt.datetime.fromtimestamp(start, tz)
                raw = self.cli.agenda(begin, begin + _dt.timedelta(days=DAYS))
                hit = self._cache = (now, start, raw)
        out = [Event(e, tz) for e in hit[2]]
        out = [e for e in out if e.start is not None and e.rsvp != "decline"]
        out.sort(key=lambda e: (e.start, e.title))
        return out


class CalendarPage(Page):
    def __init__(self, app, agenda: Agenda, card: bool = False):
        self.app = app
        self.agenda = agenda
        self.card = card

    def _split(self, events: List[Event]):
        tz = self.app.tz
        now_ts = self.agenda.clock()
        now = _dt.datetime.fromtimestamp(now_ts, tz)
        tomorrow = _dt.datetime.fromtimestamp(day_start(tz, now_ts), tz) + _dt.timedelta(days=1)
        today = [e for e in events if e.start < tomorrow]
        later = [e for e in events if e.start >= tomorrow]
        return now, today, later

    def _stats(self, now, today: List[Event], events: List[Event], version: int) -> dict:
        timed = [e for e in today if not e.all_day]
        current = [e for e in timed if e.start <= now and (e.end or e.start) > now]
        upcoming = [e for e in timed if e.start > now]
        nxt = upcoming[0] if upcoming else None
        stats = [stat("今天", "%d 个" % len(today), "已过 %d 个" % sum(1 for e in timed if (e.end or e.start) <= now) if timed else "")]
        stats.append(stat("下一个", nxt.start.strftime("%H:%M") if nxt else "—", nxt.title if nxt else "今天没有了"))
        if current:
            end = max((e.end or e.start) for e in current)
            stats.append(stat("现在", "会议中", "%s 结束" % end.strftime("%H:%M"), "caution"))
        else:
            stats.append(stat("空闲", "到 " + nxt.start.strftime("%H:%M") if nxt else "今天都空", ""))
        pending = sum(1 for e in events if e.rsvp == "needs_action")
        if not self.card:
            stats.append(stat("待回复", pending, "接下来 %d 天" % DAYS, "caution" if pending else ""))
        if version >= 2:
            return {"type": "stat_grid", "stats": stats}
        return {"type": "text", "text": " · ".join("%s %s" % (s["label"], s["value"]) for s in stats)}

    def _row(self, e: Event, with_day: bool, interactive: bool, part: str) -> dict:
        # an event can be in two sections (an invitation is also one of the next days): keys are per section
        return row(e.title, e.detail(), e.span(with_day), "open:" + e.id if interactive and e.id else "",
                   key="%s:%s" % (part, e.id), badge=e.badge())

    def render(self, params: dict) -> dict:
        version = int(params.get("ui_version") or 1)
        try:
            events = self.agenda.events()
        except CalendarError as e:
            return {"sections": [{"items": [empty("日程读不到", e.message, version)]}], "refresh_after": REFRESH * 5}
        now, today, later = self._split(events)
        sections: List[Dict[str, Any]] = [{"items": [self._stats(now, today, events, version)]}]
        if self.card:
            nxt = [e for e in today if e.all_day or (e.end or e.start) > now][:3]
            sections.append({"items": [self._row(e, False, False, "next") for e in nxt] or [empty("今天没有日程了", "", version)]})
            return {"sections": sections, "refresh_after": REFRESH}
        pending = [e for e in events if e.rsvp == "needs_action"]
        if pending:
            sections.append({"title": "待回复的邀请", "items": [self._row(e, True, True, "pending") for e in pending]})
        sections.append({"title": "今天", "items": [self._row(e, False, True, "today") for e in today]
                         or [empty("今天没有日程", "", version)]})
        if later:
            sections.append({"title": "接下来 %d 天" % (DAYS - 1),
                             "items": [self._row(e, True, True, "later") for e in later[:LATER_MOST]],
                             **({"footer": "还有 %d 个" % (len(later) - LATER_MOST)} if len(later) > LATER_MOST else {})})
        return {"title": "日程", "sections": sections, "refresh_after": REFRESH}

    def action(self, params: dict) -> dict:
        a = params.get("action")
        aid = str(a.get("id") if isinstance(a, dict) else a or "")
        if aid.startswith("open:"):
            try:
                events = self.agenda.events()
            except CalendarError as e:
                return {"toast": e.message}
            for e in events:
                if e.id == aid[5:]:
                    return {"open_url": e.link} if e.link else {"toast": "这个日程没有可以打开的链接"}
            return {"toast": "这个日程已经不在接下来 %d 天里了" % DAYS}
        return {}


def pages(app) -> Dict[str, Page]:
    agenda = Agenda(app)
    return {PAGE: CalendarPage(app, agenda), CARD: CalendarPage(app, agenda, card=True)}
