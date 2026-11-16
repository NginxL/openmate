"""The LarkAgentX backend against stand-ins of larkx, requests and websockets (tests/fakelarkx, no network): message
mapping, the QR sign-in, restoring and expiry, sends that surely failed versus ones that may have gone out, the proxy,
the live link, reads and media."""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
EXT_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(EXT_DIR))
for p in (os.path.join(ROOT, "core"), EXT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from openmate_core.backend import BackendError, CredentialsExpired, SendRejected  # noqa: E402
from openmate_feishu import larkagentx, launch  # noqa: E402

FAKES = os.path.join(HERE, "fakelarkx")
SHADOWED = ("larkx", "requests", "websockets", "dotenv", "loguru")
_saved = {}
_tmp = ""
S = None  # larkx._fake.STATE
R = None  # the fake requests.exceptions
W = None  # the fake websockets.exceptions


def setUpModule():
    """The fakes go first on sys.path, as a git checkout of LarkAgentX at the default commit (for pin.verify)."""
    global _tmp, S, R, W
    _tmp = tempfile.mkdtemp(prefix="openmate-larkx-")
    checkout = os.path.join(_tmp, "checkout")
    site = os.path.join(_tmp, "site")
    shutil.copytree(os.path.join(FAKES, "larkx"), os.path.join(checkout, "larkx"))
    os.makedirs(os.path.join(checkout, ".git"))
    with open(os.path.join(checkout, ".git", "HEAD"), "w") as f:
        f.write(larkagentx.DEFAULT_COMMIT + "\n")
    for name in ("requests", "websockets"):
        shutil.copytree(os.path.join(FAKES, name), os.path.join(site, name))
    for name in list(sys.modules):
        if name.split(".")[0] in SHADOWED:
            _saved[name] = sys.modules.pop(name)
    sys.path[:0] = [checkout, site]
    import larkx._fake
    import requests.exceptions
    import websockets.exceptions

    S, R, W = larkx._fake.STATE, requests.exceptions, websockets.exceptions


def tearDownModule():
    for p in (os.path.join(_tmp, "checkout"), os.path.join(_tmp, "site")):
        while p in sys.path:
            sys.path.remove(p)
    for name in list(sys.modules):
        if name.split(".")[0] in SHADOWED:
            del sys.modules[name]
    sys.modules.update(_saved)
    shutil.rmtree(_tmp, True)


from larkx_fixtures import ME, CREDS, frame_msg, wait_until  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        S.reset()
        self.env = dict(os.environ)
        self.addCleanup(self._restore_env)
        self.data = tempfile.mkdtemp(prefix="openmate-larkx-data-")
        self.addCleanup(shutil.rmtree, self.data, True)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self.env)

    def backend(self, region="feishu"):
        b = larkagentx.LarkXBackend(larkagentx.load_larkx(self.data, region), region)
        b.poll_timeout = b.poll_interval = 0.0
        b.expiry_wait = 0.0
        b.up_grace = 0.15
        b.heartbeat = 60.0
        self.rotated = []
        b.on_credentials = self.rotated.append
        self.addCleanup(b.close)
        return b

    def signed_in(self, **creds):
        b = self.backend()
        c = dict(CREDS)
        c.update(creds)
        b.restore(c)
        return b


# ---------------------------------------------------------------- mapping


