"""Another account signs in on the same computer while a read, a live line or an /agent start of the account before is
still under way: nothing of the one before reaches the store of the next (its messages, its runs, the names it learnt,
its chats coming back or gone), and nothing of it acts on the next one's runs. Each test holds one thread at the very
point of the switch, switches, then lets it go (or hands the callback of the account before in late)."""

try:  # first: every temporary directory of this run in one, removed at its end
    import _runtmp  # noqa: F401
except ImportError:  # (started by its path from elsewhere: its directory is not on the path)
    pass
import os
import shutil
import tempfile
import threading
import time
import unittest

from harness import CID, Harness, HubSideError, secret_json, wait_until
from openmate_core.backend import Account, Chat, DocMention, Message, SendRejected
from openmate_core.fixture import ME
from openmate_core.people import People
from openmate_core.store import Store
from openmate_core.wire import RPCError

OTHER = Account("user_example_other", "另一个示例用户")
OWN = "chat_example_b_own"  # a group of the other account's only
SHARED = "chat_example_shared"  # a group both accounts are in
TOPIC = "msg_example_topic_root"  # a topic of the shared group


class Hold:
    """Stands in for a method: the first call that matches waits until go is set (before the real call, or after it
    with its answer in hand); every other call goes straight through. A hold nobody let go is a failure."""

    def __init__(self, fn, match, after=False):
        self.fn, self.match, self.after = fn, match, after
        self.mu = threading.Lock()
        self.armed = True
        self.timed_out = False
        self.entered, self.go, self.done = threading.Event(), threading.Event(), threading.Event()

    def _wait(self) -> None:
        self.entered.set()
        if not self.go.wait(10):
            self.timed_out = True

    def __call__(self, *a, **kw):
        with self.mu:
            hold = self.armed and bool(self.match(*a, **kw))
            if hold:
                self.armed = False
        if not hold:
            return self.fn(*a, **kw)
        try:
            if self.after:
                out = self.fn(*a, **kw)
                self._wait()
                return out
            self._wait()
            return self.fn(*a, **kw)
        finally:
            self.done.set()


def hold(test, obj, name, match, after=False) -> Hold:
    h = Hold(getattr(obj, name), match, after)
    setattr(obj, name, h)
    test.addCleanup(lambda: obj.__dict__.pop(name, None))
    test.addCleanup(lambda: test.assertFalse(h.timed_out, "a held call was never let go: the test did not run as meant"))
    return h


