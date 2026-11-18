"""scripts/package.py: the archive's contents, reproducibility, the index, and the package running as installed."""

import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

from harness import CID, EXT, ROOT, StdioHub, wait_until

sys.path.insert(0, os.path.join(ROOT, "scripts"))
import package  # noqa: E402

REL = "packages/%s/%%s/%s-%%s.tar.gz" % (EXT, EXT)


def build(test):
    out = tempfile.mkdtemp(prefix="openmate-pkg-")
    test.addCleanup(shutil.rmtree, out, True)
    problems = package.run(out, log=lambda *_: None)
    test.assertEqual(problems, [])
    with open(os.path.join(out, "persona-extensions.json"), encoding="utf-8") as f:
        index = json.load(f)
    return out, index


class PackageTest(unittest.TestCase):
    def test_archive_and_index(self):
        out, index = build(self)
        self.assertEqual(index["market_version"], 1)
        self.assertEqual(index["name"], "OpenMate")
        self.assertTrue(index["homepage"].startswith("https://"))
        entry = index["extensions"][0]
        version = entry["manifest"]["version"]
        rel = REL % (version, version)
        pkg = entry["packages"]
        self.assertEqual(len(pkg), 1)
        self.assertEqual(pkg[0]["url"], rel)
        with open(os.path.join(out, rel), "rb") as f:
            data = f.read()
        self.assertEqual(pkg[0]["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(pkg[0]["size"], len(data))
        self.assertNotIn("platforms", pkg[0])
        self.assertNotIn("tags", entry["manifest"])
        self.assertNotIn("icon", entry["manifest"])
        self.assertIn("飞书", entry["tags"])
        self.assertEqual(entry["icon"], "assets/icon.png")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            listed = [m.name for m in tar.getmembers()]
            self.assertEqual(len(listed), len(set(listed)), "every name once")  # read before a dict folds them
            members = {m.name: m for m in tar.getmembers()}
            for m in members.values():
                self.assertTrue(m.isreg(), m.name)
                self.assertEqual((m.uid, m.gid, m.mtime), (0, 0, package.MTIME))
                self.assertEqual(m.mode, 0o755 if m.name == "bin/openmate-feishu" else 0o644, m.name)
            names = sorted(members)
            for want in ("persona-extension.json", "persona-plugin.json", "LICENSE", "bin/openmate-feishu",
                         "openmate_core/app.py", "openmate_core/agent.py", "openmate_feishu/__init__.py",
                         "assistant/instructions.md", "assets/icon.png"):
                self.assertIn(want, members)
            for n in names:
                self.assertFalse("/tests/" in "/" + n or n.startswith("tests/") or "__pycache__" in n or n.endswith(".pyc"), n)
            a = tar.extractfile("persona-extension.json").read()
            b = tar.extractfile("persona-plugin.json").read()
            self.assertEqual(a, b)
            self.assertEqual(json.loads(a), entry["manifest"])
            png = tar.extractfile("assets/icon.png").read()
            self.assertTrue(png.startswith(b"\x89PNG"))
            self.assertEqual(png[16:24], b"\x00\x00\x02\x00\x00\x00\x02\x00")  # 512 × 512

    def test_reproducible(self):
        a, ia = build(self)
        b, ib = build(self)
        self.assertEqual(ia, ib)
        v = ia["extensions"][0]["manifest"]["version"]
        with open(os.path.join(a, REL % (v, v)), "rb") as fa, open(os.path.join(b, REL % (v, v)), "rb") as fb:
            self.assertEqual(fa.read(), fb.read())

    def test_check_finds_drift_and_published_versions_never_change(self):
        out, index = build(self)
        self.assertEqual(package.run(out, check=True), [])
        v = index["extensions"][0]["manifest"]["version"]
        path = os.path.join(out, REL % (v, v))
        with open(path, "rb") as f:
            raw = bytearray(gzip.decompress(f.read()))
        raw[600] ^= 0x20  # a byte of the first file
        with open(path, "wb") as f:
            f.write(gzip.compress(bytes(raw)))
        problems = package.run(out, check=True)
        self.assertTrue(any("bump the version" in p for p in problems), problems)
        with self.assertRaises(package.PackageError):
            package.run(out, log=lambda *_: None)
        os.remove(os.path.join(out, "persona-extensions.json"))
        self.assertTrue(any("not up to date" in p for p in package.run(out, check=True)))

    def test_the_package_runs_as_installed(self):
        out, index = build(self)
        for entry in index["extensions"]:  # each extension's own package, not only the first
            with self.subTest(entry["manifest"]["id"]):
                self.runs_as_installed(out, entry)

    def runs_as_installed(self, out, entry):
        ext = entry["manifest"]["id"]
        v = entry["manifest"]["version"]
        cid = entry["manifest"]["contributes"]["connectors"][0]["id"]
        rel = entry["packages"][0]["url"]
        inst = tempfile.mkdtemp(prefix="openmate-inst-")
        self.addCleanup(shutil.rmtree, inst, True)
        with tarfile.open(os.path.join(out, rel), "r:gz") as tar:
            for m in tar.getmembers():
                target = os.path.join(inst, m.name)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as f:
                    f.write(tar.extractfile(m).read())
                os.chmod(target, 0o755 if m.mode & 0o111 else 0o644)
        data = os.path.join(inst, "data")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENMATE_", "PERSONA_"))}
        env.update(OPENMATE_BACKEND="fixture", PERSONA_EXTENSION_DATA=data)
        p = subprocess.Popen([sys.executable, os.path.join(inst, "bin", ext)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=inst)
        hub = StdioHub(p.stdout, p.stdin)
        try:
            m = hub.request("manifest", {})
            self.assertEqual(m, entry["manifest"])
            hub.request("initialize", {"protocol": 1, "hub": {"build": 1, "extension_points": {}},
                                       "extension": {"id": ext, "version": v}, "granted": {}})
            st = wait_until(lambda: (lambda s: s if s["state"] != "error" else None)(
                hub.request("connector.status", {"contribution": cid})), what="startup")
            self.assertEqual(st["state"], "not_signed_in")
            p.stdin.close()
            self.assertEqual(p.wait(10), 0)
            self.assertFalse(os.path.exists(os.path.join(inst, "openmate_core", "__pycache__")), "wrote into the package")
        finally:
            if p.poll() is None:
                p.kill()
                p.wait(5)
            for f in (p.stdout, p.stderr):
                f.close()


if __name__ == "__main__":
    unittest.main()
