"""Small helpers for times and text shown to people or models."""

from __future__ import annotations

import datetime as _dt
import re
from typing import Optional

try:  # Python 3.9+: zoneinfo; tzdata may be missing on a minimal system
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

_UTC = _dt.timezone.utc


def rfc3339(ts: float) -> str:
    """Epoch seconds as RFC 3339 in UTC, whole seconds ("2026-11-15T08:30:00Z")."""
    if not ts:
        return ""
    return _dt.datetime.fromtimestamp(int(ts), _UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def zone(name: str):
    """The tzinfo for an IANA name; the machine's local zone when empty or unknown."""
    if name and ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return None


def local_stamp(ts: float, tz=None) -> str:
    """"MM-DD HH:MM" in tz (local time when None)."""
    if not ts:
        return ""
    d = _dt.datetime.fromtimestamp(ts, tz) if tz is not None else _dt.datetime.fromtimestamp(ts)
    return d.strftime("%m-%d %H:%M")


_SPACES = re.compile(r"\s+")


def one_line(text: str, most: int = 120) -> str:
    """text on one line, cut to most characters with an ellipsis."""
    s = _SPACES.sub(" ", text or "").strip()
    if len(s) > most:
        s = s[: max(1, most - 1)].rstrip() + "…"
    return s


def clip(text: str, most: int) -> str:
    """text cut to most characters (kept as it is, newlines included)."""
    text = text or ""
    if len(text) <= most:
        return text
    return text[: max(1, most - 1)] + "…"


_PLACEHOLDERS = {
    "IMAGE": "[图片]",
    "STICKER": "[表情]",
    "AUDIO": "[语音]",
    "VOICE": "[语音]",
    "VIDEO": "[视频]",
    "MEDIA": "[视频]",
    "FILE": "[文件]",
    "CARD": "[卡片]",
    "LOCATION": "[位置]",
    "SHARE_CHAT": "[群名片]",
    "SHARE_USER": "[个人名片]",
}


def display_text(msg_type: str, text: str, media: Optional[list] = None) -> str:
    """What a message says, for people and models: its text, else a placeholder for what it carries."""
    if text and text.strip():
        return text
    t = (msg_type or "").upper()
    if t == "FILE" and media:
        name = (media[0] or {}).get("name") or ""
        return "[文件] " + name if name else "[文件]"
    if t in _PLACEHOLDERS:
        return _PLACEHOLDERS[t]
    if media:
        kind = (media[0] or {}).get("kind") or ""
        return {"image": "[图片]", "sticker": "[表情]", "video": "[视频]", "voice": "[语音]", "file": "[文件]"}.get(kind, "[附件]")
    return ""


_LEADING_MENTIONS = re.compile(r"^(?:\s*@\S+)+\s*")


def strip_leading_mentions(text: str) -> str:
    """text without the @mentions it starts with ("@张三 @李四 /agent …" → "/agent …")."""
    return _LEADING_MENTIONS.sub("", text or "", count=1).lstrip()
