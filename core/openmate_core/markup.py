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

Nothing else is read as markup: single *, _, ~~, tables, nested lists (an indented item is one more item), images
(![…](…) stays words), anything inside code or a bare address.
"""

import itertools
import re
from typing import Dict, List, NamedTuple, Optional, Tuple

FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*([\w+#.-]*)[ \t]*$")
HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(\S.*?)[ \t]*$")
QUOTE = re.compile(r"^ {0,3}>(?:[ \t]+(.*?))?[ \t]*$")  # "> words" or a ">" alone; ">= 80%" is words
BULLET = re.compile(r"^[ \t]*[-*+][ \t]+(\S.*?)[ \t]*$")
NUMBERED = re.compile(r"^[ \t]*(\d{1,9})[.)][ \t]+(\S.*?)[ \t]*$")
SETEXT = re.compile(r"^[-=*_][-=*_ \t]*$")
RULE = re.compile(r"^ {0,3}([-*_])([ \t]*\1){2,}[ \t]*$")  # "---", "* * *": words as written, never a list
CODE_SPAN = re.compile(r"`[^`\n]+`")
_TARGET = r"(https?://[^\s()]+(?:\([^\s()]*\)[^\s()]*)*)"  # one pair of round brackets may be in an address
LINK = re.compile(r"(?<!!)\[([^\[\]\n]+)\]\(" + _TARGET + r"\)")
# an image, its title too: words as written, never a link
IMAGE = re.compile(r"!\[[^\[\]\n]*\]\((?:[^()\n]|\([^()\n]*\))*\)")
# a bare address: up to a space, a quote, <, >, ` or CJK or fullwidth punctuation (Chinese words and fullwidth
# letters in its path are in it), without the sentence marks and stars at its end ("**see https://x.org**." is
# bold: _trim_url): as written (the service links it), no markup or @ in it
URL = re.compile(r"(?i:https?)://[^\s<>\"'`\x01\x03\x04\u3000-\u303f\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff65]+")
URL_TAIL = ".,:;!?*"
MASK = "\x01"  # stands for code's words, an address: never markup, wherever the stars are
IMAGE_MASK = "\x03"  # stands for an image, its text and address: words, never a link
LINK_MASK = "\x04"  # stands for a link's address (LINK's, http or https in lower case): its link's
MASKS = (MASK, "\x02", IMAGE_MASK, LINK_MASK)
SCHEME = re.compile(r"(?i)https?://")
# on a line masked (_masked): `code`, **bold** (no space just inside the stars), [words](its address, masked)
INLINE = re.compile(r"`([^`\n]+)`|\*\*(?=\S)(.+?)(?<=\S)\*\*|(?<!!)\[([^\[\]\n]+)\]\((\x04+)\)")
MD_SPECIAL = re.compile(r"([\\`*_~\[\]<>|&])")
NBSP = "\u00a0"

FEATURES = (("bold", "粗体"), ("link", "链接"), ("list", "列表"), ("code", "代码"), ("heading", "标题"), ("quote", "引用"))


class Run(NamedTuple):
    """An inline run. kind: "text", "url" (a bare address: words as written), "code" or "link"; words: what it shows;
    at: where its words start in the text; url: a link's address; bold; start, end: the whole run in the text, its
    markup included."""

    kind: str
    words: str
    at: int
    url: str
    bold: bool
    start: int
    end: int


def _masked(line: str) -> str:
    """The line with code's words, images and bare addresses as MASK: what no markup is read in (a "**" in code, in
    an address, closes no bold; an image is no link)."""
    m = ["\x00" if c in MASKS else c for c in line]  # (a mark written in the text is none of these)

    def blank(a: int, b: int, mark: str = MASK) -> None:
        m[a:b] = mark * (b - a)

    for c in CODE_SPAN.finditer(line):
        blank(c.start() + 1, c.end() - 1)
    once = "".join(m)
    for x in IMAGE.finditer(once):
        blank(x.start(), x.end(), IMAGE_MASK)  # (its own mark: never one run with an address beside it)
    once = "".join(m)
    # a bare address that starts outside any "[words](address)" and runs through one keeps it: the link is in its words
    candidates = [(x.start(), x.end()) for x in LINK.finditer(once)]
    bare: List[Tuple[int, int]] = []
    for st in SCHEME.finditer(once):  # each address from its own start (one inside a link takes none after it)
        p = st.start()
        if any(a <= p < b for a, b in candidates) or (bare and p < bare[-1][1]):
            continue
        u = URL.match(once, p)
        if u:
            bare.append((p, p + len(_trim_url(u.group()))))
    hidden = list(once)  # the links whole as MASK: no bare address starts in one, nor runs on from one
    for x in LINK.finditer(once):
        if any(a < x.start() < b for a, b in bare):
            continue
        hidden[x.start():x.end()] = MASK * (x.end() - x.start())
        blank(x.start(2), x.end(2), LINK_MASK)  # its address is one piece: a "**" in it closes no bold
    for u in URL.finditer("".join(hidden)):
        blank(u.start(), u.start() + len(_trim_url(u.group())))
    return "".join(m)


def _trim_url(url: str) -> str:
    """A bare address without the sentence marks and stars at its end (nor a ")" it does not open)."""
    while url and (url[-1] in URL_TAIL or (url[-1] == ")" and url.count(")") > url.count("("))):
        url = url[:-1]
    return url


def _words(line: str, masked: str, a: int, b: int, base: int, bold: bool) -> List[Run]:
    """Words between markup: "text" runs, a bare address its own "url" run."""
    out: List[Run] = []
    pos = a
    for u in re.finditer(re.escape(MASK) + "+", masked[a:b]):
        ua, ub = a + u.start(), a + u.end()
        if not (URL.fullmatch(line[ua:ub]) and _trim_url(line[ua:ub]) == line[ua:ub]):
            continue  # (a link's address: in its run)
        if ua > pos:
            out.append(Run("text", line[pos:ua], base + pos, "", bold, base + pos, base + ua))
        out.append(Run("url", line[ua:ub], base + ua, line[ua:ub], bold, base + ua, base + ub))
        pos = ub
    if pos < b:
        out.append(Run("text", line[pos:b], base + pos, "", bold, base + pos, base + b))
    return out


def inline(line: str, base: int = 0, bold: bool = False, masked: Optional[str] = None) -> List[Run]:
    """A line's runs in order; base: where the line starts in the text. Inside **bold**, code and links are read too;
    code, bare addresses and images are read before any of it (CommonMark's way: code binds first)."""
    if masked is None:
        masked = _masked(line)
    out: List[Run] = []
    pos = 0
    for m in INLINE.finditer(masked):
        if m.start() > pos:
            out += _words(line, masked, pos, m.start(), base, bold)
        if m.group(1) is not None:
            out.append(Run("code", line[m.start(1):m.end(1)], base + m.start(1), "", bold, base + m.start(),
                           base + m.end()))
        elif m.group(2) is not None:
            a, b = m.start(2), m.end(2)
            out += inline(line[a:b], base + a, True, masked[a:b])
        else:
            out.append(Run("link", line[m.start(3):m.end(3)], base + m.start(3), line[m.start(4):m.end(4)], bold,
                           base + m.start(), base + m.end()))
        pos = m.end()
    if pos < len(line):
        out += _words(line, masked, pos, len(line), base, bold)
    return out


def _md_escape(words: str) -> str:
    return MD_SPECIAL.sub(r"\\\1", words)


def _md_line(runs: List[Run]) -> str:
    """A line's runs as Markdown: the words escaped (a lone * or _ stays a star), bold runs together in one **…**."""
    out: List[str] = []
    for bold, group in itertools.groupby(runs, key=lambda r: r.bold):
        parts = []
        for r in group:
            if r.kind == "code":  # (fenced by more backticks than any run in its words)
                ticks = "`" * (max([len(t) for t in re.findall(r"`+", r.words)] + [0]) + 1)
                pad = " " if r.words.startswith("`") or r.words.endswith("`") else ""
                parts.append("%s%s%s%s%s" % (ticks, pad, r.words, pad, ticks))
            elif r.kind == "link":
                parts.append("[%s](%s)" % (_md_escape(r.words), r.url.replace("&", "\\&")))  # (no entity: "&copy;")
            elif r.kind == "url":
                parts.append(r.words)
            else:
                parts.append(_md_escape(r.words))
        piece = "".join(parts)
        core = piece.strip()
        if bold and core:
            lead, trail = piece[:len(piece) - len(piece.lstrip())], piece[len(piece.rstrip()):]
            piece = "%s**%s**%s" % (lead, core, trail)
        out.append(piece)
    line = "".join(out)
    indent = len(line) - len(line.lstrip(" \t"))
    line = NBSP * indent + line[indent:]  # an indent kept, never an indented code block
    # never a list or a heading of its own: "1. x" -> "1\. x", "- x" -> "\- x", "## x" -> "\## x" ("1.5" as is)
    line = re.sub(r"^(\d+)([.)])(?=\s|$)", r"\1\\\2", line)
    return re.sub(r"^([+-]|#+)(?=\s|$)", r"\\\1", line)


class Block(dict):
    """A block of the text. kind: "paragraph", "heading", "quote", "bullets", "numbers" or "code". lines: [(words,
    where they start in the text)]: a paragraph's or a quote's lines, a heading's one, a list's items (numbers: from
    number); code: lang and body. start, end: the block's place in the text."""

    kind = property(lambda self: self["kind"])
    lines = property(lambda self: self.get("lines") or [])
    start = property(lambda self: self["start"])
    end = property(lambda self: self["end"])
    number = property(lambda self: int(self.get("number", 1)))  # a numbered list's first number

    def runs(self, src: Optional[str] = None) -> List[List[Run]]:
        """Each line's runs (a heading's are bold); a line that is a rule ("*****", "---") is words as written. src:
        the text the blocks were read from with its @s' names masked (without_names): the runs' words and addresses
        are taken from it, as written."""
        out = [[Run("text", words, at, "", False, at, at + len(words))] if RULE.match(words)
               else inline(words, at, self.kind == "heading") for words, at in self.lines]
        if src is None:
            return out
        return [[r._replace(words=src[r.at:r.at + len(r.words)],
                            url=src[r.end - 1 - len(r.url):r.end - 1] if r.kind == "link" else
                            src[r.at:r.at + len(r.url)] if r.url else "") for r in line] for line in out]

    def has_code(self) -> bool:
        """Whether a line of it has `code`."""
        return any(r.kind == "code" for line in self.runs() for r in line)

    def markdown(self, src: Optional[str] = None) -> str:
        """The block as Markdown on its own, made from its runs (literal words escaped, each line as one, a list's
        items unindented): what a backend that renders Markdown shows for it, as the other blocks show. src: as
        runs'."""
        k = self.kind
        if k == "code":
            body = self["body"]
            if src is not None:  # (as written: a name in it too; a CRLF as an LF, as the body reads)
                body = "\n".join(line[:-1] if line.endswith("\r") else line
                                  for line in src[self["body_at"]:self["body_end"]].split("\n")) if body else ""
            ticks = max([len(t) for t in re.findall(r"`{3,}", body)] + [2]) + 1  # a fence the body never closes
            return "%s%s\n%s%s%s" % ("`" * ticks, self.get("lang") or "", body, "\n" if body else "", "`" * ticks)
        # a line of only - = * _ is words, in a quote or a list too (never a heading's underline)
        lines = [(lambda w: "\\" + w if SETEXT.match(w) else w)(_md_line(runs)) for runs in self.runs(src)]
        if k == "quote":
            return "\n".join("> " + w for w in lines)
        if k == "bullets":
            return "\n".join("- " + w for w in lines)
        if k == "numbers":
            return "\n".join("%d. %s" % (self.number + i, w) for i, w in enumerate(lines))
        return "  \n".join(lines)  # a paragraph (a heading: its one bold line): each line break a hard one


def _closes(line: str, fence: str) -> bool:
    """Whether the line closes the fence: up to 3 spaces (no tab), its mark at least as many times, nothing else."""
    lead = len(line) - len(line.lstrip(" "))
    s = line[lead:].rstrip()
    return lead <= 3 and len(s) >= len(fence) and s == fence[0] * len(s)


def _starts_block(line: str) -> bool:
    return bool(FENCE.match(line) or HEADING.match(line) or QUOTE.match(line)
                or (not RULE.match(line) and (BULLET.match(line) or NUMBERED.match(line))))


def blocks(text: str) -> List[Block]:
    """The text's blocks in order (empty lines between them only; none when the text is empty)."""
    raw = (text or "").split("\n")
    offs: List[int] = []
    at = 0
    for line in raw:
        offs.append(at)
        at += len(line) + 1
    lines = [line[:-1] if line.endswith("\r") else line for line in raw]  # a CRLF ends a line as an LF does
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
            out.append(Block(kind="code", lang=fence.group(2).lower(), body=body, start=offs[i], end=end_of(last),
                             body_at=offs[i + 1] if i + 1 < n else end_of(i),
                             body_end=(offs[j] - 1) if j < n else len(text or "")))
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
                if q.group(1) is not None:
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
            got.append((lines[j].rstrip(), offs[j]))  # its indent kept ("    run()")
            j += 1
        out.append(Block(kind="paragraph", lines=got, start=offs[i], end=end_of(j - 1)))
        i = j
    return out


def without_names(text: str, names=()) -> str:
    """The text with each "@name" of the names (those to @) masked: a name is never markup ("@**Ops**" is a name)."""
    for name in sorted({n for n in names if n}, key=len, reverse=True):
        text = text.replace("@" + name, "@" + "\x02" * len(name))
    return text


def features(text: str, names=()) -> List[str]:
    """What markup of the subset the text uses, in words (粗体, 链接, 列表, 代码, 标题, 引用): [] for a plain text, which
    goes as it always did. names: those to @, whose "@name" is never markup."""
    seen: Dict[str, bool] = {}
    for b in blocks(without_names(text, names)):
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


def formatted(text: str, names=()) -> bool:
    """Whether the text uses the subset at all (else it is sent as plain text, as before); names: as features'."""
    return bool(features(text, names))


def no_mentions(text: str) -> List[Tuple[int, int]]:
    """Where an "@name" is no @: code (fenced or `inline`), links and bare addresses, as (start, end) places."""
    out: List[Tuple[int, int]] = []
    for b in blocks(text):
        if b.kind == "code":
            out.append((b.start, b.end))
            continue
        for line in b.runs():
            out.extend((r.start, r.end) for r in line if r.kind in ("code", "link", "url"))
        for words, at in b.lines:  # an image's address too (its text is words, never an @'s place)
            plain = CODE_SPAN.sub(lambda c: "`" + MASK * (len(c.group()) - 2) + "`", words)
            out.extend((at + x.start(), at + x.end()) for x in IMAGE.finditer(plain))
    return out


FRONT_END = "\n\n"  # after the @s a formatted text does not write (textutil.mention_spans' front_end): a paragraph


def code_as_written(text: str, ats: List[int]) -> bool:
    """Whether a block with an @ in it (ats: where each "@name" is in the text) has `code`: it goes as words then
    (an @ is no Markdown's), its code's backticks shown, and the card says so."""
    return any(b.kind != "code" and b.has_code() and any(b.start <= a < b.end for a in ats) for b in blocks(text))


def block_of(bs: List[Block], at: int) -> Optional[Block]:
    """The block a place in the text is in (None: between blocks)."""
    for b in bs:
        if b.start <= at < b.end or (at == b.end and b.start < b.end):
            return b
    return None
