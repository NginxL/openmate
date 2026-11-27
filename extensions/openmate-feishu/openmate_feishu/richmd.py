"""A Feishu rich text (TEXT or POST, as LarkAgentX decodes it) as safe Markdown, for the thread page only: bold and
italics, lists, quotes, headings, code and links shown as the sender formatted them. Everything else reads the plain
text as before (commands, @s, the archive, quotes, the models' chat lines).

Safe: every word from the message is escaped (an ASCII punctuation mark gets a backslash), so nothing written in a
message becomes Markdown or HTML of its own; a link is kept only with an http(s) address, its address percent-encoded
where it could end the link; code is fenced longer than any run of backticks in it. A rich text with none of these
formats gives "" (the page shows the plain text, as before), and so does one this walk cannot read.

Written for OpenMate after the behaviour nwverse/persona-extensions describes for its larkx extension (#1073,
"Preserve rich text as safe Markdown"); the element tags and properties are LarkAgentX's (entities.RichTextElement)."""

from __future__ import annotations

import re
import string
from typing import Dict, List, Optional
from urllib.parse import urlsplit

TEXT, IMG, P, FIGURE, AT, A, B, I, U = 1, 2, 3, 4, 5, 6, 7, 8, 9
EMOTION, DIV, LINK, MEDIA, DOCS = 10, 14, 17, 18, 22
H1, H2, H3, UL, OL, LI, QUOTE, CODE, CODE_BLOCK, HR = 23, 24, 25, 26, 27, 28, 29, 30, 31, 32
CODE_BLOCK_V2, H4, H5, H6, MARKDOWN = 37, 44, 45, 46, 49
HEADINGS = {H1: 1, H2: 2, H3: 3, H4: 4, H5: 5, H6: 6}
# what makes a message formatted: without one of these its plain text is shown as it always was
FORMATS = frozenset((B, I, A, LINK, UL, OL, QUOTE, CODE, CODE_BLOCK, CODE_BLOCK_V2, HR, MARKDOWN) + tuple(HEADINGS))
PLACEHOLDERS = {IMG: "[图片]", EMOTION: "[表情]", MEDIA: "[视频]"}
DEPTH_MOST = 20
_ESCAPED = frozenset(string.punctuation)
_FENCE = re.compile(r"^(`{3,}|~{3,})([^\n`]*)\n(.*?)\n?\1[ \t]*$", re.S)


def escape(words: str) -> str:
    """Literal words: each ASCII punctuation mark escaped, so none starts Markdown or HTML."""
    return "".join("\\" + c if c in _ESCAPED else c for c in words)


def link(href: str, words: str = "") -> str:
    """A Markdown link to an http(s) address, or the escaped words (or address) when it has no such address."""
    href, words = (href or "").strip(), (words or "").strip()
    ok = bool(href) and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in href)
    if ok:
        try:
            parts = urlsplit(href)
            ok = parts.scheme.lower() in ("http", "https") and bool(parts.netloc)
        except ValueError:
            ok = False
    if not ok:
        return escape(words or href)
    for c, code in (("\\", "%5C"), ("(", "%28"), (")", "%29"), ("<", "%3C"), (">", "%3E")):
        href = href.replace(c, code)
    if not words or words == href:
        return "<%s>" % href
    return "[%s](%s)" % (escape(words), href)


def code_span(words: str) -> str:
    words = re.sub(r"\r\n?|\n", " ", words or "")
    if not words.strip():
        return escape(words)
    fence = "`" * (max([len(r) for r in re.findall(r"`+", words)] or [0]) + 1)
    pad = " " if words[:1] in ("`", " ") or words[-1:] in ("`", " ") else ""
    return fence + pad + words + pad + fence


def code_block(words: str, lang: str = "") -> str:
    words = (words or "").replace("\r\n", "\n").replace("\r", "\n")
    fence = "`" * max(3, (max([len(r) for r in re.findall(r"`+", words)] or [0]) + 1))
    lang = lang.strip() if re.match(r"^[A-Za-z0-9_+.#-]{1,30}$", (lang or "").strip()) else ""
    return fence + lang + "\n" + words.rstrip("\n") + "\n" + fence


def _tag(el: dict) -> int:
    tag = el.get("tag", 1)
    if isinstance(tag, bool):
        return -1
    try:
        return int(tag)
    except (TypeError, ValueError):
        return -1


