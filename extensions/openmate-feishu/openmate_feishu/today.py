"""The "今天" page and card: what needs the user today on one page, from Persona (Agents waiting for the user, its
inbox's "needs you", open to-dos, Agents at work) and from Feishu (chats that need the user, the calendar).

Persona's parts come through the hub API (agents.read, inbox.read, todos.read), read in parallel and at most once a
minute, each with a short wait: a slow or unreachable hub leaves its part out with one sentence, never the page.
Feishu's parts are the chats page's and the calendar page's own (the store; one shared lark-cli read).
"""

from __future__ import annotations

import datetime as _dt
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from openmate_core.hub import HubError
from openmate_core.ui import ChatsToday, Page, day_start, empty, row, stat, tile

from .agenda import Agenda, CalendarError, Event

log = logging.getLogger("openmate.today")

PAGE = "today"
CARD = "today-card"
CACHE = 60.0
WAIT = 8.0
REFRESH = 120
NEEDS_MOST = 12
WORK_MOST = 5
WEEKDAYS = "一二三四五六日"
NEEDS_TIERS = ("action", "needs_you")


class PersonaToday:
    """Persona's side of today, through the hub API: {"waiting": [...], "working": [...], "inbox": [...],
    "todos": [...], "missing": [sentences]}."""

    def __init__(self, app, clock: Callable[[], float] = time.time):
        self.app = app
        self.clock = clock
        self._mu = threading.Lock()
        self._cache: Optional[Tuple[float, dict]] = None

    def _get(self, path: str, query: Optional[dict] = None) -> Any:
        return self.app.hub.request("GET", path, query=query, timeout=WAIT)

    def read(self) -> dict:
        with self._mu:
            hit = self._cache
            if hit is not None and self.clock() - hit[0] < CACHE:
                return hit[1]
        parts: Dict[str, Any] = {}
        jobs = {
            "waiting": lambda: self._get("/api/v1/sessions", {"state": "needs_you", "summary": 1, "roots": 1, "limit": 20}),
            "working": lambda: self._get("/api/v1/sessions", {"state": "working", "summary": 1, "roots": 1, "limit": 20}),
            "inbox": lambda: self._get("/api/v1/inbox"),
            "todos": lambda: self._get("/api/v1/todos"),
        }
        names = {"waiting": "等你的 Agent", "working": "在跑的 Agent", "inbox": "Persona 收件箱", "todos": "待办"}

        def run(key, fn):
            try:
                parts[key] = fn()
            except HubError as e:
                parts[key] = e
            except Exception as e:  # never the page
                parts[key] = e

        if getattr(self.app, "hub", None) is None or not getattr(self.app.hub, "available", False):
            out = {"waiting": [], "working": [], "inbox": [], "todos": [], "missing": ["拿不到 Persona hub 的地址或令牌"]}
            return out
        threads = [threading.Thread(target=run, args=(k, f), daemon=True) for k, f in jobs.items()]
        for t in threads:
            t.start()
        deadline = time.monotonic() + WAIT + 1
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        out: Dict[str, Any] = {"missing": []}
        for key in jobs:
            v = parts.get(key)
            if v is None or isinstance(v, Exception):
                out[key] = []
                out["missing"].append(names[key])
                continue
            if key in ("waiting", "working"):
                out[key] = [s for s in (v.get("sessions") or []) if isinstance(s, dict)]
            elif key == "inbox":
                out[key] = [i for i in (v.get("items") or []) if isinstance(i, dict)]
            else:
                out[key] = [t for t in (v.get("todos") or []) if isinstance(t, dict) and not t.get("done_at")]
        if not out["missing"]:
            with self._mu:
                self._cache = (self.clock(), out)
        return out


def agent_title(s: dict) -> str:
    return str(s.get("title") or s.get("space_name") or s.get("cwd") or "Agent").strip()


def agent_detail(s: dict) -> str:
    parts = [str(s.get("activity") or s.get("last_text") or "").strip()]
    if s.get("space_name") and s.get("title"):
        parts.append(str(s["space_name"]))
    return " · ".join(p for p in parts if p)


def todo_due(t: dict, tz) -> Tuple[Optional[_dt.datetime], str]:
    """(when it is due, how to say it) of a to-do: due_at (a moment) or due_date (a day)."""
    if t.get("due_at"):
        try:
            d = _dt.datetime.fromisoformat(str(t["due_at"]).replace("Z", "+00:00")).astimezone(tz)
            return d, d.strftime("%m-%d %H:%M")
        except ValueError:
            return None, ""
    if t.get("due_date"):
        try:
            d = _dt.datetime.combine(_dt.date.fromisoformat(str(t["due_date"])[:10]), _dt.time(23, 59), tz)
            return d, d.strftime("%m-%d")
        except ValueError:
            return None, ""
    return None, ""


