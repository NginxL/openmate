"""Structured /agent replies for platforms that send pictures and plain text."""

import json
import logging
import stat
import subprocess
import sys
import tempfile
import threading
from contextlib import ExitStack, contextmanager
from hashlib import sha256
from pathlib import Path
from typing import List

from . import images, markup
from .wire import RPCError

log = logging.getLogger("openmate.cards")

REPLY_WORDS = ("要回复的内容。可以用 Markdown 排版：**粗体**、[文字](https://地址)、列表（- 或 1.）、"
               "行内代码、围栏代码块、# 标题和 > 引用，空一行另起一段。有结构的长内容会排成一张图片，"
               "先发摘要；短内容按纯文字发。进度、自动代答和已带 image 的回复请用纯文字。")


def enabled(app) -> bool:
    return (getattr(app.platform, "render_cards", False)
            and getattr(app.backend, "can_send_images", False)
            and not getattr(app.backend, "can_format", False))


LISTS = ("bullets", "numbers")


def _should_render(text: str, blocks: List[markup.Block]) -> bool:
    """The caller has already checked that the answer uses the Markdown subset."""
    return (len(text) > 200 or any(b.kind in ("heading", "quote", "code") for b in blocks)
            or sum(len(b.lines) for b in blocks if b.kind in LISTS) >= 3
            or sum(b.kind == "paragraph" for b in blocks) >= 3)


def _words(run: markup.Run) -> str:
    return run.words + " " + run.url if run.kind == "link" else run.words


def _plain_block(text: str, block: markup.Block) -> str:
    if block.kind == "code":
        return block["body"]
    lines = ["".join(_words(r) for r in line) for line in block.runs()]
    if block.kind == "bullets":
        lines = ["• " + line for line in lines]
    elif block.kind == "numbers":
        source = text[block.start:block.end].split("\n")
        lines = [original.split(maxsplit=1)[0] + " " + line for original, line in zip(source, lines)]
    return "\n".join(lines)


def _plain_gap(gap: str) -> str:
    # A lone > has no words and is omitted by the block parser.
    return "\n".join(line for line in gap.split("\n") if not markup.QUOTE.fullmatch(line))


def plain_text(text: str, blocks: List[markup.Block]) -> str:
    parts, end = [], 0
    for block in blocks:
        parts.extend((_plain_gap(text[end:block.start]), _plain_block(text, block)))
        end = block.end
    parts.append(_plain_gap(text[end:]))
    return "".join(parts)


def _summary(text: str, blocks: List[markup.Block]) -> str:
    first = next((b for b in blocks if b.kind == "paragraph"), blocks[0])
    words = " ".join(_plain_block(text, first).split())
    if len(words) > 80:
        words = words[:79] + "…"
    return words + "（全文见下图）"


WIDTH = 750


def to_document(text: str, blocks: List[markup.Block], mark: str = "") -> dict:
    """The renderer receives plain text and explicit styles for each block and run."""
    out = []
    for block in blocks:
        kind = block.kind
        if kind == "code":
            out.append({"kind": kind, "text": block["body"]})
            continue
        lines = [[{"text": _words(run), "bold": run.bold, "code": run.kind == "code"} for run in line]
                 for line in block.runs()]
        if kind == "heading":
            level = min(3, len(text[block.start:block.lines[0][1]].strip()))
            out.append({"kind": kind, "level": level, "runs": lines[0]})
        elif kind in LISTS:
            items = [{"marker": "•" if kind == "bullets" else "%d." % (block.number + i), "runs": runs}
                     for i, runs in enumerate(lines)]
            out.append({"kind": kind, "items": items})
        else:
            out.append({"kind": kind, "lines": lines})
    return {"width": WIDTH, "mark": mark, "blocks": out}


