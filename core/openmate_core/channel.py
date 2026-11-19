"""Persona's 渠道: the channel extension point (docs/extensions/channels.md "Contributing a channel", protocol 1).

Each extension declares one channel on its connector (the same id; binding, account and switch are the connector's),
so it is listed in Persona's 设置 › 渠道. It works only in the one chat the user names in 「Persona 渠道会话」
(channel_chat), none by default — and then nothing is ever sent or handed over. The chat is the user's conversation
with Persona, as a bot's direct chat is in a chat app:

- the setting is a chat's id (one this extension stored) or a name: a name counts only when exactly one stored chat
  has it and the service's search finds no other chat or person of that very name (checked again every
  channel_resolve; a chat renamed or left is none at once). While that cannot be checked (not signed in yet, the
  service away) nothing is decided: a line waits, a send is refused for the hub to try again.
- Every line of the user's own there (no AI's line, no echo) goes to Persona with channel.receive, a leading /agent
  (the configured prefix) taken off; /stop, /停止, /停 are the channel's /stop (Persona stops the conversation the
  channel talks to), except one that quotes an /agent request from before the channel, or is in its thread. Persona's
  own rules decide the rest: commands, which conversation, and whether a line answers a question it put here.
  Everywhere else /agent keeps its own way (agent.py): a conversation per request with its context, progress edited
  in place, 自动代答 and the follow-ups of a thread.
- Lines are handed over one at a time, in the order said, from a table a restart keeps (a fresh one is handed over
  after it), each only while the account it was heard for is the one signed in. A line said while a question waited
  here is held to that question: when another question came meanwhile it is not handed over (it would answer that
  one) and the chat is told.

Persona sends with channel.send: once per ref (the send journal; a second call for a ref being sent waits for the
first), as the user, with the AI mark, told to the hub's sent-as-user ledger; back where the line it answers was asked
(agent._where, by the placement it was read with), anything without one into the chat. A ref belongs to the account
it was first tried under. Nowhere to send to is cannot_send: no channel chat, not signed in, the connector off, another
account, or an answer to a line not handed over from the channel chat as it is now.

A question (kind ask, ref "ask:<interaction id>") Persona put here is followed till it ends. While one waits, the hub's
waiting list (GET /api/v1/interactions, agents.read) is read every channel_poll. Once a question seen waiting is no
longer in it, and its session's question history says it ended for good (a list rebuilt after a restart may miss one
for a while), its message is taken back (Backend.recall; when that fails, edited to say it ended) — unless a line of
the user's said while it was the latest question here was handed over (Persona read it as its answer), or the channel
chat is another one by now. One never seen waiting (the hub does not show extensions every card) is never touched.
WeChat cannot take a message back: there it only stops being followed.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Dict, List, Optional, Tuple

from . import views
from .agent import STOP_WORDS, _where, match_prefix
from .backend import BackendError
from .hub import HubError
from .textutil import rfc3339
from .wire import RPCError

log = logging.getLogger("openmate")

KINDS = ("answer", "note", "ask", "push")
#: Persona's commands in every channel that take the whole line (docs/extensions/channels.md "Message flow" 3): never
#: an answer. "/new <text>" is not one: Persona reads its text as the next message, an answer too.
COMMANDS = ("/help", "/帮助", "/main", "/主对话", "/stop", "/停", "/status", "/状态")
NEW_COMMANDS = ("/new", "/新对话")
_ASK_REF = re.compile(r"^ask:([^#]+)(?:#\d+)?$")
ENDED_NOTE = "（这个问题已经在别处处理了，不用在这里回答）"
FAILED_NOTE = "没能交给 Persona（%s），请稍后再发一次。"
NEW_ASK_NOTE = "这条没有交给 Persona：在它交出去之前 Agent 又提了一个新问题，它会被当成新问题的答案。请看清问题后再发一次。"
FINAL = ("resolved", "dismissed", "expired", "cancelled")  # how a question ends for good (its history's status)
FINAL_CODES = ("invalid_params", "no_contribution", "unknown_method", "permission_denied")
SEARCH_MOST = 200  # the service's search for the chat's name: a full page may not have every chat of that name
UNSURE_AGAIN = 30.0  # a name the service could not be asked about is asked about again after this long
HISTORY_PAGES = 10
REF_KEEP = 300.0  # a ref is the account's it was first tried under for this long (the hub's tries take seconds)
ASK_STALE = 3 * 86400.0  # a question still followed after this long is let go (Persona's wait is a day at most)
KEEP = 7 * 86400.0  # what the channel remembers of its messages
MAIN = {"reply_to": "", "root": "", "to_chat": False}


class _Later(Exception):
    """The channel chat cannot be told now: what needs it waits."""


def may_answer(text: str) -> bool:
    """Whether Persona may read the line as the answer to a waiting question (not a command it takes whole)."""
    t = (text or "").strip()
    if not t.startswith("/"):
        return True
    name, _, rest = t.partition(" ")
    name = name.lower()
    if name in COMMANDS:
        return False
    return name not in NEW_COMMANDS or bool(rest.strip())


class ChannelBridge:
    def __init__(self, app):
        self.app = app
        self.id = app.cid
        items = (app.manifest.get("contributes") or {}).get("channel") or []
        self.item: Optional[dict] = next((c for c in items if isinstance(c, dict) and c.get("id") == self.id), None)
        self.setting = str(app.config.get("channel_chat") or "").strip()
        self._mu = threading.Lock()
        self._checked: Optional[Tuple[int, str, str, float]] = None  # (account epoch, chat, ok | other | unsure, when)
        self._epochs: Dict[Tuple[str, str], int] = {}  # a line taken → the account generation it was heard in
        self._refs: Dict[str, Tuple[int, float, threading.Lock]] = {}  # a ref → its account generation, its lock
        self._wake = threading.Event()  # the question follower
        self._queued = threading.Event()  # the hand-over worker
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._last_prune = 0.0

    # ---------------------------------------------------------------- which chat

    @property
    def declared(self) -> bool:
        return self.item is not None

    def on(self) -> bool:
        """Declared, a chat named, the connector on and this hub the one doing the chat-driven work."""
        return self.declared and bool(self.setting) and self.app.connector_on and self.app.hub_role != "member"

    def chat(self, wait: bool = True) -> Tuple[str, bool]:
        """(the channel chat of the account signed in now, or ""; whether that is sure). Not sure: a name the service
        could not be asked about now. wait=False (the listener's thread): a name found before is trusted even when it
        is due to be checked again."""
        app = self.app
        store = app.store
        if not self.on() or store is None:
            return "", True
        s = self.setting
        if store.get_chat(s) is not None:
            return ("" if store.is_gone(s) else s), True
        ids = store.chat_ids_named(s)  # none the account left
        if len(ids) != 1:
            return "", True  # no chat of that name here (or an id not stored), or several: nowhere
        mine = ids[0]
        epoch = app.account_epoch()
        now = time.monotonic()
        with self._mu:
            known = self._checked
        if known is not None and known[0] == epoch and known[1] == mine:
            verdict, age = known[2], now - known[3]
            if verdict == "unsure" and age < UNSURE_AGAIN:
                return "", False
            if verdict != "unsure" and (not wait or age < app.timing.channel_resolve):
                return (mine if verdict == "ok" else ""), True
        verdict = self._search(s, mine)
        if verdict == "unsure" and known is not None and known[:3] == (epoch, mine, "ok"):
            with self._mu:  # what was found before stays, asked about again soon
                self._checked = (epoch, mine, "ok", now - app.timing.channel_resolve + UNSURE_AGAIN)
            return mine, True
        with self._mu:
            self._checked = (epoch, mine, verdict, now)
        if verdict == "unsure":
            return "", False
        return (mine if verdict == "ok" else ""), True

    def _candidate(self) -> str:
        """The one stored chat the setting may name ("" when none or several)."""
        store = self.app.store
        if not self.on() or store is None:
            return ""
        ids = store.chat_ids_named(self.setting)
        return ids[0] if len(ids) == 1 else ""

    def _search(self, name: str, mine: str) -> str:
        """ok: the service knows no other chat or person of that very name; other: it does; unsure: not asked."""
        app = self.app
        backend = app.backend
        if backend is None or not app.signed_in:
            return "unsure"
        try:
            hits = backend.search(name, SEARCH_MOST)
        except BackendError as e:
            log.info("the channel chat's name could not be checked (%s)", e.kind)
            return "unsure"
        if len(hits) >= SEARCH_MOST:
            log.info("the channel chat's name matches too many chats to be checked")
            return "unsure"
        peer = (app.store.get_chat(mine) or {}).get("peer_id") or ""
        for h in hits:
            if (h.name or "").strip() != name or h.id == mine or (h.kind == "user" and peer and h.id == peer):
                continue
            log.warning("「Persona 渠道会话」 names another chat too: the channel sends nowhere")
            return "other"
        return "ok"

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if not self.declared:
            return
        self.app.store.channel_restart(time.time() - self.app.timing.agent_fresh)
        self._queued.set()  # what a restart cut off goes now
        for target, name in ((self._follow, "openmate-channel"), (self._work, "openmate-channel-in")):
            th = threading.Thread(target=target, name=name, daemon=True)
            th.start()
            self._threads.append(th)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._queued.set()
        for th in self._threads:
            if th is not threading.current_thread():
                th.join(3.0)

    # ---------------------------------------------------------------- the hub's calls

    def handle(self, method: str, params: dict) -> dict:
        if not self.declared or method not in ("send", "typing"):
            raise RPCError("未知方法 channel.%s" % method, "unknown_method")
        c = params.get("contribution")
        if c and c != self.id:
            raise RPCError("没有渠道 %s" % c, "no_contribution")
        if method == "typing":
            return {}  # not declared (typing: false): nothing to show
        return self.send(params)

    def _ref(self, key: str, epoch: int) -> Tuple[int, threading.Lock]:
        """The account generation key was first tried under (kept a while), and its lock."""
        now = time.monotonic()
        with self._mu:
            for k in [k for k, v in self._refs.items() if now - v[1] > REF_KEEP and not v[2].locked()]:
                del self._refs[k]
            got = self._refs.get(key)
            if got is None:
                got = self._refs[key] = (epoch, now, threading.Lock())
            return got[0], got[2]

    def send(self, params: dict) -> dict:
        ref, text, kind = params.get("ref"), params.get("text"), params.get("kind")
        if not isinstance(ref, str) or not ref.strip():
            raise RPCError("channel.send 要有 ref", "invalid_params")
        if not isinstance(text, str) or kind not in KINDS:
            raise RPCError("channel.send 的 text 或 kind 不对", "invalid_params")
        reply_to = str(params.get("reply_to") or "")
        app = self.app
        epoch = app.account_epoch()  # before anything is looked up: what is sent is this account's, or nothing
        key = "channel:%s:%s" % (self.id, ref)
        first, lock = self._ref(key, epoch)
        with lock:  # one call per ref at a time: a second one waits for the first's answer
            done = app.store.send_get(key)
            if done is not None and done["state"] == "done":
                self._track(epoch, done["result"], ref, kind)  # what a cut-off first time did not write down
                return _answer(done["result"])  # the same ref: the same message, sent once
            if first != epoch:
                raise RPCError("换了%s账号，这条是之前那个账号的，不再发出" % app.platform.name, "cannot_send")
            chat, sure = self.chat()
            if not chat:
                if not sure:
                    raise RPCError("暂时确认不了「Persona 渠道会话」是哪个会话，稍后再试")
                raise RPCError("渠道没有打开" if self.setting and not self.on()
                               else "还没有设置「Persona 渠道会话」，或者找不到这个会话", "cannot_send")
            if not app.signed_in or app.link == "expired":
                raise RPCError("%s还没有登录" % app.platform.name, "cannot_send")
            if not text.strip():
                return {}
            where = MAIN
            if reply_to:
                m = app.store.channel_msg(chat, reply_to)
                if m is None or m["dir"] != "in" or m["state"] not in ("pending", "handed"):
                    # an answer to a line of another chat (the setting changed), another account's, or never handed
                    raise RPCError("这条回复对应的消息不在现在的「Persona 渠道会话」里", "cannot_send")
                where = _where(reply_to, m["root_id"], m["placement"], "")
            what = {"ask": "Agent 的问题", "push": "通知", "note": "提示"}.get(kind, "回答")
            res = self._deliver(chat, key, text, where, "Persona 渠道把%s以你的身份发到" % what, epoch)
            self._track(epoch, res, ref, kind)
            return _answer(res)

    def _deliver(self, chat: str, key: str, text: str, where: dict, summary: str, epoch: int) -> dict:
        """text with the AI mark into chat as the user, once per key, only while the account of epoch is the one
        signed in; told to the hub's sent-as-user ledger."""
        app = self.app
        body = app.agent.with_suffix(text, None, ("", ""))
        owner = getattr(app, "_owner", "")

        def go() -> dict:
            if not app.owns(epoch, owner=owner):  # another account signed in meanwhile: not said in its name
                raise RPCError("换了%s账号，这条不再以你的身份发出" % app.platform.name, "cannot_send")
            if not app.agent._remember_post(body):  # its echo is never a request
                raise RPCError("没有发出")
            mid = app.send_as_user(chat, body, where["reply_to"] or None, where["root"] or None,
                                   to_chat=where["to_chat"])
            app.record_sent(chat, body, str(mid or ""), reply_to=where["reply_to"], root=where["root"],
                            setting="channel",
                            summary=summary + views.where(app.platform.name, app.store.get_chat(chat), chat))
            return {"text": "已发送", "message_ref": mid, "chat": chat, "owner": owner}

        return app.send_once(key, "channel", go)

    def _track(self, epoch: int, res, ref: str, kind: str) -> None:
        """Write down what was sent (again: each once), for the account it was sent as only: a reply to it is
        Persona's, and a question is followed."""
        if not isinstance(res, dict) or not res.get("chat"):
            return
        app, chat, mid = self.app, str(res["chat"]), str(res.get("message_ref") or "")
        m = _ASK_REF.match(ref)
        new = False
        with app.owned(epoch, owner=str(res.get("owner") or "")) as ok:
            if not ok or app.store.get_chat(chat) is None:
                return  # another account's store by now
            if mid:
                app.store.channel_msg_put(chat, mid, "out", owner=str(res.get("owner") or ""), ref=ref)
            if kind == "ask" and m:
                new = app.store.channel_ask_put(m.group(1), ref, chat, mid)
        if new:
            self._wake.set()  # read the waiting list now: one answered at once elsewhere is still seen waiting

    # ---------------------------------------------------------------- the user's lines

    def take(self, row: dict, body: str, epoch: Optional[int] = None) -> bool:
        """A fresh line of the user's own (no AI's, no echo), body as /agent reads it: True when it is the channel's
        (queued for Persona, or nothing to hand over); False lets /agent go on with it."""
        chat, sure = self.chat(wait=False)
        if not chat and not sure:
            chat = self._candidate()  # not told yet whether it is the one: its lines wait for that (_hand_over)
        if not chat or row.get("chat_id") != chat:
            return False
        app, agent = self.app, self.app.agent
        store = app.store
        body = (body or "").strip()
        if body.lower() in STOP_WORDS:
            parent, root = row.get("parent_id") or "", row.get("root_id") or ""
            if (parent or root) and not (parent and store.channel_msg(chat, parent)):
                with app.owned(epoch) as ok:  # an /agent request's from before the channel, by its quote or thread
                    if ok and agent._target(row) is not None:
                        return False
            text = "/stop"
        else:
            req = match_prefix(body, agent.prefix)
            text = body if req is None else req
        quote = self._quote(chat, row)
        if not text and not quote:
            return True  # a bare /agent: nothing to hand over (and no /agent of its own here)
        with app.owned(epoch) as ok:
            if not ok:
                return True
            ask_ix = store.channel_latest_ask(chat) if may_answer(text) else ""
            if not store.channel_msg_put(chat, row["id"], "in", owner=getattr(app, "_owner", ""),
                                         placement=row.get("placement") or "", root_id=row.get("root_id") or "",
                                         text=text, quote=quote, ask_ix=ask_ix, state="pending"):
                return True  # taken already (delivered again)
            if epoch is not None:
                with self._mu:
                    self._epochs[(chat, row["id"])] = epoch
        self._queued.set()
        return True

    def _quote(self, chat: str, row: dict) -> str:
        parent = row.get("parent_id") or ""
        if not parent or parent == row.get("root_id") and row.get("placement") == "thread":
            return ""  # a line in a thread names its root without quoting it
        m = self.app.store.message(chat, parent)
        if m is None or not (m.get("text") or "").strip():
            return ""
        return self.app.agent._clean_answer(m["text"], None, ("", ""))

    def _work(self) -> None:
        """Hand the taken lines over one at a time, in the order they were said: a /stop never overtakes the request
        before it, and a line cut off by a restart is handed over after it (if still fresh)."""
        while not self._stop.is_set():
            self._queued.wait(5.0)
            self._queued.clear()
            while not self._stop.is_set() and not self.app.closing.is_set():
                try:
                    rows = self.app.store.channel_in_pending()
                    if not rows:
                        break
                    self._hand_over(rows[0])
                except _Later:
                    break  # the channel chat cannot be told now: tried again soon
                except Exception:
                    if not self.app.closing.is_set():
                        log.exception("handing a line to Persona's channel failed")
                    break

    def _settle(self, r: dict, state: str, note: str = "", epoch: Optional[int] = None) -> None:
        """A line's end; the chat is told why when it did not reach Persona and the account is still its own."""
        chat, mid = r["chat_id"], r["id"]
        with self._mu:
            self._epochs.pop((chat, mid), None)
        if not self.app.store.channel_in_set(chat, mid, state) or not note:
            return
        if self.app.owns(epoch, owner=r["owner"]) and self.chat(wait=False)[0] == chat:
            self._note(chat, "channel-%s:%s:%s" % (state, chat, mid), note,
                       _where(mid, r["root_id"], r["placement"], ""), epoch)

    def _current(self, r: dict, epoch: Optional[int]) -> Optional[str]:
        """Why the line can no longer go ("" when it can go now). Raises _Later when the channel chat cannot be told."""
        app, store = self.app, self.app.store
        cur = store.channel_msg(r["chat_id"], r["id"])
        if cur is None or cur["state"] != "pending":
            return "gone"  # wiped by another account's sign-in, or settled
        if not app.owns(epoch, owner=r["owner"]):
            return "expired"  # heard for another account
        if time.time() - float(r["at"]) > app.timing.agent_fresh:
            return "stale"
        chat, sure = self.chat(wait=False)
        if r["chat_id"] != chat:
            if not sure:
                raise _Later()
            return "dropped"  # the channel chat is another one by now (or none)
        if may_answer(r["text"]) and store.channel_latest_ask(r["chat_id"]) != r["ask_ix"]:
            return "new_ask"  # Persona would read it as the answer to a question that came after it was said
        return ""

    def _hand_over(self, r: dict) -> None:
        app, store = self.app, self.app.store
        chat, mid = r["chat_id"], r["id"]
        if self._stop.is_set() or app.closing.is_set():
            return  # left pending: handed over after the restart
        with self._mu:
            epoch = self._epochs.get((chat, mid))
        msg: Dict[str, object] = {"id": mid}
        if r["text"]:
            msg["text"] = r["text"]
        if r["quote"]:
            msg["quote"] = r["quote"]
        waits = list(app.timing.channel_retry)
        while True:
            why = self._current(r, epoch)
            if why == "gone":
                with self._mu:
                    self._epochs.pop((chat, mid), None)
                return
            if why == "new_ask":
                return self._settle(r, "dropped", NEW_ASK_NOTE, epoch)
            if why == "stale":
                return self._settle(r, "expired", FAILED_NOTE % "太久没能交出去", epoch)
            if why:
                return self._settle(r, why)
            if r["ask_ix"]:
                store.channel_mark_here(r["ask_ix"], mid)  # it may answer that question: not taken back once handed
            try:
                app.hub_call("channel.receive", {"contribution": self.id, "message": msg})
                return self._settle(r, "handed")
            except RPCError as e:
                final = e.code in FINAL_CODES
                if final or not waits:
                    log.warning("a line was not handed to Persona's channel (%s)", e.code or "error")
                    return self._settle(r, "failed", FAILED_NOTE % ("Persona 没有接收" if final else "暂时连不上 Persona"),
                                        epoch)
            if self._stop.wait(waits.pop(0)) or app.closing.is_set():
                return  # left pending: handed over after the restart

    def _note(self, chat: str, key: str, text: str, where: dict, epoch: Optional[int]) -> None:
        try:
            self._deliver(chat, key, text, where, "Persona 渠道把提示以你的身份发到",
                          self.app.account_epoch() if epoch is None else epoch)
        except RPCError as e:
            log.warning("a channel note was not sent: %s", e.code or "error")

    # ---------------------------------------------------------------- the questions

    def _waiting(self) -> Optional[Dict[str, dict]]:
        """The questions waiting at the hub now, by id; None when it cannot be asked."""
        hub = self.app.hub
        if not hub.available:
            return None
        try:
            return {str(i["id"]): i for i in hub.pending_interactions()}
        except HubError as e:
            log.info("the hub's waiting questions could not be read (%s)", e.code or e.status)
            return None

    def _follow(self) -> None:
        first = True
        while not self._stop.is_set():
            self._wake.wait(min(1.0, self.app.timing.channel_poll) if first else self.app.timing.channel_poll)
            first = False
            self._wake.clear()
            if self._stop.is_set() or self.app.closing.is_set():
                return
            try:
                if self.on() and self.app.signed_in:
                    self.chat()  # checked again when due, here rather than on the listener's thread
                self._round()
            except Exception:
                if not self.app.closing.is_set():
                    log.exception("following the channel's questions failed")

    def _round(self) -> None:
        app = self.app
        store = app.store
        if store is None or app.handing_off():
            return
        now = time.time()
        if now - self._last_prune > 3600:
            self._last_prune = now
            store.channel_prune(now - KEEP)
        before = store.channel_asks_open()  # taken before the list is read: one sent meanwhile waits for the next round
        if not before:
            return
        epoch = app.account_epoch()
        waiting = self._waiting()
        if waiting is None:
            return
        chat, sure = self.chat()
        if not sure:
            return  # which chat is the channel's is not known now: nothing is decided
        history: Dict[Tuple[str, str], dict] = {}
        for b in before:
            a = store.channel_ask(b["ix"], b["ref"])  # as it is now: one answered here meanwhile is marked by now
            if a is None or a["state"] != "open":
                continue
            ix, ref = a["ix"], a["ref"]
            if a["chat_id"] != chat:
                store.channel_ask_set(ix, ref, state="dropped", ended=now)  # not the channel chat any more: untouched
                continue
            item = waiting.get(ix)
            if item is not None:
                if not a["seen"] or not a["session_id"]:
                    store.channel_ask_set(ix, ref, seen=1, machine_id=str(item.get("machine_id") or ""),
                                          session_id=str(item.get("session_id") or ""))
                continue
            age = now - float(a["at"])
            if not a["seen"]:
                if age > app.timing.channel_unseen:  # never seen waiting: when it ends cannot be told
                    store.channel_ask_set(ix, ref, state="unseen", ended=now)
                continue
            if age > ASK_STALE:
                store.channel_ask_set(ix, ref, state="stale", ended=now)
                continue
            if not self._ended(a, history):
                continue  # maybe still waiting (a list rebuilt after a restart): looked at again next round
            if a["here"]:
                line = store.channel_msg(a["chat_id"], a["here"])
                if line is not None and line["state"] == "pending":
                    continue  # its answer may be on its way: decided once it went or did not
                if line is not None and line["state"] == "handed":
                    store.channel_ask_set(ix, ref, state="answered_here", ended=now)
                    continue
            backend = app.backend
            if not a["message_id"] or backend is None or not getattr(backend, "can_recall", False):
                store.channel_ask_set(ix, ref, state="ended", ended=now)  # WeChat: it only stops being followed
                continue
            if not app.signed_in or app.link != "up":
                continue  # taken back once the link is back
            if not app.owns(epoch) or not store.channel_ask_set(ix, ref, state="recalling", ended=now):
                continue  # another account's by now, or ended meanwhile: never taken back twice
            store.channel_ask_end(ix, ref, self._take_back(a["chat_id"], a["message_id"], epoch))

    def _ended(self, a: dict, history: Dict[Tuple[str, str], dict]) -> bool:
        """Whether the question history says the question ended for good: its session's records since a little
        before it was asked, page after page till it is found (read once a round per session)."""
        key = (a["machine_id"], a["session_id"])
        if not key[1]:
            return False
        h = history.setdefault(key, {"statuses": {}, "cursor": ("", ""), "done": False, "failed": False})
        since = rfc3339(float(a["at"]) - 3600)
        pages = 0
        while a["ix"] not in h["statuses"] and not h["done"] and not h["failed"] and pages < HISTORY_PAGES:
            pages += 1
            try:
                recs, cursor = self.app.hub.interaction_history(key[0], key[1], since, *h["cursor"])
            except HubError as e:
                log.info("the question history could not be read (%s)", e.code or e.status)
                h["failed"] = True
                break
            for r in recs:
                h["statuses"].setdefault(str(r["interaction"].get("id")), str(r.get("status") or ""))
            h["cursor"] = cursor
            h["done"] = not cursor[0]
        return h["statuses"].get(a["ix"]) in FINAL

    def _take_back(self, chat: str, mid: str, epoch: int) -> str:
        backend = self.app.backend
        try:
            backend.recall(chat, mid)
            log.info("a channel question answered elsewhere was taken back")
            return "recalled"
        except BackendError as e:
            log.info("a channel question could not be taken back (%s): marked ended instead", e.kind)
        if not self.app.owns(epoch):
            return "failed"
        try:
            backend.edit(chat, mid, self.app.agent.with_suffix(ENDED_NOTE, None, ("", "")))
            return "edited"
        except BackendError as e:
            log.info("a channel question could not be marked ended either (%s)", e.kind)
            return "failed"


def _answer(res) -> dict:
    """channel.send's answer from what the send journal kept: the message's id, when there is one."""
    mid = str((res or {}).get("message_ref") or "") if isinstance(res, dict) else ""
    return {"message_id": mid} if mid else {}
