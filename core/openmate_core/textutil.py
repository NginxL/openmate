"""Small helpers for times and text shown to people or models."""

from __future__ import annotations

import datetime as _dt
import re
from typing import List, Optional, Sequence, Tuple

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


def mention_pieces(text: str, mentions: Sequence[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """The text as pieces ("text", words) and ("at", user id) in order. A mention [(user id, name)] goes where the text
    first writes its "@name" that no other mention took: at each place the longest name written there wins (so
    "@Annette" is Annette's even when Ann is @-ed too). A mention the text does not write goes in front, in the order
    given, a space between them and after the last."""
    spots: List[Tuple[int, int, str]] = []  # (start, end, user id) of each mention written in the text
    taken = set()
    for uid, name in sorted(mentions, key=lambda m: -len(m[1] or "")):  # longest names first
        if not name or uid in taken:
            continue
        word = "@" + name
        start = 0
        while True:
            at = text.find(word, start)
            if at < 0:
                break
            if not any(a < at + len(word) and at < b for a, b, _ in spots):
                spots.append((at, at + len(word), uid))
                taken.add(uid)
                break
            start = at + 1
    front = [uid for uid, _ in mentions if uid not in taken]
    out: List[Tuple[str, str]] = []
    for i, uid in enumerate(front):
        if i:
            out.append(("text", " "))
        out.append(("at", uid))
    if front and text and not text[0].isspace():
        out.append(("text", " "))
    pos = 0
    for a, b, uid in sorted(spots):
        if a > pos:
            out.append(("text", text[pos:a]))
        out.append(("at", uid))
        pos = b
    if pos < len(text):
        out.append(("text", text[pos:]))
    return out


def with_mention_names(text: str, mentions: Sequence[Tuple[str, str]]) -> str:
    """The text as it will read once its mentions are @s: those it does not write go in front as "@name" (what the
    confirm card shows, what is sent and what the echo of it is known by)."""
    names = dict(mentions)
    return "".join("@" + (names.get(v) or v) if k == "at" else v for k, v in mention_pieces(text, mentions))