XCRUN = "/usr/bin/xcrun"
_COMPILE_LOCK = threading.Lock()
_SWIFT = r"""
import AppKit

struct CardDocument: Decodable {
    let width: Int
    let mark: String
    let blocks: [Block]
}

struct Block: Decodable {
    enum Kind: String, Decodable { case heading, paragraph, bullets, numbers, quote, code }

    let kind: Kind
    let level: Int?
    let runs: [Run]?
    let lines: [[Run]]?
    let items: [ListItem]?
    let text: String?
}

struct Run: Decodable {
    let text: String
    let bold: Bool
    let code: Bool
}

struct ListItem: Decodable {
    let marker: String
    let runs: [Run]
}

func color(_ rgb: UInt32) -> NSColor {
    return NSColor(srgbRed: CGFloat((rgb >> 16) & 255) / 255, green: CGFloat((rgb >> 8) & 255) / 255,
                   blue: CGFloat(rgb & 255) / 255, alpha: 1)
}

func bodyFont(_ size: CGFloat) -> NSFont {
    return NSFont(name: "PingFang SC", size: size) ?? .systemFont(ofSize: size)
}

let bodyColor = color(0x1f2328)
let quoteColor = color(0x57606a)
let inlineColor = color(0xeff1f3)
let mono = NSFont.monospacedSystemFont(ofSize: 14, weight: .regular)
let monoBold = NSFont.monospacedSystemFont(ofSize: 14, weight: .semibold)
let body = bodyFont(16)
let paragraphGap: CGFloat = 12
// Keep manual line breaks within a single TextKit paragraph, so a block's border or background spans all its lines.
let lineBreak = "\u{2028}"

func styled(_ words: String, font: NSFont, ink: NSColor) -> NSAttributedString {
    return NSAttributedString(string: words, attributes: [.font: font, .foregroundColor: ink])
}

func setParagraph(_ text: NSMutableAttributedString, font: NSFont, ink: NSColor,
                  after: CGFloat, before: CGFloat = 0, block: NSTextBlock? = nil, indent: CGFloat = 0) {
    text.append(styled("\n", font: font, ink: ink))
    let style = NSMutableParagraphStyle()
    style.lineHeightMultiple = 1.25
    style.paragraphSpacing = after
    style.paragraphSpacingBefore = before
    if let block = block {
        style.textBlocks = [block]
    }
    if indent > 0 {
        style.tabStops = [NSTextTab(textAlignment: .left, location: indent, options: [:])]
        style.headIndent = indent
        style.firstLineHeadIndent = 0
    }
    text.addAttribute(.paragraphStyle, value: style, range: NSRange(location: 0, length: text.length))
}

func appendLines(_ lines: [[Run]], to text: NSMutableAttributedString, font: NSFont, ink: NSColor, bold: Bool = false) {
    let strong = NSFontManager.shared.convert(font, toHaveTrait: .boldFontMask)
    for (index, line) in lines.enumerated() {
        if index > 0 {
            text.append(styled(lineBreak, font: font, ink: ink))
        }
        for run in line {
            let heavy = bold || run.bold
            let face = run.code ? (heavy ? monoBold : mono) : (heavy ? strong : font)
            var attributes: [NSAttributedString.Key: Any] = [.font: face, .foregroundColor: ink]
            if run.code {
                attributes[.backgroundColor] = inlineColor
            }
            text.append(NSAttributedString(string: run.text, attributes: attributes))
        }
    }
}

func fullWidthBlock() -> NSTextBlock {
    let table = NSTextTable()
    table.numberOfColumns = 1
    table.layoutAlgorithm = .fixed
    table.setValue(100, type: .percentageValueType, for: .width)
    return NSTextTableBlock(table: table, startingRow: 0, rowSpan: 1, startingColumn: 0, columnSpan: 1)
}

func attributed(_ document: CardDocument) -> NSMutableAttributedString {
    let text = NSMutableAttributedString(string: "")
    for (index, block) in document.blocks.enumerated() {
        let hasNext = index < document.blocks.count - 1 || !document.mark.isEmpty
        let after = hasNext ? paragraphGap : 0
        let paragraph = NSMutableAttributedString(string: "")
        switch block.kind {
        case .heading:
            guard let level = block.level, (1...3).contains(level), let runs = block.runs else { exit(2) }
            let font = bodyFont(CGFloat(24 - 2 * level))
            appendLines([runs], to: paragraph, font: font, ink: bodyColor, bold: true)
            setParagraph(paragraph, font: font, ink: bodyColor, after: hasNext ? 16 : 0, before: 8)
        case .paragraph:
            guard let lines = block.lines else { exit(2) }
            appendLines(lines, to: paragraph, font: body, ink: bodyColor)
            setParagraph(paragraph, font: body, ink: bodyColor, after: after)
        case .bullets, .numbers:
            guard let items = block.items else { exit(2) }
            let markerAttributes: [NSAttributedString.Key: Any] = [.font: body]
            let widest = items.map { ($0.marker as NSString).size(withAttributes: markerAttributes).width }.max() ?? 0
            let indent = max(24, ceil(widest) + 8)
            for (itemIndex, item) in items.enumerated() {
                let line = NSMutableAttributedString(attributedString: styled(item.marker + "\t", font: body, ink: bodyColor))
                appendLines([item.runs], to: line, font: body, ink: bodyColor)
                setParagraph(line, font: body, ink: bodyColor, after: itemIndex == items.count - 1 ? after : 0,
                             indent: indent)
                paragraph.append(line)
            }
        case .quote:
            guard let lines = block.lines else { exit(2) }
            let cell = fullWidthBlock()
            cell.setWidth(3, type: .absoluteValueType, for: .border, edge: .minX)
            cell.setBorderColor(color(0xd0d7de), for: .minX)
            cell.setWidth(12, type: .absoluteValueType, for: .padding, edge: .minX)
            appendLines(lines, to: paragraph, font: body, ink: quoteColor)
            setParagraph(paragraph, font: body, ink: quoteColor, after: after, block: cell)
        case .code:
            guard let words = block.text else { exit(2) }
            let cell = fullWidthBlock()
            cell.backgroundColor = color(0xf6f8fa)
            cell.setWidth(12, type: .absoluteValueType, for: .padding)
            // TextKit's cell padding absorbs paragraphSpacing. Put the gap outside the colored cell.
            cell.setWidth(after, type: .absoluteValueType, for: .margin, edge: .maxY)
            paragraph.append(styled(words.replacingOccurrences(of: "\n", with: lineBreak), font: mono, ink: bodyColor))
            setParagraph(paragraph, font: mono, ink: bodyColor, after: 0, block: cell)
        }
        text.append(paragraph)
    }
    if !document.mark.isEmpty {
        let font = bodyFont(13)
        let ink = color(0x8c959f)
        let cell = fullWidthBlock()
        cell.setWidth(1, type: .absoluteValueType, for: .border, edge: .minY)
        cell.setBorderColor(color(0xd8dee4), for: .minY)
        cell.setWidth(10, type: .absoluteValueType, for: .padding, edge: .minY)
        let footer = NSMutableAttributedString(attributedString: styled(document.mark, font: font, ink: ink))
        setParagraph(footer, font: font, ink: ink, after: 0, block: cell)
        text.append(footer)
    }
    return text
}

let padding: CGFloat = 32
let scale = 2
let maxHeight: CGFloat = 12000
guard CommandLine.arguments.count == 2,
      let document = try? JSONDecoder().decode(CardDocument.self, from: FileHandle.standardInput.readDataToEndOfFile()),
      document.width > Int(2 * padding) else { exit(2) }
let text = attributed(document)
let last = (text.string as NSString).rangeOfCharacter(
    from: CharacterSet.whitespacesAndNewlines.inverted, options: .backwards)
guard last.location != NSNotFound else { exit(2) }
text.deleteCharacters(in: NSRange(location: NSMaxRange(last), length: text.length - NSMaxRange(last)))

let storage = NSTextStorage(attributedString: text)
let layout = NSLayoutManager()
let contentWidth = CGFloat(document.width) - 2 * padding
let container = NSTextContainer(containerSize: NSSize(width: contentWidth, height: .greatestFiniteMagnitude))
container.lineFragmentPadding = 0
layout.addTextContainer(container)
storage.addLayoutManager(layout)
layout.ensureLayout(for: container)
let glyphs = layout.glyphRange(for: container)
let height = ceil(layout.usedRect(for: container).maxY) + 2 * padding
guard height.isFinite && height <= maxHeight else { exit(3) }

guard let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: document.width * scale,
                                    pixelsHigh: Int(height) * scale, bitsPerSample: 8, samplesPerPixel: 4,
                                    hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                                    bytesPerRow: 0, bitsPerPixel: 0),
      let context = NSGraphicsContext(bitmapImageRep: bitmap) else { exit(2) }
NSGraphicsContext.saveGraphicsState()
let cg = context.cgContext
NSGraphicsContext.current = NSGraphicsContext(cgContext: cg, flipped: true)
cg.translateBy(x: 0, y: height * CGFloat(scale))
cg.scaleBy(x: CGFloat(scale), y: -CGFloat(scale))
cg.setFillColor(NSColor.white.cgColor)
cg.fill(NSRect(x: 0, y: 0, width: CGFloat(document.width), height: height))
let origin = NSPoint(x: padding, y: padding)
layout.drawBackground(forGlyphRange: glyphs, at: origin)
layout.drawGlyphs(forGlyphRange: glyphs, at: origin)
NSGraphicsContext.restoreGraphicsState()

guard let png = bitmap.representation(using: .png, properties: [:]) else { exit(2) }
guard png.count <= 10 * 1024 * 1024 else { exit(3) }
do {
    try png.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
} catch {
    exit(2)
}
"""


