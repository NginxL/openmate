"""What the store's rows become on the wire: inbox things, thread pages, archive batches, and chat log lines for
models. Pure functions of rows; the app supplies the store and its settings."""

from __future__ import annotations

from typing import Dict, List, Optional

from .textutil import display_text, local_stamp, one_line, rfc3339

SYSTEM = "SYSTEM"
LABELS = {"dm": "私聊", "mention": "@我", "group": "群消息"}


def is_system(row: dict) -> bool:
    return (row.get("type") or "").upper() == SYSTEM


def text_of(row: dict) -> str:
    return display_text(row.get("type") or "", row.get("text") or "", row.get("media") or [])


def has_content(row: dict) -> bool:
    return bool((row.get("text") or "").strip() or row.get("media"))


def sender_of(row: dict, me: str) -> str:
    if row.get("mine"):
        return me or "我"
    return row.get("sender_name") or row.get("sender_id") or "未知"


def chat_name(chat: Optional[dict], chat_id: str) -> str:
    if chat and chat.get("name"):
        return chat["name"]
    return chat_id


def chat_kind(chat: Optional[dict]) -> str:
    return "dm" if chat and chat.get("kind") == "dm" else "group"


def log_line(row: dict, tz, me: str = "我", mark: str = "") -> str:
    """"MM-DD HH:MM name: text" (mine as me), for triage context and /agent context."""
    who = "我" if row.get("mine") else sender_of(row, me)
    text = text_of(row).replace("\r", "")
    line = "%s %s: %s" % (local_stamp(row.get("at") or 0, tz), who, text)
    return line + mark


def where(platform: str, chat: Optional[dict], chat_id: str) -> str:
    name = chat_name(chat, chat_id)
    if chat_kind(chat) == "dm":
        return "%s私聊「%s」" % (platform, name)
    members = (chat or {}).get("members") or 0
    return "%s群「%s」%s" % (platform, name, "（%d 人）" % members if members else "")


def reply_target(platform: str, chat: Optional[dict], chat_id: str) -> str:
    name = chat_name(chat, chat_id)
    if chat_kind(chat) == "dm":
        return "以你的身份发到%s私聊「%s」（对方能看到）" % (platform, name)
    return "以你的身份发到%s群「%s」（群里的人都能看到）" % (platform, name)


def unanswered_run(rows: List[dict]) -> List[dict]:
    """The trailing run of other people's messages (after my last one)."""
    i = len(rows)
    while i > 0 and not rows[i - 1].get("mine"):
        i -= 1
    return rows[i:]


def inbox_thing(chat: dict, rows: List[dict], *, platform: str, tz, me: str, rules: str, can_reply: bool,
                context_lines: int = 8) -> Optional[dict]:
    """One thing for a chat from its recent rows (oldest first), or None when there is nothing to show."""
    msgs = [r for r in rows if not is_system(r) and has_content(r)]
    if not msgs:
        return None
    chat_id = chat["id"]
    kind_dm = chat_kind(chat) == "dm"
    run = unanswered_run(msgs)
    done = ""
    if run:
        latest = run[-1]
        considered = run
    else:
        # I spoke last: the thread is over, if anyone had said anything before me.
        mine_from = len(msgs)
        while mine_from > 0 and msgs[mine_from - 1].get("mine"):
            mine_from -= 1
        considered = unanswered_run(msgs[:mine_from])
        if not considered:
            return None
        latest = msgs[-1]
        done = "replied"
    if kind_dm:
        kind = "dm"
    elif any(r.get("at_me") for r in considered):
        kind = "mention"
    else:
        kind = "group"
    name = chat_name(chat, chat_id)
    lines = [where(platform, chat, chat_id)]
    lines += [log_line(r, tz, me) for r in msgs[-context_lines:]]
    thing = {
        "thread": chat_id,
        "kind": kind,
        "label": LABELS[kind],
        "title": name,
        "sender": sender_of(latest, me),
        "preview": one_line(text_of(latest), 200),
        "context": "\n".join(lines),
        "at": rfc3339(latest.get("at") or 0),
        "last": latest["id"],
    }
    if can_reply:
        thing["reply_to"] = reply_target(platform, chat, chat_id)
        thing["can_reply"] = True
    if kind == "group":
        thing["tier"] = "noise"
        thing["why"] = "规则：群消息没有 @我，直接归为噪音（不调用模型）"
    if done:
        thing["done"] = done
        thing["why"] = "我已经回复"
    tags = [{"key": "chat", "value": chat_id, "name": name}]
    last_other = considered[-1]
    if last_other.get("sender_id"):
        tags.append({"key": "sender", "value": last_other["sender_id"], "name": sender_of(last_other, me)})
    thing["tags"] = tags
    if rules:
        thing["rules"] = rules[:4000]
    return thing


def media_list(row: dict) -> List[dict]:
    out = []
    for m in row.get("media") or []:
        kind = m.get("kind") or "file"
        if kind not in ("image", "sticker", "video", "voice", "file"):
            kind = "file"
        d = {"kind": kind}
        for k in ("ref", "mime", "name", "size", "width", "height"):
            if m.get(k):
                d[k] = m[k]
        out.append(d)
    return out


def thread_message(row: dict, me: str, parent: Optional[dict]) -> dict:
    event = is_system(row)
    out = {
        "id": row["id"],
        "at": rfc3339(row.get("at") or 0),
        "sender": {"name": "系统" if event else sender_of(row, me)},
    }
    if row.get("sender_id") and not event:
        out["sender"]["id"] = row["sender_id"]
    if row.get("mine"):
        out["mine"] = True
    if event:
        out["event"] = True
    text = row.get("text") or ""
    media = media_list(row)
    if not text.strip() and not media:
        text = text_of(row)
    if text:
        out["text"] = text
    if media:
        out["media"] = media
    if row.get("parent_id"):
        quote = {"id": row["parent_id"]}
        if parent:
            quote["from"] = sender_of(parent, me)
            quote["text"] = one_line(text_of(parent), 120)
        out["reply_to"] = quote
    return out


def archive_message(row: dict, me: str, handle_kind: str) -> dict:
    out = {
        "id": row["id"],
        "position": str(row["position"]),
        "type": (row.get("type") or "TEXT").upper(),
        "text": row.get("text") or "",
        "at": rfc3339(row.get("at") or 0),
    }
    if not out["text"].strip():
        out["text"] = text_of(row)
    if row.get("sender_id"):
        out["sender_id"] = row["sender_id"]
    name = sender_of(row, me)
    if name:
        out["sender_name"] = name
    if row.get("mine"):
        out["mine"] = True
    if row.get("at_me"):
        out["mentioned"] = True
    if row.get("at_all"):
        out["mentioned_all"] = True
    refs = [m.get("ref") for m in row.get("media") or [] if m.get("ref")]
    if refs:
        out["media"] = refs
    if row.get("sender_id") and handle_kind:
        out["sender_handles"] = [{"kind": handle_kind, "value": row["sender_id"]}]
    return out


def archive_chat(chat: Optional[dict], chat_id: str) -> Dict:
    out = {"id": chat_id, "name": chat_name(chat, chat_id), "kind": chat_kind(chat)}
    if chat:
        if chat.get("topic"):
            out["topic"] = True
        if chat.get("muted"):
            out["muted"] = True
        if chat.get("last_at"):
            out["updated_at"] = rfc3339(chat["last_at"])
    return out
