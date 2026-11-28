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
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from openmate_core.ui import Fresh, NotYet, Page, empty, row, stat, tile, when

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
ACCOUNTS_MOST = 40  # accounts listed on the page (four items each at most): a page holds 300
ACCOUNT_PAGES = 10  # pages of PAGE_SIZE accounts read at most (1000 accounts)
PAGE_SIZE = 100  # the most the read-only key gets a page (StarkMesh answers a larger page_size with 20)
USAGE_BATCH = 100  # the most accounts the read-only key may ask the usage of at once (more: HTTP 400)
PAGE_WAIT = 12.0  # the page waits this long at most for a first read; the 今天 page's block CARD_WAIT
CARD_WAIT = 2.0
SUBSCRIPTION_TYPES = ("oauth", "setup-token")  # the account types with 5 小时 and weekly windows (API Key, Bedrock, Vertex: none)
USAGE_ID = "starkmesh"  # the usage contribution (manifest contributes.usage): the sidebar's usage bars
USAGE_WAIT = 20.0  # usage.query waits this long at most for a first read (the manager gives a source 45 s)
USAGE_POOL = "starkmesh-pool"  # the one account it answers: the pool, not each account (below)
USAGE_WINDOWS = (("five_hour", "5h", ""), ("seven_day", "7d", ""), ("seven_day_fable", "7d", "Fable"))


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
        if _origin(req.full_url) is None or _origin(req.full_url) != _origin(new.full_url):
            log.warning("StarkMesh redirected elsewhere: not followed, the key stays here")
            return None
        key = req.get_header("X-api-key")  # only the key: the library makes the rest (a 303 has no body)
        if key:
            new.add_unredirected_header("X-api-key", key)
        return new


def _origin(url: str):
    """(scheme, host, port) with the default port filled in; None for one that cannot be read."""
    try:
        u = urllib.parse.urlsplit(url)
        return u.scheme, (u.hostname or "").lower(), u.port or {"http": 80, "https": 443}.get(u.scheme)
    except ValueError:
        return None


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


def _iso(t: float, up: bool = False) -> str:
    """t in UTC to the second; up: rounded up (an end said early would drop a window that is still on)."""
    t = math.ceil(t) if up else math.floor(t)
    return _dt.datetime.fromtimestamp(t, _dt.timezone.utc).isoformat().replace("+00:00", "Z")


_FRACTION = re.compile(r"(T\d\d:\d\d:\d\d)\.(\d+)")


def _ts(v) -> Optional[float]:
    if not v:
        return None
    # Python before 3.11 reads only 3 or 6 fractional digits; the gateway may write another number of them
    text = _FRACTION.sub(lambda m: m.group(1) + "." + (m.group(2) + "000000")[:6], str(v).replace("Z", "+00:00"), count=1)
    try:
        return _dt.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


WINDOW_SECONDS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400, "seven_day_fable": 7 * 86400}


def renewed(u: dict, key: str, t: Optional[float], now: float) -> bool:
    """Whether window key (reset time t, or None) has started again since its number. StarkMesh keeps one sample time
    for the account (updated_at, whole seconds), not one per window, and may update a window's number without its reset
    time. So: once the period the sample was taken in must have ended, it has; with a passed reset, also when the
    sample was taken before the reset's second (or not said when). A sample in or after the reset's second may be the
    new period's: kept, never shown less used than it may be."""
    sampled = _ts(u.get("updated_at"))
    over = bool(sampled) and now >= sampled + 1 + WINDOW_SECONDS.get(key, 0)  # (+1: the second's lost fraction)
    if not t:  # no end said (the gateway drops a 5 小时 end once it is over, or never had one): kept until surely over
        return over
    if t > now:
        return False
    return not sampled or sampled < math.floor(t) or over


def reset_of(a: dict, key: str, w: dict) -> Optional[float]:
    """When window key of account a resets (epoch seconds), or None. StarkMesh's passive snapshot may leave a 5 小时
    window's resets_at out while the account list still has the session window's end."""
    t = _ts(w.get("resets_at"))
    if not t and key == "five_hour":
        t = _ts(a.get("session_window_end"))
    return t