class MappingTest(Base):
    def setUp(self):
        super().setUp()
        self.b = self.signed_in()

    def m(self, **kw):
        d = {"msg_id": "101", "chat_id": "500", "msg_type": 4, "from_id": "7002", "position": 12,
             "create_time": 1763000000, "root_id": "", "parent_id": "", "anchor": "", "scope": "chat", "at_me": False,
             "content": "", "content_data": None}
        d.update(kw)
        return self.b._message(d, ME, self.b._client)

    def test_text_mentions_and_mine(self):
        from larkx._fake import text_data

        m = self.m(content_data=text_data("你好 ", ("at", ME, "@我"), " 看下"), at_me=True)
        self.assertEqual((m.id, m.chat_id, m.position, m.type, m.text), ("101", "500", 12, "TEXT", "你好 @我 看下"))
        self.assertEqual((m.at_me, m.at_all, m.mine, m.at), (True, False, False, 1763000000.0))
        everyone = self.m(content_data=text_data(("at", "all", "@所有人"), " 开会"), at_me=True)
        self.assertEqual((everyone.at_me, everyone.at_all), (False, True), "an @all alone is not @me")
        both = self.m(content_data=text_data(("at", "all", "@所有人"), ("at", ME, "@我")))
        self.assertEqual((both.at_me, both.at_all), (True, True))
        pushed = self.m(msg_type=2, content_data={"title": "", "text": "通知"}, at_me=True)
        self.assertTrue(pushed.at_me, "the push's own @me mark counts when the text has no mentions")
        mine = self.m(from_id=ME, content_data=text_data("我发的"))
        self.assertTrue(mine.mine)
        self.assertEqual(self.m(create_time=1763000000123, content_data=text_data("x")).at, 1763000000.123)

    def test_post_card_system_and_others(self):
        from larkx._fake import text_data

        post = self.m(msg_type=2, content_data=dict(text_data("正文"), title="标题"))
        self.assertEqual((post.type, post.text), ("POST", "标题\n正文"))
        card = self.m(msg_type=14, content_data={"cardHeader": {"title": "审批"}, "cardDesc": "待处理", "jsonCard": "{…}"})
        self.assertEqual((card.type, card.text), ("CARD", "审批\n待处理"))
        system = self.m(msg_type=6, content_data={"sysContentValues": {"a": {"items": [{"text": "示例同事甲"}]}}})
        self.assertEqual((system.type, system.text), ("SYSTEM", "示例同事甲"))
        share = self.m(msg_type=9, content="[群分享] 示例群", content_data={"chat": {"name": "示例群"}})
        self.assertEqual((share.type, share.text), ("SHARE_CHAT", "[群分享] 示例群"))
        broken = self.m(msg_type=24, content="[TODO消息解析失败: boom]", content_data=None)
        self.assertEqual((broken.type, broken.text), ("TODO", "[不支持的消息：TODO]"))
        self.assertEqual(self.m(msg_type=15, content_data={"key": "file_v3_v", "name": "a.mp4", "size": 9}).type, "VIDEO")
        self.assertIsNone(self.m(msg_id=""))

    def test_card_body_and_not_its_buttons(self):
        import json as _json

        body = {"schema": "2.0",
                "header": {"title": {"tag": "plain_text", "content": "示例告警"}},
                "body": {"elements": [
                    {"tag": "markdown", "content": "**服务** 示例服务 出现异常 <at id=7003></at>"},
                    {"tag": "div", "text": {"tag": "lark_md", "content": "影响：示例接口"},
                     "fields": [{"text": {"tag": "lark_md", "content": "级别：P1"}}]},
                    {"tag": "button", "text": {"tag": "plain_text", "content": "查看详情"}, "url": "https://example.com"},
                    {"tag": "note", "elements": [{"tag": "plain_text", "content": "来自 示例平台"}]},
                    {"tag": "img", "img_key": "img_v3_x", "alt": {"tag": "plain_text", "content": "图"}},
                ]},
                "i18n_elements": {"en_us": [{"tag": "markdown", "content": "English copy"}]}}
        card = self.m(msg_type=14, content_data={"cardHeader": {"title": "示例告警"}, "cardDesc": "",
                                                  "jsonCard": _json.dumps(body, ensure_ascii=False)})
        self.assertEqual(card.text, "示例告警\n**服务** 示例服务 出现异常\n影响：示例接口\n级别：P1\n来自 示例平台")
        self.assertNotIn("查看详情", card.text)
        self.assertNotIn("English", card.text)
        long = self.m(msg_type=14, content_data={"jsonCard": _json.dumps({"elements": [
            {"tag": "markdown", "content": "行%d" % i * 50} for i in range(200)]})})
        self.assertLessEqual(len(long.text), larkagentx.CARD_TEXT_MOST)
        self.assertTrue(long.text.endswith("…"))
        bad = self.m(msg_type=14, content_data={"cardHeader": {"title": "审批"}, "jsonCard": "{not json"})
        self.assertEqual(bad.text, "审批")

    def test_calendar_card(self):
        import json as _json

        cal = {"title": {"element_ids": ["title"], "elements": {"title": {"tag": 1, "property": {"content": "示例同事甲邀请你加入日程"}}}},
               "contents": {"element_ids": ["summary", "time", "attendees"], "elements": {
                   "summary": {"child_ids": ["summary_key", "summary_content"]},
                   "summary_key": {"property": {"content": "主题"}},
                   "summary_content": {"property": {"content": "示例评审会"}},
                   "time": {"child_ids": ["time_key", "time_content"]},
                   "time_key": {"property": {"content": "时间"}},
                   "time_content": {"property": {"content": "2026年11月19日 14:30 - 15:30  (GMT+8)"}},
                   "attendees": {"child_ids": ["attendees_key", "attendees_content"]},
                   "attendees_key": {"property": {"content": "参与者"}},
                   "attendees_content": {"child_ids": ["7001", "7002"]},
                   "7001": {"property": {"content": "示例用户", "user_id": "7001"}},
                   "7002": {"property": {"content": "示例同事甲", "user_id": "7002"}}}},
               "actions": {"element_ids": ["actions"], "elements": {
                   "actions": {"child_ids": ["accept"]}, "accept": {"property": {"content": "接受"}}}},
               "summary": "示例评审会", "location": "示例会议室", "key": "k"}
        m = self.m(msg_type=12, content="[日历] {...}", content_data={"calendar": _json.dumps(cal, ensure_ascii=False)})
        self.assertEqual((m.type, m.text), ("CALENDAR", "[日程] 示例同事甲邀请你加入日程 · 主题：示例评审会 · "
                                            "时间：2026年11月19日 14:30 - 15:30  (GMT+8) · 参与者：示例用户、示例同事甲 · 地点：示例会议室"))
        self.assertNotIn("接受", m.text)
        many = dict(cal)
        many["contents"] = {"element_ids": ["attendees"], "elements": dict(
            {"attendees": {"child_ids": ["attendees_key", "attendees_content"]},
             "attendees_key": {"property": {"content": "参与者"}},
             "attendees_content": {"child_ids": [str(i) for i in range(12)]}},
            **{str(i): {"property": {"content": "人%d" % i}} for i in range(12)})}
        self.assertIn("参与者：人0、人1、人2、人3、人4、人5、人6、人7、等 12 人",
                      self.m(msg_type=12, content_data={"calendar": _json.dumps(many, ensure_ascii=False)}).text)
        odd = self.m(msg_type=12, content="[日历] 原文", content_data={"calendar": "not json"})
        self.assertEqual(odd.text, "[日历] 原文", "unreadable: LarkAgentX's line")
        invite = self.m(msg_type=18, content="", content_data=None)
        self.assertEqual((invite.type, invite.text), ("GENERAL_CALENDAR", "[日程邀请]（详情请在飞书里查看）"))

    def test_shared_calendar_event(self):
        import base64 as _b64

        def vint(n):
            out = bytearray()
            while True:
                b = n & 0x7F
                n >>= 7
                if n:
                    out.append(b | 0x80)
                else:
                    out.append(b)
                    return bytes(out)

        def field(no, v):
            if isinstance(v, int):
                return vint(no << 3) + vint(v)
            v = v.encode("utf-8") if isinstance(v, str) else v
            return vint(no << 3 | 2) + vint(len(v)) + v

        start = 1763620200  # 2026-11-20 14:30 +08:00
        inner = field(1, "示例分享会") + field(2, "7095547010539421698") + field(3, start) + field(4, start + 3600) \
            + field(5, "示例会议室")
        event = field(1, "ad599ca0-3c78-479b-a8e7-ceb0523647fe") + field(2, inner)
        m = self.m(msg_type=16, content="[分享日程] 参与人数=3",
                   content_data={"calendarEvent": _b64.b64encode(event).decode(), "attendeesCount": 3})
        self.assertTrue(m.text.startswith("[分享日程] 示例分享会 · 示例会议室 · "), m.text)
        self.assertIn(time.strftime("%m-%d %H:%M", time.localtime(start)), m.text)
        self.assertTrue(m.text.endswith("3 人参与"))
        empty = self.m(msg_type=16, content="[分享日程] 参与人数=0", content_data={"calendarEvent": "", "attendeesCount": 0})
        self.assertEqual(empty.text, "[分享日程] 参与人数=0")
        junk = self.m(msg_type=16, content="[分享日程] 参与人数=0", content_data={"calendarEvent": "@@@"})
        self.assertEqual(junk.text, "[分享日程] 参与人数=0")

    def test_a_newer_card_and_a_decoded_shared_event(self):
        import base64 as _b64
        import json as _json

        def field(no, v):
            v = v.encode("utf-8")
            n, size = len(v), bytearray()
            while True:
                size.append((n & 0x7F) | (0x80 if n > 0x7F else 0))
                n >>= 7
                if not n:
                    break
            return bytes([no << 3 | 2]) + bytes(size) + v

        body = _json.dumps({"elements": [{"tag": "markdown", "content": "示例流水线 构建失败"},
                                         {"tag": "button", "text": {"content": "重试"}}]}, ensure_ascii=False)
        entity = field(1, "plain_text") + field(2, "#FFFFFF") + field(3, "示例构建通知") + field(4, body) \
            + field(5, "img_v3_0abc")
        card = self.m(msg_type=14, content_data={"cardDesc": "", "universalCardEntity": _b64.b64encode(entity).decode()})
        self.assertEqual(card.text, "示例构建通知\n示例流水线 构建失败")
        start = 1763620200
        shared = self.m(msg_type=16, content="[分享日程] 参与人数=2", content_data={"attendeesCount": 2, "calendarEvent": {
            "summary": "示例周会", "startTime": str(start), "endTime": str(start + 3600), "location": {"name": "示例会议室"},
            "key": "ad599ca0-3c78-479b-a8e7-ceb0523647fe"}})
        self.assertTrue(shared.text.startswith("[分享日程] 示例周会 · 示例会议室 · "), shared.text)
        self.assertIn(time.strftime("%m-%d %H:%M", time.localtime(start)), shared.text)

    def test_a_card_read_as_nothing_logs_its_shape_not_its_content(self):
        with self.assertLogs("openmate.larkx", "INFO") as logs:
            self.m(msg_type=14, content_data={"cardDesc": "", "mystery": {"secret": "不要写进日志", "n": 3}, "list": [1, 2]})
            self.m(msg_type=14, content_data={"cardDesc": "", "mystery": {"secret": "另一条", "n": 4}, "list": [5, 6]})
        out = "\n".join(logs.output)
        self.assertEqual(out.count("read as nothing"), 1, "once per shape")
        self.assertIn("mystery:{n:int,secret:str~1}", out)
        self.assertNotIn("不要写进日志", out)

    def test_merge_forward_names_its_senders(self):
        S.api_answers["chatters.PullChattersByIdsRequest"] = {
            "chatters": {"7003000000000000001": {"id": "7003000000000000001", "name": "示例同事丙"}}}
        line = "[合并转发] 示例群 共2条:\n  7003000000000000001: 你好\n  7003000000000000002: 好的"
        m = self.m(msg_type=11, content=line, content_data={"messages": [], "groupChatName": "示例群"})
        self.assertEqual(m.type, "MERGE_FORWARD")
        self.assertIn("  示例同事丙: 你好", m.text)
        self.assertIn("7003000000000000002: 好的", m.text, "unknown: the id stays")

    def test_media(self):
        img = self.m(msg_type=5, content="[图片] imageKey=img_v3_k", content_data={
            "imageV2": {"imageKey": "img_v3_k", "variants": [{"type": 2}, {"type": 1, "size": {"width": 640, "height": 480}}]}})
        self.assertEqual(img.text, "", "LarkAgentX's line names the key: the media says it instead")
        self.assertEqual(len(img.media), 1)
        mm = img.media[0]
        self.assertEqual((mm.kind, mm.width, mm.height), ("image", 640, 480))
        self.assertEqual(larkagentx.parse_ref(mm.ref), ("image", "500", "101", "img_v3_k"))
        f = self.m(msg_type=3, content_data={"key": "file_v3_k", "name": "报告.pdf", "size": 2048, "mime": "application/pdf"}).media[0]
        self.assertEqual((f.kind, f.name, f.size, f.mime), ("file", "报告.pdf", 2048, "application/pdf"))
        self.assertEqual(self.m(msg_type=7, content_data={"key": "k", "size": 5}).media[0].kind, "voice")
        self.assertEqual(self.m(msg_type=10, content_data={"key": "k", "width": 2, "height": 3}).media[0].kind, "sticker")
        self.assertEqual(self.m(msg_type=5, content_data={}).media[0].ref, "", "no key: nothing to fetch")

    def test_threads_and_quotes(self):
        from larkx._fake import text_data

        t = text_data("x")
        in_thread = self.m(root_id="90", parent_id="90", anchor="90", scope="topic", content_data=t)
        self.assertEqual((in_thread.root_id, in_thread.thread_id, in_thread.parent_id), ("90", "90", "90"))
        quote = self.m(root_id="80", parent_id="85", anchor="101", scope="topic", content_data=t)
        self.assertEqual((quote.root_id, quote.thread_id, quote.parent_id), ("", "", "85"),
                         "a quote in the main stream is not a thread")
        topic_root = self.m(scope="topic", anchor="101", content_data=t)
        self.assertEqual((topic_root.root_id, topic_root.thread_id), ("", "101"))
        self.assertEqual(self.m(root_id="0", parent_id="0", content_data=t).root_id, "")

    def test_positions_always_grow(self):
        from larkx._fake import text_data

        S.chats["600"] = {"id": "600", "type": 2, "lastMessagePosition": 40}
        lone = self.m(chat_id="600", position=None, content_data=text_data("话题里的回复"))
        self.assertEqual(lone.position, 40, "no position of its own: the chat's newest")
        self.m(chat_id="600", position=41, content_data=text_data("新的"))
        self.assertEqual(self.m(chat_id="600", position=0, content_data=text_data("y")).position, 41)

    def test_refs(self):
        ref = larkagentx.media_ref("file", "500", "101", "file_v3:odd/key")
        self.assertEqual(larkagentx.parse_ref(ref), ("file", "500", "101", "file_v3:odd/key"))
        for bad in ("", "img:example-1", "lx1:image:500:101", "lx1:image:abc:101:k", "lx2:image:500:101:k",
                    "lx1:nope:500:101:k", "lx1:image:500:101:"):
            self.assertIsNone(larkagentx.parse_ref(bad), bad)


