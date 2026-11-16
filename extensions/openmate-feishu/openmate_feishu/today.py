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
from openmate_core import views
from openmate_core.ui import CHATS_PAGE, ChatsToday, Page, day_start, empty, row, stat, state_text, tile, when

from .agenda import PAGE as CALENDAR_PAGE
from .agenda import Agenda, CalendarError, Event

log = logging.getLogger("openmate.today")

PAGE = "today"
CARD = "today-card"
CACHE = 60.0
WAIT = 8.0
REFRESH = 120
NEEDS_MOST = 12
WORK_MOST = 5
CAL_MOST = 5
GROUPS_MOST = 5
SLOW = 0.3  # a draw slower than this says where the time went
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

    def _calendar(self, now) -> Tuple[List[Event], List[Event], int, str]:
        """(today's events still to come or on now, invitations waiting for an answer, how many in the next six days,
        why it is missing or "")."""
        try:
            events = self.agenda.events()
        except CalendarError as e:
            return [], [], 0, e.message
        tomorrow = _dt.datetime.fromtimestamp(day_start(self.app.tz, now.timestamp()), self.app.tz) + _dt.timedelta(days=1)
        today = [e for e in events if e.start < tomorrow and (e.all_day or (e.end or e.start) > now)]
        pending = [e for e in events if e.rsvp == "needs_action"]
        return today, pending, sum(1 for e in events if e.start >= tomorrow), ""

    def render(self, params: dict) -> dict:
        version = int(params.get("ui_version") or 1)
        tz = self.app.tz
        now = _dt.datetime.fromtimestamp(self.agenda.clock(), tz)
        t0 = time.monotonic()
        p = self.persona.read()
        t1 = time.monotonic()
        chat_needs = self._safe(self.chats.needs, [])
        counts = self._safe(lambda: self.chats.counts(chat_needs), None) or {
            "mentions": sum(1 for t in chat_needs if t["kind"] == "mention"),
            "dms": sum(1 for t in chat_needs if t["kind"] == "dm"), "messages": 0, "chats": 0}
        groups = [] if self.card else self._safe(self.chats.active_groups, [])
        t2 = time.monotonic()
        today, pending, later, cal_missing = self._calendar(now)
        t3 = time.monotonic()
        if t3 - t0 > SLOW:
            log.info("%s took %dms: Persona %dms, chats %dms, calendar %dms", CARD if self.card else PAGE,
                     (t3 - t0) * 1000, (t1 - t0) * 1000, (t2 - t1) * 1000, (t3 - t2) * 1000)
        d = Day(self, version, now, p, chat_needs, counts, groups, today, pending, later, cal_missing)
        return d.card() if self.card else d.full()

    @staticmethod
    def _safe(fn, default):
        try:
            return fn()
        except Exception:
            log.exception("a part of today could not be read")
            return default

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
            return {"toast": "这个日程已经不在日历里了"}
        if aid.startswith("chat:"):
            chat_id = aid[5:]
            return {"push": {"ui": CHATS_PAGE, "state": state_text({"chat": chat_id}), "title": self._chat_title(chat_id)}}
        if aid == "go:calendar":
            return {"push": {"ui": CALENDAR_PAGE, "title": "日程"}}
        if aid == "go:chats":
            return {"push": {"ui": CHATS_PAGE, "title": "飞书今天"}}
        return {}

    def _chat_title(self, chat_id: str) -> str:
        store = getattr(self.app, "store", None)
        try:
            chat = store.get_chat(chat_id) if store is not None else None
        except Exception:
            chat = None
        return views.chat_name(chat, chat_id) if chat else "会话"


