"""The "Agent 进度" page and card: every Agent conversation the hub knows (Claude Code, Codex, … on every computer),
sorted by what it asks of the user: waiting for them, working, maybe stuck, finished and not looked at, done today.

One read of the hub's session list (agents.read; the hub's own assistant conversations left out), kept fresh behind the
page (Fresh): the page never waits on the hub after the first read. A row opens the Agent's details on this page
(its last words, model, context, branch); the hub only lets an extension open https links, so not the conversation.
"""

from __future__ import annotations

import datetime as _dt
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .agent import model_label
from .hub import HubError
from .textutil import clip, one_line
from .ui import Fresh, Page, day_start, empty, row, stat, state_of, state_text, tile, when

log = logging.getLogger("openmate.agents")

PAGE = "openmate-agents"
CARD = "agents-card"
AHEAD = 20.0  # older than this, read again in the background
STALE = 600.0  # older than this, a draw waits for a new read
KEEP = 120.0  # while running, read this often anyway
WAIT = 8.0
REFRESH = 60
STUCK = 30 * 60  # working with nothing new for this long: maybe stuck
MOST = 12  # rows in one group of the page
CARD_ROWS = 3
LAST_TEXT = 1500

GROUPS = (
    # (kind, title, icon, color, badge tone, empty sentence)
    ("needs", "等你", "bell", "red", "danger", "没有在等你的 Agent"),
    ("stuck", "可能卡住", "warning", "orange", "caution", "没有卡住的"),
    ("working", "在跑", "clock", "teal", "", "现在没有在跑的 Agent"),
    ("unread", "做完没看", "check", "blue", "positive", "没有做完没看的"),
    ("today", "今天做过", "list", "gray", "", "今天还没有别的 Agent 做过事"),
)
TITLES = {k: t for k, t, *_ in GROUPS}


