"""matefeishu_media: the pictures and files of one Feishu message, fetched through the backend's media(ref) into this
user's temporary directory, as local paths a model can read. Read-only: nothing is sent, nothing in Feishu
changes. What it prints is the message's line, each file's local path, type and size, or why a file is not there;
never the ref (its resource key) nor anything of the session.

Every call fetches again, as the account signed in now: Feishu decides what that account may see, and a copy kept
for an account signed in before (a restart in the middle of a switch) is never handed out. The files are written
into <temporary directory>/openmate-feishu-media-<uid>/<a hash of the store's account>/ (0700, this user's own, never
through a link), replacing the one written for the same message before; each call removes the folders of the accounts
signed in before and this account's files older than a day. Not the extension's data directory: Persona's
agent sandbox closes extensions/data/ to the agents it starts, and they are the ones who read these files."""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import re
import stat
import tempfile
import threading
import time
from typing import Dict, List, Tuple

from openmate_core import modelimages, views
from openmate_core.textutil import local_stamp
from openmate_core.backend import BackendError

CONNECTOR_ID = "matefeishu"
MEDIA_TOOL = CONNECTOR_ID + "_media"
MEDIA_MOST = 25 << 20  # 25 MB a file: a picture, a document; a longer video is watched in Feishu
MEDIA_DIR = "openmate-feishu-media"
BASE = ""  # where MEDIA_DIR goes; "" is this user's temporary directory (tests name another)
KEEP_SECONDS = 24 * 3600  # a file fetched for a question is read within minutes; each call fetches again anyway
_ACCOUNT_DIR = re.compile(r"[0-9a-f]{16}")  # fullmatch
_WRITING = threading.Lock()  # the tidy-up and the writes of this process, one at a time
_KINDS_WITH_MEDIA = ("IMAGE", "STICKER", "FILE", "AUDIO", "VOICE", "VIDEO", "MEDIA")
KIND_WORDS = {"image": "图片", "sticker": "表情", "video": "视频", "voice": "语音", "file": "文件"}
_ID = re.compile(r"^[0-9A-Za-z_-]{1,64}$")  # Feishu's are digits; the fixture's have letters
_EXT = re.compile(r"^\.[0-9A-Za-z]{1,10}$")
_EXTS = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp",
         "image/heic": ".heic", "image/avif": ".avif", "image/tiff": ".tif", "video/mp4": ".mp4",
         "audio/ogg": ".ogg", "audio/opus": ".opus", "audio/mpeg": ".mp3", "text/plain": ".txt",
         "application/pdf": ".pdf"}
_REASONS = {  # the backend's words say what happened; these say what it means for the one who asked
    "too_large": "，在飞书里打开看",
    "rejected": "，这个账号看不到它",
}


log = logging.getLogger("openmate.media")


class MediaError(Exception):
    """Why the tool has nothing to show: told to the model as the call's error."""


def media_tools(app) -> Dict:
    """{matefeishu_media: (schema, run)} for Platform.tools."""
    schema = {
        "name": MEDIA_TOOL,
        "description": "看一条飞书消息里的图片或文件（也包括表情、视频、语音）：以现在登录的账号下载到本机临时目录（%d MB 以内），"
                       "返回本地文件路径、类型和大小；图片会直接附在结果里给你看（Persona 支持时），也可以读这个文件看图。消息 id 来自 matefeishu_messages，或 /agent "
                       "上下文里「看图：matefeishu_media message=…」的提示。下载不了时说明原因（没有图片或文件、已撤回或过期、"
                       "超过大小上限、飞书没登录等），如实转告，不要猜内容。只读，不会发任何消息，也不改飞书里的任何东西；"
                       "不要用 lark-cli 去下载。" % (MEDIA_MOST >> 20),
        "input_schema": {"type": "object", "properties": {
            "message": {"type": "string", "description": "消息 id（matefeishu_messages 每行的 id=…，或「看图：matefeishu_media message=…」里的）"},
            "chat": {"type": "string", "description": "这条消息所在的会话 id，可选"}},
            "required": ["message"]},
    }

    def run(a: Dict):
        from openmate_core.wire import RPCError

        try:
            return fetch(app, str(a.get("message") or "").strip(), str(a.get("chat") or "").strip())
        except MediaError as e:
            raise RPCError(str(e))

    return {MEDIA_TOOL: (schema, run)}