class RenderError(Exception):
    pass


def _run(argv: list, folder: Path, timeout: int, data: bytes = b"") -> int:
    return subprocess.run(argv, input=data, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          cwd=folder, env={"PATH": "/usr/bin:/bin", "TMPDIR": str(folder)},
                          timeout=timeout).returncode


def _renderer(cache: Path) -> Path:
    src = _SWIFT.encode("utf-8")
    binary = cache / ("render-" + sha256(src).hexdigest())
    with _COMPILE_LOCK:
        if binary.is_symlink():
            raise RenderError("renderer cache entry is a symbolic link")
        if binary.is_file():
            return binary
        # Only publish a complete binary. Another process can read the cache while this one builds.
        with tempfile.TemporaryDirectory(prefix="build-", dir=cache) as work:
            work = Path(work)
            source = work / "render.swift"
            source.write_bytes(src)
            built = work / "render"
            try:
                status = _run([XCRUN, "--no-cache", "swiftc", "-O", "-module-cache-path", str(work / "modules"),
                               str(source), "-o", str(built)], work, 120)
            except subprocess.TimeoutExpired:
                raise RenderError("Swift compilation timed out") from None
            if status != 0:
                raise RenderError("Swift compilation failed (exit %d)" % status)
            built.replace(binary)
    return binary


