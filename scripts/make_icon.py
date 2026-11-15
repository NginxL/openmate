#!/usr/bin/env python3
"""Draw OpenMate's own icon (no third-party logo): a gradient rounded square with a chat bubble holding two linked
rings, "mates". Standard library only; the output is byte-for-byte the same every time.

    python3 -I scripts/make_icon.py extensions/openmate-feishu/assets/icon.png
"""

import math
import struct
import sys
import zlib

SIZE = 512
SS = 3  # samples per axis per pixel
TOP_LEFT = (37, 99, 235)
BOTTOM_RIGHT = (16, 185, 160)


def rounded_rect(x, y, x0, y0, x1, y1, r):
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r and x0 <= x <= x1 and y0 <= y <= y1


def triangle(x, y, a, b, c):
    def side(p, q):
        return (q[0] - p[0]) * (y - p[1]) - (q[1] - p[1]) * (x - p[0])

    d1, d2, d3 = side(a, b), side(b, c), side(c, a)
    neg = d1 < 0 or d2 < 0 or d3 < 0
    pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (neg and pos)


def ring(x, y, cx, cy, r, w):
    d = math.hypot(x - cx, y - cy)
    return r - w / 2 <= d <= r + w / 2


def gradient(x, y):
    t = (x + y) / (2.0 * SIZE)
    return tuple(int(round(a + (b - a) * t)) for a, b in zip(TOP_LEFT, BOTTOM_RIGHT))


def sample(x, y):
    """RGBA of one point, or None for transparent."""
    if not rounded_rect(x, y, 0, 0, SIZE, SIZE, 116):
        return None
    bubble = rounded_rect(x, y, 96, 120, 416, 352, 72) or triangle(x, y, (150, 330), (128, 424), (246, 346))
    if bubble:
        if ring(x, y, 214, 236, 54, 22) or ring(x, y, 298, 236, 54, 22):
            return gradient(x, y) + (255,)
        return (255, 255, 255, 255)
    return gradient(x, y) + (255,)


def render():
    rows = []
    step = 1.0 / SS
    for py in range(SIZE):
        row = bytearray([0])  # filter: none
        for px in range(SIZE):
            acc = [0, 0, 0, 0]
            for sy in range(SS):
                for sx in range(SS):
                    c = sample(px + (sx + 0.5) * step, py + (sy + 0.5) * step)
                    if c is not None:
                        acc[0] += c[0] * c[3]
                        acc[1] += c[1] * c[3]
                        acc[2] += c[2] * c[3]
                        acc[3] += c[3]
            n = SS * SS
            a = acc[3] / n
            if a <= 0:
                row += b"\0\0\0\0"
            else:
                row += bytes((int(round(acc[0] / acc[3])), int(round(acc[1] / acc[3])), int(round(acc[2] / acc[3])),
                              int(round(a))))
        rows.append(bytes(row))
    return b"".join(rows)


def png(raw):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: make_icon.py <out.png>\n")
        return 2
    with open(argv[1], "wb") as f:
        f.write(png(render()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
