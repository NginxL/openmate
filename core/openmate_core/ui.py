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
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import views
from .textutil import one_line
from .wire import RPCError

log = logging.getLogger("openmate.ui")

CHATS_PAGE = "chats"
CHATS_CARD = "chats-today"
NEEDS_MOST = 30
GROUPS_MOST = 10
DONE_MOST = 30  # 已标记处理完 rows on the chats page (a page holds 300 items at most)
AUTOREPLY_MOST = 50  # groups offered for 自动代答 beside the marked ones: those the user wrote in last first
SEARCH_MOST = 50  # 找群: at most this many matches shown
GROUPS_ALL = 10000  # every group the backend can list (Backend.group_candidates), for 找群
SPOKE_MOST = 500  # groups the user wrote in, looked at
ADOPT_MOST = 5000  # a switched group is looked up this far (it may have moved down the list since the page drew)
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


def done_action(key: str, version: Optional[str] = None) -> dict:
    """A row's 处理完: hides it until it changes (a new message, an update). With version, what the page showed:
    a newer one arrived since is not hidden by it."""
    return {"title": "处理完", "icon": "check", "swipe": True, "action": {"id": done_id(key, version)}}


def done_id(key: str, version: Optional[str] = None) -> str:
    """done:<key> (any version), or done2:<key>:<version> (the one the page showed), both parts quoted."""
    if version is None:
        return "done:" + key
    return "done2:%s:%s" % (urllib.parse.quote(key, safe=""), urllib.parse.quote(str(version), safe=""))


def parse_done(aid: str) -> Optional[Tuple[str, Optional[str]]]:
    """(the key, the version the page showed or None) of a 处理完 action; None for another action."""
    if aid.startswith("done2:"):
        key, _, v = aid[6:].partition(":")
        return urllib.parse.unquote(key), urllib.parse.unquote(v)
    if aid.startswith("done:"):
        return aid[5:], None  # a page from before versions: the key as it is
    return None


FAILED_KEEP = 60.0  # a failed first read is said for this long before a draw tries again