def render(document: dict, path: Path, cache: Path) -> None:
    binary = _renderer(cache)
    data = json.dumps(document, ensure_ascii=False).encode("utf-8")
    try:
        status = _run([str(binary), str(path)], path.parent, 20, data)
    except subprocess.TimeoutExpired:
        raise RenderError("rendering timed out") from None
    if status == 3:
        raise RenderError("image exceeds the height or size limit")
    if status != 0:
        raise RenderError("renderer failed (exit %d)" % status)


@contextmanager
def reply(text: str, data_dir: str, mark: str = ""):
    """Text to send first, an optional checked picture, and its full text for a rejected image send."""
    if not markup.formatted(text):
        yield text, None, ""
        return
    blocks = markup.blocks(text)
    fulltext = plain_text(text, blocks)
    if not _should_render(text, blocks):
        yield fulltext, None, ""
        return

    with ExitStack() as files:
        try:
            if sys.platform != "darwin":
                raise RenderError("macOS is required")
            if not data_dir:
                raise RenderError("extension data directory is unavailable")
            cache = Path(data_dir).resolve() / "agent-cards"
            cache.mkdir(mode=0o700, exist_ok=True)
            mode = cache.lstat().st_mode
            if not stat.S_ISDIR(mode) or stat.S_IMODE(mode) != 0o700:
                raise RenderError("renderer cache is not private")
            work = files.enter_context(tempfile.TemporaryDirectory(prefix="reply-", dir=cache))
            path = Path(work) / "reply.png"
            render(to_document(text, blocks, mark), path, cache)
            if path.lstat().st_size > images.IMAGE_MOST:
                raise RenderError("PNG exceeds the size limit")
            picture = images.take(str(path), work, source_dir=work)
        except RenderError as e:
            log.warning("an /agent reply card fell back to text: %s", e)
        except RPCError:
            log.warning("an /agent reply card fell back to text: picture could not be validated")
        except Exception as e:
            log.warning("an /agent reply card fell back to text: %s", type(e).__name__)
        else:
            yield _summary(text, blocks), picture, fulltext
            return
    yield fulltext, None, ""
