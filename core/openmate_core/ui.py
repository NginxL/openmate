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
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import views
from .textutil import one_line
from .wire import RPCError

log = logging.getLogger("openmate.ui")

CHATS_PAGE = "chats"
CHATS_CARD = "chats-today"
NEEDS_MOST = 30
GROUPS_MOST = 10
AUTOREPLY_ROWS = 20  # groups offered for 自动代答 (the marked ones always)
SLOW_RENDER = 0.3
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


def row(title: str, subtitle: str = "", value: str = "", action: str = "", key: str = "", badge: Optional[dict] = None,
        icon: str = "", icon_color: str = "", actions: Optional[List[dict]] = None) -> dict:
    out: Dict[str, Any] = {"type": "row", "title": one_line(title or "（无标题）", 120)}
    if icon:
        out["icon"] = icon
        if icon_color:
            out["icon_color"] = icon_color  # UI version 6
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
    if actions:
        out["actions"] = actions  # UI version 6: the row's menu (right click, …, long press) and its swipe
    return out


def done_action(key: str) -> dict:
    """A row's 处理完: hides it until it changes (a new message, an update)."""
    return {"title": "处理完", "icon": "check", "swipe": True, "action": {"id": "done:" + key}}


def tile(title: str, value: str = "", subtitle: str = "", icon: str = "", color: str = "", key: str = "",
         badge: Optional[dict] = None, action: str = "") -> dict:
    """A tile of a grid (UI version 6): an icon in its color, a title, a line, a value."""
    out: Dict[str, Any] = {"title": one_line(title, 40)}
    if value:
        out["value"] = one_line(value, 40)
    if subtitle:
        out["subtitle"] = one_line(subtitle, 80)
    if icon:
        out["icon"] = icon
    if color:
        out["color"] = color
    if key:
        out["key"] = key
    if badge:
        out["badge"] = badge
    if action:
        out["action"] = {"id": action}
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


class Fresh:
    """A value kept fresh behind the pages: get() answers the last read at once and reads again in the background once
    it is older than ahead; only a first read, one for another key (a new day) or one older than stale waits for it.
    A failed read keeps the last value (until stale) and raises only when there is nothing to show. spawn runs a
    background read (a thread; tests run it in place)."""

    def __init__(self, read: Callable[[], Any], ahead: float, stale: float, clock: Callable[[], float] = time.time,
                 key: Optional[Callable[[], Any]] = None, spawn: Optional[Callable[[Callable[[], None]], None]] = None,
                 name: str = ""):
        self.read, self.ahead, self.stale, self.clock = read, ahead, stale, clock
        self.key = key or (lambda: None)
        self.spawn = spawn or (lambda fn: threading.Thread(target=fn, name="openmate-fresh", daemon=True).start())
        self.name = name
        self._mu = threading.Lock()  # the kept value
        self._reading = threading.Lock()  # one read at a time
        self._kept: Optional[Tuple[float, Any, Any]] = None  # (read at, key, value)
        self._busy = False

    def _usable(self, k, within: float) -> bool:
        kept = self._kept
        return kept is not None and kept[1] == k and self.clock() - kept[0] < within

    def get(self) -> Any:
        k = self.key()
        again = False
        with self._mu:
            if self._usable(k, self.ahead):
                return self._kept[2]
            if self._usable(k, self.stale):
                value = self._kept[2]
                if not self._busy:
                    self._busy = again = True
            else:
                value = None
        if again:
            self.spawn(self._again)
            return value
        if value is not None:
            return value
        with self._reading:
            with self._mu:  # read by someone else meanwhile
                if self._usable(k, self.ahead):
                    return self._kept[2]
            value = self.read()
            self._keep(k, value)
            return value

    def _again(self) -> None:
        k = self.key()
        try:
            with self._reading:
                with self._mu:
                    if self._usable(k, self.ahead):
                        return
                value = self.read()
            self._keep(k, value)
        except Exception as e:  # the last value stays
            log.debug("%s: reading again failed (%s); the last read stays", self.name, e.__class__.__name__)
        finally:
            with self._mu:
                self._busy = False

    def _keep(self, k, value) -> None:
        with self._mu:
            self._kept = (self.clock(), k, value)

    def warm(self, every: float = 0.0, stop: Optional[threading.Event] = None) -> None:
        """Read now in the background, so that the first draw does not wait; with every, keep reading that often (a
        page opened after a long while then shows a read at most that old, and does not wait either)."""
        def run():
            while True:
                try:
                    self.get()
                except Exception as e:
                    log.debug("%s: a read failed (%s)", self.name, e.__class__.__name__)
                if not every or (stop.wait(every) if stop is not None else time.sleep(every)):
                    return

        self.spawn(run)


