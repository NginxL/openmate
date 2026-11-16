"""OpenMate core: the platform-agnostic half of OpenMate's Persona extensions.

A platform package (openmate_feishu, later WeChat or DingTalk) supplies a Platform (its connector id, names and a
Backend factory) and calls main.run(platform). Everything else lives here: the JSON-line protocol, the local store, the
connector capabilities (status, tools, actions, events, inbox, archive, login, threads) and the /agent runner.

Standard library only, Python 3.9 or newer.
"""

__version__ = "0.2.1"
