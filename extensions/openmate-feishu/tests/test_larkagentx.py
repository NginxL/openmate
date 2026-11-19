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
        as_bytes = self.m(msg_type=16, content="[分享日程] 参与人数=3", content_data={"calendarEvent": event, "attendeesCount": 3})
        self.assertEqual(as_bytes.text, m.text, "LarkAgentX hands the bytes over as they are")
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

    def test_a_card_with_nothing_handed_over_still_says_it_is_a_card(self):
        self.assertEqual(self.m(msg_type=14, content_data={}).text, "[卡片]（内容请在飞书里查看）")
        linked = self.m(msg_type=14, content_data={"actionUrl": "https://example.com/x", "richtext": {"elements": {}}})
        self.assertEqual(linked.text, "[卡片] https://example.com/x")
        self.assertEqual(self.m(msg_type=14, content_data={"actionUrl": "lark://x"}).text, "[卡片]（内容请在飞书里查看）")

    def test_a_card_read_as_nothing_logs_its_shape_not_its_content(self):
        with self.assertLogs("openmate.larkx", "INFO") as logs:
            self.m(msg_type=16, content="[分享日程]", content_data={"mystery": {"secret": "不要写进日志", "n": 3}, "list": [1, 2]})
            self.m(msg_type=16, content="[分享日程]", content_data={"mystery": {"secret": "另一条", "n": 4}, "list": [5, 6]})
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

    def test_where_a_message_shows(self):
        from larkx._fake import text_data

        t = text_data("x")
        thread = dict(root_id="90", parent_id="95", anchor="90", scope="topic", content_data=t)
        self.assertEqual(self.m(**thread).placement, "thread", "a reply only in the thread")
        sync = {"thread_type": 2, "send_to_chat": False, "sync_type": 2, "sync_related": "96", "sync_root": "90"}
        self.assertEqual(self.m(_placement=sync, **thread).placement, "thread_chat", "also sent to the chat")
        old = {"thread_type": 2, "send_to_chat": True, "sync_type": 0, "sync_related": "", "sync_root": ""}
        self.assertEqual(self.m(msg_id="102", _placement=old, **thread).placement, "thread_chat", "the older mark")
        self.assertEqual((self.b._synced["101"], self.b._synced["102"]), ("sync", "send"), "how: a reply copies it")
        alone = {"thread_type": 2, "send_to_chat": False, "sync_type": 0, "sync_related": "", "sync_root": ""}
        self.assertEqual(self.m(msg_id="103", _placement=alone, **thread).placement, "thread")
        self.assertNotIn("103", self.b._synced)
        quote = self.m(root_id="90", parent_id="95", anchor="104", scope="topic", msg_id="104", content_data=t)
        self.assertEqual(quote.placement, "chat", "a quote in the main stream")
        self.assertEqual(self.m(content_data=t).placement, "chat", "a plain group message")
        # a quote in the main stream no one replied to in its thread has no threadId: the anchor is its root
        S.chats["500"] = {"id": "500", "type": 2, "name": "示例项目群"}  # known as an ordinary group
        bare = dict(root_id="90", parent_id="90", anchor="90", scope="topic", content_data=t)
        none = dict(alone, thread_type=0, thread_known=True)
        q = self.m(msg_id="111", _placement=none, **bare)
        self.assertEqual((q.placement, q.root_id, q.thread_id, q.parent_id), ("chat", "", "", "90"),
                         "a live push says a quote in the main stream, not a thread reply")
        rooted = self.m(msg_id="112", _placement=dict(none, thread_type=1), **bare)  # a quote that opened a thread
        self.assertEqual((rooted.placement, rooted.root_id), ("chat", ""))
        self.assertEqual(self.m(msg_id="113", **bare).root_id, "90", "read from history: no marks, the old reading")
        # Feishu leaves threadMessageType unset on a quote in the main stream (a proto2 field: it reads as 0)
        unset = {"thread_type": 0, "thread_known": False, "send_to_chat": False, "sync_type": 0, "sync_related": "",
                 "sync_root": ""}
        q = self.m(msg_id="130", _placement=unset, **bare)
        self.assertEqual((q.placement, q.root_id, q.thread_id), ("chat", "", ""), "unset: a quote in the main stream")
        with self.assertLogs("openmate.larkx", level="INFO") as logs:  # the account's own quote says what it carried
            self.m(msg_id="135", from_id=ME, _placement=unset, **bare)
        self.assertTrue(any("own message 135: threadMessageType=unset" in x and "read as chat" in x
                            for x in logs.output), logs.output)
        # unset, with the marks of a thread reply also sent to the chat: what it was read as stays
        synced = dict(unset, sync_type=2, sync_root="90")
        kept = self.m(msg_id="116", _placement=synced, **bare)
        self.assertEqual((kept.placement, kept.root_id), ("thread_chat", "90"), "a thread reply's marks: the old reading")
        kept = self.m(msg_id="131", _placement=dict(unset, sync_root="90"), **bare)
        self.assertEqual(kept.root_id, "90")
        S.chats["505"] = {"id": "505", "type": 2, "name": "示例话题群", "chatMode": 3}
        topic = self.m(chat_id="505", msg_id="115", _placement=none, **bare)
        self.assertEqual((topic.placement, topic.root_id), ("thread", "90"), "a thread-first group keeps the old reading")
        S.chats["506"] = R.ConnectionError("down")  # not known which kind of group: the old reading
        self.assertEqual(self.m(chat_id="506", msg_id="117", _placement=none, **bare).root_id, "90")
        S.chats["507"] = {"id": "507", "type": 2, "name": "示例群"}  # read once, kept
        self.assertEqual(self.m(chat_id="507", msg_id="118", _placement=none, **bare).root_id, "")
        self.m(chat_id="507", msg_id="119", _placement=none, **bare)
        self.assertEqual(S.chat_info_calls.count("507"), 1)
        S.chats["508"] = {"id": "508", "name": "示例群"}  # its record does not say which kind of chat
        self.assertEqual(self.m(chat_id="508", msg_id="123", _placement=none, **bare).root_id, "90", "not known")
        self.b._chats["507"] = (0.0, self.b._chats["507"][1])  # known as ordinary a while ago, thread-first now
        S.chats["507"] = {"id": "507", "type": 2, "name": "示例群", "chatMode": 2}
        self.assertEqual(self.m(chat_id="507", msg_id="124", _placement=none, **bare).root_id, "90", "read again")
        # a read of the chat's record for anything else says its kind too
        S.chats["509"] = {"id": "509", "type": 2, "name": "示例群"}
        self.assertEqual(self.m(chat_id="509", msg_id="125", _placement=none, **bare).root_id, "", "an ordinary one")
        S.chats["509"] = {"id": "509", "type": 2, "name": "示例群", "chatMode": 3}
        self.b._chat_read(self.b._client, "509")
        self.assertEqual(self.m(chat_id="509", msg_id="126", _placement=none, **bare).root_id, "90", "now thread-first")
        # the recent chats say a group is thread-first now
        S.chats["512"] = {"id": "512", "type": 2, "name": "示例群"}
        self.assertEqual(self.m(chat_id="512", msg_id="129", _placement=none, **bare).root_id, "")
        S.api_answers["chats.PullRecentChatsRequest"] = {
            "chats": {"512": {"id": "512", "type": 2, "name": "示例群", "chatMode": 3}}, "orderedChatIds": ["512"]}
        self.b.recent_chats(5)
        self.assertEqual(self.m(chat_id="512", msg_id="128", _placement=none, **bare).root_id, "90")
        # signed out while the record was read: not known; the same account signed in again (a new client) is it
        S.chats["514"] = {"id": "514", "type": 2, "name": "示例群"}
        me, read = self.b._me, self.b._chat_read
        def away(c, chat_id, who=None):
            got = read(c, chat_id, who)
            self.b._me = None
            return got
        self.b._chat_read = away
        self.assertFalse(self.b._plain_group(self.b._client, "514", 2, ME))
        self.b._me, self.b._chat_read = me, read
        self.b._chats.pop("514", None)
        again = type("Again", (), {"get_chat_info": staticmethod(self.b._client.get_chat_info)})()
        self.assertTrue(self.b._plain_group(again, "514", 2, ME), "the same account's new client")
        # another account, or none, asks nothing
        n = len(S.chat_info_calls)
        self.assertFalse(self.b._plain_group(self.b._client, "513", 2, "other"))
        self.assertFalse(self.b._plain_group(self.b._client, "513", 2, ""))
        self.assertEqual(len(S.chat_info_calls), n)
        self.assertEqual(self.m(chat_id="504", chat_type=1, root_id="90", anchor="90", content_data=t).placement, "",
                         "a direct chat")
        self.b._peers["501"] = "7003"  # known as a direct chat, though the push did not say so
        self.assertEqual(self.m(chat_id="501", msg_id="109", content_data=t).placement, "")
        self.assertEqual(self.m(scope="topic", anchor="101", content_data=t).placement, "", "a thread-first group's root")
        marked = dict(alone, thread_type=2)  # its marks say a thread reply, it was not read as one: the old way
        self.assertEqual(self.m(msg_id="105", _placement=marked, content_data=t).placement, "")
        S.thread_chats.add("503")  # a thread-first group whose root was read as a plain message
        self.assertEqual(self.m(chat_id="503", msg_id="106", from_id=ME, content_data=t).placement, "", "asked: mine")
        self.assertEqual(self.m(chat_id="503", msg_id="107", content_data=t).placement, "chat", "others' never ask")
        copy = {"thread_type": 0, "send_to_chat": False, "sync_type": 1, "sync_related": "101", "sync_root": "90"}
        c = self.m(msg_id="108", _placement=copy, content_data=t)
        self.assertEqual((c.same_as, c.placement), ("101", "chat"), "the main stream's copy of a thread reply")
        self.assertEqual(self.m(_placement=sync, **thread).same_as, "", "the reply itself is no copy")
        nameless = dict(copy, sync_related="")
        self.assertEqual(self.m(msg_id="110", _placement=nameless, content_data=t).same_as, "110", "still a copy")
        for i in range(larkagentx.SYNCED_KEEP + 5):
            self.m(msg_id=str(1000 + i), _placement=sync, **thread)
        self.assertEqual(len(self.b._synced), larkagentx.SYNCED_KEEP, "only the newest are remembered")
        self.assertIn(str(1000 + larkagentx.SYNCED_KEEP + 4), self.b._synced)

    def test_a_quote_of_a_thread_root_shows_in_the_main_stream(self):
        # quoting, in the main stream, a message that is already a thread's root: its threadId is that thread's
        # (the anchor is the root), and only threadMessageType tells it from a reply in the thread
        from larkx._fake import text_data

        S.chats["500"] = {"id": "500", "type": 2, "name": "示例项目群"}
        none = {"thread_type": 0, "thread_known": True, "send_to_chat": False, "sync_type": 0, "sync_related": "",
                "sync_root": ""}
        opened = dict(root_id="120", parent_id="120", anchor="120", scope="topic", content_data=text_data("x"))
        q = self.m(msg_id="121", _placement=none, **opened)
        self.assertEqual((q.placement, q.root_id, q.thread_id, q.parent_id), ("chat", "", "", "120"),
                         "a quote of a thread's root shows in the main stream")
        r = self.m(msg_id="122", _placement=dict(none, thread_type=2), **opened)
        self.assertEqual((r.placement, r.root_id, r.thread_id), ("thread", "120", "120"), "a reply in that thread")
        # as Feishu pushes it: threadMessageType unset on the quote
        q = self.m(msg_id="123", _placement=dict(none, thread_known=False), **opened)
        self.assertEqual((q.placement, q.root_id, q.thread_id, q.parent_id), ("chat", "", "", "120"),
                         "the quote as pushed: no thread kind at all")

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


