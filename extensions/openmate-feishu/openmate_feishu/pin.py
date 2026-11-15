"""Which LarkAgentX commit is installed. The backend is written against one commit (the setting larkx_commit) and
refuses to run on another: LarkAgentX is a reverse-engineered client, so its internals change between commits.

How the commit is found, in this order:

1. the package is the `larkx` directory of a git checkout (the usual `pip install -e <checkout>`): the checkout's
   HEAD, read from .git without running git;
2. pip's record of where it came from (direct_url.json): the commit of a `pip install git+…@<commit>`, or, for a
   copy installed from a local checkout, that checkout's HEAD once every installed file matches the checkout's.

A copy with neither (a wheel built somewhere else) cannot be told apart and is refused with how to install it.
Local edits in a checkout are not detected: only HEAD is compared.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from typing import Callable, Optional, Tuple
from urllib.parse import unquote, urlsplit

from openmate_core.backend import BackendError

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_SKIP = {"__pycache__", ".DS_Store"}


def package_dir(name: str = "larkx", find_spec: Callable = importlib.util.find_spec) -> str:
    """The installed package's directory ("" when it is not installed in this interpreter)."""
    try:
        spec = find_spec(name)
    except (ImportError, ValueError):
        return ""
    if spec is None:
        return ""
    locations = list(spec.submodule_search_locations or [])
    if locations:
        return os.path.abspath(locations[0])
    if spec.origin:
        return os.path.dirname(os.path.abspath(spec.origin))
    return ""


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def git_dir(root: str) -> str:
    """root's .git directory (following a .git file, as worktrees and submodules have), "" when it is not a checkout."""
    dot = os.path.join(root, ".git")
    if os.path.isdir(dot):
        return dot
    if os.path.isfile(dot):
        try:
            text = _read(dot)
        except OSError:
            return ""
        if text.startswith("gitdir:"):
            target = text[len("gitdir:"):].strip()
            target = target if os.path.isabs(target) else os.path.normpath(os.path.join(root, target))
            return target if os.path.isdir(target) else ""
    return ""


def head_commit(gitdir: str) -> str:
    """The commit HEAD names in gitdir (a detached HEAD or a branch, loose or packed), "" when unreadable."""
    try:
        head = _read(os.path.join(gitdir, "HEAD"))
    except OSError:
        return ""
    if _HEX40.match(head):
        return head
    if not head.startswith("ref:"):
        return ""
    ref = head[len("ref:"):].strip()
    common = gitdir
    try:
        common = os.path.normpath(os.path.join(gitdir, _read(os.path.join(gitdir, "commondir"))))
    except OSError:
        pass
    for base in (gitdir, common):
        try:
            value = _read(os.path.join(base, ref))
        except OSError:
            continue
        if _HEX40.match(value):
            return value
    try:
        with open(os.path.join(common, "packed-refs"), encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split(" ", 1)
                if len(parts) == 2 and parts[1] == ref and _HEX40.match(parts[0]):
                    return parts[0]
    except OSError:
        pass
    return ""


def same_files(installed: str, source: str) -> bool:
    """Every file of the installed package is in source with the same bytes, and source has no module it lacks."""
    def files(base: str) -> set:
        out = set()
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _SKIP]
            for n in filenames:
                if n in _SKIP or n.endswith((".pyc", ".pyo")):
                    continue
                out.add(os.path.relpath(os.path.join(dirpath, n), base))
        return out

    a, b = files(installed), files(source)
    if not a or {f for f in b if f.endswith(".py")} - a:
        return False
    for rel in a:
        try:
            with open(os.path.join(installed, rel), "rb") as f1, open(os.path.join(source, rel), "rb") as f2:
                if f1.read() != f2.read():
                    return False
        except OSError:
            return False
    return True


def _direct_url(name: str) -> Optional[dict]:
    try:
        from importlib import metadata
    except ImportError:  # pragma: no cover - Python 3.8
        return None
    try:
        text = metadata.distribution(name).read_text("direct_url.json")
    except Exception:
        return None
    if not text:
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def installed_commit(pkg: str, direct_url: Optional[dict]) -> Tuple[str, str]:
    """(commit, how) for the package in directory pkg: how is git | pip | copy; ("", why) when it cannot be told."""
    gd = git_dir(os.path.dirname(pkg))
    if gd:
        commit = head_commit(gd)
        return (commit, "git") if commit else ("", "读不出 LarkAgentX 检出目录的 git 版本")
    if direct_url:
        vcs = direct_url.get("vcs_info") if isinstance(direct_url.get("vcs_info"), dict) else {}
        commit = str(vcs.get("commit_id") or "").lower()
        if _HEX40.match(commit):
            return commit, "pip"
        url = urlsplit(str(direct_url.get("url") or ""))
        if url.scheme == "file":
            src = unquote(url.path)
            gd = git_dir(src)
            if gd:
                commit = head_commit(gd)
                if not commit:
                    return "", "读不出 LarkAgentX 来源目录的 git 版本"
                if not same_files(pkg, os.path.join(src, os.path.basename(pkg))):
                    return "", "装好的 LarkAgentX 和它的来源检出目录里的文件不一样（检出目录换了版本，或者装完后改过）"
                return commit, "copy"
    return "", "装好的 LarkAgentX 不是从 git 检出安装的，看不出它的版本"


def verify(expected: str, pkg: Optional[str] = None, direct_url: Optional[dict] = None) -> str:
    """The installed commit when it is expected; BackendError(kind="config") otherwise."""
    short = expected[:7]
    how_to = "请用 git 检出 LarkAgentX 的提交 %s，再在这个 Python 里执行 pip install -e <检出目录>" % short
    if pkg is None:
        pkg = package_dir()
        if pkg and direct_url is None:
            direct_url = _direct_url("larkx")
    if not pkg:
        raise BackendError("LarkAgentX 的 Python 里没有装 LarkAgentX（找不到 larkx 包）。" + how_to, "config")
    commit, how = installed_commit(pkg, direct_url)
    if not commit:
        raise BackendError("无法确认 LarkAgentX 的版本：%s。%s" % (how, how_to), "config")
    if commit != expected:
        raise BackendError("LarkAgentX 的版本不对：装的是提交 %s，这个扩展适配的是 %s。请切换到适配的提交；确认新版本可用时，"
                           "也可以改设置里的“LarkAgentX 版本”（larkx_commit）" % (commit[:7], short), "config")
    return commit
