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
from openmate_core.ui import (CHATS_PAGE, ChatsToday, Fresh, Page, day_start, done_action, empty, row, stat, state_of,
                              state_text, tile, when)

from openmate_core import agents as agents_mod

from . import layout as layout_mod
from .agenda import PAGE as CALENDAR_PAGE
from .agenda import Agenda, CalendarError, Event

log = logging.getLogger("openmate.today")

# Contribution ids are one namespace across every extension on a hub: a bare "today" kept Kaboo (its card) out.
PAGE = "openmate-today"
CARD = "today-card"
AHEAD = 30.0  # older than this, read again in the background
STALE = 600.0  # older than this, a draw waits for a new read
KEEP = 300.0  # while running, read this often anyway
WAIT = 8.0
REFRESH = 120
NEEDS_MOST = 12
WORK_MOST = 5
CAL_MOST = 5
GROUPS_MOST = 5
SLOW = 0.3  # a draw slower than this says where the time went
WEEKDAYS = "一二三四五六日"
NEEDS_TIERS = ("action", "needs_you")


class Partial(Exception):
    """A read of Persona with parts missing: shown when there is nothing better, never kept."""

    def __init__(self, out: dict):
        super().__init__("parts missing: " + ", ".join(out["missing"]))
        self.out = out


class PersonaToday:
    """Persona's side of today, through the hub API: {"waiting": [...], "working": [...], "inbox": [...],
    "todos": [...], "missing": [sentences]}. A draw shows the last whole read and reads again behind it (Fresh); a read
    with a part missing is shown only when there is no whole one younger than STALE."""

    def __init__(self, app, clock: Callable[[], float] = time.time, spawn: Optional[Callable] = None):
        self.app = app
        self.clock = clock
        self._fresh = Fresh(self._read, AHEAD, STALE, clock=clock, spawn=spawn, name="Persona")

    def _get(self, path: str, query: Optional[dict] = None) -> Any:
        return self.app.hub.request("GET", path, query=query, timeout=WAIT)

    def warm(self) -> None:
        self._fresh.warm(every=KEEP, stop=getattr(self.app, "closing", None))

    def read(self) -> dict:
        try:
            return self._fresh.get()
        except Partial as p:
            return p.out

    def _read(self) -> dict:
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
            raise Partial({"waiting": [], "working": [], "inbox": [], "todos": [], "missing": ["拿不到 Persona hub 的地址或令牌"]})
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
        if out["missing"]:
            raise Partial(out)
        return out


BOOKMARKS_MOST = 30


def bookmarks(config: dict) -> List[Tuple[str, str, str]]:
    """The 书签 setting: lines "标题 | https://… | 说明" (说明 optional); a line without an https link is skipped."""
    out = []
    for line in (config or {}).get("bookmarks") or []:
        parts = [x.strip() for x in str(line).split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1].startswith("https://") or len(parts[1]) > 2000:
            continue
        out.append((parts[0][:60], parts[1], parts[2][:120] if len(parts) > 2 else ""))
    return out[:BOOKMARKS_MOST]


def agent_title(s: dict) -> str:
    return str(s.get("title") or s.get("space_name") or s.get("cwd") or "Agent").strip()


def agent_detail(s: dict) -> str:
    parts = [str(s.get("activity") or s.get("last_text") or "").strip()]
    if s.get("space_name") and s.get("title"):
        parts.append(str(s["space_name"]))
    return " · ".join(p for p in parts if p)


def version_of(d: dict, *fields: str) -> str:
    """What says a Persona item changed (a new question, an update): a marked 处理完 comes back when it differs."""
    return next((str(d[f]) for f in fields if d.get(f)), "")


