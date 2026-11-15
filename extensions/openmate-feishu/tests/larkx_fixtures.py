"""Shared bits of the LarkAgentX backend tests: the signed-in account and its saved credentials, pushed messages."""

import time

ME = "7001"
CREDS = {
    "v": 1, "region": "feishu", "cookies": {"session": "session-cookie-secret", "passport_web_did": "device-1"},
    "device_id": "device-1", "app_key": "appkey-1", "user_id": ME, "csrf_token": "csrf-1", "web_app_id": "",
    "saved_at": 1000.0, "user_name": "示例用户",
}


def frame_msg(msg_id, chat_id, position, content_data, sender="7002", msg_type=4, **kw):
    """A message as LarkAgentX's decode_push_messages / pull_history give it."""
    d = {"msg_id": msg_id, "msg_type": msg_type, "from_id": sender, "chat_id": chat_id, "chat_type": 2,
         "scope": "chat", "anchor": "", "at_me": False, "root_id": "", "parent_id": "", "position": position,
         "create_time": 1763000000 + position, "content": "", "content_data": content_data}
    d.update(kw)
    return d


def wait_until(cond, timeout=5.0, what="condition"):
    deadline = time.monotonic() + timeout
    while True:
        v = cond()
        if v:
            return v
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for " + what)
        time.sleep(0.01)
