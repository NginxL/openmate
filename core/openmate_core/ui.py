"""The extension's places in the Persona apps (docs/extensions/extensions.md "Declarative UI", "UI slots"): a page in
the main navigation with the chats that need the user, and a card on Overview. A platform adds its own (the Feishu
calendar) through Platform.ui_pages. Everything here reads the local store only: drawing never waits on the service.

ui.render {contribution, state, size, tz, …} → View; ui.action {…, action, values} → {view?, toast?, open_url?}.
The page's badge (ui.badge) is how many chats need the user; it is sent again whenever the inbox changes.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from . import views
from .textutil import one_line
from .wire import RPCError

log = logging.getLogger("openmate.ui")

CHATS_PAGE = "chats"
CHATS_CARD = "chats-today"
NEEDS_MOST = 30
GROUPS_MOST = 10
THREAD_LINES = 30
REFRESH = 60


class Page:
    """One contribution: render(params) → View; action(params) → {view?, toast?, open_url?}."""

    def render(self, params: dict) -> dict:
        raise NotImplementedError

    def action(self, params: dict) -> dict:
        return {}


def stat(label: str, value, detail: str = "", tone: str = "") -> dict:
    out = {"label": label, "value": str(value)}
    if detail:
        out["detail"] = detail
    if tone:
        out["tone"] = tone
    return out


def row(title: str, subtitle: str = "", value: str = "", action: str = "", key: str = "", badge: Optional[dict] = None) -> dict:
    out: Dict[str, Any] = {"type": "row", "title": one_line(title or "（无标题）", 120)}
    if subtitle:
        out["subtitle"] = one_line(subtitle, 200)
    if value:
        out["value"] = value
    if badge:
        out["badge"] = badge
    if action:
        out["action"] = {"id": action}
    if key:
        out["key"] = key
    return out


def empty(title: str, detail: str = "", version: int = 3) -> dict:
    if version >= 3:
        out = {"type": "empty", "title": title}
        if detail:
            out["detail"] = detail
        return out
    return {"type": "text", "text": title + ("：" + detail if detail else ""), "style": "secondary"}


def when(ts: float, tz, now: Optional[float] = None) -> str:
    """"14:35" today, "昨天 14:35", else "11-15"."""
    if not ts:
        return ""
    now = now or time.time()
    d = _dt.datetime.fromtimestamp(ts, tz)
    today = _dt.datetime.fromtimestamp(now, tz).date()
    if d.date() == today:
        return d.strftime("%H:%M")
    if d.date() == today - _dt.timedelta(days=1):
        return "昨天 " + d.strftime("%H:%M")
    return d.strftime("%m-%d")


def state_of(params: dict) -> dict:
    """The page's state as the app sends it back: a string (JSON) the extension wrote; {} when none or unreadable."""
    raw = params.get("state")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except ValueError:
            return {}
    return {}


def state_text(state: dict) -> str:
    return json.dumps(state, ensure_ascii=False, separators=(",", ":")) if state else ""


def day_start(tz, now: Optional[float] = None) -> float:
    d = _dt.datetime.fromtimestamp(now or time.time(), tz)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class ChatsToday:
    """What needs the user in the chats, from the store: unanswered direct chats and @me, today's traffic."""

    def __init__(self, app):
        self.app = app

    def needs(self) -> List[dict]:
        """Direct chats and @me the user has not answered (the inbox's rule), newest first."""
        app = self.app
        out = []
        if app.store is None:
            return out
        for chat in app.store.chats_since(time.time() - app.timing.inbox_window, app.timing.inbox_most):
            rows = app.store.messages_before(chat["id"], None, 60)
            thing = views.inbox_thing(chat, rows, platform=app.platform.name, tz=app.tz, me=app.me_name(), rules="",
                                      can_reply=False)
            if thing is None or thing.get("done") or thing["kind"] not in ("dm", "mention"):
                continue
            thing["_at"] = max((r.get("at") or 0) for r in rows) if rows else 0
            thing["_chat"] = chat
            out.append(thing)
        out.sort(key=lambda t: -t["_at"])
        return out

    def counts(self, needs: List[dict]) -> Dict[str, int]:
        app = self.app
        start = day_start(app.tz)
        today = app.store.count_since(start) if app.store is not None else {"messages": 0, "chats": 0}
        return {"needs": len(needs), "mentions": sum(1 for t in needs if t["kind"] == "mention"),
                "dms": sum(1 for t in needs if t["kind"] == "dm"), "messages": today["messages"], "chats": today["chats"]}

    def active_groups(self) -> List[dict]:
        app = self.app
        if app.store is None:
            return []
        start = day_start(app.tz)
        out = []
        for chat in app.store.chats_since(start, 50):
            if views.chat_kind(chat) == "dm":
                continue
            rows = [r for r in app.store.messages_before(chat["id"], None, 5) if views.has_content(r) and not views.is_system(r)]
            if rows:
                out.append((chat, rows[-1]))
            if len(out) >= GROUPS_MOST:
                break
        return out


