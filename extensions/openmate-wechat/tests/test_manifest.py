"""The WeChat extension's manifest: its sidebar page keeps an icon of its own."""
import json
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(os.path.dirname(HERE), "persona-extension.json"), encoding="utf-8") as f:
    MANIFEST = json.load(f)


class ManifestTest(unittest.TestCase):
    def test_sidebar_page_icon(self):
        nav = {u["id"]: u["icon"] for u in MANIFEST["contributes"]["ui"] if u["placement"] == "nav_page"}
        # person: a NavIcon name (sdk/go/wire/wire_gen.go) that none of the Feishu extension's pages uses; the plain
        # list and extension icons look like every other extension's page in the monochrome sidebar
        self.assertEqual(nav, {"wechat-chats": "person"})
        self.assertNotIn(nav["wechat-chats"], {"list", "extension", "plugin"})


if __name__ == "__main__":
    unittest.main()
