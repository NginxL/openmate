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
        login_hint="用飞书手机 App 扫码，并在手机上确认登录",
        login_hint_scanned="已扫码，请在飞书手机 App 上点“确认登录”",
        login_hint_expired="二维码过期了，已换一张新的，请重新扫码",
        ui_pages=_agenda_pages,
    )


def _agenda_pages(app):
    # the calendar page and card (lark-cli, read-only), the 今天 page and card sharing one read of the calendar, the
    # StarkMesh page and its Overview card, and the Agents' progress (the hub's session list)
    from openmate_core import agents

    from . import agenda, starkmesh, today

    shared = agenda.Agenda(app)
    persona = today.PersonaToday(app)
    mesh = starkmesh.StarkMeshPage(app, starkmesh.StarkMesh(app))
    board = agents.AgentsRead(app)
    return {
        agenda.PAGE: agenda.CalendarPage(app, shared), agenda.CARD: agenda.CalendarPage(app, shared, card=True),
        today.PAGE: today.TodayPage(app, shared, persona, mesh=mesh, agents=board),
        today.CARD: today.TodayPage(app, shared, persona, card=True),
        starkmesh.PAGE: mesh, starkmesh.CARD: starkmesh.StarkMeshCard(mesh),
        agents.PAGE: agents.AgentsPage(app, board), agents.CARD: agents.AgentsPage(app, board, card=True),
    }


def main() -> int:
    from openmate_core.app import ConnectorApp

    from . import launch

    early = ConnectorApp(platform(), env={})  # answers what comes before initialize, as the real one will
    first = launch.handoff(os.path.join(ROOT, "bin", "openmate-feishu"), early=early.handle)  # may become larkx_python
    return run(platform(), first=first)
