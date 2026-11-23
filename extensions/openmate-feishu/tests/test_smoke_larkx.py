"""Against a real LarkAgentX, without the network: runs only when OPENMATE_LARKX_PYTHON names the Python it is
installed in (at the pinned commit), e.g.

    OPENMATE_LARKX_PYTHON=/path/to/larkx-venv/bin/python scripts/test.sh

It imports the real larkx under that interpreter, builds the backend, maps messages made with LarkAgentX's own
protobuf classes, and starts the real launcher, which must re-execute itself there and come up not signed in.
Nothing here signs in, sends or connects to Feishu.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
EXT_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(EXT_DIR))
LAUNCHER = os.path.join(EXT_DIR, "bin", "openmate-feishu")
PYTHON = os.environ.get("OPENMATE_LARKX_PYTHON", "")

INSIDE = r'''
import json, os, sys, tempfile
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from openmate_feishu import larkagentx, pin
out = {"commit": pin.verify(larkagentx.DEFAULT_COMMIT)}
data = tempfile.mkdtemp()
b = larkagentx.create({"larkx_python": sys.executable, "region": "feishu"}, data)
out["backend"] = type(b).__name__
out["regions"] = [larkagentx.LarkXBackend(b.lx, r).region.label for r in ("feishu", "lark")]
P, dec = b.lx.P, b.lx.decoders
tc = P.TextContent()
for i, (tag, prop) in enumerate([(1, P.TextProperty(content="请看 ")), (5, P.AtProperty(userId="7001", content="@我")),
                                 (5, P.AtProperty(userId="all", content="@所有人"))]):
    k = "e%d" % i
    tc.richText.elementIds.append(k)
    tc.richText.elements.dictionary[k].tag = tag
    tc.richText.elements.dictionary[k].property = prop.SerializeToString()
summary, content = dec.decode_message_content(4, tc.SerializeToString())
m = b._message({"msg_id": "71", "chat_id": "900", "msg_type": 4, "from_id": "7002", "content": summary,
                "content_data": content, "position": 12, "create_time": 1763000000, "root_id": "90", "anchor": "90",
                "scope": "topic", "at_me": True}, "7001", None)
out["text"] = [m.type, m.text, m.at_me, m.at_all, m.root_id, m.mine]
ic = P.ImageContent()
ic.imageV2.imageKey = "img_v3_example"
v = ic.imageV2.variants.add(); v.type = 1; v.size.width = 640; v.size.height = 480
s2, d2 = dec.decode_message_content(5, ic.SerializeToString())
img = b._message({"msg_id": "72", "chat_id": "900", "msg_type": 5, "from_id": "7001", "content": s2, "content_data": d2,
                  "position": 13, "create_time": 1763000001}, "7001", None)
out["image"] = [img.type, img.text, img.mine, img.media[0].kind, img.media[0].width, larkagentx.parse_ref(img.media[0].ref)]
pc = P.PostContent(); pc.title = "周报"; pc.text = "本周完成"
s3, d3 = dec.decode_message_content(2, pc.SerializeToString())
out["post"] = b._message({"msg_id": "73", "chat_id": "900", "msg_type": 2, "from_id": "7002", "content": s3,
                          "content_data": d3, "position": 14}, "7001", None).text
pkt = b.lx.builders.build_send_message_packet("你好", "900", "rid", root_id="90", thread_chat=False)
req = P.PutMessageRequest(); req.ParseFromString(pkt.payload)
out["send"] = [req.chatId, req.rootId, req.parentId, req.isReplyInThread]
pkt = b.lx.builders.build_send_message_packet("你好", "900", "rid", root_id="85", thread_chat=True)
req.ParseFromString(pkt.payload)
out["quote"] = [req.rootId, req.parentId, req.isReplyInThread]
from larkx.proto import gateway, lark_all_pb2 as LA
push = LA.feed.PushFeedCardsRequest(); push.type = 2
c = push.cards.add(); c.id = "900"; c.type = 1; c.updateTime = 1763000000; c.updateTimeMs = 1763000000500
c = push.cards.add(); c.id = "901"; c.type = 4  # a thread's card
out["pushed_done"] = larkagentx.pushed_done(push.SerializeToString())
push = LA.feed.PushFeedCardsRequest(); push.type = 1
c = push.cards.add(); c.id = "900"; c.type = 1
out["pushed_inbox"] = larkagentx.pushed_done(push.SerializeToString())
out["push_cmd"] = gateway.resolve_cmd("feed.PushFeedCardsRequest")[0] == larkagentx.PUSH_FEED_CARDS_CMD
cmd, req_cls, resp_cls = gateway.resolve_cmd("feed.PullFeedCardsByIdsRequest")
req = gateway.request_from_dict(req_cls, {"feedCardIdentities": [{"id": "900", "type": larkagentx.FEED_CHAT[0]}]})
out["ask"] = [cmd, [[i.id, i.type] for i in req.feedCardIdentities]]
resp = resp_cls()
c = resp.cards["900"]; c.id = "900"; c.type = 1; c.feedType = 2; c.updateTime = 1763000003
c = resp.cards["902"]; c.id = "902"; c.type = 1; c.feedType = 1
d = gateway.response_to_dict(resp_cls, resp.SerializeToString())
out["answer"] = larkagentx.done_cards(list(d["cards"].values()), None, {"900", "902"})
from openmate_core.backend import Account
me = Account("7001", "我")
b._groups_owner = "7001"
b._groups = [larkagentx.Chat(id="910", kind="group", name="旧名", members=3)]; b._groups_join = {"910": 1}
pc = LA.chats.PushChatsRequest()
c = pc.chats["910"]; c.id = "910"; c.type = 2; c.name = "改过的名字"
c = pc.chats["914"]; c.id = "914"; c.type = 2; c.name = "新建的群"
c = pc.chats["911"]; c.id = "911"; c.type = 1; c.name = "某人"
pc.inValidChatIds.append("912")
b._pushed_chats(pc.SerializeToString(), me)
out["pushed_groups"] = [[g.id, g.name, g.kind, g.members] for g in b._groups] + [b._groups_soon]
cmd, req_cls, resp_cls = gateway.resolve_cmd("chats.PullGroupChatsRequest")
req = gateway.request_from_dict(req_cls, {"type": larkagentx.GROUPS_JOINED, "count": larkagentx.GROUPS_PAGE, "updateTime": 0})
resp = resp_cls(); resp.chatIds.extend(["910", "913"]); resp.hasMore = True; resp.nextUpdateTime = 77; resp.chatJoinTimes["910"] = 5
d = gateway.response_to_dict(resp_cls, resp.SerializeToString())
out["groups_page"] = [cmd, req.type, req.count, d["chatIds"], d["hasMore"], d["nextUpdateTime"], d["chatJoinTimes"]]
cmd, req_cls, resp_cls = gateway.resolve_cmd("chats.PullChatsByIdsRequest")
req = gateway.request_from_dict(req_cls, {"chatIds": ["910"]})
resp = resp_cls(); g = resp.chats["910"]; g.id = "910"; g.type = 2; g.name = "新建的群"; g.joinTimeMs = 5000; g.isDissolved = False
d = gateway.response_to_dict(resp_cls, resp.SerializeToString())
out["group_info"] = [cmd, list(req.chatIds), larkagentx.group_alive(d["chats"]["910"]), d["chats"]["910"]["joinTimeMs"]]
heard = []
b.on_reacted = heard.append
pr = LA.reactions.PushReactions()
mr = pr.messageReactions.add(); mr.messageId = "71"
r = mr.reactions.add(); r.type = "DONE"; r.userIds.append("7001"); r.count = 1
mr = pr.messageReactions.add(); mr.messageId = "72"
r = mr.reactions.add(); r.type = "OK"; r.userIds.append("7002")
b._pushed_reactions(pr.SerializeToString(), me)
cmd, req_cls, resp_cls = gateway.resolve_cmd("reactions.PullReactionsRequest")
req = gateway.request_from_dict(req_cls, {"messageIds": ["71", "73"]})
resp = resp_cls(); st = resp.selfReactionStatus.add(); st.userId = "7001"; st.messageId = "73"; st.reactionType = "DONE"
st.nanoUpdateTime = 1763000000000000000
out["reactions"] = [heard, cmd, list(req.messageIds), larkagentx.reacted_by(gateway.response_to_dict(resp_cls, resp.SerializeToString()), "7001")]
pm = LA.messages.PushMessagesRequest()
x = pm.messages["81"]; x.id = "81"; x.rootId = "80"; x.threadId = "80"; x.threadMessageType = 2
x.syncToChatMessageType = 2; x.syncToChatRelatedMessageId = 82; x.syncToChatThreadRootId = 80
x = pm.messages["83"]; x.id = "83"; x.rootId = "80"; x.threadId = "80"; x.threadMessageType = 2; x.sendToChat = True
x = pm.messages["84"]; x.id = "84"; x.rootId = "80"; x.threadId = "80"; x.threadMessageType = 2
x = pm.messages["85"]; x.id = "85"
x = pm.messages["86"]; x.id = "86"; x.rootId = "80"; x.parentId = "80"; x.threadMessageType = 0
x = pm.messages["87"]; x.id = "87"; x.status = 3; x.editVersion = 2  # an edited message
x = pm.messages["88"]; x.id = "88"; x.editVersion = 1
x = pm.messages["89"]; x.id = "89"; x.createTimeMs = 1763000000000; x.editTimeMs = 1763000060000  # edited a minute on
x = pm.messages["90"]; x.id = "90"; x.createTimeMs = 1763000000000; x.editTimeMs = 1763000000000
x = pm.messages["91"]; x.id = "91"; x.status = 1  # as sent: surely not an edit
x = pm.messages["92"]; x.id = "92"; x.status = 2  # deleted: not known as unedited
marks = larkagentx.pushed_threads(pm.SerializeToString())
E = LA.entities.RichTextElement
cb = E.CodeBlockProperty(); cb.content = "SELECT 1"
v2 = E.CodeBlockV2Property(); ln = v2.contents.add(); ln.contents.add().content = "a = 1"
ln = v2.contents.add(); ln.contents.add().content = "b = 2"
tp = b.lx.P.TextProperty(); tp.content = " /stop"
ap = b.lx.P.AtProperty(); ap.userId = "7001"; ap.content = "@我"
rich = {"elementIds": ["a", "t", "c1", "c2"], "elements": {"dictionary": {
    "a": {"tag": 5, "property": ap.SerializeToString()}, "t": {"tag": 1, "property": tp.SerializeToString()},
    "c1": {"tag": 31, "property": cb.SerializeToString()}, "c2": {"tag": 37, "property": v2.SerializeToString()}}}}
out["code_cut"] = [larkagentx.rich_after_mentions(rich, b.lx.P), larkagentx.rich_text(rich, b.lx.P)]
ind = E.CodeBlockProperty(); ind.content = "    def a():\n        pass"
first = {"elementIds": ["a", "c"], "elements": {"dictionary": {
    "a": {"tag": 5, "property": ap.SerializeToString()}, "c": {"tag": 31, "property": ind.SerializeToString()}}}}
out["code_first"] = larkagentx.rich_after_mentions(first, b.lx.P)
out["pushed_threads"] = [[k, marks[k], larkagentx.synced_mark(marks[k])] for k in sorted(marks)]
pkt = b.lx.builders.build_send_message_packet("也发到群里", "900", "rid", root_id="80", thread_chat=False)
pkt = larkagentx.also_to_chat(pkt, "81", "sync")
lr = LA.messages.PutMessageRequest(); lr.ParseFromString(pkt.payload)
pr = P.PutMessageRequest(); pr.ParseFromString(pkt.payload)  # what LarkAgentX's own classes still read in it
out["also_to_chat"] = [lr.chatId, lr.rootId, lr.parentId, lr.isReplyInThread, lr.syncToChat, lr.sendToChat,
                       pr.content.richText.innerText, pkt.cmd]
pkt = larkagentx.also_to_chat(b.lx.builders.build_send_message_packet("旧标记", "900", "rid", root_id="80"), "83", "send")
lr = LA.messages.PutMessageRequest(); lr.ParseFromString(pkt.payload)
out["also_to_chat_send"] = [lr.parentId, lr.syncToChat, lr.sendToChat]
cmd, req_cls, resp_cls = gateway.resolve_cmd("messages.DeleteMessageRequest")  # recall (the 渠道's ended questions)
req = gateway.request_from_dict(req_cls, {"id": "101"})
out["recall"] = [cmd, [f.name for f in req_cls.DESCRIPTOR.fields], req.id]
files = [f for _, _, fs in os.walk(data) for f in fs]
out["files"] = files
from loguru import logger
out["home"] = os.environ.get("LARKX_HOME") == os.path.join(data, "larkx")
sys.stdout.write(json.dumps(out, ensure_ascii=False))
'''


@unittest.skipUnless(PYTHON, "set OPENMATE_LARKX_PYTHON to the Python LarkAgentX is installed in")
class RealLarkAgentXTest(unittest.TestCase):
    def test_backend_under_the_real_package(self):
        res = subprocess.run([PYTHON, "-I", "-B", "-c", INSIDE, os.path.join(ROOT, "core"), EXT_DIR],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, cwd=tempfile.gettempdir())
        self.assertEqual(res.returncode, 0, res.stderr.decode("utf-8", "replace")[-2000:])
        out = json.loads(res.stdout)
        self.assertEqual(out["commit"], "97ad48e69e34bd8c78ee30f795b7c3759dd81659")
        self.assertEqual(out["backend"], "LarkXBackend")
        self.assertEqual(out["regions"], ["飞书", "Lark"])
        self.assertEqual(out["text"], ["TEXT", "请看 @我@所有人", True, True, "90", False])
        self.assertEqual(out["image"], ["IMAGE", "", True, "image", 640, ["image", "900", "72", "img_v3_example"]])
        self.assertEqual(out["post"], "周报\n本周完成")
        self.assertEqual(out["send"], ["900", "90", "90", True], "a thread reply")
        self.assertEqual(out["quote"], ["85", "85", False], "a quote that stays in the main stream")
        self.assertEqual(out["pushed_done"], {"900": 1763000000.5}, "a chat moved to 完成, read from a real push")
        self.assertEqual(out["pushed_inbox"], {}, "back in the inbox: nothing")
        self.assertTrue(out["push_cmd"])
        self.assertEqual(out["ask"], [1009, [["900", 1]]])
        self.assertEqual(out["answer"], {"900": 1763000003.0})
        self.assertEqual(out["pushed_groups"], [["910", "改过的名字", "group", 3], True],
                         "a real push: renamed (members kept), a new group read again soon")
        self.assertEqual(out["groups_page"], [82, 2, 100, ["910", "913"], True, 77, {"910": 5}])
        self.assertEqual(out["group_info"], [64, ["910"], True, 5000])
        self.assertEqual(out["reactions"], [[["71"]], 24, ["71", "73"], {"73": 1763000000.0}],
                         "a real push names the message with the account's reaction; a real answer's own status")
        none = {"thread_type": 2, "thread_known": True, "send_to_chat": False, "sync_type": 0, "sync_related": "",
                "sync_root": "", "status": None, "edited": None}
        self.assertEqual(out["pushed_threads"], [
            ["81", dict(none, sync_type=2, sync_related="82", sync_root="80"), "sync"],
            ["83", dict(none, send_to_chat=True), "send"],
            ["84", none, ""],
            ["85", dict(none, thread_type=0, thread_known=False), ""],
            ["86", dict(none, thread_type=0), ""],
            ["87", dict(none, thread_type=0, thread_known=False, status=3, edited=True), ""],
            ["88", dict(none, thread_type=0, thread_known=False, edited=True), ""],
            ["89", dict(none, thread_type=0, thread_known=False, edited=True), ""],
            ["90", dict(none, thread_type=0, thread_known=False), ""],
            ["91", dict(none, thread_type=0, thread_known=False, status=1, edited=False), ""],
            ["92", dict(none, thread_type=0, thread_known=False, status=2), ""]],
            "where pushed messages show, read from a real push; an unset thread kind is told from a quote's 0; edits")
        self.assertEqual(out["code_cut"], ["/stop\nSELECT 1\n\na = 1\nb = 2", "@我 /stop"],
                         "an @自己 request keeps code blocks (no bare /stop then); the message's text as before")
        self.assertEqual(out["code_first"], "    def a():\n        pass", "a code block first keeps its indents")
        self.assertEqual(out["also_to_chat"], ["900", "80", "81", True, True, False, "也发到群里", 5],
                         "into the thread, also to the chat, quoting the request")
        self.assertEqual(out["also_to_chat_send"], ["83", False, True])
        self.assertEqual(out["recall"], [9, ["id"], "101"], "the delete-message request: the message's id alone")
        self.assertEqual(out["files"], [], "nothing written to the data directory")
        self.assertTrue(out["home"])
        self.assertEqual(res.stderr, b"", "LarkAgentX's logging is off")

    def test_the_launcher_becomes_the_larkx_python(self):
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        from harness import StdioHub

        for commit, state in ((None, "not_signed_in"), ("1" * 40, "not_configured")):
            data = tempfile.mkdtemp(prefix="openmate-smoke-")
            self.addCleanup(shutil.rmtree, data, True)
            env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENMATE_", "PERSONA_", "PYTHON"))}
            env["PERSONA_EXTENSION_DATA"] = data
            p = subprocess.Popen([sys.executable, LAUNCHER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env=env)
            try:
                hub = StdioHub(p.stdout, p.stdin)
                config = {"larkx_python": PYTHON}
                if commit:
                    config["larkx_commit"] = commit
                hub.request("initialize", {"protocol": 1, "hub": {}, "extension": {"id": "openmate-feishu", "data_dir": data},
                                           "config": config}, timeout=60)
                deadline = time.monotonic() + 60
                while True:
                    st = hub.request("connector.status", {"contribution": "matefeishu"}, timeout=60)
                    if st["state"] != "error" or time.monotonic() > deadline:
                        break
                    time.sleep(0.1)
                self.assertEqual(st["state"], state, st)
                if commit:
                    self.assertIn("版本不对", st["error"])
                cmd = subprocess.run(["ps", "-o", "command=", "-p", str(p.pid)], stdout=subprocess.PIPE,
                                     timeout=10).stdout.decode("utf-8", "replace")
                self.assertIn(PYTHON + " -I -B " + LAUNCHER, cmd)
            finally:
                if p.poll() is None:
                    p.kill()
                    p.wait(5)
                for f in (p.stdin, p.stdout, p.stderr):
                    f.close()


if __name__ == "__main__":
    unittest.main()