class SwitchTest(Base):
    """A sign-out (and the next account's sign-in) while a request of the session before is still out: its answer is
    never the next session's."""

    def test_a_restore_begun_before_a_logout_does_not_take_the_session(self):
        b = self.backend()
        real = b._check_ticket

        def ticket(auth):  # the restore asks the service; the user signs out meanwhile
            real(auth)
            b.logout()

        b._check_ticket = ticket
        with self.assertRaises(BackendError):
            b.restore(dict(CREDS))
        self.assertEqual(b._session(), (None, None), "the account before came back after the logout")
        self.assertEqual(self.rotated, [])
        b._check_ticket = real
        self.assertEqual(b.restore(dict(CREDS)).id, ME, "a restore begun after it works")

    def test_what_the_session_before_asked_is_not_kept_for_the_next(self):
        S.chats["500"] = {"id": "500", "type": 2, "name": "示例项目群", "memberCount": 5, "lastMessagePosition": 10}
        b = self.signed_in()
        S.api_answers["chatters.PullChattersByIdsRequest"] = {"chatters": {"7004": {"name": "示例同事丁"}}}
        real_name = b._chatter_name

        def name_then_out(client, uid):
            out = real_name(client, uid)
            b.logout()  # signed out while the name was on its way
            return out

        b._chatter_name = name_then_out
        self.assertEqual(b.user_name("7004"), "示例同事丁")
        self.assertEqual(b._names, {}, "a name the session before asked for, kept for the next")
        b = self.signed_in()
        real_chat = b._chat_of

        def chat_then_out(chat_id, info):
            out = real_chat(chat_id, info)
            b.logout()
            return out

        b._chat_of = chat_then_out
        self.assertEqual(b.chat_info("500").id, "500")
        self.assertEqual(b._chats, {}, "a chat the session before asked about, kept for the next")

    def test_a_logout_forgets_the_chats(self):
        S.chats["500"] = {"id": "500", "type": 2, "name": "示例项目群", "memberCount": 5, "lastMessagePosition": 10}
        b = self.signed_in()
        b.chat_info("500")
        b._learn_peer("600", "7003", ME)
        self.assertIn("500", b._chats)
        self.assertEqual(b._peers, {"600": "7003"})
        b.logout()
        self.assertEqual((b._chats, b._peers), ({}, {}), "the account before's chats and its direct chats' people")

    def test_the_same_account_scanned_again_keeps_what_it_asked(self):
        S.chats["500"] = {"id": "500", "type": 2, "name": "示例项目群", "memberCount": 5, "lastMessagePosition": 10}
        b = self.signed_in()
        real_chat = b._chat_of

        def chat_then_again(chat_id, info):  # the same account signs in again (a new client) while it is asked
            out = real_chat(chat_id, info)
            b.restore(dict(CREDS))
            return out

        b._chat_of = chat_then_again
        b.chat_info("500")
        self.assertIn("500", b._chats, "the same account's answer is its own")

    def test_a_restore_never_takes_the_place_of_another_accounts_session(self):
        b = self.backend()
        other = b.restore(dict(CREDS, user_id="7999", user_name="另一个示例用户"))  # signed in as another (a QR)
        with self.assertRaises(BackendError):
            b.restore(dict(CREDS))  # the saved sign-in of the one before, restored late
        self.assertEqual(b._session()[1].id, other.id, "a late restore took the other account's place")


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

    def test_into_the_thread_and_also_the_chat(self):
        b = self.b
        real = larkagentx.also_to_chat
        larkagentx.also_to_chat = lambda pkt, parent, mark: dict(pkt, parent=parent, mark=mark)  # real: smoke test
        self.addCleanup(setattr, larkagentx, "also_to_chat", real)
        b.send("500", "也发到群里", reply_to="97", root="90", to_chat=True)
        self.assertEqual((self.last()["root"], self.last()["reply_in_thread"]), ("90", True))
        self.assertEqual((self.last()["parent"], self.last()["mark"]), ("97", "sync"), "quotes the request")
        b._synced["98"] = "send"  # the request carried the older mark: the reply carries it too
        b.send("500", "旧标记", reply_to="98", root="90", to_chat=True)
        self.assertEqual((self.last()["parent"], self.last()["mark"]), ("98", "send"))
        b.send("500", "只在话题里", reply_to="97", root="90")
        self.assertNotIn("mark", self.last(), "only in the thread: as before")
        b.send("502", "话题群", reply_to="60", root="60", to_chat=True)
        self.assertNotIn("mark", self.last(), "a thread-first group has no main stream to copy to")
        larkagentx.also_to_chat = lambda pkt, parent, mark: 1 / 0
        with self.assertRaises(SendRejected):  # the packet could not be made: surely not sent
            b.send("500", "x", reply_to="97", root="90", to_chat=True)

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

    def test_a_text_that_is_a_link(self):
        def pb(*fields):  # a protobuf message of string fields: (number, text)
            out = b""
            for n, text in fields:
                raw = text.encode("utf-8")
                out += bytes([(n << 3) | 2, len(raw)]) + raw
            return out

        url = "https://example.com/articles/7699"
        link = {"richText": {"elementIds": ["a"], "elements": {"dictionary": {
            "a": {"tag": 6, "property": pb((1, url), (2, "示例文章标题"))}}}}}
        bare = {"richText": {"elementIds": ["a"], "elements": {"dictionary": {"a": {"tag": 6, "property": pb((1, url))}}}}}
        mixed = {"richText": {"elementIds": ["t", "a"], "elements": {"dictionary": {
            "t": {"tag": 1, "property": "推荐看看 ".encode("utf-8")}, "a": {"tag": 6, "property": pb((2, url), (3, url))}}}}}
        inner = {"richText": {"innerText": "只有纯文本 " + url, "elements": {"dictionary": {}}}}
        S.history["600"] = {1: frame_msg("601", "600", 1, link), 2: frame_msg("602", "600", 2, bare),
                            3: frame_msg("603", "600", 3, mixed), 4: frame_msg("604", "600", 4, inner)}
        S.chats["600"] = {"id": "600", "type": 2, "name": "示例群", "lastMessagePosition": 4}
        texts = [m.text for m in self.b.history("600", None, 10)]
        self.assertEqual(texts, ["示例文章标题 " + url, url, "推荐看看 " + url, "只有纯文本 " + url])

    def test_a_link_read_by_its_own_fields(self):
        def pb(*fields):
            out = b""
            for n, text in fields:
                raw = text.encode("utf-8")
                out += bytes([(n << 3) | 2, len(raw)]) + raw
            return out

        url, pc = "https://example.com/r", "https://example.com/pc"
        cases = [
            (pb((1, url), (3, "README")), "README " + url),  # words that look like a word still count
            (pb((2, "https://shown.example.com"), (8, pc)), "https://shown.example.com " + pc),  # pcHref, not the words
            (pb((1, url)), url),
        ]
        for i, (prop, want) in enumerate(cases):
            data = {"richText": {"elementIds": ["a"], "elements": {"dictionary": {"a": {"tag": 6, "property": prop}}}}}
            S.history["62%d" % i] = {1: frame_msg("63%d" % i, "62%d" % i, 1, data)}
            S.chats["62%d" % i] = {"id": "62%d" % i, "type": 2, "name": "示例群", "lastMessagePosition": 1}
            self.assertEqual([m.text for m in self.b.history("62%d" % i, None, 5)], [want])

    def test_order_children_and_a_bare_at(self):
        data = {"richText": {"elementIds": ["p"], "elements": {"dictionary": {
            "p": {"tag": 3, "property": "".encode("utf-8"), "childIds": ["t2", "t1"]},
            "t1": {"tag": 1, "property": "二".encode("utf-8")},
            "t2": {"tag": 1, "property": "一".encode("utf-8")}}}}}
        numeric = {"richText": {"elements": {"dictionary": {
            "10": {"tag": 1, "property": "后".encode("utf-8")}, "2": {"tag": 1, "property": "先".encode("utf-8")}}}}}
        bare_at = {"richText": {"elementIds": ["a"], "elements": {"dictionary": {
            "a": {"tag": 5, "property": json.dumps({"userId": "7001"}).encode("utf-8")}}}}}
        S.history["640"] = {1: frame_msg("641", "640", 1, data), 2: frame_msg("642", "640", 2, numeric),
                            3: frame_msg("643", "640", 3, bare_at)}
        S.chats["640"] = {"id": "640", "type": 2, "name": "示例群", "lastMessagePosition": 3}
        self.assertEqual([m.text for m in self.b.history("640", None, 5)], ["一二", "先后", "[@]"])

    def test_a_text_with_no_content_takes_its_preview_links(self):
        url = "https://example.com/articles/42"
        S.history["650"] = {1: frame_msg("651", "650", 1, {}), 2: frame_msg("652", "650", 2, {})}
        S.chats["650"] = {"id": "650", "type": 2, "name": "示例群", "lastMessagePosition": 2}
        S.api_answers["messages.PullMessagesByPositionsRequest"] = {"messages": {
            "1": {"id": "651", "type": 4, "urlPreviewHangPointMap": {"h1": {"url": url, "previewId": "p1"}}},
            "2": {"id": "652", "type": 4, "isEncrypted": True, "content": b""}}}
        larkagentx._SHAPES_SEEN.clear()
        with self.assertLogs("openmate.larkx", level="INFO") as logs:
            msgs = self.b.history("650", None, 10)
        self.assertEqual([m.text for m in msgs], [url, ""])
        self.assertTrue(any('"isEncrypted": true' in line for line in logs.output), logs.output)
        self.assertFalse(any(url in line for line in logs.output), "no content in the log")

    def test_a_text_held_by_feishus_data_loss_policy(self):
        S.history["660"] = {1: frame_msg("661", "660", 1, {}), 2: frame_msg("662", "660", 2, {})}
        S.chats["660"] = {"id": "660", "type": 2, "name": "示例群", "lastMessagePosition": 2}
        S.api_answers["messages.PullMessagesByPositionsRequest"] = {"messages": {
            "1": {"id": "661", "type": 4, "contentState": {"dlpReviewState": 3}, "characterLength": 40},
            "2": {"id": "662", "type": 4, "contentState": {"dlpReviewState": "IN_PROGRESS"}, "characterLength": 12}}}
        larkagentx._SHAPES_SEEN.clear()
        with self.assertLogs("openmate.larkx", level="INFO") as logs:
            msgs = self.b.history("660", None, 10)
        self.assertEqual(msgs[0].text, larkagentx.DLP_BLOCKED_TEXT, "blocked: said so, never worked around")
        self.assertEqual(msgs[1].text, "", "under review: left empty (never archived), read again by the core")
        self.assertEqual([m.blocked for m in msgs], [True, False], "the mark, not the text, keeps it from the archive")
        self.assertTrue(any('"dlpReviewState": 3' in line for line in logs.output), logs.output)

    def test_a_held_text_is_not_shown_through_its_preview(self):
        url = "https://example.com/held"
        S.history["670"] = {1: frame_msg("671", "670", 1, {}), 2: frame_msg("672", "670", 2, {})}
        S.chats["670"] = {"id": "670", "type": 2, "name": "示例群", "lastMessagePosition": 2}
        hang = {"h": {"url": url}}
        S.api_answers["messages.PullMessagesByPositionsRequest"] = {"messages": {
            "1": {"id": "671", "contentState": {"dlpReviewState": 3}, "urlPreviewHangPointMap": hang},
            "2": {"id": "672", "contentState": {"dlpReviewState": 1}, "urlPreviewHangPointMap": hang}}}
        msgs = self.b.history("670", None, 10)
        self.assertEqual(msgs[0].text, larkagentx.DLP_BLOCKED_TEXT)
        self.assertEqual(msgs[1].text, "", "not its preview link either")

    def test_a_text_read_as_nothing_is_noted(self):
        empty = {"richText": {"elementIds": ["x"], "elements": {"dictionary": {"x": {"tag": 35, "property": b"\x08\x01"}}}}}
        S.history["610"] = {1: frame_msg("611", "610", 1, empty)}
        S.chats["610"] = {"id": "610", "type": 2, "name": "示例群", "lastMessagePosition": 1}
        larkagentx._SHAPES_SEEN.clear()
        with self.assertLogs("openmate.larkx", level="INFO") as logs:
            self.b.history("610", None, 10)
        self.assertTrue(any("read as nothing" in line and "35:" in line for line in logs.output), logs.output)

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

    def test_history_tells_a_quote_from_a_thread_reply(self):
        from larkx._fake import text_data

        S.chats["620"] = {"id": "620", "type": 2, "name": "示例群", "lastMessagePosition": 4}
        base = {"chat_id": "620", "chat_type": 2, "msg_type": 4, "from_id": "7002", "create_time": 1763000000,
                "scope": "topic", "content": "", "content_data": text_data("x"), "at_me": False}
        S.history["620"] = {
            1: dict(base, msg_id="301", position=1, root_id="", parent_id="", anchor="", scope="chat"),
            2: dict(base, msg_id="302", position=2, root_id="301", parent_id="301", anchor="301"),  # a quote
            3: dict(base, msg_id="303", position=3, root_id="301", parent_id="301", anchor="301"),  # sent to chat
            4: dict(base, msg_id="304", position=4, root_id="301", parent_id="301", anchor="301"),  # no mark
        }
        S.api_answers["messages.PullMessagesByPositionsRequest"] = {"messages": {
            "2": {"id": "302"}, "3": {"id": "303", "threadMessageType": "THREAD_REPLY_MESSAGE", "sendToChat": True}}}
        msgs = {m.id: m for m in self.b.history("620", None, 10)}
        self.assertEqual((msgs["302"].root_id, msgs["302"].thread_id, msgs["302"].parent_id), ("", "", "301"),
                         "a quote in the main stream")
        self.assertEqual((msgs["303"].root_id, msgs["303"].placement), ("301", "thread_chat"), "a reply in the thread")
        self.assertEqual(msgs["304"].root_id, "301", "its entity not in the answer: the old reading")
        self.assertNotIn("303", self.b._synced, "a history page's marks never push out a live request's")
        asked = [d for c, d in S.api_calls if c == "messages.PullMessagesByPositionsRequest"]
        self.assertEqual([d["positions"] for d in asked], [[2, 3, 4]], "asked once, only for those")
        S.api_calls.clear()
        S.history["621"] = {1: dict(S.history["620"][2], chat_id="621", position=1)}
        S.chats["621"] = dict(S.chats["620"], id="621", lastMessagePosition=1)
        S.api_answers["messages.PullMessagesByPositionsRequest"] = ValueError("down")
        self.assertEqual([m.root_id for m in self.b.history("621", None, 10)], ["301"], "a failed read: the old way")
        # the chat's kind cannot be read: the old reading, without asking the entities or the chat again per message
        del S.api_answers["messages.PullMessagesByPositionsRequest"]
        S.history["622"] = {p: dict(S.history["620"][2], chat_id="622", msg_id=str(400 + p), position=p)
                            for p in range(1, 6)}
        S.chats["622"] = dict(S.chats["620"], id="622", lastMessagePosition=5)
        self.assertEqual(len(self.b.history("622", None, 10)), 5)
        S.chats["622"] = R.ConnectionError("down")
        self.b._chats.pop("622", None)
        S.api_calls.clear()
        n = len(S.chat_info_calls)
        msgs = self.b.history("622", 6, 10)
        self.assertEqual([m.root_id for m in msgs], ["301"] * 5)
        self.assertEqual(len(S.chat_info_calls) - n, 1, "asked once for the page")
        self.assertEqual([c for c, _ in S.api_calls if c == "messages.PullMessagesByPositionsRequest"], [])

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

    def test_recent_chats_a_status_is_a_failure_not_none(self):
        # the service answered only a status: the catch-up must not count the initial history read
        S.api_answers["chats.PullRecentChatsRequest"] = {"status": 503}
        S.api_answers["feed.PullFeedCardsRequest"] = {"status": 503}
        with self.assertRaises(BackendError) as e:
            self.b.recent_chats(5)
        self.assertEqual(e.exception.kind, "unreachable")
        # one form answering is enough
        S.api_answers["feed.PullFeedCardsRequest"] = {"cards": [{"id": "500"}], "chats": {"500": S.chats["500"]}}
        self.assertEqual([c.id for c in self.b.recent_chats(5)], ["500"])

    def test_history_of_a_chat_whose_info_is_only_a_status(self):
        S.chats["503"] = {"status": 503}
        with self.assertRaises(BackendError) as e:
            self.b.history("503", None, 5)
        self.assertEqual(e.exception.kind, "unreachable")

    def test_history_answered_with_only_a_status_is_a_failure(self):
        S.api_answers["messages.PullMessagesByPositionsRequest"] = {"status": 503}
        with self.assertRaises(BackendError) as e:
            self.b.history("500", None, 5)
        self.assertEqual(e.exception.kind, "unreachable", "not an empty chat: the catch-up keeps its place")
        S.api_answers["messages.PullMessagesByPositionsRequest"] = {"messages": {}}
        S.history["500"] = {}
        self.assertEqual(self.b.history("500", None, 5), [], "an answer with no messages: every one recalled")

    def test_recent_chats_without_their_payload_are_a_failure(self):
        S.api_answers["chats.PullRecentChatsRequest"] = {"status": 200}
        S.api_answers["feed.PullFeedCardsRequest"] = {"status": 200}
        with self.assertRaises(BackendError):
            self.b.recent_chats(5)
        S.api_answers["feed.PullFeedCardsRequest"] = {"cards": [], "chats": {}}
        self.assertEqual(self.b.recent_chats(5), [], "an answer that says none: none")

    def test_history_lost_by_the_first_answer_is_a_failure(self):
        S.history["500"] = {}  # LarkAgentX's read found nothing (a 503 then)…
        S.api_answers["messages.PullMessagesByPositionsRequest"] = {"messages": {"10": {"id": "110"}}}  # …now there is
        with self.assertRaises(BackendError) as e:
            self.b.history("500", None, 5)
        self.assertEqual(e.exception.kind, "unreachable")

    def test_an_empty_business_answer_is_none(self):
        S.api_answers["chats.PullRecentChatsRequest"] = {"hasMore": False}  # protobuf leaves empty lists out
        self.assertEqual(self.b.recent_chats(5), [])

    def test_reactions_a_status_packet_leaves_unknown(self):
        client, _ = self.b._signed_in()
        orig = client.api
        self.addCleanup(setattr, client, "api", orig)
        calls = []

        def answer(kind, data=None, raw=None):
            calls.append(list(data["messageIds"]))
            return {"messageReactions": []} if len(calls) == 1 else {"status": 200, "cmd": 24}  # the 2nd: a packet

        client.api = answer
        ids = [str(900 + i) for i in range(larkagentx.REACTIONS_ASK_MOST + 2)]
        got = self.b.my_reactions(ids)
        self.assertEqual(got.unasked, set(calls[1]), "the batch answered by a packet alone is unknown")
        client.api = lambda kind, data=None, raw=None: {"status": 200, "cmd": 24}
        with self.assertRaises(BackendError):
            self.b.my_reactions(["901"])  # nothing answered at all: an error, as before

    def test_done_cards_both_forms(self):
        f = larkagentx.done_cards
        # read back (protobuf_to_dict): numbers, each card's own feedType
        self.assertEqual(f([{"id": "500", "type": 1, "feedType": 2, "updateTime": 1763000000, "updateTimeMs": 1763000000500},
                            {"id": "501", "type": 1, "feedType": 1, "updateTime": 1},  # in the inbox
                            {"id": "doc-1", "type": 3, "feedType": 2},  # a document's card
                            {"id": "502", "type": 1, "feedType": 2, "status": 3},  # deleted
                            {"id": "503", "type": 1, "feedType": 2, "updateTime": 1763000001},
                            "junk", {"type": 1, "feedType": 2}]),
                         {"500": 1763000000.5, "503": 1763000001.0})
        # a push (MessageToDict): names, int64 as strings, the push's box for its cards
        self.assertEqual(f([{"id": "500", "type": "CHAT", "updateTime": "1763000002"}, {"id": "9", "type": "THREAD"}], "DONE"),
                         {"500": 1763000002.0})
        self.assertEqual(f([{"id": "500", "type": "CHAT"}], "INBOX"), {})
        self.assertEqual(f([{"id": "500", "type": "CHAT", "feedType": "INBOX"}], "DONE"), {}, "a card's own feedType first")
        self.assertEqual(f([{"id": "500", "type": 1, "feedType": 2}], None, {"501"}), {}, "only the chats asked about")
        self.assertEqual(f([{"id": "500", "type": 1, "feedType": 2}]), {"500": 0.0}, "when unknown")

    def test_chat_done_states_by_ids(self):
        self.assertTrue(self.b.done_marks)
        S.api_answers["feed.PullFeedCardsByIdsRequest"] = {"cards": {
            "500": {"id": "500", "type": 1, "feedType": 2, "updateTimeMs": 1763000000500},
            "501": {"id": "501", "type": 1, "feedType": 1},
            "502": {"type": 1, "feedType": 2, "updateTime": 1763000002},  # keyed by its id, without it inside
            "504": {"id": "504", "type": 1}}}  # no feedType: not an answer
        self.assertEqual(self.b.chat_done_states(["500", "501", "502", "503", "504", "500", ""]),
                         {"500": 1763000000.5, "501": None, "502": 1763000002.0},
                         "answered: done (when) or in the inbox (None); no card or no feedType is no answer")
        S.api_answers["feed.PullFeedCardsByIdsRequest"] = {"status": 503}  # an error packet: no cards at all
        self.assertEqual(self.b.chat_done_states(["500"]), {}, "nothing said: nothing answered")
        S.api_answers["feed.PullFeedCardsByIdsRequest"] = {"cards": {"500": {"id": "500", "type": 1, "feedType": 2}}}
        self.assertEqual(S.api_calls[-2], ("feed.PullFeedCardsByIdsRequest", {"feedCardIdentities": [
            {"id": "500", "type": 1}, {"id": "501", "type": 1}, {"id": "502", "type": 1}, {"id": "503", "type": 1},
            {"id": "504", "type": 1}]}))
        n = len(S.api_calls)
        self.b.chat_done_states([str(i) for i in range(120)])
        self.assertEqual(len(S.api_calls) - n, 3, "at most DONE_ASK_MOST a request")

    def test_chat_done_states_a_failed_batch_leaves_only_its_chats_out(self):
        calls = []

        def answer(type_or_cmd, data=None, raw=None):
            calls.append(data)
            if len(calls) == 2:
                raise R.ConnectionError("down")
            return {"cards": {c["id"]: {"id": c["id"], "type": 1, "feedType": 2} for c in data["feedCardIdentities"]}}

        client, _ = self.b._signed_in()
        orig = client.api
        client.api = answer
        self.addCleanup(setattr, client, "api", orig)
        out = self.b.chat_done_states([str(i) for i in range(110)])
        self.assertEqual(sorted(out, key=int), [str(i) for i in range(50)] + [str(i) for i in range(100, 110)])
        self.assertTrue(all(v == 0.0 for v in out.values()))
        client.api = lambda *a, **k: (_ for _ in ()).throw(R.ConnectionError("down"))
        with self.assertRaises(BackendError) as e:
            self.b.chat_done_states(["500"])
        self.assertEqual(e.exception.kind, "unreachable", "none answered: an error")

    def test_every_group_page_by_page_newest_joined_first(self):
        client, me = self.b._signed_in()
        asked = []

        def answer(kind, data=None, raw=None):
            asked.append((kind, data))
            if kind == "chats.PullGroupChatsRequest":
                if data["updateTime"] == 0:
                    return {"chatIds": ["g1", "g2", "g4"], "hasMore": True, "nextUpdateTime": 100,
                            "chatJoinTimes": {"g1": 10, "g2": 20, "g4": 15}}
                return {"chatIds": ["g3", "g1"], "hasMore": False, "nextUpdateTime": 200}
            if kind == "chats.PullChatsByIdsRequest":
                every = {"g1": {"id": "g1", "type": 2, "name": "项目群", "joinTimeMs": 10000, "memberCount": 9},
                         "g2": {"id": "g2", "type": 2, "name": "解散的群", "isDissolved": True},
                         "g3": {"id": "g3", "type": 3, "name": "话题群", "joinTimeMs": 30000},
                         "g4": {"id": "g4", "type": 2, "name": "冻结的群", "isFrozen": True}}
                return {"chats": {c: every[c] for c in data["chatIds"] if c in every}}
            raise ValueError(kind)

        orig = client.api
        client.api = answer
        self.addCleanup(setattr, client, "api", orig)
        self.assertEqual(self.b.group_candidates(10), [], "nothing until read")
        self.b._groups_work(client, me)
        groups = self.b.group_candidates(10)
        self.assertEqual([(g.id, g.name, g.kind) for g in groups], [("g3", "话题群", "group"), ("g1", "项目群", "group")])
        self.assertEqual(groups[1].members, 9)
        self.assertEqual([d["updateTime"] for k, d in asked if k == "chats.PullGroupChatsRequest"], [0, 100])
        self.assertEqual([d["chatIds"] for k, d in asked if k == "chats.PullChatsByIdsRequest"], [["g1", "g2", "g4", "g3"]])
        self.assertEqual([g.id for g in self.b.group_candidates(1)], ["g3"])
        self.b.logout()
        self.assertEqual(self.b.group_candidates(10), [], "another sign-in starts from nothing")

    def set_groups(self, me, groups):
        with self.b._groups_mu:
            self.b._groups_owner = me.id
            self.b._groups = [larkagentx.Chat(id=i, kind="group", name=n, members=m, topic=t) for i, n, m, t in groups]
            self.b._groups_join = {g[0]: 100 - k for k, g in enumerate(groups)}

    def test_groups_change_with_pushes_and_messages(self):
        client, me = self.b._signed_in()
        self.set_groups(me, [("g1", "旧名", 5, True), ("g3", "话题群", 3, False), ("g4", "围观的群", 2, False)])
        self.b._apply_chats({"g9": {"id": "g9", "type": 2, "name": "新建的群"}, "g1": {"id": "g1", "type": 2, "name": "新名"},
                             "g4": {"id": "g4", "role": 2}, "d1": {"id": "d1", "type": 1, "name": "某人"}}, ["g3"], me)
        groups = self.b.group_candidates(10)
        self.assertEqual([(g.id, g.name, g.members, g.topic) for g in groups], [("g1", "新名", 5, True)],
                         "renamed with what it had kept; left, and only looked in: gone")
        self.assertTrue(self.b._groups_soon, "a group the list lacks: read again soon, not taken from the push alone")
        self.b._apply_chats({"g1": {"id": "g1", "isDissolved": True}}, [], me)
        self.assertEqual(self.b.group_candidates(10), [], "dissolved: gone, even without its type")
        self.assertNotIn("g1", self.b._groups_join, "joined again later: a new time")
        with self.b._lock:
            self.b._chats["g8"] = (time.monotonic() + 600, larkagentx.Chat(id="g8", kind="group", name="刚被拉进的群"))
            self.b._chats["d2"] = (time.monotonic() + 600, larkagentx.Chat(id="d2", kind="dm", name="某人"))
        self.b._groups_soon = False
        self.b._seen_group("d2", me)
        self.b._seen_group("unknown", me)
        self.assertFalse(self.b._groups_soon, "a direct chat, or one not read: nothing")
        self.b._seen_group("g8", me)
        self.assertEqual(self.b.group_candidates(10), [], "a message alone does not say the account is a member…")
        self.assertTrue(self.b._groups_soon, "…the next read does")
        self.set_groups(me, [("g8", "群八", 2, False)])
        other = larkagentx.Account("someone-else", "别人")
        self.b._apply_chats({"g8": {"id": "g8", "isDissolved": True}}, [], other)
        self.assertEqual([g.id for g in self.b.group_candidates(10)], ["g8"], "another account's push changes nothing")

    def test_a_push_for_another_account_is_not_put_on_this_ones_read(self):
        client, me = self.b._signed_in()
        self.set_groups(me, [("g1", "群一", 3, False)])
        with self.b._groups_mu:
            self.b._groups_busy = True  # a read of every group is out
        other = larkagentx.Account("someone-else", "别人")
        self.b._apply_chats({"g1": {"id": "g1", "isDissolved": True}}, [], other)  # heard on the link of another
        real = self.b._read_groups
        self.addCleanup(setattr, self.b, "_read_groups", real)
        self.b._read_groups = lambda c: ([larkagentx.Chat(id="g1", kind="group", name="群一")], {"g1": 1}, True, {"g1"},
                                         set())
        self.b._groups_work(client, me)
        self.assertEqual([g.id for g in self.b.group_candidates(10)], ["g1"], "another account's push put on the read")

    def test_groups_left_or_dissolved_are_told_to_the_core(self):
        client, me = self.b._signed_in()
        gone, whose = [], set()
        self.b.on_chat_gone = lambda cid, account="": (gone.append(cid), whose.add(account))
        self.set_groups(me, [("g1", "群一", 3, False), ("g2", "群二", 3, False), ("g3", "群三", 3, False)])
        self.b._apply_chats({"g1": {"id": "g1", "isDissolved": True}, "g2": {"id": "g2", "type": 2, "name": "群二新名"}},
                            ["g9"], me)
        self.assertEqual(sorted(gone), ["g1", "g9"], "dissolved, and one the push says is no longer valid")
        self.assertEqual(whose, {me.id}, "said for the account it was heard for (the core keeps it only for that one)")
        self.assertEqual([g.id for g in self.b.group_candidates(10)], ["g2", "g3"])

        def read_whole(ids, whole=True, listed=None, ended=None):
            return lambda c: ([larkagentx.Chat(id=i, kind="group", name=i) for i in ids], {i: 1 for i in ids}, whole,
                              set(ids) | set(listed or ()), set(ended or ()))

        real = self.b._read_groups
        self.addCleanup(setattr, self.b, "_read_groups", real)
        del gone[:]
        self.b._groups_at = 0.0
        self.b._read_groups = read_whole(["g2"])
        self.b._groups_work(client, me)
        self.assertEqual(gone, ["g3"], "known from before (a part read, or pushed): compared")
        del gone[:]
        self.b._groups, self.b._groups_whole_ids, self.b._groups_at = [], set(), 0.0
        self.b._groups_work(client, me)
        self.assertEqual(gone, [], "the first read of all compares with nothing")
        self.b._read_groups = read_whole(["g2", "g4"], whole=False)
        self.b._groups_work(client, me)
        self.assertEqual(gone, [], "a part read says nothing about the others")
        self.b._read_groups = read_whole(["g2"])
        self.b._groups_work(client, me)
        self.assertEqual(gone, ["g4"], "a whole read after a whole read: the one no longer listed was left")
        # listed without its details: kept, not left
        del gone[:]
        self.b._read_groups = read_whole(["g2", "g5"])
        self.b._groups_work(client, me)
        self.b._read_groups = read_whole(["g2"], listed=["g5"])
        self.b._groups_work(client, me)
        self.assertEqual(gone, [], "g5 is still listed, only its details did not come")
        # a part read between two whole reads does not hide a group left
        self.b._read_groups = read_whole(["g2"], whole=False)
        self.b._groups_work(client, me)
        self.b._read_groups = read_whole(["g2"])
        self.b._groups_work(client, me)
        self.assertEqual(gone, ["g5"], "listed by the last whole read, gone from this one")
        # listed, but its details say it is over (dissolved, or only looked into now)
        del gone[:]
        self.b._read_groups = read_whole(["g2", "g6"])
        self.b._groups_work(client, me)
        self.b._read_groups = read_whole(["g2"], listed=["g6"], ended=["g6"])
        self.b._groups_work(client, me)
        self.assertEqual(gone, ["g6"])
        # a part read keeps what it did not reach on the list (找群 still finds a quiet group)
        self.b._read_groups = read_whole(["g2", "g7"])
        self.b._groups_work(client, me)
        self.b._read_groups = read_whole(["g7"], whole=False)
        self.b._groups_work(client, me)
        self.assertEqual(sorted(g.id for g in self.b.group_candidates(10)), ["g2", "g7"])
        # a whole answer of no group at all (the service said so): the last ones were left
        del gone[:]
        self.b._read_groups = read_whole([])
        self.b._groups_work(client, me)
        self.assertEqual(sorted(gone), ["g2", "g7"])
        self.assertEqual(self.b.group_candidates(10), [])

    def test_what_is_heard_during_a_read_is_put_on_its_result(self):
        client, me = self.b._signed_in()
        self.set_groups(me, [("g1", "要退的群", 3, False)])
        real = self.b._read_groups

        def read(c):
            self.b._apply_chats({}, ["g1"], me)  # left while the read was out
            self.b._apply_chats({"g2": {"id": "g2", "type": 2, "name": "群二新名"}}, [], me)
            return [larkagentx.Chat(id="g1", kind="group", name="要退的群"), larkagentx.Chat(id="g2", kind="group", name="群二")], \
                {"g1": 5, "g2": 4}, True, {"g1", "g2"}, set()

        self.b._read_groups = read
        self.addCleanup(setattr, self.b, "_read_groups", real)
        gone = []
        self.b.on_chat_gone = lambda cid, account="": gone.append(cid)
        with self.b._groups_mu:
            self.b._groups_busy = True
        self.b._groups_work(client, me)
        self.assertEqual(gone, ["g1"], "told once, when it was pushed, not again when the read's result came")
        self.assertEqual([(g.id, g.name) for g in self.b.group_candidates(10)], [("g2", "群二新名")],
                         "the read's older list does not bring it back, nor the old name")
        self.b._apply_chats({"g2": {"id": "g2", "type": 2, "name": "群二又改名"}}, [], me)
        self.assertEqual([g.name for g in self.b.group_candidates(10)], ["群二又改名"], "a later push stays")
        self.assertEqual(self.b._groups_delta, [])

    def test_the_end_said_outright_or_not_at_all(self):
        client, _ = self.b._signed_in()
        orig = client.api
        self.addCleanup(setattr, client, "api", orig)
        client.api = lambda kind, data=None, raw=None: {"hasMore": False}
        groups, _, whole, listed, ended = self.b._read_groups(client)
        self.assertEqual((groups, whole, listed), ([], True, set()), "in no group: said outright")
        client.api = lambda kind, data=None, raw=None: {"nextUpdateTime": 5}
        _, _, whole, _, _ = self.b._read_groups(client)
        self.assertFalse(whole, "neither ids nor an end: not the whole list")

    def test_details_without_a_type_are_not_the_end(self):
        client, me = self.b._signed_in()
        gone = []
        self.b.on_chat_gone = lambda cid, account="": gone.append(cid)
        self.set_groups(me, [("g1", "群一", 3, False)])
        self.b._groups_whole_ids, self.b._groups_at = {"g1"}, 1.0

        def answer(kind, data=None, raw=None):
            if kind == "chats.PullGroupChatsRequest":
                return {"chatIds": ["g1"], "hasMore": False}
            return {"chats": {"g1": {"id": "g1", "name": "群一", "role": "MEMBER", "status": "NORMAL"}}}  # no type

        orig = client.api
        client.api = answer
        self.addCleanup(setattr, client, "api", orig)
        self.b._groups_work(client, me)
        self.assertEqual(gone, [])
        self.assertEqual([g.id for g in self.b.group_candidates(10)], ["g1"], "kept as it was shown")

    def test_a_packet_without_the_list_is_no_read(self):
        client, me = self.b._signed_in()
        gone = []
        self.b.on_chat_gone = lambda cid, account="": gone.append(cid)
        self.set_groups(me, [("g1", "群一", 3, False)])
        self.b._groups_whole_ids = {"g1"}
        self.b._groups_at = 1.0
        orig = client.api
        client.api = lambda kind, data=None, raw=None: {"status": 200, "cmd": 82}
        self.addCleanup(setattr, client, "api", orig)
        self.b._groups_work(client, me)
        self.assertEqual(gone, [], "a status 200 with no list is not 'in no group at all'")
        self.assertEqual([g.id for g in self.b.group_candidates(10)], ["g1"])

    def test_a_push_told_once_even_when_the_read_also_lacks_it(self):
        client, me = self.b._signed_in()
        gone = []
        self.b.on_chat_gone = lambda cid, account="": gone.append(cid)
        self.set_groups(me, [("g1", "群一", 3, False), ("g2", "群二", 3, False)])
        self.b._groups_whole_ids = {"g1", "g2"}
        self.b._groups_at = 1.0
        real = self.b._read_groups

        def read(c):
            self.b._apply_chats({}, ["g1"], me)  # left while the read was out
            return [larkagentx.Chat(id="g2", kind="group", name="群二")], {"g2": 4}, True, {"g2"}, set()

        self.b._read_groups = read
        self.addCleanup(setattr, self.b, "_read_groups", real)
        with self.b._groups_mu:
            self.b._groups_busy = True
        self.b._groups_work(client, me)
        self.assertEqual(gone, ["g1"], "once")

    def test_a_status_packet_keeps_the_list(self):
        client, me = self.b._signed_in()
        self.set_groups(me, [("g1", "群一", 3, False)])
        orig = client.api
        client.api = lambda kind, data=None, raw=None: {"status": 503}
        self.addCleanup(setattr, client, "api", orig)
        self.b._groups_work(client, me)
        self.assertEqual([g.id for g in self.b.group_candidates(10)], ["g1"])

        pages = []

        def answer(kind, data=None, raw=None):
            if kind == "chats.PullGroupChatsRequest":
                pages.append(data["updateTime"])
                if data["updateTime"] == 0:
                    return {"hasMore": True, "nextUpdateTime": 50}  # an empty page, more after it
                return {"chatIds": ["g5"], "hasMore": False}
            return {"chats": {"g5": {"id": "g5", "type": 2, "name": "群五"}}}

        client.api = answer
        self.b._groups_work(client, me)
        self.assertEqual(pages, [0, 50])
        self.assertEqual([g.id for g in self.b.group_candidates(10)], ["g5"])

    def test_reacted_by_both_forms(self):
        f = larkagentx.reacted_by
        d = {"messageReactions": [
                {"messageId": "m1", "reactions": [{"type": "DONE", "userIds": ["me", "u2"], "udpateTimes": {"me": 1763000000}}]},
                {"messageId": "m2", "reactions": [{"type": "OK", "userIds": ["u2"]}]},
                {"messageId": "m3", "reactions": [{"type": "OK", "chatterIds": ["me"], "nanoUpdateTime": 1763000005000000000}]},
                "junk"],
             "selfReactionStatus": [{"userId": "me", "messageId": "m4", "reactionType": "DONE", "nanoUpdateTime": 1763000009000000000},
                                    {"userId": "me", "messageId": "m5", "isCancelled": True}]}
        self.assertEqual(f(d, "me"), {"m1": 1763000000.0, "m3": 1763000005.0, "m4": 1763000009.0})
        self.assertEqual(f(d, "me", {"m1"}), {"m1": 1763000000.0}, "only the messages asked about")
        self.assertEqual(f(d, "me", None, ("done", "OK")), {}, "the types ignored (the /agent's own 🤔 and ❌)")
        self.assertEqual(f(d, ""), {})

    def test_my_reactions_asks_by_message(self):
        client, me = self.b._signed_in()
        S.api_answers["reactions.PullReactionsRequest"] = {"messageReactions": [
            {"messageId": "201", "reactions": [{"type": "DONE", "userIds": [me.id]}]}]}
        self.assertEqual(self.b.my_reactions(["201", "202", "201", ""]), {"201": 0.0})
        self.assertEqual(S.api_calls[-1], ("reactions.PullReactionsRequest", {"messageIds": ["201", "202"]}))
        S.api_answers["reactions.PullReactionsRequest"] = {"status": 503}
        with self.assertRaises(BackendError):
            self.b.my_reactions(["201"])

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

    def test_document_mentions(self):
        d = {"status": "NEW", "docFeed": {"id": "f1", "title": "示例方案", "url": "https://example.feishu.cn/wiki/WikiTok1", "type": "WIKI"},
             "docMessages": {
                 "m1": {"id": "m1", "type": "MENTION", "fromId": "7003", "isAtMe": True, "createTime": "1763300000000",
                        "content": "@示例用户 这一段要改吗？", "extra": '{"comment_id": "c9", "reply_id": "r1"}'},
                 "m2": {"id": "m2", "type": "REPLY", "fromId": "7004", "isAtMe": True,
                        "richText": {"elements": {"e1": {"property": {"text": {"content": "看下这里"}}}}},
                        "extra": '{"data": {"commentId": "c10"}}'},
                 "m3": {"id": "m3", "type": "COMMENT", "fromId": "7003", "isAtMe": False, "content": "not for me"},
                 "m4": {"id": "m4", "type": "MENTION", "fromId": ME, "isAtMe": True, "content": "my own"},
                 "m5": {"id": "m5", "type": "SHARE", "fromId": "7003", "isAtMe": True, "content": "a share"},
                 "m6": {"id": "m6", "type": "MENTION", "fromId": "7005", "isAtMe": True, "content": "no extra"}}}
        got = larkagentx.doc_mentions(d, ME)
        self.assertEqual([(m.id, m.comment_id, m.from_id, m.text, m.kind) for m in got],
                         [("m1", "c9", "7003", "@示例用户 这一段要改吗？", "mention"), ("m2", "c10", "7004", "看下这里", "reply"),
                          ("m6", "", "7005", "no extra", "mention")])
        self.assertEqual((got[0].doc_url, got[0].doc_title, got[0].at), ("https://example.feishu.cn/wiki/WikiTok1", "示例方案", 1763300000.0))
        self.assertEqual(larkagentx.doc_mentions({"docMessages": d["docMessages"]}, ME), [], "no document: nothing")

    def test_a_comment_reply_goes_through_lark_cli(self):
        b = self.backend()
        with self.assertRaises(larkagentx.BackendError):
            b.reply_doc_comment("https://example.feishu.cn/docx/T", "c1", "好")  # no lark-cli on this backend

        class CLI:
            def __init__(self):
                self.calls = []

            def add_reply(self, url, cid, text):
                self.calls.append((url, cid, text))
                return "r9"

        b.cli = CLI()
        self.assertEqual(b.reply_doc_comment("https://example.feishu.cn/docx/T", "c1", "好"), "r9")
        self.assertEqual(b.cli.calls, [("https://example.feishu.cn/docx/T", "c1", "好")])

    def test_a_reaction(self):
        S.api_answers["reactions.PutReactionRequest"] = {}
        self.b.react("500", "101", "THINKING")
        self.assertEqual(S.api_calls[-1], ("reactions.PutReactionRequest", {"messageId": "101", "reactionType": "THINKING"}))
        with self.assertRaises(larkagentx.BackendError):
            self.b.react("500", "", "THINKING")
        del S.api_answers["reactions.PutReactionRequest"]  # refused: a BackendError with no library words
        with self.assertRaises(larkagentx.BackendError) as e:
            self.b.react("500", "102", "NOPE")
        self.assertEqual(e.exception.kind, "unavailable")

    def test_taking_a_reaction_back(self):
        S.api_answers["reactions.DeleteReactionRequest"] = {}
        self.b.unreact("500", "101", "THINKING")
        self.assertEqual(S.api_calls[-1], ("reactions.DeleteReactionRequest", {"messageId": "101", "reactionType": "THINKING"}))
        del S.api_answers["reactions.DeleteReactionRequest"]
        with self.assertRaises(larkagentx.BackendError) as e:
            self.b.unreact("500", "102", "THINKING")
        self.assertEqual(e.exception.kind, "unavailable")

    def test_editing_a_sent_message(self):
        import base64
        import json as _json
        S.api_answers["messages.EditMessageRequest"] = {}
        self.b.edit("500", "101", "进度：第二步")
        name, data = S.api_calls[-1]
        self.assertEqual(name, "messages.EditMessageRequest")
        self.assertEqual((data["msgId"], data["type"]), ("101", 4))
        rich = data["content"]["richText"]
        self.assertEqual(rich["innerText"], "进度：第二步")
        (element,) = rich["elementIds"]
        cell = rich["elements"]["dictionary"][element]
        self.assertEqual(cell["tag"], 1)
        self.assertEqual(_json.loads(base64.b64decode(cell["property"]))["content"], "进度：第二步", "the text element carries it")
        for bad in (("500", "", "x"), ("500", "abc", "x"), ("500", "101", "  ")):
            with self.assertRaises(larkagentx.BackendError):
                self.b.edit(*bad)
        S.api_answers["messages.EditMessageRequest"] = {"status": 403}  # only a status: refused, never "edited"
        with self.assertRaises(larkagentx.BackendError) as e:
            self.b.edit("500", "101", "被拒了")
        self.assertEqual(e.exception.kind, "unavailable")
        S.api_answers["messages.EditMessageRequest"] = {"status": 200}
        self.b.edit("500", "101", "可以")
        del S.api_answers["messages.EditMessageRequest"]
        with self.assertRaises(larkagentx.BackendError) as e:
            self.b.edit("500", "101", "改不了")
        self.assertEqual(e.exception.kind, "unavailable")

    def test_who_is_a_bot(self):
        S.api_answers["chatters.PullChattersByIdsRequest"] = {"chatters": {
            "7003": {"id": "7003", "name": "示例同事丙", "type": "USER"},
            "8001": {"id": "8001", "name": "示例通知机器人", "type": "BOT"},
            "8002": {"name": "示例智能助手", "type": 3},
            "8003": {"id": "8003", "name": "说不清的"},
            "9999": {"id": "9999", "type": "BOT"}}}
        kinds = self.b.people_kinds(["7003", "8001", "8002", "8003", "0", ""])
        self.assertEqual(kinds, {"7003": "user", "8001": "bot", "8002": "bot"}, "only those asked, only known types")
        self.assertEqual(S.api_calls[-1], ("chatters.PullChattersByIdsRequest", {"chatterIds": ["7003", "8001", "8002", "8003"]}))
        self.assertEqual(self.b.people_kinds([]), {})
        self.assertEqual([larkagentx.chatter_kind(v) for v in ("USER", 1, "bot", 2, "AI", "3", 0, None, "UNKNOWN_CHATTER")],
                         ["user", "user", "bot", "bot", "bot", "bot", "", "", ""])

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

    def test_media_failure_kinds(self):
        """What each failed download is (the core turns it into the code Persona's history import acts on)."""
        b = self.b
        host = "https://files.feishu.example/download/messages/101/keys/"
        S.files[host + "img_v3_enc?chat_id=500"] = (b"\x13\x37" * 10, "image/png")
        S.files[host + "img_v3_page?chat_id=500"] = (b"<html>sign in</html>", "text/html")
        S.files[host + "img_v3_k?chat_id=500"] = (b"\x89PNG\r\n\x1a\n" + b"\0" * 20, "image/png")
        ref = larkagentx.media_ref("image", "500", "101", "img_v3_k")
        cases = [(R.HTTPError("x", response=R.Response(404)), "not_found"),
                 (R.HTTPError("x", response=R.Response(410)), "not_found"),
                 (R.HTTPError("x", response=R.Response(401)), "expired"),
                 (R.HTTPError("x", response=R.Response(403)), "rejected"),
                 (R.HTTPError("x", response=R.Response(408)), "unreachable"),
                 (R.HTTPError("x", response=R.Response(429)), "unreachable"),
                 (R.HTTPError("x", response=R.Response(503)), "unreachable"),
                 (R.HTTPError("x", response=R.Response(400)), "unknown"),
                 (R.HTTPError("x", response=R.Response(422)), "unknown"),
                 (R.ChunkedEncodingError("broke off"), "unreachable"),
                 (R.ReadTimeout("slow"), "unreachable"),
                 (R.ConnectionError("down"), "unreachable")]
        for err, kind in cases:
            S.download_error = err
            with self.assertRaises(BackendError, msg=repr(err)) as e:
                b.media(ref, 1 << 20)
            self.assertEqual(e.exception.kind, kind, repr(err))
        S.download_error = None
        with self.assertRaises(BackendError) as e:
            b.media(larkagentx.media_ref("image", "500", "101", "img_v3_enc"), 1 << 20)
        self.assertEqual(e.exception.kind, "unsupported", "a picture that is not one: asking again won't help")
        with self.assertRaises(BackendError) as e:
            b.media(larkagentx.media_ref("image", "500", "101", "img_v3_page"), 1 << 20)
        self.assertEqual(e.exception.kind, "unknown", "a page instead of the picture: not this file's fault")
        odd = {"img_v3_nl": (b"\n\n<html>sign in</html>", "application/octet-stream", "unknown"),
               "img_v3_bom": (b"\xef\xbb\xbf{\"code\": 99991663}", "application/octet-stream", "unknown"),
               "img_v3_json": (b"\x00\x01", "application/json; charset=utf-8", "unknown"),
               "img_v3_svg": (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml", "unsupported"),
               "img_v3_xsvg": (b"<?xml version='1.0'?>\n<svg/>", "application/octet-stream", "unsupported"),
               "img_v3_xhtml": (b"<?xml version='1.0'?><!DOCTYPE html><html/>", "application/octet-stream", "unknown"),
               "img_v3_comment": (b"<!-- login --><html/>", "application/octet-stream", "unknown"),
               "img_v3_spaces": (b" " * 256 + b"<html/>", "application/octet-stream", "unknown"),
               "img_v3_bin": (b"[\x00\xff\x10", "application/octet-stream", "unsupported"),
               "img_v3_xml": (b"\x00\x01", "application/problem+xml", "unknown")}
        for key, (body, ctype, kind) in odd.items():
            S.files[host + key + "?chat_id=500"] = (body, ctype)
            with self.assertRaises(BackendError, msg=key) as e:
                b.media(larkagentx.media_ref("image", "500", "101", key), 1 << 20)
            self.assertEqual(e.exception.kind, kind, key)
        self.assertEqual(b.media(ref, 1 << 20)[0], "image/png")


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

    def test_pushed_messages_say_where_they_show(self):
        from larkx._fake import frame, text_data

        real = larkagentx.pushed_threads
        larkagentx.pushed_threads = lambda p: json.loads(p.decode("utf-8"))  # the real decoding: test_smoke_larkx
        self.addCleanup(setattr, larkagentx, "pushed_threads", real)
        t = self.start()
        sync = {"thread_type": 2, "send_to_chat": False, "sync_type": 2, "sync_related": "299", "sync_root": "90"}
        thread = dict(root_id="90", parent_id="95", anchor="90", scope="topic")
        S.frames.put(frame(messages=[frame_msg("206", "500", 25, text_data("/agent 也发到群里"), sender=ME, **thread),
                                     frame_msg("207", "500", 26, text_data("只在话题里"), sender=ME, **thread),
                                     frame_msg("208", "500", 27, text_data("主界面"), parent_id="95")],
                           payload=json.dumps({"206": sync, "207": dict(sync, sync_type=0, sync_related="")}).encode()))
        S.frames.put(frame(messages=[frame_msg("209", "500", 28, text_data("读不出标记"), **thread)], payload=b"bad"))
        wait_until(lambda: len(self.got) == 4, what="the messages")
        self.assertEqual([(m.id, m.placement) for m in self.got],
                         [("206", "thread_chat"), ("207", "thread"), ("208", "chat"), ("209", "thread")])
        self.stop.set()
        t.join(5)
        self.assertEqual(self.error, [])

    def test_chats_moved_to_done_reach_the_core(self):
        from larkx._fake import frame, text_data

        heard = []
        self.b.on_chats_done = heard.append
        real = larkagentx.pushed_done
        larkagentx.pushed_done = lambda p: json.loads(p.decode("utf-8"))  # the real decoding: test_smoke_larkx
        self.addCleanup(setattr, larkagentx, "pushed_done", real)
        t = self.start()
        cmd = larkagentx.PUSH_FEED_CARDS_CMD
        S.frames.put(frame(sid="s3", cmd=cmd, payload=json.dumps({"500": 1763000000.5}).encode("utf-8")))
        S.frames.put(frame(sid="s4", cmd=cmd, payload=b"{}"))  # cards back in the inbox: nothing to tell
        S.frames.put(frame(sid="s5", cmd=cmd, payload=b"not json"))  # undecodable: logged, the link goes on
        S.frames.put(frame(messages=[frame_msg("205", "500", 24, text_data("之后的消息"))]))
        wait_until(lambda: len(self.got) == 1, what="the message after them")
        self.assertEqual(heard, [{"500": 1763000000.5}])
        self.assertIn(("ack", "s3"), S.ws_sent)
        self.assertIn(("ack", "s5"), S.ws_sent)
        self.stop.set()
        t.join(5)
        self.assertEqual(self.error, [])

    def test_reaction_pushes_go_to_their_reader(self):
        from larkx._fake import frame

        got = []
        self.b._pushed_reactions = lambda payload, me: got.append(payload)  # the real decoding: test_smoke_larkx
        t = self.start()
        S.frames.put(frame(sid="r1", cmd=larkagentx.PUSH_REACTIONS_CMD, payload=b"reactions"))
        wait_until(lambda: got == [b"reactions"], what="handed on")
        self.assertIn(("ack", "r1"), S.ws_sent)
        self.stop.set()
        t.join(5)

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
