"""The Feishu package: its manifest against what the program does, and backend selection."""

try:  # first: every temporary directory of this run in one, removed at its end
    import _runtmp  # noqa: F401
except ImportError:  # (started by its path from elsewhere: its directory is not on the path)
    pass
import json
import os
import re
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
EXT_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(EXT_DIR))
for p in (os.path.join(ROOT, "core"), EXT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import openmate_feishu  # noqa: E402
from openmate_core.app import ConnectorApp, config_defaults, packaged_manifest  # noqa: E402
from openmate_core.backend import BackendError  # noqa: E402
from openmate_core.fixture import FixtureBackend  # noqa: E402

with open(os.path.join(EXT_DIR, "persona-extension.json"), encoding="utf-8") as f:
    MANIFEST = json.load(f)
CONN = MANIFEST["contributes"]["connectors"][0]


# The icons the Persona web draws for a nav_page (web/src/lib/extension-ui.ts: UI_ICONS, with UI version 6's NEW_ICONS_6).
NAV_ICONS = set("""extension plugin chart gauge list bell doc calendar money branch code pull_request merge map
home sofa light power fan thermometer droplet lock unlock door window camera video play pause stop speaker music tv plug
battery bolt sun moon cloud rain snow wind leaf car location robot wifi shield trend_up trend_down wallet bank heart plus
minus trash edit refresh share info""".split())


class ManifestTest(unittest.TestCase):
    def test_identity(self):
        self.assertEqual(MANIFEST["id"], "openmate-feishu")
        self.assertEqual(MANIFEST["name"], "飞书（OpenMate）")
        self.assertEqual(MANIFEST["publisher"], "NginxL")
        self.assertEqual(MANIFEST["homepage"], "https://github.com/NginxL/openmate")
        self.assertEqual(MANIFEST["engines"], {"persona": ">=202611220000", "extension_protocol": 1})  # usage (#12434)
        self.assertEqual(MANIFEST["activation_events"], ["on_startup"])
        self.assertTrue(re.match(r"^\d+\.\d+\.\d+$", MANIFEST["version"]))
        self.assertTrue(os.access(os.path.join(EXT_DIR, MANIFEST["runtime"]["command"]), os.X_OK))
        self.assertTrue(os.path.isfile(os.path.join(EXT_DIR, MANIFEST["icon"])))
        self.assertTrue(os.path.isfile(os.path.join(EXT_DIR, MANIFEST["contributes"]["assistant"][0]["instructions"])))

    def test_connector(self):
        self.assertEqual(CONN["id"], openmate_feishu.CONNECTOR_ID)
        self.assertEqual(CONN["capabilities"], ["status", "tools", "actions", "events", "inbox", "archive", "history", "login", "threads"])
        self.assertEqual(CONN["kinds"], ["dm", "mention", "group"])
        self.assertEqual(sorted(CONN["kind_descriptions"]), sorted(CONN["kinds"]))
        descs = list(CONN["kind_descriptions"].values())
        self.assertEqual(len(set(descs)), len(descs))
        for d in descs:
            self.assertTrue(d.startswith("类型："))
        self.assertEqual(CONN["poll_seconds"], 60)
        self.assertEqual(CONN["confirm_always"], ["matefeishu_*"])
        self.assertIs(CONN["sends_as_user"], True)  # /agent replies go out unconfirmed and are put in the ledger
        self.assertNotIn("matefeishu_send", CONN["agent_tools"])
        self.assertNotIn("matefeishu_send_dm", CONN["agent_tools"])

    def test_permissions(self):
        perms = MANIFEST["permissions"]
        self.assertEqual(sorted(perms["hub"]), ["agents.control", "agents.read", "assistant.ask",
                                                "extensions.call:openmate-wechat", "inbox.read", "kb.read", "machines.read",
                                                "todos.read"])  # machines.read: /agent's placement by load
        self.assertEqual(perms["network"], ["feishu.cn", "*.feishu.cn", "larksuite.com", "*.larksuite.com",
                                            "larkoffice.com", "*.larkoffice.com", "config:starkmesh_url:http"])
        props = MANIFEST["configuration"]["properties"]
        for entry in perms["network"]:
            if entry.startswith("config:"):
                field = entry.split(":")[1]
                self.assertEqual(props[field].get("format"), "uri", entry)
                self.assertNotIn("default", props[field], "the hub drops an extension whose network address has a default")
        self.assertEqual(MANIFEST["host_integrations"], [{"id": "lark-cli", "version": ">=1.0.72 <2.0.0"}])
        # the sidebar's usage bars (StarkMesh's pool): the hub asks a usage contribution only with launcher_keys; the
        # answer never uses a launcher's key (StarkMeshPage.usage), and the hub has the usage point from 2026-11-21
        self.assertEqual(MANIFEST["contributes"]["usage"], [{"id": "starkmesh", "name": "StarkMesh"}])
        self.assertIs(perms["launcher_keys"], True)
        self.assertEqual(set(perms), {"hub", "network", "launcher_keys"}, "no other permission")

    def test_configuration(self):
        props = MANIFEST["configuration"]["properties"]
        self.assertEqual(list(props), ["larkx_python", "larkx_commit", "region", "http_proxy", "agent", "agent_prefix",
                                       "agent_self_mention",
                                       "agent_machine", "agent_balance", "agent_launcher", "agent_forward", "channel_chat", "agent_reply_header", "agent_mark_position",
                                       "agent_mark_effort",
                                       "agent_ack_reaction",
                                       "agent_done_reaction", "agent_fail_reaction",
                                       "auto_reply", "auto_reply_docs", "auto_reply_daily_most", "auto_reply_hourly_most",
                                       "auto_reply_header", "auto_reply_at_asker",
                                       "agent_reply_suffix",
                                       "history_chats", "triage_rules", "lark_cli", "lark_cli_profile", "starkmesh_url", "needs_hours", "bookmarks"])
        self.assertEqual([(s["name"], s.get("hint")) for s in MANIFEST["secrets"]], [("starkmesh_key", "last4")])
        self.assertIn("config:starkmesh_url:http", MANIFEST["permissions"]["network"])
        self.assertNotIn("default", props["larkx_python"])
        self.assertEqual(config_defaults(MANIFEST).get("lark_cli_profile", ""), "", "lark-cli's default unless named")

        d = config_defaults(MANIFEST)
        self.assertEqual(props["starkmesh_url"]["format"], "uri")
        self.assertNotIn("starkmesh_url", d, "an address a network permission names is the user's to write: no default")
        self.assertEqual((d["auto_reply"], d["auto_reply_daily_most"], d["auto_reply_header"], d["auto_reply_at_asker"]),
                         (True, 20, "> 🤖 AI 代答 · {model} · {effort}", True))
        self.assertEqual(d["larkx_commit"], "97ad48e69e34bd8c78ee30f795b7c3759dd81659")
        self.assertTrue(re.match(props["larkx_commit"]["pattern"], d["larkx_commit"]))
        self.assertEqual([c["const"] for c in props["region"]["oneOf"]], ["feishu", "lark"])
        self.assertIs(d["agent"], False)
        self.assertIs(d["agent_forward"], True, "a follow-up goes to the task's Agent unless switched off")
        self.assertEqual(props["agent_machine"]["format"], "machine")
        self.assertEqual(props["agent_launcher"]["format"], "launcher")
        self.assertEqual((d["agent_reply_header"], d["agent_ack_reaction"], d["agent_reply_suffix"]),
                         ("> 🤖 AI 回复 · {model} · {effort}", "THINKING", ""))
        self.assertIs(d["agent_mark_effort"], False, "the mark names the model alone")
        self.assertEqual((props["history_chats"]["minimum"], props["history_chats"]["maximum"], d["history_chats"]), (0, 50, 20))
        self.assertLessEqual(props["triage_rules"]["maxLength"], 4000)
        self.assertIn("http://", props["http_proxy"]["description"])
        self.assertNotIn("socks5://", props["http_proxy"]["description"])
        self.assertEqual(MANIFEST["configuration"]["type"], "object")


    def test_pages_and_cards(self):
        ui = {u["id"]: u for u in MANIFEST["contributes"]["ui"]}
        self.assertEqual({k: (u["placement"], u.get("icon"), u.get("opens")) for k, u in ui.items()}, {
            "openmate-today": ("nav_page", "sun", None), "today-card": ("overview_card", None, "openmate-today"),
            "openmate-agents": ("nav_page", "robot", None), "agents-card": ("overview_card", None, "openmate-agents"),
            "chats": ("nav_page", "bell", None), "calendar": ("nav_page", "calendar", None),
            "starkmesh": ("nav_page", "wallet", None), "starkmesh-card": ("overview_card", None, "starkmesh")})
        # Each page keeps its own shape in the sidebar's monochrome icons: no two of our pages share an icon, and each is
        # one the Persona web draws for a nav_page (web/src/lib/extension-ui.ts UI_ICONS; any other becomes the plain
        # extension icon there, so "chat" or "person" would not help yet).
        nav = [u["icon"] for u in ui.values() if u["placement"] == "nav_page"]
        self.assertTrue(set(nav) <= NAV_ICONS, set(nav) - NAV_ICONS)
        drawn = [{"plugin": "extension"}.get(i, i) for i in nav]  # plugin is extension's old name: the same picture
        self.assertEqual(len(drawn), len(set(drawn)), nav)
        self.assertFalse(set(drawn) & {"list", "extension"}, "the plain list and extension icons look like every other extension's page")
        self.assertEqual(ui["starkmesh"]["title"], "中转站")
        self.assertTrue(all(1 <= len(u["title"]) <= 40 for u in ui.values()))
        # every contribution is drawn by the program (a missing one is an error on the Overview)
        from openmate_core.ui import UI

        app = types.SimpleNamespace(config={}, tz=None, hub=None, secrets={}, store=None, closing=None)
        drawn = UI(app, openmate_feishu.platform().ui_pages).pages
        self.assertEqual(sorted(k for k in ui if k not in drawn), [])
    def test_packaged_manifest_drops_source_only_fields(self):
        m = packaged_manifest(MANIFEST)
        self.assertNotIn("tags", m)
        self.assertNotIn("icon", m)
        self.assertEqual(m["contributes"], MANIFEST["contributes"])


class BackendSelectionTest(unittest.TestCase):
    def setUp(self):
        self.old = os.environ.get("OPENMATE_BACKEND")
        self.addCleanup(self._restore)

    def _restore(self):
        if self.old is None:
            os.environ.pop("OPENMATE_BACKEND", None)
        else:
            os.environ["OPENMATE_BACKEND"] = self.old

    def test_fixture(self):
        os.environ["OPENMATE_BACKEND"] = "fixture"
        self.assertIsInstance(openmate_feishu.make_backend({}, "/tmp"), FixtureBackend)

    def test_larkagentx_is_the_default(self):
        os.environ.pop("OPENMATE_BACKEND", None)
        with self.assertRaises(BackendError) as e:
            openmate_feishu.make_backend({}, "/tmp")
        self.assertEqual(e.exception.kind, "config")
        self.assertIn("larkx_python", e.exception.message)
        if "larkx" in sys.modules:
            return  # another test module put a stand-in in place
        with self.assertRaises(BackendError) as e:
            openmate_feishu.make_backend({"larkx_python": sys.executable}, "/tmp")
        self.assertEqual(e.exception.kind, "config")
        self.assertIn("没有装 LarkAgentX", e.exception.message)  # this interpreter has no LarkAgentX

    def test_unknown(self):
        os.environ["OPENMATE_BACKEND"] = "nope"
        with self.assertRaises(BackendError):
            openmate_feishu.make_backend({}, "/tmp")

    def test_platform(self):
        p = openmate_feishu.platform()
        self.assertEqual((p.extension_id, p.connector_id, p.name, p.handle_kind, p.agent_purpose),
                         ("openmate-feishu", "matefeishu", "飞书", "lark", "lark.agent"))
        app = ConnectorApp(p, env={})
        self.assertEqual(app.manifest["id"], "openmate-feishu")


if __name__ == "__main__":
    unittest.main()