def background(fn, *a, **kw) -> threading.Thread:
    out = {}

    def run():
        try:
            out["result"] = fn(*a, **kw)
        except Exception as e:  # noqa: BLE001 - the thread's own outcome, checked by the test
            out["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.out = out
    t.start()
    return t


def to_other(h, keep=()) -> Message:
    """The account signs out; the service shows the other account's chats from now on (keep: groups it is in too). The
    other one's own message, for its first history."""
    h.c("logout")
    w = h.world
    with w.lock:
        w.me = OTHER
        for cid in [c for c in w.chats if c not in keep]:
            del w.chats[cid]
            w.history.pop(cid, None)
        w.people["user_example_e"] = "另一个账号的同事"
        w.add_chat(Chat(OWN, "group", "另一个账号的群", members=3))
    return w.make(OWN, "user_example_e", "另一个账号自己的消息")


def bound(h) -> bool:
    a = h.app.account
    return a is not None and a.id == OTHER.id


def agent_threads_ended(before: set):
    """No /agent start, 自动代答, 文档代答 or /stop thread started since before (the threads alive then) still runs."""
    return lambda: not any(t.name.startswith("openmate-agent-") and t not in before for t in threading.enumerate())


def seen_keys(h, text: str) -> list:
    return [r["key"] for r in h.app.store._all("SELECT key FROM seen") if text in r["key"]]


def signed_in(test, config=None) -> Harness:
    h = Harness(test, config=config)
    h.sign_in()
    wait_until(lambda: h.app._catchup_done.is_set(), what="the first catch-up")
    return h


def with_shared(h) -> None:
    h.world.add_chat(Chat(SHARED, "group", "两个账号都在的群", members=6))


class ReadAcrossSwitchTest(unittest.TestCase):
    """A history read asked for as one account, answered after the next signed in."""

    def test_a_catch_up_read_is_not_kept_for_the_next_account(self):
        h = signed_in(self)
        app = h.app
        late = h.world.make("chat_example_team", "user_example_a", "A 的补读正文")
        # the catch-up has read it and passed its link check: it is on its way into ingest when the switch comes
        held = hold(self, app, "ingest", lambda msgs, *a, **kw: any(m.id == late.id for m in msgs))
        app._request_catch_up(app._gen)
        self.assertTrue(held.entered.wait(5), "the catch-up read the new message")
        own = to_other(h)
        signing = background(h.sign_in)
        wait_until(lambda: bound(h), what="the other account bound")
        held.go.set()
        self.assertTrue(held.done.wait(5))
        signing.join(10)
        self.assertFalse(signing.is_alive())
        self.assertNotIn("error", signing.out)
        wait_until(lambda: app.store.message(OWN, own.id), what="the other account's own history")
        self.assertIsNone(app.store.message("chat_example_team", late.id), "A's text in the other account's store")

    def test_a_thread_page_read_is_not_kept_for_the_next_account(self):
        h = signed_in(self)
        app = h.app
        w = h.world
        w.add_chat(Chat("chat_example_a_only", "group", "只有 A 在的群", members=4))
        older = [w.make("chat_example_a_only", "user_example_a", "A 的旧消息 %d" % i, ago=600 - i) for i in range(3)]
        held = hold(self, app.backend, "history", lambda chat_id, *a, **kw: chat_id == "chat_example_a_only", after=True)
        page = background(h.c, "thread.page", thread="chat_example_a_only", limit=20)
        self.assertTrue(held.entered.wait(5), "the page asked the service for older messages")
        own = to_other(h)
        h.sign_in()
        wait_until(lambda: app.store.message(OWN, own.id), what="the other account's own history")
        held.go.set()
        page.join(10)
        self.assertFalse(page.is_alive())
        for m in older:
            self.assertIsNone(app.store.message("chat_example_a_only", m.id), "A's text in the other account's store")
        self.assertEqual(app.store.message_count(), 1, "only the other account's own message")
        # the page was asked of A's store: it says so instead of answering with the other one's
        self.assertNotIn("result", page.out)
        self.assertIn("刚换了", str(page.out.get("error")))


class AgentAcrossSwitchTest(unittest.TestCase):
    """A live line of the account before, stored before the switch, acted on after it."""

    def setUp(self):
        self.before = set(threading.enumerate())

    def config(self, **kw):
        c = {"agent": True, "agent_reply_header": " ", "agent_ack_reaction": "OK"}
        c.update(kw)
        return c

    def ended(self):
        wait_until(agent_threads_ended(self.before), what="the /agent threads to end")

    def assert_nothing_for_the_next(self, h, m):
        self.ended()
        self.assertEqual([r for r in h.app.store.runs_in_chat(m.chat_id) if r["request_id"] == m.id], [],
                         "A's request in the other account's runs")
        self.assertEqual(h.app.store.run_count(), 0)
        self.assertEqual(seen_keys(h, m.id), [], "A's request claimed in the other account's store")
        self.assertEqual(h.http.starts(), [], "a conversation started for A's request after the switch")
        self.assertEqual([r for r in h.world.reactions if r[1] == m.id], [])
        self.assertEqual(h.sent(), [], "something said in the user's name for A's request")

    def test_a_line_handed_to_agent_after_the_switch_is_not_claimed(self):
        h = signed_in(self, self.config())
        app = h.app
        held = hold(self, app.agent, "on_message", lambda row, *a, **kw: (row.get("text") or "").startswith("/agent"))
        box = {}
        live = background(lambda: box.setdefault("m", h.world.push("chat_example_team", "me", "/agent 帮我总结今天的讨论")))
        self.assertTrue(held.entered.wait(5), "the line was stored and handed to /agent")  # past the store's lock
        to_other(h)
        h.sign_in()
        held.go.set()
        self.assertTrue(held.done.wait(5))
        live.join(5)
        self.assert_nothing_for_the_next(h, box["m"])

    def test_a_request_claimed_before_the_switch_is_not_started_after_it(self):
        h = signed_in(self, self.config())
        app = h.app
        held = hold(self, app.agent, "_start_run", lambda *a, **kw: True)
        m = h.world.push("chat_example_team", "me", "/agent 帮我总结今天的讨论")
        self.assertTrue(held.entered.wait(5), "the request was claimed")
        self.assertEqual(app.store.run_count(), 1, "claimed in A's store")
        to_other(h)
        h.sign_in()
        held.go.set()
        self.assertTrue(held.done.wait(5))
        self.assert_nothing_for_the_next(h, m)

    def test_a_request_starting_across_the_switch_is_not_written_back(self):
        h = signed_in(self, self.config())
        app = h.app
        # the start has passed every check and is on its way to the hub when the switch comes
        held = hold(self, app.hub, "start_conversation", lambda *a, **kw: True)
        h.world.push("chat_example_team", "me", "/agent 帮我总结今天的讨论")
        self.assertTrue(held.entered.wait(5), "the conversation is being started")
        to_other(h)
        h.sign_in()
        held.go.set()
        self.assertTrue(held.done.wait(5))
        self.ended()
        self.assertEqual(app.store.run_count(), 0, "A's run written back into the other account's store")
        self.assertEqual(len(h.http.starts()), 1)
        sid = next(iter(h.http.sessions))
        wait_until(lambda: h.http.sessions[sid]["stopped"], what="the conversation nothing here can answer stopped")

    def test_a_request_waiting_for_the_hub_is_not_sent_after_the_switch(self):
        h = signed_in(self, self.config(agent_ack_reaction=""))
        app = h.app
        h.http.offline = 10 ** 6  # the hub cannot be reached: the start waits for it
        m = h.world.push("chat_example_team", "me", "/agent 帮我总结今天的讨论")
        wait_until(lambda: sum(1 for r in h.http.requests if r["path"] == "/api/v1/health") >= 2,
                   what="the start waiting for the hub")
        to_other(h)
        h.sign_in()
        h.http.offline = 0  # the hub is back
        self.assert_nothing_for_the_next(h, m)

    def test_an_auto_reply_taken_before_the_switch_is_not_started_after_it(self):
        h = signed_in(self, self.config(auto_reply=True))
        app = h.app
        app.store.autoreply_set("chat_example_team", True)
        held = hold(self, app.agent, "_auto_start", lambda *a, **kw: True)
        m = h.world.push("chat_example_team", "user_example_b", "@示例用户 这个接口谁负责？", at_me=True)
        self.assertTrue(held.entered.wait(5), "the @ was taken for 自动代答")
        to_other(h)
        h.sign_in()
        held.go.set()
        self.assertTrue(held.done.wait(5))
        self.assert_nothing_for_the_next(h, m)
        self.assertEqual(app.store.people_kinds(["user_example_b"]), {}, "who A's sender is, in the other one's store")

    def test_who_an_auto_reply_sender_is_is_not_kept_for_the_next_account(self):
        h = signed_in(self, self.config(auto_reply=True))
        app = h.app
        h.world.people["user_example_f"] = "示例同事己"
        app.store.autoreply_set("chat_example_team", True)
        app.people._ask = lambda ids, epoch=None: None  # no draw asks behind: 自动代答 itself asks the service (and is held there)
        self.addCleanup(lambda: app.people.__dict__.pop("_ask", None))
        # 自动代答 asks the service who the sender is; the switch comes while it answers
        held = hold(self, app.backend, "people_kinds",
                    lambda ids, *a, **kw: threading.current_thread().name == "openmate-agent-auto", after=True)
        m = h.world.push("chat_example_team", "user_example_f", "@示例用户 这个接口谁负责？", at_me=True)
        self.assertTrue(held.entered.wait(5), "who the sender is was asked")
        to_other(h)
        h.sign_in()
        held.go.set()
        self.assertTrue(held.done.wait(5))
        self.assert_nothing_for_the_next(h, m)
        self.assertEqual(app.store.people_kinds(["user_example_f"]), {}, "who A's sender is, in the other one's store")

    def next_ones_run(self, h, text):
        """The other account asks /agent in the shared group's topic: its run, running in its own conversation."""
        mine = h.world.push(SHARED, "me", text, root_id=TOPIC)
        run = wait_until(lambda: next((r for r in h.app.store.runs_in_chat(SHARED)
                                       if r["request_id"] == mine.id and r["state"] == "running"), None),
                         what="the other account's run")
        return run

    def test_a_stop_of_the_account_before_does_not_stop_the_next_ones_run(self):
        h = signed_in(self, self.config(agent_ack_reaction=""))
        app = h.app
        with_shared(h)
        held = hold(self, app.agent, "_on_stop", lambda *a, **kw: True)
        stop = h.world.push(SHARED, "me", "/stop", root_id=TOPIC)
        self.assertTrue(held.entered.wait(5), "A's /stop was taken")
        to_other(h, keep=(SHARED,))
        h.sign_in()
        theirs = self.next_ones_run(h, "/agent 另一个账号的请求")
        held.go.set()
        self.assertTrue(held.done.wait(5))
        self.ended()
        self.assertEqual(app.store.run_get(theirs["id"])["state"], "running", "A's /stop stopped the other one's run")
        self.assertFalse(h.http.sessions[theirs["session_id"]]["stopped"])
        self.assertEqual(seen_keys(h, stop.id), [])

    def test_a_stop_choosing_its_run_holds_off_the_next_account(self):
        h = signed_in(self, self.config(agent_ack_reaction=""))
        app = h.app
        with_shared(h)
        held = hold(self, app.agent, "_target", lambda *a, **kw: True)
        h.world.push(SHARED, "me", "/stop", root_id=TOPIC)
        self.assertTrue(held.entered.wait(5), "A's /stop is choosing its run")
        self.assertTrue(app._wipe_mu.locked(), "A's /stop chooses among A's runs with the switch held off")
        to_other(h, keep=(SHARED,))
        binding = hold(self, app, "_bind_account", lambda *a, **kw: True)
        binding.go.set()  # not held: only seen
        signing = background(h.sign_in)
        self.assertTrue(binding.entered.wait(5), "the other account's sign-in reached the store")
        self.assertFalse(binding.done.wait(0.3), "the store changed hands while A's /stop was choosing among A's runs")
        self.assertFalse(bound(h))
        held.go.set()
        signing.join(10)
        self.assertFalse(signing.is_alive())
        self.assertNotIn("error", signing.out)
        theirs = self.next_ones_run(h, "/agent 另一个账号的请求")
        self.ended()
        self.assertEqual(app.store.run_get(theirs["id"])["state"], "running")

    def test_a_start_of_the_account_before_does_not_go_on_in_the_next_ones_conversation(self):
        h = signed_in(self, self.config(agent_ack_reaction=""))
        app = h.app
        with_shared(h)
        # A's start looks for an earlier run of the topic to go on in; the switch comes just before
        held = hold(self, app.agent, "_previous", lambda *a, **kw: True)
        h.world.push(SHARED, "me", "/agent A 的请求", root_id=TOPIC)
        self.assertTrue(held.entered.wait(5), "A's request is starting")
        to_other(h, keep=(SHARED,))
        h.sign_in()
        theirs = self.next_ones_run(h, "/agent 另一个账号的请求")
        held.go.set()
        self.assertTrue(held.done.wait(5))
        self.ended()
        self.assertEqual(app.store.run_get(theirs["id"])["state"], "running", "A's start took over the other one's run")
        said = [it.get("text") or "" for it in h.http.sessions[theirs["session_id"]]["items"] if it["role"] == "user"]
        self.assertEqual(len(said), 1, "A's request went on in the other account's conversation")
        self.assertEqual(len(h.http.starts()), 1)
        self.assertEqual(app.store.run_count(), 1)


    def test_a_request_going_on_in_a_conversation_is_not_sent_after_the_switch(self):
        h = signed_in(self, self.config(agent_ack_reaction=""))
        app = h.app
        with_shared(h)
        h.world.push(SHARED, "me", "/agent 第一个请求", root_id=TOPIC)
        first = wait_until(lambda: next((r for r in app.store.runs_in_chat(SHARED) if r["state"] == "running"), None),
                           what="A's first run")
        # the next request of the topic goes on in that conversation; the hub is asked where it stands first
        held = hold(self, app.agent, "_latest_seq", lambda *a, **kw: True)
        h.world.push(SHARED, "me", "/agent 第二个请求", root_id=TOPIC)
        self.assertTrue(held.entered.wait(5), "the second request asks the hub before it goes on")
        to_other(h)
        h.sign_in()
        held.go.set()
        self.assertTrue(held.done.wait(5))
        self.ended()
        said = [it for it in h.http.sessions[first["session_id"]]["items"] if it["role"] == "user"]
        self.assertEqual(len(said), 1, "A's request went to the hub after the other account signed in")

    def test_a_reply_whose_run_is_wiped_meanwhile_is_refused_plainly(self):
        h = signed_in(self, self.config(agent_ack_reaction=""))
        app = h.app
        m = h.world.push("chat_example_team", "me", "/agent 帮我总结今天的讨论")
        run = wait_until(lambda: next((r for r in app.store.runs_in_chat(m.chat_id) if r["state"] == "running"), None),
                         what="the run")
        held = hold(self, app.agent, "_clean_answer", lambda *a, **kw: True)  # the reply's first look at its text
        reply = background(app.agent.reply_tool, run["id"], "今天讨论了接口文档")
        self.assertTrue(held.entered.wait(5))
        to_other(h)
        h.sign_in()
        held.go.set()
        reply.join(5)
        self.assertFalse(reply.is_alive())
        self.assertIsInstance(reply.out.get("error"), RPCError, reply.out)
        self.assertEqual([s for s in h.sent() if s["chat"] == m.chat_id], [])

    def test_a_recall_from_the_account_before_does_not_stop_the_current_run(self):
        h = signed_in(self, self.config(agent_ack_reaction=""))
        app = h.app
        with_shared(h)
        old_epoch = app.account_epoch()
        to_other(h, keep=(SHARED,))
        h.sign_in()
        run = self.next_ones_run(h, "/agent 另一个账号的请求")
        app.agent.on_recalled(SHARED, run["request_id"], old_epoch)
        self.assertEqual(app.store.run_get(run["id"])["state"], "running")
        self.assertFalse(app.store.seen_has("recalled:%s:%s" % (SHARED, run["request_id"])))
        self.assertEqual(h.http.posts("/stop"), [])

    def test_a_document_reply_asked_across_the_switch_is_not_sent(self):
        h = signed_in(self, self.config(auto_reply_docs=True, agent_ack_reaction=""))
        app = h.app
        app._doc_mention(DocMention(id="doc_mention_a", doc_url="https://example.feishu.cn/docx/ExampleDoc003",
                                    doc_title="示例文档", comment_id="c1", from_id="user_example_a",
                                    from_name="示例同事甲", text="@示例用户 这里怎么改？", at=time.time()), account=ME.id)
        run = wait_until(lambda: next((r for r in app.store.runs_in_state(["running"]) if r.get("kind") == "doc"), None),
                         what="the document run")
        held = hold(self, app.backend, "doc_writer_name", lambda *a, **kw: kw.get("fresh"))
        reply = background(app.agent.reply_tool, run["id"], "改成第二种写法就好")
        self.assertTrue(held.entered.wait(5), "who lark-cli writes as is being asked")
        to_other(h)
        h.sign_in()
        held.go.set()
        reply.join(5)
        self.assertFalse(reply.is_alive())
        self.assertEqual(h.world.doc_replies, [], "the other account answered A's document comment")
        self.assertIsInstance(reply.out.get("error"), RPCError, reply.out)


class NamesAndChatsAcrossSwitchTest(unittest.TestCase):
    def test_names_learnt_for_the_account_before_are_not_kept(self):
        h = signed_in(self)
        app = h.app
        h.world.people["user_example_d"] = "A 眼里的丁"
        now = time.time()
        unnamed = Message(id="msg_example_unnamed", chat_id="chat_example_team", position=900, sender_id="user_example_d",
                          sender_name="", text="没带名字的消息", at=now)
        named = Message(id="msg_example_named", chat_id="chat_example_team", position=901, sender_id="user_example_named",
                        sender_name="A 那边的名字", text="带了名字的消息", at=now)
        # the service is asked who sent the first; the switch comes while it answers
        held = hold(self, app.backend, "user_name", lambda uid, *a, **kw: uid == "user_example_d", after=True)
        reading = background(app.ingest, [unnamed, named], False)
        self.assertTrue(held.entered.wait(5), "the name was asked for")
        to_other(h)
        h.sign_in()
        h.world.people["user_example_d"] = "B 眼里的丁"
        held.go.set()
        reading.join(5)
        self.assertEqual(reading.out.get("result"), [])
        self.assertEqual(app.store.name("user_example_named"), "", "a name A's message carried, in the other's store")
        self.assertEqual(app.store.name("user_example_d"), "", "a name looked up for A, in the other's store")
        self.assertNotIn("user_example_d", app._names, "a name looked up for A, in the names the next one uses")
        # the other account's next message from that person, without a name: its own service is asked
        nxt = Message(id="msg_example_b_next", chat_id=OWN, position=5, sender_id="user_example_d", sender_name="",
                      text="另一个账号收到的消息", at=time.time())
        app.ingest([nxt], live=False)
        self.assertEqual(app.store.message(OWN, nxt.id)["sender_name"], "B 眼里的丁")

    def test_a_name_learnt_later_is_not_undone_by_an_older_read(self):
        h = signed_in(self)  # one account, no switch: names are kept in the order they are learnt
        app = h.app
        h.world.people["user_example_v"] = "示例同事戊"
        now = time.time()
        old_name = Message(id="msg_example_u_old", chat_id="chat_example_team", position=910, sender_id="user_example_u",
                           sender_name="旧名字", text="一条旧消息", at=now - 60)
        unnamed = Message(id="msg_example_v", chat_id="chat_example_team", position=911, sender_id="user_example_v",
                          sender_name="", text="没带名字的消息", at=now - 50)
        held = hold(self, app.backend, "user_name", lambda uid, *a, **kw: uid == "user_example_v", after=True)
        reading = background(app.ingest, [old_name, unnamed], False)
        self.assertTrue(held.entered.wait(5), "the older read waits on a name")
        new_name = Message(id="msg_example_u_new", chat_id="chat_example_team", position=912, sender_id="user_example_u",
                           sender_name="新名字", text="一条新消息", at=now)
        app.ingest([new_name], live=False)  # meanwhile a newer message says the person's new name
        held.go.set()
        reading.join(5)
        self.assertEqual(len(reading.out.get("result") or []), 2)
        self.assertEqual(app.store.name("user_example_u"), "新名字", "an older read put back the name before")
        self.assertEqual(app._names.get("user_example_u"), "新名字")

    def test_a_name_learnt_while_the_same_one_was_asked_stays(self):
        h = signed_in(self)  # one account
        app = h.app
        h.world.people["user_example_w"] = "旧名字"
        unnamed = Message(id="msg_example_w_old", chat_id="chat_example_team", position=920, sender_id="user_example_w",
                          sender_name="", text="没带名字的消息", at=time.time() - 60)
        held = hold(self, app.backend, "user_name", lambda uid, *a, **kw: uid == "user_example_w", after=True)
        reading = background(app.ingest, [unnamed], False)
        self.assertTrue(held.entered.wait(5), "the name is being asked for")
        named = Message(id="msg_example_w_new", chat_id="chat_example_team", position=921, sender_id="user_example_w",
                        sender_name="新名字", text="带了新名字的消息", at=time.time())
        app.ingest([named], live=False)
        held.go.set()
        reading.join(5)
        self.assertEqual(app.store.name("user_example_w"), "新名字", "the name asked before put back the old one")
        self.assertEqual(app._names.get("user_example_w"), "新名字")
        self.assertEqual(app.store.message("chat_example_team", unnamed.id)["sender_name"], "新名字")

    def test_a_chat_asked_about_across_the_switch_says_so(self):
        h = signed_in(self)
        app = h.app
        h.world.add_chat(Chat("chat_example_a_new", "group", "A 新进的群", members=3))
        held = hold(self, app.backend, "chat_info", lambda chat_id, *a, **kw: chat_id == "chat_example_a_new", after=True)
        page = background(h.c, "thread.page", thread="chat_example_a_new", limit=20)
        self.assertTrue(held.entered.wait(5), "the page asked the service about the chat")
        to_other(h)
        h.sign_in()
        held.go.set()
        page.join(10)
        self.assertFalse(page.is_alive())
        self.assertIsNone(app.store.get_chat("chat_example_a_new"))
        self.assertIn("刚换了", str(page.out.get("error")), "not \"no such chat\": the account changed")

    def test_a_late_read_does_not_bring_back_a_group_the_next_account_left(self):
        h = signed_in(self)
        app = h.app
        w = h.world
        with_shared(h)
        a_line = w.make(SHARED, "user_example_a", "A 读到的消息", at=time.time() + 60)  # said after the other one leaves
        held = hold(self, app.backend, "history", lambda chat_id, *a, **kw: chat_id == SHARED, after=True)
        page = background(h.c, "thread.page", thread=SHARED, limit=20)
        self.assertTrue(held.entered.wait(5), "the page asked the service for the group's messages")
        to_other(h, keep=(SHARED,))
        with w.lock:
            w.history[SHARED] = []
        b_line = w.make(SHARED, "user_example_e", "另一个账号在这个群里的消息", ago=30)
        h.sign_in()
        wait_until(lambda: app.store.message(SHARED, b_line.id), what="the other account's history of the group")
        app._chat_gone(SHARED)  # the other account leaves the group
        self.assertTrue(app.store.is_gone(SHARED))
        held.go.set()
        page.join(10)
        self.assertFalse(page.is_alive())
        self.assertTrue(app.store.is_gone(SHARED), "A's later message brought back a group the other account left")
        self.assertIsNone(app.store.message(SHARED, a_line.id))
        self.assertIn("刚换了", str(page.out.get("error")))

    def test_a_group_the_account_before_left_is_not_gone_for_the_next(self):
        h = signed_in(self)
        app = h.app
        with_shared(h)
        to_other(h, keep=(SHARED,))
        h.world.make(SHARED, "user_example_e", "另一个账号在这个群里的消息", ago=30)
        h.sign_in()
        wait_until(lambda: app.store.get_chat(SHARED), what="the shared group in the other account's store")
        app.store.autoreply_set(SHARED, True)
        app._chat_gone(SHARED, account=ME.id)  # heard on A's link, handed in after the other one signed in
        self.assertFalse(app.store.is_gone(SHARED), "A leaving the group hid it from the other account")
        self.assertIn(SHARED, app.store.autoreply_chats(), "A leaving the group turned the other one's 自动代答 off")
        app._chat_gone(SHARED, account=OTHER.id)
        self.assertTrue(app.store.is_gone(SHARED), "the other account's own leaving is kept")

    def test_a_document_mention_heard_for_the_account_before_is_not_taken(self):
        h = signed_in(self, {"agent": True, "auto_reply_docs": True, "agent_reply_header": " "})
        app = h.app
        to_other(h)
        h.sign_in()
        before = set(threading.enumerate())

        def mention(mid):
            return DocMention(id=mid, doc_url="https://example.feishu.cn/docx/ExampleDoc002", doc_title="示例文档",
                              comment_id="c1", from_id="user_example_e", from_name="另一个账号的同事",
                              text="@另一个示例用户 这里怎么改？", at=time.time())

        app._doc_mention(mention("doc_mention_a"), account=ME.id)  # heard on A's link, handed in late
        wait_until(agent_threads_ended(before), what="nothing started")
        self.assertEqual(seen_keys(h, "doc_mention_a"), [], "A's document mention claimed in the other one's store")
        self.assertEqual(app.store.run_count(), 0)
        app._doc_mention(mention("doc_mention_b"), account=OTHER.id)  # the other account's own is answered
        wait_until(lambda: app.store.run_count() == 1, what="the other account's own document mention")


class SignInTest(unittest.TestCase):
    def reply_during_hand_over(self, h, to):
        """An inbox reply asked while the store changes hands to account to: (what the reply answered, what was sent
        meanwhile)."""
        app = h.app
        binding = hold(self, app, "_bind_account", lambda *a, **kw: True)
        h.world.me = to
        signing = background(h.sign_in)
        self.assertTrue(binding.entered.wait(5), "the adapter answers as the account signed in; the store is A's")
        try:
            reply = background(h.c, "inbox.reply", thread="chat_example_team", text="A 那边想回的话", client_id="c-1")
            reply.join(0.3)
            self.assertTrue(reply.is_alive(), "the reply did not wait for the hand-over")
            sent_meanwhile = list(h.sent())
        finally:
            binding.go.set()
        signing.join(10)
        self.assertNotIn("error", signing.out)
        reply.join(10)
        self.assertFalse(reply.is_alive())
        return reply.out, sent_meanwhile

    def test_a_reply_asked_as_the_account_before_waits_and_is_not_sent_as_the_next(self):
        h = signed_in(self)  # A stays signed in; the user scans the other account's QR
        out, meanwhile = self.reply_during_hand_over(h, OTHER)
        self.assertEqual(meanwhile, [], "something went out while the store changed hands")
        self.assertIn("刚换了", str(out.get("error")), out)
        self.assertEqual(h.sent(), [], "A's reply went out as the other account")

    def test_a_reply_waits_for_the_same_account_scanned_again_and_goes(self):
        h = signed_in(self)  # the same account scans its QR again
        out, meanwhile = self.reply_during_hand_over(h, ME)
        self.assertEqual(meanwhile, [])
        self.assertNotIn("error", out, out)
        self.assertEqual([s["text"] for s in h.sent()], ["A 那边想回的话"], "the same account's reply was lost")

    def test_a_store_nobody_had_is_claimed_by_the_first_account(self):
        h = Harness(self)  # a new store; a restore of another account has not come back yet
        app = h.app
        stray = Message(id="msg_example_stray", chat_id="chat_example_team", position=990, sender_id="user_example_a",
                        sender_name="示例同事甲", text="还没有主人时读进来的", at=time.time())
        app.ingest([stray], live=False)  # read by a session not bound to the store
        before = app.account_epoch()
        app._bind_account(OTHER)
        self.assertEqual(app.store.meta_get("account_id"), OTHER.id, "a later restore of another would keep its data")
        self.assertIsNone(app.store.message("chat_example_team", stray.id), "what nobody's session read kept for it")
        self.assertGreater(app.account_epoch(), before, "a read on its way when it was claimed is kept for it")

    def test_a_rotation_of_a_sign_in_not_sealed_is_never_written(self):
        h = signed_in(self)  # A stays signed in; the user scans the other account's QR
        app = h.app
        a_creds = secret_json(h.hub.secrets[CID])
        paused, go, first = threading.Event(), threading.Event(), [True]

        def sealing(creds):  # the other account's sign-in is sealed, and fails; nothing after it is held
            if first[0]:
                first[0] = False
                paused.set()
                go.wait(10)
                raise HubSideError("unavailable", "secrets unavailable")

        h.hub.save_hook = sealing
        h.world.me = OTHER
        signing = background(h.sign_in)
        try:
            self.assertTrue(paused.wait(5))
            app._credentials_rotated({"fixture_token": "rotated", "user": OTHER.id})  # its session refreshed meanwhile
        finally:
            go.set()
        signing.join(10)
        self.assertIn("error", signing.out)
        wait_until(lambda: app._creds_writer is None, what="the rotation writer to end")
        self.assertEqual(secret_json(h.hub.secrets[CID]), a_creds, "the failed sign-in's rotation sealed over A's")

    def test_a_rotation_while_the_store_changes_hands_waits_for_the_sealing(self):
        h = signed_in(self)  # A stays signed in; the user scans the other account's QR
        app = h.app
        a_creds = secret_json(h.hub.secrets[CID])
        binding = hold(self, app, "_bind_account", lambda *a, **kw: True)
        first = [True]

        def sealing(creds):  # the other account's sign-in cannot be sealed
            if first[0]:
                first[0] = False
                raise HubSideError("unavailable", "secrets unavailable")

        h.world.me = OTHER
        signing = background(h.sign_in)
        self.assertTrue(binding.entered.wait(5), "the adapter answers as the other account; the store is A's still")
        try:
            app._credentials_rotated({"fixture_token": "rotated", "user": OTHER.id})  # its session refreshed meanwhile
            time.sleep(0.2)  # the background writer had its chance
            self.assertEqual(secret_json(h.hub.secrets[CID]), a_creds, "the other account's rotation sealed before it")
            h.hub.save_hook = sealing
        finally:
            binding.go.set()
        signing.join(10)
        self.assertIn("error", signing.out)
        wait_until(lambda: app._creds_writer is None, what="the rotation writer to end")
        self.assertEqual(secret_json(h.hub.secrets[CID]), a_creds, "the failed sign-in's rotation sealed over A's")

    def test_a_send_that_reaches_the_adapter_during_the_hand_over_is_refused(self):
        h = signed_in(self)  # A stays signed in; the user scans the other account's QR
        app = h.app
        binding = hold(self, app, "_bind_account", lambda *a, **kw: True)
        h.world.me = OTHER
        signing = background(h.sign_in)
        self.assertTrue(binding.entered.wait(5), "the adapter answers as the other account; the store is A's")
        try:
            with self.assertRaises(SendRejected):  # an action asked as A (it checked nothing of the hand-over)
                app.send_as_user("chat_example_team", "A 那边要发的话")
        finally:
            binding.go.set()
        signing.join(10)
        self.assertNotIn("error", signing.out)
        self.assertEqual(h.sent(), [])

    def test_a_rotation_of_a_sign_in_not_bound_is_never_written(self):
        h = signed_in(self)
        app = h.app
        a_creds = secret_json(h.hub.secrets[CID])
        binding = hold(self, app, "_bind_account", lambda *a, **kw: True)

        def wipe_fails(owner=""):
            raise RuntimeError("disk full")

        app.store.wipe_account_data = wipe_fails  # the store cannot be taken over
        self.addCleanup(lambda: app.store.__dict__.pop("wipe_account_data", None))
        h.world.me = OTHER
        signing = background(h.sign_in)
        self.assertTrue(binding.entered.wait(5))
        try:
            app._credentials_rotated({"fixture_token": "rotated", "user": OTHER.id})  # its session refreshed meanwhile
        finally:
            binding.go.set()
        signing.join(10)
        self.assertIn("error", signing.out)
        wait_until(lambda: app._creds_writer is None, what="the rotation writer to end")
        self.assertEqual(secret_json(h.hub.secrets[CID]), a_creds, "a sign-in not bound sealed its rotation over A's")

    def test_the_name_kept_for_a_start_waits_for_the_seal(self):
        h = signed_in(self)
        app = h.app
        paused, go = threading.Event(), threading.Event()

        def sealing(creds):
            paused.set()
            go.wait(10)

        h.hub.save_hook = sealing
        h.world.me = OTHER
        signing = background(h.sign_in)
        try:
            self.assertTrue(paused.wait(5))
            self.assertNotEqual(app.store.meta_get("account_name"), OTHER.name,
                                "a sign-in not sealed yet would be the account a start shows")
            self.assertEqual(app.account.id, OTHER.id)
        finally:
            go.set()
        signing.join(10)
        self.assertNotIn("error", signing.out)
        self.assertEqual(app.store.meta_get("account_name"), OTHER.name)

    def test_the_next_accounts_own_document_mention_while_it_is_sealed_is_answered(self):
        h = signed_in(self, {"agent": True, "auto_reply_docs": True, "agent_reply_header": " "})
        app = h.app
        paused, go = threading.Event(), threading.Event()

        def sealing(creds):
            paused.set()
            go.wait(10)

        h.hub.save_hook = sealing
        h.world.me = OTHER  # scanned while A stays signed in
        h.world.people["user_example_e"] = "另一个账号的同事"
        signing = background(h.sign_in)
        try:
            self.assertTrue(paused.wait(5), "the other account's sign-in is being sealed")
            app._doc_mention(DocMention(id="doc_mention_b_early", doc_url="https://example.feishu.cn/docx/ExampleDoc004",
                                        doc_title="示例文档", comment_id="c1", from_id="user_example_e",
                                        from_name="另一个账号的同事", text="@另一个示例用户 看下这里", at=time.time()),
                             account=OTHER.id)
            wait_until(lambda: app.store.run_count() == 1, what="the other account's own document mention taken")
        finally:
            go.set()
        signing.join(10)
        self.assertNotIn("error", signing.out)

    def test_the_next_accounts_own_group_left_while_it_is_sealed_is_kept(self):
        h = signed_in(self)
        app = h.app
        with_shared(h)
        to_other(h, keep=(SHARED,))
        paused, go = threading.Event(), threading.Event()

        def sealing(creds):
            paused.set()
            go.wait(10)

        h.hub.save_hook = sealing
        signing = background(h.sign_in)
        try:
            self.assertTrue(paused.wait(5), "the other account's sign-in is being sealed")
            app.store.upsert_chat(Chat(SHARED, "group", "两个账号都在的群", members=6))
            app._chat_gone(SHARED, account=OTHER.id)  # the other account leaves the group meanwhile (its own link)
            self.assertTrue(app.store.is_gone(SHARED), "the other account's own leaving was dropped")
        finally:
            go.set()
        signing.join(10)
        self.assertNotIn("error", signing.out)


    def test_a_sign_in_that_fails_while_its_link_reconnected_leaves_nothing_running(self):
        h = signed_in(self)  # A stays signed in; the user scans the other account's QR
        app = h.app
        paused, go = threading.Event(), threading.Event()

        def sealing(creds):  # the other account's sign-in is being sealed, and then it cannot be
            paused.set()
            go.wait(10)
            raise HubSideError("unavailable", "secrets unavailable")

        h.hub.save_hook = sealing
        h.world.me = OTHER
        late = h.world.listener
        signing = background(h.sign_in)
        try:
            self.assertTrue(paused.wait(5), "the other account's sign-in is being sealed")
            h.world.drop()  # the link before drops and connects again, as the other account by now
            wait_until(lambda: h.world.listener is not None and h.world.listener is not late, what="the reconnect")
            again = h.world.listener
        finally:
            go.set()
        signing.join(10)
        self.assertIn("error", signing.out, "the sign-in was not sealed: it failed")
        h.hub.save_hook = None
        line = Message(id="msg_example_after_failure", chat_id="chat_example_team", position=980,
                       sender_id="user_example_e", sender_name="另一个账号的同事", text="失败之后到的消息", at=time.time())
        again(line)  # a line the reconnected link hands over after the failure
        self.assertIsNone(app.store.message("chat_example_team", line.id), "a link kept running after the failure")
        self.assertNotEqual(h.status()["state"], "ok")
        p = h.c("login.step", step="qr")
        self.assertNotIn("done", p, "a poll after the failure said the sign-in was done")

    def test_the_store_is_the_next_accounts_while_the_sealing_lock_is_busy(self):
        h = signed_in(self)  # A stays signed in; the user scans the other account's QR
        app = h.app
        before = app.account_epoch()
        busy, free = threading.Event(), threading.Event()

        def sealing_lock():  # a rotation of A's credentials is being sealed (it holds the lock across the hub call)
            with app._creds_mu:
                busy.set()
                free.wait(10)

        real = app.backend.login_finish

        def finish(state):
            out = real(state)  # the adapter answers as the other account from here
            threading.Thread(target=sealing_lock, daemon=True).start()
            busy.wait(5)
            return out

        app.backend.login_finish = finish
        self.addCleanup(lambda: app.backend.__dict__.pop("login_finish", None))
        h.world.me = OTHER
        signing = background(h.sign_in)
        try:
            wait_until(lambda: app.account_epoch() > before, what="the store bound while the lock is busy")
            self.assertEqual(app.account.id, OTHER.id, "the account said is the one the store is bound to, never A")
        finally:
            free.set()
        signing.join(10)
        self.assertNotIn("error", signing.out)
        self.assertTrue(bound(h))

    def test_a_sign_in_not_sealed_leaves_nothing_of_it_for_the_account_before(self):
        h = signed_in(self)
        app = h.app
        h.world.me = OTHER  # the other account's QR, scanned while A stays signed in
        h.hub.secrets_fail = True  # its credentials cannot be sealed
        with self.assertRaises(Exception):
            h.sign_in()
        self.assertEqual(app.store.meta_get("account_id"), OTHER.id, "the store, wiped for the other, still says A")
        # a read of the other account's while it was taken in
        theirs = Message(id="msg_example_b_handoff", chat_id="chat_example_team", position=960, sender_id="user_example_e",
                         sender_name="另一个账号的同事", text="另一个账号的消息", at=time.time())
        app.ingest([theirs], live=False)
        h.hub.secrets_fail = False
        h.world.me = ME
        h2 = h.restart()  # A's saved sign-in comes back
        h2.wait_ok()
        self.assertIsNone(h2.app.store.message("chat_example_team", theirs.id), "the other account's message kept for A")

    def test_a_late_line_of_a_link_before_a_reconnect_is_the_links_own(self):
        h = signed_in(self)  # A stays signed in; the user scans the other account's QR
        app = h.app
        late = h.world.listener  # A's link: a line it read may still be handed over after it ended
        paused, go = threading.Event(), threading.Event()

        def sealing(creds):  # the other account's sign-in is being sealed (the hub takes its time)
            paused.set()
            go.wait(10)

        h.hub.save_hook = sealing
        h.world.me = OTHER
        signing = background(h.sign_in)
        try:
            self.assertTrue(paused.wait(5), "the other account's sign-in is being sealed")
            h.world.drop()  # A's link drops meanwhile and connects again (its generation still current)
            wait_until(lambda: h.world.listener is not None and h.world.listener is not late, what="the reconnect")
            line = Message(id="msg_example_a_late_line", chat_id="chat_example_team", position=970,
                           sender_id="user_example_a", sender_name="示例同事甲", text="A 的连接迟到的消息", at=time.time())
            late(line)  # handed over late by the connection before
        finally:
            go.set()
        signing.join(10)
        self.assertNotIn("error", signing.out)
        self.assertIsNone(app.store.message("chat_example_team", line.id), "A's line kept for the other account")

    def test_the_store_is_the_next_accounts_before_its_sign_in_is_sealed(self):
        h = signed_in(self)
        app = h.app
        before = app.account_epoch()
        seen = {}

        def sealing(creds):  # the hub is saving the other account's sign-in (it may take a while)
            seen.setdefault("epoch", app.account_epoch())
            seen.setdefault("messages", app.store.message_count())

        to_other(h)
        h.hub.save_hook = sealing
        h.sign_in()
        self.assertGreater(seen["epoch"], before, "reads in the meantime would be kept for the account before")
        self.assertEqual(seen["messages"], 0, "the account before's messages still there while the next one's sealed")


class StoreAcrossSwitchTest(unittest.TestCase):
    def test_a_run_pruned_then_wiped_is_never_written_back(self):
        d = tempfile.mkdtemp(prefix="openmate-test-")
        self.addCleanup(shutil.rmtree, d, True)
        store = Store(os.path.join(d, "openmate.db"))
        self.addCleanup(store.close)
        store.run_put({"id": "omr_example_pruned", "chat_id": "chat_example_team", "request_id": "msg_example_1",
                       "thread_key": "msg_example_1", "state": "done", "created": time.time() - 30})
        held = store.run_get("omr_example_pruned")  # a reply of the account before still holds it
        store.runs_prune(time.time() - 3600, 0)  # kept no more (done, beyond the most kept)
        self.assertIsNone(store.run_get("omr_example_pruned"))
        store.wipe_account_data()  # then another account signs in
        self.assertTrue(store.run_wiped("omr_example_pruned"))
        self.assertFalse(store.run_put(held))
        self.assertIsNone(store.run_get("omr_example_pruned"))


class PeopleAcrossSwitchTest(unittest.TestCase):
    def test_who_is_asked_about_for_the_account_before_is_not_kept_for_the_next(self):
        d = tempfile.mkdtemp(prefix="openmate-test-")
        self.addCleanup(shutil.rmtree, d, True)
        store = Store(os.path.join(d, "openmate.db"))
        self.addCleanup(store.close)
        gen = [1]

        class Backend:
            def people_kinds(self, ids):
                return {i: "user" for i in ids}

        import contextlib

        class App:  # just what People reads of the connector
            def __init__(self):
                self.store, self.backend, self.signed_in = store, Backend(), True

            def account_epoch(self):
                return gen[0]

            def owns(self, epoch, account="", owner="", settle=True):
                return epoch is None or epoch == gen[0]

            @contextlib.contextmanager
            def owned(self, epoch, account="", owner=""):
                yield self.owns(epoch)

            def mark_changed(self, *scopes):
                pass

        later = []
        people = People(App(), spawn=later.append)
        people._can = lambda: True
        people.bots(["user_example_a_contact"])  # A's page draws someone not known yet: asked about behind
        gen[0] = 2  # another account signs in before the question goes
        for run in later:
            run()
        self.assertEqual(store.people_kinds(["user_example_a_contact"]), {}, "asked for A, kept for the next")

    def test_a_draw_of_the_account_before_queues_nothing_for_the_next(self):
        import contextlib

        gen = [2]  # the other account is bound already

        class App:
            store, signed_in = None, True

            def account_epoch(self):
                return gen[0]

            def owns(self, epoch, account="", owner="", settle=True):
                return epoch is None or epoch == gen[0]

            @contextlib.contextmanager
            def owned(self, epoch, account="", owner=""):
                yield self.owns(epoch)

            def mark_changed(self, *scopes):
                pass

        later = []
        people = People(App(), spawn=later.append)
        people._can = lambda: True
        people._epoch = lambda: 1  # the draw began for the account before; forget ran before it queued them
        people._ask({"user_example_shared"})
        self.assertEqual((people._want, people._asking), ({}, set()), "queued for the next account, never let go")


if __name__ == "__main__":
    unittest.main()
