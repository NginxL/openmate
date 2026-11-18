"""StarkMesh, the user's AI gateway: a dashboard of its accounts' quota, read through a scoped admin key the user made
(read only: accounts.read, accounts.usage.read, accounts.stats.read, dashboard.read). Nothing is ever changed there.

The key is the manifest's secret starkmesh_key; the address is starkmesh_url, which only the user writes (the hub lets
the program reach the address in that setting, so it has no default). Reads are kept fresh behind the page (Fresh): the accounts, their passive usage snapshots (never an active
refresh upstream) and the site's stats of today.

Quota is counted in 号, accounts' worth: for a window (5 小时, 本周, Fable 本周) the sum over usable Claude accounts of
what is left of it (1 - utilization). 13 usable accounts each half used are 6.5 号.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from openmate_core.ui import Fresh, Page, empty, row, stat, tile, when

log = logging.getLogger("openmate.starkmesh")

PAGE = "starkmesh"
TITLE = "中转站 · StarkMesh"  # the sidebar says 中转站 (the 资产 group of the user's console), the page names the gateway
CARD_TITLE = "中转站"
EXAMPLE_URL = "http://127.0.0.1:18080"  # the gateway on this computer's tunnel; the user writes it (a network permission)
WAIT = 10.0
AHEAD = 30.0
STALE = 600.0
KEEP = 300.0
REFRESH = 120
WINDOWS = (("five_hour", "5 小时"), ("seven_day", "本周"), ("seven_day_fable", "Fable 本周"))
ACCOUNT_WINDOWS = (("five_hour", "5 小时"), ("seven_day", "7D"), ("seven_day_fable", "Fable 7D"))  # an account's bars
PERMISSIONS = "accounts.read、accounts.usage.read、accounts.stats.read、dashboard.read"


class StarkMeshError(Exception):
    def __init__(self, message: str, kind: str = "failed"):
        super().__init__(message)
        self.message = message
        self.kind = kind


class SameOriginRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to the same scheme, host and port, with the key; any other one is an HTTP error. The key
    is an unredirected header, so it never rides along by itself."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        a, b = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(new.full_url)
        try:
            same = (a.scheme, a.hostname, a.port) == (b.scheme, b.hostname, b.port)
        except ValueError:
            same = False
        if not same:
            log.warning("StarkMesh redirected elsewhere: not followed, the key stays here")
            return None
        for k, v in req.unredirected_hdrs.items():
            new.add_unredirected_header(k, v)
        return new


_OPENER = urllib.request.build_opener(SameOriginRedirects)


class StarkMeshAPI:
    def __init__(self, app, urlopen: Optional[Callable] = None):
        self.app = app
        self._open = urlopen or _OPENER.open

    @property
    def url(self) -> str:
        return str(self.app.config.get("starkmesh_url") or "").strip().rstrip("/")

    @property
    def key(self) -> str:
        return str((getattr(self.app, "secrets", None) or {}).get("starkmesh_key") or "").strip()

    def call(self, method: str, path: str, query: Optional[dict] = None, body: Optional[dict] = None) -> Any:
        if not self.key or not self.url:
            raise StarkMeshError("还没有填 StarkMesh 的地址和只读管理密钥", "setup")
        url = self.url + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Accept": "application/json", "Content-Type": "application/json"})
        req.add_unredirected_header("x-api-key", self.key)  # to this address only (SameOriginRedirects)
        try:
            with self._open(req, timeout=WAIT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            if e.fp is not None:  # an HTTPError made without a body (3.9) has no file to close
                e.close()
            if e.code in (401, 403):
                raise StarkMeshError("StarkMesh 不认这个密钥，或它缺权限（要 %s）" % PERMISSIONS, "auth")
            raise StarkMeshError("StarkMesh 回答 HTTP %d" % e.code)
        except (urllib.error.URLError, OSError):
            raise StarkMeshError("连不上 StarkMesh（%s）：网关或隧道没开？" % self.url, "offline")
        try:
            d = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            raise StarkMeshError("StarkMesh 的回答看不懂")
        if isinstance(d, dict) and "data" in d and "code" in d:
            if d.get("code") not in (0, None):
                raise StarkMeshError("StarkMesh 拒绝了：%s" % str(d.get("message") or d.get("code"))[:80])
            return d.get("data")
        return d


def _ts(v) -> Optional[float]:
    if not v:
        return None
    try:
        return _dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def usable(a: dict, now: float) -> bool:
    """An account the gateway would schedule now."""
    if str(a.get("status") or "") != "active" or not a.get("schedulable", True):
        return False
    for f in ("rate_limit_reset_at", "overload_until", "temp_unschedulable_until"):
        t = _ts(a.get(f))
        if t and t > now:
            return False
    return True


def state_of(a: dict, now: float) -> Tuple[str, str]:
    """(what to say, tone) of an account."""
    if str(a.get("status") or "") == "error":
        return "出错", "danger"
    if str(a.get("status") or "") != "active":
        return "停用", "neutral"
    if not a.get("schedulable", True):
        return "暂停调度", "neutral"
    t = _ts(a.get("rate_limit_reset_at"))
    if t and t > now:
        return "限流中", "caution"
    for f in ("overload_until", "temp_unschedulable_until"):
        t = _ts(a.get(f))
        if t and t > now:
            return "暂不可用", "caution"
    return "可用", "positive"


def span(seconds: float) -> str:
    seconds = max(0, int(seconds))
    d, rest = divmod(seconds, 86400)
    h, m = rest // 3600, rest % 3600 // 60
    if d:
        return "%d 天 %d 小时" % (d, h)
    if h:
        return "%d 小时 %d 分" % (h, m)
    return "%d 分" % max(1, m)


class Snapshot:
    """One read of the gateway: its accounts, their usage, today's stats; and the quota counted in 号."""

    def __init__(self, accounts: List[dict], usage: Dict[str, dict], stats: dict, now: float):
        self.accounts, self.usage, self.stats, self.now = accounts, usage, stats or {}, now
        self.claude = [a for a in accounts if str(a.get("platform") or "") == "anthropic"]
        self.ok = [a for a in accounts if usable(a, now)]
        self.ok_claude = [a for a in self.claude if usable(a, now)]

    def window(self, key: str) -> Optional[dict]:
        """{left: 号 left, of: accounts counted, ratio, reset: seconds to the soonest reset} or None (no snapshot)."""
        left, of, resets = 0.0, 0, []
        for a in self.ok_claude:
            w = (self.usage.get(str(a.get("id"))) or {}).get(key)
            if not isinstance(w, dict):
                continue
            util = float(w.get("utilization") or 0.0)
            left += max(0.0, 1.0 - min(util, 100.0) / 100.0)
            of += 1
            t = _ts(w.get("resets_at"))
            if t and t > self.now:
                resets.append(t - self.now)
            elif w.get("remaining_seconds"):
                resets.append(float(w["remaining_seconds"]))
        if not of:
            return None
        return {"left": left, "of": of, "ratio": left / of, "reset": min(resets) if resets else None}


