"""Persona's 渠道: the channel extension point (docs/extensions/channels.md "Contributing a channel", protocol 1).

Each extension declares one channel on its connector (the same id; binding, account and switch are the connector's),
so it is listed in Persona's 设置 › 渠道. It works only in the one chat the user names in 「Persona 渠道会话」
(channel_chat: the id of a chat this extension stored, not one the account left), none by default — and then nothing is
ever sent or handed over. The chat is the user's conversation with Persona, as a bot's direct chat is in a chat app:

- Every line of the user's own there (not an AI's line, not an echo of what this program sent) goes to Persona with
  channel.receive, as it was said, a leading /agent (the configured prefix) taken off. /stop, /停止, /停 are the
  channel's /stop (Persona stops the conversation the channel talks to), except one that quotes an /agent request from
  before the channel, or is in its thread: that request is stopped. Persona's own rules decide the rest: commands,
  which conversation, and whether a line answers a question it put here (the next line does).
  Everywhere else /agent keeps its own way (agent.py): a conversation per request with its context, progress edited
  in place, 自动代答 and the follow-ups of a thread.
- Lines are handed over one at a time, in the order they came, from a table a restart keeps (a fresh one is handed over
  after it), each only while its own account is signed in and the live link is up; signing out lets them go. A line
  is not handed over when Persona put another question in the chat after it came (handing it over later would make it
  that question's answer): the chat is told instead.

Persona sends with channel.send: once per ref (the send journal; a second call for a ref being sent waits for the
first), as the user, with the AI mark, told to the hub's sent-as-user ledger; back where the line it answers was asked
(agent._where, by the placement it was read with), anything without one into the chat. A ref belongs to the account it
was first tried under (kept in the store). Nowhere to send to is cannot_send: no channel chat, not signed in, the
connector off, another account, or an answer to a line not handed over from the channel chat as it is now.

A question (kind ask, ref "ask:<interaction id>") Persona put here is followed till it ends. While one waits, the hub's
waiting list (GET /api/v1/interactions, agents.read) is read every channel_poll. Once a question seen waiting is no
longer in it, and its session's question history says it ended for good (a list rebuilt after a restart may miss one
for a while), its message is taken back (Backend.recall; when that fails, edited to say it ended) — unless a line of
the user's that came while it was open was handed over (Persona may have read it as its answer), or the channel chat
is another one by now. One never seen waiting (the hub does not show extensions every card) is never touched. WeChat
cannot take a message back: there it only stops being followed.

What the protocol cannot tell: Persona takes a line in asynchronously and reads it as the answer to the latest question
still waiting when it gets to it; which one that is, is Persona's to decide (as in every text channel).
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
import threading
import time
from typing import Dict, List, Optional, Tuple

from . import images, views
from .agent import STOP_WORDS, _where, match_prefix
from .backend import BackendError
from .hub import HubError
from .textutil import rfc3339
from .wire import RPCError

log = logging.getLogger("openmate")

KINDS = ("answer", "note", "ask", "push")
IMAGES_MOST = 4  # pictures in one channel.send (docs/extensions/channels.md: images_out, at most 4)
#: Persona's commands in every channel that take the whole line (docs/extensions/channels.md "Message flow" 3): never
#: an answer. "/new <text>" is not one: Persona reads its text as the next message, an answer too.
COMMANDS = ("/help", "/帮助", "/main", "/主对话", "/stop", "/停", "/status", "/状态")
NEW_COMMANDS = ("/new", "/新对话")
_ASK_REF = re.compile(r"^ask:([^#]+)(?:#\d+)?$")
ENDED_NOTE = "（这个问题已经在别处处理了，不用在这里回答）"
FAILED_NOTE = "没能交给 Persona（%s），请稍后再发一次。"
NEW_ASK_NOTE = "这条没有交给 Persona：它交出去之前 Agent 又提了一个问题，交出去会被当成那个问题的答案。请看清问题后再发一次。"
FINAL = ("resolved", "dismissed", "expired", "cancelled")  # how a question ends for good (its history's status)
FINAL_CODES = ("invalid_params", "no_contribution", "unknown_method", "permission_denied")
HISTORY_PAGES = 100  # pages of a session's question history a round reads at most
ECHO_WINDOW = 600.0  # a send's text stands for its echo till its id is known, at most this long (agent_fresh: a line
# older than that is never taken anyway)
ASK_STALE = 3 * 86400.0  # a question still followed after this long is let go (Persona's wait is a day at most)
TAKE_BACK_TRIES = 5  # a question not taken back (nor marked ended) is tried again so many rounds, then let be
TEXT_TYPES = ("TEXT", "POST")  # a line of words; anything else (a picture, a sticker, a file …) is no answer
KEEP = 7 * 86400.0  # what the channel remembers of its messages
REFS_KEEP = 30 * 86400.0  # a ref's account: as long as the send journal keeps the ref
MAIN = {"reply_to": "", "root": "", "to_chat": False}
PICTURE = "\x00picture"  # outgoing's words for a picture being sent: its echo is a line of type IMAGE


class _Later(Exception):
    """A line cannot go now (not signed in, the link down): it waits."""


def may_answer(text: str) -> bool:
    """Whether Persona may read the line as the answer to a waiting question (not a command it takes whole)."""
    t = (text or "").strip()
    if not t:
        return False  # only a quote, or only a picture: nothing Persona could read as an answer
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
        # A line's last check and its hand-over, and a question's sending and writing down: one at a time, so a question
        # is either before a line's check (and the line waits) or after its hand-over. A sign-out waits for them too.
        self._submit = threading.RLock()
        self._intake = threading.Lock()  # taking a line in (signed in?) and a sign-out's letting the lines go
        self._history: Dict[tuple, dict] = {}  # a session's question history read so far (kept across rounds)
        self._signed_out = 0.0  # when the user signed out last: a line stored before it is not taken in after it
        self._epochs: Dict[Tuple[str, str], int] = {}  # a line taken → the account generation it was heard in
        self._refs: Dict[str, Tuple[float, threading.Lock]] = {}  # a ref being sent → its lock
        self._outs: Dict[Tuple[str, str], List[float]] = {}  # (chat, text) → when each send of it began, id not known yet
        self._wake = threading.Event()  # the question follower
        self._queued = threading.Event()  # the hand-over worker
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._last_prune = 0.0

    # ---------------------------------------------------------------- which chat

    @property
    def declared(self) -> bool:
        return self.item is not None

    @property
    def declares_images_out(self) -> bool:
        """The channel declares images_out: channel.send's images are pictures to send after the text."""
        return bool(((self.item or {}).get("capabilities") or {}).get("images_out"))

    @property
    def images_out(self) -> bool:
        """It declares images_out and the backend can send a picture now."""
        return self.declares_images_out and bool(getattr(self.app.backend, "can_send_images", False))

    def _pictures(self, given) -> List[bytes]:
        """channel.send's images (only with images_out): each a PNG or JPEG of at most images.IMAGE_MOST, at most
        IMAGES_MOST of them; one that is not is left out (logged), the others go."""
        if not self.images_out or not isinstance(given, list):
            return []
        out: List[bytes] = []
        for img in given[:IMAGES_MOST]:
            data = b""
            if isinstance(img, dict) and isinstance(img.get("data"), str):
                try:
                    data = base64.b64decode(img["data"], validate=True)
                except (binascii.Error, ValueError):
                    data = b""
            if data and len(data) <= images.IMAGE_MOST and images.kind_of(data[:16])[0]:
                out.append(data)
            else:
                log.warning("channel.send: a picture that is not a PNG or JPEG of at most %d MB was left out",
                            images.IMAGE_MOST >> 20)
        return out

    def on(self) -> bool:
        """Declared, a chat named, the connector on and this hub the one doing the chat-driven work."""
        return self.declared and bool(self.setting) and self.app.connector_on and self.app.hub_role != "member"

    def chat(self) -> str:
        """The channel chat: the setting, when it is a chat this extension stored and the account is still in ("")."""
        store = self.app.store
        if not self.on() or store is None:
            return ""
        s = self.setting
        if store.get_chat(s) is None or store.is_gone(s):
            return ""
        return s

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if not self.declared:
            return
        self.app.store.channel_restart(time.time() - self.app.timing.agent_fresh)
        # a question sent but cut off before it was written down: written down before any line is handed over
        for key, res in self.app.store.sends_done("channel:%s:ask:" % self.id, time.time() - KEEP):
            self._track(None, res, key[len("channel:%s:" % self.id):], "ask")
        self._queued.set()  # what a restart cut off goes now (once its account is signed in)
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

    def forget_pending(self) -> None:
        """Signed out: the lines not handed over yet go nowhere (after one being handed over now)."""
        if not self.declared or self.app.store is None:
            return
        with self._intake, self._submit:
            self._signed_out = time.time()
            n = self.app.store.channel_forget_pending()
        if n:
            log.info("signed out: %d line(s) not handed to Persona were let go", n)

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

    def _ref_lock(self, key: str) -> threading.Lock:
        now = time.monotonic()
        with self._mu:
            for k in [k for k, v in self._refs.items() if now - v[0] > 300 and not v[1].locked()]:
                del self._refs[k]
            got = self._refs.get(key)
            if got is None:
                got = self._refs[key] = (now, threading.Lock())
            return got[1]

    def send(self, params: dict) -> dict:
        ref, text, kind = params.get("ref"), params.get("text"), params.get("kind")
        if not isinstance(ref, str) or not ref.strip():
            raise RPCError("channel.send 要有 ref", "invalid_params")
        if not isinstance(text, str) or kind not in KINDS:
            raise RPCError("channel.send 的 text 或 kind 不对", "invalid_params")
        reply_to = str(params.get("reply_to") or "")
        app = self.app
        pictures = self._pictures(params.get("images"))
        if self.images_out and params.get("images") and not pictures and not text.strip():
            raise RPCError("channel.send 的图片都不是 PNG 或 JPEG，又没有文字：什么都发不了", "invalid_params")
        # whose it is, taken before anything is looked up (the generation read on both sides of the owner: one moving in
        # between is a switch, and nothing is sent)
        epoch = app.account_epoch()
        owner = getattr(app, "_owner", "")
        moved = app.account_epoch() != epoch
        key = "channel:%s:%s" % (self.id, ref)
        with self._ref_lock(key), (self._submit if kind == "ask" else _NO_LOCK):
            # one call per ref at a time (a second one waits for the first's answer); a question is sent and written
            # down while no line is being checked and handed over
            done = app.store.send_get(key)
            if done is not None and done["state"] == "done":
                self._track(epoch, done["result"], ref, kind)  # what a cut-off first time did not write down
                if not pictures:
                    return _answer(done["result"])  # the same ref: the same message, sent once
            elif done is not None:  # an outcome nobody knows (it may be in the chat): the journal says so, never again
                return app.send_once(key, "channel", _never)
            gone_to = ""  # the chat what of this ref went out already went to: the rest goes there or nowhere
            if pictures:
                # the text and each picture once each (their own keys): a ref sent again sends only what did not go
                sent = [app.store.send_get(_picture_key(self.id, ref, i)) for i in range(len(pictures))]
                if all(r is not None and r["state"] == "done" for r in sent) and (done is not None or not text.strip()):
                    return _answer(done["result"] if done is not None else sent[0]["result"])
                for r in [done] + sent:
                    if r is not None and r["state"] == "done" and isinstance(r["result"], dict) and r["result"].get("chat"):
                        gone_to = str(r["result"]["chat"])
            if moved:
                raise RPCError("正在换%s账号，这条不再发出" % app.platform.name, "cannot_send")
            bind = key.split("#", 1)[0] if _ASK_REF.match(ref) else key  # every piece of a question: one account
            if app.store.channel_ref_bind(bind, owner) != owner:
                raise RPCError("换了%s账号，这条是之前那个账号的，不再发出" % app.platform.name, "cannot_send")
            chat = self.chat()
            if not chat:
                raise RPCError("渠道没有打开" if self.setting and not self.on()
                               else "还没有设置「Persona 渠道会话」，或者找不到这个会话", "cannot_send")
            if not app.signed_in or app.link == "expired":
                raise RPCError("%s还没有登录" % app.platform.name, "cannot_send")
            if gone_to and gone_to != chat:
                raise RPCError("这条的一部分已经发到之前的「Persona 渠道会话」，其余的不发到别的会话", "cannot_send")
            if not text.strip() and not pictures:
                return {}
            where = MAIN
            if reply_to:
                m = app.store.channel_msg(chat, reply_to)
                if m is None or m["dir"] != "in" or m["state"] in ("dropped", "failed"):
                    # an answer to a line of another chat (the setting changed), another account's, or one never handed
                    # over (one given up on may have reached Persona all the same: its answer is taken)
                    raise RPCError("这条回复对应的消息不在现在的「Persona 渠道会话」里", "cannot_send")
                where = _where(reply_to, m["root_id"], m["placement"], "")
            what = {"ask": "Agent 的问题", "push": "通知", "note": "提示"}.get(kind, "回答")
            res = None
            if text.strip():
                res = done["result"] if done is not None else \
                    self._deliver(chat, key, text, where, "Persona 渠道把%s以你的身份发到" % what, epoch, owner)
                self._track(epoch, res, ref, kind)
            for i, data in enumerate(pictures):
                got = self._deliver_picture(chat, _picture_key(self.id, ref, i), data, where,
                                            "Persona 渠道把%s里的一张图片以你的身份发到" % what, epoch, owner)
                self._track(epoch, got, ref, "answer" if kind == "ask" else kind)  # (the question is its text)
                res = res or got
            return _answer(res)

    def _deliver(self, chat: str, key: str, text: str, where: dict, summary: str, epoch: int, owner: str) -> dict:
        """text with the AI mark into chat as the user, once per key, only while the account of epoch and owner is the
        one signed in; told to the hub's sent-as-user ledger."""
        app = self.app
        body = app.agent.with_suffix(text, None, ("", ""))

        def go() -> dict:
            if not app.owns(epoch, owner=owner):  # another account signed in meanwhile: not said in its name
                raise RPCError("换了%s账号，这条不再以你的身份发出" % app.platform.name, "cannot_send")
            at = time.time()  # before it is in the chat: a reply to it always comes after
            # its echo is known by its id (send_as_user tells outgoing), only in this chat: not /agent's text cache,
            # which would take the user's own same words for an echo for ten minutes
            mid = app.send_as_user(chat, body, where["reply_to"] or None, where["root"] or None,
                                   to_chat=where["to_chat"])
            app.record_sent(chat, body, str(mid or ""), reply_to=where["reply_to"], root=where["root"],
                            setting="channel",
                            summary=summary + views.where(app.platform.name, app.store.get_chat(chat), chat))
            return {"text": "已发送", "message_ref": mid, "chat": chat, "owner": owner, "at": at}

        return app.send_once(key, "channel", go)

    def _deliver_picture(self, chat: str, key: str, data: bytes, where: dict, summary: str, epoch: int,
                         owner: str) -> dict:
        """A picture into chat as the user, where its text went, once per key, only while the account of epoch and
        owner is the one signed in; told to the hub's sent-as-user ledger."""
        app = self.app
        path, _ = images.keep(data, app.data_dir)

        def go() -> dict:
            if not app.owns(epoch, owner=owner):
                raise RPCError("换了%s账号，这张图片不再以你的身份发出" % app.platform.name, "cannot_send")
            at = time.time()
            mid = app.send_image_as_user(chat, path, where["reply_to"], where["root"], where["to_chat"])
            app.record_sent(chat, "[图片]", str(mid or ""), reply_to=where["reply_to"], root=where["root"],
                            setting="channel",
                            summary=summary + views.where(app.platform.name, app.store.get_chat(chat), chat))
            return {"text": "已发送", "message_ref": mid, "chat": chat, "owner": owner, "at": at}

        return app.send_once(key, "channel", go)

    def _track(self, epoch: Optional[int], res, ref: str, kind: str) -> None:
        """Write down what was sent (again: each once), for the account it was sent as only: a reply to it is
        Persona's, and a question is followed."""
        if not isinstance(res, dict) or not res.get("chat"):
            return
        app, chat, mid = self.app, str(res["chat"]), str(res.get("message_ref") or "")
        owner = str(res.get("owner") or "")
        m = _ASK_REF.match(ref)
        new = False
        with app.owned(epoch, owner=owner) as ok:
            if not ok or app.store.get_chat(chat) is None:
                return  # another account's store by now
            if mid:
                app.store.channel_msg_put(chat, mid, "out", owner=owner, ref=ref)
            if kind == "ask" and m:  # when it was sent, even when written down only now
                new = app.store.channel_ask_put(m.group(1), ref, chat, mid, now=float(res.get("at") or 0) or None)
        if new:
            self._wake.set()  # read the waiting list now: one answered at once elsewhere is still seen waiting

    # ---------------------------------------------------------------- the program's own sends

    def outgoing(self, chat_id: str, text: str, message_id: Optional[str] = None, failed: bool = False,
                 picture: bool = False) -> None:
        """Something this program sends into a chat as the user (any send: the channel's, a tool's, an inbox reply):
        its echo in the channel chat is never a line of the user's. Called before the send with its text; after it
        with its id (known by that from then on; without one the text stands for it a while), or failed (surely not
        sent: the text stands for nothing). picture: a picture, whose echo (a line of type IMAGE, which may come
        before its id is known) a picture being sent stands for."""
        if not self.declared or chat_id != self.chat() or (picture and not self.declares_images_out):
            return  # only the channel chat's lines are read as the user's (a picture: only where the channel sends some)
        if message_id and self.app.store is not None:  # known by its id first, then the text stands for it no more
            self.app.store.channel_msg_put(chat_id, message_id, "echo", owner=getattr(self.app, "_owner", ""))
        key = (chat_id, PICTURE if picture else (text or "").strip())
        now = time.monotonic()
        with self._mu:
            for k in [k for k, v in self._outs.items() if all(now - at > ECHO_WINDOW for at in v)]:
                del self._outs[k]
            sends = self._outs.setdefault(key, [])
            if message_id is None and not failed:
                sends.append(now)  # one more send of it under way
            elif (failed or message_id or picture) and sends:
                sends.pop(0)  # that send is done: known by its id, or surely not sent (a picture: over, either way)
            if not sends:
                self._outs.pop(key, None)

    def _ours(self, chat: str, row: dict) -> bool:
        m = self.app.store.channel_msg(chat, row["id"])
        if m is not None and m["dir"] in ("out", "echo"):
            return True
        now = time.monotonic()
        said = PICTURE if (row.get("type") or "").upper() == "IMAGE" else (row.get("text") or "").strip()
        with self._mu:
            return any(now - at < ECHO_WINDOW for at in self._outs.get((chat, said), ()))

    # ---------------------------------------------------------------- the user's lines

    def take(self, row: dict, body: str, epoch: Optional[int] = None) -> bool:
        """A fresh line of the user's own (no AI's mark, no echo of /agent's), body as /agent reads it: True when it is
        the channel's (queued for Persona, or nothing to hand over, or a stop of an /agent request from before);
        False lets /agent go on with it."""
        chat = self.chat()
        if not chat or row.get("chat_id") != chat:
            return False
        app, agent = self.app, self.app.agent
        store = app.store
        said = (row.get("text") or "").strip()  # as the user said it (an @ in it is theirs to keep)
        if agent._other_ai(said) or self._ours(chat, row):
            return True  # an AI's line, or what this program sent there itself (a tool's, an inbox reply): not theirs
        req = match_prefix(said, agent.prefix)
        if req is None:
            req = match_prefix(body or "", agent.prefix)  # after the @s it starts with: "@x /agent …"
        text = said if req is None else req
        unsupported = ""
        if (row.get("type") or "TEXT").upper() not in TEXT_TYPES:
            # a picture, a file, a voice message: the channel takes none yet (images_in, files_in: false), and the
            # words standing for it ("[图片]") are not the user's
            text, unsupported = "", "%s（这个渠道还转交不了，请在 App 里发）" % _KIND_WORDS.get(
                (row.get("type") or "").upper(), "附件")
        if text.lower() in STOP_WORDS:
            if self._stops_old(chat, row, epoch):
                return True
            text = "/stop"
        quote = self._quote(chat, row)
        if not text and not quote and not unsupported:
            return True  # a bare /agent: nothing to hand over (and no /agent of its own here)
        came = float(row.get("received") or 0) or time.time()  # when it came here (this computer's clock)
        with self._intake, app.owned(epoch) as ok:
            if not ok or not app.signed_in or came <= self._signed_out:
                return True  # heard before a sign-out: nothing said before it goes after it
            if not store.channel_msg_put(chat, row["id"], "in", owner=getattr(app, "_owner", ""),
                                         placement=row.get("placement") or "", root_id=row.get("root_id") or "",
                                         text=text, quote=quote, parent=row.get("parent_id") or "",
                                         unsupported=unsupported, state="pending", now=came):
                return True  # taken already (delivered again)
            if epoch is not None:
                with self._mu:
                    self._epochs[(chat, row["id"])] = epoch
        self._queued.set()
        return True

    def _stops_old(self, chat: str, row: dict, epoch: Optional[int]) -> bool:
        """A stop quoting an /agent request from before the channel (or in its thread), not one of the channel's own
        messages: that request is stopped, the /agent way (True)."""
        app, store = self.app, self.app.store
        parent, root = row.get("parent_id") or "", row.get("root_id") or ""
        if not (parent or root):
            return False
        mine = store.channel_msg(chat, parent) if parent else None
        if mine is not None and mine["dir"] in ("in", "out"):
            return False  # it quotes the channel's: the channel's stop
        with app.owned(epoch) as ok:
            if not ok or app.agent._target(row) is None:
                return False
            if not store.seen_add("stop:%s:%s" % (chat, row["id"])):
                return True
        threading.Thread(target=app.agent._on_stop, args=(row, epoch), name="openmate-agent-stop", daemon=True).start()
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
        """Hand the taken lines over one at a time, in the order they came: a /stop never overtakes the request before
        it, and a line cut off by a restart is handed over after it (if still fresh)."""
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
                    break  # tried again soon
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
        if self.app.owns(epoch, owner=r["owner"]) and self.chat() == chat:
            self._note(chat, "channel-%s:%s:%s" % (state, chat, mid), note,
                       _where(mid, r["root_id"], r["placement"], ""), epoch, r["owner"])

    def _current(self, r: dict, epoch: Optional[int]) -> str:
        """Why the line cannot go ("" when it can go now). Raises _Later when it must wait."""
        app, store = self.app, self.app.store
        cur = store.channel_msg(r["chat_id"], r["id"])
        if cur is None or cur["state"] != "pending":
            return "gone"  # wiped by another account's sign-in, let go at a sign-out, or settled
        if not app.owns(epoch, owner=r["owner"]):
            return "expired"  # heard for another account
        if not app.signed_in or app.account is None or app.link != "up":
            raise _Later()  # not signed in (yet), or the link down: after a restart the account is restored first
        if r["owner"] and app.account.id != r["owner"]:
            return "expired"
        if time.time() - float(r["at"]) > app.timing.agent_fresh:
            return "stale"
        if r["chat_id"] != self.chat():
            return "dropped"  # the channel chat is another one by now (or none)
        if may_answer(r["text"]):
            if store.channel_asked_since(r["chat_id"], float(r["at"])):
                return "new_ask"  # Persona would read it as the answer to a question that came after it
            quoted = self._quoted_ask(r)
            if quoted and store.channel_asked_since(r["chat_id"], store.channel_ask_first(quoted) or 0, quoted):
                return "new_ask"  # it answers the question it quotes, and a newer one came after that one
        return ""

    def _quoted_ask(self, r: dict) -> str:
        """The question a line quotes (its message, or a piece of it), "" for none."""
        if not r["parent"]:
            return ""
        m = self.app.store.channel_msg(r["chat_id"], r["parent"])
        got = _ASK_REF.match(m["ref"]) if m is not None and m["dir"] == "out" else None
        return got.group(1) if got else ""

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
        if r["unsupported"]:
            msg["unsupported"] = [r["unsupported"]]
        waits = list(app.timing.channel_retry)
        while True:
            with self._submit:  # its last check and the hand-over: a sign-out comes before or after both
                why = self._current(r, epoch)
                if why == "gone":
                    with self._mu:
                        self._epochs.pop((chat, mid), None)
                    return
                if not why:
                    if may_answer(r["text"]):
                        # it may answer the questions open here that came before it: not taken back once it is handed
                        store.channel_mark_here(chat, mid, float(r["at"]))
                    try:
                        app.hub_call("channel.receive", {"contribution": self.id, "message": msg})
                        store.channel_in_set(chat, mid, "handed")  # before a sign-out can let it go
                        why = "handed"
                    except RPCError as e:
                        if e.code in FINAL_CODES:
                            log.warning("a line was not handed to Persona's channel (%s)", e.code)
                            why = "refused"
                        # else: Persona may have it (an answer lost): the same id again (it takes each id once), till
                        # the line is too old (stale)
            if why:
                break
            wait = waits.pop(0) if len(waits) > 1 else (waits[0] if waits else 5.0)
            if self._stop.wait(wait) or app.closing.is_set():
                return  # left pending: handed over after the restart
        if why == "handed":
            with self._mu:
                self._epochs.pop((chat, mid), None)
        elif why == "new_ask":
            self._settle(r, "dropped", NEW_ASK_NOTE, epoch)
        elif why == "stale":
            self._settle(r, "expired", FAILED_NOTE % "很久都没能确认 Persona 收到", epoch)
        elif why == "refused":
            self._settle(r, "failed", FAILED_NOTE % "Persona 没有接收", epoch)
        else:
            self._settle(r, why)

    def _note(self, chat: str, key: str, text: str, where: dict, epoch: Optional[int], owner: str) -> None:
        try:
            self._deliver(chat, key, text, where, "Persona 渠道把提示以你的身份发到",
                          self.app.account_epoch() if epoch is None else epoch, owner)
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
            store.channel_prune(now - KEEP, now - REFS_KEEP)
        before = store.channel_asks_open()  # taken before the list is read: one sent meanwhile waits for the next round
        if not before:
            self._history.clear()
            return
        epoch = app.account_epoch()
        waiting = self._waiting()
        if waiting is None:
            return
        chat = self.chat()
        history = self._history
        wanted = {(b["machine_id"], b["session_id"], b["raised"] or rfc3339(float(b["at"]) - 86400)) for b in before}
        for k in [k for k in history if k not in wanted]:
            del history[k]  # no question read that way followed any more
        for h in history.values():
            h["failed"] = False  # read again (from where it stopped) this round
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
                    store.channel_ask_set(ix, ref, seen=1, raised=str(item.get("created_at") or ""),
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
            how = self._take_back(a["chat_id"], a["message_id"], epoch)
            if how == "failed" and int(a["tries"]) + 1 < TAKE_BACK_TRIES:
                store.channel_ask_end(ix, ref, "open")  # the service away: tried again next round
                store.channel_ask_set(ix, ref, tries=int(a["tries"]) + 1)
            else:
                store.channel_ask_end(ix, ref, how)

    def _ended(self, a: dict, history: Dict[tuple, dict]) -> bool:
        """Whether the question history says the question ended for good: its session's records since it was raised
        (as the waiting list said), page after page till it is found (read once a round per session)."""
        mid, sid = a["machine_id"], a["session_id"]
        if not sid:
            return False
        since = a["raised"] or rfc3339(float(a["at"]) - 86400)
        h = history.setdefault((mid, sid, since), {"statuses": {}, "cursor": ("", ""), "done": False, "failed": False})
        pages = 0  # this round's: the next round goes on from where this one stopped
        while a["ix"] not in h["statuses"] and not h["done"] and not h["failed"] and pages < HISTORY_PAGES:
            pages += 1
            try:
                recs, cursor = self.app.hub.interaction_history(mid, sid, since, *h["cursor"])
            except HubError as e:
                log.info("the question history could not be read (%s)", e.code or e.status)
                h["failed"] = True
                break
            for r in recs:  # only how one ended for good is kept: one still waiting is read again later
                if str(r.get("status") or "") in FINAL:
                    h["statuses"].setdefault(str(r["interaction"].get("id")), str(r["status"]))
            h["cursor"] = cursor
            h["done"] = not cursor[0]
        status = h["statuses"].get(a["ix"])
        if status is None and h["done"]:
            h.update(cursor=("", ""), done=False)  # read to the end without it: from the newest again next round
        return status in FINAL

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


_KIND_WORDS = {"IMAGE": "图片", "FILE": "文件", "AUDIO": "语音", "VOICE": "语音", "VIDEO": "视频", "MEDIA": "视频",
               "STICKER": "表情"}


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_NO_LOCK = _NoLock()


def _picture_key(channel: str, ref: str, i: int) -> str:
    """The send journal's key of a channel.send's i-th picture: a namespace of its own, apart from every text's
    ("channel:<id>:<ref>"), whatever the ref is."""
    return "channel-picture:%s:%d:%s" % (channel, i, ref)


def _never() -> dict:
    raise RPCError("没有发出")  # never called: send_once answers a key it has seen from its journal


def _answer(res) -> dict:
    """channel.send's answer from what the send journal kept: the message's id, when there is one."""
    mid = str((res or {}).get("message_ref") or "") if isinstance(res, dict) else ""
    return {"message_id": mid} if mid else {}