def expired(a: dict, now: float) -> bool:
    """The account's credentials have expired and StarkMesh pauses it for that (auto_pause_on_expired; expires_at): it
    refuses the account at once, before its pause worker sets schedulable off. Only when the flag is said: the read-only
    key's answer leaves it out, and an account with it off is still scheduled (not said: the pause worker's
    schedulable decides, a little later)."""
    if not a.get("auto_pause_on_expired"):
        return False
    v = a.get("expires_at")
    t = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else _ts(v)
    return bool(t) and now >= t


FABLE_MODEL = "claude-fable-5"  # StarkMesh's scope for Fable in an account's extra.model_rate_limits


def fable_blocked(a: dict, now: float) -> bool:
    """StarkMesh keeps the account but sends it no Fable until then (extra.model_rate_limits, e.g. credits required)."""
    limits = (a.get("extra") or {}).get("model_rate_limits") if isinstance(a.get("extra"), dict) else None
    hold = limits.get(FABLE_MODEL) if isinstance(limits, dict) else None
    t = _ts(hold.get("rate_limit_reset_at")) if isinstance(hold, dict) else None
    return bool(t) and t > now


def usable(a: dict, now: float) -> bool:
    """An account the gateway would schedule now."""
    if str(a.get("status") or "") != "active" or not a.get("schedulable", True):
        return False
    for f in ("rate_limit_reset_at", "overload_until", "temp_unschedulable_until"):
        t = _ts(a.get(f))
        if t and t > now:
            return False
    return True


def pool_usable(a: dict, now: float) -> bool:
    return usable(a, now) and not expired(a, now)


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

    def __init__(self, accounts: List[dict], usage: Dict[str, dict], stats: dict, now: float, stats_failed: str = ""):
        self.accounts, self.usage, self.stats, self.now = accounts, usage, stats or {}, now
        self.stats_failed = stats_failed
        self.claude = [a for a in accounts if str(a.get("platform") or "") == "anthropic"]
        self.ok = [a for a in accounts if usable(a, now)]
        self.ok_claude = [a for a in self.claude if usable(a, now)]

    def window(self, key: str, at: Optional[float] = None, keep: Optional[Callable[[dict], bool]] = None) -> Optional[dict]:
        """{left: 号 left, of: accounts counted, ratio, reset: seconds to the soonest reset, last: to the latest} or None
        (no snapshot), as of at (default: when it was read). A window whose reset time has passed has started again:
        nothing of it is used (the gateway's snapshot is passive, so it still has the last period's number until the
        account is used again)."""
        now = self.now if at is None else at
        left, of, resets, open_ended = 0.0, 0, [], False
        counted = self.ok_claude if at is None else [a for a in self.claude if usable(a, now)]
        for a in counted if keep is None else [a for a in counted if keep(a)]:
            u = self.usage.get(str(a.get("id"))) or {}
            w = u.get(key)
            if not isinstance(w, dict):
                continue
            util = float(w.get("utilization") or 0.0)
            t = reset_of(a, key, w)
            if not t and w.get("remaining_seconds"):
                t = self.now + float(w["remaining_seconds"])  # counted from the read
            if renewed(u, key, t, now):
                util = 0.0
            elif t and t <= now:
                t = None  # sampled after that reset: the number is kept, its end not known
            left += max(0.0, 1.0 - min(util, 100.0) / 100.0)
            of += 1
            if t and t > now:
                resets.append(t - now)
            elif not t and util > 0:
                open_ended = True  # used, with no reset said: the pool's window has no known end
        if not of:
            return None
        return {"left": left, "of": of, "ratio": left / of, "reset": min(resets) if resets else None,
                "last": max(resets) if resets and not open_ended else None,
                "dated": len(resets) == of}  # every counted account has a reset ahead (none renewed, none undated)


def tone_of(ratio: float) -> Tuple[str, str]:
    """(color, tone) of what is left: green from half, amber from a fifth, red below."""
    if ratio >= 0.5:
        return "green", "positive"
    if ratio >= 0.2:
        return "amber", "caution"
    return "red", "danger"