# ---------------------------------------------------------------- sign-in


class LoginTest(Base):
    def test_qr_states(self):
        b = self.backend()
        qr = b.login_start()
        self.assertEqual(json.loads(qr.url), {"qrlogin": {"token": "qr-token-1"}})
        self.assertTrue(qr.state.startswith("qr_") and len(qr.state) > 20)
        self.assertEqual(b.login_poll(qr.state), "waiting")
        S.qr_script = [(False, None, 2)]
        self.assertEqual(b.login_poll(qr.state), "scanned")
        self.assertEqual(b.login_poll(qr.state), "scanned", "still scanned while the phone has not confirmed")
        S.qr_script = [(True, {"next_step": "done"}, 0)]
        self.assertEqual(b.login_poll(qr.state), "confirmed")
        self.assertEqual(b.login_poll(qr.state), "confirmed", "asked again: no new poll")
        self.assertEqual(b.login_poll("qr_unknown"), "expired")
        ql = b._qr[qr.state]["ql"]
        self.assertTrue(all(interval >= timeout for timeout, interval in ql.waits), "one poll per call")

    def test_expired_and_cancelled(self):
        b = self.backend()
        for status in (5, 3, 4):
            qr = b.login_start()
            S.qr_script = [(False, {"step_info": {"status": status}}, status)]
            self.assertEqual(b.login_poll(qr.state), "expired")
            self.assertEqual(b.login_poll(qr.state), "expired", "a finished flow is gone")
        qr = b.login_start()
        b._qr[qr.state]["at"] -= larkagentx.QR_LIFE + 1
        self.assertEqual(b.login_poll(qr.state), "expired")

    def test_finish_hands_over_credentials_and_writes_nothing(self):
        b = self.backend()
        S.api_answers["chatters.PullChattersByIdsRequest"] = {"chatters": {ME: {"id": ME, "name": "示例用户"}}}
        qr = b.login_start()
        S.qr_script = [(True, {}, 0)]
        b.login_poll(qr.state)
        done = b.login_finish(qr.state)
        self.assertEqual((done.account.id, done.account.name), (ME, "示例用户"))
        c = done.credentials
        self.assertEqual(c["cookies"]["session"], "session-cookie-secret")
        self.assertEqual((c["user_id"], c["app_key"], c["device_id"], c["region"], c["user_name"]),
                         (ME, "appkey-1", "device-1", "feishu", "示例用户"))
        json.dumps(c)
        self.assertTrue(S.saves, "LarkAgentX saved its credentials…")
        for dirpath, dirnames, filenames in os.walk(self.data):
            self.assertEqual(filenames, [], "…but nothing reached the disk")
        self.assertNotIn(qr.state, b._qr)
        with self.assertRaises(BackendError):
            b.login_finish(qr.state)

    def test_finish_failures(self):
        b = self.backend()
        qr = b.login_start()
        with self.assertRaises(BackendError):
            b.login_finish(qr.state)  # not confirmed
        for knob in ("finish_error", "finish_app_key"):
            S.reset()
            if knob == "finish_error":
                S.finish_error = sys.modules["larkx.auth"].AuthExpired("no session")
            else:
                S.finish_app_key = ""
            qr = b.login_start()
            S.qr_script = [(True, {}, 0)]
            b.login_poll(qr.state)
            with self.assertRaises(BackendError) as e:
                b.login_finish(qr.state)
            self.assertNotIn("session", e.exception.message)
        self.assertIsNone(b._session()[0])

    def test_network_errors_are_sentences(self):
        b = self.backend()
        S.qr_init_error = R.ConnectionError("https://accounts.example/?token=secret")
        with self.assertRaises(BackendError) as e:
            b.login_start()
        self.assertEqual(e.exception.kind, "unreachable")
        self.assertNotIn("secret", e.exception.message)
        S.qr_init_error = R.ProxyError("proxy http://user:pass@proxy.example")
        with self.assertRaises(BackendError) as e:
            b.login_start()
        self.assertEqual(e.exception.kind, "proxy")
        self.assertNotIn("pass", e.exception.message)

    def test_flows_are_bounded(self):
        b = self.backend()
        states = [b.login_start().state for _ in range(10)]
        self.assertLessEqual(len(b._qr), larkagentx.QR_KEEP)
        self.assertIn(states[-1], b._qr)