class NotYet(Exception):
    """A first read is still on its way: the page shows that, and draws again soon."""


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
        self._cold: Optional[Tuple[Any, threading.Event, dict]] = None  # (key, done, its result) of a bounded read
        self._failed: Optional[Tuple[Any, float, BaseException]] = None  # (key, when, error) of the last failed one
        self.on_ready: Optional[Callable[[], None]] = None  # told when a read a page did not wait for is in

    def _usable(self, k, within: float) -> bool:
        kept = self._kept
        return kept is not None and kept[1] == k and self.clock() - kept[0] < within

    def get(self, wait: Optional[float] = None) -> Any:
        """The value. With wait, a read that has to be waited for is waited for that long at most (the page has 20
        seconds in all): NotYet when it is still on its way (it goes on in the background), its error when it failed."""
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
        if wait is not None:
            return self._bounded(k, wait)
        with self._reading:
            with self._mu:  # read by someone else meanwhile
                if self._usable(k, self.ahead):
                    return self._kept[2]
            value = self.read()
            self._keep(k, value)
            return value

    def _bounded(self, k, wait: float) -> Any:
        with self._mu:
            failed = self._failed
            if failed is not None and failed[0] == k and self.clock() - failed[1] < FAILED_KEEP:
                raise failed[2]  # failed a moment ago: said, not tried again at once
            cold = self._cold
            start = cold is None or cold[0] != k  # a read for another key (yesterday's) is not this one
            if start:
                cold = self._cold = (k, threading.Event(), {})
        _, ev, box = cold

        def run():
            waited = [False]
            try:
                with self._reading:
                    with self._mu:
                        done = self._usable(k, self.ahead)
                    if not done:
                        self._keep(k, self.read())
                with self._mu:
                    if self._failed is not None and self._failed[0] == k:
                        self._failed = None
            except BaseException as e:
                box["error"] = e
                with self._mu:
                    self._failed = (k, self.clock(), e)
            finally:
                with self._mu:
                    if self._cold is not None and self._cold[1] is ev:
                        self._cold = None
                    waited[0] = not box.get("taken")
                ev.set()
            if waited[0] and "error" not in box and self.on_ready is not None:
                try:
                    self.on_ready()  # a page showed 正在读取: it draws again now
                except Exception:
                    pass

        if start:
            self.spawn(run)
        done = ev.wait(max(0.0, wait))
        with self._mu:
            if done:
                box["taken"] = True
            if self._usable(k, self.stale):
                return self._kept[2]
        if done and box.get("error") is not None:
            raise box["error"]
        raise NotYet(self.name)

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
DONE_SLACK = 60.0  # a service's done mark this much older than a need's latest line still covers it (clock, rounding)
REOPENED = "reopened:"  # a 处理完 mark's version brought back (恢复): the service's done mark leaves that one alone
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
    # each line of theirs soon after the one before (mine first): a gap of hours starts a new thing, even after a
    # short answer then; a line with no time (0) says nothing either way
    prev = mine.get("at") or 0
    for r in run:
        at = r.get("at") or 0
        if at and prev and at - prev > TALK_GAP:
            return False
        prev = at or prev
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

    def needs(self, only: Optional[set] = None) -> List[dict]:
        """Direct chats and @me the user has not answered (the inbox's rule), newest first. A bot's (People) are
        notices: left out, and counted in self.bots. The ones marked 处理完 and still the same are left out too, in
        self.done. With only, just those chats are looked at (the counts then cover only them)."""
        app = self.app
        out = []
        self.bots = 0
        self.done = []
        self.expired = self.closed = 0
        if app.store is None:
            return out
        oldest = time.time() - self.needs_hours() * 3600.0
        since = min(time.time() - app.timing.inbox_window, oldest)  # needs_hours may go back further (7 days)
        chats = app.store.chats_since(since, app.timing.inbox_most if only is None else len(only), only=only)
        for chat in chats:
            if only is not None and chat["id"] not in only:
                continue
            rows = app.store.inbox_rows(chat["id"], 60)
            thing = views.inbox_thing(chat, rows, platform=app.platform.name, tz=app.tz, me=app.me_name(), rules="",
                                      can_reply=False)
            if thing is None or thing.get("done") or thing["kind"] not in ("dm", "mention"):
                continue
            if thing["kind"] == "dm" and talk_closed(rows):
                self.closed += 1
                continue
            thing["_at"] = max((r.get("at") or 0) for r in rows) if rows else 0
            thing["_version_at"] = next((r.get("at") or 0 for r in rows if r.get("id") == thing.get("last")), thing["_at"])
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
                t["_version_at"] = r.get("at") or t["_at"]
            t["_done_key"] = "chat:" + t["_chat"]["id"]
        fresh = [t for t in out if t["_at"] >= oldest]  # the @me itself (a group) or the last line (a direct chat)
        self.expired = len(out) - len(fresh)
        out = fresh
        out.sort(key=lambda t: -t["_at"])
        marked = app.store.dismissed(t["_done_key"] for t in out) if out else {}
        for t in out:
            t["_mark"] = marked.get(t["_done_key"])
        self.done = [t for t in out if marked.get(t["_done_key"]) == t["_version"]]
        return [t for t in out if marked.get(t["_done_key"]) != t["_version"]]

    def dismiss(self, chat_id: str, version: Optional[str] = None) -> str:
        """Mark a chat's need 处理完 (until a newer message, or a newer @me in a group). With version, only the need
        the page showed: "newer" when a newer message came since (it stays), "none" when it needs nothing now, "ok"."""
        app = self.app
        for t in self.needs(only={chat_id}) + self.done:
            if t["_chat"]["id"] == chat_id:
                if version is not None and version != t["_version"]:
                    return "newer"
                if t.get("_mark") != t["_version"] and not app.store.dismiss_if(t["_done_key"], t["_version"], t.get("_mark")):
                    return "none"  # marked meanwhile (another page, the service's 完成): as it is now
                if hasattr(app, "mark_changed"):
                    app.mark_changed("inbox")  # the badge counts again
                return "ok"
        return "none"

    def undismiss(self, chat_id: str) -> None:
        """Bring a chat's need back (恢复). It stays back while the service still says the chat is done there (the
        mark says so until the service is seen not done). Nothing when it is not 处理完 now (a newer message already
        brought it back: a 恢复 from a page drawn before must not cover that one)."""
        app = self.app
        self.needs(only={chat_id})
        t = next((t for t in self.done if t["_chat"]["id"] == chat_id), None)
        if t is None:
            return
        app.store.dismiss_if(t["_done_key"], REOPENED + t["_version"], t["_version"])
        if hasattr(app, "ask_done"):
            app.ask_done([chat_id])  # the service not done there now: a 完成 there from now on counts again
        if hasattr(app, "mark_changed"):
            app.mark_changed("inbox")

    def done_snapshot(self, only: Optional[set] = None) -> Tuple[List[dict], Dict[str, str]]:
        """The needs to ask the service about and their marks now (恢复 ones included), read before asking."""
        needs = self.needs(only)
        return needs, (self.app.store.dismissed(t["_done_key"] for t in needs) if needs else {})

    def service_done(self, needs: List[dict], marked: Dict[str, str], states: Dict[str, Optional[float]]) -> int:
        """The service's own done marks (Feishu's 完成) on needs read before asking it (done_snapshot): a need it says
        is done counts as 处理完 here too, for that need's version (a newer message, or a newer @me in a group,
        brings it back as with 处理完 here). Each write holds only while the mark is still what was read (the user may
        have pressed 处理完 or 恢复 meanwhile). A done mark older than the line that made the need is left alone; so
        is the one a 恢复 here came after, until the service is seen not done (then a new 完成 there counts again).
        How many it marked."""
        app = self.app
        n = 0
        for t in needs:
            chat_id = t["_chat"]["id"]
            if chat_id not in states:
                continue  # not answered about
            key, version = t["_done_key"], t["_version"]
            prev = marked.get(key)
            when = states[chat_id]
            if when is None:
                if prev == REOPENED + version:
                    app.store.undismiss_if(key, prev)
                continue
            if prev == REOPENED + version or prev == version:
                continue
            if when and when + DONE_SLACK < (t.get("_version_at") or t["_at"]):
                continue
            if app.store.dismiss_if(key, version, prev):
                n += 1
        if n and hasattr(app, "mark_changed"):
            app.mark_changed("inbox")
        return n

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
        if not self.card and state.get("groups"):
            return self._groups_view(str(state.get("q") or ""), int(params.get("ui_version") or 1))
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
        if aid == "groups:search":  # 找群's search box: {"q": text}
            return {"view": self._groups_view(str((params.get("values") or {}).get("q") or "").strip(), version)}
        if aid == "groups:open":  # an app without push
            return {"view": self._groups_view("", version)}

        def here():
            if state.get("groups") and not self.card:
                return self._groups_view(str(state.get("q") or ""), version)
            if state.get("chat") and not self.card:
                return self._chat(str(state["chat"]), version)
            return self._overview(version)

        done = parse_done(aid)
        if done is not None and done[0].startswith("chat:"):
            chat_id, seen = done[0][len("chat:"):], done[1]
            got = self.data.dismiss(chat_id, seen)
            if got == "newer":
                return {"toast": "这个会话又有新消息了，先看一下", "view": here()}
            if got == "none":
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
            missing = []
            for chat_id, on in changes.items():
                if on and self.app.store.get_chat(chat_id) is None:
                    self._adopt_candidate(chat_id)  # a group from the backend's list, with no message here yet
                if self.app.store.get_chat(chat_id) is not None:
                    self.app.store.autoreply_set(chat_id, on)
                elif on:
                    missing.append(chat_id)
            if not changes:
                return {}
            if missing:
                return {"toast": "没能开启：这个群暂时找不到了，请刷新页面再试", "view": here()}
            on = any(changes.values())
            return {"toast": "已开启自动代答：有人 @ 你时 AI 直接代你回复" if on else "已关闭自动代答", "view": here()}
        return {}

    def _candidates(self) -> list:
        backend = getattr(self.app, "backend", None)
        if backend is None:
            return []
        try:
            return [g for g in backend.group_candidates(GROUPS_ALL) if g.kind != "dm"]
        except Exception:
            log.exception("the backend's group list could not be read")
            return []

    def _adopt_candidate(self, chat_id: str) -> None:
        backend = getattr(self.app, "backend", None)
        try:
            listed = backend.group_candidates(GROUPS_ALL) if backend is not None else []  # as far as 找群 looks
        except Exception:
            log.exception("the backend's group list could not be read")
            listed = []
        for g in listed:
            if g.id == chat_id and g.kind != "dm":
                self.app.store.upsert_chat(g)
                return

    def _groups(self, q: str = "") -> Tuple[List[dict], set, int]:
        """The groups for 自动代答, in order: the marked ones (those the user wrote in last first), then those the user
        wrote in (the latest first), then those active this week, then every other one the service lists (the newest
        joined first); with q, only those whose name holds it. The service's name wins over a stored older one.
        (groups, the marked ids, how many groups there are in all.)"""
        app = self.app
        marked = app.store.autoreply_chats()
        spoke = app.store.groups_i_spoke_in(SPOKE_MOST)
        mine_at = {c["id"]: c["mine_at"] for c in spoke}
        listed = self._candidates()  # a group made or joined shows up at the next refresh of the service's list
        names = {g.id: g.name for g in listed if g.name}
        pool: Dict[str, dict] = {}

        def put(c) -> None:
            if c is None or c["id"] in pool or views.chat_kind(c) == "dm":
                return
            c = dict(c)
            if names.get(c["id"]):
                c["name"] = names[c["id"]]
            if c["id"] in mine_at:
                c["mine_at"] = mine_at[c["id"]]
            pool[c["id"]] = c

        for cid in marked:
            put(app.store.get_chat(cid))
        for c in spoke:
            put(c)
        for c in app.store.chats_since(time.time() - 7 * 86400, 200):
            put(c)
        if listed:
            stored = {c["id"]: c for c in app.store.chats_recent(GROUPS_ALL)}  # one read, not one per group
            for g in listed:
                if g.id not in pool:
                    put(stored.get(g.id) or {"id": g.id, "name": g.name, "kind": "group"})
        for c in pool.values():
            if not c.get("name"):  # an unnamed group shows a stand-in here only; nothing is stored under it
                c["name"] = "未命名的群（%s）" % c["id"].split("@", 1)[0][-4:]
        total = len(pool)
        on = sorted((c for c in pool.values() if c["id"] in set(marked)), key=lambda c: -float(c.get("mine_at") or 0))
        groups = on + [c for c in pool.values() if c["id"] not in set(marked)]
        if q:
            needle = q.casefold()
            groups = [c for c in groups if needle in views.chat_name(c, c["id"]).casefold() or c["id"] == q]
        return groups, set(marked), total

    def _switch_rows(self, chats: List[dict], marked: set, version: int) -> List[dict]:
        agent = getattr(self.app, "agent", None)
        rows = []
        for c in chats:
            on = c["id"] in marked
            used = agent.auto_used(c["id"]) if agent is not None and on else 0
            sub = ("开着 · 今天代答 %d 次" % used) if on else "关着"
            if c.get("mine_at"):
                sub += " · 我最近 %s 发过言" % when(c["mine_at"], self.app.tz)
            if version >= 6:
                r = row(views.chat_name(c, c["id"]), sub, "", key="auto:" + c["id"])
                r["control"] = {"kind": "switch", "name": "auto:" + c["id"], "on": on, "action": {"id": "autoreply"}}
            else:
                r = row(views.chat_name(c, c["id"]), sub, "关掉" if on else "开启",
                        "autoreply:%s:%s" % ("off" if on else "on", c["id"]), key="auto:" + c["id"])
            rows.append(r)
        return rows

    def _autoreply_footer(self) -> str:
        agent = getattr(self.app, "agent", None)
        most = getattr(agent, "auto_most", 20)
        enabled = bool(getattr(agent, "enabled", False) and getattr(agent, "auto", False))
        return ("开着的群里有人 @ 你，AI 直接以你的身份回复，不用你确认；每个群每天最多 %d 次。私聊、机器人和 @所有人 不代答。" % most
                if enabled else "自动代答没有生效：要先在 设置 › 连接器 里打开 /agent 和“自动代答”。")

    def _autoreply(self, version: int) -> Optional[dict]:
        """The groups the user can mark for 自动代答: the marked ones, then AUTOREPLY_MOST more (those the user wrote
        in last first), each a switch (UI version 6) or a row that turns it on or off; and 找群 for every other."""
        groups, marked, total = self._groups()
        if not groups:
            return None
        shown = groups[:AUTOREPLY_MOST]  # the marked ones first: 50 rows in all (a page holds 300 items)
        rows = self._switch_rows(shown, marked, version)
        if total > len(shown):
            title = "找群：搜索全部 %d 个群" % total
            if version >= 3:
                r = row(title, "按群名搜索，打开或关掉自动代答", "", key="groups:open")
                r["action"] = {"id": "groups:open", "push": {"ui": getattr(self.app.platform, "chats_page", CHATS_PAGE),
                                        "state": state_text({"groups": 1}), "title": "自动代答 · 找群"}}
            else:
                r = row(title, "按群名搜索，打开或关掉自动代答", "", "groups:open", key="groups:open")
            rows.append(r)
        return {"title": "自动代答", "items": rows, "footer": self._autoreply_footer()}

    def _groups_view(self, q: str, version: int) -> dict:
        """找群: a search over every group (the name), at most SEARCH_MOST at a time; without words, the overview's."""
        app = self.app
        st = state_text({"groups": 1, "q": q}) if q else state_text({"groups": 1})
        view: Dict[str, Any] = {"title": "自动代答 · 找群", "state": st}
        if version >= 3:
            view["search"] = {"placeholder": "搜索群名", "value": q, "action": {"id": "groups:search"}}
        if app.store is None or not app.signed_in and app.creds != "present":
            view["sections"] = [{"items": [empty("还没有登录%s" % app.platform.name, "", version)]}]
            return view
        groups, marked, total = self._groups(q)
        if q:
            shown = groups[:SEARCH_MOST]
            more = len(groups) - len(shown)
            head = "“%s”：%d 个群" % (one_line(q, 40), len(groups)) + ("，先列出 %d 个，换个更具体的词试试" % len(shown) if more else "")
        else:
            shown = groups[:AUTOREPLY_MOST]
            head = "共 %d 个群；没搜的时候列出开着的和你最近发过言的" % total
        rows = self._switch_rows(shown, marked, version)
        view["sections"] = [{"title": "自动代答", "items": [{"type": "text", "style": "caption", "text": head}]
                             + (rows or [empty("没有名字里带“%s”的群" % one_line(q, 40) if q else "还没有可以选的群", "", version)]),
                             "footer": self._autoreply_footer()}]
        return view

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
                            actions=[done_action("chat:" + chat["id"], t["_version"])] if version >= 6 and not self.card else None))
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
                    for t in self.data.done[:DONE_MOST]]
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
        # the need first, the lines after: the button's version is never a message the lines do not show (one that
        # came between is shown, and a 处理完 on the older version leaves it to come back)
        need = next((t for t in ChatsToday(app).needs(only={chat_id}) if t["_chat"]["id"] == chat_id), None)
        rows = [r for r in app.store.messages_before(chat_id, None, THREAD_LINES)
                if views.has_content(r) and not views.is_system(r)]
        me = app.me_name()
        items = [row(views.sender_of(r, me), views.text_of(r), when(r.get("at") or 0, app.tz), key="m:" + r["id"])
                 for r in rows]
        if not items:
            items = [empty("最近没有消息", "", version)]
        top = [{"type": "button", "title": "返回", "action": {"id": "back"}}]
        if need is not None:  # this chat's need only (not every chat's), with the version shown
            top.append({"type": "button", "title": "处理完", "action": {"id": done_id("chat:" + chat_id, need["_version"])}})
            if need.get("_version") and need["_version"] not in {r["id"] for r in rows}:
                # an @me older than the lines shown: say which one the button is for
                items.insert(0, row("要处理的 @我", "%s：%s" % (need.get("sender") or "", need.get("preview") or ""),
                                    when(need.get("_at") or 0, app.tz), key="need-at"))
        return {"title": views.chat_name(chat, chat_id), "state": state_text({"chat": chat_id}), "refresh_after": REFRESH,
                "sections": [{"items": top}, {"title": views.where(app.platform.name, chat, chat_id), "items": items}]}


