import json


def build_send_message_packet(text, chat_id, request_id, root_id=None, thread_chat=False):
    """The fake packet says what the real one would carry: the root (and parent) and the reply-in-thread mark."""
    return {"text": text, "chat": chat_id, "request": request_id, "root": root_id or "",
            "reply_in_thread": bool(root_id) and not thread_chat}


def decode_put_message_response(content):
    text = content.decode("utf-8")
    if text.startswith("id:"):
        return text[3:]
    if text == "empty":
        return ""
    raise ValueError("unreadable")


_ = json