class TodayPage(Page):
    def __init__(self, app, agenda: Agenda, persona: PersonaToday, chats: Optional[ChatsToday] = None,
                 card: bool = False):
        self.app = app
        self.agenda = agenda
        self.persona = persona
        self.chats = chats or ChatsToday(app)
        self.card = card

    def _calendar(self, now) -> Tuple[List[Event], List[Event], str]:
        """(today's events still to come or on now, invitations waiting for an answer, why it is missing or "")."""
        try:
            events = self.agenda.events()
        except CalendarError as e:
            return [], [], e.message
        tomorrow = _dt.datetime.fromtimestamp(day_start(self.app.tz, now.timestamp()), self.app.tz) + _dt.timedelta(days=1)
        today = [e for e in events if e.start < tomorrow and (e.all_day or (e.end or e.start) > now)]
        pending = [e for e in events if e.rsvp == "needs_action"]
        return today, pending, ""

    def render(self, params: dict) -> dict:
        version = int(params.get("ui_version") or 1)
        tz = self.app.tz
        now = _dt.datetime.fromtimestamp(self.agenda.clock(), tz)
        p = self.persona.read()
        try:
            chat_needs = self.chats.needs()
        except Exception:
            log.exception("the chats that need the user could not be read")
            chat_needs = []
        try:
            counts = self.chats.counts(chat_needs)
        except Exception:
            counts = {"mentions": sum(1 for t in chat_needs if t["kind"] == "mention"),
                      "dms": sum(1 for t in chat_needs if t["kind"] == "dm"), "messages": 0, "chats": 0}
        today, pending, cal_missing = self._calendar(now)
        current = [e for e in today if not e.all_day and e.start <= now]
        upcoming = [e for e in today if not e.all_day and e.start > now]
        inbox_needs = [i for i in p["inbox"] if str(i.get("tier") or "") in NEEDS_TIERS]
        end_of_day = _dt.datetime.fromtimestamp(day_start(tz, now.timestamp()), tz) + _dt.timedelta(days=1)
        due_today = [t for t in p["todos"] if (todo_due(t, tz)[0] or end_of_day) < end_of_day]
        needs_total = len(p["waiting"]) + len(inbox_needs) + len(chat_needs)

        if cal_missing:
            cal_value, cal_line = "—", "读不到"
        else:
            cal_value = "%d 个" % len(today)
            if current:
                cal_line = "会议中，%s 结束" % max((e.end or e.start) for e in current).strftime("%H:%M")
            elif upcoming:
                cal_line = "下一个 %s · %s" % (upcoming[0].start.strftime("%H:%M"), upcoming[0].title)
            else:
                cal_line = "今天没有了"
        blocks = [
            # (title, value, line, icon, color, badge, key)
            ("需要我", str(needs_total), "飞书 %d · 收件箱 %d · Agent %d" % (len(chat_needs), len(inbox_needs), len(p["waiting"])),
             "bell", "red" if needs_total else "gray", None, "b:needs"),
            ("日程", cal_value, cal_line, "calendar", "blue",
             {"text": "待回复 %d" % len(pending), "tone": "caution"} if pending else None, "b:calendar"),
            ("飞书", "@我 %d · 私聊 %d" % (counts["mentions"], counts["dms"]),
             "今天收到 %d 条 · %d 个会话" % (counts["messages"], counts["chats"]), "chat", "indigo", None, "b:feishu"),
            ("Agent", "%d 在跑" % len(p["working"]), "等你 %d" % len(p["waiting"]) if p["waiting"] else "没有在等你的",
             "robot", "teal", {"text": "等你 %d" % len(p["waiting"]), "tone": "danger"} if p["waiting"] else None, "b:agents"),
            ("待办", "%d 个" % len(p["todos"]), "今天到期 %d" % len(due_today) if due_today else "今天没有到期的",
             "check", "green", {"text": "到期 %d" % len(due_today), "tone": "caution"} if due_today else None, "b:todos"),
        ]
        if self.card:
            blocks = blocks[:4]  # a card's grid holds four tiles
        if version >= 6:
            head = {"type": "grid", "max_columns": 4 if self.card else 5, "key": "blocks",
                    "tiles": [tile(t, v, line, icon, color, key, badge) for t, v, line, icon, color, badge, key in blocks]}
        elif version >= 2:
            head = {"type": "stat_grid", "stats": [stat(t, v, line, "caution" if badge else "") for t, v, line, _, _, badge, _ in blocks[:6]]}
        else:
            head = {"type": "text", "text": " · ".join("%s %s" % (t, v) for t, v, *_ in blocks)}

        icons = version >= 6
        needs_rows = []
        for s in p["waiting"]:
            needs_rows.append(row(agent_title(s), agent_detail(s) or "在等你回答", "", key="wait:" + str(s.get("session_id")),
                                  badge={"text": "Agent 等你", "tone": "danger"}, icon="robot" if icons else "",
                                  icon_color="red" if icons else ""))
        for i in inbox_needs:
            needs_rows.append(row(str(i.get("title") or "收件箱"), str(i.get("summary") or i.get("preview") or ""), "",
                                  key="inbox:" + str(i.get("id")), badge={"text": "收件箱", "tone": "caution"},
                                  icon="mail" if icons else "", icon_color="amber" if icons else ""))
        for t in chat_needs:
            mention = t["kind"] == "mention"
            needs_rows.append(row(t["title"], "%s：%s" % (t["sender"], t["preview"]), "", key="chat:" + t["thread"],
                                  badge={"text": "@我" if mention else "私聊", "tone": "caution" if mention else "neutral"},
                                  icon="chat" if icons else "", icon_color=("orange" if mention else "indigo") if icons else ""))
        if self.card:
            return {"sections": [{"items": [head]}, {"items": needs_rows[:3] or [empty("没有要你处理的事", "", version)]}],
                    "refresh_after": REFRESH}

        sections: List[Dict[str, Any]] = [{"items": [head]}]
        need_sec: Dict[str, Any] = {"title": "需要我 · %d" % needs_total if needs_total else "需要我",
                                    "items": needs_rows[:NEEDS_MOST] or [empty("没有要你处理的事", "", version)]}
        if len(needs_rows) > NEEDS_MOST:
            need_sec["footer"] = "还有 %d 件，在“飞书”页和收件箱里看" % (len(needs_rows) - NEEDS_MOST)
        sections.append(need_sec)
        if cal_missing:
            sections.append({"title": "日程", "items": [empty("日程读不到", cal_missing, version)]})
        else:
            cal_rows = [row(e.title, e.detail(), e.span(False), "open:" + e.id if e.id else "", key="ev:" + e.id,
                            badge={"text": "进行中", "tone": "positive"} if e in current else e.badge(),
                            icon="clock" if icons else "", icon_color=("green" if e in current else "blue") if icons else "")
                        for e in today[:5]]
            cal_sec: Dict[str, Any] = {"title": "日程 · %s" % cal_line, "items": cal_rows or [empty("今天没有日程了", "", version)]}
            footer = []
            if len(today) > 5:
                footer.append("今天还有 %d 个" % (len(today) - 5))
            if pending:
                footer.append("%d 个邀请没回复，在“日程”页里看" % len(pending))
            if footer:
                cal_sec["footer"] = "；".join(footer)
            sections.append(cal_sec)
        work = []
        for t in p["todos"][:WORK_MOST]:
            _, due = todo_due(t, tz)
            sub = " · ".join(x for x in (("到期 " + due) if due else "", str(t.get("stage") or "")) if x)
            work.append(row(str(t.get("title") or "待办"), sub, "", key="todo:" + str(t.get("id")),
                            badge={"text": "等你拍板", "tone": "caution"} if t.get("ask") else None,
                            icon="check" if icons else "", icon_color="green" if icons else ""))
        for s in p["working"][:WORK_MOST]:
            work.append(row(agent_title(s), agent_detail(s), str(s.get("agent") or ""), key="work:" + str(s.get("session_id")),
                            icon="robot" if icons else "", icon_color="teal" if icons else ""))
        sections.append({"title": "在推进的事", "items": work or [empty("没有进行中的待办和 Agent", "", version)]})
        if p["missing"]:
            sections.append({"items": [{"type": "text", "style": "caption",
                                        "text": "暂时读不到：%s（稍后自动重试）" % "、".join(p["missing"])}]})
        title = "今天 · %s 周%s" % (now.strftime("%m月%d日").lstrip("0"), WEEKDAYS[now.weekday()])
        return {"title": title, "sections": sections, "refresh_after": REFRESH}

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
        return {}
