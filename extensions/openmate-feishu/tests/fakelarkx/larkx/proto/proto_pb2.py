import json


class AtProperty:
    def __init__(self):
        self.userId = ""
        self.content = ""

    def ParseFromString(self, data):
        d = json.loads(data.decode("utf-8"))
        self.userId, self.content = d.get("userId", ""), d.get("content", "")