class RestoreTest(Base):
    def test_restore(self):
        b = self.backend()
        acct = b.restore(dict(CREDS))
        self.assertEqual((acct.id, acct.name), (ME, "示例用户"))
        self.assertEqual(self.rotated, [], "nothing changed: nothing to seal again")
        self.assertEqual(b._client.auth.cookies, CREDS["cookies"])

    def test_filled_in_credentials_are_handed_back(self):
        b = self.backend()
        S.api_answers["chatters.PullChattersByIdsRequest"] = {"chatters": {ME: {"name": "示例用户"}}}
        c = dict(CREDS, app_key="", user_name="")
        b.restore(c)
        self.assertEqual(len(self.rotated), 1)
        self.assertEqual((self.rotated[0]["app_key"], self.rotated[0]["user_name"]), ("appkey-fetched", "示例用户"))
        # LarkAgentX saving again later (a refreshed token) reaches the core too, once per change
        auth = b._client.auth
        auth.csrf_token = "csrf-2"
        auth.save()
        auth.save()
        self.assertEqual([r["csrf_token"] for r in self.rotated[1:]], ["csrf-2"])

    def test_expired_only_when_refused_twice(self):
        b = self.backend()
        S.tickets = ["expired", "expired"]
        with self.assertRaises(CredentialsExpired):
            b.restore(dict(CREDS))
        S.reset()
        S.tickets = ["expired", "ok"]
        self.assertEqual(b.restore(dict(CREDS)).id, ME, "one refusal is a hiccup")

    def test_network_is_not_expiry(self):
        b = self.backend()
        S.tickets = [R.ConnectionError("down")]
        with self.assertRaises(BackendError) as e:
            b.restore(dict(CREDS))
        self.assertNotIsInstance(e.exception, CredentialsExpired)
        self.assertEqual(e.exception.kind, "unreachable")
        # LarkClient folds a network error into "invalid": the ticket is asked again before calling it expired
        S.reset()
        S.tickets = ["ok", R.ConnectionError("down"), "ok"]
        with self.assertRaises(BackendError) as e:
            b.restore(dict(CREDS))
        self.assertNotIsInstance(e.exception, CredentialsExpired)

    def test_bad_credentials(self):
        b = self.backend()
        for c in ({}, {"cookies": {}}, dict(CREDS, user_id=""), "nope"):
            with self.assertRaises(CredentialsExpired):
                b.restore(c)
        with self.assertRaises(CredentialsExpired) as e:
            self.backend("lark").restore(dict(CREDS))
        self.assertIn("区域", e.exception.message)

    def test_logout_forgets(self):
        b = self.signed_in()
        b.logout()
        self.assertEqual(b._session(), (None, None))
        with self.assertRaises(SendRejected):
            b.send("500", "x")


