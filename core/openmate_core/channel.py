"""Persona's 渠道: the channel extension point (docs/extensions/channels.md "Contributing a channel", protocol 1).

Each extension declares one channel on its connector (the same id; binding, account and switch are the connector's),
so it is listed in Persona's 设置 › 渠道. It works only in the one chat the user names in 「Persona 渠道会话」
(channel_chat), none by default — and then nothing is ever sent. The setting is a chat's id, or a name: a name counts
only when exactly one chat has it, among the stored chats and what the service's search finds (a person or a group of
that name elsewhere makes it no chat at all), checked again every channel_resolve.

In that chat the user's own lines for Persona are handed over (channel.receive), in the order they were said, from a
table that outlives a restart; Persona's pipeline does the rest (which conversation, commands, the turn, questions):

- /agent … (the configured prefix; the rest is the message). Everywhere else /agent keeps its own way (agent.py):
  a conversation per request with its context, progress edited in place, 自动代答 and the follow-ups of a thread.
- /stop, /停止, /停: the channel's /stop (Persona stops the conversation the channel talks to). One that quotes an
  /agent request from before the channel, or is in its thread, goes that request's way.
- a reply to a message of the channel's (quoting it, or in a thread a channel line was asked in);
- any other line while a question Persona put here waits for a text answer (Persona reads the next line as its
  answer, as in every text channel); once none waits, such a line stays the user's own.

Persona sends with channel.send: once per ref (the send journal), as the user, with the AI mark, told to the hub's
sent-as-user ledger; back where the request was asked (agent._where, by the placement it was read with), anything
without a request into the chat. Nowhere to send to is cannot_send: no channel chat, not signed in, the connector off,
or an answer to a line not handed over from the channel chat as it is now (another chat before, another account).

A question (kind ask, ref "ask:<interaction id>") Persona put here is followed till it ends. While one waits, the hub's
waiting list (GET /api/v1/interactions, agents.read) is read every channel_poll. Once a question seen waiting is no
longer in it, and the question history says it ended for good (a list rebuilt after a restart may miss one for a
while), its message is taken back (Backend.recall; when that fails, edited to say it ended) — unless the user answered
it here (a line of theirs handed over while it waited), or the channel chat is another one by now. One never seen
waiting (the hub does not show extensions every card) is never touched. WeChat cannot take a message back: there it
only stops waiting for an answer.
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
FINAL = ("resolved", "dismissed", "expired", "cancelled")  # how a question ends for good (its history's status)
FINAL_CODES = ("invalid_params", "no_contribution", "unknown_method", "permission_denied")
ASK_STALE = 3 * 86400.0  # a question still followed after this long is let go (Persona's wait is a day at most)
KEEP = 7 * 86400.0  # what the channel remembers of its messages
MAIN = {"reply_to": "", "root": "", "to_chat": False}


def answerable(item: dict) -> bool:
    """Whether a waiting question (an entry of the hub's list) is answered by a line of text in a channel: as Persona
    asks it there (channel.AskText) — a permission, a plan, a question, a terminal's single choice; never a form."""
    if not isinstance(item, dict) or item.get("fields"):
        return False
    kind = item.get("kind")
    if kind in ("permission", "plan"):
        return True
    if kind == "question":
        return bool(item.get("questions"))
    if kind == "screen":
        d = item.get("dialog") if isinstance(item.get("dialog"), dict) else {}
        return bool(d.get("options")) and not d.get("multi")
    return False


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
        self._resolved: Optional[Tuple[int, str, float]] = None  # (account epoch, chat id or "", when)
        self._epochs: Dict[Tuple[str, str], int] = {}  # a line taken → the account generation it was heard in
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

    def chat_id(self, wait: bool = True) -> str:
        """The channel chat of the account signed in now ("" when none). wait=False (the listener's thread): what was
        found last for this account, even when it is due to be checked again; looked up only when nothing was."""
        app = self.app
        if not self.on() or app.store is None:
            return ""
        epoch = app.account_epoch()
        with self._mu:
            known = self._resolved
        if known is not None and known[0] == epoch and (
                not wait or time.monotonic() - known[2] < app.timing.channel_resolve):
            return known[1]
        cid, keep = self._resolve()
        if keep:
            with self._mu:
                self._resolved = (epoch, cid, time.monotonic())
        elif known is not None and known[0] == epoch and known[1]:
            return known[1]  # the check could not be made now (the service away): the chat found last stays
        return cid

    def _resolve(self) -> Tuple[str, bool]:
        """(the chat, whether to keep the answer): kept when it is sure, not while the service could not be asked."""
        app, s = self.app, self.setting
        store = app.store
        if store.get_chat(s) is not None:
            return ("" if store.is_gone(s) else s), True
        ids = store.chat_ids_named(s)
        if len(ids) != 1:
            if ids:
                log.warning("「Persona 渠道会话」 names %d chats: the channel sends nowhere", len(ids))
            return "", bool(ids)
        backend = app.backend
        if backend is None or not app.signed_in:
            return "", False
        try:
            hits = backend.search(s, 50)
        except BackendError as e:
            log.info("the channel chat's name could not be checked (%s)", e.kind)
            return "", False
        mine = ids[0]
        peer = (store.get_chat(mine) or {}).get("peer_id") or ""
        for h in hits:
            if (h.name or "").strip() != s or h.id == mine or (h.kind == "user" and peer and h.id == peer):
                continue
            log.warning("「Persona 渠道会话」 names another chat too: the channel sends nowhere")
            return "", True
        return mine, True

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

    def send(self, params: dict) -> dict:
        ref, text, kind = params.get("ref"), params.get("text"), params.get("kind")
        if not isinstance(ref, str) or not ref.strip():
            raise RPCError("channel.send 要有 ref", "invalid_params")
        if not isinstance(text, str) or kind not in KINDS:
            raise RPCError("channel.send 的 text 或 kind 不对", "invalid_params")
        reply_to = str(params.get("reply_to") or "")
        app = self.app
        store = app.store
        epoch = app.account_epoch()  # before anything is looked up: what is sent is this account's, or nothing
        key = "channel:%s:%s" % (self.id, ref)
        done = store.send_get(key)
        if done is not None and done["state"] == "done":
            self._track(epoch, done["result"], ref, kind)  # what a cut-off first time did not write down
            return _answer(done["result"])  # the same ref: the same message, sent once
        chat = self.chat_id()
        if not chat:
            raise RPCError("渠道没有打开" if self.setting and not self.on()
                           else "还没有设置「Persona 渠道会话」，或者找不到这个会话", "cannot_send")
        if not app.signed_in or app.link == "expired":
            raise RPCError("%s还没有登录" % app.platform.name, "cannot_send")
        if not text.strip():
            return {}
        where = MAIN
        if reply_to:
            m = store.channel_msg(chat, reply_to)
            if m is None or m["dir"] != "in":
                # an answer to a line of another chat (the setting changed) or another account's: nowhere here
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

        def go() -> dict:
            if not app.owns(epoch):  # another account signed in meanwhile: not said in its name
                raise RPCError("换了%s账号，这条不再以你的身份发出" % app.platform.name)
            if not app.agent._remember_post(body):  # its echo is never a request
                raise RPCError("没有发出")
            mid = app.send_as_user(chat, body, where["reply_to"] or None, where["root"] or None,
                                   to_chat=where["to_chat"])
            app.record_sent(chat, body, str(mid or ""), reply_to=where["reply_to"], root=where["root"],
                            setting="channel",
                            summary=summary + views.where(app.platform.name, app.store.get_chat(chat), chat))
            return {"text": "已发送", "message_ref": mid, "chat": chat}

        return app.send_once(key, "channel", go)

    def _track(self, epoch: int, res, ref: str, kind: str) -> None:
        """Write down what was sent (again: each once): a reply to it is Persona's, and a question is followed."""
        if not isinstance(res, dict) or not res.get("chat"):
            return
        app, chat, mid = self.app, str(res["chat"]), str(res.get("message_ref") or "")
        m = _ASK_REF.match(ref)
        new = False
        with app.owned(epoch) as ok:
            if not ok or app.store.get_chat(chat) is None:
                return  # another account's store by now
            if mid:
                app.store.channel_msg_put(chat, mid, "out", ref=ref)
            if kind == "ask" and m:
                new = app.store.channel_ask_put(m.group(1), ref, chat, mid)
        if new:
            self._wake.set()  # read the waiting list now: one answered at once elsewhere is still seen waiting

    # ---------------------------------------------------------------- the user's lines

    def take(self, row: dict, body: str, epoch: Optional[int] = None) -> bool:
        """A fresh line of the user's own (no AI's, no echo), body as /agent reads it: True when it is the channel's
        (queued to be handed to Persona, or nothing to hand over); False lets /agent go on with it."""
        chat = self.chat_id(wait=False)
        if not chat or row.get("chat_id") != chat:
            return False
        app, agent = self.app, self.app.agent
        store = app.store
        body = (body or "").strip()
        reply = self._follows(chat, row)
        if body.lower() in STOP_WORDS:
            if not reply and (row.get("parent_id") or row.get("root_id")):  # an /agent request's, from before
                with app.owned(epoch) as ok:
                    if ok and agent._target(row) is not None:
                        return False
            text, why = "/stop", "stop"
        else:
            req = match_prefix(body, agent.prefix)
            if req is not None:
                text, why = req, "agent"
            elif reply:
                text, why = body, "follow"
            elif store.channel_asks_open(chat):
                text, why = body, "answer"  # handed over only if a question here still waits for it (_hand_over)
            else:
                return False
        quote = self._quote(chat, row)
        if not text and not quote:
            return True  # a bare /agent: nothing to hand over (and no /agent of its own here)
        with app.owned(epoch) as ok:
            if not ok or not store.seen_add("channel:%s:%s" % (chat, row["id"])):
                return True
            store.channel_msg_put(chat, row["id"], "in", placement=row.get("placement") or "",
                                  root_id=row.get("root_id") or "", text=text, quote=quote, why=why, state="pending")
            if epoch is not None:
                with self._mu:
                    self._epochs[(chat, row["id"])] = epoch
        self._queued.set()
        return True

    def _follows(self, chat: str, row: dict) -> bool:
        """A reply to the channel: quoting one of its messages, or in a thread one of them is in or was asked in."""
        store = self.app.store
        parent, root = row.get("parent_id") or "", row.get("root_id") or ""
        if any(i and store.channel_msg(chat, i) is not None for i in (parent, root)):
            return True
        return bool(root) and store.channel_thread(chat, root)

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
                except Exception:
                    if not self.app.closing.is_set():
                        log.exception("handing a line to Persona's channel failed")
                    break

    def _hand_over(self, r: dict) -> None:
        app, store = self.app, self.app.store
        chat, mid, text, why = r["chat_id"], r["id"], r["text"], r["why"]
        if self._stop.is_set() or app.closing.is_set():
            return  # left pending: handed over after the restart
        with self._mu:
            epoch = self._epochs.get((chat, mid))
        if not app.owns(epoch):
            store.channel_in_set(chat, mid, "expired")  # heard for another account (its store is wiped anyway)
            self._forget(chat, mid)
            return
        if chat != self.chat_id(wait=False):
            store.channel_in_set(chat, mid, "dropped")  # the channel chat is another one by now (or none)
            self._forget(chat, mid)
            return
        marked: List[dict] = []
        opens = store.channel_asks_open(chat) if may_answer(text) else []
        if why == "answer" and may_answer(text) and not opens:
            store.channel_in_set(chat, mid, "dropped")  # let in as an answer, and nothing waits any more
            self._forget(chat, mid)
            return
        if opens:
            waiting = self._waiting()
            if why == "answer" and waiting is not None and not any(
                    a["ix"] in waiting and answerable(waiting[a["ix"]]) for a in opens):
                store.channel_in_set(chat, mid, "dropped")  # none waits for it any more: the user's own line
                self._forget(chat, mid)
                return
            for a in opens:  # Persona takes it as the latest one's answer: none still waiting is taken back
                if (waiting is None or a["ix"] in waiting) and not a["here"] and \
                        store.channel_ask_set(a["ix"], a["ref"], here=1):
                    marked.append(a)
        msg: Dict[str, object] = {"id": mid}
        if text:
            msg["text"] = text
        if r["quote"]:
            msg["quote"] = r["quote"]
        waits = list(app.timing.channel_retry)
        while True:
            if not app.owns(epoch) or self._stop.is_set():
                return  # left pending: handed over after a restart, if still fresh
            try:
                app.hub_call("channel.receive", {"contribution": self.id, "message": msg})
                store.channel_in_set(chat, mid, "handed")
                self._forget(chat, mid)
                return
            except RPCError as e:
                final = e.code in FINAL_CODES
                if final or not waits:
                    log.warning("a line was not handed to Persona's channel (%s)", e.code or "error")
                    for a in marked:  # it answered nothing
                        store.channel_ask_set(a["ix"], a["ref"], here=0)
                    store.channel_in_set(chat, mid, "failed")
                    self._forget(chat, mid)
                    if app.owns(epoch):
                        self._note(chat, "channel-failed:%s:%s" % (chat, mid),
                                   FAILED_NOTE % ("Persona 没有接收" if final else "暂时连不上 Persona"),
                                   _where(mid, r["root_id"], r["placement"], ""), epoch)
                    return
            if self._stop.wait(waits.pop(0)):
                return

    def _forget(self, chat: str, mid: str) -> None:
        with self._mu:
            self._epochs.pop((chat, mid), None)

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
                    self.chat_id()  # checked again when due, here rather than on the listener's thread
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
        chat = self.chat_id()
        history: Dict[Tuple[str, str], Optional[Dict[str, str]]] = {}
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
                    store.channel_ask_set(ix, ref, seen=1, answerable=int(answerable(item)),
                                          machine_id=str(item.get("machine_id") or ""),
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
                store.channel_ask_set(ix, ref, state="answered_here", ended=now)
                continue
            backend = app.backend
            if not a["message_id"] or backend is None or not getattr(backend, "can_recall", False):
                store.channel_ask_set(ix, ref, state="ended", ended=now)  # WeChat: it only stops waiting
                continue
            if not app.signed_in or app.link != "up":
                continue  # taken back once the link is back
            if not app.owns(epoch) or not store.channel_ask_set(ix, ref, state="recalling", ended=now):
                continue  # another account's by now, or ended meanwhile: never taken back twice
            store.channel_ask_end(ix, ref, self._take_back(a["chat_id"], a["message_id"], epoch))

    def _ended(self, a: dict, history: Dict[Tuple[str, str], Optional[Dict[str, str]]]) -> bool:
        """Whether the question history says the question ended for good (read once a round per session)."""
        key = (a["machine_id"], a["session_id"])
        if not key[1]:
            return False
        if key not in history:
            try:
                recs = self.app.hub.interaction_history(*key)
                history[key] = {str(r["interaction"].get("id")): str(r.get("status") or "") for r in recs}
            except HubError as e:
                log.info("the question history could not be read (%s)", e.code or e.status)
                history[key] = None
        statuses = history[key]
        return statuses is not None and statuses.get(a["ix"]) in FINAL

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
