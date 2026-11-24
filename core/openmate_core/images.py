"""The picture an /agent reply carries (<connector>_agent_reply's image): taken from where the Agent left it, a
temporary directory on this computer, checked (a PNG or JPEG, not too big), and copied into the extension's own data
so what goes out is what was checked, the same bytes on a retry (the copy is named by its SHA-256)."""

from __future__ import annotations

import errno
import hashlib
import os
import secrets
import stat
import tempfile
import time
from typing import List, Tuple

from .wire import RPCError

IMAGE_MOST = 10 << 20  # 10 MB: a diagram or a screenshot; WeChat itself takes up to 25 MB
KEEP = 7 * 86400.0  # copies older than this are removed (their sends are long over)
SNIFF = ((b"\x89PNG\r\n\x1a\n", ".png", "image/png"), (b"\xff\xd8\xff", ".jpg", "image/jpeg"))


def roots() -> List[Tuple[str, str]]:
    """Where the Agent may leave a picture: the temporary directories of this computer (never the user's own files),
    each as written ("/tmp") and the directory it is (its real path: "/private/tmp" on a Mac)."""
    out: List[Tuple[str, str]] = []
    for d in ("/tmp", os.environ.get("TMPDIR") or "", tempfile.gettempdir()):
        if not d or not os.path.isabs(d) or not os.path.isdir(d):
            continue
        real = os.path.realpath(d)
        if real == "/":
            continue
        for shown in (os.path.normpath(d), real):
            if (shown, real) not in out:
                out.append((shown, real))
    return out


def kind_of(head: bytes) -> Tuple[str, str]:
    """(extension, mime) of a PNG or JPEG by its first bytes; ("", "") for anything else."""
    for magic, ext, mime in SNIFF:
        if head.startswith(magic):
            return ext, mime
    return "", ""


def _open_inside(path: str) -> int:
    """The file at path, opened only through a temporary directory: that directory itself, then each name below it,
    one level at a time and never through a link (a directory swapped for a link meanwhile is refused, not followed).
    The open file's descriptor (non-blocking: a pipe never holds the reply)."""
    norm = os.path.normpath(path)
    for shown, real in roots():
        if os.path.commonpath([norm, shown]) != shown or norm == shown:
            continue
        parts = os.path.relpath(norm, shown).split(os.sep)
        if any(p in ("", ".", "..") for p in parts):
            continue
        fd = os.open(real, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            for name in parts[:-1]:
                nxt = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                              dir_fd=fd)
                os.close(fd)
                fd = nxt
            return os.open(parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                           dir_fd=fd)
        finally:
            os.close(fd)
    raise RPCError("图片要放在临时目录里（/tmp 下），其他位置的文件不会发出：%s" % path)


def take(path: str, data_dir: str) -> Tuple[str, str, str]:
    """(the checked copy's path, its SHA-256, the picture's file name). RPCError, said so the Agent can fix it, when the
    path is not a PNG or JPEG of at most IMAGE_MOST in a temporary directory of this computer (reached without a
    link)."""
    path = str(path or "").strip()
    if not path or not os.path.isabs(path):
        raise RPCError("image 要填图片的绝对路径（比如 /tmp/flow.png）")
    try:
        fd = _open_inside(path)
    except FileNotFoundError:
        raise RPCError("找不到这个图片文件：%s（图片要在运行扩展的这台电脑上）" % path)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
            raise RPCError("图片路径里不能有符号链接，请把图片本身放在 /tmp 下：%s" % path)
        raise RPCError("读不了这个图片文件：%s（%s）" % (path, e.strerror or type(e).__name__))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RPCError("image 不是一个普通文件：%s" % path)
        if st.st_size <= 0:
            raise RPCError("图片文件是空的：%s" % path)
        if st.st_size > IMAGE_MOST:
            raise RPCError("图片太大了（%.1f MB），最多 %d MB" % (st.st_size / 1048576.0, IMAGE_MOST >> 20))
        chunks, n = [], 0
        while n <= IMAGE_MOST:
            b = os.read(fd, 1 << 20)
            if not b:
                break
            chunks.append(b)
            n += len(b)
        data = b"".join(chunks)
    finally:
        os.close(fd)
    if not data or len(data) > IMAGE_MOST:
        raise RPCError("图片文件的大小不对（读的时候还在变？）：%s" % path)
    if not kind_of(data[:16])[0]:
        raise RPCError("只能发 PNG 或 JPG 图片：%s 不是（SVG 要先渲染成 PNG）" % path)
    dest, sha = keep(data, data_dir)
    return dest, sha, os.path.basename(path)[:120]


def keep(data: bytes, data_dir: str) -> Tuple[str, str]:
    """(the copy's path, its SHA-256): a PNG or JPEG of at most IMAGE_MOST (RPCError else) written into the extension's
    own data, named by its bytes, so what goes out is these checked bytes, the same on a retry."""
    ext, _ = kind_of(bytes(data or b"")[:16])
    if not ext or len(data) > IMAGE_MOST:
        raise RPCError("只能发 %d MB 以内的 PNG 或 JPG 图片" % (IMAGE_MOST >> 20))
    sha = hashlib.sha256(data).hexdigest()
    folder = os.path.join(data_dir or tempfile.gettempdir(), "agent-images")
    os.makedirs(folder, mode=0o700, exist_ok=True)
    _prune(folder)
    dest = os.path.join(folder, sha + ext)
    # written fresh every time, under a name of its own (two replies with the same picture never share a half file),
    # then put in place in one step: what is there is these checked bytes
    tmp = "%s.%s.part" % (dest, secrets.token_hex(8))
    try:
        fdw = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fdw, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RPCError("图片没能存进扩展的目录（%s）" % (e.strerror or type(e).__name__))
    return dest, sha


def _prune(folder: str) -> None:
    cut = time.time() - KEEP
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        p = os.path.join(folder, name)
        try:
            if os.path.getmtime(p) < cut:
                os.remove(p)
        except OSError:
            pass