def fetch(app, mid: str, chat_id: str = ""):
    """The tool's answer: its text, or {"text", "images"} with the pictures themselves for the model when the hub takes
    them (tool_images): then the model sees each picture without reading its file."""
    store = app.store
    epoch = app.account_epoch() if hasattr(app, "account_epoch") else None  # whose these pictures are
    if store is None:
        error = getattr(app, "setup_error", "")
        raise MediaError("飞书扩展有问题：%s" % error if error else "扩展还在启动，请稍后再试")
    try:
        folder = _folder(app, store.meta_get("account_id"))
    except OSError as e:  # no temporary directory this user can write to
        raise MediaError("存不到本机：没有可用的临时目录（%s）。" % (e.strerror or type(e).__name__))
    if folder:
        try:
            with _WRITING:
                _own_folder(folder)  # and the tidy-up: first, whatever becomes of this call
        except OSError as e:
            raise MediaError("存不到本机：%s。" % (e.strerror or type(e).__name__))
    if not mid:
        raise MediaError("缺少消息 id（matefeishu_messages 每行的 id=…）")
    if not _ID.match(mid):  # not echoed: what was given may be a ref, with its key in it
        raise MediaError("消息 id 不对：飞书消息 id 是一串数字（用 matefeishu_messages 每行的 id=…）")
    row = store.message(chat_id, mid) if chat_id else store.find_message(mid)
    if row is None:
        raise MediaError("本地没有这条飞书消息%s。先用 matefeishu_messages 读它所在的会话，再用那里给出的 id。"
                         % ("：" + mid if mid.isdigit() else ""))
    if row.get("blocked"):
        raise MediaError("消息 id=%s：飞书的安全策略拦下了这条消息，图片和文件都看不到。" % mid)
    # who and when, never what it says: a wrong id must not bring back a chat's words
    who = "我" if row.get("mine") else views.sender_of(row, app.me_name())
    head = "消息 id=%s（%s，%s）" % (mid, who[:60], local_stamp(row.get("at") or 0, app.tz))
    media = [m for m in row.get("media") or [] if isinstance(m, dict)]
    if not media and str(row.get("type") or "").upper() in _KINDS_WITH_MEDIA:
        raise MediaError(head + "\n这条消息的内容没有解出来，没有留下下载信息，取不到内容。")
    if not media:
        raise MediaError(head + "\n这条消息没有图片或文件。")
    if not any(m.get("ref") for m in media):
        raise MediaError(head + "\n这条消息的%s没有留下下载信息，取不到内容。" % _word(media[0]))
    backend = app.backend
    not_ready = _not_ready(app, bool(folder))
    if not_ready:
        raise MediaError(head + "\n" + not_ready)
    lines: List[str] = [head]
    got = 0
    pictures = modelimages.Pictures() if modelimages.CAPABILITY in getattr(app, "hub_capabilities", ()) else None
    for i, m in enumerate(media):
        word = _word(m)
        if not m.get("ref"):
            lines.append("%s：没有留下下载信息，取不到内容。" % word)
            continue
        stem = mid if i == 0 else "%s-%d" % (mid, i)
        try:
            path, mime, size, data = _fetch_one(folder, stem, m, backend,
                                                lambda: epoch is None or app.account_epoch() == epoch)
        except BackendError as e:
            link = getattr(app, "link", "")
            if e.kind == "expired" and link == "error" and getattr(app, "link_reason", ""):
                lines.append("%s下载不了：飞书连不上：%s。" % (word, app.link_reason))  # a proxy, a setting
            elif e.kind == "expired" and link in ("idle", "connecting", "down"):
                lines.append("%s下载不了：飞书正在恢复登录（扩展刚启动或刚重连），稍后再试一次。" % word)
            else:
                lines.append("%s下载不了：%s%s。" % (word, e.message, _REASONS.get(e.kind, "")))
            continue
        except OSError as e:
            lines.append("%s下载了但存不到本机：%s。" % (word, e.strerror or type(e).__name__))
            continue
        got += 1
        bits = [mime, _size(size)]
        if m.get("width") and m.get("height"):
            bits.append("%d×%d" % (int(m["width"]), int(m["height"])))
        name = "「%s」" % str(m["name"])[:120] if m.get("name") else ""
        lines.append("%s%s 本地文件：%s（%s）" % (word, name, path, "，".join(bits)))
        if not mime.startswith("image/"):
            continue
        why = ""
        if pictures is not None:
            why = pictures.add(data, "消息 id=%s 的%s%s" % (mid, word, (" %d" % (i + 1)) if len(media) > 1 else ""))
            if not why:
                lines.append("这张%s已经直接附在这条结果里（第 %d 张），可以直接看。" % (word, len(pictures.images)))
                continue
        lines.append("%s可以直接读这个文件看图（路径在运行飞书扩展的这台电脑上）。" % (why + "，" if why else ""))
    if not got:
        raise MediaError("\n".join(lines))
    if epoch is not None and app.account_epoch() != epoch:  # another account signed in meanwhile: none of it
        raise MediaError("刚换了飞书账号，这次取到的内容不再给出，请重新读这条消息。")
    if pictures is not None and pictures.images:
        return {"text": "\n".join(lines), "images": pictures.images}
    return "\n".join(lines)


