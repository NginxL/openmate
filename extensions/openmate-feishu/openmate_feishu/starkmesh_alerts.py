"""Background alerts for the same StarkMesh pool the sidebar shows."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path

from openmate_core.hub import HubError

from . import starkmesh

log = logging.getLogger("openmate.starkmesh.alerts")

CHECK_EVERY = 300.0
DOWN_AFTER = 15 * 60.0
HOUR = 3600.0
KEY_COOLDOWN = 6 * HOUR
HOURLY_MOST = 6
NOTIFY_WAIT = 10.0
STATE_FILE = "starkmesh-alerts.json"
LOW = {"five_hour": "sm-low-5h", "seven_day": "sm-low-7d", "seven_day_fable": "sm-low-fable"}
ACCOUNT, NONE, DOWN = "sm-acct", "sm-none", "sm-down"
LABELS = {LOW[key]: "%s额度低" % label for key, label in starkmesh.WINDOWS}
LABELS.update({ACCOUNT: "Claude 号出错", NONE: "没有可用的 Claude 号", DOWN: "读不到 StarkMesh"})


def _empty_state() -> dict:
    return {"version": 1, "checked_at": None, "failed_since": None, "active": {}, "last_sent": {}, "accounts": {}}


def _number(value) -> bool:
    return type(value) in (int, float) and 0 <= value < float("inf")


class StarkMeshAlerts:
    def __init__(self, app, mesh: starkmesh.StarkMesh):
        self.app, self.mesh = app, mesh
        data_dir = getattr(app, "data_dir", "")
        self.path = Path(data_dir) / STATE_FILE if data_dir else None
        self._errors: dict = {}
        self._state = self._load()
        self._status = self._summary()
        self._start_lock = threading.Lock()
        self._thread = None

    @property
    def enabled(self) -> bool:
        return self.app.config.get("starkmesh_alerts") is True

    def _running(self) -> bool:
        return self.enabled and not self.app.closing.is_set() and self.mesh.configured

    def start(self) -> None:
        with self._start_lock:
            if not self._running() or self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="openmate-starkmesh-alerts", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while self._running():
            next_check = time.monotonic() + CHECK_EVERY
            try:
                self.check()
            except Exception as e:
                self._log_error("check", type(e).__name__)
            else:
                self._errors.pop("check", None)
            if self.app.closing.wait(max(0.0, next_check - time.monotonic())):
                return

    def caption(self) -> str:
        if not self.enabled:
            return ""
        checked, active = self._status
        note = ("上次检查 %s" % starkmesh.clock_at(checked, checked, self.app.tz)
                if checked is not None else "尚未检查")
        return "后台告警已开，" + note + (" · " + "、".join(active) if active else "")

    def _summary(self) -> tuple:
        return self._state["checked_at"], tuple(label for key, label in LABELS.items() if key in self._state["active"])

    def _load(self) -> dict:
        state = _empty_state()
        if self.path is None:
            return state
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or saved.get("version") != state["version"]:
                raise ValueError
            for key in ("checked_at", "failed_since"):
                if _number(saved.get(key)):
                    state[key] = saved[key]
            for key, at in saved["last_sent"].items():
                if key in LABELS and _number(at):
                    state["last_sent"][key] = at
            for key, episode in saved["active"].items():
                if key in LABELS and isinstance(episode, dict) and _number(episode.get("since")):
                    active = {"since": episode["since"], "sent": episode.get("sent") is True}
                    if _number(episode.get("reset_at")):
                        active["reset_at"] = episode["reset_at"]
                    state["active"][key] = active
            state["accounts"] = {key: status for key, status in saved["accounts"].items()
                                 if status in ("usable", "unavailable", "pending", "sent")}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, AttributeError) as e:
            self._log_error("state", type(e).__name__)
            return _empty_state()
        return state

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".starkmesh-alerts-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self._state, f, separators=(",", ":"))
                os.replace(tmp, self.path)  # a restart sees one whole round's state
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as e:
            self._log_error("state", str(e.errno or type(e).__name__))
        else:
            self._errors.pop("state", None)

    def _log_error(self, key: str, code: str) -> None:
        if self._errors.get(key) != code:
            self._errors[key] = code
            log.warning("%s: %s", key, code)

    def _activate(self, key: str, now: float) -> dict:
        return self._state["active"].setdefault(key, {"since": now, "sent": False})

    def _recover(self, key: str) -> None:
        self._state["active"].pop(key, None)
        self._errors.pop(key, None)

    def _due(self, key: str, now: float) -> bool:
        sent = self._state["last_sent"]
        # Each key can have at most one successful send in the rolling hour, so its latest send suffices.
        last = sent.get(key)
        cooldown = HOUR if key == ACCOUNT else KEY_COOLDOWN
        return ((last is None or now - last >= cooldown)
                and sum(now - at < HOUR for at in sent.values()) < HOURLY_MOST)

    def _send(self, key: str, message: str) -> bool:
        try:
            result = self.app.hub.request("POST", "/api/v1/notify", {
                "id": key, "title": ("StarkMesh · " + LABELS[key])[:120], "body": message[:1000],
                "ui": starkmesh.PAGE, "push_level": "P0" if key == NONE else "P1",
            }, timeout=NOTIFY_WAIT)
        except HubError as e:
            code = e.code or str(e.status or "hub_error")
        except Exception as e:
            code = type(e).__name__
        else:
            if not isinstance(result, dict) or result.get("sent") is not False:
                self._errors.pop(key, None)
                log.info("sent %s", key)
                return True
            code = str(result.get("reason") or "not_sent")
        self._log_error(key, code if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", code) else "hub_error")
        return False

    def check(self) -> None:
        if not self._running():
            return
        try:
            snap = self.mesh.read(current=True)
        except Exception as e:
            if not self._running() or isinstance(e, starkmesh.StarkMeshError) and e.kind in ("setup", "loading"):
                return
            now = self.mesh.clock()
            messages = self._failed_read(e, now)
            accounts = []
        else:
            if not self._running():
                return
            now = self.mesh.clock()
            self._state["failed_since"] = None
            self._recover(DOWN)
            pool = starkmesh.PoolUsage(snap, now)
            messages = self._availability(snap, pool, now)
            accounts = self._account_errors(snap, pool.partial, now)
            if accounts:
                messages[ACCOUNT] = self._account_message(accounts)
            if not (pool.partial or getattr(snap, "usage_failed", ())):
                messages.update(self._quota(pool, now))

        self._state["checked_at"] = now
        self._status = self._summary()
        self._save()
        for key, message in messages.items():
            if not self._running():
                break
            if self._due(key, self.mesh.clock()) and self._send(key, message):
                self._state["last_sent"][key] = self.mesh.clock()
                self._state["active"][key]["sent"] = True
                if key == ACCOUNT:
                    for account in accounts:
                        self._state["accounts"][str(account["id"])] = "sent"
                self._save()

    def _failed_read(self, error: Exception, now: float) -> dict:
        first = self._state["failed_since"]
        checked = self._state["checked_at"]
        last, _ = self.mesh.state
        if last is not None and (checked is None or last.now > checked):
            # A foreground read can recover and then fail again between two checks.
            self._recover(DOWN)
            first = None
        failure = self.mesh.failure
        if first is None:
            first = failure[0] if failure is not None else now
        self._state["failed_since"] = first
        auth = isinstance(error, starkmesh.StarkMeshError) and error.kind == "auth"
        if not auth and now - first < DOWN_AFTER:
            return {}
        if self._activate(DOWN, now)["sent"]:
            return {}
        body = "StarkMesh 拒绝了只读管理密钥，请检查密钥和读取权限。" if auth else "已连续 15 分钟读不到 StarkMesh，请检查网关连接。"
        return {DOWN: body}

    def _availability(self, snap, pool, now: float) -> dict:
        if pool.ok:
            self._recover(NONE)
        elif snap.claude and not pool.partial and not self._activate(NONE, now)["sent"]:
            return {NONE: "当前 %d 个 Claude 号均不可用。" % len(snap.claude)}
        return {}

    def _account_errors(self, snap, partial: bool, now: float) -> list:
        previous = self._state["accounts"]
        states = dict(previous) if partial else {}
        pending = []
        for a in snap.claude:
            if a.get("id") is None:
                continue
            key = str(a["id"])
            was = previous.get(key)
            if starkmesh.pool_usable(a, now):
                states[key] = "usable"
                continue
            status = a.get("status")
            persistent = status == "error" or status == "active" and starkmesh.expired(a, now)
            if persistent and was in ("usable", "pending"):
                states[key] = "pending"
                pending.append(a)
            else:
                states[key] = was if was in ("pending", "sent") else "unavailable"
        self._state["accounts"] = states
        if any(s in ("pending", "sent") for s in states.values()):
            episode = self._activate(ACCOUNT, now)
            if pending:
                episode["sent"] = False
        else:
            self._recover(ACCOUNT)
        return pending

    @staticmethod
    def _account_message(accounts: list) -> str:
        names = ["".join(c for c in str(a.get("name") or a["id"]) if c.isprintable()) for a in accounts]
        header = "%d 个 Claude 号出错或已过期：" % len(names)
        body = header + "、".join(names)
        # Leave room for the hub's push envelope even with four-byte characters in account names.
        if len(body) > 1000 or len(body.encode("utf-8")) > 3000:
            return body[:999].encode("utf-8")[:2997].decode("utf-8", errors="ignore") + "…"
        return body

    def _quota(self, pool, now: float) -> dict:
        try:
            line = max(5, min(50, int(self.app.config.get("starkmesh_alert_left", 10))))
        except (TypeError, ValueError, OverflowError):
            line = 10
        messages = {}
        for window, label in starkmesh.WINDOWS:
            key = LOW[window]
            active = self._state["active"].get(key)
            if active is not None and active.get("reset_at", float("inf")) <= now:
                self._recover(key)
            w = pool.windows.get(window)
            if w is None:
                continue
            left = round(100.0 - w["used_percent"], 10)  # keep inclusive boundaries across floating-point subtraction
            if left >= line + 5:
                self._recover(key)
            episode = self._activate(key, now) if left <= line else self._state["active"].get(key)
            reset = now + w["reset"] if w["reset"] is not None else None
            if episode is not None and reset is not None:
                episode["reset_at"] = reset
            if left <= line and not episode["sent"]:
                soon = "最早 %s 重置" % starkmesh.clock_at(reset, now, self.app.tz) if reset is not None else "最早重置时间未知"
                messages[key] = "%s额度剩余 %.1f%%，%s。" % (label, left, soon)
        return messages