# ---------------------------------------------------------------- sends


class SendTest(Base):
    def setUp(self):
        super().setUp()
        self.b = self.signed_in()
        S.chats.update({"500": {"id": "500", "type": 2, "name": "示例项目群"}, "501": {"id": "501", "type": 1},
                        "502": {"id": "502", "type": 2, "chatMode": 2}})
        S.thread_chats.add("502")

    def last(self):
        return S.posts[-1]

    def test_plain_thread_and_quote(self):
        b = self.b
        self.assertEqual(b.send("500", "你好"), "9001")
        self.assertEqual((self.last()["root"], self.last()["reply_in_thread"]), ("", False))
        b.send("500", "进话题", reply_to="90", root="90")
        self.assertEqual((self.last()["root"], self.last()["reply_in_thread"]), ("90", True))
        b.send("500", "引用回复", reply_to="85")
        self.assertEqual((self.last()["root"], self.last()["reply_in_thread"]), ("85", False), "stays in the main stream")
        b.send("501", "私聊里", reply_to="70", root="70")
        self.assertEqual((self.last()["root"], self.last()["reply_in_thread"]), ("70", False), "a direct chat quotes")
        b.send("502", "话题群", reply_to="60", root="60")
        self.assertEqual((self.last()["root"], self.last()["reply_in_thread"]), ("60", False), "a topic group's own way")
        self.assertEqual(self.last()["text"], "话题群")

    def test_surely_not_sent(self):
        b = self.b
        for err in (R.ConnectTimeout("t"), R.ProxyError("p"), R.InvalidSchema("socks"),
                    R.ConnectionError(R.MaxRetryError(R.NewConnectionError("refused"))),
                    R.HTTPError("403", response=R.Response(403)), R.HTTPError("429", response=R.Response(429))):
            S.post_answers = [err]
            with self.assertRaises(SendRejected, msg=type(err).__name__):
                b.send("500", "x")
        for args in (("500", "  "), ("not-a-chat", "x"), ("500", "x", "bad-id")):
            with self.assertRaises(SendRejected):
                b.send(*args)

    def test_maybe_sent(self):
        b = self.b
        for answer in (R.ReadTimeout("r"), R.ConnectionError("reset after sending"), R.SSLError("ssl"),
                       R.HTTPError("502", response=R.Response(502)), b"empty", b"garbage", RuntimeError("?")):
            S.post_answers = [answer]
            with self.assertRaises(BackendError) as e:
                b.send("500", "x")
            self.assertNotIsInstance(e.exception, SendRejected, repr(answer))

    def test_create_chat(self):
        b = self.b
        S.created["7003"] = "503"
        self.assertEqual(b.create_chat("7003"), "503")
        self.assertEqual(S.posts, [], "opening a chat sends nothing")
        self.assertTrue(b._is_dm(b._client, "503"))
        with self.assertRaises(BackendError) as e:
            b.create_chat("7004")
        self.assertEqual(e.exception.kind, "not_found")
        with self.assertRaises(BackendError):
            b.create_chat("x")


