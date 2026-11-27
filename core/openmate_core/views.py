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


def log_line(row: dict, tz, me: str = "我", mark: str = "", media_tool: str = "") -> str:
    """"MM-DD HH:MM name: text" (mine as me), for triage context and /agent context. With media_tool, a message with
    a picture or file that can be fetched says how to see it ("[图片]（看图：matefeishu_media message=…）": the tool's
    own argument)."""
    who = "我" if row.get("mine") else sender_of(row, me)
    text = text_of(row).replace("\r", "")
    line = "%s %s: %s" % (local_stamp(row.get("at") or 0, tz), who, text)
    return line + media_hint(row, media_tool) + mark


_MEDIA_VERBS = {"image": "看图", "sticker": "看图", "video": "下载视频", "voice": "下载语音", "file": "下载文件"}


def media_hint(row: dict, media_tool: str) -> str:
    """"（看图：<tool> message=…）" for a message whose picture or file the tool can fetch; "" otherwise."""
    if not media_tool or row.get("blocked"):
        return ""
    kinds = [str(m.get("kind") or "") for m in row.get("media") or [] if isinstance(m, dict) and m.get("ref")]
    if not kinds:
        return ""
    verb = _MEDIA_VERBS.get(kinds[0], "下载附件") if len(set(kinds)) == 1 else "看图片和文件"
    return "（%s：%s message=%s）" % (verb, media_tool, row.get("id") or "")


def where(platform: str, chat: Optional[dict], chat_id: str) -> str:
    name = chat_name(chat, chat_id)
    if chat_kind(chat) == "dm":
        return "%s私聊「%s」" % (platform, name)
    members = (chat or {}).get("members") or 0
    return "%s群「%s」%s" % (platform, name, "（%d 人）" % members if members else "")


def reply_target(platform: str, chat: Optional[dict], chat_id: str, topic: bool = False) -> str:
    name = chat_name(chat, chat_id)
    if chat_kind(chat) == "dm":
        return "以你的身份发到%s私聊「%s」（对方能看到）" % (platform, name)
    if topic:
        return "以你的身份回复到%s群「%s」的这个话题（群里的人都能看到）" % (platform, name)
    return "以你的身份发到%s群「%s」（群里的人都能看到）" % (platform, name)


def unanswered_run(rows: List[dict]) -> List[dict]:
    """The trailing run of other people's messages (after my last one)."""
    i = len(rows)
    while i > 0 and not rows[i - 1].get("mine"):
        i -= 1
    return rows[i:]


PRIVATE_PREVIEW = "（这个私聊设置为不交给收件箱 AI：内容不在这里，打开会话查看）"


def inbox_thing(chat: dict, rows: List[dict], *, platform: str, tz, me: str, rules: str, can_reply: bool,
                context_lines: int = 8, root: str = "", lead: Optional[dict] = None, mute: str = "",
                triage: bool = False, private: bool = False) -> Optional[dict]:
    """One thing for a chat from its recent rows (oldest first), or None when there is nothing to show.

    root: the rows are one topic's of a group (inbox_things): the thing is that topic's ("<chat>:<root>"), its context,
    done state and reply that topic's; lead: the topic's root message when it is the chat's own item's (an ordinary
    group: the root was said in the main stream), shown first in the context and not counted as the topic's.
    mute: the group's mute state ("muted", "normal", "unknown" or "": not asked). A muted group's thing that does not
    @ the user is handed over as already done (read: Persona's own muted items are), and in one that does, what came
    after the user's @ is not what it is about. triage: the user listed the group for AI triage (setting
    inbox_triage_chats): its messages without an @ go to the triage model too, muted or not. private: the user listed
    the direct chat as one not handed to the Inbox's AI (setting inbox_private_dms): its tier is the rule's and no
    words of it are in the thing (preview, context): no model reads them."""
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
    muted = mute == "muted" and not triage and not kind_dm and not private
    if muted and kind == "group" and not done:
        done = "read"  # muted in the service: nothing to do (still archived)
    elif muted and kind == "mention" and not done:
        # muted: the @ of the user is the thing, not the chatter after it
        latest = [r for r in considered if r.get("at_me")][-1]
    name = chat_name(chat, chat_id)
    head = where(platform, chat, chat_id)
    if root:
        head += "的一个话题（根消息 id=%s）" % root
    hidden = private  # (listed by its id: whatever kind it was kept as)
    lines = [head]
    if hidden:
        lines.append(PRIVATE_PREVIEW)
    else:
        if lead is not None and has_content(lead) and not is_system(lead):
            lines.append(log_line(lead, tz, me) + "（话题的根消息）")
        upto = msgs.index(latest) + 1 if latest in msgs else len(msgs)
        lines += [log_line(r, tz, me) for r in msgs[:upto][-context_lines:]]
    thing = {
        "thread": "%s:%s" % (chat_id, root) if root else chat_id,
        "kind": kind,
        "label": LABELS[kind],
        "title": name,
        "sender": sender_of(latest, me),
        "preview": PRIVATE_PREVIEW if hidden else one_line(text_of(latest), 200),
        "context": "\n".join(lines),
        "at": rfc3339(latest.get("at") or 0),
        "last": latest["id"],
    }
    if can_reply:
        thing["reply_to"] = reply_target(platform, chat, chat_id, topic=bool(root))
        thing["can_reply"] = True
    if hidden:
        thing["tier"] = "action"
        thing["why"] = "规则：这个私聊设置为不交给收件箱 AI（不调用模型）"
    elif kind == "group":
        if triage:
            # sorted by the model with the user's rules; the rule's tier only where the model cannot be asked
            thing["extra"] = {"rule_tier": "noise"}
        else:
            thing["tier"] = "noise"
            thing["why"] = "规则：群消息没有 @我，直接归为噪音（不调用模型）"
    if done == "read":
        thing["done"] = "read"
        thing["why"] = "规则：这个群在飞书里设了消息免打扰，没有 @我"
    elif done:
        thing["done"] = done
        thing["why"] = "我已经回复"
    tags = [{"key": "chat", "value": chat_id, "name": name}]
    last_other = latest if not latest.get("mine") else considered[-1]  # (a muted group's @: its sender)
    if last_other.get("sender_id"):
        tags.append({"key": "sender", "value": last_other["sender_id"], "name": sender_of(last_other, me)})
    thing["tags"] = tags
    if rules:
        thing["rules"] = rules[:4000]
    return thing


