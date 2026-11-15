import json

import requests

from ._fake import STATE
from .region import get_region


class AuthExpired(Exception):
    pass


class LarkAuth:
    def __init__(self, path=None):
        self.path = path
        self.region = get_region("feishu")
        self.cookies = {}
        self.saved_at = 0
        self.device_id = ""
        self.app_key = ""
        self.user_id = ""
        self.csrf_token = ""
        self.web_app_id = ""
        self.load()

    def load(self):
        if not self.path.exists():
            return False
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.cookies = data.get("cookies", {})
        return bool(self.cookies)

    def save(self):
        text = json.dumps({"cookies": self.cookies, "user_id": self.user_id, "app_key": self.app_key})
        STATE.saves.append(text)
        self.path.write_text(text, encoding="utf-8")

    def get_ticket(self):
        r = STATE.next_ticket()
        if r == "expired":
            raise AuthExpired("no ticket")
        if isinstance(r, BaseException):
            raise r
        return "ticket-secret"

    def require_valid(self):
        r = STATE.next_ticket()
        if r == "expired":
            raise AuthExpired("invalid")
        if isinstance(r, BaseException):
            raise AuthExpired("network")  # LarkAgentX folds a network error into its validity check
        return "ok"


class QrLogin:
    def __init__(self, region=None):
        STATE.qr_inits += 1
        if STATE.qr_init_error is not None:
            raise STATE.qr_init_error
        self.region = region
        self.token = "qr-token-%d" % STATE.qr_inits
        self.qr_content = json.dumps({"qrlogin": {"token": self.token}}, separators=(",", ":"))
        self.waits = []

    def wait(self, timeout=180, interval=2, on_status=None):
        self.waits.append((timeout, interval))
        ok, data, status = STATE.qr_script.pop(0) if STATE.qr_script else (False, None, 1)
        if on_status is not None:
            on_status(status, "qr_login_polling")
        return ok, data

    def finish(self, auth):
        if STATE.finish_error is not None:
            raise STATE.finish_error
        auth.region = self.region
        auth.cookies = {"session": "session-cookie-secret", "passport_web_did": "device-1", "swp_csrf_token": "csrf-1"}
        auth.saved_at = 1000.0
        auth.device_id = "device-1"
        auth.csrf_token = "csrf-1"
        auth.user_id = STATE.user_id
        auth.app_key = STATE.finish_app_key
        auth.save()
        return "ok"


_ = requests  # the real module makes its requests with it