# ---------------------------------------------------------------- reads


class ReadTest(Base):
    def setUp(self):
        super().setUp()
        self.b = self.signed_in()
        from larkx._fake import text_data

        S.chats["500"] = {"id": "500", "type": 2, "name": "示例项目群", "memberCount": 5, "lastMessagePosition": 10}
        S.history["500"] = {p: frame_msg(str(100 + p), "500", p, text_data("第%d条" % p), sender=ME if p == 9 else "7002")
                            for p in range(1, 11) if p not in (6, 7)}  # 6 and 7 were recalled

    def test_history_oldest_first_and_gaps_filled(self):
        msgs = self.b.history("500", None, 5)
        self.assertEqual([m.position for m in msgs], [4, 5, 8, 9, 10])
        self.assertEqual(S.history_calls[0], ("500", [6, 7, 8, 9, 10]))
        self.assertTrue(msgs[3].mine)
        older = self.b.history("500", 4, 50)
        self.assertEqual([m.position for m in older], [1, 2, 3])
        self.assertEqual(self.b.history("500", 1, 10), [])
        with self.assertRaises(BackendError) as e:
            self.b.history("404", None, 5)
        self.assertEqual(e.exception.kind, "not_found")

    def test_recent_chats_newest_first(self):
        S.api_answers["chats.PullRecentChatsRequest"] = {
            "chats": {"500": S.chats["500"], "501": {"id": "501", "type": 1, "name": "示例同事甲"}},
            "orderedChatIds": ["501", "500"]}
        chats = self.b.recent_chats(20)
        self.assertEqual([(c.id, c.kind, c.name) for c in chats], [("501", "dm", "示例同事甲"), ("500", "group", "示例项目群")])
        self.assertEqual(chats[1].members, 5)
        self.assertEqual(S.api_calls[-1], ("chats.PullRecentChatsRequest", {"count": 20}))

    def test_recent_chats_from_the_feed(self):
        S.api_answers["chats.PullRecentChatsRequest"] = ValueError("not served")
        S.api_answers["feed.PullFeedCardsRequest"] = {"cards": [{"id": "500"}, {"id": "doc-1"}],
                                                      "chats": {"500": S.chats["500"]}}
        self.assertEqual([c.id for c in self.b.recent_chats(5)], ["500"])
        S.api_answers["feed.PullFeedCardsRequest"] = R.ConnectionError("down")
        with self.assertRaises(BackendError):
            self.b.recent_chats(5)

    def test_chat_info_topic_and_cache(self):
        S.chats["502"] = {"id": "502", "type": 2, "chatMode": 3, "name": "", "i18nNames": {"zh_cn": "话题群"}}
        c = self.b.chat_info("502")
        self.assertEqual((c.kind, c.topic, c.name), ("group", True, "话题群"))
        n = len(S.chat_info_calls)
        self.b.chat_info("502")
        self.assertEqual(len(S.chat_info_calls), n, "cached")
        with self.assertRaises(BackendError) as e:
            self.b.chat_info("404")
        self.assertEqual(e.exception.kind, "not_found")

    def test_the_chat_with_yourself_is_named_after_you(self):
        calls = []
        real = self.b._client.create_chat
        self.b._client.create_chat = lambda uid: (calls.append(uid), real(uid))[1]
        S.created[ME] = "505"
        S.chats["505"] = {"id": "505", "type": 1, "name": ""}
        S.chats["506"] = {"id": "506", "type": 1, "name": ""}  # a dm nobody else has written in yet
        c = self.b.chat_info("505")
        self.assertEqual((c.kind, c.name, c.peer_id), ("dm", "示例用户", ME))
        c = self.b.chat_info("506")
        self.assertEqual((c.name, c.peer_id), ("", ""), "never guessed to be the chat with yourself")
        self.assertEqual(calls, [ME], "asked once")
        self.assertEqual(S.posts, [], "nothing sent")

    def test_the_chat_with_yourself_unknown(self):
        S.chats["505"] = {"id": "505", "type": 1, "name": ""}
        self.b._client.create_chat = lambda uid: (_ for _ in ()).throw(R.ConnectionError("down"))
        c = self.b.chat_info("505")
        self.assertEqual((c.name, c.peer_id), ("", ""))

    def test_a_direct_chat_is_named_after_the_other_person(self):
        from larkx._fake import text_data

        S.chats["501"] = {"id": "501", "type": 1, "name": ""}
        self.assertEqual(self.b.chat_info("501").name, "")
        S.api_answers["chatters.PullChattersByIdsRequest"] = {"chatters": {"7003": {"name": "示例同事丙"}}}
        self.b._message(frame_msg("301", "501", 3, text_data("在吗"), sender="7003", chat_type=1), ME, self.b._client)
        c = self.b.chat_info("501")
        self.assertEqual((c.kind, c.name, c.peer_id), ("dm", "示例同事丙", "7003"))
        self.b._message(frame_msg("302", "501", 4, text_data("我回"), sender=ME, chat_type=1), ME, self.b._client)
        self.assertEqual(self.b._peers["501"], "7003", "my own message does not make me the other person")

    def test_search_and_names(self):
        S.search_hits = [{"type": "user", "id": "7003", "title": "<h>示例</h>同事丙"},
                         {"type": "group", "id": "500", "title": "示例项目群"}, {"type": "doc", "id": "1", "title": "x"}]
        hits = self.b.search("示例", 10)
        self.assertEqual([(h.kind, h.id, h.name) for h in hits], [("user", "7003", "示例同事丙"), ("group", "500", "示例项目群")])
        self.assertEqual(len(self.b.search("示例", 1)), 1)
        S.user_names[("7003", "500")] = "丙（群昵称）"
        self.assertEqual(self.b.user_name("7003", "500"), "丙（群昵称）")
        self.b.user_name("7003", "500")
        self.assertEqual(len(S.user_name_calls), 1, "cached")
        S.api_answers["chatters.PullChattersByIdsRequest"] = {"chatters": {"7004": {"name": "示例同事丁"}}}
        self.assertEqual(self.b.user_name("7004"), "示例同事丁")
        self.assertEqual(self.b.user_name(ME), "示例用户")
        self.assertEqual(self.b.user_name(""), "")

    def test_media(self):
        b = self.b
        png = b"\x89PNG\r\n\x1a\n" + b"\0" * 20
        host = "https://files.feishu.example/download/messages/101/keys/"
        S.files[host + "img_v3_k?chat_id=500"] = (png, "application/octet-stream")
        S.files[host + "img_v3_enc?chat_id=500"] = (b"\x13\x37" * 10, "image/png")
        S.files[host + "file_v3_k?chat_id=500"] = (b"%PDF-1.7", "application/pdf; charset=binary")
        self.assertEqual(b.media(larkagentx.media_ref("image", "500", "101", "img_v3_k"), 1 << 20), ("image/png", png))
        self.assertEqual(S.downloads[-1][2], CREDS["cookies"], "downloaded with the session")
        self.assertEqual(b.media(larkagentx.media_ref("file", "500", "101", "file_v3_k"), 1 << 20), ("application/pdf", b"%PDF-1.7"))
        with self.assertRaises(BackendError) as e:
            b.media(larkagentx.media_ref("image", "500", "101", "img_v3_enc"), 1 << 20)
        self.assertIn("加密", e.exception.message)
        with self.assertRaises(BackendError) as e:
            b.media(larkagentx.media_ref("image", "500", "101", "img_v3_k"), 10)
        self.assertEqual(e.exception.kind, "too_large")
        S.download_error = R.HTTPError("404", response=R.Response(404))
        with self.assertRaises(BackendError) as e:
            b.media(larkagentx.media_ref("file", "500", "101", "file_v3_k"), 1 << 20)
        self.assertEqual(e.exception.kind, "not_found")
        with self.assertRaises(BackendError) as e:
            b.media("img:example-1", 10)
        self.assertEqual(e.exception.kind, "not_found")


