"""Where a new /agent conversation starts when no computer is set (agent_machine empty) and agent_balance is on.

The assistant's own computer stays home: a new conversation starts there, exactly as before (no machine in the start),
unless that computer is busy right now. Then it goes to the least loaded other computer that is online and not busy
itself. Only a NEW conversation is placed: a follow-up goes to its conversation where it runs, and a new conversation
in a thread that already had one starts on that thread's computer again (its files and context are there), as long
as the thread's runs are kept (store.prune_runs: 3 days, 200 runs).

Busy: the 1-minute load is over BUSY_LOAD_PER_CORE times the computer's cores, less than MIN_FREE_BYTES of memory is
free, or BUSY_WORKING or more of its sessions are working a turn: the lines Persona's own admission gate holds a busy
computer to (assistant.admission: load_per_core, min_memory_bytes, max_claude_procs), so a /agent spills where an
assistant task would. The working count is one read of the hub's session list (agents.read) for every computer at
once, hidden ones included; a session waiting for the user's answer is listed apart (needs_you) and not counted: it
waits without load, where the gate still counts it. That count is an extra line: unread (an older hub, no
agents.read, a list too long for one page, no time left) it is no count, never a busy one, and the load and memory
lines decide alone, as before it was read. Another computer is chosen only with both load and memory readings fresh
and none of the three busy; home without a load or memory reading stays home.
Nothing here ever stops a start, it only picks where: an error reading the computers or home, and a hub too slow
(BUDGET), is the old behaviour.
Pictures an Agent leaves are read on the extension's computer, taken to be the assistant's (as before placement): a
conversation placed elsewhere is told it cannot attach one. The launcher Persona gives `/agent` (its "Lark /agent"
setting) has to run on the chosen computer too: the built-in claude does everywhere.
A computer that cannot take an assistant conversation is never chosen: one without Claude Code, or one the list marks
(trust work or pending, an incompatible node); a work computer the list does not mark is not known here: with one,
set agent_machine or switch agent_balance off. A start that still fails there fails as any start does (never started
again elsewhere: an answer may be lost, and the request must not run twice). BUDGET bounds each request's waiting, not
a hub that answers in slow trickles.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("openmate.placement")

BUSY_LOAD_PER_CORE = 1.0
MIN_FREE_BYTES = 2 << 30
BUSY_WORKING = 8  # sessions working a turn at once
FRESH_SECONDS = 120.0  # a reading older than this (by its own time) is no reading
BUDGET = 8.0  # seconds placement may take in all: the request waits for it, and a restart meanwhile loses it
ASK_TIMEOUT = 4.0  # one hub request
WORKING_TIMEOUT = 2.0  # the session list: an extra line, it must not eat the readings' budget

Reading = Tuple[Optional[float], Optional[int]]  # (load per core, free memory bytes); either may be unknown


def reading(metrics: dict, now: Optional[float] = None) -> Optional[Reading]:
    """(load per core, free bytes) from a computer's metrics, either None when not read (Windows sends no load; a Mac
    sends its total before its first used-memory reading); None when the computer is offline, the sample is missing
    or stale, or neither is known."""
    if not isinstance(metrics, dict) or metrics.get("online") is False:
        return None
    latest, system = metrics.get("latest"), metrics.get("system")
    if not isinstance(latest, dict):
        return None
    try:
        t = float(latest.get("t") or 0)
    except (TypeError, ValueError):
        return None
    if t and abs((now or time.time()) - t) > FRESH_SECONDS:
        return None
    lpc: Optional[float] = None
    try:
        cores = int((system or {}).get("cores") or 0) if isinstance(system, dict) else 0
        load = latest.get("load")
        if cores > 0 and load is not None and float(load) >= 0:
            lpc = float(load) / cores
    except (TypeError, ValueError):
        lpc = None
    free: Optional[int] = None
    try:
        total, used = int(latest.get("mem_total") or 0), int(latest.get("mem_used") or 0)
        if total > 0 and used > 0:
            free = max(0, total - used)
    except (TypeError, ValueError):
        free = None
    if lpc is None and free is None:
        return None
    return lpc, free


def eligible(machine: dict) -> bool:
    """A listed computer the hub may start an assistant conversation on: with Claude Code (its node reported a
    version), and not one it says is a work computer, still pending, or running a node too old for it."""
    return (bool(machine.get("claude_version")) and str(machine.get("trust") or "personal") == "personal"
            and not machine.get("node_incompatible"))


def busy(r: Reading, working: Optional[int] = None) -> bool:
    lpc, free = r
    return ((lpc is not None and lpc > BUSY_LOAD_PER_CORE) or (free is not None and free < MIN_FREE_BYTES)
            or (working is not None and working >= BUSY_WORKING))


def _words(r: Reading, working: Optional[int] = None) -> str:
    lpc, free = r
    return "负载 %s/核，可用内存 %s，在干活的会话 %s" % (
        "%.2f" % lpc if lpc is not None else "未知",
        "%.1f GiB" % (free / float(1 << 30)) if free is not None else "未知",
        "%d 个" % working if working is not None else "未知")


def choose(hub, sticky: str = "", clock=time.monotonic) -> Tuple[str, str]:
    """(machine id to start on, "" for the assistant's own computer as before; why, for the log)."""
    deadline = clock() + BUDGET
    try:
        home = hub.assistant_machine(timeout=ASK_TIMEOUT)
        machines: List[dict] = hub.machines(timeout=ASK_TIMEOUT)
    except Exception as e:  # an older hub, or the extension without machines.read: where it always went
        return "", "读不到电脑列表（%s）：用助手所在的电脑" % (getattr(e, "code", "") or e.__class__.__name__)
    online = [str(m["id"]) for m in machines if m.get("online") and eligible(m)]

    def metrics(mid: str) -> Optional[dict]:
        left = deadline - clock()
        if left < 0.5:  # not worth asking: the budget is spent
            return None
        try:
            return hub.machine_metrics(mid, timeout=min(ASK_TIMEOUT, left))
        except Exception:
            return None

    def read(mid: str) -> Optional[Reading]:
        m = metrics(mid)
        return reading(m) if m is not None else None

    def working_counts() -> Optional[Dict[str, int]]:
        left = deadline - clock()
        if left < 0.5:
            return None
        try:
            return hub.working_sessions(timeout=min(WORKING_TIMEOUT, left))
        except Exception:  # an older hub, no agents.read, a list too long, a slow hub: the other lines alone
            return None

    if not home:
        return "", "不知道助手在哪台电脑上：照旧交给 Persona"
    home_m = metrics(home)  # (home by any id the hub knows it by: an id kept after two entries were merged is unlisted)
    home_now = str((home_m or {}).get("machine_id") or "")  # its current id, when the setting names an older one
    if sticky:
        if sticky in (home, home_now):
            return "", "这个话题之前就在助手所在的电脑上：还开在那里"
        if sticky in online:
            return sticky, "这个话题之前就在 %s 上：还开在那台电脑上" % sticky
        m = metrics(sticky) or {}  # an id kept after two entries were merged: the hub answers for its current id
        now_id = str(m.get("machine_id") or "")
        if m.get("online") is True and now_id and now_id != sticky and now_id in online:
            return ("" if now_id in (home, home_now) else now_id), \
                "这个话题之前就在 %s（现在叫 %s）上：还开在那台电脑上" % (sticky, now_id)
        log.info("/agent: the thread's computer %s is offline: placed anew", sticky)
    here = reading(home_m) if home_m is not None else None
    if here is None:
        return "", "%s 没有最近的负载读数（或不在线）：照旧用它" % home
    working = working_counts()  # every computer's, one read: a computer missing from a read list has none working

    def at(mid: str) -> Optional[int]:
        return None if working is None else working.get(mid, 0)

    here_working = None if working is None else max(working.get(home, 0), working.get(home_now or home, 0))
    if not busy(here, here_working):
        return "", "%s 不忙（%s）" % (home, _words(here, here_working))
    best: Optional[Tuple[float, str, Reading]] = None
    for mid in online:
        if mid in (home, home_now):
            continue
        r = read(mid)
        if r is None or r[0] is None or r[1] is None or busy(r, at(mid)):
            continue  # another computer only with both readings, none of its lines busy
        if best is None or r[0] < best[0]:
            best = (r[0], mid, r)
    if best is None:
        return "", "%s 忙（%s），别的电脑也都忙、没有读数或来不及读：还用它" % (home, _words(here, here_working))
    return best[1], "%s 忙（%s）：开到 %s（%s）" % (home, _words(here, here_working), best[1],
                                                 _words(best[2], at(best[1])))