class ChatsPage(Page):
    def __init__(self, app, card: bool = False):
        self.app = app
        self.card = card
        self.data = ChatsToday(app)

    def render(self, params: dict) -> dict:
        state = state_of(params)
        if not self.card and state.get("chat"):
            return self._chat(str(state["chat"]), int(params.get("ui_version") or 1))
        return self._overview(int(params.get("ui_version") or 1))

    def action(self, params: dict) -> dict:
        aid = str((params.get("action") or {}).get("id") if isinstance(params.get("action"), dict) else params.get("action") or "")
        version = int(params.get("ui_version") or 1)
        if aid.startswith("chat:"):
            return {"view": self._chat(aid[5:], version)}
        if aid == "back":
            return {"view": self._overview(version)}
        return {}

    def _summary(self, c: Dict[str, int], version: int) -> dict:
        stats = [stat("需要我", c["needs"], tone="caution" if c["needs"] else ""), stat("@我", c["mentions"]),
                 stat("私聊未回", c["dms"]), stat("今天收到", c["messages"], "%d 个会话" % c["chats"])]
        if version >= 2:
            return {"type": "stat_grid", "stats": stats[:3] if self.card else stats}
        return {"type": "text", "text": " · ".join("%s %s" % (s["label"], s["value"]) for s in stats[:3])}

    def _overview(self, version: int) -> dict:
        app = self.app
        p = app.platform.name
        if app.store is None or not app.signed_in and app.creds != "present":
            return {"sections": [{"items": [empty("还没有登录%s" % p, "在 设置 › 连接器 里登录后就能看到", version)]}]}
        needs = self.data.needs()
        c = self.data.counts(needs)
        rows = []
        for t in needs[: 3 if self.card else NEEDS_MOST]:
            chat = t["_chat"]
            label = "@我" if t["kind"] == "mention" else "私聊"
            rows.append(row(t["title"], "%s：%s" % (t["sender"], t["preview"]), when(t["_at"], app.tz),
                            "" if self.card else "chat:" + chat["id"], key="need:" + chat["id"],
                            badge={"text": label, "tone": "caution" if t["kind"] == "mention" else "neutral"}))
        sections = [{"items": [self._summary(c, version)]}]
        if rows:
            sections.append({"title": "需要我", "items": rows})
        elif not self.card:
            sections.append({"title": "需要我", "items": [empty("没有要你处理的私聊和 @我", "", version)]})
        if not self.card:
            groups = [row(views.chat_name(chat, chat["id"]), "%s：%s" % (views.sender_of(last, app.me_name()),
                          views.text_of(last)), when(last.get("at") or 0, app.tz), "chat:" + chat["id"], key="group:" + chat["id"])
                      for chat, last in self.data.active_groups()]
            if groups:
                sections.append({"title": "今天活跃的群", "items": groups})
        view: Dict[str, Any] = {"sections": sections, "refresh_after": REFRESH, "state": ""}
        if not self.card:
            view["title"] = "%s今天" % p
        return view

    def _chat(self, chat_id: str, version: int) -> dict:
        app = self.app
        chat = app.store.get_chat(chat_id) if app.store is not None else None
        if chat is None:
            return self._overview(version)
        rows = [r for r in app.store.messages_before(chat_id, None, THREAD_LINES)
                if views.has_content(r) and not views.is_system(r)]
        me = app.me_name()
        items = [row(views.sender_of(r, me), views.text_of(r), when(r.get("at") or 0, app.tz), key="m:" + r["id"])
                 for r in rows]
        if not items:
            items = [empty("最近没有消息", "", version)]
        back = {"type": "button", "title": "返回", "action": {"id": "back"}}
        return {"title": views.chat_name(chat, chat_id), "state": state_text({"chat": chat_id}), "refresh_after": REFRESH,
                "sections": [{"items": [back]}, {"title": views.where(app.platform.name, chat, chat_id), "items": items}]}