# ---------------------------------------------------------------- the live link


class ListenTest(Base):
    def setUp(self):
        super().setUp()
        self.b = self.signed_in()
        self.got, self.links = [], []
        self.stop = threading.Event()
        self.error = []

    def start(self):
        def run():
            try:
                self.b.listen(self.got.append, lambda s, r="": self.links.append(s), self.stop)
            except BaseException as e:
                self.error.append(e)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.addCleanup(self.stop.set)
        return t

    def test_messages_in_order_up_once_acks(self):
        from larkx._fake import frame, text_data

        t = self.start()
        S.frames.put(frame(sid="s1", messages=[frame_msg("201", "500", 20, text_data("第一条")),
                                               frame_msg("202", "500", 21, text_data("我的"), sender=ME)]))
        S.frames.put(frame(sid="s2", cmd=4))  # a heartbeat answer: acked, not a message
        S.frames.put(frame(messages=[frame_msg("203", "500", 22, text_data("第三条")), frame_msg("204", "500", 23, {}, sender="")]))
        wait_until(lambda: len(self.got) == 3, what="three messages")
        self.assertEqual([m.id for m in self.got], ["201", "202", "203"])
        self.assertEqual([m.mine for m in self.got], [False, True, False])
        self.assertEqual(self.links, ["up"])
        self.assertIn(("ack", "s1"), S.ws_sent)
        self.assertIn(("ack", "s2"), S.ws_sent)
        self.assertEqual(S.ws_sent[0], ("heartbeat",), "a heartbeat right after connecting")
        self.assertEqual(S.ws_kwargs[0]["max_size"], larkagentx.WS_MAX)
        self.stop.set()
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(self.error, [])
        self.assertEqual(self.links, ["up"])

    def test_up_without_frames_after_the_grace(self):
        t = self.start()
        wait_until(lambda: self.links == ["up"], what="up")
        S.frames.put("drop")
        t.join(5)
        self.assertFalse(t.is_alive(), "a dropped connection returns: the core reconnects")
        self.assertEqual(self.error, [])

    def test_closed_before_up_is_not_up(self):
        self.b.up_grace = 5.0
        S.frames.put("close")
        t = self.start()
        t.join(5)
        self.assertEqual((self.links, self.error), ([], []))

    def test_proxy_without_python_socks(self):
        S.ws_connects = [ImportError("python-socks is required to use a SOCKS proxy")]
        self.start().join(5)
        e = self.error[0]
        self.assertEqual(e.kind, "proxy")
        self.assertIn("python-socks", e.message)
        self.assertEqual(self.links, [])
        S.ws_connects = [W.ProxyError("failed to connect to SOCKS proxy")]
        self.error.clear()
        self.start().join(5)
        self.assertEqual(self.error[0].kind, "proxy")

    def test_refused_connection_and_expiry(self):
        S.ws_connects = [W.InvalidStatus(R.Response(403))]
        S.tickets = ["ok", "expired", "expired"]  # build_ws_url, then the checks after the 403
        self.start().join(5)
        self.assertIsInstance(self.error[0], CredentialsExpired)
        S.reset()
        S.ws_connects = [W.InvalidStatus(R.Response(503))]
        self.error.clear()
        self.start().join(5)
        self.assertNotIsInstance(self.error[0], CredentialsExpired)
        self.assertEqual(self.error[0].kind, "unreachable")
        S.ws_connects = [OSError("unreachable"), ]
        self.error.clear()
        self.start().join(5)
        self.assertEqual(self.error[0].kind, "unreachable")

    def test_no_ticket(self):
        S.tickets = ["expired", "expired", "expired"]
        self.start().join(5)
        self.assertIsInstance(self.error[0], CredentialsExpired)

    def test_not_signed_in(self):
        self.b.logout()
        self.start().join(5)
        self.assertIsInstance(self.error[0], CredentialsExpired)

    def test_stop_before_connected(self):
        self.stop.set()
        self.start().join(5)
        self.assertEqual((self.links, self.error), ([], []))