# kind: (the list in PersonaToday.read, its id field, the fields that change when it needs the user again)
DONE_KINDS = {"wait": ("waiting", "session_id", ("state_since", "updated_at")),
              "inbox": ("inbox", "id", ("updated_at", "created_at", "at")),
              "ask": ("todos", "id", ("updated_at", "created_at"))}


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
                 card: bool = False, mesh=None, agents: Optional[agents_mod.AgentsRead] = None):
        self.app = app
        self.agenda = agenda
        self.persona = persona
        self.chats = chats or ChatsToday(app)
        self.card = card
        self.mesh = mesh  # starkmesh.StarkMeshPage: its block on the page once a key is set
        self.agents = agents  # the Agent progress read: the Agent block sorts by it (else Persona's working / waiting)

    def warm(self) -> None:
        self.agenda.warm()
        self.persona.warm()
        if self.agents is not None:
            self.agents.warm()

    def layout(self) -> layout_mod.Layout:
        return layout_mod.Layout.load(getattr(self.app, "store", None))

    def title(self, now) -> str:
        return "今天 · %s 周%s" % (now.strftime("%m月%d日").lstrip("0"), WEEKDAYS[now.weekday()])

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
        if not self.card and state_of(params).get("edit"):
            return layout_mod.edit_view(self.layout(), version, self.title(now), state_text({"edit": 1}))
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
        if aid.startswith("done:"):
            kind, _, ident = aid[5:].partition(":")
            ok = self.chats.dismiss(ident) if kind == "chat" else self._dismiss(kind, ident)
            toast = "已标记处理完，有新消息会再出现" if kind == "chat" else "已标记处理完，有新变化会再出现"
            return {"toast": toast if ok else "这一件现在没有要处理的了", "view": self.render(params)}
        if aid.startswith("chat:"):
            chat_id = aid[5:]
            return {"push": {"ui": CHATS_PAGE, "state": state_text({"chat": chat_id}), "title": self._chat_title(chat_id)}}
        if aid.startswith("doc:"):
            run = self.app.store.run_get(aid[4:]) if getattr(self.app, "store", None) is not None else None
            return {"open_url": run["doc_url"]} if run and run.get("doc_url") else {"toast": "这条评论的文档链接不在了"}
        if aid.startswith("layout:"):
            return self._layout(params, aid[7:])
        if aid.startswith("agent:"):
            return {"push": {"ui": agents_mod.PAGE, "state": state_text({"session": aid[6:]}), "title": "Agent"}}
        if aid == "go:agents":
            return {"push": {"ui": agents_mod.PAGE, "title": "Agent 进度"}}
        if aid.startswith("url:"):
            for i, (_, url, _) in enumerate(bookmarks(self.app.config)):
                if "url:%d" % i == aid:
                    return {"open_url": url}
            return {"toast": "这个书签已经不在了"}
        if aid == "go:starkmesh":
            return {"push": {"ui": "starkmesh", "title": "中转站 · StarkMesh"}}
        if aid == "go:calendar":
            return {"push": {"ui": CALENDAR_PAGE, "title": "日程"}}
        if aid == "go:chats":
            return {"push": {"ui": CHATS_PAGE, "title": "飞书今天"}}
        return {}

    def _layout(self, params: dict, what: str) -> dict:
        store = getattr(self.app, "store", None)
        if store is None:
            return {"toast": "扩展还在启动，请稍后再试"}
        if what == "edit":
            return {"view": self.render(dict(params, state=state_text({"edit": 1})))}
        if what == "done":
            return {"view": self.render(dict(params, state=""))}
        if what == "reset":
            layout_mod.Layout.default().save(store)
            return {"toast": "已恢复默认布局", "view": self.render(dict(params, state=state_text({"edit": 1})))}
        change, _, block = what.partition(":")
        lay = self.layout()
        toast = lay.change(change, block)
        if toast:
            lay.save(store)
        view = self.render(dict(params, state=state_text({"edit": 1})))
        return {"toast": toast, "view": view} if toast else {"view": view}

    def _dismiss(self, kind: str, ident: str) -> bool:
        store = getattr(self.app, "store", None)
        if kind not in DONE_KINDS or store is None:
            return False
        part, id_field, fields = DONE_KINDS[kind]
        for d in self.persona.read()[part]:
            if str(d.get(id_field)) == ident:
                store.dismiss("%s:%s" % (kind, ident), version_of(d, *fields))
                return True
        return False

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
        self.v8 = version >= 8  # a page of cards side by side (layout columns), with icons and links on the cards
        self.current = [e for e in today if not e.all_day and e.start <= now]
        self.upcoming = [e for e in today if not e.all_day and e.start > now]
        self.inbox_needs = [i for i in p["inbox"] if str(i.get("tier") or "") in NEEDS_TIERS]
        self.asks = [t for t in p["todos"] if t.get("ask")]
        self.todos = [t for t in p["todos"] if not t.get("ask")]
        self.done = len(getattr(page.chats, "done", []) or [])
        store = getattr(page.app, "store", None)
        if store is not None:
            def keep(kind: str, items: List[dict]) -> List[dict]:
                _, id_field, fields = DONE_KINDS[kind]
                keyed = [("%s:%s" % (kind, d.get(id_field)), version_of(d, *fields), d) for d in items]
                marked = store.dismissed(k for k, _, _ in keyed) if keyed else {}
                kept = [d for k, v, d in keyed if marked.get(k) != v]
                self.done += len(items) - len(kept)
                return kept
            p = dict(p, waiting=keep("wait", p["waiting"]))
            self.p = p
            self.inbox_needs = keep("inbox", self.inbox_needs)
            self.asks = keep("ask", self.asks)
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

    def _card(self, sec: Dict[str, Any], icon: str, color: str, link: Optional[Tuple[str, str]] = None) -> Dict[str, Any]:
        """A card's icon and its link beside the title (UI version 8)."""
        if self.v8:
            sec["icon"], sec["icon_color"] = icon, color
            if link is not None:
                sec["link"] = {"title": link[0], "action": {"id": link[1]}}
        return sec

    def _todo_row(self, t: dict, key: str) -> dict:
        _, due = todo_due(t, self.page.app.tz)
        sub = " · ".join(x for x in (("到期 " + due) if due else "", str(t.get("stage") or "")) if x)
        return row(str(t.get("title") or "待办"), sub, "", key=key + str(t.get("id")),
                   badge={"text": "等你拍板", "tone": "caution"} if t.get("ask") else None, **self._icon("check", "green"),
                   actions=self._done("ask:" + str(t.get("id"))) if t.get("ask") else None)

    def _done(self, key: str) -> Optional[List[dict]]:
        return [done_action(key)] if self.icons else None  # a row's menu came with UI version 6

    def needs_rows(self) -> List[dict]:
        out = []
        for s in self.p["waiting"]:
            out.append(row(agent_title(s), agent_detail(s) or "在等你回答", "", key="wait:" + str(s.get("session_id")),
                           badge={"text": "Agent 等你", "tone": "danger"}, **self._icon("robot", "red"),
                           actions=self._done("wait:" + str(s.get("session_id")))))
        for i in self.inbox_needs:
            out.append(row(str(i.get("title") or "收件箱"), str(i.get("summary") or i.get("preview") or ""), "",
                           key="inbox:" + str(i.get("id")), badge={"text": "收件箱", "tone": "caution"},
                           **self._icon("mail", "amber"), actions=self._done("inbox:" + str(i.get("id")))))
        for t in self.asks:
            out.append(self._todo_row(t, "ask:"))
        for t in self.chat_needs:
            mention = t["kind"] == "mention"
            chat_id = str((t.get("_chat") or {}).get("id") or t.get("thread") or "")
            at = when(t.get("_at") or 0, self.page.app.tz, self.now.timestamp()) if t.get("_at") else ""
            out.append(row(t["title"], "%s：%s" % (t["sender"], t["preview"]), at,
                           "chat:" + chat_id if self.links and chat_id else "", key="chat:" + str(t.get("thread") or chat_id),
                           badge={"text": "@我" if mention else "私聊", "tone": "caution" if mention else "neutral"},
                           **self._icon("chat", "orange" if mention else "indigo"),
                           actions=self._done("chat:" + chat_id) if chat_id else None))
        return out

    # ---- the card

    def card(self) -> dict:
        v = self.version
        head = self._tiles(self.status_blocks(), "blocks")
        rows = [{k: x for k, x in r.items() if k not in ("action", "actions")} for r in self.needs_rows()[:3]]  # a card only shows
        return {"sections": [{"items": [head]}, {"items": rows or [empty("没有要你处理的事", "", v)]}], "refresh_after": REFRESH}

    # ---- the page

    def full(self) -> dict:
        lay = self.page.layout()
        build = {"status": self.status_block, "needs": self.needs_card, "calendar": self.calendar_card,
                 "feishu": self.feishu_card, "agents": self.agents_block, "mesh": self.mesh_card, "auto": self.auto_card,
                 "todos": self.todos_card, "shortcuts": self.shortcuts_block, "bookmarks": self.bookmarks_block}
        sections = []
        for b in lay.shown():
            sec = build[b]()
            if sec is None:
                continue
            if self.v8:  # a whole row or one of two (layout columns); below 8 every section is a whole row
                if lay.is_wide(b):
                    sec["span"] = "full"
                else:
                    sec.pop("span", None)
                    sec.pop("columns", None)
            sections.append(sec)
        if self.p["missing"]:
            note: Dict[str, Any] = {"items": [self._caption("暂时读不到：%s（稍后自动重试）" % "、".join(self.p["missing"]))]}
            if self.v8:
                note["span"] = "full"
            sections.append(note)
        tail: Dict[str, Any] = {"items": [{"type": "button", "title": "调整布局", "action": {"id": "layout:edit"}, "key": "layout"}]}
        if self.v8:
            tail["span"] = "full"
        sections.append(tail)
        view = {"title": self.page.title(self.now), "sections": sections, "refresh_after": REFRESH, "state": ""}
        if self.v8:
            view["layout"] = "columns"  # the blocks the user made whole rows across, the others two by two
        return view

    # ---- the blocks only the page has

    def status_blocks(self, mesh: bool = False) -> List[tuple]:
        """The status tiles: (title, value, line, icon, color, badge, key), each with a pill saying how it stands."""
        p = self.p
        unread = self.counts["mentions"] + self.counts["dms"]
        ok = {"text": "正常", "tone": "positive"}
        out = [
            ("需要我", str(self.needs_total), "飞书 %d · 收件箱 %d · Agent %d" % (len(self.chat_needs), len(self.inbox_needs), len(p["waiting"])),
             "bell", "red" if self.needs_total else "gray",
             {"text": "待处理", "tone": "danger"} if self.needs_total else {"text": "清空", "tone": "positive"}, "b:needs"),
            ("日程", "—" if self.cal_missing else str(len(self.today)), self.cal_line, "calendar", "blue",
             {"text": "读不到", "tone": "caution"} if self.cal_missing else
             {"text": "待回复 %d" % len(self.pending), "tone": "caution"} if self.pending else ok, "b:calendar"),
            ("飞书", str(unread), "@我 %d · 私聊 %d · 今天 %d 条" % (self.counts["mentions"], self.counts["dms"], self.counts["messages"]),
             "chat", "indigo", {"text": "@我 %d" % self.counts["mentions"], "tone": "caution"} if self.counts["mentions"] else ok, "b:feishu"),
            ("Agent", str(len(p["working"])), "在跑 · 等你 %d" % len(p["waiting"]) if p["waiting"] else "在跑 · 没有在等你的",
             "robot", "teal", {"text": "等你 %d" % len(p["waiting"]), "tone": "danger"} if p["waiting"] else ok, "b:agents"),
        ]
        page = getattr(self.page, "mesh", None)
        if mesh and page is not None:
            t = page.status_tile()
            if t is not None:
                out.append(t)
        return out

    def _tiles(self, blocks: List[tuple], key: str) -> dict:
        if self.version >= 6:
            return {"type": "grid", "max_columns": len(blocks), "key": key,
                    "tiles": [tile(t, val, line, icon, color, k, badge) for t, val, line, icon, color, badge, k in blocks]}
        if self.version >= 2:
            return {"type": "stat_grid", "stats": [stat(t, val, line, (badge or {}).get("tone") if (badge or {}).get("tone") in ("caution", "danger") else "")
                                                    for t, val, line, _, _, badge, _ in blocks][:6]}
        return {"type": "text", "text": " · ".join("%s %s" % (t, val) for t, val, *_ in blocks)}

    def status_block(self) -> dict:
        return {"items": [self._tiles(self.status_blocks(mesh=True), "status")]}

    def agents_block(self) -> dict:
        """等你, maybe stuck and working Agents, from the Agent progress read (else Persona's working list)."""
        read = getattr(self.page, "agents", None)
        if read is None:
            return self.agents_card()
        try:
            board = agents_mod.Board(read.read(), self.now.timestamp(), self.page.app.tz)
        except Exception as e:
            log.info("the Agent block could not be read: %s", e.__class__.__name__)
            return self.agents_card()
        parts = ["%s %d" % (t, board.count(k)) for k, t, *_ in agents_mod.GROUPS if board.count(k)]
        rows = []
        for k in ("needs", "stuck", "working"):
            for s in board.groups[k]:
                if len(rows) < WORK_MOST:
                    rows.append(agents_mod.AgentsPage.row_of(board, s, k, self.version, self.links))
        busy = board.count("needs") + board.count("stuck") + board.count("working")
        sec: Dict[str, Any] = {"title": "Agent · %d" % busy if busy else "Agent",
                               "items": [self._caption(" · ".join(parts) or "今天还没有 Agent 做过事")]
                               + (rows or [empty("没有等你或在跑的 Agent", "", self.version)])}
        if busy > WORK_MOST:
            sec["footer"] = "还有 %d 个" % (busy - WORK_MOST)
        if not self.v8:
            sec["items"] += self._link("打开 Agent 进度 ›", "go:agents", "link:agents")
        return self._card(sec, "robot", "teal", ("全部", "go:agents"))

    SHORTCUTS = (("飞书", "@我、私聊和今天活跃的群", "chat", "indigo", "go:chats"),
                 ("日程", "今天和这周的会", "calendar", "blue", "go:calendar"),
                 ("中转站", "各账号额度和重置时间", "gauge", "teal", "go:starkmesh"),
                 ("Agent 进度", "所有电脑上的 Agent", "robot", "purple", "go:agents"))

    def shortcuts_block(self) -> Optional[dict]:
        if not self.links:
            return None  # push came with UI version 3
        items = [x for x in self.SHORTCUTS if x[4] != "go:starkmesh" or getattr(self.page, "mesh", None) is not None]
        if self.icons:
            body = {"type": "grid", "max_columns": len(items), "key": "shortcuts",
                    "tiles": [tile(t, "", sub, icon, color, "sc:" + a, None, a) for t, sub, icon, color, a in items]}
            return self._card({"title": "快捷入口", "items": [body]}, "link", "gray")
        return {"title": "快捷入口", "items": [row(t + " ›", sub, "", a, key="sc:" + a) for t, sub, _, _, a in items]}

    def bookmarks_block(self) -> Optional[dict]:
        marks = bookmarks(self.page.app.config)
        if not marks:
            return None
        rows = [row(t, about or url, "", "url:%d" % i, key="bm:%d" % i, **self._icon("link", "blue"))
                for i, (t, url, about) in enumerate(marks)]
        return self._card({"title": "书签", "items": rows}, "star", "amber")

    def needs_card(self) -> dict:
        p = self.p
        parts = ["飞书 %d" % len(self.chat_needs), "收件箱 %d" % len(self.inbox_needs), "Agent 等你 %d" % len(p["waiting"])]
        if self.asks:
            parts.append("待办等你拍板 %d" % len(self.asks))
        if self.done:
            parts.append("已处理 %d" % self.done)
        rows = self.needs_rows()
        sec: Dict[str, Any] = {"title": "需要我 · %d" % self.needs_total if self.needs_total else "需要我",
                               "items": [self._caption(" · ".join(parts))]
                               + (rows[:NEEDS_MOST] or [empty("没有要你处理的事", "", self.version)])}
        if len(rows) > NEEDS_MOST:
            sec["footer"] = "还有 %d 件" % (len(rows) - NEEDS_MOST)
        if self.v8:
            sec["span"], sec["columns"] = "full", 2
            return self._card(sec, "bell", "red" if self.needs_total else "gray", ("全部", "go:chats") if self.chat_needs else None)
        if self.chat_needs:
            sec["items"] += self._link("在“飞书今天”里看全部私聊和 @我 ›", "go:chats", "link:needs")
        return sec

    def calendar_card(self) -> dict:
        v = self.version
        if self.cal_missing:
            return self._card({"title": "日程", "items": [empty("日程读不到", self.cal_missing, v)]}, "calendar", "blue")
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
        if self.later or not self.v8:
            more = "本周还有 %d 个 ›" % self.later if self.later else "打开日程 ›"
            sec["items"] += self._link(more, "go:calendar", "link:calendar",
                                       "待回复 %d" % len(self.pending) if self.pending else "")
        return self._card(sec, "calendar", "blue", ("日历", "go:calendar"))

    def feishu_card(self) -> dict:
        app, c = self.page.app, self.counts
        line = "@我 %d · 私聊 %d · 今天收到 %d 条，%d 个会话" % (c["mentions"], c["dms"], c["messages"], c["chats"])
        if c.get("bots"):
            line += " · 机器人通知 %d" % c["bots"]
        rows = []
        for chat, last in self.groups[:GROUPS_MOST]:
            rows.append(row(views.chat_name(chat, chat["id"]),
                            "%s：%s" % (views.sender_of(last, app.me_name()), views.text_of(last)),
                            when(last.get("at") or 0, app.tz, self.now.timestamp()), "chat:" + chat["id"] if self.links else "",
                            key="group:" + chat["id"], **self._icon("chat", "indigo")))
        sec: Dict[str, Any] = {"title": "飞书今天", "items": [self._caption(line)]
                               + (rows or [empty("今天群里还没有新消息", "", self.version)])}
        if not self.v8:
            sec["items"] += self._link("打开飞书今天 ›", "go:chats", "link:feishu")
        return self._card(sec, "chat", "indigo", ("飞书", "go:chats"))

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
        return self._card(sec, "robot", "teal")

    def mesh_card(self) -> Optional[dict]:
        page = getattr(self.page, "mesh", None)
        if page is None:
            return None
        sec = page.card(self.version, self.links, self.icons)
        return self._card(sec, "gauge", "teal", ("打开", "go:starkmesh")) if sec is not None else None

    def auto_card(self) -> Optional[dict]:
        """What the Agent answered as the user today without being asked each time (自动代答 in a marked group, a
        document's comment): to look over."""
        store = getattr(self.page.app, "store", None)
        if store is None:
            return None
        since = day_start(self.page.app.tz, self.now.timestamp())
        runs = [r for r in store.runs_since(since, 100) if r.get("kind") in ("auto", "doc")]
        if not runs:
            return None
        app = self.page.app
        rows = []
        for r in runs[:WORK_MOST]:
            replied = int(r.get("replies") or 0) > 0
            if replied:
                badge = {"text": "已代答", "tone": "positive"}
            elif r["state"] in ("starting", "running"):
                badge = {"text": "进行中", "tone": "neutral"}
            else:
                badge = {"text": "没有代答", "tone": "caution"}
            if r.get("kind") == "doc":
                title, action = str(r.get("doc_title") or "云文档评论"), ("doc:" + r["id"]) if r.get("doc_url") else ""
            else:
                chat = store.get_chat(r["chat_id"])
                title, action = views.chat_name(chat, r["chat_id"]), ("chat:" + r["chat_id"]) if self.links else ""
            sub = "%s：%s" % (r.get("asker") or "有人", r.get("request_text") or "（只 @ 了你）")
            rows.append(row(title, sub, when(r["created"], app.tz, self.now.timestamp()), action, key="auto:" + r["id"], badge=badge,
                            **self._icon("robot", "purple")))
        answered = sum(1 for r in runs if int(r.get("replies") or 0) > 0)
        sec: Dict[str, Any] = {"title": "AI 代答 · %d" % len(runs),
                               "items": [self._caption("今天代答 %d 次，没有代答 %d 次" % (answered, len(runs) - answered))] + rows}
        if len(runs) > WORK_MOST:
            sec["footer"] = "还有 %d 次" % (len(runs) - WORK_MOST)
        return self._card(sec, "robot", "purple", ("设置", "go:chats"))

    def todos_card(self) -> Optional[dict]:
        if not self.todos:
            return None
        line = "未完成 %d" % len(self.p["todos"])
        line += " · 今天到期 %d" % len(self.due_today) if self.due_today else " · 今天没有到期的"
        sec: Dict[str, Any] = {"title": "待办 · %d" % len(self.todos),
                               "items": [self._caption(line)] + [self._todo_row(t, "todo:") for t in self.todos[:WORK_MOST]]}
        if len(self.todos) > WORK_MOST:
            sec["footer"] = "还有 %d 个" % (len(self.todos) - WORK_MOST)
        return self._card(sec, "check", "green")
