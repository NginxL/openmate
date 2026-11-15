import asyncio

from ._fake import STATE
from .auth import AuthExpired


class LarkClient:
    def __init__(self, auth):
        STATE.clients += 1
        if STATE.client_error is not None:
            raise STATE.client_error
        self.auth = auth
        self.auth.require_valid()
        if not self.auth.app_key or not self.auth.user_id:
            self.auth.app_key = self.auth.app_key or "appkey-fetched"
            self.auth.save()
        self.loop = asyncio.new_event_loop()
        self._loop_started = False
        self._thread_chats = {}

    def build_ws_url(self):
        self.auth.get_ticket()
        return "wss://msg.example/ws?ticket=ticket-secret"

    async def send_ack(self, ws, sid):
        STATE.ws_sent.append(("ack", sid))

    async def send_heartbeat(self, ws):
        STATE.ws_sent.append(("heartbeat",))

    async def heartbeat_loop(self, ws, interval=30):
        try:
            while True:
                await asyncio.sleep(interval)
                await self.send_heartbeat(ws)
        except Exception:
            pass

    def _gateway_post(self, packet):
        STATE.posts.append(packet)
        answer = STATE.post_answers.pop(0) if STATE.post_answers else None
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            return answer(packet)
        if answer is None:
            return ("id:%d" % (9000 + len(STATE.posts))).encode()
        return answer

    def search(self, query):
        return list(STATE.search_hits)

    def create_chat(self, user_id):
        return STATE.created.get(str(user_id))

    def get_user_name(self, user_id, chat_id):
        STATE.user_name_calls.append((user_id, chat_id))
        return STATE.user_names.get((str(user_id), str(chat_id)))

    def get_chat_info(self, chat_id):
        STATE.chat_info_calls.append(chat_id)
        info = STATE.chats.get(str(chat_id))
        if isinstance(info, BaseException):
            raise info
        return dict(info) if info else None

    def is_thread_chat(self, chat_id):
        return str(chat_id) in STATE.thread_chats

    def pull_history(self, chat_id, positions, save_storage=None):
        STATE.history_calls.append((chat_id, list(positions)))
        msgs = STATE.history.get(str(chat_id), {})
        return [dict(msgs[p]) for p in sorted(positions) if p in msgs]

    def api(self, type_or_cmd, data=None, raw=None):
        STATE.api_calls.append((type_or_cmd, data))
        answer = STATE.api_answers.get(type_or_cmd)
        if isinstance(answer, BaseException):
            raise answer
        if answer is None:
            raise ValueError("no such request in the fake")
        return answer


_ = AuthExpired