class PoolUsage:
    """The sidebar's pool measurements, before formatting them for the UI or an alert."""

    def __init__(self, snap: Snapshot, now: float):
        self.ok = [a for a in snap.claude if pool_usable(a, now)]
        self.subs = [a for a in self.ok if str(a.get("type") or "") in SUBSCRIPTION_TYPES]
        self.total = getattr(snap, "total", len(snap.accounts))
        self.partial = self.total > len(snap.accounts) or getattr(snap, "cut", False)
        failed = getattr(snap, "usage_failed", ())
        self.missing = sum(str(a.get("id")) in failed for a in self.subs)
        self.windows: Dict[str, dict] = {}
        self.cover: Dict[str, int] = {}
        if self.partial or self.missing:
            return

        def live(a):
            return not expired(a, now)

        def live_fable(a):
            return live(a) and not fable_blocked(a, now)

        for key, _, _ in USAGE_WINDOWS:
            w = snap.window(key, now, live_fable if key == "seven_day_fable" else live)
            counted = w["of"] if w else 0
            if key != "seven_day_fable" and counted < len(self.subs):
                self.cover[key] = len(self.subs) - counted  # the missing accounts might be the least used
            elif w:
                w["used_percent"] = 100.0 * (1.0 - min(1.0, max(0.0, w["ratio"])))
                self.windows[key] = w


class MeshFresh(Fresh):
    def current(self, stop, failure) -> Snapshot:
        """An observer needs the outcome of the read, even when a page can show the older value."""
        while not stop.is_set():
            if not self._reading.acquire(timeout=0.25):
                continue
            try:
                if stop.is_set():
                    break
                failed = failure()
                if failed is not None and self.clock() - failed[1] < self.ahead:
                    raise failed[2].with_traceback(None)
                k = self.key()
                with self._mu:
                    if self._usable(k, self.ahead):
                        return self._kept[2]
                value = self.read()
                self._keep(k, value)
                return value
            finally:
                self._reading.release()
        raise NotYet(self.name)