def _not_ready(app, account_known: bool) -> str:
    """Why nothing can be fetched now ("" when it can), told as the core tells the hub's media reads."""
    signed_in = app.backend is not None and app.signed_in
    link = getattr(app, "link", "")
    if signed_in and link != "expired" and account_known:
        return ""
    if link == "expired" or getattr(app, "creds", "") == "none":
        return "飞书还没登录（或登录已失效），下载不了：先在 Persona 的飞书扩展里扫码登录，再试一次。"
    error = getattr(app, "setup_error", "") or (getattr(app, "creds_error", "") if getattr(app, "creds", "") == "error"
                                               else "")
    if error:
        return "飞书扩展有问题，下载不了：%s" % error
    return "飞书正在恢复登录（扩展刚启动或刚重连），稍后再试一次。"


def _folder(app, account: str) -> str:
    """<temporary directory>/openmate-feishu-media-<uid>-<this extension's>/<the account's>; "" for no account."""
    if not account:
        return ""
    whose = hashlib.sha256(("openmate-feishu:" + account).encode("utf-8")).hexdigest()[:16]
    ours = hashlib.sha256(os.path.abspath(app.data_dir or "").encode("utf-8")).hexdigest()[:8]  # this extension's own
    return os.path.join(BASE or tempfile.gettempdir(), "%s-%d-%s" % (MEDIA_DIR, os.getuid(), ours), whose)


def _fetch_one(folder: str, stem: str, m: dict, backend, still=lambda: True) -> Tuple[str, str, int, bytes]:
    """(path, mime, size, bytes): the media fetched now and written in place of the one written for it before; the
    bytes as they came (what the model is shown: never read back from the file, which others may replace)."""
    mime, data = backend.media(str(m["ref"]), MEDIA_MOST)
    data = bytes(data or b"")
    if len(data) > MEDIA_MOST:
        raise BackendError("附件超过 %d MB" % (MEDIA_MOST >> 20), "too_large")
    if not data:
        raise BackendError("飞书给回来的是空的", "unknown")
    mime = (mime or "").split(";", 1)[0].strip().lower()
    declared = str(m.get("mime") or "").split(";", 1)[0].strip().lower()
    # Feishu said this attachment is not text (its kind or its type), and answered with a web page: an answer (a sign-in page), not the file.
    # By the answer's own type only: what a file holds is not guessed at (an HTML template sent as text is a file)
    not_text = str(m.get("kind") or "") in ("voice", "video", "image", "sticker") or (
        declared and not declared.startswith("text/") and declared not in ("application/xhtml+xml", "application/octet-stream"))
    if mime in ("text/html", "application/xhtml+xml") and not_text:
        raise BackendError("下载时飞书回答的是一个网页，不是这个附件（可能登录失效了）", "unknown")
    if mime in ("", "application/octet-stream", "binary/octet-stream") and m.get("mime"):
        mime = str(m["mime"]).split(";", 1)[0].strip().lower()  # what the message itself said it is
    mime = mime or "application/octet-stream"
    with _WRITING:
        if not still():  # another account signed in while it came: its folder is not touched for this one
            raise MediaError("刚换了飞书账号，这次取到的内容不再给出，请重新读这条消息。")
        path, mime, size = _write(folder, stem, m, mime, data)
    return path, mime, size, data