def _place(r: dict) -> tuple:
    """A row's place in its chat: its position, then when it was said (a thread's replies may share a position), then
    when it was kept."""
    return (int(r.get("position") or 0), float(r.get("at") or 0), int(r.get("seq") or 0))


def inbox_things(chat: dict, rows: List[dict], *, topics: bool = False, since: float = 0.0, until: float = 0.0,
                 **kw) -> List[dict]:
    """The chat's things from its recent rows (oldest first): one for the chat (inbox_thing), or, with topics, in a
    group one for each topic its rows are in besides the chat's own.

    A group in topic mode (chat.topic: every message opens a topic or replies in one) has a thing per topic, its root
    and replies. In an ordinary group a topic's thing is its replies (the root above them, for context): the root was
    said in the main stream, so it stays the chat's own thing's, and the user's own replies in a topic whose root is
    there end that thing as well (it asked, the user answered where it was asked). A topic whose last line is older
    than since is left out. until: when topics became items of their own; a topic group's lines said before it were
    its one item, which is handed over done (with those lines only, so it stays that one item)."""
    if not topics or chat_kind(chat) == "dm":
        thing = inbox_thing(chat, rows, **kw)
        return [thing] if thing is not None else []
    rows = sorted(rows, key=_place)
    topic_mode = bool(chat.get("topic"))
    scopes: Dict[str, List[dict]] = {}
    for r in rows:
        key = str(r.get("root_id") or "") or (str(r.get("id") or "") if topic_mode else "")
        scopes.setdefault(key, []).append(r)
    out = []
    main = scopes.pop("", [])
    if not topic_mode:
        ids = {str(r.get("id") or "") for r in main}
        answered = [r for key, part in scopes.items() if key in ids for r in part if r.get("mine")]
        mine_too = sorted(main + answered, key=_place)
        thing = inbox_thing(chat, mine_too, **kw)
        if thing is not None:
            out.append(thing)
    else:
        before = [r for r in rows if float(r.get("at") or 0) <= until] if until else []
        old = inbox_thing(chat, before, **kw) if before and not main else None
        if old is not None and not old.get("done"):
            old["done"], old["why"] = "read", "规则：这个群的消息现在按话题各成一条"
            out.append(old)  # the one item a version before this one kept for the whole group: ended
        if main:  # (a topic group's line outside any topic: the chat's own)
            thing = inbox_thing(chat, main, **kw)
            if thing is not None:
                out.append(thing)
    by_id = {str(r.get("id") or ""): r for r in rows}
    for root, part in scopes.items():
        if not part or max(float(r.get("at") or 0) for r in part) < since:
            continue
        lead = None
        if not topic_mode:
            part = [r for r in part if str(r.get("id") or "") != root]
            lead = by_id.get(root)
            if not part:
                continue
        thing = inbox_thing(chat, part, root=root, lead=lead, **kw)
        if thing is not None:
            out.append(thing)
    return out


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
    # a sticker's picture is not offered: the line keeps "[表情]", and the knowledge base downloads no emoji (most of
    # the bytes a chat archive held, and nothing anyone looks up)
    refs = [m.get("ref") for m in row.get("media") or [] if m.get("ref") and m.get("kind") != "sticker"]
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
