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
        self._want: Dict[str, Optional[int]] = {}  # wanted asked about → the account generation of the draw that did
        self._asking: Set[str] = set()  # asked now (between leaving _want and being stored)
        self._next: Dict[str, float] = {}  # not asked about again before then
        self._running = False

    def forget(self) -> None:
        """Another account signed in: who the one before wanted asked about, and when, is not this one's."""
        with self._mu:
            self._want.clear()
            self._next.clear()
            self._asking.clear()  # a batch on its way is the account before's: the next draw asks again

    def _can(self) -> bool:
        backend = getattr(self.app, "backend", None)
        return backend is not None and type(backend).people_kinds is not Backend.people_kinds

    def bots(self, ids: Iterable[str], epoch: Optional[int] = None) -> Set[str]:
        """Those of ids known to be programs. The unknown ones are asked about behind, for the account generation the
        caller read them for (epoch; None: now)."""
        ids = {str(i) for i in ids if i}
        store = getattr(self.app, "store", None)
        if not ids or store is None:
            return set()
        known = store.people_kinds(ids)
        unknown = ids - set(known)
        if unknown:
            self._ask(unknown, epoch)
        return {i for i, k in known.items() if k == "bot"}

    def _epoch(self) -> Optional[int]:
        get = getattr(self.app, "account_epoch", None)  # the connector's account generation (a stand-in may have none)
        return get() if get is not None else None

    def _keep(self, kinds: Dict[str, str], epoch: Optional[int]) -> bool:
        """Store what the service said, unless another account's sign-in took the store since it was asked."""
        owned = getattr(self.app, "owned", None)
        if owned is None:
            self.app.store.set_people_kinds(kinds)
            return True
        with owned(epoch) as ok:
            if ok:
                self.app.store.set_people_kinds(kinds)
            return ok

    def kind_now(self, user_id: str, epoch: Optional[int] = None) -> str:
        """"bot", "user", or "" when the service cannot tell now: the store's answer, else the service's, asked here
        (for a choice that must not guess, such as answering as the user). epoch: the account generation the caller
        acts for (None: now); "" once another account's sign-in took the store."""
        store = getattr(self.app, "store", None)
        if not user_id or store is None:
            return ""
        if epoch is None:
            epoch = self._epoch()
        known = store.people_kinds([user_id]).get(user_id)
        if known:
            return known
        if not self._can() or not self.app.signed_in:
            return ""
        try:
            kinds = self.app.backend.people_kinds([user_id]) or {}
        except Exception as e:
            log.info("who is a bot could not be read now (%s)", e.__class__.__name__)
            return ""
        kind = kinds.get(user_id, "")
        if kind in ("bot", "user") and self._keep({user_id: kind}, epoch):
            return kind
        return ""

    def _ask(self, ids: Set[str], epoch: Optional[int] = None) -> None:
        if not self._can():
            return
        if epoch is None:
            epoch = self._epoch()  # the account whose draw wants them: asked and kept for that one only
        owns = getattr(self.app, "owns", None)
        now = self.clock()
        with self._mu:
            if epoch is not None and owns is not None and not owns(epoch, settle=False):
                return  # drawn for the account before (forget may have run already): nothing queued for the next
            for i in ids:
                if now >= self._next.get(i, now) and i not in self._asking:
                    self._want.setdefault(i, epoch)
            if not self._want or self._running:
                return
            self._running = True
        self.spawn(self._run)

    def _run(self) -> None:
        app = self.app
        told = False
        while True:
            with self._mu:
                if not self._want or not app.signed_in:
                    self._running = False  # the ones left are asked about with the next draw
                    break
                epoch = self._want[min(self._want)]  # one account's at a time: the generation they were wanted in
                batch = sorted(i for i, e in self._want.items() if e == epoch)[:BATCH]
                for i in batch:
                    del self._want[i]
                self._asking.update(batch)  # on their way: a draw meanwhile does not ask them again
            wait = RETRY
            try:
                kinds = app.backend.people_kinds(batch) or {}
            except Exception as e:
                log.info("who is a bot could not be read (%s); asked again in %ds", e.__class__.__name__, RETRY_ERROR)
                kinds, wait = {}, RETRY_ERROR
            kinds = {i: k for i, k in kinds.items() if i in batch and k in ("bot", "user")}
            owns = getattr(app, "owns", None)
            switched = epoch is not None and owns is not None and not owns(epoch)
            if kinds and app.store is not None and not switched:
                if self._keep(kinds, epoch):
                    told = True
                else:
                    switched = True
            now = self.clock()
            with self._mu:  # forget empties these under this lock, after the generation moved on: checked again here
                switched = switched or (epoch is not None and owns is not None and not owns(epoch, settle=False))
                self._asking.difference_update(batch)  # stored (or put off, or let go) before they leave the set
                if not switched:  # asked for the account before: nothing put off for the next
                    for i in batch:
                        if i not in kinds:
                            self._next[i] = now + wait
        if told:
            app.mark_changed("inbox")  # the badge and the pages count them again
