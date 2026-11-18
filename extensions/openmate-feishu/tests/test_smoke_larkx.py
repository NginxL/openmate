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