def tone_of(ratio: float) -> Tuple[str, str]:
    """(color, tone) of what is left: green from half, amber from a fifth, red below."""
    if ratio >= 0.5:
        return "green", "positive"
    if ratio >= 0.2:
        return "amber", "caution"
    return "red", "danger"


class StarkMesh:
    """The gateway's reads, kept fresh behind the pages."""

    def __init__(self, app, api: Optional[StarkMeshAPI] = None, clock: Callable[[], float] = time.time,
                 spawn: Optional[Callable] = None):
        self.app = app
        self.api = api or StarkMeshAPI(app)
        self.clock = clock
        self._fresh = Fresh(self._read, AHEAD, STALE, clock=clock, spawn=spawn, name="StarkMesh")

    @property
    def configured(self) -> bool:
        return bool(self.api.key and self.api.url)

    def warm(self) -> None:
        if self.configured:
            self._fresh.warm(every=KEEP, stop=getattr(self.app, "closing", None))

    def read(self) -> Snapshot:
        return self._fresh.get()

    def _read(self) -> Snapshot:
        data = self.api.call("GET", "/api/v1/admin/accounts", {"page": 1, "page_size": 200}) or {}
        accounts = [a for a in (data.get("items") or []) if isinstance(a, dict)] if isinstance(data, dict) else []
        ids = [int(a["id"]) for a in accounts if str(a.get("platform") or "") == "anthropic" and a.get("id") is not None]
        usage: Dict[str, dict] = {}
        if ids:
            got = self.api.call("POST", "/api/v1/admin/accounts/usage/batch",
                                body={"account_ids": ids[:200], "force": False, "refresh_claude": False}) or {}
            usage = {str(k): v for k, v in (got.get("usage") or {}).items() if isinstance(v, dict)}
        try:
            stats = self.api.call("GET", "/api/v1/admin/dashboard/stats") or {}
        except StarkMeshError as e:
            if e.kind != "auth":  # a key without dashboard.read still shows the quota
                raise
            stats = {}
        return Snapshot(accounts, usage, stats if isinstance(stats, dict) else {}, self.clock())


