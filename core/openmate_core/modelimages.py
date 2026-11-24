"""Pictures a read tool hands the model with its text (tools.call's images, docs/protocol/connector-protocol.md
"Pictures in a tool's answer", #8484): only on a hub whose initialize lists the capability tool_images.

The hub takes a whole JPEG, PNG or WebP of at most 1 MiB and 2000 pixels a side, at most 16 of them, 4 MiB and 8
million pixels in all, and leaves out the rest. A picture is sent as it is when it is within those; otherwise it is
drawn again smaller as a JPEG (Pillow, in the interpreter that runs the extension; else macOS's sips), and left out
when neither can (the text still says where the file is). Each one sent is checked here first, by its own bytes."""

from __future__ import annotations

import base64
import io
import os
import shutil
import struct
import subprocess
import tempfile
from typing import List, Optional, Tuple

CAPABILITY = "tool_images"
ONE_MOST = 1 << 20  # the hub's limits
SIDE_MOST = 2000
COUNT_MOST = 16
TOTAL_MOST = 4 << 20
PIXELS_MOST = 8_000_000
SIDE_DRAWN = 1568  # a picture drawn again: what Claude looks at without scaling it down itself (2.5 million pixels)
TYPES = ("image/jpeg", "image/png", "image/webp")


def kind_of(data: bytes) -> str:
    """The type of a JPEG, PNG or WebP by its bytes; "" for anything else."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def size_of(data: bytes) -> Optional[Tuple[int, int]]:
    """(width, height) read from a PNG's, JPEG's or WebP's header; None when it cannot be read."""
    try:
        kind = kind_of(data)
        if kind == "image/png" and data[12:16] == b"IHDR":
            w, h = struct.unpack(">II", data[16:24])
            return (w, h) if w and h else None
        if kind == "image/jpeg":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    return None
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                length = struct.unpack(">H", data[i + 2:i + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return (w, h) if w and h else None
                i += 2 + length
            return None
        if kind == "image/webp":
            chunk = data[12:16]
            if chunk == b"VP8X":
                w = 1 + int.from_bytes(data[24:27], "little")
                h = 1 + int.from_bytes(data[27:30], "little")
                return w, h
            if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
                w, h = struct.unpack("<HH", data[26:30])
                return (w & 0x3FFF, h & 0x3FFF)
            if chunk == b"VP8L" and data[20] == 0x2F:
                b = data[21:25]
                w = 1 + (((b[1] & 0x3F) << 8) | b[0])
                h = 1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
                return w, h
    except (struct.error, IndexError):
        return None
    return None


def _within(data: bytes) -> Optional[Tuple[str, Tuple[int, int]]]:
    kind = kind_of(data)
    size = size_of(data) if kind else None
    if not kind or size is None or len(data) > ONE_MOST or max(size) > SIDE_MOST:
        return None
    return kind, size


def _pillow(data: bytes, side: int) -> Optional[bytes]:
    try:
        from PIL import Image  # the interpreter's own, when it has it (LarkAgentX's has)
    except Exception:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.seek(0)  # an animated sticker: its first frame
            im.draft("RGB", (side, side))  # a big JPEG: decoded at a fraction of its size to begin with
            frame = im.convert("RGBA") if im.mode in ("RGBA", "LA", "P", "PA") else im.convert("RGB")
            frame.thumbnail((side, side))
            if frame.mode == "RGBA":  # a transparent picture on white, as a chat shows it
                back = Image.new("RGB", frame.size, (255, 255, 255))
                back.paste(frame, mask=frame.split()[3])
                frame = back
            out = io.BytesIO()
            frame.save(out, "JPEG", quality=85, optimize=True)
            return out.getvalue()
    except Exception:
        return None


def _sips(data: bytes, side: int) -> Optional[bytes]:
    tool = shutil.which("sips") or ("/usr/bin/sips" if os.path.exists("/usr/bin/sips") else "")
    if not tool:
        return None
    with tempfile.TemporaryDirectory(prefix="openmate-picture-") as d:
        src, dst = os.path.join(d, "in"), os.path.join(d, "out.jpg")
        with open(src, "wb") as f:
            f.write(data)
        try:
            subprocess.run([tool, "-s", "format", "jpeg", "-s", "formatOptions", "80", "-Z", str(side), src,
                            "--out", dst], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30, check=True)
            with open(dst, "rb") as f:
                return f.read(ONE_MOST + 1)
        except (OSError, subprocess.SubprocessError):
            return None


def fit(data: bytes) -> Optional[Tuple[str, bytes, int]]:
    """(mime, bytes, pixels) of the picture the hub takes: data itself when it is within the limits, else drawn
    again smaller; None when it cannot be (not a picture these tools read, or no way to draw it here)."""
    data = bytes(data or b"")
    got = _within(data)
    if got is not None:
        return got[0], data, got[1][0] * got[1][1]
    for side in (SIDE_DRAWN, 1024, 768):
        for draw in (_pillow, _sips):
            out = draw(data, side)
            got = _within(out) if out else None
            if got is not None:
                return got[0], out, got[1][0] * got[1][1]
    return None


class Pictures:
    """The pictures of one tool answer, within the hub's totals: add() says whether one was taken."""

    def __init__(self):
        self.images: List[dict] = []
        self._bytes = 0
        self._pixels = 0

    def add(self, data: bytes, caption: str) -> str:
        """"" when the picture is in the answer; else why it is not (a few words for the text)."""
        if len(self.images) >= COUNT_MOST:
            return "这次结果里的图片已经够 %d 张了" % COUNT_MOST
        got = fit(data)
        if got is None:
            return "这张图片没法转成模型能看的格式"
        mime, body, pixels = got
        if self._bytes + len(body) > TOTAL_MOST or self._pixels + pixels > PIXELS_MOST:
            return "这次结果里的图片已经够多了"
        self._bytes += len(body)
        self._pixels += pixels
        self.images.append({"mime": mime, "data": base64.b64encode(body).decode("ascii"),
                            "caption": " ".join(str(caption or "").split())[:200]})
        return ""
