"""Persona's 渠道: the channel extension point (docs/extensions/channels.md "Contributing a channel", protocol 1).

Each extension declares one channel on its connector (the same id; binding, account and switch are the connector's),
so it is listed in Persona's 设置 › 渠道. It works only in the one chat the user names in 「Persona 渠道会话」
(channel_chat: a chat's id, or the name of exactly one chat), none by default — and then nothing is ever sent.

In that chat, the user's own lines for Persona are handed over with channel.receive, and Persona's pipeline does the
rest (which conversation, commands, the turn, questions):

- /agent … (the configured prefix; the rest is the message). Everywhere else /agent keeps its own way (agent.py):
  a conversation per request with its context, progress edited in place, 自动代答 and the follow-ups of a thread.
- /stop, /停止, /停: the channel's /stop (Persona stops the conversation the channel talks to). One that quotes an
  /agent request from before goes that request's way.
- a reply quoting a message of the channel's, or in the thread of one, and any line while a question Persona put here
  waits (Persona reads the next line as its answer, as it does in every text channel).

Persona sends with channel.send: once per ref (the send journal), as the user, with the AI mark, told to the hub's
sent-as-user ledger; back where the request was asked (agent._where, by the placement it was read with), anything
else into the chat. Nowhere to send to (no channel chat, not signed in, the connector off) is cannot_send.

A question (kind ask, ref "ask:<interaction id>") Persona put here is followed till it ends: while one waits, the
hub's waiting list (GET /api/v1/interactions, agents.read) is read every channel_poll. Once it is no longer waiting,
its message is taken back (Backend.recall; when that fails, edited to say it ended) unless the user answered it here
(a line of theirs handed over while it waited), and only when it was seen waiting at least once (one the hub does not
show extensions is never touched). WeChat cannot take a message back: there it only stops waiting for an answer.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Dict, List, Optional

from . import views
from .agent import STOP_WORDS, _where, match_prefix
from .backend import BackendError
from .hub import HubError
from .wire import RPCError

log = logging.getLogger("openmate")

KINDS = ("answer", "note", "ask", "push")
#: Persona's own commands in every channel (docs/extensions/channels.md "Message flow" 3): never an answer
COMMANDS = ("/help", "/帮助", "/main", "/主对话", "/new", "/新对话", "/stop", "/停", "/status", "/状态")
_ASK_REF = re.compile(r"^ask:([^#]+)(?:#\d+)?$")
ENDED_NOTE = "（这个问题已经在别处处理了，不用在这里回答）"
FAILED_NOTE = "没能交给 Persona（%s），请稍后再发一次。"
ASK_STALE = 3 * 86400.0  # a question still followed after this long is let go (Persona's wait is a day at most)
KEEP = 7 * 86400.0  # what the channel remembers of its messages


class ChannelBridge:
    def __init__(self, app):
        self.app = app
        self.id = app.cid
        items = (app.manifest.get("contributes") or {}).get("channel") or []
        self.item: Optional[dict] = next((c for c in items if isinstance(c, dict) and c.get("id") == self.id), None)
        self.setting = str(app.config.get("channel_chat") or "").strip()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_prune = 0.0

    # ---------------------------------------------------------------- where

    @property
    def declared(self) -> bool:
        return self.item is not None

    def on(self) -> bool:
        """Declared, a chat named, the connector on and this hub the one doing the chat-driven work."""
        return self.declared and bool(self.setting) and self.app.connector_on and self.app.hub_role != "member"

    def chat_id(self) -> str:
        """The channel chat: the setting as a chat's id, else the one chat with that name ("" when none or several)."""
        store = self.app.store
        if not self.on() or store is None:
            return ""
        if store.get_chat(self.setting) is not None:
            return self.setting
        ids = store.chat_ids_named(self.setting)
        return ids[0] if len(ids) == 1 else ""

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if not self.declared:
            return
        self._thread = threading.Thread(target=self._loop, name="openmate-channel", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        th = self._thread
        if th is not None and th is not threading.current_thread():
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
        key = "channel:%s:%s" % (self.id, ref)
        app = self.app
        done = app.store.send_get(key)
        if done is not None and done["state"] == "done":
            return _answer(done["result"])  # the same ref: the same message, sent once
        chat = self.chat_id()
        if not chat:
            raise RPCError("还没有设置「Persona 渠道会话」，或者找不到这个会话" if self.on() or not self.setting
                           else "渠道没有打开", "cannot_send")
        if not app.signed_in or app.link == "expired":
            raise RPCError("%s还没有登录" % app.platform.name, "cannot_send")
        if not text.strip():
            return {}
        where = self._where_to(chat, reply_to)
        what = {"ask": "Agent 的问题", "push": "通知", "note": "提示"}.get(kind, "回答")
        res = self._deliver(chat, key, text, where, "Persona 渠道把%s以你的身份发到" % what)
        mid = str(res.get("message_ref") or "")
        if mid and done is None:
            # the first time only (a journal answer is not sent again): a reply to it is Persona's, and a question
            # is followed till it ends
            app.store.channel_msg_put(chat, mid, "out", ref=ref)
            m = _ASK_REF.match(ref)
            if kind == "ask" and m:
                app.store.channel_ask_put(m.group(1), chat, mid)
                self._wake.set()  # read the waiting list now: one answered at once elsewhere is still seen waiting
        return _answer(res)

    def _where_to(self, chat: str, reply_to: str) -> dict:
        """Back where the request was asked (its thread, the chat quoting it), by the placement it was read with;
        anything else (a push, a request not handed over from here) into the chat."""
        if reply_to:
            m = self.app.store.channel_msg(chat, reply_to)
            if m is not None and m["dir"] == "in":
                return _where(reply_to, m["root_id"], m["placement"], "")
        return {"reply_to": "", "root": "", "to_chat": False}

    def _deliver(self, chat: str, key: str, text: str, where: dict, summary: str) -> dict:
        """text with the AI mark into chat as the user, once per key; told to the hub's sent-as-user ledger."""
        app = self.app
        body = app.agent.with_suffix(text, None, ("", ""))

        def go() -> dict:
            if not app.agent._remember_post(body):  # its echo is never a request
                raise RPCError("没有发出")
            mid = app.send_as_user(chat, body, where["reply_to"] or None, where["root"] or None,
                                   to_chat=where["to_chat"])
            app.record_sent(chat, body, str(mid or ""), reply_to=where["reply_to"], root=where["root"],
                            setting="channel",
                            summary=summary + views.where(app.platform.name, app.store.get_chat(chat), chat))
            return {"text": "已发送", "message_ref": mid}

        return app.send_once(key, "channel", go)

    # ---------------------------------------------------------------- the user's lines

    def take(self, row: dict, body: str, epoch: Optional[int] = None) -> bool:
        """A fresh line of the user's own (no AI's, no echo), body as /agent reads it: True when it is the channel's
        (handed to Persona, or nothing to hand over); False lets /agent go on with it."""
        chat = self.chat_id()
        if not chat or row.get("chat_id") != chat:
            return False
        app, agent = self.app, self.app.agent
        body = (body or "").strip()
        text: Optional[str] = None
        if body.lower() in STOP_WORDS:
            old = False
            if row.get("parent_id") or row.get("root_id"):  # quoting a request, or in its thread
                with app.owned(epoch) as ok:
                    old = ok and agent._target(row) is not None
            if old:
                return False  # it stops an /agent request asked here before
            text = "/stop"
        else:
            req = match_prefix(body, agent.prefix)
            if req is not None:
                text = req
            elif self._follows(chat, row) or app.store.channel_asks_open(chat):
                text = body
        if text is None:
            return False
        quote = self._quote(chat, row)
        if not text and not quote:
            return True  # a bare /agent: nothing to hand over (and no /agent of its own here)
        with app.owned(epoch) as ok:
            if not ok or not app.store.seen_add("channel:%s:%s" % (chat, row["id"])):
                return True
            app.store.channel_msg_put(chat, row["id"], "in", placement=row.get("placement") or "",
                                      root_id=row.get("root_id") or "")
        threading.Thread(target=self._hand_over, args=(row, text, quote, epoch), name="openmate-channel-in",
                         daemon=True).start()
        return True

    def _follows(self, chat: str, row: dict) -> bool:
        """A reply to one of the channel's messages (quoting it, or in its thread)."""
        store = self.app.store
        return any(i and store.channel_msg(chat, i) is not None for i in (row.get("parent_id"), row.get("root_id")))

    def _quote(self, chat: str, row: dict) -> str:
        parent = row.get("parent_id") or ""
        if not parent or parent == row.get("root_id") and row.get("placement") == "thread":
            return ""  # a line in a thread names its root without quoting it
        m = self.app.store.message(chat, parent)
        if m is None or not (m.get("text") or "").strip():
            return ""
        return self.app.agent._clean_answer(m["text"], None, ("", ""))

    def _hand_over(self, row: dict, text: str, quote: str, epoch: Optional[int]) -> None:
        app = self.app
        chat = row["chat_id"]
        try:
            if not text or text.split(None, 1)[0].lower() not in COMMANDS:
                self._answered_here(chat)  # Persona's commands never answer a question
            msg: Dict[str, object] = {"id": row["id"]}
            if text:
                msg["text"] = text
            if quote:
                msg["quote"] = quote
            waits = list(app.timing.channel_retry)
            while True:
                if not app.owns(epoch) or self._stop.is_set():
                    return
                try:
                    app.hub_call("channel.receive", {"contribution": self.id, "message": msg})
                    return
                except RPCError as e:
                    final = e.code in ("invalid_params", "no_contribution", "unknown_method", "permission_denied")
                    if final or not waits:
                        log.warning("a line was not handed to Persona's channel (%s)", e.code or "error")
                        why = "Persona 没有接收" if final else "暂时连不上 Persona"
                        if app.owns(epoch):
                            self._note(chat, "channel-failed:%s:%s" % (chat, row["id"]), FAILED_NOTE % why,
                                       _where(row["id"], row.get("root_id") or "", row.get("placement") or "", ""))
                        return
                if self._stop.wait(waits.pop(0)):
                    return
        except Exception:
            if not app.closing.is_set():
                log.exception("handing a line to Persona's channel failed")

    def _note(self, chat: str, key: str, text: str, where: dict) -> None:
        try:
            self._deliver(chat, key, text, where, "Persona 渠道把提示以你的身份发到")
        except RPCError as e:
            log.warning("a channel note was not sent: %s", e.code or "error")

    def _answered_here(self, chat: str) -> None:
        """A line of the user's goes to Persona while questions wait here: Persona takes it as the answer to the
        latest of them, so none still waiting is taken back later (all of them, when the hub cannot be asked)."""
        store = self.app.store
        opens = store.channel_asks_open(chat)
        if not opens:
            return
        waiting = self._waiting()
        for a in opens:
            if waiting is None or a["ix"] in waiting:
                store.channel_ask_set(a["ix"], a["message_id"], here=1)

    # ---------------------------------------------------------------- the questions

    def _waiting(self) -> Optional[set]:
        """The ids of the questions waiting at the hub now; None when it cannot be asked."""
        hub = self.app.hub
        if not hub.available:
            return None
        try:
            return set(hub.pending_interactions())
        except HubError as e:
            log.info("the hub's waiting questions could not be read (%s)", e.code or e.status)
            return None

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.app.timing.channel_poll)
            self._wake.clear()
            if self._stop.is_set() or self.app.closing.is_set():
                return
            try:
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
        if not store.channel_asks_open():
            return
        epoch = app.account_epoch()
        waiting = self._waiting()  # first, then the rows: one answered here meanwhile is marked by now
        if waiting is None:
            return
        backend = app.backend
        for a in store.channel_asks_open():
            ix, mid = a["ix"], a["message_id"]
            if ix in waiting:
                if not a["seen"]:
                    store.channel_ask_set(ix, mid, seen=1)
                continue
            if not a["seen"]:
                if now - float(a["at"]) > ASK_STALE:
                    store.channel_ask_set(ix, mid, state="stale", ended=now)
                else:  # never seen waiting (one the hub does not show extensions): it cannot be told when it ended
                    store.channel_ask_set(ix, mid, state="unseen", ended=now)
                continue
            if a["here"]:
                store.channel_ask_set(ix, mid, state="answered_here", ended=now)
                continue
            if backend is None or not getattr(backend, "can_recall", False):
                store.channel_ask_set(ix, mid, state="ended", ended=now)  # WeChat: it only stops waiting
                continue
            if not app.owns(epoch) or not store.channel_ask_set(ix, mid, state="recalling", ended=now):
                continue  # another account's by now, or ended meanwhile: never taken back twice
            store.channel_ask_end(ix, mid, self._take_back(a["chat_id"], mid))

    def _take_back(self, chat: str, mid: str) -> str:
        backend = self.app.backend
        try:
            backend.recall(chat, mid)
            log.info("a channel question answered elsewhere was taken back")
            return "recalled"
        except BackendError as e:
            log.info("a channel question could not be taken back (%s): marked ended instead", e.kind)
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