class StarkMesh:
    """The gateway's reads, kept fresh behind the pages."""

    def __init__(self, app, api: Optional[StarkMeshAPI] = None, clock: Callable[[], float] = time.time,
                 spawn: Optional[Callable] = None):
        self.app = app
        self.api = api or StarkMeshAPI(app)
        self.clock = clock
        self._fresh = MeshFresh(self._read, AHEAD, STALE, clock=clock, spawn=spawn, name="StarkMesh")
        # (the newest read that came in, when a read last failed or None once one is in again), for usage.query: one
        # tuple, set at once and read at once, so a read coming in between never pairs the old read with no failure
        self.state: Tuple[Optional[Snapshot], Optional[float]] = (None, None)
        self.failure: Optional[Tuple[float, float, Exception]] = None  # first, latest, error of consecutive failed reads

    @property
    def configured(self) -> bool:
        return bool(self.api.key and self.api.url)

    def warm(self) -> None:
        if self.configured:
            self._fresh.warm(every=KEEP, stop=getattr(self.app, "closing", None))

    def read(self, wait: Optional[float] = None, *, current: bool = False) -> Snapshot:
        try:
            if current:
                return self._fresh.current(self.app.closing, lambda: self.failure)
            return self._fresh.get(wait)
        except NotYet:
            raise StarkMeshError("正在读取 StarkMesh，稍后自动刷新", "loading")

    @property
    def failed_at(self) -> Optional[float]:
        return self.state[1]

    @property
    def last_read(self) -> Optional[Snapshot]:
        return self.state[0]

    def _read(self) -> Snapshot:
        # reads take turns (Fresh's lock), so the failure below changes a state no other read is changing
        try:
            snap = self._read_once()
        except Exception as e:
            at = self.clock()
            self.failure = (self.failure[0] if self.failure is not None else at, at, e.with_traceback(None))
            self.state = (self.state[0], at)
            raise
        self.failure = None
        self.state = (snap, None)
        return snap

    def _call(self, *args, **kwargs):
        closing = getattr(self.app, "closing", None)
        if closing is not None and closing.is_set():
            raise StarkMeshError("扩展正在退出", "loading")
        return self.api.call(*args, **kwargs)

    def _read_once(self) -> Snapshot:
        accounts: List[dict] = []
        seen = set()
        total = 0
        full = False  # whether the last page read was full (more may follow)
        for page in range(1, ACCOUNT_PAGES + 1):  # every page, each account once (a gateway may ignore page)
            data = self._call("GET", "/api/v1/admin/accounts", {"page": page, "page_size": PAGE_SIZE}) or {}
            items = [a for a in (data.get("items") or []) if isinstance(a, dict)] if isinstance(data, dict) else []
            full = len(items) >= PAGE_SIZE
            new = []
            for a in items:  # each id once, within a page too
                if str(a.get("id")) not in seen:
                    seen.add(str(a.get("id")))
                    new.append(a)
            accounts += new
            total = int(data.get("total") or 0) if isinstance(data, dict) else 0
            if not new or (total and len(accounts) >= total) or (not total and len(items) < PAGE_SIZE):
                break
        cut = not total and full  # the last page read was full (or repeated a full one) and no count said: maybe more
        ids = [int(a["id"]) for a in accounts if str(a.get("platform") or "") == "anthropic" and a.get("id") is not None]
        usage: Dict[str, dict] = {}
        failed_ids: set = set()
        missed = 0
        for i in range(0, len(ids), USAGE_BATCH):  # the usage of every account, a batch at a time; one that fails is passed
            try:
                got = self._call("POST", "/api/v1/admin/accounts/usage/batch",
                                 body={"account_ids": ids[i:i + USAGE_BATCH], "force": False, "refresh_claude": False}) or {}
            except StarkMeshError as e:
                if e.kind in ("auth", "loading") or i == 0 and len(ids) <= USAGE_BATCH:
                    raise
                missed += len(ids[i:i + USAGE_BATCH])
                failed_ids.update(str(x) for x in ids[i:i + USAGE_BATCH])
                if missed >= len(ids):  # not one batch came in: no usage at all is a failed read, not an empty one
                    raise
                continue
            usage.update({str(k): v for k, v in (got.get("usage") or {}).items() if isinstance(v, dict)})
            if isinstance(got.get("errors"), dict):  # a batch that answered may still miss some accounts
                failed_ids.update(str(k) for k in got["errors"])
        stats_failed = ""
        try:
            stats = self._call("GET", "/api/v1/admin/dashboard/stats") or {}
        except StarkMeshError as e:
            if e.kind == "loading":
                raise
            stats = {}
            if e.kind != "auth":  # a key without dashboard.read still shows the quota without a warning
                # The error message may contain the gateway URL or credentials echoed by the server.
                stats_failed = "offline" if e.kind == "offline" else "failed"
        snap = Snapshot(accounts, usage, stats if isinstance(stats, dict) else {}, self.clock(), stats_failed=stats_failed)
        snap.total = max(total, len(accounts))  # the gateway's count, when it said one beyond what was read
        snap.usage_missed = missed  # accounts whose usage a failed batch left out
        snap.usage_failed = failed_ids - set(usage)  # accounts the gateway could not say the usage of
        snap.cut = cut
        return snap


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

    def __init__(self, app, mesh: StarkMesh, alerts=None):
        self.app = app
        self.mesh = mesh
        self.alerts = alerts

    def warm(self) -> None:
        self.mesh.warm()
        if self.alerts is not None:
            self.alerts.start()

    def _alert_footer(self) -> List[dict]:
        note = self.alerts.caption() if self.alerts is not None else ""
        return [{"type": "text", "style": "caption", "text": note}] if note else []

    def _snapshot(self, version: int) -> Tuple[Optional[Snapshot], Optional[dict]]:
        if not self.mesh.configured:
            return None, empty("还没有接上 StarkMesh",
                               "在这个扩展的设置里填“StarkMesh 地址”（例如 %s）和“StarkMesh 只读管理密钥”（在 StarkMesh 后台 设置 › 管理密钥 新建，权限只勾 %s）" % (EXAMPLE_URL, PERMISSIONS),
                               version)
        try:
            return self.mesh.read(PAGE_WAIT), None
        except StarkMeshError as e:
            if e.kind == "loading":
                return None, empty("正在读取 StarkMesh", "稍后自动刷新", version)
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
            t = reset_of(a, key, w)
            if not t and w.get("remaining_seconds"):
                t = s.now + float(w["remaining_seconds"])
            if renewed(u, key, t, s.now):  # started again since the snapshot (Snapshot.window counts it so too)
                left, reset = 100.0, "已重置" if t else "未开始"
            elif t and t <= s.now:  # sampled after that reset: the number may be the new period's
                reset = "重置时间未知"
            elif t:
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
            return {"title": TITLE, "sections": [{"items": [problem] + self._alert_footer()}],
                    "refresh_after": 5 if "正在读取" in str(problem) else REFRESH}
        st = s.stats
        errors = [a for a in s.accounts if state_of(a, s.now)[1] == "danger"]
        heads = [("服务", "%d/%d" % (len(s.ok), len(s.accounts)), "可用账号 / 全部", "power", "green" if not errors else "amber"),
                 ("可用号", "%d/%d" % (len(s.ok_claude), len(s.claude)), "Claude 账号", "robot", "teal")]
        note = stats_notice(s, params.get("lang"))
        if not note:
            heads += [("今日请求", count(st.get("today_requests")), "RPM %s" % count(st.get("rpm")), "chart", "blue"),
                      ("今日 Token", count(st.get("today_tokens")), "费用 %s" % money(st.get("today_actual_cost", st.get("today_cost"))),
                       "money", "indigo")]
        if version >= 6:
            head = {"type": "grid", "max_columns": 4, "key": "heads",
                    "tiles": [tile(t, v, sub, icon, color, key="h:" + t) for t, v, sub, icon, color in heads]}
        else:
            head = {"type": "stat_grid", "stats": [stat(t, v, sub) for t, v, sub, _, _ in heads]}
        summary = [head]
        if note:
            summary.append({"type": "text", "style": "caption", "text": note})
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
        listed = sorted(s.accounts, key=lambda a: (order.get(state_of(a, s.now)[1], 9), str(a.get("name") or "")))
        for a in listed[:ACCOUNTS_MOST]:  # the ones with a problem first
            text, tone = state_of(a, s.now)
            sub = " · ".join(x for x in (str(a.get("platform") or ""), str(a.get("type") or ""),
                                          str(a.get("error_message") or "")[:60]) if x)
            rows.append(row(str(a.get("name") or a.get("id")), sub, "", key="a:%s" % a.get("id"),
                            badge={"text": text, "tone": tone}))
            rows += self.account_bars(a, s)
        total = getattr(s, "total", len(s.accounts))
        title = "账号 · %d" % len(s.accounts) if total <= len(s.accounts) else "账号 · 读到 %d / %d" % (len(s.accounts), total)
        accounts = {"title": title, "items": rows or [empty("没有账号", "", version)]}
        if len(listed) > ACCOUNTS_MOST:
            accounts["footer"] = "还有 %d 个账号（都能用的排在后面），完整列表在 StarkMesh 后台" % (len(listed) - ACCOUNTS_MOST)
        updated = "数据读于 %s" % when(s.now, self.app.tz)
        view = {"title": TITLE, "refresh_after": REFRESH,
                "sections": [{"items": summary}, quota, accounts,
                             {"items": [{"type": "text", "style": "caption", "text": updated}] + self._alert_footer()}]}
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
            s = self.mesh.read(CARD_WAIT)
        except StarkMeshError as e:
            if e.kind == "loading":
                return {"title": CARD_TITLE, "items": [empty("正在读取 StarkMesh", "稍后自动刷新", version)]}
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
        note = stats_notice(s)
        if note:
            items.append({"type": "text", "style": "caption", "text": note})
        sec: Dict[str, Any] = {"title": CARD_TITLE, "items": items}
        if links and version < 8:
            items.append(row("打开中转站 ›", "", "", "go:starkmesh", key="link:starkmesh"))
        return sec

    def status_tile(self) -> Optional[tuple]:
        """The 今天 page's status tile: usable accounts and a pill for how much is left (None until a key is set)."""
        if not self.mesh.configured:
            return None
        try:
            s = self.mesh.read(CARD_WAIT)
        except StarkMeshError as e:
            if e.kind == "loading":
                return ("中转站", "—", "正在读取", "gauge", "gray", {"text": "读取中", "tone": "neutral"}, "b:mesh")
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
        note = stats_notice(s)
        if note:
            lines.append(note)
        elif st:
            lines.append("今天：请求 %s，Token %s，费用 %s" % (count(st.get("today_requests")), count(st.get("today_tokens")),
                                                     money(st.get("today_actual_cost", st.get("today_cost")))))
        return "\n".join(lines)

    def usage(self, params: dict) -> dict:
        """usage.query (contributes.usage): the sidebar's usage bars. The pool is one Claude account whose windows are the
        page's 号 as a percentage: what the usable Claude accounts have used together. Not one row per account: the strip
        draws the two tightest accounts, which would be the two most used accounts, not what is left to work with.

        Whatever launcher the usage source names: Claude Code reaches StarkMesh outside Persona's launchers, so no
        launcher has its address; neither that launcher's gateway nor its key (usage.http) is used. The StarkMesh read is
        this extension's own, with its read-only admin key. A read that failed, or one that missed some accounts' usage,
        is an error, so the manager keeps the last answer, faded, rather than drawing a part of the pool as the whole."""
        from openmate_core.wire import RPCError

        say = _text(params.get("lang"))
        pool: Dict[str, Any] = {"id": USAGE_POOL, "provider": "claude", "name": say["name"]}
        if not self.mesh.configured:
            pool["notice"] = say["setup"]
            return {"accounts": [pool]}
        try:
            s = self.mesh.read(USAGE_WAIT)
        except StarkMeshError as e:
            raise RPCError(e.message, "busy" if e.kind == "loading" else "unavailable")
        newer, failed = self.mesh.state  # at once; reads take turns, so it is the newest that came in (not by the clock)
        if newer is not None:
            s = newer
        if failed is not None:  # the newest read failed: this one is what was, not what is (told by the state, not the clock)
            raise RPCError("StarkMesh 读不到，留着的是 %s 的数据" % when(s.now, self.app.tz), "unavailable")
        now = self.mesh.clock()  # as of this query (its clock, even one set back since the read): resets, rate limits
        measured = PoolUsage(s, now)
        ok = measured.ok
        pool["sampled_at"] = _iso(s.now)
        if measured.partial:  # a part of the pool is not the pool: no bars (the collapsed strip draws a bar without its words)
            partial = (say["partial"].format(read=len(s.accounts), total=measured.total) if measured.total > len(s.accounts)
                       else say["first"].format(read=len(s.accounts)))
            pool["headline"] = say["usable"].format(ok=len(ok), all=len(s.claude)) + partial
            pool["notice"] = say["incomplete"] + partial
            return {"accounts": [pool]}
        if measured.missing:
            raise RPCError("有 %d 个号的用量这次没读到" % measured.missing, "unavailable")
        tz = _zone(params.get("tz")) or self.app.tz
        windows, soonest = [], None
        cover = [say["cover"].format(window=say["w_" + key], miss=miss) for key, miss in measured.cover.items()]
        for key, label, model in USAGE_WINDOWS:
            w = measured.windows.get(key)
            if w is None:
                continue
            if key == "five_hour" and w["reset"]:
                soonest = now + w["reset"]
            note = say["left"].format(of=w["of"], left=w["left"])
            if w["reset"]:
                note += say["soonest"].format(at=clock_at(now + w["reset"], now, tz))
            out: Dict[str, Any] = {"label": label, "used_percent": w["used_percent"],  # unrounded:
                                   "note": note}  # the web's warning lines (5%, 20% left) are read from it
            if model:
                out["model"] = model
            if w["dated"] and w["last"] and w["last"] - w["reset"] <= 60:  # every counted account resets together: one end
                out["resets_at"] = _iso(now + w["last"], up=True)  # (staggered or undated: none; the headline: the soonest)
            windows.append(out)
        pool["headline"] = (say["usable"].format(ok=len(ok), all=len(s.claude))
                            + (say["soon5h"].format(at=clock_at(soonest, now, tz)) if soonest else "")
                            + "".join(cover))
        if windows:
            pool["windows"] = windows
        else:  # the sidebar shows the notice rather than the headline (with subscriptions, cover says which bars went)
            pool["notice"] = (say["no_claude"] if not s.claude else say["none_usable"] if not ok else
                              say["no_subscription"] if not measured.subs else say["gaps"] + "".join(cover))
        return {"accounts": [pool]}


