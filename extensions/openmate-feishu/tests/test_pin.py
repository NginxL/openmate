"""Which LarkAgentX commit is installed: a checkout's HEAD (detached, a branch, packed, a worktree), pip's record of a
git install or of a copy from a checkout, and the refusals."""

import os
import shutil
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
EXT_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(EXT_DIR))
for p in (os.path.join(ROOT, "core"), EXT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from openmate_core.backend import BackendError  # noqa: E402
from openmate_feishu import pin  # noqa: E402

A = "97ad48e69e34bd8c78ee30f795b7c3759dd81659"
B = "1111111111111111111111111111111111111111"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class PinTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="openmate-pin-")
        self.addCleanup(shutil.rmtree, self.d, True)

    def checkout(self, name="checkout", head=A + "\n"):
        root = os.path.join(self.d, name)
        write(os.path.join(root, "larkx", "__init__.py"), "x = 1\n")
        write(os.path.join(root, "larkx", "proto", "cmd_map.json"), "{}\n")
        write(os.path.join(root, ".git", "HEAD"), head)
        return root

    def test_detached_branch_and_packed(self):
        root = self.checkout()
        pkg = os.path.join(root, "larkx")
        self.assertEqual(pin.installed_commit(pkg, None), (A, "git"))
        write(os.path.join(root, ".git", "HEAD"), "ref: refs/heads/main\n")
        write(os.path.join(root, ".git", "refs", "heads", "main"), B + "\n")
        self.assertEqual(pin.installed_commit(pkg, None), (B, "git"))
        os.remove(os.path.join(root, ".git", "refs", "heads", "main"))
        write(os.path.join(root, ".git", "packed-refs"), "# pack-refs with: peeled\n%s refs/heads/other\n%s refs/heads/main\n^%s\n" % (B, A, B))
        self.assertEqual(pin.installed_commit(pkg, None), (A, "git"))
        os.remove(os.path.join(root, ".git", "packed-refs"))
        self.assertEqual(pin.installed_commit(pkg, None)[0], "")

    def test_a_worktree(self):
        main = os.path.join(self.d, "main.git")
        write(os.path.join(main, "packed-refs"), "%s refs/heads/feature\n" % A)
        wt_git = os.path.join(main, "worktrees", "wt")
        write(os.path.join(wt_git, "HEAD"), "ref: refs/heads/feature\n")
        write(os.path.join(wt_git, "commondir"), "../..\n")
        root = os.path.join(self.d, "wt")
        write(os.path.join(root, "larkx", "__init__.py"), "")
        write(os.path.join(root, ".git"), "gitdir: %s\n" % wt_git)
        self.assertEqual(pin.installed_commit(os.path.join(root, "larkx"), None), (A, "git"))

    def test_only_the_packages_own_checkout_counts(self):
        # a site-packages copy inside some other repository (a venv in a project) is not "a checkout of LarkAgentX"
        outer = self.checkout("project", B + "\n")
        pkg = os.path.join(outer, ".venv", "lib", "site-packages", "larkx")
        write(os.path.join(pkg, "__init__.py"), "")
        self.assertEqual(pin.installed_commit(pkg, None)[0], "")

    def test_pip_records(self):
        site = os.path.join(self.d, "site")
        pkg = os.path.join(site, "larkx")
        write(os.path.join(pkg, "__init__.py"), "x = 1\n")
        write(os.path.join(pkg, "proto", "cmd_map.json"), "{}\n")
        self.assertEqual(pin.installed_commit(pkg, {"url": "https://github.com/cv-cat/LarkAgentX",
                                                    "vcs_info": {"vcs": "git", "commit_id": A}}), (A, "pip"))
        src = self.checkout("src")
        rec = {"url": "file://" + src.replace(" ", "%20"), "dir_info": {}}
        self.assertEqual(pin.installed_commit(pkg, rec), (A, "copy"))
        write(os.path.join(src, "larkx", "__init__.py"), "x = 2\n")
        commit, why = pin.installed_commit(pkg, rec)
        self.assertEqual(commit, "")
        self.assertIn("不一样", why)
        write(os.path.join(src, "larkx", "__init__.py"), "x = 1\n")
        write(os.path.join(src, "larkx", "extra.py"), "")
        self.assertEqual(pin.installed_commit(pkg, rec)[0], "", "a module the copy lacks")
        self.assertIn("不是从 git 检出安装的", pin.installed_commit(pkg, {"url": "https://example.com/x.whl"})[1])
        self.assertEqual(pin.installed_commit(pkg, None)[0], "")

    def test_verify(self):
        pkg = os.path.join(self.checkout(), "larkx")
        self.assertEqual(pin.verify(A, pkg=pkg), A)
        with self.assertRaises(BackendError) as e:
            pin.verify(B, pkg=pkg)
        self.assertEqual(e.exception.kind, "config")
        self.assertIn("装的是提交 97ad48e", e.exception.message)
        self.assertIn("适配的是 1111111", e.exception.message)
        with self.assertRaises(BackendError) as e:
            pin.verify(A, pkg="")
        self.assertIn("没有装 LarkAgentX", e.exception.message)
        loose = os.path.join(self.d, "loose", "larkx")
        write(os.path.join(loose, "__init__.py"), "")
        with self.assertRaises(BackendError) as e:
            pin.verify(A, pkg=loose, direct_url={})
        self.assertIn("pip install -e", e.exception.message)

    def test_package_dir(self):
        spec = types.SimpleNamespace(submodule_search_locations=[os.path.join(self.d, "x", "larkx")], origin=None)
        self.assertEqual(pin.package_dir(find_spec=lambda n: spec), os.path.join(self.d, "x", "larkx"))
        self.assertEqual(pin.package_dir(find_spec=lambda n: None), "")

        def broken(n):
            raise ValueError("larkx.__spec__ is None")

        self.assertEqual(pin.package_dir(find_spec=broken), "")


if __name__ == "__main__":
    unittest.main()