NEEDS_HOURS = 24  # an @me or a direct message older than this no longer needs the user (setting needs_hours)
TALK_GAP = 3600.0  # the other side answered within this of my last message: the talk goes on, it is not a request
CLOSER_MOST = 3  # at most this many messages back from them, none asking anything: the talk is over
ASKING = ("?", "？", "吗", "么", "呢", "嘛", "能不能", "可不可以", "可以不", "帮忙", "帮我", "麻烦", "请你", "请帮",
          "看下", "看一下", "看看", "确认", "给点", "审批", "approval")


def asks_something(row: dict) -> bool:
    """A message that asks the user something or for something (a question, a request, an approval link)."""
    text = views.text_of(row)
    return bool(row.get("at_me")) or any(w in text for w in ASKING)


def talk_closed(rows: List[dict]) -> bool:
    """A direct chat where the user spoke last and the other side only answered (a few lines, soon after, asking
    nothing): "27层" after "几楼呀" needs no answer."""
    msgs = [r for r in rows if not views.is_system(r) and views.has_content(r)]
    run = views.unanswered_run(msgs)
    if not run or len(run) == len(msgs) or len(run) > CLOSER_MOST:
        return False
    mine = msgs[len(msgs) - len(run) - 1]
    if (run[0].get("at") or 0) - (mine.get("at") or 0) > TALK_GAP:
        return False
    return not any(asks_something(r) for r in run)


