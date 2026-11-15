from ._fake import STATE


def message_resource_url(msg_id, key, chat_id, host=None):
    return "%s/download/messages/%s/keys/%s?chat_id=%s" % (host, msg_id, key, chat_id)


def extract_resource_key(msg_type, content_data):
    if msg_type == 5:
        return (content_data.get("imageV2") or {}).get("imageKey", "")
    if msg_type in (3, 15, 7, 10):
        return content_data.get("key", "")
    return ""


def download(auth, url, out_path=None, max_bytes=200 * 1024 * 1024):
    STATE.downloads.append((url, max_bytes, dict(auth.cookies)))
    if STATE.download_error is not None:
        raise STATE.download_error
    data, ctype = STATE.files[url]
    if len(data) > max_bytes:
        raise ValueError("too large")
    return data, ctype
