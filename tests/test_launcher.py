"""The real launcher as the hub starts it: a subprocess speaking the protocol on stdin / stdout."""

import _runtmp  # noqa: F401  (first: every temporary directory of this run in one, removed at its end)
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from harness import CID, EXT, ROOT, StdioHub, wait_until

LAUNCHER = os.path.join(ROOT, "extensions", EXT, "bin", "openmate-feishu")


def spawn(test, launcher=LAUNCHER, backend="fixture"):
    data = tempfile.mkdtemp(prefix="openmate-launch-")
    test.addCleanup(shutil.rmtree, data, True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENMATE_", "PERSONA_"))}
    env["PERSONA_EXTENSION_DATA"] = data
    if backend:
        env["OPENMATE_BACKEND"] = backend
    p = subprocess.Popen([sys.executable, launcher], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=env)
    hub = StdioHub(p.stdout, p.stdin)

    def stop():
        if p.poll() is None:
            p.kill()
            p.wait(5)
        for f in (p.stdin, p.stdout, p.stderr):
            try:
                f.close()
            except OSError:
                pass

    test.addCleanup(stop)
    return p, hub, data


def init(hub):
    return hub.request("initialize", {"protocol": 1, "hub": {"build": 202611130000, "extension_points": {"connectors": 2}},
                                      "extension": {"id": EXT, "version": "0.1.0"}, "config": {}, "granted": {}})


def c(hub, method, **params):
    params.setdefault("contribution", CID)
    return hub.request("connector." + method, params)


class LauncherTest(unittest.TestCase):
    def test_a_whole_run(self):
        p, hub, data = spawn(self)
        res = init(hub)
        self.assertEqual(res["id"], EXT)
        st = wait_until(lambda: (lambda s: s if s["state"] != "error" else None)(c(hub, "status")), what="startup")
        self.assertEqual(st["state"], "not_signed_in")
        prompt = c(hub, "login.step", step="")
        while not prompt.get("done"):
            prompt = c(hub, "login.step", step=prompt["step"])
        wait_until(lambda: c(hub, "status")["state"] == "ok", what="ok")
        self.assertTrue(c(hub, "inbox.collect")["things"])
        self.assertTrue(c(hub, "archive.changes", cursor="", limit=50)["batches"])
        self.assertTrue(os.path.exists(os.path.join(data, "openmate.db")))
        c(hub, "logout")
        self.assertNotIn(CID, hub.secrets)
        p.stdin.close()  # the hub closes stdin: the program exits
        self.assertEqual(p.wait(10), 0)
        err = p.stderr.read().decode("utf-8", "replace")
        for body in ("周五评审", "接口文档", "fixture_token"):
            self.assertNotIn(body, err, "a message body or secret in the log")
        for line in hub.raw_lines:
            msg = json.loads(line)
            self.assertTrue({"id", "event", "method"} & set(msg))

    def test_the_default_backend_says_what_is_missing(self):
        p, hub, _ = spawn(self, backend=None)
        init(hub)
        st = wait_until(lambda: (lambda s: s if s["state"] != "error" else None)(c(hub, "status")), what="startup")
        self.assertEqual(st["state"], "not_configured")
        self.assertIn("larkx_python", st["error"])


if __name__ == "__main__":
    unittest.main()
