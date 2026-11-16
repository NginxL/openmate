"""What a platform package tells the core about itself, and the core's timings (tests shorten them)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Tuple

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
    agent_poll: float = 4.0  # how often a running /agent's chat is read
    agent_timeout: float = 4 * 3600.0
    agent_fresh: float = 600.0  # a request older than this is not taken
    agent_keep: float = 3 * 86400.0  # runs kept this long, at most agent_keep_most of them
    agent_keep_most: int = 200
    agent_hub_wait: float = 300.0  # how long a /agent request waits for the hub to be reachable before it fails
    agent_hub_probe: float = 5.0  # meanwhile the hub is asked this often whether it answers
    agent_start_tries: int = 3  # starts that may have reached the hub (each with a new request id) before it fails
    agent_settle: float = 8.0  # a session quiet this long after it answered: its status says whether the turn ended
    secrets_retry: float = 2.0
    sent_retry: Tuple[float, ...] = (5.0, 30.0, 120.0)  # waits before sent.record is tried again (the hub was busy)
    extra: Dict[str, float] = field(default_factory=dict)