def money(v) -> str:
    try:
        return "$%.2f" % float(v)
    except (TypeError, ValueError):
        return "—"


def count(v) -> str:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "—"
    for unit, size in (("亿", 1e8), ("万", 1e4)):
        if n >= size:
            return "%.1f%s" % (n / size, unit)
    return "%d" % n


class StarkMeshPage(Page):
    """The StarkMesh dashboard (a page of its own); card() is its block on the 今天 page."""

    def __init__(self, app, mesh: StarkMesh):
        self.app = app
        self.mesh = mesh

    def warm(self) -> None:
        self.mesh.warm()

    def _snapshot(self, version: int) -> Tuple[Optional[Snapshot], Optional[dict]]:
        if not self.mesh.configured:
            return None, empty("还没有接上 StarkMesh",
                               "在这个扩展的设置里填“StarkMesh 地址”（例如 %s）和“StarkMesh 只读管理密钥”（在 StarkMesh 后台 设置 › 管理密钥 新建，权限只勾 %s）" % (EXAMPLE_URL, PERMISSIONS),
                               version)
        try:
            return self.mesh.read(), None
        except StarkMeshError as e:
            return None, empty("StarkMesh 读不到", e.message, version)

    def quota_tiles(self, s: Snapshot, version: int) -> List[dict]:
        """The quota left per window: from UI version 9 a ring (Tile.gauge) filled to what is left, the 号 in its centre;
        before, a tile saying it."""
        out = []
        for key, label in WINDOWS:
            w = s.window(key)
            if w is None:
                continue
            color, _ = tone_of(w["ratio"])
            reset = "%s后重置" % span(w["reset"]) if w["reset"] else "重置时间未知"
            if version >= 9:
                t = tile(label, "%.2f" % w["left"], reset, "", color, key="q:" + key)
                t["gauge"] = {"ratio": round(min(1.0, max(0.0, w["ratio"])), 4), "unit": "号", "color": color}
            else:
                t = tile(label, "%.2f 号" % w["left"], reset, "gauge", color, key="q:" + key)
            out.append(t)
        return out

    def quota_bars(self, s: Snapshot) -> List[dict]:
        out = []
        for key, label in WINDOWS:
            w = s.window(key)
            if w is None:
                continue
            _, tone = tone_of(w["ratio"])
            reset = "，%s后重置" % span(w["reset"]) if w["reset"] else ""
            out.append({"type": "progress", "key": "p:" + key, "label": "%s · 剩 %.2f 号" % (label, w["left"]),
                        "ratio": round(min(1.0, w["ratio"]), 4), "detail": "%d 个号里算%s" % (w["of"], reset), "tone": tone})
        return out

    def account_bars(self, a: dict, s: Snapshot) -> List[dict]:
        """An account's windows as the user's screenshot has them: what is left (not what is used), a bar, and when it
        resets ("5 小时 ▬▬▬ 100% 11/16 15:40")."""
        u = s.usage.get(str(a.get("id"))) or {}
        out = []
        for key, label in ACCOUNT_WINDOWS:
            w = u.get(key)
            if not isinstance(w, dict):
                continue
            left = max(0.0, min(100.0, 100.0 - float(w.get("utilization") or 0.0)))
            t = _ts(w.get("resets_at"))
            if not t and w.get("remaining_seconds"):
                t = s.now + float(w["remaining_seconds"])
            if t:
                reset = _dt.datetime.fromtimestamp(t, self.app.tz).strftime("%m/%d %H:%M")
            else:  # a window with no reset time has not started: nothing used in it yet
                reset = "未开始" if left >= 100.0 else "重置时间未知"
            out.append({"type": "progress", "key": "a:%s:%s" % (a.get("id"), key), "label": label,
                        "ratio": round(left / 100.0, 4), "detail": "剩 %d%% · %s" % (round(left), reset),
                        "tone": tone_of(left / 100.0)[1]})
        return out

    def render(self, params: dict) -> dict:
        version = int(params.get("ui_version") or 1)
        s, problem = self._snapshot(version)
        if s is None:
            return {"title": TITLE, "sections": [{"items": [problem]}], "refresh_after": REFRESH}
        st = s.stats
        errors = [a for a in s.accounts if state_of(a, s.now)[1] == "danger"]
        heads = [("服务", "%d/%d" % (len(s.ok), len(s.accounts)), "可用账号 / 全部", "power", "green" if not errors else "amber"),
                 ("可用号", "%d/%d" % (len(s.ok_claude), len(s.claude)), "Claude 账号", "robot", "teal"),
                 ("今日请求", count(st.get("today_requests")), "RPM %s" % count(st.get("rpm")), "chart", "blue"),
                 ("今日 Token", count(st.get("today_tokens")), "费用 %s" % money(st.get("today_actual_cost", st.get("today_cost"))),
                  "money", "indigo")]
        if version >= 6:
            head = {"type": "grid", "max_columns": 4, "key": "heads",
                    "tiles": [tile(t, v, sub, icon, color, key="h:" + t) for t, v, sub, icon, color in heads]}
        else:
            head = {"type": "stat_grid", "stats": [stat(t, v, sub) for t, v, sub, _, _ in heads]}
        quota: Dict[str, Any] = {"title": "Claude 额度", "items": []}
        if version >= 6:
            tiles = self.quota_tiles(s, version)
            if tiles:
                quota["items"].append({"type": "grid", "max_columns": 3, "key": "quota", "tiles": tiles})
        bars = self.quota_bars(s)
        if version < 9 or not bars:  # the rings say it from UI version 9
            quota["items"] += bars or [empty("还没有额度快照", "账号用过之后 StarkMesh 才有 5 小时和每周的用量", version)]
        resets = [w["reset"] for w in (s.window(k) for k, _ in WINDOWS) if w and w["reset"]]
        if resets:
            quota["footer"] = "最近一次重置在 %s后" % span(min(resets))
        rows = []
        order = {"danger": 0, "caution": 1, "neutral": 2, "positive": 3}
        for a in sorted(s.accounts, key=lambda a: (order.get(state_of(a, s.now)[1], 9), str(a.get("name") or ""))):
            text, tone = state_of(a, s.now)
            sub = " · ".join(x for x in (str(a.get("platform") or ""), str(a.get("type") or ""),
                                          str(a.get("error_message") or "")[:60]) if x)
            rows.append(row(str(a.get("name") or a.get("id")), sub, "", key="a:%s" % a.get("id"),
                            badge={"text": text, "tone": tone}))
            rows += self.account_bars(a, s)
        accounts = {"title": "账号 · %d" % len(s.accounts), "items": rows or [empty("没有账号", "", version)]}
        updated = "数据读于 %s" % when(s.now, self.app.tz)
        view = {"title": TITLE, "refresh_after": REFRESH,
                "sections": [{"items": [head]}, quota, accounts, {"items": [{"type": "text", "style": "caption", "text": updated}]}]}
        if version >= 8:
            view["layout"] = "columns"
            view["sections"][0]["span"] = "full"
            quota.update(icon="gauge", icon_color="teal")
            accounts.update(icon="list", icon_color="blue")
            view["sections"][3]["span"] = "full"
        return view

    def card(self, version: int, links: bool, icons: bool) -> Optional[dict]:
        """The 今天 page's StarkMesh block: None until a key is set."""
        if not self.mesh.configured:
            return None
        try:
            s = self.mesh.read()
        except StarkMeshError as e:
            return {"title": CARD_TITLE, "items": [empty("StarkMesh 读不到", e.message, version)]}
        parts = ["可用号 %d/%d" % (len(s.ok_claude), len(s.claude))]
        for key, label in WINDOWS[:2]:
            w = s.window(key)
            if w is not None:
                parts.append("%s剩 %.2f 号" % (label, w["left"]))
        items: List[dict] = [{"type": "text", "style": "caption", "text": " · ".join(parts)}]
        if icons:
            tiles = self.quota_tiles(s, version)
            if tiles:
                items.append({"type": "grid", "max_columns": 3, "key": "sm:quota", "tiles": tiles})
        else:
            items += self.quota_bars(s)
        sec: Dict[str, Any] = {"title": CARD_TITLE, "items": items}
        if links and version < 8:
            items.append(row("打开中转站 ›", "", "", "go:starkmesh", key="link:starkmesh"))
        return sec

    def status_tile(self) -> Optional[tuple]:
        """The 今天 page's status tile: usable accounts and a pill for how much is left (None until a key is set)."""
        if not self.mesh.configured:
            return None
        try:
            s = self.mesh.read()
        except StarkMeshError:
            return ("中转站", "—", "读不到", "gauge", "gray", {"text": "读不到", "tone": "caution"}, "b:mesh")
        word, tone = standing(s)
        w = s.window(WINDOWS[0][0])
        line = "5 小时剩 %.2f 号" % w["left"] if w is not None else "Claude 账号"
        color = {"positive": "green", "caution": "amber", "danger": "red"}[tone]
        return ("中转站", "%d/%d" % (len(s.ok_claude), len(s.claude)), line, "gauge", color, {"text": word, "tone": tone}, "b:mesh")

    def action(self, params: dict) -> dict:
        return {}

    def tools(self) -> Dict[str, Tuple[dict, Callable[[dict], str]]]:
        """The assistant's read of the quota (the morning brief says how much is left), from the page's own fresh read."""
        name = self.app.cid + "_starkmesh"
        return {name: ({"name": name, "description": "StarkMesh 中转站的 Claude 额度：总体充足 / 偏紧 / 快用完，可用号几个，"
                                                      "5 小时、本周、Fable 本周各剩多少号、多久后重置，哪些号不可用、什么时候恢复，"
                                                      "以及今天的请求数、Token 和费用。只读，不会改 StarkMesh 的任何东西。",
                        "input_schema": {"type": "object", "properties": {}}}, self.brief)}

    def brief(self, _args: dict) -> str:
        if not self.mesh.configured:
            return "还没有接上 StarkMesh：OpenMate 飞书扩展的设置里还没填 StarkMesh 地址和只读管理密钥。"
        try:
            s = self.mesh.read()
        except StarkMeshError as e:
            return "StarkMesh 读不到：" + e.message
        word, _ = standing(s)
        lines = ["StarkMesh 额度：%s（数据读于 %s）" % (word, when(s.now, self.app.tz)),
                 "可用 Claude 号 %d/%d" % (len(s.ok_claude), len(s.claude))]
        for key, label in WINDOWS:
            w = s.window(key)
            if w is not None:
                reset = "，%s后重置" % span(w["reset"]) if w["reset"] else ""
                lines.append("%s：剩 %.2f 号（%d 个可用号满额 %d 号）%s" % (label, w["left"], w["of"], w["of"], reset))
        for a in s.claude:
            if usable(a, s.now):
                continue
            text, _ = state_of(a, s.now)
            back = [t for t in (_ts(a.get(f)) for f in ("rate_limit_reset_at", "overload_until", "temp_unschedulable_until"))
                    if t and t > s.now]
            lines.append("不可用：%s，%s%s" % (a.get("name") or a.get("id"), text, "，%s后恢复" % span(max(back) - s.now) if back else ""))
        st = s.stats
        if st:
            lines.append("今天：请求 %s，Token %s，费用 %s" % (count(st.get("today_requests")), count(st.get("today_tokens")),
                                                     money(st.get("today_actual_cost", st.get("today_cost")))))
        return "\n".join(lines)