def _write(folder: str, stem: str, m: dict, mime: str, data: bytes) -> Tuple[str, str, int]:
    _own_folder(folder)
    path = os.path.join(folder, stem + _ext(mime, str(m.get("name") or "")))
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".part-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)  # a reader of the one before keeps it whole; a failed write leaves it as it was
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    for old in _written(folder, stem):  # the one before, of another type now: gone once this one is in place
        if old != path:
            try:
                os.remove(old)
            except OSError:
                pass
    return path, mime, len(data)


def _own_folder(folder: str) -> None:
    """The folder and the one above it as real directories of this user's, closed to others (in a /tmp shared with
    other users, one of them may have made the name first), never through a link. The accounts before are tidied
    away as soon as the folder above is known to be ours, before this account's is made (which may fail)."""
    for d in (os.path.dirname(folder), folder):
        if d == folder:
            _tidy_others(folder)
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode):
            raise OSError(0, "%s 不是一个目录（可能是链接），不往那里写" % os.path.basename(d))
        if st.st_uid != os.getuid():
            raise OSError(0, "%s 不是这个用户的，不往那里写" % os.path.basename(d))
        if stat.S_IMODE(st.st_mode) != 0o700:
            os.chmod(d, 0o700)
    _tidy_old(folder)  # only now that it is known to be this user's own folder


def _tidy_others(folder: str) -> None:
    """The folders of the accounts signed in here before (one account at a time): no one should read them any more."""
    parent, mine = os.path.split(folder)
    try:
        names = os.listdir(parent)
    except OSError:
        return
    for n in names:
        p = os.path.join(parent, n)
        if n == mine or not _ACCOUNT_DIR.fullmatch(n) or os.path.islink(p) or not os.path.isdir(p):
            continue
        _remove_tree(p)


def _tidy_old(folder: str) -> None:
    """This account's files older than KEEP_SECONDS (each call fetches again)."""
    old = time.time() - KEEP_SECONDS
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for n in names:
        p = os.path.join(folder, n)
        try:
            st = os.lstat(p)
            if not stat.S_ISDIR(st.st_mode) and st.st_mtime < old:
                os.remove(p)
        except OSError:
            pass


def _remove_tree(top: str) -> None:
    """An account's folder before, whatever its modes: made writable first (it is this user's), then removed. What is
    left (another user's file in it) is logged, not hidden."""
    try:
        os.chmod(top, 0o700)  # a folder that cannot be read cannot be walked
    except OSError:
        pass
    for d, subdirs, _ in os.walk(top):
        for x in [os.path.join(d, s) for s in subdirs]:
            try:
                if not os.path.islink(x):
                    os.chmod(x, 0o700)
            except OSError:
                pass
    left = 0
    for d, subdirs, files in os.walk(top, topdown=False):
        for x in [os.path.join(d, f) for f in files] + [os.path.join(d, s) for s in subdirs]:
            try:
                os.rmdir(x) if os.path.isdir(x) and not os.path.islink(x) else os.remove(x)
            except OSError:
                left += 1
    try:
        os.rmdir(top)
    except OSError:
        left += 1
    if left:
        log.warning("matefeishu_media: %d old entries could not be removed", left)


def _written(folder: str, stem: str) -> List[str]:
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    return [os.path.join(folder, n) for n in names if os.path.splitext(n)[0] == stem and not n.startswith(".")]


def _ext(mime: str, name: str) -> str:
    own = os.path.splitext(os.path.basename(name))[1]
    if _EXT.match(own):
        return own.lower()
    return _EXTS.get(mime) or mimetypes.guess_extension(mime) or ".bin"


def _word(m: dict) -> str:
    return KIND_WORDS.get(str(m.get("kind") or ""), "附件")


def _size(n: int) -> str:
    if n >= 1 << 20:
        return "%.1f MB" % (n / float(1 << 20))
    if n >= 1 << 10:
        return "%.0f KB" % (n / 1024.0)
    return "%d 字节" % n
