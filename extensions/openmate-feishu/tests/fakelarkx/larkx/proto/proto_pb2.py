import json


class AtProperty:
    def __init__(self):
        self.userId = ""
        self.content = ""

    def ParseFromString(self, data):
        d = json.loads(data.decode("utf-8"))
        self.userId, self.content = d.get("userId", ""), d.get("content", "")


class TextProperty:
    def __init__(self):
        self.content = ""

    def ParseFromString(self, data):
        # the fake's text elements hold their words as they are (a real one is protobuf: field 1)
        try:
            d = json.loads(data.decode("utf-8"))
            self.content = d.get("content", "") if isinstance(d, dict) else data.decode("utf-8")
        except ValueError:
            self.content = data.decode("utf-8")

    def SerializeToString(self):
        return json.dumps({"content": self.content}, ensure_ascii=False).encode("utf-8")
