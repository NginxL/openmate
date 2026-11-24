"""The platform interface: what a chat service adapter (Feishu through LarkAgentX, later WeChat, DingTalk) implements.

The core owns everything above it: the store, the inbox, threads, the archive, sends with their journal, the link's
reconnect policy, /agent. An adapter only talks to its service, and is called from several threads at once (the link
thread runs listen; sends, history and media come from request threads), so it must be thread-safe.

Errors an adapter raises are BackendError (or a subclass) with a Chinese sentence for people: never a token, a cookie,
a phone number or a message body, since status and logs show it.
"""

from __future__ import annotations

import abc
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple


# ---- data ----


@dataclass
class Media:
    """A picture, file, sticker, video or voice message in a message. ref names the bytes for media(): it must never
    change meaning (the hub keeps what a ref fetched for good)."""

    kind: str  # image | sticker | video | voice | file
    ref: str = ""
    mime: str = ""
    name: str = ""
    size: int = 0
    width: int = 0
    height: int = 0

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in ("", 0, None)}


@dataclass
class Message:
    """One chat message, normalized.

    position is the message's place in its chat: an integer that grows with time, so history(before_position) can page
    back. root_id is the thread (topic) root the message is in ("" outside a thread); parent_id is the message it quotes
    or replies to. mine: sent by the signed-in account. at_me: it @-mentions the signed-in account (an @all alone is
    at_all, not at_me). type is upper case: TEXT, POST, IMAGE, FILE, STICKER, AUDIO, VIDEO, CARD, SYSTEM…
    placement: where a group message shows, as the service says it when it is read: "thread" (only in the thread on
    root_id), "thread_chat" (in that thread and also in the chat's main stream), "chat" (in the main stream, quoting
    or not); "" when the backend cannot tell (a direct chat, a thread-first group). A /agent reply goes back there.
    same_as: the message this one is a copy of (Feishu's copy in the main stream of a thread reply also sent there):
    a copy is never a command or a question to answer, its original is. Neither is stored: only a live read hands them on (the core's rows of new
    messages carry them).
    text_after_mentions: the text after the @-mentions it starts with, cut where the service's own @ elements end
    (an "@Jane Doe" with a space in the name, or one right before the words; the whole text when it starts with
    none); None when the backend cannot tell (the core then cuts at spaces). edited: this reading is of an edited
    message (True), surely not (False), or the backend could not tell (None: never a new @自己 request). Live-only
    too, like placement.
    """

    id: str
    chat_id: str
    position: int
    sender_id: str = ""
    sender_name: str = ""
    mine: bool = False
    type: str = "TEXT"
    text: str = ""
    at: float = 0.0  # epoch seconds
    root_id: str = ""
    thread_id: str = ""
    parent_id: str = ""
    at_me: bool = False
    at_all: bool = False
    media: List[Media] = field(default_factory=list)
    blocked: bool = False
    placement: str = ""
    same_as: str = ""
    text_after_mentions: Optional[str] = None
    edited: Optional[bool] = False


@dataclass
class Chat:
    """A conversation: kind dm (one person) or group. peer_id is a dm's other person; topic marks a thread-first group."""

    id: str
    kind: str
    name: str = ""
    peer_id: str = ""
    topic: bool = False
    members: int = 0
    muted: bool = False


@dataclass
class DocMention:
    """Someone @-mentioned the user in a document's comment (a doc, a wiki page, a sheet…): once per notice id."""

    id: str
    doc_url: str
    doc_title: str = ""
    comment_id: str = ""
    from_id: str = ""
    from_name: str = ""
    text: str = ""
    kind: str = "mention"  # mention | comment | reply
    at: float = 0.0


@dataclass
class Person:
    """A search hit: kind user (id is a user id) or group (id is a chat id)."""

    kind: str
    id: str
    name: str
    detail: str = ""


@dataclass
class Account:
    """Who is signed in."""

    id: str
    name: str


@dataclass
class LoginQR:
    """A sign-in QR code: url is what the code contains (the hub draws it); state is the adapter's own handle for
    login_poll / login_finish (kept in memory only, never shown)."""

    url: str
    state: str


@dataclass
class LoginDone:
    """A finished sign-in: credentials is a JSON-serialisable dict the core seals in the hub's connector secrets
    (secrets.save) and hands back to restore() on the next start. It never goes to a file or a log."""

    credentials: Dict
    account: Account


# ---- errors ----


