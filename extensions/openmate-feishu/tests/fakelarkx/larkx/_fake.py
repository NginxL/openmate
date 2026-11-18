"""The fake service's state: the tests set the knobs and read what happened."""

import json
import queue
import threading


class State:
    def __init__(self):
        self.reset()

    def reset(self):
        self.lock = threading.RLock()
        self.user_id = "7001"
        self.qr_inits = 0
        self.qr_init_error = None  # an exception QrLogin() raises
        self.qr_script = []  # (ok, data, status) per wait() call; default: (False, None, 1)
        self.finish_error = None
        self.finish_app_key = "appkey-1"
        self.tickets = []  # "ok" | "expired" | an exception, per get_ticket call; default "ok"
        self.ticket_calls = 0
        self.client_error = None  # an exception LarkClient() raises
        self.clients = 0
        self.saves = []  # what LarkAuth.save wrote (to its path object)
        self.posts = []  # packets given to _gateway_post
        self.post_answers = []  # per post: bytes (a response), an exception, or a callable(packet) -> bytes
        self.chats = {}  # chat id -> chat info dict (PullChatsByIdsRequest's)
        self.chat_info_calls = []
        self.thread_chats = set()
        self.history = {}  # chat id -> {position: message dict}
        self.history_calls = []
        self.api_answers = {}  # request type -> dict or exception
        self.api_calls = []
        self.search_hits = []
        self.user_names = {}  # (user id, chat id) -> name
        self.user_name_calls = []
        self.created = {}  # user id -> chat id
        self.files = {}  # url -> (bytes, content type)
        self.download_error = None
        self.downloads = []
        self.ws_connects = []  # what each websockets.connect call does: an exception, or "open"
        self.ws_kwargs = []
        self.ws = None  # the open fake connection
        self.frames = queue.Queue()  # what the open connection yields: bytes, "close", "drop"
        self.ws_sent = []
        self.pushed = []  # the messages of each frame()

    def next_ticket(self):
        with self.lock:
            self.ticket_calls += 1
            return self.tickets.pop(0) if self.tickets else "ok"


STATE = State()


def frame(sid=None, cmd=6, messages=(), payload=None):
    """A websocket frame: what parse_ws_frame and decode_push_messages of the fake read back (the messages, which
    hold bytes, stay in STATE.pushed under the frame's number; payload, bytes, is the packet's own)."""
    with STATE.lock:
        STATE.pushed.append([dict(m) for m in messages])
        n = len(STATE.pushed) - 1
    d = {"sid": sid, "cmd": cmd, "pushed": n}
    if payload is not None:
        d["payload"] = payload.decode("latin-1")
    return json.dumps(d).encode("utf-8")


def at(user_id, content=""):
    return json.dumps({"userId": user_id, "content": content}).encode("utf-8")


def text_data(*parts):
    """content_data of a TEXT message: parts are strings or ("at", user id, shown)."""
    dic, ids = {}, []
    for i, p in enumerate(parts):
        k = "e%d" % i
        ids.append(k)
        if isinstance(p, tuple):
            dic[k] = {"tag": 5, "property": at(p[1], p[2]), "_text": p[2]}
        else:
            dic[k] = {"tag": 1, "property": p.encode("utf-8"), "_text": p}
    return {"richText": {"elementIds": ids, "elements": {"dictionary": dic}}}
