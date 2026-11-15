"""The Feishu backend through LarkAgentX: phase 1b (written separately).

LarkAgentX is installed by the user (configuration larkx_python, pinned at larkx_commit); it is never shipped with
this extension. Phase 1b replaces create() with one that returns an openmate_core.backend.Backend talking to Feishu
through that Python. Until then, selecting it says so clearly: the connector reports not_configured with the sentence
below instead of failing in an obscure way.
"""

from __future__ import annotations

from typing import Dict

from openmate_core.backend import Backend, BackendError


def create(config: Dict, data_dir: str) -> Backend:
    if not str(config.get("larkx_python") or "").strip():
        raise BackendError("还没有设置 LarkAgentX 的 Python（larkx_python）：请先自己安装 LarkAgentX，再在扩展设置里填它的 Python 路径", "config")
    raise BackendError("LarkAgentX 后端还没有实现（OpenMate 一期 1b），暂时不能连接飞书", "config")
