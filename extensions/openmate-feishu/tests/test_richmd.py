"""richmd: a Feishu rich text as safe Markdown for the thread page (bold, lists, code, links, headings, quotes), "" for
a text with no formatting; every word escaped, only http(s) links."""

try:  # first: every temporary directory of this run in one, removed at its end
    import _runtmp  # noqa: F401
except ImportError:  # (started by its path from elsewhere: its directory is not on the path)
    pass
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "fakelarkx"))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "..", "core"))

from larkx.proto import proto_pb2 as P  # noqa: E402  (the fake: a text property is its words)

from openmate_feishu import richmd  # noqa: E402


def pb(*fields):
    out = b""
    for n, s in fields:
        b = s.encode("utf-8")
        out += bytes([(n << 3) | 2, len(b)]) + b
    return out


def rich(ids, **els):
    return {"elementIds": ids, "elements": {"dictionary": els}}


def text(words, **kw):
    return dict({"tag": 1, "property": words.encode("utf-8")}, **kw)


class MarkdownTest(unittest.TestCase):
    def test_plain_text_has_none(self):
        r = rich(["p"], p={"tag": 3, "property": b"", "childIds": ["t"]}, t=text("就是普通的一句话。*不算*"))
        self.assertEqual(richmd.rich_markdown(r, P), "")
        self.assertEqual(richmd.rich_markdown({}, P), "")
        self.assertEqual(richmd.rich_markdown({"elements": {"dictionary": "x"}}, P), "")

    def test_bold_by_style_and_by_tag_lists_quote_heading(self):
        r = rich(["d"],
                 d={"tag": 22, "property": b"", "childIds": ["h", "p", "ul", "ol", "q"]},
                 h={"tag": 23, "property": b"", "childIds": ["ht"]}, ht=text("周报"),
                 p={"tag": 3, "property": b"", "childIds": ["a", "b", "c"]},
                 a=text("这是 "), b=text("重点", style={"fontWeight": "bold"}), c={"tag": 7, "property": b"", "childIds": ["c1"]},
                 c1=text("也重要"),
                 ul={"tag": 26, "property": b"", "childIds": ["l1", "l2"]},
                 l1={"tag": 28, "property": b"", "childIds": ["l1t"]}, l1t=text("第一项"),
                 l2={"tag": 28, "property": b"", "childIds": ["l2t"]}, l2t=text("第二项"),
                 ol={"tag": 27, "property": b"\x08\x00\x10\x03", "childIds": ["o1", "o2"]},
                 o1={"tag": 28, "property": b"", "childIds": ["o1t"]}, o1t=text("三"),
                 o2={"tag": 28, "property": b"", "childIds": ["o2t"]}, o2t=text("四"),
                 q={"tag": 29, "property": b"", "childIds": ["qp"]},
                 qp={"tag": 3, "property": b"", "childIds": ["qt"]}, qt=text("引一句"))
        md = richmd.rich_markdown(r, P)
        self.assertIn("# 周报", md)
        self.assertIn("这是 **重点也重要**", md)  # two bold runs that meet: one, no stars shown
        self.assertIn("- 第一项\n- 第二项", md)
        self.assertIn("3. 三\n4. 四", md)
        self.assertIn("> 引一句", md)

    def test_words_are_escaped_nothing_becomes_markup_or_html(self):
        r = rich(["p"], p={"tag": 3, "property": b"", "childIds": ["b"]},
                 b=text("<img src=x onerror=alert(1)> [x](javascript:alert(1)) *a* `c` # h", style={"fontWeight": "bold"}))
        md = richmd.rich_markdown(r, P)
        self.assertTrue(md.startswith("**") and md.endswith("**"))
        import re

        self.assertIsNone(re.search(r"(?<!\\)[<>\[`$]", md), "every mark escaped")
        self.assertNotIn("\\(", md)  # "\\(" opens a formula where the renderer has math: "(" needs no escape
        self.assertNotIn("\\]", md)
        self.assertIn("\\<img", md)
        self.assertIn("\\[x](javascript\\:alert(1))", md)  # "[" escaped: no link can start
        self.assertIn("\\*a\\*", md)
        self.assertIn("\\`c\\`", md)

    def test_links_only_http(self):
        good = rich(["a"], a={"tag": 6, "property": pb((1, "https://example.com/a_(b)"), (2, "文档"))})
        self.assertEqual(richmd.rich_markdown(good, P), "[文档](https://example.com/a_%28b%29)")
        bare = rich(["a"], a={"tag": 6, "property": pb((1, "https://example.com/x"))})
        self.assertEqual(richmd.rich_markdown(bare, P), "<https://example.com/x>")
        bad = rich(["a"], a={"tag": 6, "property": pb((1, "javascript:alert(1)"), (2, "点我"))})
        self.assertEqual(richmd.rich_markdown(bad, P), "点我")
        link = rich(["l"], l={"tag": 17, "property": pb((1, "http://example.com/y"))})
        self.assertEqual(richmd.rich_markdown(link, P), "<http://example.com/y>")

    def test_code(self):
        r = rich(["p", "m"], p={"tag": 3, "property": b"", "childIds": ["t", "c"]}, t=text("运行 "),
                 c={"tag": 30, "property": "a`b".encode("utf-8")},
                 m={"tag": 49, "property": pb((1, "```python\nprint('<x>')\n```"))})
        md = richmd.rich_markdown(r, P)
        self.assertIn("运行 ``a`b``", md)
        self.assertIn("```python\nprint('<x>')\n```", md)
        loose = rich(["m"], m={"tag": 49, "property": pb((1, "**粗** <b>x</b>"))})
        self.assertEqual(richmd.rich_markdown(loose, P), "\\*\\*粗\\*\\* \\<b\\>x\\<\\/b\\>")

    def test_title_and_line_breaks(self):
        r = rich(["p"], p={"tag": 3, "property": b"", "childIds": ["t", "b"]}, t=text("一行\n二行 "),
                 b=text("粗", style={"fontWeight": "bold"}))
        md = richmd.rich_markdown(r, P, title="标题 #1")
        self.assertTrue(md.startswith("**标题 \\#1**\n\n"))
        self.assertIn("一行  \n二行 **粗**", md)

    def test_nothing_read_twice_or_lost(self):
        r = rich(["p", "ul"], p={"tag": 3, "property": b"", "childIds": ["t", "c"]}, t=text("run "),
                 c={"tag": 30, "property": b"", "childIds": ["ct"]}, ct=text("build"),
                 ul={"tag": 26, "property": b"", "childIds": ["li"]},
                 li={"tag": 28, "property": b"", "childIds": ["lp", "cb"]},
                 lp={"tag": 3, "property": b"", "childIds": ["lt"]}, lt=text("details"),
                 cb={"tag": 49, "property": pb((1, "```\n# Notes\nx = 1\n```"))})
        md = richmd.rich_markdown(r, P)
        self.assertIn("run `build`", md)
        self.assertIn("- details\n  ```\n  # Notes\n  x = 1\n  ```", md)  # the code inside the item, every line

    def test_code_alone(self):
        r = rich(["m"], m={"tag": 49, "property": pb((1, "```\nls -la\n```"))})
        self.assertEqual(richmd.rich_markdown(r, P), "```\nls -la\n```")

    def test_odd_trees_never_raise(self):
        loop = rich(["a"], a={"tag": 3, "property": b"", "childIds": ["b"]}, b={"tag": 7, "property": b"", "childIds": ["a"]})
        self.assertIsInstance(richmd.rich_markdown(loop, P), str)
        weird = rich(["a"], a={"tag": "B", "property": None, "childIds": None})
        self.assertEqual(richmd.rich_markdown(weird, P), "")


if __name__ == "__main__":
    unittest.main()
