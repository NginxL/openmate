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

    #: Set by the core: call it with new credentials when the service rotated them (a refreshed session), so the core
    #: seals them in the hub. Never write credentials anywhere yourself.
    on_credentials: Optional[Callable[[Dict], None]] = None
    #: Set by the core too: seal these credentials now and say whether it worked (True) — one at a time, in the order
    #: called. For a service that must not use rotated credentials before they are stored (WeChat); on_credentials
    #: stays the fire-and-forget form.
    on_credentials_sync: Optional[Callable[[Dict], bool]] = None

    #: Set by the core: call it with a DocMention when the live link hears the user was @-mentioned in a document's
    #: comment (from the listener's own thread; it returns at once).
    on_doc_mention: Optional[Callable[["DocMention"], None]] = None

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
        ends or stop is set. Raise CredentialsExpired when the service says the sign-in is gone, BackendError("…",
        "proxy") when the proxy cannot work, BackendError("…", "unreachable") for a connection that failed."""

    # reads

    @abc.abstractmethod
    def history(self, chat_id: str, before_position: Optional[int] = None, limit: int = 50) -> List[Message]:
        """Up to limit messages of chat before before_position (the newest when None), oldest first."""

    @abc.abstractmethod
    def recent_chats(self, limit: int) -> List[Chat]:
        """The account's most recently active conversations, newest first (the one-time initial history)."""

    @abc.abstractmethod
    def search(self, query: str, limit: int = 20) -> List[Person]:
        """People and groups matching query."""

    @abc.abstractmethod
    def chat_info(self, chat_id: str) -> Chat:
        """One conversation's kind and name."""

    @abc.abstractmethod
    def user_name(self, user_id: str, chat_id: Optional[str] = None) -> str:
        """A person's display name ("" when unknown); chat_id when the name is per chat (an alias in a group)."""

    def doc_text(self, doc_url: str, most: int) -> str:
        """A document's text (read as the user), at most `most` characters; "" when it can't be read. For a guest
        conversation answering its comment, which can't read anything itself."""
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

    def people_kinds(self, user_ids: List[str]) -> Dict[str, str]:
        """Which of these are programs ("bot": bots and AI assistants) and which people ("user"), for those the service
        can tell; {} when it cannot. Read-only; the core asks in the background, at most 50 at a time."""
        return {}

    @abc.abstractmethod
    def media(self, ref: str, max_bytes: int) -> Tuple[str, bytes]:
        """(mime, bytes) of a media ref; BackendError(kind="too_large") when it is bigger than max_bytes."""

    # writes (the core calls them only after the user agreed: a confirmed action, an inbox Send, a /agent reply)

    @abc.abstractmethod
    def send(self, chat_id: str, text: str, reply_to: Optional[str] = None, root: Optional[str] = None) -> str:
        """Send text as the account and return the new message's id. reply_to: the message it answers (quoted);
        root: the thread to post in (reply_to == root with no thread yet: open a thread on that message). Raise
        SendRejected only when it surely did not go out."""

    @abc.abstractmethod
    def create_chat(self, user_id: str) -> str:
        """The direct chat with a person (created when there is none yet, without sending anything); its chat id."""

    def close(self) -> None:
        """The program is stopping."""
