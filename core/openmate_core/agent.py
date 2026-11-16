"""/agent: the user's own chat message that starts with the prefix becomes a request an Agent handles through the
hub, and the answer goes back into the chat as the user, ending with the AI-reply suffix.

A request is taken once, only from a live message of the user's own (not a system line, not from history, at most
`agent_fresh` old, not one of the Agent's own replies). The Agent gets the context the extension can gather (the
thread or the last 40 messages, the quoted message, the knowledge base's view of the chat, up to two linked documents)
and answers with <connector>_agent_reply, bound to this request by its id. A request in the thread of an earlier one
goes on in that conversation. If the turn ends without a reply through the tool, the latest answer is posted once
instead (never both). A turn ends at its turn_duration item; Claude Code does not always write one, so a session that
answered and then stayed quiet for `agent_settle` has ended its turn too when its status says done or idle. /stop, /停止 or /停 in the thread (or quoting the request) stops it. Runs are kept in the store,
so a restart goes on waiting for the running ones.

The hub is reached over a network that drops now and then. A request waits for it (`agent_hub_wait`), and a start that
failed on the way is tried again. A new conversation has no idempotency key, so a start that may have reached the hub
is tried again under a new request id (`<run id>.<try>`): a conversation the lost start may have opened can then never
reply into the chat, so the user never gets two answers.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import threading
import time
from typing import Dict, List, Optional
from urllib.parse import urlsplit

from . import views
from .hub import HubError
from .textutil import clip, local_stamp, strip_leading_mentions
from .wire import RPCError

log = logging.getLogger("openmate.agent")

DEFAULT_PREFIX = "/agent"
DEFAULT_SUFFIX = "> 🤖 AI 代答，仅供参考"
KNOWN_SUFFIXES = ("🤖 AI 代答，仅供参考", "🤖")
STOP_WORDS = ("/stop", "/停止", "/停")
ACTIVE = ("starting", "running")
PROMPT_MOST = 30000
CONTEXT_MESSAGES = 40
DOC_LINKS_MOST = 2
_DOC_PATHS = ("/docx/", "/docs/", "/doc/", "/wiki/", "/sheets/", "/sheet/", "/base/", "/bitable/", "/mindnotes/", "/slides/", "/file/")
_URL = re.compile(r"https?://[^\s<>\"'，。）)】\]]+")


def valid_prefix(v) -> str:
    """The configured prefix if it is usable: starts with "/", no spaces, not a stop word; else /agent."""
    s = str(v or "").strip()
    if not s or not s.startswith("/") or len(s) < 2 or any(c.isspace() for c in s) or s.lower() in STOP_WORDS:
        if s and s != DEFAULT_PREFIX:
            log.warning("agent_prefix is not usable (it must start with /, have no spaces and not be a stop word): using /agent")
        return DEFAULT_PREFIX
    return s


def parse_suffix(v) -> str:
    """The reply suffix: the default when unset or empty, none for a single space, a literal \\n is a newline."""
    if v is None:
        return DEFAULT_SUFFIX
    s = str(v)
    if s == " ":
        return ""
    if s == "":
        return DEFAULT_SUFFIX
    return s.replace("\\n", "\n")


def match_prefix(body: str, prefix: str) -> Optional[str]:
    """The request after the prefix (maybe empty), or None when body does not start with it as a word."""
    if not body.lower().startswith(prefix.lower()):
        return None
    rest = body[len(prefix):]
    if rest == "":
        return ""
    if rest[0] in " \t\r\n:：":
        return rest.lstrip(" \t\r\n:：").strip()
    return None


def _attr(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def _body(s: str) -> str:
    # a quoted chat line must not close or open a context block
    return (s or "").replace("persona-context", "persona context")


def _block(kind: str, title: str, body: str, url: str = "") -> str:
    u = ' url="%s"' % _attr(url) if url else ""
    return '<persona-context kind="%s" title="%s"%s>\n%s\n</persona-context>' % (kind, _attr(title), u, _body(body).strip())


def _span(seconds: float) -> str:
    if seconds >= 3600:
        return "%g 小时" % round(seconds / 3600, 1)
    if seconds >= 60:
        return "%d 分钟" % int(seconds // 60)
    return "%d 秒" % max(1, int(seconds))


class AgentRunner:
    def __init__(self, app):
        self.app = app
        cfg = app.config
        self.enabled = bool(cfg.get("agent"))
        self.prefix = valid_prefix(cfg.get("agent_prefix"))
        self.machine = str(cfg.get("agent_machine") or "").strip()
        self.launcher = str(cfg.get("agent_launcher") or "").strip()
        self.suffix = parse_suffix(cfg.get("agent_reply_suffix"))
        self.purpose = getattr(app.platform, "agent_purpose", "") or ""
        self._mu = threading.Lock()
        self._run_locks: Dict[str, threading.RLock] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_prune = 0.0
        self._posted: Dict[str, float] = {}  # hashes of what /agent posted lately: their echoes are never requests
        self._asked: Dict[str, float] = {}  # run id → when its session's status was last asked

    # ---------------------------------------------------------------- lifecycle

    @property
    def store(self):
        return self.app.store

    @property
    def hub(self):
        return self.app.hub

    def _lock(self, run_id: str) -> threading.RLock:
        with self._mu:
            lk = self._run_locks.get(run_id)
            if lk is None:
                lk = self._run_locks[run_id] = threading.RLock()
            return lk

    def start(self) -> None:
        self._prune(force=True)
        for run in self.store.runs_in_state(["starting"]):
            # Cut off by a restart before the conversation was surely started: starting it again could start two.
            with self._lock(run["id"]):
                run["state"] = "failed"
                run["error"] = "restart"
                self.store.run_put(run)
            self._post_once(run, "agent-restart:" + run["id"],
                            "扩展重启了，这个 /agent 请求没能开始处理，请重新发送。", reply_to=run["request_id"])
        resumed = len(self.store.runs_in_state(["running"]))
        if resumed:
            log.info("resuming %d running /agent request(s)", resumed)
        self._thread = threading.Thread(target=self._loop, name="openmate-agent", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop polling; the store closes next, so wait (a little) for a poll under way."""
        self._stop.set()
        self._wake.set()
        th = self._thread
        if th is not None and th is not threading.current_thread():
            th.join(3.0)

    def _prune(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_prune < 600:
            return
        self._last_prune = now
        t = self.app.timing
        self.store.runs_prune(now - t.agent_keep, t.agent_keep_most)
        self.store.seen_prune(now - t.agent_keep)

    # ---------------------------------------------------------------- messages

    @staticmethod
    def _digest(text: str) -> str:
        return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()

    def _echo(self, text: str) -> bool:
        with self._mu:
            now = time.time()
            for k in [k for k, at in self._posted.items() if now - at > 600]:
                del self._posted[k]
            return self._digest(text) in self._posted

    def _remember_post(self, body: str) -> None:
        with self._mu:
            self._posted[self._digest(body)] = time.time()

    def is_agent_text(self, text: str) -> bool:
        t = (text or "").rstrip()
        for s in (self.suffix.strip(), DEFAULT_SUFFIX) + KNOWN_SUFFIXES:
            if s and t.endswith(s):
                return True
        return False

    def on_message(self, row: dict) -> None:
        """A newly stored live message."""
        if not row.get("mine") or row.get("from_history") or views.is_system(row):
            return
        text = row.get("text") or ""
        if not text.strip():
            return
        now = time.time()
        at = row.get("at") or 0
        if at and abs(now - at) > self.app.timing.agent_fresh:
            return
        if self.is_agent_text(text) or self._echo(text):
            return
        body = strip_leading_mentions(text).strip()
        if body.lower() in STOP_WORDS:
            if self.store.seen_add("stop:%s:%s" % (row["chat_id"], row["id"])):
                threading.Thread(target=self._on_stop, args=(row,), name="openmate-agent-stop", daemon=True).start()
            return
        if not self.enabled:
            return
        req = match_prefix(body, self.prefix)
        if req is None:
            return
        if not self.store.seen_add("agent:%s:%s" % (row["chat_id"], row["id"])):
            return
        run = {
            "id": "omr_" + secrets.token_hex(10),
            "chat_id": row["chat_id"],
            "request_id": row["id"],
            "thread_key": row.get("root_id") or row["id"],
            "root_id": row.get("root_id") or "",
            "state": "starting",
            "created": now,
            "request_text": req,
            "replies": 0,
            "reply_inflight": 0,
            "reply_ids": [],
            "fallback": "",
            "machine_id": "",
            "session_id": "",
            "last_seq": 0,
            "ours_seen": True,
            "client_msg_id": "",
            "last_text": None,
        }
        self.store.run_put(run)
        self._prune()
        threading.Thread(target=self._start_run, args=(run, row), name="openmate-agent-start", daemon=True).start()

    # ---------------------------------------------------------------- starting

    def _previous(self, run: dict) -> Optional[dict]:
        keep = self.app.timing.agent_keep
        for r in self.store.runs_in_thread(run["chat_id"], run["thread_key"]):
            if r["id"] != run["id"] and r.get("session_id") and r["state"] != "failed" and time.time() - r["created"] <= keep:
                return r
        return None

    def _start_run(self, run: dict, row: dict) -> None:
        try:
            self._start(run, row)
        except Exception:
            log.exception("/agent start failed")
            self._fail(run["id"], "没能启动 Agent：扩展内部错误")

    @staticmethod
    def _ref(run: dict) -> str:
        """The request id the Agent replies with: the run's id, then `<id>.<try>` for each start tried again."""
        return run.get("ref") or run["id"]

    def _start(self, run: dict, row: dict) -> None:
        if not self.hub.available:
            self._fail(run["id"], "没能处理 /agent：扩展拿不到 Persona hub 的地址或令牌")
            return
        prev = self._previous(run)
        try:
            prompt = self.build_prompt(run, row, prev)
        except Exception:
            log.exception("/agent context could not be gathered")
            prompt = self._instructions(run) + "\n" + (run["request_text"] or self.prefix)
        t = self.app.timing
        deadline = time.time() + t.agent_hub_wait
        cmid = "openmate-" + run["id"]
        baseline: Optional[int] = None
        if prev is not None:
            self._supersede(prev["id"], run["id"])
        tries = 1
        while True:
            if self._stop.is_set():  # shutting down: the next start tells the user to send it again
                return
            if self._state(run["id"]) == "stopped":
                return
            if not self._hub_up(run["id"], deadline):
                if not self._stop.is_set() and self._state(run["id"]) != "stopped":
                    self._fail(run["id"], "这台电脑和 Persona 之间的网络断了，等了 %s 还没恢复，这个 /agent 请求没有开始处理。"
                                          "网络恢复后请重新发送。" % _span(t.agent_hub_wait))
                return
            try:
                if prev is not None:
                    mid, sid = prev["machine_id"], prev["session_id"]
                    if baseline is None:
                        baseline = self._latest_seq(mid, sid)
                    self.hub.send_message(mid, sid, prompt, cmid)  # the hub sends each client_msg_id once
                    fields = {"machine_id": mid, "session_id": sid, "last_seq": baseline, "client_msg_id": cmid,
                              "ours_seen": False, "continued": prev["id"]}
                else:
                    sess = self.hub.start_conversation(prompt, self.purpose, True, self.machine, self.launcher)
                    fields = {"machine_id": sess.get("machine_id") or "", "session_id": sess["session_id"], "last_seq": 0,
                              "ours_seen": True}
                break
            except HubError as e:
                log.warning("/agent could not start: HTTP %s %s", e.status, e.code)
                if not e.transient:
                    self._fail(run["id"], "没能启动 Agent：%s" % (e.message or "hub 拒绝了请求"))
                    return
                if time.time() >= deadline:
                    self._fail(run["id"], "没能启动 Agent：%s。等了 %s 还是不行，请稍后重新发送。"
                               % (_hub_words(e), _span(t.agent_hub_wait)))
                    return
                if prev is None and not e.unsent:
                    # it may have reached the hub and opened a conversation: start again under a new request id
                    if tries >= t.agent_start_tries:
                        self._fail(run["id"], "没能启动 Agent：这台电脑和 Persona 之间的网络不稳定，试了 %d 次都没成功，请稍后重新发送。"
                                   % tries)
                        return
                    tries += 1
                    old = self._ref(run)
                    run = self._retry_as(run["id"], "%s.%d" % (run["id"], tries)) or run
                    prompt = prompt.replace(old, self._ref(run))
                self._stop.wait(min(t.agent_hub_probe, max(0.0, deadline - time.time())))
        stopped = False
        with self._lock(run["id"]):
            cur = self.store.run_get(run["id"]) or run
            cur.update(fields)
            if cur["state"] == "stopped":
                stopped = True
            else:
                cur["state"] = "running"
            self.store.run_put(cur)
        if stopped:  # /stop arrived while it was starting
            try:
                self.hub.stop_conversation(fields["session_id"])
            except HubError:
                pass
            return
        log.info("/agent request started (%s%s)", "continued" if prev is not None else "new conversation",
                 ", try %d" % tries if tries > 1 else "")
        self._wake.set()

    def _hub_up(self, run_id: str, deadline: float) -> bool:
        """Wait until the hub answers; False when it did not by the deadline, or the request was stopped meanwhile."""
        t = self.app.timing
        while True:
            if self._stop.is_set() or self._state(run_id) == "stopped":
                return False
            try:
                self.hub.health(timeout=max(1.0, min(10.0, deadline - time.time())))
                return True
            except HubError as e:
                if not e.unreachable:
                    return True  # it answered: whatever it said, the start's own answer will tell
            left = deadline - time.time()
            if left <= 0:
                return False
            self._stop.wait(min(t.agent_hub_probe, left))

    def _retry_as(self, run_id: str, ref: str) -> Optional[dict]:
        with self._lock(run_id):
            cur = self.store.run_get(run_id)
            if cur is None:
                return None
            cur["ref"] = ref
            self.store.run_put(cur)
            return cur

    def _supersede(self, prev_id: str, run_id: str) -> None:
        """A newer request goes on in the same conversation: the turn's end is the newer one's to answer, so the older
        run stops waiting (and can never post a fallback for the same turn). Its reply tool keeps working."""
        with self._lock(prev_id):
            cur = self.store.run_get(prev_id)
            if cur is not None and cur["state"] in ACTIVE:
                cur["state"] = "done"
                cur["superseded_by"] = run_id
                self.store.run_put(cur)

    def _latest_seq(self, mid: str, sid: str) -> int:
        try:
            res = self.hub.items(mid, sid) or {}
        except HubError:
            return 0
        return max([int(it.get("seq") or 0) for it in res.get("items") or []] or [0])

    def _instructions(self, run: dict) -> str:
        c, p = self.app.cid, self.app.platform.name
        body = "\n".join([
            "我在%s里发了一条 /agent 请求，请你替我处理。" % p,
            "- 这次请求的 id 是 %s。处理完调用工具 %s_agent_reply 回复：request 填 \"%s\"，text 填要发的内容。回复会以我的身份发到这个话题里，并自动加上 AI 代答标记，你不用自己加。" % (self._ref(run), c, self._ref(run)),
            "- 回复以我的名义发出：只写已经做完的事和查到的事实；还没做的写成建议，不要替我答应任何事。",
            "- 可以多次调用 %s_agent_reply 发进度，但只发有意义的进展和最终结果。" % c,
            "- 不要用 %s_send 回复这条请求；要给别人发消息，照常走手机确认，结果不确定时不要重发。" % c,
            "- 下面的上下文是自动整理的，可能不全；缺什么就用 %s_messages、%s_search、知识库等工具自己查。" % (c, c),
        ])
        return _block("instructions", "/agent 说明", body)

    def build_prompt(self, run: dict, row: dict, prev: Optional[dict]) -> str:
        app = self.app
        store = app.store
        chat_id = run["chat_id"]
        chat = store.get_chat(chat_id)
        me = app.me_name()
        tz = app.tz
        mark = "  ← 请求"
        blocks: List[str] = [self._instructions(run)]
        loc = "%s，chat=%s，请求消息 id=%s，时间 %s" % (views.where(app.platform.name, chat, chat_id), chat_id, row["id"],
                                                 local_stamp(row.get("at") or 0, tz))
        if run["root_id"]:
            loc += "，在话题里（根消息 id=%s）" % run["root_id"]
        blocks.append(_block("context", "位置", loc))
        # the chat: the thread, or the last 40 messages up to the request
        if run["root_id"]:
            rows = store.thread_messages(chat_id, run["root_id"], CONTEXT_MESSAGES + 10)
            title = "话题里的消息"
        else:
            upto = int(row["position"]) + 1
            rows = store.messages_before(chat_id, upto, CONTEXT_MESSAGES + 1)
            if len(rows) < CONTEXT_MESSAGES + 1:
                app.fill_older(chat_id, rows[0]["position"] if rows else upto, CONTEXT_MESSAGES + 1 - len(rows))
                rows = store.messages_before(chat_id, upto, CONTEXT_MESSAGES + 1)
            title = "最近 %d 条消息" % CONTEXT_MESSAGES
        if prev is not None:
            prev_row = store.message(chat_id, prev["request_id"])
            if prev_row is not None:
                rows = [r for r in rows if r["position"] > prev_row["position"]]
                title = "上次请求之后的消息"
        rows = [r for r in rows if views.has_content(r) and r["position"] <= int(row["position"]) or r["id"] == row["id"]]
        lines = [clip(views.log_line(r, tz, me, mark if r["id"] == row["id"] else ""), 2000) for r in rows]
        quoted = store.message(chat_id, row["parent_id"]) if row.get("parent_id") else None
        tail: List[str] = []
        if quoted is not None:
            tail.append(_block("context", "引用的消息", clip(views.log_line(quoted, tz, me), 4000) + "（id=%s）" % quoted["id"]))
        if prev is None:
            kb = self._kb_context(chat_id)
            if kb:
                tail.append(_block("context", "知识库里的会话背景", kb))
        sources = [row.get("text") or ""] + ([quoted.get("text") or ""] if quoted else []) + [r.get("text") or "" for r in rows[-10:]]
        for title_d, url, body in self._doc_blocks(sources):
            tail.append(_block("doc", title_d, body, url))
        sentence = run["request_text"] or self.prefix
        fixed = sum(len(b) + 1 for b in blocks + tail) + len(sentence) + 200
        budget = max(2000, PROMPT_MOST - fixed)
        kept: List[str] = []
        used = 0
        for line in reversed(lines):  # newest kept
            if used + len(line) + 1 > budget:
                break
            kept.append(line)
            used += len(line) + 1
        kept.reverse()
        if kept:
            blocks.append(_block("context", title, "\n".join(kept)))
        blocks += tail
        return "\n".join(blocks) + "\n" + sentence

    def _kb_context(self, chat_id: str) -> str:
        try:
            ctx = self.hub.kb_chat_context("%s-%s" % (self.app.cid, chat_id))
        except HubError as e:
            log.info("knowledge base context unavailable: HTTP %s", e.status)
            return ""
        if not ctx:
            return ""
        lines = []
        if ctx.get("summary"):
            lines.append(str(ctx["summary"]).strip())
        for p in (ctx.get("projects") or [])[:3]:
            if isinstance(p, dict) and p.get("name"):
                lines.append("项目「%s」：%s" % (p["name"], str(p.get("summary") or "").strip()))
        return clip("\n".join(lines), 6000)

    def _doc_links(self, texts: List[str]) -> List[str]:
        hosts = tuple(h.lower() for h in getattr(self.app.platform, "doc_hosts", ()) or ())
        out: List[str] = []
        for t in texts:
            for url in _URL.findall(t or ""):
                parts = urlsplit(url)
                host = (parts.hostname or "").lower()
                if not any(host == h or host.endswith("." + h) for h in hosts):
                    continue
                if not any(seg in parts.path + "/" for seg in _DOC_PATHS):
                    continue
                if url not in out:
                    out.append(url)
                if len(out) >= DOC_LINKS_MOST:
                    return out
        return out

    def _doc_blocks(self, texts: List[str]):
        from .tools import cli_search_docs  # here: tools imports nothing of this module

        out = []
        cli_ok = True
        for url in self._doc_links(texts):
            token = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
            found = None
            note = ""
            if cli_ok and token:
                try:
                    for d in cli_search_docs(self.app, token, 5):
                        if token in d.get("url", ""):
                            found = d
                            break
                    if found is None:
                        note = "lark-cli 没有搜到这篇文档，需要的话请自己打开链接读。"
                except RPCError as e:
                    cli_ok = False
                    note = "%s；需要的话请自己打开链接读。" % e.message
            if found is not None:
                body = "标题：%s\n%s" % (found["title"], found.get("summary") or "（没有摘要，需要全文请自己打开链接）")
                out.append((found["title"], url, body))
            else:
                out.append(("链接里的文档", url, "链接：%s\n%s" % (url, note)))
        return out

    # ---------------------------------------------------------------- waiting for the answer

    def _state(self, run_id: str) -> str:
        r = self.store.run_get(run_id)
        return r["state"] if r else ""

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.app.timing.agent_poll)
            self._wake.clear()
            if self._stop.is_set():
                return
            for run in self.store.runs_in_state(["running"]):
                if self._stop.is_set():
                    return
                try:
                    self._poll(run)
                except HubError as e:
                    log.info("/agent poll: HTTP %s", e.status)
                except Exception:
                    log.exception("/agent poll failed")
            self._prune()

    def _poll(self, run: dict) -> None:
        if time.time() - run["created"] > self.app.timing.agent_timeout:
            self._timeout(run["id"])
            return
        mid, sid = run["machine_id"], run["session_id"]
        after = int(run.get("last_seq") or 0)
        items: List[dict] = []
        for _ in range(20):
            res = self.hub.items(mid, sid, after_seq=after) or {}
            page = res.get("items") or []
            items += page
            if page:
                after = max(after, max(int(it.get("seq") or 0) for it in page))
            if not page or not res.get("more_after"):
                break
        ended = interrupted = False
        if items:
            with self._lock(run["id"]):
                cur = self.store.run_get(run["id"])
                if cur is None or cur["state"] != "running":
                    return
                ours = bool(cur.get("ours_seen"))
                last_text = cur.get("last_text")
                answered = bool(cur.get("answered"))
                for it in items:
                    if not ours:
                        if (cur.get("client_msg_id") and it.get("client_msg_id") == cur["client_msg_id"]) or (
                                it.get("role") == "user" and cur["id"] in (it.get("text") or "")):
                            ours = True
                        continue
                    kind, role = it.get("kind") or "", it.get("role") or ""
                    if role == "assistant":
                        answered = True
                    if role == "assistant" and kind == "text" and (it.get("text") or "").strip():
                        last_text = {"id": it.get("id") or "", "text": it.get("text") or "", "full": int(it.get("full_size") or 0)}
                    elif kind == "turn_duration" or it.get("turn_duration"):
                        ended = True
                    elif kind == "notice" and _interrupted(it.get("text") or ""):
                        interrupted = True
                cur["ours_seen"] = ours
                cur["last_text"] = last_text
                cur["answered"] = answered
                cur["quiet_since"] = time.time()
                cur["last_seq"] = after
                self.store.run_put(cur)
                run = cur
        if not ended and not interrupted and self._settled(run):
            ended = True
        if interrupted:
            # stopped in Persona (the app's stop button): say so, and never post the half-finished answer
            with self._lock(run["id"]):
                cur = self.store.run_get(run["id"])
                if cur is None or cur["state"] != "running":
                    return
                cur["state"] = "stopped"
                self.store.run_put(cur)
            self._post_once(cur, "agent:%s:app-stop" % run["id"], "已在 Persona 里停止", reply_to=cur["request_id"],
                            root=cur["root_id"] or cur["request_id"])
        elif ended:
            self._turn_ended(run["id"])

    def _settled(self, run: dict) -> bool:
        """The turn ended without its turn_duration item: the session answered our request, has been quiet for
        agent_settle (so the session list's status, which lags the transcript, has caught up) and is done or idle."""
        if not run.get("ours_seen") or not run.get("answered"):
            return False
        settle = self.app.timing.agent_settle
        now = time.time()
        if now - float(run.get("quiet_since") or run["created"]) < settle:
            return False
        with self._mu:
            if now - self._asked.get(run["id"], 0.0) < settle:
                return False
            self._asked[run["id"]] = now
            for k in [k for k, at in self._asked.items() if now - at > 3600]:
                del self._asked[k]
        try:
            row = self.hub.session_row(run["machine_id"], run["session_id"])
        except HubError as e:
            log.info("/agent session status: HTTP %s", e.status)
            return False
        return bool(row) and (row.get("status") in ("done", "idle") or row.get("state") in ("unread", "idle"))

    def _turn_ended(self, run_id: str) -> None:
        with self._lock(run_id):
            cur = self.store.run_get(run_id)
            if cur is None or cur["state"] != "running":
                return
            post = None
            if not cur["replies"] and not cur["reply_inflight"] and not cur.get("fallback"):
                lt = cur.get("last_text") or {}
                text = lt.get("text") or ""
                if lt.get("full") and lt.get("id"):
                    try:
                        full = self.hub.item_full(cur["machine_id"], cur["session_id"], lt["id"]) or {}
                        text = full.get("text") or text
                    except HubError:
                        pass
                if text.strip():
                    cur["fallback"] = "claimed"  # first, so a reply through the tool now cannot also go out
                    post = text
            cur["state"] = "done"
            self.store.run_put(cur)
        if post is not None:
            mid = self._post_once(cur, "agent:%s:fallback" % run_id, post, reply_to=cur["request_id"],
                                  root=cur["root_id"] or cur["request_id"])
            with self._lock(run_id):
                cur = self.store.run_get(run_id)
                cur["fallback"] = "sent" if mid else "failed"
                if mid:
                    cur["reply_ids"] = (cur.get("reply_ids") or []) + [mid]
                self.store.run_put(cur)

    def _timeout(self, run_id: str) -> None:
        with self._lock(run_id):
            cur = self.store.run_get(run_id)
            if cur is None or cur["state"] != "running":
                return
            cur["state"] = "timeout"
            self.store.run_put(cur)
        if not cur["replies"] and not cur["reply_inflight"]:
            self._post_once(cur, "agent:%s:timeout" % run_id,
                            "等了 %s 还没有结果，不再等待。可以在 Persona 里打开这个会话看进度。" % _span(self.app.timing.agent_timeout),
                            reply_to=cur["request_id"], root=cur["root_id"] or cur["request_id"])

    def _fail(self, run_id: str, message: str) -> None:
        with self._lock(run_id):
            cur = self.store.run_get(run_id)
            if cur is None or cur["state"] not in ACTIVE:
                return
            cur["state"] = "failed"
            cur["error"] = message
            self.store.run_put(cur)
        self._post_once(cur, "agent:%s:failed" % run_id, message, reply_to=cur["request_id"],
                        root=cur["root_id"] or cur["request_id"])

    # ---------------------------------------------------------------- sending as the user

    def with_suffix(self, text: str) -> str:
        t = (text or "").rstrip()
        if not self.suffix:
            return t
        for s in {self.suffix.strip(), DEFAULT_SUFFIX, KNOWN_SUFFIXES[0]}:
            while s and t.endswith(s):  # the Agent wrote the mark itself: keep one
                t = t[: -len(s)].rstrip()
                if t.endswith(">"):
                    t = t[:-1].rstrip()
        sep = "" if self.suffix.startswith("\n") else "\n\n"
        return t + sep + self.suffix

    def _send(self, run: dict, key: str, body: str, reply_to: str, root: str) -> dict:
        """Send body into the run's chat as the user, once per key (the send journal). Nobody confirms these sends
        (the user asked for them with /agent), so each one that went out is told to the hub's sent-as-user ledger
        (sent.record, allowed by the manifest's sends_as_user); one answered from the journal is not told again."""

        def go() -> dict:
            mid = self.app.send_as_user(run["chat_id"], body, reply_to or None, root or None)
            chat = self.store.get_chat(run["chat_id"])
            self.app.record_sent(run["chat_id"], body, str(mid or ""), reply_to=reply_to, root=root,
                                 session_id=run.get("session_id") or "", machine_id=run.get("machine_id") or "",
                                 setting="agent",
                                 summary="/agent 以你的身份回复到" + views.where(self.app.platform.name, chat, run["chat_id"]))
            return {"text": "已回复", "message_ref": mid}

        return self.app.send_once(key, "agent", go)

    def _post_once(self, run: dict, key: str, text: str, reply_to: str = "", root: str = "") -> str:
        """Send text (with the suffix) into the run's chat once per key; the new message's id, "" when it failed."""
        body = self.with_suffix(text)
        self._remember_post(body)
        try:
            res = self._send(run, key, body, reply_to, root)
            return str(res.get("message_ref") or "")
        except RPCError as e:
            log.warning("an /agent message was not sent: %s", e.code or "error")
            return ""

    def reply_tool(self, request: str, text: str) -> str:
        """<connector>_agent_reply: only the run this request id names, and only while it may still reply."""
        run = self.store.run_get(request.split(".", 1)[0])
        if run is None:
            raise RPCError("没有这个 /agent 请求：request 不对，或者已经过期")
        if request != self._ref(run):
            raise RPCError("这个 /agent 请求已经交给另一个会话处理了，这里不用再回复")
        if run["state"] in ("stopped", "failed"):
            raise RPCError("这个 /agent 请求已经停止了，不再回复")
        if time.time() - run["created"] > self.app.timing.agent_keep:
            raise RPCError("这个 /agent 请求已经过期了")
        if not text.strip():
            raise RPCError("回复内容是空的")
        key = "agent:%s:%s" % (run["id"], hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:32])
        with self._lock(run["id"]):
            cur = self.store.run_get(run["id"])
            if cur["state"] in ("stopped", "failed"):
                raise RPCError("这个 /agent 请求已经停止了，不再回复")
            cur["reply_inflight"] = int(cur.get("reply_inflight") or 0) + 1
            self.store.run_put(cur)
        body = self.with_suffix(text)
        self._remember_post(body)
        sent_id = ""
        try:
            res = self._send(cur, key, body, cur["request_id"], cur["root_id"] or cur["request_id"])
            sent_id = str(res.get("message_ref") or "")
            went = True
        except RPCError:
            row = self.store.send_get(key)
            went = row is not None and row["state"] in ("uncertain", "done")  # it may have gone out: no fallback
            with self._lock(run["id"]):
                cur = self.store.run_get(run["id"])
                cur["reply_inflight"] = max(0, int(cur.get("reply_inflight") or 0) - 1)
                if went:
                    cur["replies"] = int(cur.get("replies") or 0) + 1
                self.store.run_put(cur)
            raise
        with self._lock(run["id"]):
            cur = self.store.run_get(run["id"])
            cur["reply_inflight"] = max(0, int(cur.get("reply_inflight") or 0) - 1)
            cur["replies"] = int(cur.get("replies") or 0) + 1
            if sent_id:
                cur["reply_ids"] = (cur.get("reply_ids") or []) + [sent_id]
            self.store.run_put(cur)
        return "已回复到%s（消息 id=%s）" % (self.app.platform.name, sent_id or "未知")

    # ---------------------------------------------------------------- /stop

    def _target(self, row: dict) -> Optional[dict]:
        chat_id = row["chat_id"]
        parent = row.get("parent_id") or ""
        if parent:
            for r in self.store.runs_in_chat(chat_id):
                if r["request_id"] == parent or parent in (r.get("reply_ids") or []):
                    return r
        root = row.get("root_id") or ""
        if root:
            runs = self.store.runs_in_thread(chat_id, root)
            if runs:
                return runs[0]
            return None
        if parent or views.chat_kind(self.store.get_chat(chat_id)) != "dm":
            return None
        # a bare /stop in a private chat (no threads there, the answers quote the request): the chat's latest request
        # still at work, else a recent one. In a group the answers are in the request's thread, and so is its /stop.
        runs = self.store.runs_in_chat(chat_id)
        for r in runs:
            if r["state"] in ACTIVE:
                return r
        if runs and time.time() - runs[0]["created"] <= self.app.timing.agent_fresh:
            return runs[0]
        return None

    def _on_stop(self, row: dict) -> None:
        try:
            run = self._target(row)
            if run is None:
                return
            self._stop_run(run, row)
        except Exception:
            log.exception("/stop failed")

    def _stop_run(self, run: dict, row: dict) -> None:
        with self._lock(run["id"]):
            cur = self.store.run_get(run["id"]) or run
            before = cur["state"]
            cur["state"] = "stopped"
            cur["stopped_at"] = time.time()
            self.store.run_put(cur)
        tasks: List[str] = []
        failed = ""
        if before == "stopped":
            already = True
        elif cur.get("session_id"):
            try:
                res = self.hub.stop_conversation(cur["session_id"]) or {}
                already = bool(res.get("already"))
                tasks = [str(t.get("title") or "") for t in res.get("tasks") or [] if isinstance(t, dict) and t.get("title")]
            except HubError as e:
                if e.status == 404:  # an older hub, or not a reporting conversation: interrupt the turn at least
                    try:
                        self.hub.interrupt(cur["machine_id"], cur["session_id"])
                    except HubError:
                        pass
                    already = before not in ACTIVE
                elif e.unreachable:  # replies are refused from now on all the same: the chat side has stopped
                    failed = "网络不稳，没能通知 Persona，那边的会话可能还在运行；它不会再回复到这里"
                    already = False
                else:
                    failed = e.message or "hub 没有回答"
                    already = False
        else:
            already = before not in ACTIVE
        if failed:
            text = ("已停止回复：%s" if before in ACTIVE else "没能停止：%s") % failed
        elif already:
            text = "已经结束了"
        else:
            text = "已停止" + ("：" + "、".join(tasks) if tasks else "")
        self._post_once(cur, "agent-stop:%s:%s" % (row["chat_id"], row["id"]), text, reply_to=row["id"],
                        root=row.get("root_id") or "")


def _hub_words(e: HubError) -> str:
    if e.unreachable:  # the relay's own words are English and say nothing more
        return "这台电脑和 Persona 之间的网络断了"
    return (e.message or "hub 没有回答").rstrip("。.")


def _interrupted(text: str) -> bool:
    """The notice Persona's transcript writes for a stopped turn (transcript.InterruptedNotice)."""
    return (text or "").strip() == "已中断"
