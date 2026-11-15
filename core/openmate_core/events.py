"""connector.changed, debounced: at most one event per scope every `interval` seconds; a change inside the interval is
sent once the interval is over, so nothing is lost and the hub is not flooded."""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Set


class Changed:
    def __init__(self, emit: Callable[[str], None], interval: float = 3.0):
        self._emit = emit
        self._interval = interval
        self._cond = threading.Condition()
        self._last: Dict[str, float] = {}
        self._due: Set[str] = set()
        self._stop = False
        self._thread = threading.Thread(target=self._loop, name="openmate-changed", daemon=True)
        self._thread.start()

    def mark(self, scope: str) -> None:
        with self._cond:
            self._due.add(scope)
            self._cond.notify()

    def stop(self) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify()

    def _loop(self) -> None:
        while True:
            with self._cond:
                while True:
                    if self._stop:
                        return
                    now = time.monotonic()
                    ready = [s for s in self._due if now - self._last.get(s, -1e18) >= self._interval]
                    if ready:
                        for s in ready:
                            self._due.discard(s)
                            self._last[s] = now
                        break
                    if self._due:
                        wait = min(self._last.get(s, now) + self._interval - now for s in self._due)
                        self._cond.wait(max(0.01, wait))
                    else:
                        self._cond.wait()
            for s in sorted(ready):
                try:
                    self._emit(s)
                except Exception:
                    pass
