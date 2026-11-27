"""matefeishu_members: who is in a Feishu chat, the people and the bots, each with the id and the exact display name a
real @ takes (the send tools' `at`, the /agent reply's `at`). Read-only; the backend keeps a chat's list for a few
minutes per sign-in (LarkXBackend.chat_members), and a sign-in or logout forgets every list."""

from __future__ import annotations

from typing import Dict, List, Optional

MEMBERS_TOOL = "matefeishu_members"
SHOWN_MOST = 500  # members one call lists at most (the backend reads at most this many)
SHOWN_DEFAULT = 200


class MembersError(Exception):
    """Why the tool has no list to show: told to the model as the call's error."""


def members_tools(app) -> Dict:
    """{matefeishu_members: (schema, run)} for Platform.tools."""
    schema = {
        "name": MEMBERS_TOOL,
        "description": "列出一个飞书群（或私聊）里的人和机器人：每个的用户 id、在这个群里显示的准确名字、是人还是机器人。要真正 @ 某人或某个"
                       "机器人时，用这里的 id 和名字（matefeishu_send 的 at、matefeishu_agent_reply 的 at），不要只在正文里打「@名字」，"
                       "那只是普通文字。只读，不发消息；列表会缓存几分钟。",
        "input_schema": {"type": "object", "properties": {
            "chat": {"type": "string", "description": "会话 id（matefeishu_chats、matefeishu_search 或 /agent 上下文里的 chat=…）"},
            "query": {"type": "string", "description": "只列名字里含这个词的，可选"},
            "limit": {"type": "integer", "minimum": 1, "maximum": SHOWN_MOST, "description": "最多列几个，默认 %d" % SHOWN_DEFAULT}},
            "required": ["chat"]},
    }

    def run(a: Dict):
        from openmate_core.wire import RPCError

        try:
            limit = int(a.get("limit") or SHOWN_DEFAULT)
        except (TypeError, ValueError):
            limit = SHOWN_DEFAULT
        try:
            return roster_text(app, str(a.get("chat") or "").strip(), str(a.get("query") or "").strip(),
                               max(1, min(SHOWN_MOST, limit)))
        except MembersError as e:
            raise RPCError(str(e))

    return {MEMBERS_TOOL: (schema, run)}


def roster(app, chat_id: str) -> List[dict]:
    """The chat's people and bots as the backend lists them ([{"id", "name", "real_name", "kind"}]); MembersError
    when there is no list to give."""
    from openmate_core.backend import BackendError

    if not chat_id:
        raise MembersError("缺少会话 id（chat）")
    backend = getattr(app, "backend", None)
    if backend is None or not callable(getattr(backend, "chat_members", None)):
        raise MembersError("飞书扩展还没准备好，请稍后再试")
    if not getattr(app, "signed_in", False):
        raise MembersError("还没有登录飞书")
    try:
        return backend.chat_members(chat_id)
    except BackendError as e:
        raise MembersError("没有读到群成员：%s" % e.message)


def roster_text(app, chat_id: str, query: str = "", limit: int = SHOWN_DEFAULT) -> str:
    people = roster(app, chat_id)
    me = getattr(getattr(app, "account", None), "id", "") or ""
    store = getattr(app, "store", None)
    chat: Optional[dict] = store.get_chat(chat_id) if store is not None else None
    name = (chat or {}).get("name") or chat_id
    shown = [p for p in people if not query or query.casefold() in (p["name"] + " " + p.get("real_name", "")).casefold()]
    bots = sum(1 for p in people if p.get("kind") == "bot")
    head = "飞书%s「%s」（chat=%s）：%d 个成员，其中机器人 %d 个" % (
        "私聊" if (chat or {}).get("kind") == "dm" else "群", name, chat_id, len(people), bots)
    if query:
        head += "；名字含「%s」的 %d 个" % (query, len(shown))
    lines = [head + "。真 @ 时用这里的 id 和名字："]
    for p in shown[:limit]:
        what = "机器人" if p.get("kind") == "bot" else ("我" if p["id"] == me else "成员")
        also = "，本名 %s" % p["real_name"] if p.get("real_name") and p["real_name"] != p["name"] else ""
        lines.append("- %s（%s，id=%s%s）" % (p["name"], what, p["id"], also))
    if len(shown) > limit:
        lines.append("（还有 %d 个没列出：用 query 按名字找）" % (len(shown) - limit))
    if not shown:
        lines.append("（没有符合的）")
    return "\n".join(lines)


def verify(app, chat_id: str, wanted: List[dict]) -> List[tuple]:
    """The (user id, name) pairs of wanted ([{"id", "name"}]) after checking each against the chat's list: the id is a
    member's, and the name is the one shown for them there (or their own name). MembersError naming the first that is
    not, so nothing is @-ed on a guess."""
    people = {p["id"]: p for p in roster(app, chat_id)}
    out: List[tuple] = []
    for w in wanted:
        uid = str((w or {}).get("id") or "").strip()
        name = str((w or {}).get("name") or "").strip().lstrip("@").strip()
        p = people.get(uid)
        if p is None:
            raise MembersError("id=%s 不在这个会话的成员里：先用 %s 查准确的 id" % (uid or "（空）", MEMBERS_TOOL))
        if name not in (p["name"], p.get("real_name") or p["name"]):
            raise MembersError("id=%s 在这个会话里显示为「%s」，不是「%s」：用 %s 给出的名字" % (uid, p["name"], name, MEMBERS_TOOL))
        if uid not in {u for u, _ in out}:
            out.append((uid, name))
    return out