CARD = "starkmesh-card"


def standing(s: "Snapshot") -> Tuple[str, str]:
    """(word, tone) of the quota as a whole, by the 5 小时 window (else 本周): what the Overview card's pill says."""
    for key, _ in WINDOWS[:2]:
        w = s.window(key)
        if w is not None:
            _, tone = tone_of(w["ratio"])
            return {"positive": "充足", "caution": "偏紧", "danger": "快用完"}[tone], tone
    return ("没有可用号", "danger") if not s.ok_claude else ("正常", "positive")


class StarkMeshCard(Page):
    """The Overview card (overview_card): usable accounts, a pill for how much is left, then each window's quota (rings
    from UI version 9, tiles before, bars before 6) and when it resets."""

    def __init__(self, page: StarkMeshPage):
        self.page = page

    def warm(self) -> None:
        self.page.warm()

    def render(self, params: dict) -> dict:
        version = int(params.get("ui_version") or 1)
        s, note = self.page._snapshot(version)
        if s is None:
            return {"sections": [{"items": [note]}], "refresh_after": REFRESH}
        word, tone = standing(s)
        head: Dict[str, Any] = {"type": "row", "key": "sm:head", "title": "可用号 %d/%d" % (len(s.ok_claude), len(s.claude)),
                                "subtitle": "Claude 账号 · 数据读于 %s" % when(s.now, self.page.app.tz),
                                "badge": {"text": word, "tone": tone}}
        if version >= 6:
            head.update(icon="gauge", icon_color={"positive": "green", "caution": "amber", "danger": "red"}[tone])
        items: List[dict] = [head]
        if version >= 6:
            tiles = self.page.quota_tiles(s, version)
            if tiles:
                items.append({"type": "grid", "max_columns": 3, "key": "sm:quota", "tiles": tiles})
        else:
            items += self.page.quota_bars(s)
        return {"sections": [{"items": items}], "refresh_after": REFRESH}