class BackendError(Exception):
    """Something the service or the adapter could not do. kind says what for the core:

    expired      the sign-in is no longer valid (raise CredentialsExpired)
    unreachable  the service cannot be reached now (the core retries with a backoff)
    proxy        the proxy cannot be used as configured (for example SOCKS without python-socks)
    config       the adapter is not set up (a missing path, an unsupported setting)
    too_large    media() would return more than max_bytes
    not_found    no such chat, user or media
    unsupported  media() got this file, and it is not what it should be (an encrypted picture): asking again won't help
    rejected     the service refused a request (raise SendRejected for a send that surely did not go out)
    unknown      anything else
    """

    def __init__(self, message: str, kind: str = "unknown"):
        super().__init__(message)
        self.message = message
        self.kind = kind


class CredentialsExpired(BackendError):
    def __init__(self, message: str = "飞书登录已失效，需要重新扫码登录"):
        super().__init__(message, "expired")


class SendRejected(BackendError):
    """A send that certainly did not go out (refused before anything was transmitted, or refused by the service). Any
    other exception from send() is taken as "it may have gone out", and the core will never send it again."""

    def __init__(self, message: str):
        super().__init__(message, "rejected")


# ---- the interface ----

OnMessage = Callable[[Message], None]
OnLink = Callable[[str, str], None]  # (state, reason): state is up | down | expired