class UI:
    """The contributions this program draws, and the navigation badge."""

    def __init__(self, app, extra: Optional[Callable[[Any], Dict[str, Page]]] = None):
        self.app = app
        plat = getattr(app, "platform", None)
        self.chats_page = getattr(plat, "chats_page", "") or CHATS_PAGE
        self.chats_card = getattr(plat, "chats_card", "") or CHATS_CARD
        self.pages: Dict[str, Page] = {self.chats_page: ChatsPage(app), self.chats_card: ChatsPage(app, card=True)}
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

    def tools(self) -> Dict[str, Tuple[dict, Callable[[dict], str]]]:
        """Read tools the platform's pages offer the assistant (a page's tools(): {name: (schema, call)}), by name. Only
        names in this connector's own namespace (<cid>_…) are taken; a page that fails is left out, not the list."""
        out: Dict[str, Tuple[dict, Callable[[dict], str]]] = {}
        for page in {id(p): p for p in self.pages.values()}.values():
            fn = getattr(page, "tools", None)
            if fn is None:
                continue
            try:
                offered = fn() or {}
            except Exception:
                log.exception("a page's tools could not be listed")
                continue
            for name, entry in offered.items():
                if name.startswith(self.app.cid + "_"):
                    out.setdefault(name, entry)
        return out

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
        params = {"contribution": self.chats_page, "text": text}
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
            act = item.get("action")
            if act is not None and not (isinstance(act, dict) and str(act.get("id") or "")):
                problems.append(where + ": action.id: shorter than 1 (a push needs an id too)")
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
