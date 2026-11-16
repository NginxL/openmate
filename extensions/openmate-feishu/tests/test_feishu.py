"""The Feishu package: its manifest against what the program does, and backend selection."""

import json
import os
import re
import sys
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


class ManifestTest(unittest.TestCase):
    def test_identity(self):
        self.assertEqual(MANIFEST["id"], "openmate-feishu")
        self.assertEqual(MANIFEST["name"], "飞书（OpenMate）")
        self.assertEqual(MANIFEST["publisher"], "NginxL")
        self.assertEqual(MANIFEST["homepage"], "https://github.com/NginxL/openmate")
        self.assertEqual(MANIFEST["engines"], {"persona": ">=202611110000", "extension_protocol": 1})
        self.assertEqual(MANIFEST["activation_events"], ["on_startup"])
        self.assertTrue(re.match(r"^\d+\.\d+\.\d+$", MANIFEST["version"]))
        self.assertTrue(os.access(os.path.join(EXT_DIR, MANIFEST["runtime"]["command"]), os.X_OK))
        self.assertTrue(os.path.isfile(os.path.join(EXT_DIR, MANIFEST["icon"])))
        self.assertTrue(os.path.isfile(os.path.join(EXT_DIR, MANIFEST["contributes"]["assistant"][0]["instructions"])))

    def test_connector(self):
        self.assertEqual(CONN["id"], openmate_feishu.CONNECTOR_ID)
        self.assertEqual(CONN["capabilities"], ["status", "tools", "actions", "events", "inbox", "archive", "login", "threads"])
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
        self.assertEqual(sorted(perms["hub"]), ["agents.control", "agents.read", "assistant.ask", "kb.read"])
        self.assertEqual(perms["network"], ["feishu.cn", "*.feishu.cn", "larksuite.com", "*.larksuite.com",
                                            "larkoffice.com", "*.larkoffice.com"])
        self.assertEqual(MANIFEST["host_integrations"], [{"id": "lark-cli", "version": ">=1.0.72 <2.0.0"}])

    def test_configuration(self):
        props = MANIFEST["configuration"]["properties"]
        self.assertEqual(list(props), ["larkx_python", "larkx_commit", "region", "http_proxy", "agent", "agent_prefix",
                                       "agent_machine", "agent_launcher", "agent_reply_suffix", "history_chats",
                                       "triage_rules", "lark_cli", "lark_cli_profile"])
        self.assertNotIn("default", props["larkx_python"])
        self.assertEqual(config_defaults(MANIFEST).get("lark_cli_profile", ""), "", "lark-cli's default unless named")

        d = config_defaults(MANIFEST)
        self.assertEqual(d["larkx_commit"], "97ad48e69e34bd8c78ee30f795b7c3759dd81659")
        self.assertTrue(re.match(props["larkx_commit"]["pattern"], d["larkx_commit"]))
        self.assertEqual([c["const"] for c in props["region"]["oneOf"]], ["feishu", "lark"])
        self.assertIs(d["agent"], False)
        self.assertEqual(props["agent_machine"]["format"], "machine")
        self.assertEqual(props["agent_launcher"]["format"], "launcher")
        self.assertEqual(d["agent_reply_suffix"], "> 🤖 AI 代答，仅供参考")
        self.assertEqual((props["history_chats"]["minimum"], props["history_chats"]["maximum"], d["history_chats"]), (0, 50, 20))
        self.assertLessEqual(props["triage_rules"]["maxLength"], 4000)
        self.assertIn("http://", props["http_proxy"]["description"])
        self.assertNotIn("socks5://", props["http_proxy"]["description"])
        self.assertEqual(MANIFEST["configuration"]["type"], "object")


    def test_pages_and_cards(self):
        ui = {u["id"]: u for u in MANIFEST["contributes"]["ui"]}
        self.assertEqual({k: (u["placement"], u.get("icon"), u.get("opens")) for k, u in ui.items()}, {
            "chats": ("nav_page", "list", None), "chats-today": ("overview_card", None, "chats"),
            "calendar": ("nav_page", "calendar", None), "calendar-today": ("overview_card", None, "calendar")})
        self.assertTrue(all(1 <= len(u["title"]) <= 40 for u in ui.values()))
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