def _styled(el: dict) -> tuple:
    """(bold, italic) as the element's style says (the web client marks bold on a TEXT run: fontWeight bold)."""
    style = el.get("style") if isinstance(el.get("style"), dict) else {}
    weight = str(style.get("fontWeight") or "").lower()
    slant = str(style.get("fontStyle") or "").lower()
    return weight in ("bold", "700", "800", "900"), slant == "italic"


def rich_markdown(rich: dict, P_, title: str = "") -> str:
    """The rich text as safe Markdown, title (a POST's) as a bold line first; "" when it has no format (FORMATS, or a
    bold or italic style) or cannot be read."""
    try:
        return _render(rich, P_, title)
    except Exception:
        return ""


def _render(rich: dict, P_, title: str) -> str:
    from .larkagentx import code_block_words, link_text_parts, wire_fields

    dictionary = (rich.get("elements") or {}).get("dictionary") or {} if isinstance(rich, dict) else {}
    if not isinstance(dictionary, dict) or not dictionary:
        return ""
    els: Dict[str, dict] = {str(k): v for k, v in dictionary.items() if isinstance(v, dict)}
    formatted = any(_tag(el) in FORMATS or any(_styled(el)) for el in els.values())
    if not formatted:
        return ""
    ids = [str(k) for k in rich.get("elementIds") or [] if str(k) in els]
    kids_of = {k: [str(c) for c in (el.get("childIds") or []) if str(c) in els] for k, el in els.items()}
    if not ids:  # no roots named: the elements no other one holds, in their numbers' order
        held = {c for cs in kids_of.values() for c in cs}
        ids = [k for k in els if k not in held]
        try:
            ids.sort(key=int)
        except ValueError:
            pass
    seen: set = set()

    def words_of(prop) -> str:
        if not prop:
            return ""
        try:
            tp = P_.TextProperty()
            tp.ParseFromString(prop)
            return tp.content
        except Exception:
            return ""

    def inline(k: str, depth: int) -> str:
        """An element inside a line, and what it holds."""
        if k in seen or depth > DEPTH_MOST:
            return ""
        seen.add(k)
        el = els[k]
        tag, prop = _tag(el), el.get("property") or b""
        kids = "".join(inline(c, depth + 1) for c in kids_of[k])
        if tag == CODE:
            return code_span(words_of(prop) + "".join(plain(c) for c in kids_of[k]))
        if tag == A:
            href, words = link_text_parts(prop) if prop else ("", "")
            return link(href, words) + kids
        if tag == LINK:
            href = next((w for n, w in wire_fields(prop) if n == 1), "") if prop else ""
            return link(href) + kids
        if tag == AT:
            try:
                ap = P_.AtProperty()
                ap.ParseFromString(prop)
                own = escape(ap.content or "@")
            except Exception:
                own = escape("@")
            return own + kids
        if tag in PLACEHOLDERS:
            return escape(PLACEHOLDERS[tag]) + kids
        if tag in (CODE_BLOCK, CODE_BLOCK_V2, MARKDOWN) or tag in HEADINGS or tag in (UL, OL, QUOTE, HR, P, DOCS, DIV):
            seen.discard(k)
            return block(k, depth)  # a block inside a line (a client's odd tree): on lines of its own
        own = escape(words_of(prop)) if tag in (TEXT, B, I, U, LI) else ""
        body = (own + kids).replace("\n", "  \n")  # a line break inside a paragraph stays one
        bold, italic = _styled(el)
        bold, italic = bold or tag == B, italic or tag == I
        core = body.strip()
        if core and (bold or italic):
            lead, tail = body[:len(body) - len(body.lstrip())], body[len(body.rstrip()):]
            mark = "**" if bold else ""
            core = mark + ("*%s*" % core if italic else core) + mark
            return lead + core + tail
        return body

    def plain(k: str) -> str:
        """An element's words with nothing made of them (inside code)."""
        if k in seen:
            return ""
        seen.add(k)
        return words_of(els[k].get("property") or b"") + "".join(plain(c) for c in kids_of[k])

    def block(k: str, depth: int, number: int = 0, indent: str = "") -> str:
        """A block and what it holds, ending with a blank line (a list item: with a line break)."""
        if k in seen or depth > DEPTH_MOST:
            return ""
        seen.add(k)
        el = els[k]
        tag, prop = _tag(el), el.get("property") or b""
        if tag in (CODE_BLOCK, CODE_BLOCK_V2):
            lang = ""
            if tag == CODE_BLOCK and prop:
                lang = next((w for n, w in wire_fields(prop) if n == 1), "")
            return code_block(code_block_words(tag, prop) if prop else "", lang) + "\n\n"
        if tag == MARKDOWN:
            source = next((w for n, w in wire_fields(prop) if n == 1), "") if prop else ""
            fenced = _FENCE.match(source.strip().replace("\r\n", "\n"))
            if fenced:  # one fenced code block (a formatted send's code): code again, fenced anew
                return code_block(fenced.group(3), fenced.group(2)) + "\n\n"
            return escape(source).replace("\n", "  \n") + "\n\n" if source.strip() else ""  # its words, inert
        if tag == HR:
            return "---\n\n"
        if tag in (UL, OL):
            start = 1
            if tag == OL and prop:
                start = next((int(w) for n, w in _varints(prop) if n == 2 and 0 < int(w) < 100000), 1)
            out, n = "", start
            for c in kids_of[k]:
                if _tag(els[c]) == LI:
                    out += block(c, depth + 1, n if tag == OL else 0, indent)
                    n += 1
                else:
                    piece = block(c, depth + 1, 0, indent) if _is_block(els[c]) else inline(c, depth + 1)
                    out += indent + piece.strip("\n") + "\n" if piece.strip() else ""
            return out + ("\n" if not indent else "")
        if tag == LI:
            marker = "%d. " % number if number else "- "
            line, nested = "", ""
            own = escape(words_of(prop))
            for c in kids_of[k]:
                if _tag(els[c]) in (UL, OL):
                    nested += block(c, depth + 1, 0, indent + " " * len(marker))
                else:
                    line += inline(c, depth + 1)
            text = (own + line).strip().replace("  \n", " ")
            return indent + marker + text + "\n" + nested
        if tag == QUOTE:
            inner = "".join(block(c, depth + 1) if _is_block(els[c]) else inline(c, depth + 1) for c in kids_of[k])
            inner = inner.strip("\n")
            return "\n".join("> " + line if line.strip() else ">" for line in inner.split("\n")) + "\n\n" if inner else ""
        if tag in HEADINGS:
            words = (escape(words_of(prop)) + "".join(inline(c, depth + 1) for c in kids_of[k])).strip()
            words = words.replace("  \n", " ")
            return "#" * HEADINGS[tag] + " " + words + "\n\n" if words else ""
        if tag in (DOCS, DIV, FIGURE) or (not prop and kids_of[k] and tag not in (P, TEXT, B, I, U)):
            return "".join(block(c, depth + 1) if _is_block(els[c]) else para_of(c, depth + 1) for c in kids_of[k])
        if tag == P:
            words = (escape(words_of(prop)) + "".join(inline(c, depth + 1) for c in kids_of[k])).strip("\n")
            return words + "\n\n" if words.strip() else ""
        seen.discard(k)
        return para_of(k, depth)

    def para_of(k: str, depth: int) -> str:
        words = inline(k, depth).strip("\n")
        return words + "\n\n" if words.strip() else ""

    out: List[str] = []
    if title.strip():
        out.append("**%s**\n\n" % escape(title.strip()).replace("\n", " "))
    loose = ""  # inline elements at the top (a TEXT message's runs): one paragraph until a block comes
    for k in ids + [k for k in els if k not in ids]:
        if k in seen:
            continue
        if _is_block(els[k]):
            if loose.strip():
                out.append(loose.strip("\n") + "\n\n")
            loose = ""
            out.append(block(k, 0))
        else:
            loose += inline(k, 0)
    if loose.strip():
        out.append(loose.strip("\n") + "\n\n")
    return "".join(out).strip()


def _is_block(el: dict) -> bool:
    return _tag(el) in (P, DOCS, DIV, FIGURE, UL, OL, LI, QUOTE, CODE_BLOCK, CODE_BLOCK_V2, HR, MARKDOWN) \
        or _tag(el) in HEADINGS


def _varints(b: bytes):
    """(field number, value) of a property's varint fields."""
    out, i = [], 0
    try:
        while i < len(b):
            key, i = _uvarint(b, i)
            field, wire = key >> 3, key & 7
            if wire == 0:
                v, i = _uvarint(b, i)
                out.append((field, v))
            elif wire == 2:
                n, i = _uvarint(b, i)
                i += n
            elif wire == 5:
                i += 4
            elif wire == 1:
                i += 8
            else:
                break
    except IndexError:
        pass
    return out


def _uvarint(b: bytes, i: int):
    shift = v = 0
    while True:
        c = b[i]
        i += 1
        v |= (c & 0x7F) << shift
        if not c & 0x80:
            return v, i
        shift += 7
        if shift > 63:
            raise IndexError