class Backend(abc.ABC):
    """A chat service adapter.

    The core calls, in this order: login_start / login_poll / login_finish when the user signs in, or restore with the
    saved credentials at start; then listen in its own thread, again after each return (with a backoff it owns:
    exponential, reset once a link stayed up for a minute); everything else from other threads at any time.
    """

    #: Set by the core as soon as it has read them (and again whenever they change; None once signed out): the
    #: saved credentials restore() will get. An adapter whose account must not change in place checks a sign-in
    #: against them even before a restore ran.
    saved_credentials: Optional[Dict] = None
    #: Set by the core too: the account its own store belongs to ("" before any sign-in), the same check's other half
    core_account: str = ""
    #: Set by the core before anything else is called: the manifest's secrets the user set (name → value), such as a
    #: token for a service the adapter connects to. Never logged or written anywhere.
    secrets: Dict[str, str] = {}
    #: Set by the core: call it with new credentials when the service rotated them (a refreshed session), so the core
    #: seals them in the hub. Never write credentials anywhere yourself.
    on_credentials: Optional[Callable[[Dict], None]] = None
    #: Set by the core too: seal these credentials now and say whether it worked (True) — one at a time, in the order
    #: called. For a service that must not use rotated credentials before they are stored (WeChat); on_credentials
    #: stays the fire-and-forget form.
    on_credentials_sync: Optional[Callable[[Dict], bool]] = None
    #: Set by the core: hand it messages to keep as history (from the listener's or a sender's thread): caught up
    #: after a reconnect, or the account's own sends the service never echoes back. Never acted on (/agent, 代答).
    on_history: Optional[Callable[[List["Message"]], None]] = None
    #: Set by the core too: the same, saying whose they are (an account id): dropped when another account is signed
    #: in by the time they are kept (an adapter that hands history over from another thread uses it)
    on_history_for: Optional[Callable[[List["Message"], str], None]] = None
    #: Set by the core: call it with {user_id: "bot" | "user"} when the backend learns what someone is after it
    #: answered people_kinds (WeChat finds an official account with a person-like id later): the core keeps what it
    #: was told before for good, so this is how it changes (from any thread; it returns at once).
    on_people_kinds: Optional[Callable[[Dict[str, str]], None]] = None
    #: Set by the core: call it with a chat's id when the account left or deleted it (a group quit on the phone): the
    #: core takes it out of its lists and unmarks it for 自动代答; a newer message brings it back (any thread). An
    #: adapter may add account=<the account id it heard it for>: dropped once another account is signed in.
    on_chat_gone: Optional[Callable[..., None]] = None

    #: Set by the core: call it with a DocMention when the live link hears the user was @-mentioned in a document's
    #: comment (from the listener's own thread; it returns at once). account=<the link's account id> as for
    #: on_chat_gone.
    on_doc_mention: Optional[Callable[..., None]] = None

    #: Set by the core: call it with {chat_id: when (unix seconds, 0 when unknown)} when the live link hears the user
    #: marked chats done in the service's own client (Feishu's 完成), from the listener's own thread (it returns at
    #: once). The core takes it as a hint and asks chat_done_states about those chats. For a backend with done_marks.
    on_chats_done: Optional[Callable[[Dict[str, float]], None]] = None
    #: Set by the core: call it with the ids of messages the live link hears the account put a reaction on (from the
    #: listener's own thread; it returns at once). The core takes it as a hint and asks my_reactions. With done_marks.
    on_reacted: Optional[Callable[[List[str]], None]] = None
    #: The service has a per-chat done mark (Feishu's 完成: the chat leaves the message list until a new message): the
    #: core asks chat_done_states about the chats that need the user and counts the marked ones as 处理完.
    done_marks: bool = False
    #: Types a message of which an older reading may have stored with no text at all (Feishu: a text that is only a
    #: link). When render_version changes, the core reads those messages again where they are (not only the newest).
    rerender_empty: Tuple[str, ...] = ()
    #: A reply in a group's thread quotes the thread's first message, whichever message of it was picked (Feishu: the
    #: send carries only the root). The confirm card says so, so it shows what the message will really quote.
    thread_reply_quotes_root: bool = False

    #: The version of how this backend reads messages into text. When it is newer than the one a store was filled
    #: with, the core reads the recent chats with a message of `rerender_types` once more (a stored message's text
    #: is renewed when it reads otherwise now).
    render_version: str = "1"
    rerender_types: Tuple[str, ...] = ()

    # sign-in

    @abc.abstractmethod
    def login_start(self) -> LoginQR:
        """Start a QR sign-in. Fast (one request)."""

    @abc.abstractmethod
    def login_poll(self, state: str) -> str:
        """Where the QR sign-in stands, without blocking for long: waiting | scanned | expired | confirmed."""

    @abc.abstractmethod
    def login_finish(self, state: str) -> LoginDone:
        """After login_poll said confirmed: complete it, leave the adapter signed in, return the credentials."""

    #: True for an adapter whose sign-in has nothing to scan (login_direct answers): it takes the account signed in
    #: where it connects, so the core reads an unread saved sign-in before it
    signs_in_elsewhere: bool = False

    def login_direct(self) -> Optional[LoginDone]:
        """A sign-in with nothing to scan, for an adapter whose account is signed in where it connects (WeChat through
        AgentWechat): its LoginDone, as login_finish's. None (the default): sign in with login_start's QR.
        BackendError: it cannot sign in now (its message is shown to the user)."""
        return None

    @abc.abstractmethod
    def restore(self, credentials: Dict) -> Account:
        """Sign in again with saved credentials (no QR). Raise CredentialsExpired when they no longer work."""

    def logout(self) -> None:
        """Forget the session (the core deletes the saved credentials itself)."""

    # the live link

    @abc.abstractmethod
    def listen(self, on_message: OnMessage, on_link: OnLink, stop: threading.Event) -> None:
        """Run one live connection: call on_link("up", "") once it is really up (connected and receiving, not merely
        dialling), on_message for every new message (the account's own included), and return when the connection
        ends or stop is set. on_message raises when the core could not take the message (it is not stored): do not
        count it delivered (no ack that loses it, no cursor past it); deliver it again, or let a history read bring
        it. Raise CredentialsExpired when the service says the sign-in is gone, BackendError("…", "proxy") when the
        proxy cannot work, BackendError("…", "unreachable") for a connection that failed."""

    # reads

    @abc.abstractmethod
    def history(self, chat_id: str, before_position: Optional[int] = None, limit: int = 50) -> List[Message]:
        """Up to limit messages of chat before before_position (the newest when None), oldest first."""

    @abc.abstractmethod
    def recent_chats(self, limit: int) -> List[Chat]:
        """The account's most recently active conversations, newest first (the initial history; the catch-up after
        each reconnect). BackendError when the service could not say: an empty list means there are none."""

    @abc.abstractmethod
    def search(self, query: str, limit: int = 20) -> List[Person]:
        """People and groups matching query."""

    @abc.abstractmethod
    def chat_info(self, chat_id: str) -> Chat:
        """One conversation's kind and name."""

    @abc.abstractmethod
    def user_name(self, user_id: str, chat_id: Optional[str] = None) -> str:
        """A person's display name ("" when unknown); chat_id when the name is per chat (an alias in a group)."""

    def chat_done_states(self, chat_ids: List[str]) -> Dict[str, Optional[float]]:
        """Where these chats stand in the service's own client now, for those it answered about: when the chat last
        changed there (unix seconds, 0 when unknown) for one the user marked done and no new message reopened, None
        for one that is not done. A chat it could not ask about is left out; BackendError when it could ask about
        none (CredentialsExpired when the sign-in is gone). A backend with done_marks."""
        return {}

    def my_reactions(self, message_ids: List[str], ignore: Tuple[str, ...] = ()) -> Dict[str, float]:
        """Of these messages, the ones the account has a reaction on now (a DONE, a 👍 …: an answer without words),
        reactions of the types in ignore not counted (the /agent's own 🤔 and ❌ say nothing was answered):
        {message_id: when (unix seconds, 0 when unknown)}. BackendError when it could ask about none. With
        done_marks; the core counts the need that message made as 处理完, as a reply would."""
        return {}

    def doc_text(self, doc_url: str, most: int) -> str:
        """A document's text (read as the user), at most `most` characters; "" when it can't be read. For a guest
        conversation answering its comment, which can't read anything itself."""
        return ""

    def doc_writer_name(self, fresh: bool = False) -> str:
        """The display name of the account doc_text / reply_doc_comment act as ("" when unknown): a 文档代答 goes out
        only when it is the signed-in account's own. fresh: asked now, not from a cache (right before a write)."""
        return ""

    def reply_doc_comment(self, doc_url: str, comment_id: str, text: str) -> str:
        """Reply to a document's comment as the user; the reply's id. BackendError when it can't (no such comment, a
        solved one, no rights)."""
        raise BackendError("这个平台不支持回复文档评论", "unsupported")

    def react(self, chat_id: str, message_id: str, reaction: str) -> None:
        """Put a reaction (the service's emoji key, e.g. THINKING) on a message, as the account. BackendError when
        the service has none or refused it. The core uses it to say a /agent request was taken."""
        raise BackendError("这个平台不支持表情回应", "unsupported")

    def unreact(self, chat_id: str, message_id: str, reaction: str) -> None:
        """Take back a reaction the account put (react). BackendError when the service has none or refused it. The
        core swaps the /agent request's 🤔 for how it ended."""
        raise BackendError("这个平台不支持撤回表情回应", "unsupported")

    def edit(self, chat_id: str, message_id: str, text: str) -> None:
        """Replace the text of a message the account sent (send). BackendError when the service has no editing or
        refused it (too old, not the account's). The core edits a /agent run's progress message in place."""
        raise BackendError("这个平台不支持编辑消息", "unsupported")

    #: Whether recall works: the 渠道 takes back a question it put in the chat once it was answered elsewhere
    can_recall: bool = False

    def recall(self, chat_id: str, message_id: str) -> None:
        """Take back (recall) a message the account sent (send), for everyone in the chat. BackendError when the
        service has none or refused it (too old, not the account's)."""
        raise BackendError("这个平台不支持撤回消息", "unsupported")

    def people_kinds(self, user_ids: List[str]) -> Dict[str, str]:
        """Which of these are programs ("bot": bots and AI assistants) and which people ("user"), for those the service
        can tell; {} when it cannot. Read-only; the core asks in the background, at most 50 at a time."""
        return {}

    def group_candidates(self, limit: int) -> List[Chat]:
        """Every group the account is in that the service can list (for the 自动代答 switches), the newest first; []
        when it cannot. Quick and offline: the adapter's own cache, kept fresh in the background (a group made or
        joined shows up there), since the page calls it while it draws."""
        return []

    @abc.abstractmethod
    def media(self, ref: str, max_bytes: int) -> Tuple[str, bytes]:
        """(mime, bytes) of a media ref; BackendError(kind="too_large") when it is bigger than max_bytes."""

    # writes (the core calls them only after the user agreed: a confirmed action, an inbox Send, a /agent reply)

    @abc.abstractmethod
    def send(self, chat_id: str, text: str, reply_to: Optional[str] = None, root: Optional[str] = None,
             to_chat: bool = False) -> str:
        """Send text as the account and return the new message's id. reply_to: the message it answers (quoted);
        root: the thread to post in (reply_to == root with no thread yet: open a thread on that message). to_chat:
        into root's thread and also into the chat's main stream, quoting reply_to (the core passes it only when true,
        and only for a message the backend read as "thread_chat"). A backend with can_mention also takes mentions
        [(user id, name)]: real @s of those people (the core passes it only when there are some). Raise SendRejected
        only when it surely did not go out."""

    #: Whether send takes mentions (real @s that tell those people); the send tool offers them only then
    can_mention: bool = False

    #: Whether chats may be thread-first (Chat.topic: every message opens a topic or replies in one); the send card
    #: asks chat_info only then
    topic_groups: bool = False

    @abc.abstractmethod
    def create_chat(self, user_id: str) -> str:
        """The direct chat with a person (created when there is none yet, without sending anything); its chat id."""

    #: Whether send_image works now (an /agent reply may carry a picture then; the tool offers it only then)
    can_send_images: bool = False

    def send_image(self, chat_id: str, path: str) -> str:
        """Send the picture at path (a PNG or JPEG of at most 10 MB the core checked and copied into the extension's
        data) into chat_id as the account; the new message's id. Raise SendRejected only when it surely did not go
        out; BackendError for an unknown outcome (never sent again)."""
        raise BackendError("这个平台不支持发图片", "unsupported")

    def close(self) -> None:
        """The program is stopping."""
