"""Pictures, voice notes, videos and files in WeChat messages: fetched into the extension's data directory, so the
assistant and /agent read a local file instead of a placeholder.

How it goes:
- matewechat-native reads what a media message says about its content (the CDN file ids, their AES keys, sizes) into
  the message event's "media", and a voice note's SILK or a picture's thumbnail that came inside the message into
  "inline". The event, with "media", is kept only in this backend's own database (wechat.db, 0600): the keys open
  only these files, and never go to the hub, a log or a message text. "inline" is written to a file and dropped.
- Right after a message is kept (live or caught up), a worker fetches its media through the program ("media" for a
  CDN object, "voice" for a voice note as WAV) into data/media/, and the message's text becomes what is there:
  "[图片] 本地文件：/…/123.jpg". The core gets the new text as history (it renews a stored message's text), so
  matewechat_messages and /agent's context show it.
- A forwarded chat record (聊天记录) needs no fetch: its lines are in the message, shown as text.
- Limits: a CDN object of at most 25 MB (one ordinary transfer; a bigger file or video is left with its reason), a
  voice note of at most 60 s. WeChat keeps media only for a while: what it no longer has is said so. Local copies go
  after MEDIA_KEEP_DAYS.
- A failure leaves the placeholder with the reason: "[图片]（没能下载：文件已过期，微信服务器上取不到了）".
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .native import clean_env

log = logging.getLogger("openmate.wechat.media")

MOST = 25 * 1024 * 1024  # one ordinary CDN transfer (matewechat-native's limit too)
MEDIA_KEEP_DAYS = 30
MEDIA_TOTAL_MOST = 2 << 30  # the local copies together: beyond it the oldest go first (checked before each fetch)
ROOM_EVERY = 60.0  # how often the total is measured
VOICE_TEXT_WAIT = 120.0
FRAME_WAIT = 30.0
RECORD_LINES = 60
RECORD_CHARS = 4000
WORKERS = 2
KINDS = ("image", "voice", "video", "file")

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS media (
        id TEXT PRIMARY KEY, state TEXT NOT NULL, files TEXT NOT NULL DEFAULT '{}', note TEXT NOT NULL DEFAULT '',
        at REAL NOT NULL)""",
)

# What a file's first bytes say it is: (extension, mime)
MAGIC = (
    (b"\xff\xd8\xff", ("jpg", "image/jpeg")),
    (b"\x89PNG\r\n\x1a\n", ("png", "image/png")),
    (b"GIF87a", ("gif", "image/gif")),
    (b"GIF89a", ("gif", "image/gif")),
    (b"BM", ("bmp", "image/bmp")),
)
MIMES = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
         "heic": "image/heic", "wav": "audio/wav", "mp4": "video/mp4", "pdf": "application/pdf",
         "txt": "text/plain", "md": "text/markdown", "csv": "text/csv", "json": "application/json",
         "zip": "application/zip", "doc": "application/msword", "xls": "application/vnd.ms-excel",
         "ppt": "application/vnd.ms-powerpoint",
         "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}
RECORD_TYPES = {"1": "", "2": "[图片]", "3": "[语音]", "4": "[视频]", "5": "[链接]", "6": "[位置]", "8": "[文件]",
                "15": "[视频]", "17": "[聊天记录]", "19": "[小程序]", "22": "[视频号]", "47": "[表情]"}


def sniff(data: bytes) -> Tuple[str, str]:
    """(extension, mime) of a picture's bytes; ("", "") when it is not a picture this knows."""
    for head, kind in MAGIC:
        if data.startswith(head):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"):
        return "heic", "image/heic"
    return "", ""


def mime_of(path: str) -> str:
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return MIMES.get(ext, "application/octet-stream")


def safe_name(name: str, fallback: str) -> str:
    """A file name to keep on disk: no directories, no control characters, not hidden, bounded."""
    n = re.sub(r"[\x00-\x1f\x7f/\\:]", "_", (name or "").strip()).strip(". ")
    if not n:
        n = fallback
    if len(n) > 120:
        root, ext = os.path.splitext(n)
        n = root[: 120 - len(ext[:16])] + ext[:16]
    return n


