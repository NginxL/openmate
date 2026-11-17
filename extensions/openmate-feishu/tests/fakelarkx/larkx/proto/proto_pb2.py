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

    def SerializeToString(self):
        return json.dumps({"content": self.content}, ensure_ascii=False).encode("utf-8")
