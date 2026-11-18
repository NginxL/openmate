"""OpenMate for WeChat: the platform half of the openmate-wechat extension.

Phase one: /agent (the user's own WeChat message starting with the prefix) and 自动代答 in the groups the user marks on
the 微信 page (someone else's real @ of the user). The link to WeChat is the matewechat-native program (this
repository's native/matewechat-native, over thisnick/agent-wechat's MIT protocol library), fetched once and pinned by
SHA-256 (install.py). OPENMATE_BACKEND=fixture selects the core's deterministic fixture instead (tests, conformance).
"""

from __future__ import annotations

import os
import re
from typing import Dict

from openmate_core.backend import Backend, BackendError
from openmate_core.main import run
from openmate_core.platform import Platform

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTENSION_ID = "openmate-wechat"
CONNECTOR_ID = "matewechat"
CHATS_PAGE = "wechat-chats"
CHATS_CARD = "wechat-chats-today"
AT_PREFIX = re.compile("^(?:\\s*@[^\u2005\\n]{1,1024}\u2005)+")  # WeChat's real @s: "@name" ended by U+2005


def mark_view(text: str) -> str:
    """The text without the real @s it starts with: where a reply's AI mark is found (the message keeps them)."""
    return AT_PREFIX.sub("", text or "", count=1)


def make_backend(config: Dict, data_dir: str) -> Backend:
    which = (os.environ.get("OPENMATE_BACKEND") or "native").strip().lower()
    if which == "fixture":
        from openmate_core.fixture import FixtureBackend

        return FixtureBackend()
    if which != "native":
        raise BackendError("OPENMATE_BACKEND=%s 不认识：只有 native 和 fixture" % which, "config")
    from .backend import WeChatBackend
    from .install import Installer

    inst = Installer(data_dir, str(config.get("native_path") or ""))
    inst.start()  # in the background: the extension answers the hub meanwhile
    return WeChatBackend(inst.path, data_dir)  # path(wait): waits that long for a download in progress


def platform(manifest_path: str = "") -> Platform:
    return Platform(
        extension_id=EXTENSION_ID,
        connector_id=CONNECTOR_ID,
        name="微信",
        manifest_path=manifest_path or os.path.join(ROOT, "persona-extension.json"),
        make_backend=make_backend,
        agent_purpose="lark.agent",  # the hub's /agent model setting (Settings › Model configuration › Lark /agent)
        login_hint="用微信扫码，并在手机上确认登录。会作为一台新电脑登录：你已登录的电脑版微信可能会下线；"
                   "这是非官方协议，不能排除账号被限制的可能。",
        login_hint_scanned="已扫码，请在手机微信上点“登录”",
        login_hint_expired="二维码过期了，已换一张新的，请重新扫码",
        chats_page=CHATS_PAGE,
        chats_card=CHATS_CARD,
        can_edit=False,
        mark_view=mark_view,
        # phase one is /agent and 自动代答 only: no generic sends, inbox, conversation pages or archive
        methods_off=("actions.list", "actions.describe", "actions.do", "inbox.collect", "inbox.reply",
                     "thread.page", "thread.media", "archive.changes", "archive.media"),
    )


def main() -> int:
    return run(platform())
