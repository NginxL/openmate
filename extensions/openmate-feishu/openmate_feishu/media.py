"""matefeishu_media: the pictures and files of one Feishu message, fetched through the backend's media(ref) into this
user's temporary directory, as local paths a model can read. Read-only: nothing is sent, nothing in Feishu
changes. What it prints is the message's line, each file's local path, type and size, or why a file is not there;
never the ref (its resource key) nor anything of the session.

Every call fetches again, as the account signed in now: Feishu decides what that account may see, and a copy kept
for an account signed in before (a restart in the middle of a switch) is never handed out. The files are written
into <temporary directory>/openmate-feishu-media-<uid>/<a hash of the store's account>/ (0700, this user's own, never
through a link), replacing the one written for the same message before. Not the extension's data directory: Persona's
agent sandbox closes extensions/data/ to the agents it starts, and they are the ones who read these files."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import stat
import tempfile
from typing import Dict, List, Tuple

from openmate_core import views
from openmate_core.backend import BackendError

CONNECTOR_ID = "matefeishu"
MEDIA_TOOL = CONNECTOR_ID + "_media"
MEDIA_MOST = 25 << 20  # 25 MB a file: a picture, a document; a longer video is watched in Feishu
MEDIA_DIR = "openmate-feishu-media"
BASE = ""  # where MEDIA_DIR goes; "" is this user's temporary directory (tests name another)
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


class MediaError(Exception):
    """Why the tool has nothing to show: told to the model as the call's error."""


def media_tools(app) -> Dict:
    """{matefeishu_media: (schema, run)} for Platform.tools."""
    schema = {
        "name": MEDIA_TOOL,
        "description": "看一条飞书消息里的图片或文件（也包括表情、视频、语音）：以现在登录的账号下载到本机临时目录（%d MB 以内），"
                       "返回本地文件路径、类型和大小，图片可以直接读这个文件看图。消息 id 来自 matefeishu_messages，或 /agent "
                       "上下文里「看图：matefeishu_media message=…」的提示。下载不了时说明原因（没有图片或文件、已撤回或过期、"
                       "超过大小上限、飞书没登录等），如实转告，不要猜内容。只读，不会发任何消息，也不改飞书里的任何东西；"
                       "不要用 lark-cli 去下载。" % (MEDIA_MOST >> 20),
        "input_schema": {"type": "object", "properties": {
            "message": {"type": "string", "description": "消息 id（matefeishu_messages 每行的 id=…，或「看图：matefeishu_media message=…」里的）"},
            "chat": {"type": "string", "description": "这条消息所在的会话 id，可选"}},
            "required": ["message"]},
    }

    def run(a: Dict) -> str:
        from openmate_core.wire import RPCError

        try:
            return fetch(app, str(a.get("message") or "").strip(), str(a.get("chat") or "").strip())
        except MediaError as e:
            raise RPCError(str(e))

    return {MEDIA_TOOL: (schema, run)}


def fetch(app, mid: str, chat_id: str = "") -> str:
    if not mid:
        raise MediaError("缺少消息 id（matefeishu_messages 每行的 id=…）")
    if not _ID.match(mid):  # not echoed: what was given may be a ref, with its key in it
        raise MediaError("消息 id 不对：飞书消息 id 是一串数字（用 matefeishu_messages 每行的 id=…）")
    store = app.store
    if store is None:
        raise MediaError("扩展还在启动，请稍后再试")
    row = store.message(chat_id, mid) if chat_id else store.find_message(mid)
    if row is None:
        raise MediaError("本地没有这条飞书消息%s。先用 matefeishu_messages 读它所在的会话，再用那里给出的 id。"
                         % ("：" + mid if mid.isdigit() else ""))
    head = "消息 id=%s：%s" % (mid, views.log_line(row, app.tz, app.me_name()).replace("\n", " ")[:300])
    if row.get("blocked"):
        raise MediaError(head + "\n飞书的安全策略拦下了这条消息，图片和文件都看不到。")
    media = [m for m in row.get("media") or [] if isinstance(m, dict)]
    if not media and str(row.get("type") or "").upper() in _KINDS_WITH_MEDIA:
        raise MediaError(head + "\n这条消息的内容没有解出来，没有留下下载信息，取不到内容。")
    if not media:
        raise MediaError(head + "\n这条消息没有图片或文件。")
    if not any(m.get("ref") for m in media):
        raise MediaError(head + "\n这条消息的%s没有留下下载信息，取不到内容。" % _word(media[0]))
    backend = app.backend
    account = store.meta_get("account_id")
    if backend is None or not app.signed_in or getattr(app, "link", "") == "expired" or not account:
        raise MediaError(head + "\n飞书还没登录（或登录已失效），下载不了：先在 Persona 的飞书扩展里扫码登录，再试一次。")
    whose = hashlib.sha256(("openmate-feishu:" + account).encode("utf-8")).hexdigest()[:16]
    folder = os.path.join(BASE or tempfile.gettempdir(), "%s-%d" % (MEDIA_DIR, os.getuid()), whose)
    lines: List[str] = [head]
    got = 0
    for i, m in enumerate(media):
        word = _word(m)
        if not m.get("ref"):
            lines.append("%s：没有留下下载信息，取不到内容。" % word)
            continue
        stem = mid if i == 0 else "%s-%d" % (mid, i)
        try:
            path, mime, size = _fetch_one(folder, stem, m, backend)
        except BackendError as e:
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
        if mime.startswith("image/"):
            lines.append("可以直接读这个文件看图（路径在运行飞书扩展的这台电脑上）。")
    if not got:
        raise MediaError("\n".join(lines))
    return "\n".join(lines)


def _fetch_one(folder: str, stem: str, m: dict, backend) -> Tuple[str, str, int]:
    """(path, mime, size): the media fetched now and written in place of the one written for it before."""
    mime, data = backend.media(str(m["ref"]), MEDIA_MOST)
    data = bytes(data or b"")
    if len(data) > MEDIA_MOST:
        raise BackendError("附件超过 %d MB" % (MEDIA_MOST >> 20), "too_large")
    if not data:
        raise BackendError("飞书给回来的是空的", "unknown")
    mime = (mime or "application/octet-stream").split(";", 1)[0].strip().lower()
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
    other users, one of them may have made the name first), never through a link."""
    for d in (os.path.dirname(folder), folder):
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode):
            raise OSError(0, "%s 不是一个目录（可能是链接），不往那里写" % os.path.basename(d))
        if st.st_uid != os.getuid():
            raise OSError(0, "%s 不是这个用户的，不往那里写" % os.path.basename(d))
        if st.st_mode & 0o077:
            os.chmod(d, 0o700)


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
