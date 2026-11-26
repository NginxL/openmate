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

        b = FixtureBackend()
        b.can_mention = b.topic_groups = False  # standing in for WeChat: no @s by id, no topic groups
        return b
    if which != "native":
        raise BackendError("OPENMATE_BACKEND=%s 不认识：只有 native 和 fixture" % which, "config")
    from .backend import WeChatBackend

    hub_url = str(config.get("hub_url") or "")
    if hub_url.strip():  # through AgentWechat (hub.py): the program is not fetched nor run (the address as written)
        return WeChatBackend(None, data_dir, ack_text=str(config.get("agent_ack_reaction") or ""),
                             voice_cmd=str(config.get("voice_to_text") or ""), hub_url=hub_url)
    from .install import Installer

    inst = Installer(data_dir, str(config.get("native_path") or ""))
    inst.start()  # in the background: the extension answers the hub meanwhile
    # path(wait): waits that long for a download in progress
    return WeChatBackend(inst.path, data_dir, ack_text=str(config.get("agent_ack_reaction") or ""),
                         voice_cmd=str(config.get("voice_to_text") or ""))


def archive_chat(chat_id: str, chat, config: Dict) -> Optional[bool]:
    """What goes to the knowledge base: direct chats and the user's own notes (文件传输助手); groups only with the setting
    群聊进知识库 (the others in them did not agree to be recorded); never official accounts or WeChat's own. A chat
    with an account of its own choosing (腾讯科技 is qqtech) that WeChat has not described yet: None, so the core
    holds the archive back until WeChat says (an official account's must never reach the archive first)."""
    from .backend import PERSONS, SURE_PEOPLE, is_group, kind_of

    if is_group(chat_id) or (chat or {}).get("kind") == "group":
        return bool(config.get("archive_groups"))
    if chat_id == "filehelper":
        return True
    if kind_of(chat_id) != "user":
        return False  # an official account (known as one), WeChat's own, or not a person's id at all
    if chat_id.startswith("wxid_") or chat_id.endswith(SURE_PEOPLE) or chat_id in PERSONS:
        return True  # wxid_… ids are given to people only; the others once WeChat said so
    return None  # WeChat has not said (or gave no flags): the core asks, and holds the archive back meanwhile


MEDIA_TOOL = CONNECTOR_ID + "_media"


def media_tools(app) -> Dict:
    """matewechat_media: a picture, voice note, video or file of one message, as local files (fetched when not yet)."""
    schema = {
        "name": MEDIA_TOOL,
        "description": "看一条微信消息里的图片、语音、视频或文件：下载到本机（还没下载时），返回本地文件路径（图片可以直接读），"
                       "语音的转写文字（设置了语音转文字时），视频的封面和截图，下载不了时说明原因。消息 id 来自 matewechat_messages。"
                       "合并转发的聊天记录在 matewechat_messages 里已经展开成文字。只读，不会发任何消息。",
        "input_schema": {"type": "object", "properties": {
            "message": {"type": "string", "description": "消息 id（matewechat_messages 每行的 id=…）"},
            "again": {"type": "boolean", "description": "重新下载（上次失败了，或本地副本被清理了），默认 false"}},
            "required": ["message"]},
    }

    def run(a: Dict) -> str:
        from openmate_core.wire import RPCError

        mid = str(a.get("message") or "").strip()
        if not mid:
            raise RPCError("缺少消息 id")
        backend = app.backend
        if backend is None or not app.signed_in or not hasattr(backend, "fetch_media"):
            raise RPCError("微信还没登录")
        try:
            ev, st, text = backend.fetch_media(mid, again=bool(a.get("again")))
        except BackendError as e:
            raise RPCError(e.message)
        m = ev.get("media")
        if not isinstance(m, dict):
            kind = str(ev.get("type") or "")
            if kind in ("IMAGE", "AUDIO", "VIDEO", "FILE"):
                return "%s\n这条消息是在能下载媒体之前收到的，没有留下下载信息，取不到内容。" % text
            return "%s\n这条消息没有图片、语音、视频或文件。" % text
        lines = [text]
        if st is not None and st.get("state") == "pending":
            lines.append("还在下载，稍后再查一次。")
        return "\n".join(lines)

    return {MEDIA_TOOL: (schema, run)}


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
        render_cards=True,
        ack_auto=False,  # someone's @ of the user is answered straight away; /agent keeps its 接单提示
        mark_view=mark_view,
        archive_chat=archive_chat,
        handle_kind="",  # no sender handles in archive batches (WeChat ids are not a kind the knowledge base knows)
        today_api_callers=("extension:openmate-feishu",),  # Feishu's 今天 shows these chats (its own HTTP)
        tools=media_tools,
    )


def main() -> int:
    return run(platform())
