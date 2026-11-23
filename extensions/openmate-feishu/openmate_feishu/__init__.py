"""OpenMate for Feishu / Lark: the platform half of the openmate-feishu extension.

The backend talks to Feishu through LarkAgentX, which the user installs themselves (larkx_python); the program runs
under that interpreter (launch.handoff re-executes it there once initialize says which one). OPENMATE_BACKEND=fixture
selects the core's deterministic fixture instead (tests, conformance runs); nothing else changes.
"""

from __future__ import annotations

import os
from typing import Dict

from openmate_core.backend import Backend, BackendError
from openmate_core.main import run
from openmate_core.platform import Platform

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTENSION_ID = "openmate-feishu"
CONNECTOR_ID = "matefeishu"
DOC_HOSTS = ("feishu.cn", "larksuite.com", "larkoffice.com", "feishu.net", "larksuite.cn")
#: a message Feishu's data-loss policy keeps from this client (dlpReviewState BLOCK) reads as this (Message.blocked)
DLP_BLOCKED_TEXT = "[飞书的安全策略拦下了这条消息，内容看不到]"


def make_backend(config: Dict, data_dir: str) -> Backend:
    which = (os.environ.get("OPENMATE_BACKEND") or "larkagentx").strip().lower()
    if which == "fixture":
        from openmate_core.fixture import FixtureBackend

        return FixtureBackend()
    if which != "larkagentx":
        raise BackendError("OPENMATE_BACKEND=%s 不认识：只有 larkagentx 和 fixture" % which, "config")
    from . import larkagentx

    return larkagentx.create(config, data_dir)


def platform(manifest_path: str = "") -> Platform:
    return Platform(
        extension_id=EXTENSION_ID,
        connector_id=CONNECTOR_ID,
        name="飞书",
        manifest_path=manifest_path or os.path.join(ROOT, "persona-extension.json"),
        make_backend=make_backend,
        handle_kind="lark",
        doc_hosts=DOC_HOSTS,
        agent_purpose="lark.agent",
        self_mention=True,  # 「@自己 …」 is a request like /agent (agent_self_mention)
        login_hint="用飞书手机 App 扫码，并在手机上确认登录",
        login_hint_scanned="已扫码，请在飞书手机 App 上点“确认登录”",
        login_hint_expired="二维码过期了，已换一张新的，请重新扫码",
        ui_pages=_agenda_pages,
        legacy_blocked_texts=(DLP_BLOCKED_TEXT,),  # what told a blocked message before 0.11.29 stored the mark
        tools=_media_tools,  # matefeishu_media: a message's pictures and files as local files (read-only)
        media_tool=CONNECTOR_ID + "_media",
    )


def _media_tools(app):
    from .media import media_tools

    return media_tools(app)


def _agenda_pages(app):
    # the calendar page and card (lark-cli, read-only), the 今天 page and card sharing one read of the calendar, the
    # StarkMesh page and its Overview card, and the Agents' progress (the hub's session list)
    from openmate_core import agents

    from . import agenda, starkmesh, today

    shared = agenda.Agenda(app)
    persona = today.PersonaToday(app)
    wechat = today.PeerToday(app)  # openmate-wechat's chats, when it is installed beside this one
    mesh = starkmesh.StarkMeshPage(app, starkmesh.StarkMesh(app))
    board = agents.AgentsRead(app)

    def drawn_again(*contributions):
        # a first read a page did not wait for ("正在读取") is in: those pages draw again now, not in 30 s
        def tell():
            peer = getattr(app, "peer", None)
            for c in contributions:
                try:
                    peer.notify("ui.invalidate", {"contribution": c})
                except Exception:
                    pass
        return tell

    shared._fresh.on_ready = drawn_again(agenda.PAGE, agenda.CARD, today.PAGE, today.CARD)
    persona._fresh.on_ready = drawn_again(today.PAGE, today.CARD)
    mesh.mesh._fresh.on_ready = drawn_again(starkmesh.PAGE, starkmesh.CARD, today.PAGE)
    board._fresh.on_ready = drawn_again(agents.PAGE, agents.CARD, today.PAGE)
    return {
        agenda.PAGE: agenda.CalendarPage(app, shared), agenda.CARD: agenda.CalendarPage(app, shared, card=True),
        today.PAGE: today.TodayPage(app, shared, persona, mesh=mesh, agents=board, wechat=wechat),
        today.CARD: today.TodayPage(app, shared, persona, card=True, wechat=wechat),
        starkmesh.PAGE: mesh, starkmesh.CARD: starkmesh.StarkMeshCard(mesh),
        agents.PAGE: agents.AgentsPage(app, board), agents.CARD: agents.AgentsPage(app, board, card=True),
    }


def main() -> int:
    from openmate_core.app import ConnectorApp

    from . import launch

    early = ConnectorApp(platform(), env={})  # answers what comes before initialize, as the real one will
    first = launch.handoff(os.path.join(ROOT, "bin", "openmate-feishu"), early=early.handle)  # may become larkx_python
    return run(platform(), first=first)
