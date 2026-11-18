#!/usr/bin/env python3
"""A stand-in for matewechat-native in tests: the same JSON lines, scripted by environment variables.

FAKE_LOG     every request is appended here as a JSON line (and every persist ack)
FAKE_EVENTS  a JSON list of events sent after "listen" ({"sleep": s} waits; {"event":"persist",…} waits for its ack)
"""
import json
import os
import sys
import threading
import time

LOG = os.environ.get("FAKE_LOG", "")
EVENTS = json.loads(os.environ.get("FAKE_EVENTS", "[]"))
out_mu = threading.Lock()
acks = {}
ack_cv = threading.Condition()
polls = [0]
started = [False]


def out(v):
    with out_mu:
        sys.stdout.write(json.dumps(v, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def record(v):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(v, ensure_ascii=False) + "\n")


gen = [0]


def play(g):
    for ev in EVENTS:
        if "sleep" in ev:
            time.sleep(ev["sleep"])
            continue
        if ev.get("event") != "persist":
            ev = dict(ev, gen=ev.get("gen", g))
        out(ev)
        if ev.get("event") == "persist":
            with ack_cv:
                ack_cv.wait_for(lambda: ev["seq"] in acks, timeout=5)
        if ev.get("event") == "exit":
            os._exit(3)


def answer(req):
    op = req.get("op")
    rid = req.get("id")
    ok = lambda **kw: out(dict(kw, id=rid, ok=True))  # noqa: E731
    fail = lambda kind, msg: out({"id": rid, "ok": False, "error": {"kind": kind, "message": msg}})  # noqa: E731
    if op == "login_start":
        started[0] = True
        return ok(qr="https://login.weixin.qq.com/l/fake", expires_at=0, device={"device": 1})
    if op == "login_poll":
        if not started[0]:
            return fail("input", "没有进行中的扫码登录")
        polls[0] += 1
        return ok(status=["pending", "scanned", "confirmed"][min(polls[0] - 1, 2)])
    if op == "login_finish":
        return ok(state={"k": "v1"}, account={"id": "wxid_me", "name": "我"})
    if op == "restore":
        if req.get("state") == {"k": "bad"}:
            return fail("expired", "expired")
        return ok(account={"id": req.get("state", {}).get("who", "wxid_me"), "name": "我"})
    if op == "listen":
        gen[0] = req.get("gen") or gen[0] + 1
        ok(gen=gen[0])
        threading.Thread(target=play, args=(gen[0],), daemon=True).start()
        return None
    if op == "send":
        t = req.get("text", "")
        if "DIE" in t:
            os._exit(4)
        if "REJECT" in t:
            return ok(status="rejected", message="refused")
        if "NOTSENT" in t:
            return ok(status="not_sent", message="not sent")
        if "UNCERTAIN" in t:
            return ok(status="uncertain", message="no receipt")
        return ok(status="sent", message_id="srv_%d" % rid, quoted=bool(req.get("quote")), created_at=1700000000)
    if op == "contacts":
        if any(not i for i in req.get("ids", [])):
            return fail("input", "Empty contact identity")
        biz = set(os.environ.get("FAKE_BUSINESS", "").split(","))
        silent = set(os.environ.get("FAKE_NO_KIND", "").split(","))  # WeChat gave no flags for these
        unknown = set(os.environ.get("FAKE_UNKNOWN", "").split(","))  # WeChat shows nothing for these
        return ok(contacts=[{"id": i, "nickname": ("群" + i.split("@")[0]) if i.endswith("@chatroom") else "昵称" + i[-1],
                             "remark": ("备注" + i[-1]) if i == "wxid_bob" else None,
                             "verify_flag": None if i in silent else (8 if i in biz else 0), "business": i in biz}
                            for i in req.get("ids", []) if i not in unknown])
    if op == "room":
        if not isinstance(req.get("room_id"), str):
            return fail("input", "缺少 room_id")
        left = set(os.environ.get("FAKE_LEFT", "").split(","))  # groups the account is no longer in
        unknown = os.environ.get("FAKE_ROOM_PARTIAL", "") == "1"  # WeChat gave no full id list
        return ok(room_id=req.get("room_id"), member_count=3, me_in=None if unknown else req.get("room_id") not in left,
                  members=[{"id": "wxid_bob", "nickname": "Bob", "display": "群里的鲍勃"},
                                             {"id": "wxid_me", "nickname": "我", "display": None}])
    if op == "groups":
        return ok(groups=json.loads(os.environ.get("FAKE_GROUPS", '["g1@chatroom", "g2@chatroom"]')),
                  complete=os.environ.get("FAKE_GROUPS_COMPLETE", "1") == "1")
    if op == "hello":
        return ok(protocol=int(os.environ.get("FAKE_PROTOCOL", "2")), version="fake")
    if op in ("stop", "logout"):
        return ok()
    return fail("unsupported", op)


for line in sys.stdin:
    try:
        req = json.loads(line)
    except ValueError:
        continue
    record(req)
    if req.get("op") == "persist_ack":
        with ack_cv:
            acks[req.get("seq")] = req.get("ok")
            ack_cv.notify_all()
        continue
    threading.Thread(target=answer, args=(req,), daemon=True).start()
