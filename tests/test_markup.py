"""The Markdown subset a formatted send is written in (markup), and where its @s may go (textutil's skip)."""

try:  # first: every temporary directory of this run in one, removed at its end
    import _runtmp  # noqa: F401
except ImportError:  # (started by its path from elsewhere: its directory is not on the path)
    pass
import unittest

import harness  # noqa: F401  (puts the core on the path)
from openmate_core import markup
from openmate_core.textutil import mention_spans


class FormattedTest(unittest.TestCase):
    def test_what_is_formatted(self):
        for text, words in (("**结论**：可以", ["粗体"]), ("看 [文档](https://example.com/d)", ["链接"]),
                            ("- 一\n- 二", ["列表"]), ("1. 一\n2) 二", ["列表"]), ("改 `a.py`", ["代码"]),
                            ("```\nx\n```", ["代码"]), ("## 进展", ["标题"]), ("> 原话", ["引用"]),
                            ("# **粗** 标题", ["标题"])):
            self.assertEqual(markup.features(text), words, text)
        for text in ("你好\n第二行", "版本 1.5 和 2*3*4", "a*b* c", "https://example.com/x 看下", "---", "#话题",
                     "[文字](ftp://x)", "** 不是粗体 **", "单个 ` 反引号", "", "1.不算列表", "-不算列表"):
            self.assertEqual(markup.features(text), [], text)
            self.assertFalse(markup.formatted(text), text)

    def test_blocks(self):
        text = "第一行 **粗**\n第二行\n\n- 一\n* 二\n3. 三\n4. 四\n> 引\n>\n> 用\n# 标题\n```py\ncode\n```\n尾"
        bs = markup.blocks(text)
        self.assertEqual([b.kind for b in bs], ["paragraph", "bullets", "numbers", "quote", "heading", "code", "paragraph"])
        self.assertEqual([w for w, _ in bs[0].lines], ["第一行 **粗**", "第二行"])
        self.assertEqual([w for w, _ in bs[1].lines], ["一", "二"])
        self.assertEqual((bs[2].number, [w for w, _ in bs[2].lines]), (3, ["三", "四"]))
        self.assertEqual([w for w, _ in bs[3].lines], ["引", "用"], "an empty quote line is no line")
        self.assertEqual((bs[5]["lang"], bs[5]["body"]), ("py", "code"))
        for b in bs:  # every line's words where they are in the text
            for words, at in b.lines:
                self.assertEqual(text[at:at + len(words)], words)
            self.assertTrue(0 <= b.start < b.end <= len(text))

    def test_an_unclosed_fence_runs_to_the_end_and_a_longer_one_closes_only_with_its_kind(self):
        bs = markup.blocks("````\na\n```\nb")
        self.assertEqual([(b.kind, b["body"]) for b in bs], [("code", "a\n```\nb")])
        self.assertEqual(markup.blocks("~~~\nx\n~~~\n后")[0]["body"], "x")

    def test_runs(self):
        runs = markup.inline("看 **粗 `c` [链](https://e.com/a_(b))** 和 `x`", 10)
        self.assertEqual([(r.kind, r.words, r.bold) for r in runs], [
            ("text", "看 ", False), ("text", "粗 ", True), ("code", "c", True), ("text", " ", True),
            ("link", "链", True), ("text", " 和 ", False), ("code", "x", False)])
        self.assertEqual(runs[4].url, "https://e.com/a_(b)")
        line = "看 **粗 `c` [链](https://e.com/a_(b))** 和 `x`"
        for r in runs:
            self.assertEqual(line[r.at - 10:r.at - 10 + len(r.words)], r.words)

    def test_markdown_of_a_block(self):
        md = [b.markdown() for b in markup.blocks("  * 一 `x`\n  * 二\n\n7. 七\n8. 八\n\n段 `c`\n---\n\n## 题 `t`\n\n> 引")]
        self.assertEqual(md, ["- 一 `x`\n- 二", "7. 七\n8. 八", "段 `c`  \n\\---", "**题 `t`**", "> 引"])
        self.assertEqual(markup.blocks("```\na ``` b\n```")[0].markdown(), "````\na ``` b\n````",
                         "a fence its body never closes")

    def test_no_mentions_in_code_or_links(self):
        text = "@甲 `@乙` [@丙](https://e.com) **@丁**\n```\n@戊\n```"
        places = markup.no_mentions(text)
        hidden = [n for n in "甲乙丙丁戊" if any(a <= text.index("@" + n) < b for a, b in places)]
        self.assertEqual(hidden, ["乙", "丙", "戊"])


class MentionSkipTest(unittest.TestCase):
    def test_an_at_in_code_is_no_at_and_goes_in_front_on_a_line_of_its_own(self):
        text = "看 `@张三` 和 @李四"
        out, spans = mention_spans(text, [("1", "张三"), ("2", "李四")], markup.no_mentions(text), "\n")
        self.assertEqual(out, "@张三\n看 `@张三` 和 @李四")
        self.assertEqual([(u, out[at:at + len(n) + 1]) for u, n, at in spans], [("1", "@张三"), ("2", "@李四")])
        self.assertEqual(spans[1][2], out.rindex("@李四"))
        self.assertEqual(mention_spans("看", [("1", "张三")])[0], "@张三 看", "as before without skip")
        self.assertEqual(mention_spans(" 看", [("1", "张三")])[0], "@张三 看")
        self.assertEqual(mention_spans("  - 一", [("1", "张三")], (), "\n")[0], "@张三\n  - 一", "the list stays one")
        self.assertEqual(mention_spans("\n- 一", [("1", "张三")], (), "\n")[0], "@张三\n- 一")


if __name__ == "__main__":
    unittest.main()
