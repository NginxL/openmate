"""Who is a program: a bot's direct messages and @-mentions are notices, not things that need the user. The store
keeps what the service said (Backend.people_kinds); the ones not known yet are asked about in the background, so a
draw never waits, and the pages and the badge are drawn again once they are known."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, Iterable, Optional, Set

from .backend import Backend

log = logging.getLogger("openmate.people")

BATCH = 50
RETRY = 600.0  # one the service could not tell about is asked again after this long
RETRY_ERROR = 30.0  # after a failed read (not signed in yet, the network): sooner


class People:
    def __init__(self, app, spawn: Optional[Callable[[Callable[[], None]], None]] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.app = app
        self.clock = clock
        self.spawn = spawn or (lambda fn: threading.Thread(target=fn, name="openmate-people", daemon=True).start())
        self._mu = threading.Lock()
        self._want: Set[str] = set()
        self._next: Dict[str, float] = {}  # not asked about again before then
        self._running = False

    def _can(self) -> bool:
        backend = getattr(self.app, "backend", None)
        return backend is not None and type(backend).people_kinds is not Backend.people_kinds

    def bots(self, ids: Iterable[str]) -> Set[str]:
        """Those of ids known to be programs. The unknown ones are asked about behind."""
        ids = {str(i) for i in ids if i}
        store = getattr(self.app, "store", None)
        if not ids or store is None:
            return set()
        known = store.people_kinds(ids)
        unknown = ids - set(known)
        if unknown:
            self._ask(unknown)
        return {i for i, k in known.items() if k == "bot"}

    def _ask(self, ids: Set[str]) -> None:
        if not self._can():
            return
        now = self.clock()
        with self._mu:
            self._want |= {i for i in ids if now >= self._next.get(i, now)}
            if not self._want or self._running:
                return
            self._running = True
        self.spawn(self._run)

    def _run(self) -> None:
        app = self.app
        told = False
        while True:
            with self._mu:
                batch = sorted(self._want)[:BATCH]
                if not batch or not app.signed_in:
                    self._running = False  # the ones left are asked about with the next draw
                    break
                self._want.difference_update(batch)
            wait = RETRY
            try:
                kinds = app.backend.people_kinds(batch) or {}
            except Exception as e:
                log.info("who is a bot could not be read (%s); asked again in %ds", e.__class__.__name__, RETRY_ERROR)
                kinds, wait = {}, RETRY_ERROR
            kinds = {i: k for i, k in kinds.items() if i in batch and k in ("bot", "user")}
            if kinds and app.store is not None:
                app.store.set_people_kinds(kinds)
                told = True
            now = self.clock()
            with self._mu:
                for i in batch:
                    if i not in kinds:
                        self._next[i] = now + wait
        if told:
            app.mark_changed("inbox")  # the badge and the pages count them again