def human_size(n: int) -> str:
    if n >= 1 << 20:
        return "%.1f MB" % (n / (1 << 20))
    if n >= 1 << 10:
        return "%d KB" % (n // 1024)
    return "%d 字节" % n


def find_ffmpeg() -> str:
    for p in (shutil.which("ffmpeg"), "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return ""


# ---------------------------------------------------------------- text


def record_text(media: dict) -> str:
    """A forwarded chat record as lines: "[聊天记录] title（n 条）：" then "who（when）：text"."""
    items = [i for i in media.get("items") or [] if isinstance(i, dict)]
    title = str(media.get("title") or "聊天记录")
    head = "[聊天记录] %s（%d 条）：" % (title, len(items)) if items else "[聊天记录] " + title
    lines = [head]
    for it in items[:RECORD_LINES]:
        kind = str(it.get("type") or "")
        said = str(it.get("text") or "").strip()
        tag = RECORD_TYPES.get(kind, "[消息]")
        if kind == "8" and it.get("title"):
            said = "[文件] " + str(it.get("title"))
        elif kind == "17":
            said = "[聊天记录] " + str(it.get("title") or said)
        elif tag and kind != "1":
            said = (tag + " " + said).strip() if kind == "5" else tag
        who = str(it.get("who") or "").strip() or "未知"
        when = str(it.get("when") or "").strip()
        lines.append("%s%s：%s" % (who, "（%s）" % when if when else "", said.replace("\n", " ") or tag or "[消息]"))
    if len(items) > RECORD_LINES:
        lines.append("……还有 %d 条" % (len(items) - RECORD_LINES))
    if not items and media.get("desc"):
        lines.append(str(media.get("desc")))
    text = "\n".join(lines)
    return text if len(text) <= RECORD_CHARS else text[: RECORD_CHARS - 1] + "…"


def render(ev: dict, row: Optional[dict]) -> str:
    """The message's text with what was fetched: local paths, a voice note's words, or why there is nothing."""
    media = ev.get("media") if isinstance(ev.get("media"), dict) else None
    base = str(ev.get("text") or "")
    if media is None:
        return base
    kind = media.get("kind")
    if kind == "record":
        return record_text(media)
    if kind not in KINDS:
        return base
    if kind == "voice" and media.get("ms"):
        base = "[语音 %d 秒]" % max(1, round(int(media["ms"]) / 1000))
    elif kind == "video" and media.get("seconds"):
        base = "[视频 %d 秒]" % int(media["seconds"])
    elif kind == "file":
        name = str(media.get("name") or "")
        base = "[文件] %s%s" % (name, "（%s）" % human_size(int(media["size"])) if media.get("size") else "")
    if row is None:
        return base
    files, note, state = row.get("files") or {}, row.get("note") or "", row.get("state")
    if state == "pending":
        return base + "（正在下载）"
    if state == "gone":
        return base + "（本地副本已按 %d 天清理）" % MEDIA_KEEP_DAYS
    parts: List[str] = []
    if kind == "image" and files.get("image"):
        what = "本地文件" if not files.get("thumbnail_only") else "本地文件（只有缩略图）"
        parts.append("%s：%s" % (what, files["image"]))
    elif kind == "voice" and files.get("audio"):
        parts.append("音频：%s" % files["audio"])
        if files.get("transcript"):
            parts.append("转文字：%s" % files["transcript"])
    elif kind == "video":
        if files.get("cover"):
            parts.append("封面：%s" % files["cover"])
        if files.get("video"):
            parts.append("视频：%s" % files["video"])
        if files.get("frames"):
            parts.append("截图：%s" % "、".join(files["frames"]))
    elif kind == "file" and files.get("file"):
        parts.append("本地文件：%s" % files["file"])
    text = base + (" " + "；".join(parts) if parts else "")
    if note:
        text += "（%s）" % note
    return text


# ---------------------------------------------------------------- fetching


class Fetcher:
    """Fetches media into `root` with the program, one message at a time per worker; tells `done(id)` after each."""

    def __init__(self, root: str, db_call: Callable[..., Any], call: Callable[..., dict],
                 done: Callable[[str], None], voice_cmd: str = ""):
        self.root = root
        self._db = db_call  # (sql, args, fetch) → rows: the backend's store, under its lock
        self._call = call  # (op, timeout, **params) → answer; raises NativeError-like (kind, message)
        self._done = done
        self.voice_cmd = (voice_cmd or "").strip()
        self._mu = threading.Lock()
        self._cv = threading.Condition(self._mu)
        self._queue: List[Tuple[str, dict]] = []
        self._busy: set = set()
        self._workers = 0  # running workers, counted under the lock (one leaving can't strand a new request)
        self._closed = False
        self._finished = threading.Condition(threading.Lock())
        self._epoch = 0  # an account's fetches: another account signed in drops them (reset)
        self._room_at = 0.0
        os.makedirs(root, exist_ok=True)
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass

    # ---- state

    def state(self, mid: str) -> Optional[dict]:
        rows = self._db("SELECT state, files, note, at FROM media WHERE id=?", (mid,), True)
        if not rows:
            return None
        state, files, note, at = rows[0]
        try:
            files = json.loads(files or "{}")
        except ValueError:
            files = {}
        return {"state": state, "files": files, "note": note, "at": at}

    def _set(self, mid: str, state: str, files: dict, note: str) -> None:
        self._db("INSERT OR REPLACE INTO media (id, state, files, note, at) VALUES (?, ?, ?, ?, ?)",
                 (mid, state, json.dumps(files, ensure_ascii=False), note, time.time()), False)

    def pending(self) -> List[str]:
        return [r[0] for r in self._db("SELECT id FROM media WHERE state='pending'", (), True)]

    def path(self, mid: str, suffix: str) -> str:
        return os.path.join(self.root, safe_name(mid, "m") + suffix)

    # ---- inline data (the event's "inline"): kept as a file, never in the database

    def keep_inline(self, mid: str, media: dict, data_b64: str) -> None:
        try:
            data = base64.b64decode(data_b64, validate=True)
        except (ValueError, TypeError):
            return
        kind = media.get("kind")
        if kind == "voice" and data[1:10] == b"#!SILK_V3":
            name = self.path(mid, ".silk")
        elif kind == "image" and sniff(data)[0] in ("jpg", "png"):
            name = self.path(mid, "-inline." + sniff(data)[0])
        else:
            return
        try:
            with open(name, "wb") as f:
                f.write(data)
        except OSError:
            log.warning("a message's inline media could not be kept")

    # ---- the queue

    def want(self, mid: str, media: dict, force: bool = False) -> None:
        """Fetch this message's media (again when force), in the background."""
        if not mid or not isinstance(media, dict) or media.get("kind") not in KINDS:
            return
        st = self.state(mid)
        if st is not None and not force and st["state"] in ("done", "failed", "gone"):
            return
        if st is None or force:
            self._set(mid, "pending", (st or {}).get("files") or {}, "")
        with self._cv:
            if self._closed or mid in self._busy or any(q[0] == mid for q in self._queue):
                return
            self._queue.append((mid, media))
            self._cv.notify()
            if self._workers < WORKERS:
                self._workers += 1
                threading.Thread(target=self._work, name="matewechat-media", daemon=True).start()

    def wait(self, mid: str, timeout: float) -> Optional[dict]:
        """The state once this message's fetch has ended (or after timeout)."""
        deadline = time.time() + timeout
        while True:
            with self._mu:
                busy = mid in self._busy or any(q[0] == mid for q in self._queue)
            st = self.state(mid)
            if not busy and (st is None or st["state"] != "pending"):
                return st
            left = deadline - time.time()
            if left <= 0:
                return st
            with self._finished:
                self._finished.wait(min(left, 0.5))

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._queue.clear()
            self._cv.notify_all()

    def reset(self) -> None:
        """Another account signed in: its queued fetches are dropped, and one running now keeps nothing."""
        with self._cv:
            self._epoch += 1
            self._queue.clear()

    def _work(self) -> None:
        while True:
            with self._cv:
                while not self._queue and not self._closed:
                    if not self._cv.wait(timeout=30) and not self._queue:
                        self._workers -= 1  # idle: the next want() starts a worker again
                        return
                if self._closed:
                    self._workers -= 1
                    return
                mid, media = self._queue.pop(0)
                self._busy.add(mid)
                epoch = self._epoch
            stale = False
            try:
                before = self.state(mid) or {}
                for gone in self.make_room():
                    self._tell(gone)
                files, note = self.fetch(mid, media)
                with self._cv:
                    stale = epoch != self._epoch
                if stale:
                    self._remove(mid, files)  # another account signed in meanwhile: nothing of this one's kept
                else:
                    if not files and self._present(before.get("files")):
                        files, note = before["files"], ""  # a fetch again that failed keeps the copy from before
                    self._set(mid, "done" if files else "failed", files, note)
            except Exception as e:  # noqa: BLE001
                log.exception("fetching a WeChat message's media failed")
                with self._cv:
                    stale = epoch != self._epoch
                if not stale:
                    self._set(mid, "failed", {}, "下载出错：%s" % type(e).__name__)
            finally:
                with self._cv:
                    self._busy.discard(mid)
                with self._finished:
                    self._finished.notify_all()
            if not stale:
                self._tell(mid)

    def _tell(self, mid: str) -> None:
        try:
            self._done(mid)
        except Exception:
            log.exception("the new text of a WeChat media message could not be passed on")

    def _present(self, files: Any) -> bool:
        return isinstance(files, dict) and any(
            isinstance(p, str) and p.startswith(self.root + os.sep) and os.path.isfile(p) for p in files.values())

    # ---- one message

    def _cdn(self, quality: str, file_id: Any, key: Any, out: str, size: Any = None,
             md5: Any = None) -> Tuple[bool, str]:
        """One CDN object into out: (ok, why not). A file's size and MD5 (from the message) are checked."""
        if not file_id or not key:
            return False, "消息里没有下载信息"
        params: Dict[str, Any] = {"quality": quality, "file_id": file_id, "aes_key": key, "path": out, "most": MOST}
        if size:
            params["size"] = int(size)
        if isinstance(md5, str) and md5:
            params["md5"] = md5
        try:
            self._call("media", 240, **params)
            return True, ""
        except Exception as e:  # noqa: BLE001
            return False, reason(e)

    def fetch(self, mid: str, media: dict) -> Tuple[dict, str]:
        """({what: local path}, a note) for one message; no files means nothing could be fetched (the note says why)."""
        kind = media.get("kind")
        if kind == "image":
            return self._image(mid, media)
        if kind == "voice":
            return self._voice(mid, media)
        if kind == "video":
            return self._video(mid, media)
        if kind == "file":
            return self._file(mid, media)
        return {}, ""

    def _picture(self, raw_path: str, mid: str, suffix: str) -> Tuple[str, str]:
        """Name a fetched picture by what it is: (path, why not a picture)."""
        try:
            with open(raw_path, "rb") as f:
                head = f.read(16)
        except OSError:
            return "", "下载的文件读不出来"
        ext, _ = sniff(head)
        if not ext:
            os.remove(raw_path)
            if head.startswith(b"wxgf"):
                return "", "原图是微信专有格式（wxgf），读不了"
            return "", "下载的不是能识别的图片格式"
        final = self.path(mid, "%s.%s" % (suffix, ext))
        os.replace(raw_path, final)
        return final, ""

    def _image(self, mid: str, m: dict) -> Tuple[dict, str]:
        key = m.get("aes_key")
        why = ""
        for quality, fid, k in (("standard", m.get("mid"), key), ("original", m.get("big"), key),
                                ("thumbnail", m.get("thumb"), m.get("thumb_aes_key") or key)):
            if not fid:
                continue
            raw = self.path(mid, ".download")
            ok, w = self._cdn(quality, fid, k, raw)
            if ok:
                pic, w = self._picture(raw, mid, "" if quality != "thumbnail" else "-thumb")
                if pic:
                    files: Dict[str, Any] = {"image": pic}
                    if quality == "thumbnail":
                        files["thumbnail_only"] = True
                        return files, ("原图没下载到：" + why) if why else ""
                    return files, ""
            why = why or w
        inline = [self.path(mid, "-inline." + e) for e in ("jpg", "png")]
        for p in inline:
            if os.path.isfile(p):
                return {"image": p, "thumbnail_only": True}, "原图没下载到：" + (why or "消息里没有下载信息")
        return {}, "没能下载：" + (why or "消息里没有下载信息")

    def _voice(self, mid: str, m: dict) -> Tuple[dict, str]:
        out = self.path(mid, ".wav")
        silk = self.path(mid, ".silk")
        why = ""
        ok = False
        if os.path.isfile(silk):
            try:
                self._call("voice", 60, silk=silk, path=out)
                ok = True
            except Exception as e:  # noqa: BLE001
                why = reason(e)
        if not ok and m.get("length"):
            try:
                self._call("voice", 120, server_id=mid, length=int(m["length"]), path=out)
                ok = True
            except Exception as e:  # noqa: BLE001
                why = reason(e)
        if os.path.isfile(silk):
            try:
                os.remove(silk)
            except OSError:
                pass
        if not ok:
            return {}, "没能下载：" + (why or "消息里没有下载信息")
        files: Dict[str, Any] = {"audio": out}
        if not self.voice_cmd:
            return files, "没有转文字：设置里没有配语音转文字命令"
        text, why = self.transcribe(out)
        if text:
            files["transcript"] = text
            return files, ""
        return files, "转文字没成功：" + why

    def transcribe(self, wav: str) -> Tuple[str, str]:
        """The configured command (its words, then the WAV file's path) prints the words on stdout."""
        try:
            argv = shlex.split(self.voice_cmd) + [wav]
        except ValueError:
            return "", "语音转文字命令写得不对"
        try:
            # the same allow-listed environment as the WeChat program: the hub's token never reaches a local tool
            r = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               timeout=VOICE_TEXT_WAIT, check=False, env=clean_env())
        except (OSError, subprocess.SubprocessError) as e:
            return "", "命令运行不了（%s）" % type(e).__name__
        text = r.stdout.decode("utf-8", "replace").strip()
        if r.returncode != 0 or not text:
            return "", "命令没有给出文字（退出码 %d）" % r.returncode
        return text[:4000], ""

    def _video(self, mid: str, m: dict) -> Tuple[dict, str]:
        files: Dict[str, Any] = {}
        notes: List[str] = []
        key = m.get("aes_key")
        if m.get("thumb"):
            raw = self.path(mid, ".download")
            ok, w = self._cdn("thumbnail", m.get("thumb"), m.get("thumb_aes_key") or key, raw)
            if ok:
                pic, w = self._picture(raw, mid, "-cover")
                if pic:
                    files["cover"] = pic
            if not files.get("cover"):
                notes.append("封面没下载到：" + w)
        size = int(m.get("size") or 0)
        if size > MOST:
            notes.append("视频 %s，超过 %d MB 上限，没有下载" % (human_size(size), MOST >> 20))
        elif m.get("url"):
            out = self.path(mid, ".mp4")
            ok, w = self._cdn("video", m.get("url"), key, out, size or None)
            if ok:
                files["video"] = out
                frames, fw = self.frames(mid, out, int(m.get("seconds") or 0))
                if frames:
                    files["frames"] = frames
                elif fw:
                    notes.append(fw)
            else:
                notes.append("视频没下载到：" + w)
        else:
            notes.append("消息里没有视频的下载信息")
        return files, "；".join(notes)

    def frames(self, mid: str, video: str, seconds: int) -> Tuple[List[str], str]:
        """Up to three stills from the video (at 10 %, 50 %, 90 %) with ffmpeg, when it is installed."""
        ff = find_ffmpeg()
        if not ff:
            return [], "没有装 ffmpeg，没抽截图"
        dur = seconds if seconds > 0 else 3
        out = []
        for i, at in enumerate((dur * 0.1, dur * 0.5, dur * 0.9), 1):
            path = self.path(mid, "-frame%d.jpg" % i)
            try:
                subprocess.run([ff, "-nostdin", "-v", "error", "-y", "-ss", "%.2f" % at, "-i", video, "-frames:v", "1",
                                "-vf", "scale='min(1280,iw)':-2", path], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=FRAME_WAIT, check=False,
                               env=clean_env())
            except (OSError, subprocess.SubprocessError):
                continue
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                out.append(path)
        return out, "" if out else "截图没抽出来"

    def _file(self, mid: str, m: dict) -> Tuple[dict, str]:
        size = int(m.get("size") or 0)
        if size > MOST:
            return {}, "文件 %s，超过 %d MB 上限，没有下载" % (human_size(size), MOST >> 20)
        name = safe_name(str(m.get("name") or ""), "file" + ("." + str(m["ext"]) if m.get("ext") else ""))
        folder = self.path(mid, "")
        os.makedirs(folder, exist_ok=True)
        out = os.path.join(folder, name)
        ok, w = self._cdn("file", m.get("url"), m.get("aes_key"), out, size or None, m.get("md5"))
        if not ok:
            try:
                os.rmdir(folder)
            except OSError:
                pass
            return {}, "没能下载：" + w
        return {"file": out}, ""

    # ---- local copies age out

    def _remove(self, mid: str, files: Any) -> None:
        """This message's local copies off the disk (only under root)."""
        paths = files if isinstance(files, dict) else {}
        for k in ("image", "audio", "cover", "video", "file"):
            p = paths.get(k)
            if isinstance(p, str) and p.startswith(self.root + os.sep) and os.path.isfile(p):
                os.remove(p)
        for p in paths.get("frames") or []:
            if isinstance(p, str) and p.startswith(self.root + os.sep) and os.path.isfile(p):
                os.remove(p)
        folder = self.path(mid, "")
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
        for leftover in (self.path(mid, "-inline.jpg"), self.path(mid, "-inline.png")):
            if os.path.isfile(leftover):
                os.remove(leftover)

    def _drop(self, rows: List[Tuple[str, str]]) -> List[str]:
        gone = []
        for mid, files in rows:
            try:
                paths = json.loads(files or "{}")
            except ValueError:
                paths = {}
            self._remove(mid, paths)
            self._set(mid, "gone", {}, "")
            gone.append(mid)
        return gone

    def prune(self, now: Optional[float] = None) -> List[str]:
        """Remove local copies older than MEDIA_KEEP_DAYS; the ids whose text changes."""
        cutoff = (now or time.time()) - MEDIA_KEEP_DAYS * 86400
        return self._drop(self._db("SELECT id, files FROM media WHERE state='done' AND at < ?", (cutoff,), True))

    def total(self) -> int:
        n = 0
        for top, _, names in os.walk(self.root):
            for name in names:
                try:
                    n += os.path.getsize(os.path.join(top, name))
                except OSError:
                    pass
        return n

    def make_room(self, most: int = 0) -> List[str]:
        """Beyond MEDIA_TOTAL_MOST on the disk (measured every ROOM_EVERY): the oldest copies go until it fits."""
        most = most or MEDIA_TOTAL_MOST
        if time.time() - self._room_at < ROOM_EVERY:
            return []
        self._room_at = time.time()
        over = self.total() - most
        if over <= 0:
            return []
        gone: List[str] = []
        for mid, files in self._db("SELECT id, files FROM media WHERE state='done' ORDER BY at", (), True):
            if over <= 0:
                break
            try:
                paths = json.loads(files or "{}")
            except ValueError:
                paths = {}
            size = sum(os.path.getsize(p) for p in paths.values()
                       if isinstance(p, str) and p.startswith(self.root + os.sep) and os.path.isfile(p))
            gone += self._drop([(mid, files)])
            over -= size
        if gone:
            log.info("WeChat media: %d old local copies removed to stay under %d MB", len(gone), most >> 20)
        return gone


def reason(e: Exception) -> str:
    """Why a program request failed, in words (its message, or what an old program means)."""
    kind = getattr(e, "kind", "")
    if kind == "unsupported":
        return "微信组件版本太旧，还不能下载媒体（要 matewechat-native 0.1.10 或更新）"
    msg = getattr(e, "message", "") or str(e)
    return msg or type(e).__name__
