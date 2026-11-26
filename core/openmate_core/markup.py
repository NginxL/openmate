"""The Markdown subset a message sent as the user may be written in (on a platform whose backend can_format): what it
is made of (blocks of lines, inline runs), whether a text uses it at all, and where an "@name" is no @ (in code, in a
link). Platform-neutral: a backend turns the blocks into its own rich text.

The subset (what the send tools tell the model):
- **bold**
- [words](https://address): words that link (http and https only); a bare address stays as written
- lines starting "- ", "* " or "+ " (a bulleted list), "1. " or "1) " (a numbered list, from its first number)
- `code` in a line, and fenced code blocks (``` or ~~~, a language after the fence may be named)
- "# " to "###### " (a heading: shown as a bold line), "> " (a quote)
- a line break is one, an empty line starts a new paragraph

Nothing else is read as markup: single *, _, ~~, tables, nested lists (an indented item is one more item), images.
"""

import re
from typing import Dict, List, NamedTuple, Optional, Tuple

FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*([\w+#.-]*)[ \t]*$")
HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(\S.*?)[ \t]*$")
QUOTE = re.compile(r"^ {0,3}>[ \t]?(.*?)[ \t]*$")
BULLET = re.compile(r"^[ \t]*[-*+][ \t]+(\S.*?)[ \t]*$")
NUMBERED = re.compile(r"^[ \t]*(\d{1,9})[.)][ \t]+(\S.*?)[ \t]*$")
SETEXT = re.compile(r"^[-=*_][-=*_ \t]*$")
RULE = re.compile(r"^ {0,3}([-*_])([ \t]*\1){2,}[ \t]*$")  # "---", "* * *": words as written, never a list
# `code`, **bold** (no space just inside the stars), [words](http(s) address: one pair of round brackets may be in it)
INLINE = re.compile(r"`([^`\n]+)`"
                    r"|\*\*(?=\S)(.+?)(?<=\S)\*\*"
                    r"|\[([^\[\]\n]+)\]\((https?://[^\s()]+(?:\([^\s()]*\)[^\s()]*)*)\)")

FEATURES = (("bold", "粗体"), ("link", "链接"), ("list", "列表"), ("code", "代码"), ("heading", "标题"), ("quote", "引用"))


class Run(NamedTuple):
    """An inline run. kind: "text", "code" or "link"; words: what it shows; at: where its words start in the text;
    url: a link's address; bold; start, end: the whole run in the text, its markup included."""

    kind: str
    words: str
    at: int
    url: str
    bold: bool
    start: int
    end: int


def inline(line: str, base: int = 0, bold: bool = False) -> List[Run]:
    """A line's runs in order; base: where the line starts in the text. Inside **bold**, code and links are read too."""
    out: List[Run] = []
    pos = 0
    for m in INLINE.finditer(line):
        if m.start() > pos:
            out.append(Run("text", line[pos:m.start()], base + pos, "", bold, base + pos, base + m.start()))
        if m.group(1) is not None:
            out.append(Run("code", m.group(1), base + m.start(1), "", bold, base + m.start(), base + m.end()))
        elif m.group(2) is not None:
            out.extend(inline(m.group(2), base + m.start(2), True))
        else:
            out.append(Run("link", m.group(3), base + m.start(3), m.group(4), bold, base + m.start(), base + m.end()))
        pos = m.end()
    if pos < len(line):
        out.append(Run("text", line[pos:], base + pos, "", bold, base + pos, base + len(line)))
    return out


class Block(dict):
    """A block of the text. kind: "paragraph", "heading", "quote", "bullets", "numbers" or "code". lines: [(words,
    where they start in the text)]: a paragraph's or a quote's lines, a heading's one, a list's items (numbers: from
    number); code: lang and body. start, end: the block's place in the text."""

    kind = property(lambda self: self["kind"])
    lines = property(lambda self: self.get("lines") or [])
    start = property(lambda self: self["start"])
    end = property(lambda self: self["end"])
    number = property(lambda self: int(self.get("number") or 1))  # a numbered list's first number

    def runs(self) -> List[List[Run]]:
        """Each line's runs (a heading's are bold)."""
        return [inline(words, at, self.kind == "heading") for words, at in self.lines]

    def has_code(self) -> bool:
        """Whether a line of it has `code`."""
        return any(r.kind == "code" for line in self.runs() for r in line)

    def markdown(self) -> str:
        """The block as Markdown on its own, written plainly (each line as one, a list's items unindented): what a
        backend that renders Markdown shows for it."""
        k = self.kind
        if k == "code":
            body = self["body"]
            ticks = max([len(t) for t in re.findall(r"`{3,}", body)] + [2]) + 1  # a fence the body never closes
            return "%s%s\n%s%s%s" % ("`" * ticks, self.get("lang") or "", body, "\n" if body else "", "`" * ticks)
        lines = [w for w, _ in self.lines]
        if k == "heading":
            return "**%s**" % lines[0] if "**" not in lines[0] else lines[0]
        if k == "quote":
            return "\n".join("> " + w for w in lines)
        if k == "bullets":
            return "\n".join("- " + w for w in lines)
        if k == "numbers":
            return "\n".join("%d. %s" % (self.number + i, w) for i, w in enumerate(lines))
        # a paragraph: each line break a hard one; a line of only - = * _ is words (never a heading's underline)
        return "  \n".join("\\" + w if SETEXT.match(w) else w for w in lines)


def _closes(line: str, fence: str) -> bool:
    s = line.strip()
    return len(line) - len(line.lstrip(" ")) <= 3 and len(s) >= len(fence) and s == fence[0] * len(s)


def _starts_block(line: str) -> bool:
    return bool(FENCE.match(line) or HEADING.match(line) or QUOTE.match(line)
                or (not RULE.match(line) and (BULLET.match(line) or NUMBERED.match(line))))


def blocks(text: str) -> List[Block]:
    """The text's blocks in order (empty lines between them only; none when the text is empty)."""
    lines = (text or "").split("\n")
    offs: List[int] = []
    at = 0
    for line in lines:
        offs.append(at)
        at += len(line) + 1
    n, i = len(lines), 0
    out: List[Block] = []

    def end_of(j: int) -> int:
        return offs[j] + len(lines[j])

    while i < n:
        line = lines[i]
        fence = FENCE.match(line)
        if fence:
            j = i + 1
            while j < n and not _closes(lines[j], fence.group(1)):
                j += 1
            body = "\n".join(lines[i + 1:j])
            last = min(j, n - 1)  # not closed: it runs to the end
            out.append(Block(kind="code", lang=fence.group(2).lower(), body=body, start=offs[i], end=end_of(last)))
            i = j + 1
            continue
        if not line.strip():
            i += 1
            continue
        m = HEADING.match(line)
        if m:
            out.append(Block(kind="heading", lines=[(m.group(1), offs[i] + m.start(1))], start=offs[i], end=end_of(i)))
            i += 1
            continue
        if QUOTE.match(line):
            got, j = [], i
            while j < n and QUOTE.match(lines[j]) and not FENCE.match(lines[j]):
                q = QUOTE.match(lines[j])
                if q.group(1):
                    got.append((q.group(1), offs[j] + q.start(1)))
                j += 1
            if got:
                out.append(Block(kind="quote", lines=got, start=offs[i], end=end_of(j - 1)))
            i = j
            continue
        if not RULE.match(line) and (BULLET.match(line) or NUMBERED.match(line)):
            pattern = NUMBERED if NUMBERED.match(line) else BULLET
            got, j = [], i
            while j < n and not RULE.match(lines[j]) and pattern.match(lines[j]):
                m = pattern.match(lines[j])
                g = 2 if pattern is NUMBERED else 1
                got.append((m.group(g), offs[j] + m.start(g)))
                j += 1
            b = Block(kind="numbers" if pattern is NUMBERED else "bullets", lines=got, start=offs[i], end=end_of(j - 1))
            if pattern is NUMBERED:
                b["number"] = int(NUMBERED.match(line).group(1))
            out.append(b)
            i = j
            continue
        got, j = [], i
        while j < n and lines[j].strip() and (j == i or not _starts_block(lines[j])):
            raw = lines[j]
            lead = len(raw) - len(raw.lstrip())
            got.append((raw.strip(), offs[j] + lead))
            j += 1
        out.append(Block(kind="paragraph", lines=got, start=offs[i], end=end_of(j - 1)))
        i = j
    return out


def features(text: str) -> List[str]:
    """What markup of the subset the text uses, in words (粗体, 链接, 列表, 代码, 标题, 引用): [] for a plain text, which
    goes as it always did."""
    seen: Dict[str, bool] = {}
    for b in blocks(text):
        if b.kind == "code":
            seen["code"] = True
            continue
        if b.kind in ("bullets", "numbers"):
            seen["list"] = True
        elif b.kind in ("heading", "quote"):
            seen[b.kind] = True
        for line in b.runs():
            for r in line:
                if r.kind == "code":
                    seen["code"] = True
                elif r.kind == "link":
                    seen["link"] = True
                if r.bold and b.kind != "heading":
                    seen["bold"] = True
    return [words for key, words in FEATURES if seen.get(key)]


def formatted(text: str) -> bool:
    """Whether the text uses the subset at all (else it is sent as plain text, as before)."""
    return bool(features(text))


def no_mentions(text: str) -> List[Tuple[int, int]]:
    """Where an "@name" is no @: code (fenced or `inline`) and links, as (start, end) places in the text."""
    out: List[Tuple[int, int]] = []
    for b in blocks(text):
        if b.kind == "code":
            out.append((b.start, b.end))
            continue
        for line in b.runs():
            out.extend((r.start, r.end) for r in line if r.kind in ("code", "link"))
    return out


def block_of(bs: List[Block], at: int) -> Optional[Block]:
    """The block a place in the text is in (None: between blocks)."""
    for b in bs:
        if b.start <= at < b.end or (at == b.end and b.start < b.end):
            return b
    return None
