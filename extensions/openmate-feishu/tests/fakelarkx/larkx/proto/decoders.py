import json

MSG_TYPE_NAMES = {0: "UNKNOWN", 2: "POST", 3: "FILE", 4: "TEXT", 5: "IMAGE", 6: "SYSTEM", 7: "AUDIO", 10: "STICKER",
                  11: "MERGE_FORWARD", 12: "CALENDAR", 14: "CARD", 15: "MEDIA", 16: "SHARE_CALENDAR_EVENT",
                  18: "GENERAL_CALENDAR", 9: "SHARE_GROUP_CHAT", 24: "TODO"}


def enum_to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def is_thread_chat(chat_info):
    return enum_to_int(chat_info.get("chatMode", 0)) in (2, 3) or enum_to_int(chat_info.get("type", 0)) == 3


def extract_rich_text(rich):
    dic = (rich.get("elements") or {}).get("dictionary") or {}
    return "".join(dic[k].get("_text", "") for k in rich.get("elementIds") or [] if k in dic)


def parse_ws_frame(raw):
    d = json.loads(raw.decode("utf-8"))
    packet = {"cmd": d.get("cmd")}
    if d.get("sid") is not None:
        packet["sid"] = d["sid"]
    return {}, packet


def decode_push_messages(raw):
    from .._fake import STATE

    return [dict(m) for m in STATE.pushed[json.loads(raw.decode("utf-8"))["pushed"]]]