class ChatsToday:
    """What needs the user in the chats, from the store: unanswered direct chats and @me, today's traffic."""

    def __init__(self, app):
        self.app = app
        self.expired = 0  # left out by needs(): older than needs_hours
        self.closed = 0  # left out by needs(): direct chats where the other side only answered

    def needs_hours(self) -> int:
        try:
            n = int((getattr(self.app, "config", None) or {}).get("needs_hours") or NEEDS_HOURS)
        except (TypeError, ValueError):
            n = NEEDS_HOURS
        return max(1, min(168, n))

    def needs(self) -> List[dict]:
        """Direct chats and @me the user has not answered (the inbox's rule), newest first. A bot's (People) are
        notices: left out, and counted in self.bots. The ones marked 处理完 and still the same are left out too, in
        self.done."""
        app = self.app
        out = []
        self.bots = 0
        self.done = []
        self.expired = self.closed = 0
        if app.store is None:
            return out
        oldest = time.time() - self.needs_hours() * 3600.0
        for chat in app.store.chats_since(time.time() - app.timing.inbox_window, app.timing.inbox_most):
            rows = app.store.messages_before(chat["id"], None, 60)
            thing = views.inbox_thing(chat, rows, platform=app.platform.name, tz=app.tz, me=app.me_name(), rules="",
                                      can_reply=False)
            if thing is None or thing.get("done") or thing["kind"] not in ("dm", "mention"):
                continue
            if thing["kind"] == "dm" and talk_closed(rows):
                self.closed += 1
                continue
            thing["_at"] = max((r.get("at") or 0) for r in rows) if rows else 0
            thing["_chat"] = chat
            if thing["kind"] == "dm":
                who = {chat.get("peer_id") or ""} if chat.get("peer_id") else {r.get("sender_id") for r in rows if not r.get("mine")}
            else:
                thing["_ats"] = [r for r in rows if r.get("at_me") and not r.get("mine")]
                who = {r.get("sender_id") for r in thing["_ats"]}
            thing["_who"] = {w for w in who if w}
            out.append(thing)
        people = getattr(app, "people", None)
        bots: set = set()
        if people is not None and out:
            bots = people.bots(set().union(*(t["_who"] for t in out)))
            kept = [t for t in out if not (t["_who"] and t["_who"] <= bots)]
            self.bots = len(out) - len(kept)
            out = kept
        me = app.me_name()
        for t in out:
            # an @me says what asked for me, from whom and when, not the chat's last line (often a bot's)
            ats = [r for r in t.get("_ats") or [] if r.get("sender_id") not in bots]
            t["_version"] = str(t.get("last") or "")
            if ats:
                r = ats[-1]
                t["sender"], t["preview"] = views.sender_of(r, me), one_line(views.text_of(r), 200)
                t["_at"] = r.get("at") or t["_at"]
                t["_version"] = str(r.get("id") or "")  # a new @me brings it back, not any line of the group
            t["_done_key"] = "chat:" + t["_chat"]["id"]
        fresh = [t for t in out if t["_at"] >= oldest]  # the @me itself (a group) or the last line (a direct chat)
        self.expired = len(out) - len(fresh)
        out = fresh
        out.sort(key=lambda t: -t["_at"])
        marked = app.store.dismissed(t["_done_key"] for t in out) if out else {}
        self.done = [t for t in out if marked.get(t["_done_key"]) == t["_version"]]
        return [t for t in out if marked.get(t["_done_key"]) != t["_version"]]

    def dismiss(self, chat_id: str) -> bool:
        """Mark a chat's need 处理完 (until a newer message, or a newer @me in a group). False: it needs nothing now."""
        app = self.app
        for t in self.needs() + self.done:
            if t["_chat"]["id"] == chat_id:
                app.store.dismiss(t["_done_key"], t["_version"])
                if hasattr(app, "mark_changed"):
                    app.mark_changed("inbox")  # the badge counts again
                return True
        return False

    def undismiss(self, chat_id: str) -> None:
        self.app.store.undismiss("chat:" + chat_id)
        if hasattr(self.app, "mark_changed"):
            self.app.mark_changed("inbox")

    def counts(self, needs: List[dict]) -> Dict[str, int]:
        app = self.app
        start = day_start(app.tz)
        today = app.store.count_since(start) if app.store is not None else {"messages": 0, "chats": 0}
        return {"needs": len(needs), "mentions": sum(1 for t in needs if t["kind"] == "mention"),
                "dms": sum(1 for t in needs if t["kind"] == "dm"), "messages": today["messages"], "chats": today["chats"],
                "bots": getattr(self, "bots", 0), "done": len(getattr(self, "done", []))}

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
        state = state_of(params)
        here = lambda: self._chat(str(state["chat"]), version) if state.get("chat") and not self.card else self._overview(version)  # noqa: E731
        if aid.startswith("done:chat:"):
            if not self.data.dismiss(aid[len("done:chat:"):]):
                return {"toast": "这个会话现在没有要处理的了", "view": here()}
            return {"toast": "已标记处理完，有新消息会再出现", "view": here()}
        if aid.startswith("undone:chat:"):
            self.data.undismiss(aid[len("undone:chat:"):])
            return {"toast": "已恢复", "view": here()}
        if aid == "autoreply" or aid.startswith("autoreply:"):
            changes = {}
            for name, v in (params.get("values") or {}).items():  # a switch: {"auto:<chat>": "true" | "false"}
                if str(name).startswith("auto:"):
                    changes[str(name)[5:]] = str(v).lower() in ("true", "1", "on")
            if aid.startswith("autoreply:"):  # an app without switches: autoreply:on:<chat> / autoreply:off:<chat>
                _, how, chat_id = aid.split(":", 2)
                changes[chat_id] = how == "on"
            for chat_id, on in changes.items():
                if self.app.store.get_chat(chat_id) is not None:
                    self.app.store.autoreply_set(chat_id, on)
            if not changes:
                return {}
            on = any(changes.values())
            return {"toast": "已开启自动代答：有人 @ 你时 AI 直接代你回复" if on else "已关闭自动代答", "view": here()}
        return {}

    def _autoreply(self, version: int) -> Optional[dict]:
        """The groups the user can mark for 自动代答 (the ones marked, then the groups active this week), each a
        switch (UI version 6) or a row that turns it on or off."""
        app = self.app
        marked = app.store.autoreply_chats()
        seen, chats = set(), []
        for cid in marked:
            c = app.store.get_chat(cid)
            if c is not None:
                chats.append(c)
                seen.add(cid)
        for c in app.store.chats_since(time.time() - 7 * 86400, 60):
            if c["id"] not in seen and views.chat_kind(c) != "dm" and len(chats) < AUTOREPLY_ROWS:
                chats.append(c)
                seen.add(c["id"])
        if not chats:
            return None
        agent = getattr(app, "agent", None)
        most = getattr(agent, "auto_most", 20)
        rows = []
        for c in chats:
            on = c["id"] in marked
            used = agent.auto_used(c["id"]) if agent is not None and on else 0
            sub = ("开着 · 今天代答 %d 次" % used) if on else "关着"
            if version >= 6:
                r = row(views.chat_name(c, c["id"]), sub, "", key="auto:" + c["id"])
                r["control"] = {"kind": "switch", "name": "auto:" + c["id"], "on": on, "action": {"id": "autoreply"}}
            else:
                r = row(views.chat_name(c, c["id"]), sub, "关掉" if on else "开启",
                        "autoreply:%s:%s" % ("off" if on else "on", c["id"]), key="auto:" + c["id"])
            rows.append(r)
        enabled = bool(getattr(agent, "enabled", False) and getattr(agent, "auto", False))
        footer = ("开着的群里有人 @ 你，AI 直接以你的身份回复，不用你确认；每个群每天最多 %d 次。私聊、机器人和 @所有人 不代答。" % most
                  if enabled else "自动代答没有生效：要先在 设置 › 连接器 里打开 /agent 和“自动代答”。")
        return {"title": "自动代答", "items": rows, "footer": footer}

    def _summary(self, c: Dict[str, int], version: int) -> dict:
        stats = [stat("需要我", c["needs"], "机器人通知 %d 条不算在内" % c["bots"] if c.get("bots") else "",
                      tone="caution" if c["needs"] else ""), stat("@我", c["mentions"]),
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
                            badge={"text": label, "tone": "caution" if t["kind"] == "mention" else "neutral"},
                            actions=[done_action("chat:" + chat["id"])] if version >= 6 and not self.card else None))
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
            done = [row(t["title"], "%s：%s" % (t["sender"], t["preview"]), when(t["_at"], app.tz), "chat:" + t["_chat"]["id"],
                        key="done:" + t["_chat"]["id"],
                        actions=[{"title": "恢复", "icon": "refresh", "action": {"id": "undone:chat:" + t["_chat"]["id"]}}]
                        if version >= 6 else None)
                    for t in self.data.done]
            if done:
                sections.append({"title": "已标记处理完", "items": done,
                                 "footer": "有新消息会自动回到“需要我”；要提前放回，在这一行的菜单里选“恢复”"})
            auto = self._autoreply(version)
            if auto is not None:
                sections.append(auto)
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
        top = [{"type": "button", "title": "返回", "action": {"id": "back"}}]
        if any(t["_chat"]["id"] == chat_id for t in self.data.needs()):
            top.append({"type": "button", "title": "处理完", "action": {"id": "done:chat:" + chat_id}})
        return {"title": views.chat_name(chat, chat_id), "state": state_text({"chat": chat_id}), "refresh_after": REFRESH,
                "sections": [{"items": top}, {"title": views.where(app.platform.name, chat, chat_id), "items": items}]}


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
        page = self._page(params)
        t0 = time.monotonic()
        try:
            return page.render(params)
        finally:
            took = time.monotonic() - t0
            if took > SLOW_RENDER:  # the hub and the apps wait on it: say which page was slow
                log.info("ui.render %s took %dms", params.get("contribution"), took * 1000)

    def action(self, params: dict) -> dict:
        return self._page(params).action(params)

    def warm(self) -> None:
        """Have the pages read what they show (the calendar, Persona) in the background, before anyone looks."""
        for page in {id(p): p for p in self.pages.values()}.values():
            fn = getattr(page, "warm", None)
            if fn is not None:
                try:
                    fn()
                except Exception:
                    log.exception("a page could not start reading")

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
COLORS = ("red", "orange", "amber", "yellow", "green", "teal", "blue", "indigo", "purple", "pink", "gray")


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
    if view.get("layout") is not None and view["layout"] not in ("stack", "split", "columns"):
        problems.append("layout %r" % view["layout"])
    sections = view.get("sections")
    if not isinstance(sections, list):
        return problems + ["sections must be a list"]
    keys = set()
    count = 0
    for i, sec in enumerate(sections):
        # UI version 8 (#9158): span, icon + icon_color, link {title, action} and columns of a section
        if sec.get("span") is not None and sec["span"] != "full":
            problems.append("section %d: span %r" % (i, sec["span"]))
        if sec.get("icon_color") is not None and sec["icon_color"] not in COLORS:
            problems.append("section %d: icon_color %r" % (i, sec["icon_color"]))
        if sec.get("columns") is not None and (not isinstance(sec["columns"], int) or not 1 <= sec["columns"] <= 3):
            problems.append("section %d: columns 1–3" % i)
        link = sec.get("link")
        if link is not None and (not isinstance(link, dict) or not 1 <= len(link.get("title") or "") <= 40
                                 or not (link.get("action") or {}).get("id")):
            problems.append("section %d: a link needs a title (1–40) and an action" % i)
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
            if item.get("type") == "grid":
                tiles = item.get("tiles") or []
                if not 1 <= len(tiles) <= (4 if compact else 60):
                    problems.append(where + ": 1–%d tiles" % (4 if compact else 60))
                count += max(0, len(tiles) - 1)  # each tile counts as a component
                for n, t in enumerate(tiles):
                    tw = "%s tile %d" % (where, n)
                    if not 1 <= len(t.get("title") or "") <= 40 or len(t.get("subtitle") or "") > 80 or len(t.get("value") or "") > 40:
                        problems.append(tw + ": title 1–40, subtitle ≤80, value ≤40")
                    if t.get("color") is not None and t["color"] not in COLORS:
                        problems.append("%s: color %r" % (tw, t["color"]))
                    g = t.get("gauge")  # UI version 9
                    if g is not None:
                        ratio = g.get("ratio") if isinstance(g, dict) else None
                        if (not isinstance(ratio, (int, float)) or not 0 <= ratio <= 1 or len(g.get("unit") or "") > 8
                                or (g.get("color") is not None and g["color"] not in COLORS) or t.get("image") or t.get("spark")):
                            problems.append(tw + ": a gauge needs a ratio 0–1, a unit of at most 8, a known color, no image or spark")
                    if t.get("key") is not None:
                        if t["key"] in keys:
                            problems.append("%s: key %r twice" % (tw, t["key"]))
                        keys.add(t["key"])
            if item.get("icon_color") is not None and item["icon_color"] not in COLORS:
                problems.append("%s: icon_color %r" % (where, item["icon_color"]))
            ctl = item.get("control")
            if ctl is not None and (ctl.get("kind") not in ("switch", "slider", "stepper") or not ctl.get("name")
                                    or not (ctl.get("action") or {}).get("id") or not item.get("key")):
                problems.append(where + ": a control needs a kind, a name, an action, and the row a key")
            acts = item.get("actions")
            if acts is not None:
                if not isinstance(acts, list) or not 1 <= len(acts) <= 4 or sum(1 for a in acts if a.get("swipe")) > 2 \
                        or any(not 1 <= len(a.get("title") or "") <= 20 or not (a.get("action") or {}).get("id") for a in acts):
                    problems.append(where + ": 1–4 actions, each a title (1–20) and an action, at most 2 swiped")

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