class UI:
    """The contributions this program draws, and the navigation badge."""

    def __init__(self, app, extra: Optional[Callable[[Any], Dict[str, Page]]] = None):
        self.app = app
        self.pages: Dict[str, Page] = {CHATS_PAGE: ChatsPage(app), CHATS_CARD: ChatsPage(app, card=True)}
        if extra is not None:
            try:
                self.pages.update(extra(app) or {})
            except Exception:
                log.exception("the platform's pages could not be made")
        self._badge: Optional[str] = None
        self._mu = threading.Lock()

    def _page(self, params: dict) -> Page:
        page = self.pages.get(str(params.get("contribution") or ""))
        if page is None:
            raise RPCError("没有这个界面：%s" % params.get("contribution"), code="no_contribution")
        return page

    def render(self, params: dict) -> dict:
        return self._page(params).render(params)

    def action(self, params: dict) -> dict:
        return self._page(params).action(params)

    def update_badge(self) -> None:
        """How many chats need the user, on the navigation entry (sent when it changed, and always after a start)."""
        app = self.app
        closing = getattr(app, "closing", None)
        if app.peer is None or app.store is None or closing is not None and closing.is_set():
            return
        try:
            n = len(ChatsToday(app).needs())
        except Exception:
            if closing is None or not closing.is_set():  # the store closing under it: nothing to say
                log.exception("the badge could not be counted")
            return
        text = str(n) if n else ""
        with self._mu:
            if text == self._badge:
                return
            self._badge = text
        params = {"contribution": CHATS_PAGE, "text": text}
        if text:
            params["tone"] = "caution"
        app.peer.notify("ui.badge", params)


TONES = ("neutral", "positive", "caution", "danger")


def check_view(view: dict, compact: bool = False) -> List[str]:
    """What the hub would refuse in a View (docs/extensions/extensions.md "Declarative UI": the schema and ui.go's
    checks), as sentences; [] when it draws. For tests, and for a page that might grow past a limit."""
    problems: List[str] = []
    if not isinstance(view, dict):
        return ["not an object"]
    if "state" in view and (not isinstance(view["state"], str) or len(view["state"]) > 16384):
        problems.append("state must be a string of at most 16 KB")
    if "refresh_after" in view and (not isinstance(view["refresh_after"], int) or view["refresh_after"] < 0):
        problems.append("refresh_after must be seconds")
    sections = view.get("sections")
    if not isinstance(sections, list):
        return problems + ["sections must be a list"]
    keys = set()
    count = 0
    for i, sec in enumerate(sections):
        for j, item in enumerate(sec.get("items") or []):
            count += 1
            where = "section %d item %d (%s)" % (i, j, item.get("type"))
            if not item.get("type"):
                problems.append(where + ": no type")
            k = item.get("key")
            if k is not None:
                if k in keys:
                    problems.append("%s: key %r twice" % (where, k))
                keys.add(k)
            if item.get("type") == "row" and not item.get("title"):
                problems.append(where + ": a row needs a title")
            if item.get("type") == "stat_grid" and not 1 <= len(item.get("stats") or []) <= 6:
                problems.append(where + ": 1–6 stats")
            if item.get("type") == "link" and not str(item.get("url") or "").startswith("https://"):
                problems.append(where + ": https links only")
            if compact and item.get("type") == "field":
                problems.append(where + ": no fields on a card")

    def walk(v, path):
        if isinstance(v, dict):
            for k, x in v.items():
                if k == "tone" and x not in TONES:
                    problems.append("%s.tone %r" % (path, x))
                walk(x, path + "." + str(k))
        elif isinstance(v, list):
            for n, x in enumerate(v):
                walk(x, "%s[%d]" % (path, n))
        elif isinstance(v, str) and len(v) > 2000 and not path.endswith(".text") and not path.endswith("state"):
            problems.append("%s: longer than 2000 characters" % path)

    walk(sections, "sections")
    if count > 300:
        problems.append("more than 300 items")
    if compact and (len(sections) > 2 or count > 12):
        problems.append("a card has at most 2 sections and 12 items")
    return problems