def ts(v: Any) -> float:
    """A hub time (RFC 3339, Go's zero time for none) as a timestamp; 0 when none."""
    s = str(v or "").strip()
    if not s or s.startswith("0001-"):
        return 0.0
    try:
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def span(seconds: float) -> str:
    """How long, as people say it: 3 分钟, 1 小时 20 分, 2 天."""
    m = int(max(0, seconds) // 60)
    if m < 1:
        return "不到 1 分钟"
    if m < 60:
        return "%d 分钟" % m
    h, m = divmod(m, 60)
    if h < 24:
        return "%d 小时%s" % (h, " %d 分" % m if m else "")
    return "%d 天" % (h // 24)


def last_moved(s: dict) -> float:
    return max(ts(s.get("updated_at")), ts(s.get("activity_at")), ts(s.get("state_since")))


def kind_of(s: dict, now: float, today_start: float) -> str:
    """needs / stuck / working / unread / today, or "" for older ones."""
    state = str(s.get("state") or "")
    if state == "needs_you" or int(s.get("pending_interactions") or 0) > 0:
        return "needs"
    if state == "working":
        if not s.get("waiting") and now - last_moved(s) >= STUCK:
            return "stuck"  # a background wait (a loop, a subagent, a timer) may be quiet for long: not stuck
        return "working"
    if state == "unread":
        return "unread"
    if last_moved(s) >= today_start:
        return "today"
    return ""


def project(s: dict) -> str:
    return str(s.get("space_name") or "").strip() or _base(str(s.get("repo_root") or s.get("workspace_cwd") or s.get("cwd") or ""))


def _base(path: str) -> str:
    path = path.rstrip("/")
    return path.rsplit("/", 1)[-1] if path else ""


def context_share(s: dict) -> Optional[int]:
    try:
        used, window = int(s.get("context_tokens") or 0), int(s.get("context_window") or 0)
    except (TypeError, ValueError):
        return None
    return round(used * 100 / window) if used > 0 and window > 0 else None


class AgentsRead:
    """The hub's Agent conversations: [Session] (summary rows, children folded under their parents)."""

    def __init__(self, app, clock: Callable[[], float] = time.time, spawn: Optional[Callable] = None):
        self.app = app
        self.clock = clock
        self._fresh = Fresh(self._read, AHEAD, STALE, clock=clock, spawn=spawn, name="Agents")

    def warm(self) -> None:
        self._fresh.warm(every=KEEP, stop=getattr(self.app, "closing", None))

    def read(self) -> List[dict]:
        return self._fresh.get()

    def _read(self) -> List[dict]:
        hub = getattr(self.app, "hub", None)
        if hub is None or not getattr(hub, "available", False):
            raise HubError(0, "拿不到 Persona hub 的地址或令牌")
        v = hub.request("GET", "/api/v1/sessions", query={"summary": 1, "roots": 1, "agents_only": 1, "limit": 200},
                        timeout=WAIT)
        sessions = (v or {}).get("sessions") if isinstance(v, dict) else None
        return [s for s in (sessions or []) if isinstance(s, dict) and not s.get("assistant")]


class Board:
    """One read sorted into the groups, as the page and the card draw it."""

    def __init__(self, sessions: List[dict], now: float, tz):
        self.now, self.tz = now, tz
        start = day_start(tz, now)
        self.groups: Dict[str, List[dict]] = {k: [] for k, *_ in GROUPS}
        for s in sessions:
            k = kind_of(s, now, start)
            if k:
                self.groups[k].append(s)
        oldest_first = lambda s: (ts(s.get("state_since")) or last_moved(s))  # noqa: E731
        self.groups["needs"].sort(key=oldest_first)
        self.groups["stuck"].sort(key=last_moved)
        self.groups["working"].sort(key=oldest_first)
        self.groups["unread"].sort(key=last_moved, reverse=True)
        self.groups["today"].sort(key=last_moved, reverse=True)
        self.by_id = {str(s.get("session_id")): s for s in sessions}

    def count(self, kind: str) -> int:
        return len(self.groups[kind])

    def line(self, s: dict, kind: str) -> str:
        """The row's second line: project · what it does · since when."""
        bits = [project(s)]
        if kind in ("needs", "working", "stuck"):
            if kind == "stuck":
                bits.append("%s没有动静" % span(self.now - last_moved(s)))
            else:
                bits.append(str(s.get("activity") or "").strip() or ("在等你" if kind == "needs" else "在跑"))
                since = ts(s.get("state_since"))
                if since:
                    bits.append(("等了 " if kind == "needs" else "已经 ") + span(self.now - since))
            if kind == "working" and s.get("waiting"):
                bits.append("后台等待")
        else:
            text = one_line(str(s.get("last_text") or ""), 80)
            if text:
                bits.append(text)
            bits.append(when(last_moved(s), self.tz, self.now))
        return " · ".join(b for b in bits if b)

    def value(self, s: dict) -> str:
        share = context_share(s)
        return "上下文 %d%%" % share if share is not None else ""

    def projects(self) -> List[Tuple[str, Dict[str, int]]]:
        """Projects with something today, the busiest first: (name, {kind: count})."""
        out: Dict[str, Dict[str, int]] = {}
        for k, items in self.groups.items():
            for s in items:
                c = out.setdefault(project(s) or "（没有项目）", {})
                c[k] = c.get(k, 0) + 1
        order = lambda kv: (-(kv[1].get("needs", 0) + kv[1].get("stuck", 0)), -sum(kv[1].values()), kv[0])  # noqa: E731
        return sorted(out.items(), key=order)


class AgentsPage(Page):
    def __init__(self, app, agents: AgentsRead, card: bool = False):
        self.app = app
        self.agents = agents
        self.card = card

    def warm(self) -> None:
        self.agents.warm()

    def _board(self) -> Tuple[Optional[Board], str]:
        try:
            sessions = self.agents.read()
        except HubError as e:
            return None, e.message
        except Exception as e:  # never the page
            log.exception("the Agents could not be read")
            return None, "读 Agent 列表出错了（%s）" % e.__class__.__name__
        return Board(sessions, self.agents.clock(), self.app.tz), ""

    def render(self, params: dict) -> dict:
        version = int(params.get("ui_version") or 1)
        board, missing = self._board()
        if board is None:
            note = empty("暂时读不到 Agent", missing + "，稍后自动重试", version)
            return {"sections": [{"items": [note]}], "refresh_after": REFRESH} if self.card else \
                {"title": "Agent 进度", "sections": [{"items": [note]}], "refresh_after": REFRESH}
        if self.card:
            return self._card(board, version)
        st = state_of(params)
        if st.get("session"):
            return self._detail(board, str(st["session"]), version)
        return self._page(board, version)

    # ---- pieces

    def _head(self, board: Board, version: int, kinds) -> dict:
        blocks = []
        for k, title, icon, color, tone, _ in GROUPS:
            if k not in kinds:
                continue
            n = board.count(k)
            badge = {"text": "有 %d 个" % n, "tone": tone} if n and tone in ("danger", "caution") else None
            first = board.groups[k][0] if n else None
            line = one_line(first.get("title") or project(first), 40) if first else "没有"
            blocks.append((title, str(n), line, icon, color if n else "gray", badge, "t:" + k))
        if version >= 6:
            return {"type": "grid", "max_columns": len(blocks), "key": "tiles",
                    "tiles": [tile(t, v, line, icon, color, key, badge) for t, v, line, icon, color, badge, key in blocks]}
        if version >= 2:
            return {"type": "stat_grid", "stats": [stat(t, v, line, "caution" if badge else "") for t, v, line, _, _, badge, _ in blocks]}
        return {"type": "text", "text": " · ".join("%s %s" % (t, v) for t, v, *_ in blocks)}

    def _row(self, board: Board, s: dict, kind: str, version: int, interactive: bool = True) -> dict:
        _, _, icon, color, tone, _ = [g for g in GROUPS if g[0] == kind][0]
        badge = {"text": TITLES[kind], "tone": tone} if tone and kind in ("needs", "stuck") else None
        sid = str(s.get("session_id") or "")
        extra = {"icon": icon, "icon_color": color} if version >= 6 else {}
        return row(str(s.get("title") or project(s) or "Agent"), board.line(s, kind), board.value(s),
                   action="agent:" + sid if interactive else "", key="%s:%s" % (kind, sid), badge=badge, **extra)

    def _card(self, board: Board, version: int) -> dict:
        head = self._head(board, version, ("needs", "stuck", "working", "unread"))
        rows = []
        for k in ("needs", "stuck", "working", "unread"):
            for s in board.groups[k]:
                if len(rows) < CARD_ROWS:
                    rows.append(self._row(board, s, k, version, interactive=False))  # a card only shows
        return {"sections": [{"items": [head]},
                             {"items": rows or [empty("没有等你或在跑的 Agent", "今天做过 %d 个" % board.count("today"), version)]}],
                "refresh_after": REFRESH}

    def _page(self, board: Board, version: int) -> dict:
        sections: List[Dict[str, Any]] = [{"items": [self._head(board, version, [k for k, *_ in GROUPS])]}]
        for k, title, _, _, _, nothing in GROUPS:
            items = board.groups[k]
            if not items and k in ("stuck", "unread"):
                continue  # only the groups that always say something stay when empty
            sec: Dict[str, Any] = {"title": "%s · %d" % (title, len(items)) if items else title,
                                   "items": [self._row(board, s, k, version) for s in items[:MOST]] or [empty(nothing, "", version)]}
            if len(items) > MOST:
                sec["footer"] = "还有 %d 个" % (len(items) - MOST)
            sections.append(sec)
        projects = board.projects()
        if projects:
            lines = []
            for name, c in projects[:MOST]:
                bits = ["%s %d" % (TITLES[k], c[k]) for k, *_ in GROUPS if c.get(k)]
                lines.append(row(name, " · ".join(bits), "", key="p:" + name, **({"icon": "folder", "icon_color": "indigo"} if version >= 6 else {})))
            sections.append({"title": "按项目", "items": lines})
        return {"title": "Agent 进度", "sections": sections, "refresh_after": REFRESH}

    def _detail(self, board: Board, sid: str, version: int) -> dict:
        s = board.by_id.get(sid)
        if s is None:
            return {"title": "Agent", "sections": [{"items": [empty("这个 Agent 不在最近的列表里了", "可能已经归档，或者离线超过 7 天", version)]}],
                    "refresh_after": REFRESH}
        start = day_start(self.app.tz, board.now)
        kind = kind_of(s, board.now, start) or "today"
        facts = [stat("状态", TITLES.get(kind, "空闲")), stat("项目", project(s) or "—")]
        model = model_label(str(s.get("model") or ""))
        if model:
            facts.append(stat("模型", model + (" · " + str(s["effort"]) if s.get("effort") else "")))
        share = context_share(s)
        if share is not None:
            facts.append(stat("上下文", "%d%%" % share, "%d 万 / %d 万" % (int(s["context_tokens"]) // 10000, int(s["context_window"]) // 10000),
                              "caution" if share >= 80 else ""))
        info = []
        for label, value in (("在做", s.get("activity")), ("分支", s.get("branch")), ("电脑", s.get("machine_id")),
                             ("开始", when(ts(s.get("started_at")), self.app.tz, board.now)),
                             ("最近变化", when(last_moved(s), self.app.tz, board.now)),
                             ("后台任务", s.get("background_tasks_count") or ""),
                             ("等你处理的卡片", s.get("pending_interactions") or "")):
            if value:
                info.append(row(label, "", one_line(str(value), 60), key="i:" + label))
        sections: List[Dict[str, Any]] = [{"items": [{"type": "stat_grid", "stats": facts}] if version >= 2 else
                                           [{"type": "text", "text": " · ".join("%s %s" % (f["label"], f["value"]) for f in facts)}]}]
        if info:
            sections.append({"title": "详情", "items": info})
        children = [c for c in (s.get("children") or []) if isinstance(c, dict)]
        if children:
            sections.append({"title": "子任务 · %d" % len(children),
                             "items": [row(str(c.get("title") or "子任务"), str(c.get("state") or ""), "", key="c:%d" % i)
                                       for i, c in enumerate(children[:MOST])]})
        text = str(s.get("last_text") or "").strip()
        sections.append({"title": "最近说的", "items": [{"type": "text", "text": clip(text, LAST_TEXT)} if text else
                                                         empty("还没有回复", "", version)]})
        sections.append({"items": [{"type": "button", "title": "返回 Agent 进度", "action": {"id": "back"}, "key": "back"}]})
        return {"title": one_line(str(s.get("title") or "Agent"), 60), "sections": sections, "refresh_after": REFRESH}

    def action(self, params: dict) -> dict:
        a = params.get("action")
        aid = str(a.get("id") if isinstance(a, dict) else a or "")
        if aid.startswith("agent:"):
            sid = aid[6:]
            board, missing = self._board()
            title = "Agent"
            if board is not None and sid in board.by_id:
                title = one_line(str(board.by_id[sid].get("title") or "Agent"), 40)
            return {"push": {"ui": PAGE, "state": state_text({"session": sid}), "title": title}}
        if aid == "back":
            return {"view": self.render(dict(params, state=""))}
        if aid == "go:agents":
            return {"push": {"ui": PAGE, "title": "Agent 进度"}}
        return {}