class Day:
    """One draw of today: the card (tiles and the first things that need the user) or the page (cards of their own:
    需要我 across, then 日程 | 飞书今天, 正在跑的 Agent | 待办 once the apps lay sections out in columns)."""

    def __init__(self, page: TodayPage, version, now, p, chat_needs, counts, groups, today, pending, later, cal_missing):
        self.page, self.version, self.now, self.p = page, version, now, p
        self.chat_needs, self.counts, self.groups = chat_needs, counts, groups
        self.today, self.pending, self.later, self.cal_missing = today, pending, later, cal_missing
        tz = page.app.tz
        self.icons = version >= 6
        self.links = version >= 3  # push (another page on top) came with UI version 3
        self.current = [e for e in today if not e.all_day and e.start <= now]
        self.upcoming = [e for e in today if not e.all_day and e.start > now]
        self.inbox_needs = [i for i in p["inbox"] if str(i.get("tier") or "") in NEEDS_TIERS]
        self.asks = [t for t in p["todos"] if t.get("ask")]
        self.todos = [t for t in p["todos"] if not t.get("ask")]
        end_of_day = _dt.datetime.fromtimestamp(day_start(tz, now.timestamp()), tz) + _dt.timedelta(days=1)
        self.due_today = [t for t in p["todos"] if (todo_due(t, tz)[0] or end_of_day) < end_of_day]
        self.needs_total = len(p["waiting"]) + len(self.inbox_needs) + len(chat_needs) + len(self.asks)
        if cal_missing:
            self.cal_line = "读不到"
        elif self.current:
            self.cal_line = "会议中，%s 结束" % max((e.end or e.start) for e in self.current).strftime("%H:%M")
        elif self.upcoming:
            self.cal_line = "下一个 %s · %s" % (self.upcoming[0].start.strftime("%H:%M"), self.upcoming[0].title)
        else:
            self.cal_line = "今天没有了"

    # ---- pieces

    def _icon(self, name: str, color: str) -> dict:
        return {"icon": name, "icon_color": color} if self.icons else {}

    def _caption(self, text: str) -> dict:
        return {"type": "text", "style": "caption", "text": text}

    def _link(self, title: str, action: str, key: str, value: str = "") -> List[dict]:
        return [row(title, "", value, action, key=key)] if self.links else []

    def _todo_row(self, t: dict, key: str) -> dict:
        _, due = todo_due(t, self.page.app.tz)
        sub = " · ".join(x for x in (("到期 " + due) if due else "", str(t.get("stage") or "")) if x)
        return row(str(t.get("title") or "待办"), sub, "", key=key + str(t.get("id")),
                   badge={"text": "等你拍板", "tone": "caution"} if t.get("ask") else None, **self._icon("check", "green"))

    def needs_rows(self) -> List[dict]:
        out = []
        for s in self.p["waiting"]:
            out.append(row(agent_title(s), agent_detail(s) or "在等你回答", "", key="wait:" + str(s.get("session_id")),
                           badge={"text": "Agent 等你", "tone": "danger"}, **self._icon("robot", "red")))
        for i in self.inbox_needs:
            out.append(row(str(i.get("title") or "收件箱"), str(i.get("summary") or i.get("preview") or ""), "",
                           key="inbox:" + str(i.get("id")), badge={"text": "收件箱", "tone": "caution"},
                           **self._icon("mail", "amber")))
        for t in self.asks:
            out.append(self._todo_row(t, "ask:"))
        for t in self.chat_needs:
            mention = t["kind"] == "mention"
            chat_id = str((t.get("_chat") or {}).get("id") or t.get("thread") or "")
            at = when(t.get("_at") or 0, self.page.app.tz) if t.get("_at") else ""
            out.append(row(t["title"], "%s：%s" % (t["sender"], t["preview"]), at,
                           "chat:" + chat_id if self.links and chat_id else "", key="chat:" + str(t.get("thread") or chat_id),
                           badge={"text": "@我" if mention else "私聊", "tone": "caution" if mention else "neutral"},
                           **self._icon("chat", "orange" if mention else "indigo")))
        return out

    # ---- the card

    def card(self) -> dict:
        v, p = self.version, self.p
        blocks = [
            # (title, value, line, icon, color, badge, key)
            ("需要我", str(self.needs_total), "飞书 %d · 收件箱 %d · Agent %d" % (len(self.chat_needs), len(self.inbox_needs), len(p["waiting"])),
             "bell", "red" if self.needs_total else "gray", None, "b:needs"),
            ("日程", "—" if self.cal_missing else "%d 个" % len(self.today), self.cal_line, "calendar", "blue",
             {"text": "待回复 %d" % len(self.pending), "tone": "caution"} if self.pending else None, "b:calendar"),
            ("飞书", "@我 %d · 私聊 %d" % (self.counts["mentions"], self.counts["dms"]),
             "今天收到 %d 条 · %d 个会话" % (self.counts["messages"], self.counts["chats"]), "chat", "indigo", None, "b:feishu"),
            ("Agent", "%d 在跑" % len(p["working"]), "等你 %d" % len(p["waiting"]) if p["waiting"] else "没有在等你的",
             "robot", "teal", {"text": "等你 %d" % len(p["waiting"]), "tone": "danger"} if p["waiting"] else None, "b:agents"),
        ]
        if v >= 6:
            head = {"type": "grid", "max_columns": 4, "key": "blocks",
                    "tiles": [tile(t, val, line, icon, color, key, badge) for t, val, line, icon, color, badge, key in blocks]}
        elif v >= 2:
            head = {"type": "stat_grid", "stats": [stat(t, val, line, "caution" if badge else "") for t, val, line, _, _, badge, _ in blocks]}
        else:
            head = {"type": "text", "text": " · ".join("%s %s" % (t, val) for t, val, *_ in blocks)}
        rows = [{k: x for k, x in r.items() if k != "action"} for r in self.needs_rows()[:3]]  # a card only shows
        return {"sections": [{"items": [head]}, {"items": rows or [empty("没有要你处理的事", "", v)]}], "refresh_after": REFRESH}

    # ---- the page

    def full(self) -> dict:
        sections = [self.needs_card(), self.calendar_card(), self.feishu_card(), self.agents_card()]
        todos = self.todos_card()
        if todos is not None:
            sections.append(todos)
        if self.p["missing"]:
            sections.append({"items": [self._caption("暂时读不到：%s（稍后自动重试）" % "、".join(self.p["missing"]))]})
        title = "今天 · %s 周%s" % (self.now.strftime("%m月%d日").lstrip("0"), WEEKDAYS[self.now.weekday()])
        return {"title": title, "sections": sections, "refresh_after": REFRESH}

    def needs_card(self) -> dict:
        p = self.p
        parts = ["飞书 %d" % len(self.chat_needs), "收件箱 %d" % len(self.inbox_needs), "Agent 等你 %d" % len(p["waiting"])]
        if self.asks:
            parts.append("待办等你拍板 %d" % len(self.asks))
        rows = self.needs_rows()
        sec: Dict[str, Any] = {"title": "需要我 · %d" % self.needs_total if self.needs_total else "需要我",
                               "items": [self._caption(" · ".join(parts))]
                               + (rows[:NEEDS_MOST] or [empty("没有要你处理的事", "", self.version)])}
        if len(rows) > NEEDS_MOST:
            sec["footer"] = "还有 %d 件" % (len(rows) - NEEDS_MOST)
        if self.chat_needs:
            sec["items"] += self._link("在“飞书今天”里看全部私聊和 @我 ›", "go:chats", "link:needs")
        return sec

    def calendar_card(self) -> dict:
        v = self.version
        if self.cal_missing:
            return {"title": "日程", "items": [empty("日程读不到", self.cal_missing, v)]}
        parts = ["今天 %d 个" % len(self.today), self.cal_line]
        if self.pending:
            parts.append("%d 个邀请没回复" % len(self.pending))
        rows = [row(e.title, e.detail(), e.span(False), "open:" + e.id if e.id else "", key="ev:" + e.id,
                    badge={"text": "进行中", "tone": "positive"} if e in self.current else e.badge(),
                    **self._icon("clock", "green" if e in self.current else "blue"))
                for e in self.today[:CAL_MOST]]
        sec: Dict[str, Any] = {"title": "日程", "items": [self._caption(" · ".join(parts))]
                               + (rows or [empty("今天没有日程了", "", v)])}
        if len(self.today) > CAL_MOST:
            sec["footer"] = "今天还有 %d 个" % (len(self.today) - CAL_MOST)
        more = "本周还有 %d 个 ›" % self.later if self.later else "打开日程 ›"
        sec["items"] += self._link(more, "go:calendar", "link:calendar",
                                   "待回复 %d" % len(self.pending) if self.pending else "")
        return sec

    def feishu_card(self) -> dict:
        app, c = self.page.app, self.counts
        line = "@我 %d · 私聊 %d · 今天收到 %d 条，%d 个会话" % (c["mentions"], c["dms"], c["messages"], c["chats"])
        rows = []
        for chat, last in self.groups[:GROUPS_MOST]:
            rows.append(row(views.chat_name(chat, chat["id"]),
                            "%s：%s" % (views.sender_of(last, app.me_name()), views.text_of(last)),
                            when(last.get("at") or 0, app.tz), "chat:" + chat["id"] if self.links else "",
                            key="group:" + chat["id"], **self._icon("chat", "indigo")))
        sec: Dict[str, Any] = {"title": "飞书今天", "items": [self._caption(line)]
                               + (rows or [empty("今天群里还没有新消息", "", self.version)])}
        sec["items"] += self._link("打开飞书今天 ›", "go:chats", "link:feishu")
        return sec

    def agents_card(self) -> dict:
        p = self.p
        line = "在跑 %d" % len(p["working"])
        if p["waiting"]:
            line += " · 等你 %d（在“需要我”里）" % len(p["waiting"])
        rows = [row(agent_title(s), agent_detail(s), str(s.get("agent") or ""), key="work:" + str(s.get("session_id")),
                    **self._icon("robot", "teal"))
                for s in p["working"][:WORK_MOST]]
        sec: Dict[str, Any] = {"title": "正在跑的 Agent · %d" % len(p["working"]) if p["working"] else "正在跑的 Agent",
                               "items": [self._caption(line)] + (rows or [empty("现在没有在跑的 Agent", "", self.version)])}
        if len(p["working"]) > WORK_MOST:
            sec["footer"] = "还有 %d 个" % (len(p["working"]) - WORK_MOST)
        return sec

    def todos_card(self) -> Optional[dict]:
        if not self.todos:
            return None
        line = "未完成 %d" % len(self.p["todos"])
        line += " · 今天到期 %d" % len(self.due_today) if self.due_today else " · 今天没有到期的"
        sec: Dict[str, Any] = {"title": "待办 · %d" % len(self.todos),
                               "items": [self._caption(line)] + [self._todo_row(t, "todo:") for t in self.todos[:WORK_MOST]]}
        if len(self.todos) > WORK_MOST:
            sec["footer"] = "还有 %d 个" % (len(self.todos) - WORK_MOST)
        return sec
