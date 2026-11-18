"""What a platform package tells the core about itself, and the core's timings (tests shorten them)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple

from .backend import Backend


@dataclass
class Platform:
    #: the extension's id (persona-extension.json "id")
    extension_id: str
    #: the connector's id: the prefix of every tool name ("matefeishu" → matefeishu_send)
    connector_id: str
    #: the service's name in sentences ("飞书")
    name: str
    #: the source manifest (persona-extension.json)
    manifest_path: str
    #: makes the adapter from the extension's configuration and data directory; raises BackendError(kind="config")
    #: when it cannot (a missing setting)
    make_backend: Callable[[Dict, str], Backend]
    #: the kind of the sender handles in archive batches (kbwire handle kinds: lark, email, phone, telegram…)
    handle_kind: str = ""
    #: link hosts whose documents /agent looks up (with their subdomains)
    doc_hosts: Tuple[str, ...] = ()
    #: the model purpose /agent conversations open with (POST /api/v1/assistant/conversations "purpose"); "" is the
    #: assistant's own. The hub knows "lark.agent" (Settings › Model configuration › Lark /agent).
    agent_purpose: str = ""
    #: the sign-in hints, in the QR step
    login_hint: str = "用手机 App 扫码，并在手机上确认登录"
    login_hint_scanned: str = "已扫码，请在手机上确认登录"
    login_hint_expired: str = "二维码过期了，已换一张新的，请重新扫码"
    #: the platform's own pages and cards, besides the core's chats page and card: app → {contribution id: ui.Page}
    ui_pages: Optional[Callable[[Any], Dict[str, Any]]] = None
    #: the contribution ids of the core's chats page and its card. Contribution ids are one namespace per hub, so a
    #: second platform installed beside another needs its own (Feishu keeps "chats", the id its users' layouts know)
    chats_page: str = "chats"
    chats_card: str = "chats-today"
    #: the service can edit a sent message (Backend.edit). Without it /agent sends no progress messages at all: each
    #: would be a new message in the chat, as the user
    can_edit: bool = True
    #: connector methods this platform does not serve at all (a platform that offers less than the core can do):
    #: they are not in the method table, so the hub's call gets unknown_method whatever its manifest says
    methods_off: Tuple[str, ...] = ()
    #: how the AI-mark check reads a message (None: as it is). WeChat drops the "@name " a reply starts with, so a
    #: mark on the first line after it is seen, the built-in marks and the configured header alike
    mark_view: Optional[Callable[[str], str]] = None


@dataclass
class Timing:
    debounce: float = 3.0  # connector.changed: at most one per scope this often
    backoff_min: float = 1.0  # the live link's reconnect backoff
    backoff_max: float = 60.0
    stable_after: float = 60.0  # a link up this long resets the backoff
    login_wait: float = 10.0  # how long a finished sign-in waits for the link and the first catch-up
    inbox_window: float = 3 * 86400.0  # chats active this recently are collected
    inbox_most: int = 200
    catchup_chats: int = 20  # chats read again after each reconnect
    catchup_messages: int = 30
    initial_messages: int = 50
    rerender_window: float = 14 * 86400.0  # a newer reading renews the chats with such messages this recent…
    rerender_chats: int = 30  # …at most this many of them…
    rerender_messages: int = 50  # …their latest messages
    agent_poll: float = 4.0  # how often a running /agent's chat is read
    agent_timeout: float = 4 * 3600.0
    agent_fresh: float = 600.0  # a request older than this is not taken
    agent_keep: float = 3 * 86400.0  # runs kept this long, at most agent_keep_most of them
    agent_keep_most: int = 200
    agent_hub_wait: float = 300.0  # how long a /agent request waits for the hub to be reachable before it fails
    agent_hub_probe: float = 5.0  # meanwhile the hub is asked this often whether it answers
    warm_pages: bool = True  # the pages read what they show in the background right after the start
    agent_model_wait: float = 4.0  # a reply waits this long at most for the hub to list its session's model
    agent_start_tries: int = 3  # starts that may have reached the hub (each with a new request id) before it fails
    agent_settle: float = 8.0  # a session quiet this long after it answered: its status says whether the turn ended
    secrets_retry: float = 2.0
    sent_retry: Tuple[float, ...] = (5.0, 30.0, 120.0)  # waits before sent.record is tried again (the hub was busy)
    extra: Dict[str, float] = field(default_factory=dict)