def _zone(name):
    """The time zone of usage.query's tz (an IANA name), or None for none or one this Python does not know."""
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(str(name))
    except Exception:
        return None


def clock_at(t: float, now: float, tz) -> str:
    """t as a clock time in tz: 15:40 today, 11/27 15:40 another day. A time, not a countdown: the bars keep an answer
    until the next poll, and "in 1 minute" would be wrong by then."""
    at, today = _dt.datetime.fromtimestamp(t, tz), _dt.datetime.fromtimestamp(now, tz)
    return at.strftime("%H:%M") if at.date() == today.date() else at.strftime("%m/%d %H:%M")


def _text(lang) -> dict:
    return TEXT.get(str(lang or "").split("-")[0].lower(), TEXT["zh"])


def stats_notice(s: Snapshot, lang=None) -> str:
    if not s.stats_failed:
        return ""
    say = _text(lang)
    reason = say["stats_offline"] if s.stats_failed == "offline" else say["stats_error"]
    return say["stats_failed"].format(reason=reason)


# The usage bars and stats notice use the requested language (zh-Hans, en, ja; Chinese by default).
TEXT = {
    "zh": {"name": "Claude Code Max", "usable": "可用 Claude 号 {ok}/{all}", "partial": "（只读到 {read}/{total} 个号）",
           "stats_failed": "今日统计暂时读不到（{reason}）", "stats_offline": "连接失败或超时", "stats_error": "网关响应异常",
           "first": "（只读了前 {read} 个号）", "incomplete": "号没有读全，额度条不画，详情看中转站页",
           "left": "{of} 个可用号合计剩 {left:.2f} 号", "soonest": "，最早 {at} 有号重置",
           "setup": "还没有接上 StarkMesh：在飞书（OpenMate）扩展的设置里填 StarkMesh 地址和只读管理密钥",
           "no_claude": "读到的号里没有 Claude 号",
           "no_subscription": "能用的 Claude 号都不是订阅号（API Key、Bedrock、Vertex 这类），没有 5 小时和每周额度",
           "soon5h": "，5 小时最早 {at} 有号重置", "cover": "，{window}有 {miss} 个号没有快照，不画", "gaps": "有号还没有用量快照，额度条先不画", "w_five_hour": "5 小时", "w_seven_day": "每周",
           "none_usable": "Claude 号现在都用不了（限流、暂停调度、凭据过期或出错），详情看中转站页"},
    "en": {"name": "Claude Code Max", "usable": "{ok} of {all} Claude accounts usable", "partial": " (read {read} of {total} accounts)",
           "stats_failed": "Today's stats are temporarily unavailable ({reason})", "stats_offline": "connection failed or timed out",
           "stats_error": "unexpected gateway response",
           "first": " (read the first {read} accounts only)", "incomplete": "Not every account was read, so no bars: see the 中转站 page",
           "left": "{left:.2f} of {of} usable accounts left", "soonest": "; the first resets at {at}",
           "setup": "StarkMesh is not set up: add its address and read-only admin key in the OpenMate Feishu extension's settings",
           "no_claude": "No Claude account among the accounts read",
           "no_subscription": "The usable Claude accounts are no subscriptions (API Key, Bedrock, Vertex and the like): no 5-hour or weekly quota",
           "soon5h": "; first 5-hour reset at {at}", "cover": "; no {window} bar: {miss} without a snapshot", "gaps": "Some accounts have no usage snapshot yet, so no bars", "w_five_hour": "5-hour", "w_seven_day": "weekly",
           "none_usable": "No Claude account can be used now (rate-limited, paused, expired or failing): see the 中转站 page"},
    "ja": {"name": "Claude Code Max", "usable": "使える Claude アカウント {ok}/{all}", "partial": "（{read}/{total} 件だけ読めました）",
           "stats_failed": "今日の統計を一時的に取得できません（{reason}）", "stats_offline": "接続失敗またはタイムアウト",
           "stats_error": "ゲートウェイの応答異常",
           "first": "（最初の {read} 件だけ読みました）", "incomplete": "全アカウントを読めなかったのでバーは出しません。詳しくは 中转站 ページへ",
           "left": "使えるアカウント {of} 件で残り {left:.2f} 件分", "soonest": "、最初のリセットは {at}",
           "setup": "StarkMesh が未設定です：飛書（OpenMate）拡張の設定で StarkMesh のアドレスと読み取り専用の管理キーを入力してください",
           "no_claude": "読めたアカウントに Claude のものがありません",
           "no_subscription": "使える Claude アカウントはサブスクリプションではありません（API Key・Bedrock・Vertex など）。5 時間・週の枠がありません",
           "soon5h": "、5 時間枠の最初のリセットは {at}", "cover": "、{window}は {miss} 件のスナップショットがないので出しません", "gaps": "使用量スナップショットのないアカウントがあるのでバーは出しません", "w_five_hour": "5 時間枠", "w_seven_day": "週枠",
           "none_usable": "今使える Claude アカウントがありません（レート制限・停止・認証期限切れ・エラー）。詳しくは 中转站 ページへ"},
}

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
        note = stats_notice(s, params.get("lang"))
        if note:
            items.append({"type": "text", "style": "caption", "text": note})
        return {"sections": [{"items": items}], "refresh_after": REFRESH}
