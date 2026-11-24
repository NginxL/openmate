"""The process protocol's handshake and errors, over the real JSON-line pipes."""

import _runtmp  # noqa: F401  (first: every temporary directory of this run in one, removed at its end)
import json
import os
import unittest

from harness import CID, EXT, ROOT, Harness, HubSideError, wait_until


class HandshakeTest(unittest.TestCase):
    def test_before_initialize(self):
        h = Harness(self, initialize=False)
        with self.assertRaises(HubSideError) as e:
            h.hub.request("ping", {})
        self.assertEqual(e.exception.code, "not_initialized")
        with self.assertRaises(HubSideError) as e:
            h.c("status")
        self.assertEqual(e.exception.code, "not_initialized")
        # manifest is answered before initialize, without the source-only fields
        m = h.hub.request("manifest", {})
        self.assertEqual(m["id"], EXT)
        self.assertNotIn("tags", m)
        self.assertNotIn("icon", m)

    def test_wrong_protocol_then_initialize(self):
        h = Harness(self, initialize=False)
        with self.assertRaises(HubSideError) as e:
            h.hub.request("initialize", {"protocol": 101, "hub": {"build": 0, "extension_points": {}}, "granted": {}})
        self.assertEqual(e.exception.code, "wrong_protocol")
        res = h.initialize()
        with open(os.path.join(ROOT, "extensions", EXT, "persona-extension.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(res["protocol"], 1)
        self.assertEqual(res["id"], EXT)
        self.assertEqual(res["version"], manifest["version"])
        # the connector contribution is the manifest's, field for field
        self.assertEqual(res["contributes"]["connectors"], manifest["contributes"]["connectors"])
        self.assertEqual(h.hub.request("ping", {}), {})

    def test_calls_right_after_initialize_wait_for_startup(self):
        h = Harness(self, initialize=False)
        h.hub.secrets_delay = 0.5  # the saved sign-in takes a while to read
        h.hub.request("initialize", {"protocol": 1, "hub": {"build": 0, "extension_points": {}}, "granted": {},
                                     "extension": {"id": EXT, "version": "0.1.0", "data_dir": h.data_dir}})
        self.assertEqual(h.c("status")["state"], "error")  # memory only: answers at once, says it is starting
        p = h.c("login.step", step="")  # waits for the program to be ready instead of refusing
        self.assertEqual(p["kind"], "qr")

    def test_unknown_and_undeclared(self):
        h = Harness(self)
        for method, code in (("nonexistent.method", "unknown_method"), ("connector.nonexistent_method", "unknown_method"),
                             ("connector.history.anchors", "no_capability"), ("connector.items.list", "no_capability"),
                             ("connector.recall.do", "no_capability")):
            with self.assertRaises(HubSideError) as e:
                h.hub.request(method, {"contribution": CID})
            self.assertEqual(e.exception.code, code, method)
        with self.assertRaises(HubSideError) as e:
            h.hub.request("connector.status", {"contribution": "no-such-contribution"})
        self.assertEqual(e.exception.code, "no_contribution")
        # the contribution may be left out when there is only one
        self.assertIn("state", h.hub.request("connector.status", {}))

    def test_error_answers_have_a_message(self):
        h = Harness(self)
        with self.assertRaises(HubSideError) as e:
            h.c("tools.call", name=CID + "_messages", args={})
        self.assertTrue(e.exception.message)
        with self.assertRaises(HubSideError) as e:
            h.c("tools.call", name="other_tool", args={})
        self.assertIn("other_tool", e.exception.message)

    def test_every_line_is_protocol_json(self):
        h = Harness(self)
        h.sign_in()
        h.c("inbox.collect")
        h.close()
        for line in h.hub.raw_lines:
            msg = json.loads(line)
            self.assertTrue({"id", "event", "method"} & set(msg), line)

    def test_changed_events_are_well_formed(self):
        h = Harness(self)
        h.sign_in()
        wait_until(lambda: h.hub.changed(), what="a changed event")
        for _, name, params in h.hub.changed():
            self.assertEqual(name, "connector.changed")
            self.assertEqual(params["contribution"], CID)
            self.assertIn(params["scope"], ("inbox", "chats"))


class PrefixedInputTest(unittest.TestCase):
    """A launcher that read the first line itself (to pick the interpreter) hands it to the core with the rest."""

    def test_a_whole_line_then_the_stream(self):
        import io

        from openmate_core.main import PrefixedInput

        inp = PrefixedInput(b'{"id":"h1"}\n', io.BytesIO(b'{"id":"h2"}\n'))
        self.assertEqual([inp.readline(), inp.readline(), inp.readline()], [b'{"id":"h1"}\n', b'{"id":"h2"}\n', b""])

    def test_a_line_cut_short_is_finished_from_the_stream(self):
        import io

        from openmate_core.main import PrefixedInput

        inp = PrefixedInput(b'{"id":', io.BytesIO(b'"h1"}\n{"id":"h2"}\n'))
        self.assertEqual(inp.readline(), b'{"id":"h1"}\n')
        self.assertEqual(inp.readline(), b'{"id":"h2"}\n')
        self.assertEqual(PrefixedInput(b"", io.BytesIO(b"x\n")).readline(), b"x\n")


if __name__ == "__main__":
    unittest.main()