# ---------------------------------------------------------------- create and the proxy setting


class CreateTest(Base):
    def test_settings(self):
        for config, word in (({}, "larkx_python"), ({"larkx_python": "/x", "region": "cn"}, "区域"),
                             ({"larkx_python": "/x", "larkx_commit": "abc"}, "40 位"),
                             ({"larkx_python": "/x", "http_proxy": "socks5://127.0.0.1:1080"}, "http://"),
                             ({"larkx_python": "/x", "larkx_commit": "0" * 40}, "版本不对")):
            with self.assertRaises(BackendError, msg=config) as e:
                larkagentx.create(config, self.data)
            self.assertEqual(e.exception.kind, "config")
            self.assertIn(word, e.exception.message)

    def test_a_launch_problem_is_the_status(self):
        old = launch._problem
        launch._problem = "LarkAgentX 的 Python 是 3.8，需要 3.9 或更新"
        self.addCleanup(setattr, launch, "_problem", old)
        with self.assertRaises(BackendError) as e:
            larkagentx.create({"larkx_python": "/x"}, self.data)
        self.assertEqual((e.exception.kind, e.exception.message), ("config", launch._problem))

    def test_made(self):
        b = larkagentx.create({"larkx_python": "/x", "region": "lark", "http_proxy": "http://127.0.0.1:7890"}, self.data)
        self.assertIsInstance(b, larkagentx.LarkXBackend)
        self.assertEqual(b.region.name, "lark")
        self.assertEqual(os.environ["LARKX_HOME"], os.path.join(self.data, "larkx"))
        self.assertEqual(os.environ["LARKX_REGION"], "lark")
        for name in ("http_proxy", "HTTPS_PROXY", "wss_proxy"):
            self.assertEqual(os.environ[name], "http://127.0.0.1:7890")

    def test_proxy_setting(self):
        self.assertEqual(larkagentx.proxy_setting(""), "")
        self.assertEqual(larkagentx.proxy_setting(" https://proxy.example:8443 "), "https://proxy.example:8443")
        for bad in ("socks5://127.0.0.1:1080", "127.0.0.1:7890", "http://", "http://h:port", "ftp://h"):
            with self.assertRaises(BackendError, msg=bad) as e:
                larkagentx.proxy_setting(bad)
            self.assertEqual(e.exception.kind, "config")
        env = {"https_proxy": "socks5://127.0.0.1:1080"}
        larkagentx.use_proxy("http://127.0.0.1:7890", env)
        self.assertEqual(env["https_proxy"], "http://127.0.0.1:7890", "the setting wins over the environment's")

    def test_sniff(self):
        self.assertEqual(larkagentx.sniff_image(b"\xff\xd8\xff\xe0"), "image/jpeg")
        self.assertEqual(larkagentx.sniff_image(b"GIF89a"), "image/gif")
        self.assertEqual(larkagentx.sniff_image(b"RIFF\0\0\0\0WEBPVP8"), "image/webp")
        self.assertEqual(larkagentx.sniff_image(b"\0\0\0\x18ftypheic"), "image/heic")
        self.assertEqual(larkagentx.sniff_image(b"plain"), "")


if __name__ == "__main__":
    unittest.main()
