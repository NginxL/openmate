"""OpenMate for WeChat: the platform half of the openmate-wechat extension.

Phase one: /agent (the user's own WeChat message starting with the prefix) and 自动代答 in the groups the user marks on
the 微信 page (someone else's real @ of the user). The link to WeChat is the matewechat-native program (this
repository's native/matewechat-native, over thisnick/agent-wechat's MIT protocol library), fetched once and pinned by
SHA-256 (install.py). OPENMATE_BACKEND=fixture selects the core's deterministic fixture instead (tests, conformance).
"""

from __future__ import annotations

import os
import re
from typing import Dict, Optional

from openmate_core.backend import Backend, BackendError
from openmate_core.main import run
from openmate_core.platform import Platform

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTENSION_ID = "openmate-wechat"
CONNECTOR_ID = "matewechat"
CHATS_PAGE = "wechat-chats"
CHATS_CARD = "wechat-chats-today"
# One of WeChat's real @s: "@name" ended by U+2005. A name with an "@" in it cannot be told from a typed "@x …" before a
# real @, so it is never taken off: a missed /agent is safe, a sentence turned into a command is not.
AT_ONE = re.compile("^\\s*@([^\u2005\\n]{1,1024}?)\u2005")
TYPED_AT = re.compile("@")  # any "@" inside means a typed "@x …" came first: then nothing is taken off


def mark_view(text: str) -> str:
    """The text without the real @s it starts with: where a reply's AI mark is found and a request is read (the
    message keeps them)."""
    t = text or ""
    while True:
        m = AT_ONE.match(t)
        if m is None or TYPED_AT.search(m.group(1)):
            return t
        t = t[m.end():]


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
    # path(wait): waits that long for a download in progress
    return WeChatBackend(inst.path, data_dir, ack_text=str(config.get("agent_ack_reaction") or ""))


def archive_chat(chat_id: str, chat, config: Dict) -> Optional[bool]:
    """What goes to the knowledge base: direct chats and the user's own notes (文件传输助手); groups only with the setting
    群聊进知识库 (the others in them did not agree to be recorded); never official accounts or WeChat's own. A chat
    with an account of its own choosing (腾讯科技 is qqtech) that WeChat has not been asked about yet: None, so the
    core holds its messages until WeChat says (an official account's must never reach the archive first)."""
    from .backend import ANSWERED, PERSONS, SURE_PEOPLE, is_group, kind_of

    if is_group(chat_id) or (chat or {}).get("kind") == "group":
        return bool(config.get("archive_groups"))
    if chat_id == "filehelper":
        return True
    if kind_of(chat_id) != "user":
        return False  # an official account (known as one), WeChat's own, or not a person's id at all
    if chat_id.startswith("wxid_") or chat_id.endswith(SURE_PEOPLE) or chat_id in PERSONS or chat_id in ANSWERED:
        return True  # wxid_… ids are given to people only; the others as WeChat described them
    return None


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
        archive_chat=archive_chat,
        handle_kind="",  # no sender handles in archive batches (WeChat ids are not a kind the knowledge base knows)
        today_api_callers=("extension:openmate-feishu",),  # Feishu's 今天 shows these chats (its own HTTP)
    )


def main() -> int:
    return run(platform())
