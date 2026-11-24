"""The connector's tools (reads, no gate) and actions (writes: only after the hub's gate, the phone's confirmation).

Names are <connector id>_<what>, so another platform's connector reuses this module with its own prefix.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from . import views
from .backend import BackendError
from .textutil import clip, local_stamp, mention_spans, one_line
from .wire import RPCError

log = logging.getLogger("openmate.tools")

TEXT_MOST = 8000  # characters in one message the actions send


def _obj(props: Dict[str, Any], required: Optional[List[str]] = None) -> dict:
    s: Dict[str, Any] = {"type": "object", "properties": props}
    if required:
        s["required"] = required
    return s


def read_tools(app) -> List[dict]:
    c, p = app.cid, app.platform.name
    tools = [
        {"name": c + "_status", "description": "%s连接器的状态：是否登录、实时连接是否正常、最近收到消息的时间、本地记录了多少会话。" % p,
         "input_schema": _obj({})},
        {"name": c + "_chats", "description": "最近活跃的%s会话（私聊和群），按最近消息排序，带会话 id、类型和还没回的消息数。可以按名字过滤。" % p,
         "input_schema": _obj({"query": {"type": "string", "description": "按会话名过滤，可选"},
                               "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "最多几个，默认 30"}})},
        {"name": c + "_messages", "description": "读一个%s会话的消息，从旧到新，每页最多 50 条；用返回的 before 往前翻。给 thread 只看一个话题。"
                                         "给 since / until 只看一段时间，给 query 只看含这个词的消息（内容或发送人），不够时会去%s补读更早的历史。" % (p, p),
         "input_schema": _obj({"chat": {"type": "string", "description": "会话 id（%s_chats 或 %s_search 给出）" % (c, c)},
                               "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "条数，默认 20"},
                               "before": {"type": "string", "description": "上一页返回的 before，往前翻时用"},
                               "thread": {"type": "string", "description": "话题根消息 id，可选"},
                               "since": {"type": "string", "description": "从这个时间起，如 2026-11-12 15:49 或 2026-11-12（我的时区），可选"},
                               "until": {"type": "string", "description": "到这个时间为止，格式同 since，只写日期时含这一整天，可选"},
                               "query": {"type": "string", "description": "关键词，可选；多个词用空格隔开，要全部出现"}}, ["chat"])},
        {"name": c + "_search", "description": "在%s里搜人和群，返回用户 id（给 %s_send_dm）或群的会话 id（给 %s_send、%s_messages）。" % (p, c, c, c),
         "input_schema": _obj({"query": {"type": "string", "description": "名字或关键词"},
                               "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "最多几个，默认 20"}}, ["query"])},
        _agent_reply_tool(app, c, p),
    ]
    if _has_docs(app):  # Feishu's place for it: before the /agent reply
        tools.insert(4, _DOCS_TOOL(c, p))
    return tools + [schema for schema, _ in _page_tools(app).values()]


def _agent_reply_tool(app, c: str, p: str) -> dict:
    """The /agent reply tool; progress (edited in place) only where the service can edit a message."""
    props = {"request": {"type": "string", "description": "这次 /agent 请求的 id（请求说明里给出）"},
             "text": {"type": "string", "description": "要回复的内容（纯文本）"}}
    if getattr(app.platform, "can_edit", True):
        props["progress"] = {"type": "boolean", "description": "true 表示这是中途进度：原地更新同一条进度消息，不另发新消息。最终结果不要带它"}
        what = "可以多次调用发进度，不要编造没做到的事。"
    else:
        what = "只回复一次最终结果（%s不能编辑消息，进度不发），不要编造没做到的事。" % p
    if getattr(app.backend, "can_send_images", False):
        props["image"] = {"type": "string", "description": "可选：随最终回复一起发的一张图片的绝对路径（PNG 或 JPG，放在 /tmp 下，最多 10 MB），比如流程图、表格。文字先发，图片紧跟着单独发出；进度消息、自动代答不能带图"}
        what += "要发图片就在同一次调用里带上 image（之后不能再单独补发）。"
    return {"name": c + "_agent_reply",
            "description": "只给 /agent 用：把处理结果以我的身份回复到发出 /agent 的那个%s话题。request 必须是这次请求给出的 id；" % p + what,
            "input_schema": _obj(props, ["request", "text"])}


def _has_docs(app) -> bool:
    """Only a platform with documents (doc_hosts: Feishu's) offers the lark-cli document search."""
    return bool(getattr(app.platform, "doc_hosts", ()))


def _DOCS_TOOL(c: str, p: str) -> dict:  # noqa: N802
    return {"name": c + "_docs", "description": "通过管理端的 lark-cli 只读集成搜%s文档（标题、链接、摘要）。管理端没开启这个集成时会说明。" % p,
            "input_schema": _obj({"query": {"type": "string", "description": "关键词"},
                                  "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "最多几个，默认 10"}}, ["query"])}


def _page_tools(app) -> dict:
    """The read tools the platform's pages add (UI.tools: the StarkMesh quota, say; none from an app without pages),
    and the platform's own (Platform.tools: WeChat's matewechat_media)."""
    ui = getattr(app, "_ui", None)
    out = dict(ui().tools()) if ui is not None else {}
    extra = getattr(getattr(app, "platform", None), "tools", None)
    if extra is not None:
        out.update(extra(app))
    return out


MENTIONS_MOST = 20  # people one message @-s at most
_USER_ID = re.compile(r"[A-Za-z0-9_.:@-]{1,64}")  # what a user id looks like on any platform (the backend checks its own)


def write_tools(app) -> List[dict]:
    c, p = app.cid, app.platform.name
    send = {"chat": {"type": "string", "description": "会话 id"},
            "text": {"type": "string", "description": "完整内容（纯文本）"},
            "reply_to": {"type": "string", "description": "要回复的消息 id，可选"}}
    what = "以我的身份在%s会话（私聊或群）里发一条消息，可以回复某条消息。每次都要我在手机上确认全文和发给谁。" % p
    if getattr(app.backend, "can_mention", False):
        send["at"] = {"type": "array", "items": {"type": "string"}, "maxItems": MENTIONS_MOST,
                      "description": "要 @ 的人的用户 id（%s_search 或消息里给出），可选。正文里写到的「@名字」会变成真正的 @，"
                                     "没写到的放在最前面；只在正文里手写 @名字 不会提醒到人" % c}
        what += "要提醒谁就把他放进 at。"
    if getattr(app.backend, "topic_groups", False):
        what += "话题群里不给 reply_to 会开一个新话题，给 reply_to 就回复到那条所在的话题；开了话题模式的群只能回复已有话题（要给 reply_to）。"
    return [
        {"name": c + "_send", "description": what, "input_schema": _obj(send, ["chat", "text"])},
        {"name": c + "_send_dm", "description": "以我的身份私聊一个%s用户（没有私聊会话时先建一个）。每次都要我在手机上确认全文和发给谁。" % p,
         "input_schema": _obj({"user": {"type": "string", "description": "对方的用户 id（%s_search 给出）" % c},
                               "text": {"type": "string", "description": "完整内容（纯文本）"}}, ["user", "text"])},
    ]


def _args(params: dict) -> dict:
    a = params.get("args")
    if a is None:
        return {}
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except ValueError:
            raise RPCError("参数不是 JSON 对象")
    if not isinstance(a, dict):
        raise RPCError("参数不是 JSON 对象")
    return a


def _need(a: dict, key: str, what: str) -> str:
    v = a.get(key)
    if not isinstance(v, str) or not v.strip():
        raise RPCError("缺少%s（%s）" % (what, key))
    return v.strip() if key != "text" else v


def _limit(a: dict, default: int, most: int) -> int:
    try:
        n = int(a.get("limit") or default)
    except (TypeError, ValueError):
        n = default
    return max(1, min(most, n))


def _require_store(app) -> None:
    if app.store is None:
        raise RPCError("扩展还在启动，请稍后再试")


# ---------------------------------------------------------------- reads


def call_tool(app, params: dict) -> dict:
    name = str(params.get("name") or "")
    a = _args(params)
    c = app.cid
    fn = {
        c + "_status": _status,
        c + "_chats": _chats,
        c + "_messages": _messages,
        c + "_search": _search,
        c + "_docs": _docs if _has_docs(app) else None,
        c + "_agent_reply": _agent_reply,
    }.get(name)
    if fn is not None:
        return {"text": fn(app, a)}
    page_tool = _page_tools(app).get(name)
    if page_tool is None:
        raise RPCError("没有这个工具：%s" % name)
    return {"text": page_tool[1](a)}


def _status(app, a: dict) -> str:
    st = app.m_status({})
    p = app.platform.name
    words = {"ok": "已连接", "not_signed_in": "没有登录", "not_configured": "还没设置好", "error": "有问题"}
    lines = ["%s：%s%s" % (app.manifest.get("name", p), words.get(st["state"], st["state"]),
                           "，账号 " + st["account"] if st.get("account") else "")]
    if st.get("error"):
        lines.append("原因：" + st["error"])
    if app.last_event:
        lines.append("最近收到消息：" + local_stamp(app.last_event, app.tz))
    if app.last_sync:
        lines.append("最近补收历史：" + local_stamp(app.last_sync, app.tz))
    if app.store is not None:
        lines.append("本地记录：%d 个会话，%d 条消息" % (len(app.store.chats_recent(100000)), app.store.message_count()))
    if app.agent is not None:
        lines.append("/agent：" + ("已开启（前缀 %s）" % app.agent.prefix if app.agent.enabled else "没有开启"))
    return "\n".join(lines)


def _chats(app, a: dict) -> str:
    _require_store(app)
    query = str(a.get("query") or "").strip()
    chats = app.store.chats_recent(_limit(a, 30, 100), query)
    if not chats:
        return "没有找到会话" + ("（过滤：%s）" % query if query else "")
    me = app.me_name()
    lines = ["最近的%s会话（按最近消息排序）：" % app.platform.name]
    for ch in chats:
        rows = [r for r in app.store.messages_before(ch["id"], None, 40) if not views.is_system(r) and views.has_content(r)]
        run = views.unanswered_run(rows)
        bits = [ch.get("name") or ch["id"], "私聊" if ch.get("kind") == "dm" else "群", "chat=" + ch["id"]]
        if ch.get("last_at"):
            bits.append("最近 " + local_stamp(ch["last_at"], app.tz))
        if run:
            bits.append("有 %d 条没回%s" % (len(run), "（@我）" if any(r.get("at_me") for r in run) else ""))
        elif rows:
            bits.append("最后一条是我发的")
        lines.append("- " + " · ".join(bits))
    return "\n".join(lines)


def _messages(app, a: dict) -> str:
    epoch = app.account_epoch()  # one account's store: another's sign-in while it reads (or asks the service) ends it
    out = _messages_read(app, a, epoch)
    if epoch != app.account_epoch():
        raise RPCError("刚换了%s账号，请重新读这个会话" % app.platform.name)
    return out


def _messages_read(app, a: dict, epoch: int) -> str:
    _require_store(app)
    chat_id = _need(a, "chat", "会话 id")
    limit = _limit(a, 20, 50)
    before = str(a.get("before") or "").strip()
    before_pos: Optional[int] = None
    if before:
        try:
            before_pos = int(before)
        except ValueError:
            raise RPCError("before 不对：用上一页返回的 before")
    chat = app.store.get_chat(chat_id)
    if chat is None and app.signed_in:
        try:
            chat = app.chat_from_service(chat_id, epoch)
        except BackendError:
            chat = None
    if chat is None:
        if epoch != app.account_epoch():
            raise RPCError("刚换了%s账号，请重新读这个会话" % app.platform.name)
        raise RPCError("没有这个%s会话：%s" % (app.platform.name, chat_id))
    thread = str(a.get("thread") or "").strip()
    since, until = _when(app, a.get("since"), False), _when(app, a.get("until"), True)
    words = [w.casefold() for w in str(a.get("query") or "").split()]
    filtered = since is not None or until is not None or bool(words)
    more = ""
    if filtered:
        rows, more, scanned = _scan(app, chat_id, thread, before_pos, since, until, words, limit, epoch)
    elif thread:
        rows = app.store.thread_messages(chat_id, thread, limit)
    else:
        rows = app.store.messages_before(chat_id, before_pos, limit)
        if len(rows) < limit:
            app.fill_older(chat_id, rows[0]["position"] if rows else before_pos, limit - len(rows), epoch)
            rows = app.store.messages_before(chat_id, before_pos, limit)
    me = app.me_name()
    head = "%s（%s，chat=%s）" % (views.chat_name(chat, chat_id), "私聊" if chat.get("kind") == "dm" else "群", chat_id)
    if filtered:
        head += "，" + _filter_words(app, since, until, a.get("query"))
        if not rows:
            return head + "：看了 %d 条，没有符合的消息%s" % (scanned, "；" + more if more else "")
        lines = [head + "：%d 条，从旧到新：" % len(rows)]
        lines += [_row_line(app, r, me) for r in rows]
        if more:
            lines.append(more)
        return "\n".join(lines)
    if not rows:
        return head + "：没有更多消息"
    lines = [head + "，%d 条，从旧到新：" % len(rows)]
    lines += [_row_line(app, r, me) for r in rows]
    if not thread and len(rows) >= limit:
        lines.append("更早的消息：before=%s" % rows[0]["position"])
    return "\n".join(lines)


def _row_line(app, r: dict, me: str) -> str:
    extra = ""
    if r.get("parent_id"):
        extra += " 回复=" + r["parent_id"]
    if r.get("root_id"):
        extra += " 话题=" + r["root_id"]
    line = views.log_line(r, app.tz, me, media_tool=getattr(app.platform, "media_tool", "") or "")
    return "[%s] %s (id=%s%s)" % (r["position"], line, r["id"], extra)


SCAN_PAGE = 200  # messages read from the store (and asked of the service) at a time by a filtered read
SCAN_MOST = 3000  # messages one filtered read looks through before it hands back a before= to go on from


def _when(app, value: Any, end: bool) -> Optional[float]:
    """since / until as a timestamp: "2026-11-12 15:49", ISO 8601 with or without an offset, or a date (until: that whole
    day; to the minute: that whole minute). Without an offset it is the user's time zone."""
    s = str(value or "").strip()
    if not s:
        return None
    try:
        if len(s) == 10:
            d = _dt.datetime.combine(_dt.date.fromisoformat(s.replace("/", "-")), _dt.time())
            if end:
                d += _dt.timedelta(days=1)
        else:
            d = _dt.datetime.fromisoformat(s.replace("/", "-").replace("Z", "+00:00"))
    except ValueError:
        raise RPCError("时间看不懂：%s（写成 2026-11-12 15:49 或 2026-11-12）" % s)
    if d.tzinfo is None and app.tz is not None:
        d = d.replace(tzinfo=app.tz)
    t = d.timestamp()  # naive: the machine's local time
    if end and len(s) == 10:
        return t - 0.001
    if end and re.search(r"[ T]\d{1,2}:\d{2}$", s):
        return t + 59.999  # "until 16:15" keeps what was said during 16:15
    return t


def _filter_words(app, since: Optional[float], until: Optional[float], query: Any) -> str:
    bits = []
    if since is not None or until is not None:
        bits.append("%s 到 %s" % (local_stamp(since, app.tz) if since is not None else "最早",
                                 local_stamp(until, app.tz) if until is not None else "现在"))
    q = " ".join(str(query or "").split())
    if q:
        bits.append("含“%s”" % q)
    return "，".join(bits)


def _scan(app, chat_id: str, thread: str, before_pos: Optional[int], since: Optional[float], until: Optional[float],
          words: List[str], limit: int, epoch: Optional[int] = None):
    """The newest limit messages that fit, oldest first, looking back from before_pos (the store first, then older
    history from the service), at most SCAN_MOST of them. Returns (rows, how to go on or "", how many were looked at)."""

    def fits(r: dict) -> bool:
        if until is not None and r["at"] > until:
            return False
        if since is not None and r["at"] < since:
            return False
        hay = ((r.get("text") or "") + " " + (r.get("sender_name") or "")).casefold()
        return all(w in hay for w in words)

    if thread:
        rows = [r for r in app.store.thread_messages(chat_id, thread, SCAN_MOST)
                if before_pos is None or r["position"] < before_pos]
        hits = [r for r in rows if fits(r)]
        more = "更早的符合消息：before=%s" % hits[-limit]["position"] if len(hits) > limit else ""
        return hits[-limit:], more, len(rows)
    hits: List[dict] = []  # newest first
    scanned, pos, can_fill, done = 0, before_pos, True, False
    last: Optional[dict] = None
    while scanned < SCAN_MOST and not done:
        page = app.store.messages_before(chat_id, pos, SCAN_PAGE)
        if len(page) < SCAN_PAGE and can_fill:
            had = len(page)
            none_older = app.fill_older(chat_id, page[0]["position"] if page else pos, SCAN_PAGE - had, epoch)
            page = app.store.messages_before(chat_id, pos, SCAN_PAGE)
            can_fill = not none_older and len(page) > had  # nothing came: not signed in, failed, or the start
        if not page:
            done = True
            break
        for r in reversed(page):
            if since is not None and r["at"] < since:
                done = True  # older than the window: what is left is older still
                break
            scanned += 1
            last = r
            if fits(r):
                hits.append(r)
                if len(hits) >= limit:
                    break
        if len(hits) >= limit:
            break
        pos = page[0]["position"]
        if len(page) < SCAN_PAGE and not can_fill:
            done = True  # the chat's first message
    hits.reverse()
    if len(hits) >= limit and not done:
        return hits, "更早的符合消息：before=%s" % hits[0]["position"], scanned
    if not done and last is not None:
        return hits, "看到 %s 还没看完，接着往前：before=%s" % (local_stamp(last["at"], app.tz), last["position"]), scanned
    return hits, "", scanned


def _search(app, a: dict) -> str:
    query = _need(a, "query", "关键词")
    if not app.signed_in:
        raise RPCError("还没有登录%s" % app.platform.name)
    try:
        hits = app.backend.search(query, _limit(a, 20, 50))
    except BackendError as e:
        raise RPCError("搜索失败：" + e.message)
    if not hits:
        return "没有找到“%s”" % query
    lines = ["搜到 %d 个：" % len(hits)]
    for h in hits:
        if h.kind == "group":
            lines.append("- 群：%s（chat=%s）%s" % (h.name, h.id, " " + h.detail if h.detail else ""))
        else:
            lines.append("- 人：%s（user=%s）%s" % (h.name, h.id, " " + h.detail if h.detail else ""))
    return "\n".join(lines)


_CLI_REASONS = {
    "permission_denied": "管理端没有开启 lark-cli 只读集成（manager 的 lark.cli.extensions），或者没有授权给这个扩展",
    "unsupported": "管理端的 lark-cli 版本不符合要求",
    "unavailable": "lark-cli 暂时不可用（可能没有登录）",
    "rate_limited": "lark-cli 被限流了，请稍后再试",
    "timeout": "lark-cli 超时了",
    "unknown_method": "这个 hub 不支持 lark-cli 集成",
}


def cli_search_docs(app, query: str, limit: int) -> List[dict]:
    """Documents found through the host lark-cli integration: [{title, url, summary}]. Raises RPCError with a reason."""
    try:
        res = app.hub_call("cli.call", {"operation": "search", "kind": "docs", "query": query, "limit": limit}, timeout=30)
    except RPCError as e:
        raise RPCError("文档搜索不可用：" + _CLI_REASONS.get(e.code, e.message or "lark-cli 集成出错"))
    data = ((res or {}).get("result") or {}).get("data") if isinstance(res, dict) else None
    return _docs_in(data)[:limit]


def _docs_in(data: Any) -> List[dict]:
    """Pick documents out of lark-cli's search answer, whatever its exact shape: objects with a link and a title."""
    out: List[dict] = []
    seen = set()

    def text(v: Any) -> str:
        if isinstance(v, str):
            return re.sub(r"</?[a-zA-Z][^>]*>", "", v).strip()
        return ""

    def walk(v: Any, depth: int) -> None:
        if depth > 8 or len(out) >= 50:
            return
        if isinstance(v, dict):
            url = text(v.get("url") or v.get("link") or v.get("doc_url") or v.get("obj_url") or "")
            title = text(v.get("title") or v.get("name") or v.get("title_highlighted") or v.get("doc_title") or "")
            if url and title and url not in seen:
                seen.add(url)
                summary = text(v.get("summary") or v.get("summary_highlighted") or v.get("preview") or v.get("description") or "")
                out.append({"title": title, "url": url, "summary": summary})
            for x in v.values():
                walk(x, depth + 1)
        elif isinstance(v, list):
            for x in v:
                walk(x, depth + 1)

    walk(data, 0)
    return out


def _docs(app, a: dict) -> str:
    query = _need(a, "query", "关键词")
    docs = cli_search_docs(app, query, _limit(a, 10, 20))
    if not docs:
        return "没有搜到和“%s”有关的文档" % query
    lines = ["搜到 %d 篇文档：" % len(docs)]
    for d in docs:
        lines.append("- %s %s%s" % (d["title"], d["url"], ("\n  " + one_line(d["summary"], 200)) if d.get("summary") else ""))
    return "\n".join(lines)


def _agent_reply(app, a: dict) -> str:
    if app.agent is None:
        raise RPCError("扩展还在启动，请稍后再试")
    image = a.get("image")
    if image is not None and not isinstance(image, str):
        raise RPCError("image 要填图片的绝对路径（字符串）")
    return app.agent.reply_tool(_need(a, "request", "请求 id"), _need(a, "text", "回复内容"), progress=a.get("progress") is True,
                                image=(image or "").strip())


# ---------------------------------------------------------------- writes


def _check_text(text: str) -> str:
    if not text.strip():
        raise RPCError("消息内容是空的")
    if len(text) > TEXT_MOST:
        raise RPCError("消息太长了（%d 字），最多 %d 字" % (len(text), TEXT_MOST))
    return text


def _mentions(app, a: dict, chat_id: str) -> List[tuple]:
    """The send's at: [(user id, name)], each person once, named as the store or the service knows them (RPCError when
    one is not a user id, is not known, or there are too many, or this platform cannot @)."""
    raw = a.get("at")
    if raw in (None, "", []):
        return []
    if not getattr(app.backend, "can_mention", False):
        raise RPCError("%s这里不能 @人" % app.platform.name)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise RPCError("at 要写成用户 id 的列表")
    ids: List[str] = []
    for v in raw:
        uid = str(v or "").strip() if isinstance(v, (str, int)) else ""
        if not _USER_ID.fullmatch(uid) or (uid.isdigit() and str(int(uid)) != uid):  # "07001" is no other id
            raise RPCError("at 里的 %r 不是用户 id（先用 %s_search 查）" % (str(v)[:40], app.cid))
        if uid not in ids:
            ids.append(uid)
    if len(ids) > MENTIONS_MOST:  # before any name is looked up
        raise RPCError("一条消息最多 @ %d 个人" % MENTIONS_MOST)
    me = getattr(app, "account", None)
    if me is not None and me.id in ids:  # sent as you, an @ of you reads as an @自己 request
        raise RPCError("at 里不能有你自己：以你的身份 @ 你自己，会被当成 @自己 的请求")
    out: List[tuple] = []
    for uid in ids:
        name = app.store.name(uid) if app.store is not None else ""
        if not name:
            try:
                name = app.backend.user_name(uid, chat_id) or ""
            except BackendError:
                name = ""
        if not name or name == uid:
            raise RPCError("不认识用户 %s，没法 @ 他（先用 %s_search 查到他）" % (uid, app.cid))
        out.append((uid, name))
    same = sorted({n for _, n in out if [m for _, m in out].count(n) > 1})
    if same:  # "@name" in the text could not say which of them
        raise RPCError("要 @ 的人里有重名（%s），分不清谁是谁，没有发" % "、".join(same))
    return out


def _topic_mode(app, chat_id: str) -> bool:
    """Whether the backend knows the group shows its messages as topics though it is no topic group (no new message
    can be posted there, only replies into its topics)."""
    check = getattr(app.backend, "topic_mode", None)
    try:
        return bool(check(chat_id)) if callable(check) else False
    except Exception:
        return False


def _opens_topic(app, chat_id: str) -> bool:
    """Whether a new message (no reply) opens a topic there: a thread-first group, as the service says now (the store
    keeps what a chat was when first seen). Asked only of a backend that has such groups (topic_groups)."""
    if not getattr(app.backend, "topic_groups", False):
        return False
    try:
        return bool(app.backend.chat_info(chat_id).topic)
    except Exception:
        return False


def _send_card(app, a: dict) -> tuple:
    chat_id = _need(a, "chat", "会话 id")
    text = _check_text(_need(a, "text", "消息内容"))
    reply_to = str(a.get("reply_to") or "").strip()
    mentions = _mentions(app, a, chat_id)
    spans: List[tuple] = []
    if mentions:  # the text as it will read: one not written goes in front (the card, the send and its echo agree)
        text, spans = mention_spans(text, mentions)
        text = _check_text(text)
    p = app.platform.name
    chat = app.store.get_chat(chat_id) if app.store is not None else None
    if not reply_to and (chat or {}).get("kind") != "dm" and _topic_mode(app, chat_id):
        raise RPCError("这个群的消息以话题形式展示，OpenMate 只能回复群里已有的话题（给 reply_to），开不了新话题")
    me = getattr(app, "account", None)
    ref = "会话 id %s" % chat_id  # beside the name, to check it by
    if chat is None:
        target = "%s会话 %s（本地没有记录，名字未知）" % (p, chat_id)
        visible = "会话 %s 里的人" % chat_id
    elif chat.get("kind") == "dm":
        name = chat.get("name") or ""
        if me is not None and me.id and chat.get("peer_id") == me.id:
            target = "%s里你和自己的私聊「%s」（%s）" % (p, name or app.me_name(), ref)
            visible = "只有你自己"
        elif name:
            target = "%s私聊「%s」（%s）" % (p, name, ref)
            visible = name
        else:
            target = "%s私聊（对方名字未知，%s）" % (p, ref)
            visible = "这个私聊里的对方（名字未知）"
    else:
        members = chat.get("members") or 0
        name = views.chat_name(chat, chat_id)
        target = "%s群「%s」（%s%s）" % (p, name, "%d 人，" % members if members else "", ref)
        visible = "%s群「%s」的所有成员" % (p, name)
    lines = ["以你的身份发到" + target]
    if reply_to:
        quoted = app.store.message(chat_id, reply_to) if app.store is not None else None
        root = str((quoted or {}).get("root_id") or "")
        if (quoted is not None and root and root != reply_to and (chat or {}).get("kind") != "dm"
                and getattr(app.backend, "thread_reply_quotes_root", False)):
            # what the message will really quote: the thread's first message, not the one picked
            head = app.store.message(chat_id, root)
            first = ("%s：“%s”" % (views.sender_of(head, app.me_name()), one_line(views.text_of(head), 80))
                     if head is not None else "消息 %s" % root)
            lines.append("回复到 %s 那条所在的话题里，引用的是话题第一条 %s（%s不能引用话题里的某一条）"
                         % (views.sender_of(quoted, app.me_name()), first, p))
        elif quoted is not None:
            lines.append("回复 %s：“%s”" % (views.sender_of(quoted, app.me_name()), one_line(views.text_of(quoted), 80)))
        else:
            lines.append("回复消息 %s" % reply_to)
    elif (chat or {}).get("kind") != "dm" and _opens_topic(app, chat_id):
        lines.append("在这个话题群里开一个新话题")
    if mentions:
        lines.append("会 @ 并提醒：" + "、".join("%s（%s）" % (n, u) for u, n in mentions))
    lines += ["——", text]
    return chat_id, text, reply_to, visible, "\n".join(lines), spans


def _dm_card(app, a: dict) -> tuple:
    user = _need(a, "user", "用户 id")
    text = _check_text(_need(a, "text", "消息内容"))
    name = ""
    if app.store is not None:
        name = app.store.name(user)
    who = "%s（%s）" % (name, user) if name else "用户 %s" % user
    summary = "以你的身份私聊 %s（%s）\n——\n%s" % (who, app.platform.name, text)
    return user, text, name or user, summary


def describe(app, params: dict) -> dict:
    name = str(params.get("name") or "")
    a = _args(params)
    c = app.cid
    if name == c + "_send":
        _, _, _, visible, summary, _ = _send_card(app, a)
        return {"action": {"name": name, "visible": visible}, "summary": summary}
    if name == c + "_send_dm":
        _, _, visible, summary = _dm_card(app, a)
        return {"action": {"name": name, "visible": visible}, "summary": summary}
    raise RPCError("没有这个写操作：%s" % name)


def do(app, params: dict) -> dict:
    name = str(params.get("name") or "")
    request_id = str(params.get("request_id") or "").strip()
    if not request_id:
        raise RPCError("缺少 request_id，不执行")
    a = _args(params)
    c = app.cid
    _require_store(app)
    key = "action:" + request_id
    p = app.platform.name
    epoch = app.account_epoch()  # the account the user agreed to send as: nothing goes out as another

    def as_asked() -> None:
        if not app.owns(epoch):  # (a sign-in being taken in is waited for first)
            raise RPCError("刚换了%s账号，没有发出：请在新账号下重新操作" % p)

    if name == c + "_send":
        try:
            chat_id, text, reply_to, visible, _, mentions = _send_card(app, a)
            root = None
            if reply_to:
                quoted = app.store.message(chat_id, reply_to)
                if quoted is not None and quoted.get("root_id"):
                    root = quoted["root_id"]
        except Exception as e:
            known = app.send_known(key)  # a request seen before: the journal answers, never taken (RPCError: going
            if known is not None:  # or may have gone), whatever the card reads now (its names may be gone)
                return _result(known)
            if isinstance(e, RPCError):
                raise
            # (a store busy for a moment): nothing sent, the same request may try again
            raise RPCError("没能读出这条消息的发送信息（%s），没有发出，请再试一次" % type(e).__name__)

        def send():
            as_asked()
            mid = app.send_as_user(chat_id, text, reply_to or None, root, mentions=mentions or None)
            return {"text": "已发送到%s" % visible, "message_ref": mid}

        return _result(app.send_once(key, name, send))
    if name == c + "_send_dm":
        user, text, who, _ = _dm_card(app, a)
        earlier = app.store.send_get(key)
        if earlier is None:
            # The chat first, before the request id is taken: a failure here leaves the id free for a retry.
            if not app.signed_in:
                raise RPCError("还没有登录%s" % p)
            as_asked()
            try:
                chat_id = app.backend.create_chat(user)
            except BackendError as e:
                raise RPCError("没能打开和 %s 的私聊：%s" % (who, e.message))
        else:
            chat_id = ""

        def send():
            as_asked()  # again: opening the chat asked the service, and the account may have changed meanwhile
            mid = app.send_as_user(chat_id, text)
            return {"text": "已私聊 %s" % who, "message_ref": mid}

        return _result(app.send_once(key, name, send))
    raise RPCError("没有这个写操作：%s" % name)


def _result(r: dict) -> dict:
    out = {"text": str(r.get("text") or "已发送")}
    ref = str(r.get("message_ref") or "")
    if ref and len(ref) <= 200:
        out["message_ref"] = ref
    return out
