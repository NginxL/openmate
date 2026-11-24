"""Pictures for the model in a tool's answer (modelimages, connector-protocol "Pictures in a tool's answer"): what the
hub takes is sent as it is, a picture over a limit is drawn again smaller (Pillow, else sips; both stand-ins here, so
the run needs neither), and the answer's totals are kept."""

try:  # first: every temporary directory of this run in one, removed at its end
    import _runtmp  # noqa: F401
except ImportError:  # (started by its path from elsewhere: its directory is not on the path)
    pass
import base64
import struct
import unittest

import harness  # noqa: F401  (the core on the path)
from openmate_core import modelimages as mi
from openmate_core.fixture import tiny_png


def jpeg_header(w: int, h: int, pad: int = 0) -> bytes:
    """The start of a JPEG (SOI, an APP0, SOF0 with its size) and pad bytes: enough for its size to be read."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof + b"\x00" * pad + b"\xff\xd9"


def webp_vp8x(w: int, h: int) -> bytes:
    body = b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00" + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little")
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + body


class ModelImagesTest(unittest.TestCase):
    def setUp(self):
        self.drawn = []
        for name in ("_pillow", "_sips"):
            self.addCleanup(setattr, mi, name, getattr(mi, name))
        mi._pillow = lambda data, side: self.drawn.append(("pillow", side)) or jpeg_header(side, side // 2)
        mi._sips = lambda data, side: self.drawn.append(("sips", side)) or None

    def test_sizes_are_read_from_the_headers(self):
        self.assertEqual(mi.size_of(tiny_png(size=3)), (3, 3))
        self.assertEqual(mi.size_of(jpeg_header(640, 480)), (640, 480))
        self.assertEqual(mi.size_of(webp_vp8x(300, 200)), (300, 200))
        self.assertIsNone(mi.size_of(b"GIF89a" + b"\x00" * 20))
        self.assertIsNone(mi.size_of(b"\xff\xd8\xff"), "cut off")

    def test_within_the_limits_it_goes_as_it_is(self):
        png = tiny_png(size=4)
        self.assertEqual(mi.fit(png), ("image/png", png, 16))
        big = jpeg_header(2000, 1000, pad=1000)
        self.assertEqual(mi.fit(big)[1], big)
        self.assertEqual(self.drawn, [])

    def test_too_wide_or_too_heavy_or_another_type_is_drawn_again(self):
        for data in (jpeg_header(2001, 10), jpeg_header(100, 100, pad=mi.ONE_MOST), b"GIF89a" + b"\x00" * 30):
            self.drawn.clear()
            mime, body, pixels = mi.fit(data)
            self.assertEqual((mime, mi.size_of(body), pixels), ("image/jpeg", (1568, 784), 1568 * 784))
            self.assertEqual(self.drawn, [("pillow", 1568)])

    def test_sips_when_pillow_cannot_and_none_when_neither(self):
        mi._pillow = lambda data, side: None
        mi._sips = lambda data, side: self.drawn.append(("sips", side)) or tiny_png(size=8)
        self.assertEqual(mi.fit(jpeg_header(3000, 3000))[0], "image/png")
        self.assertEqual(self.drawn, [("sips", 1568)])
        mi._sips = lambda data, side: None
        self.assertIsNone(mi.fit(jpeg_header(3000, 3000)))
        self.assertIsNone(mi.fit(b"not a picture"))

    def test_an_answer_keeps_the_hubs_totals(self):
        p = mi.Pictures()
        self.assertEqual(p.add(tiny_png(size=2), "第一张\n换行  和\t空白" + "长" * 300), "")
        self.assertEqual(p.images[0]["mime"], "image/png")
        self.assertEqual(base64.b64decode(p.images[0]["data"]), tiny_png(size=2))
        self.assertEqual(len(p.images[0]["caption"]), 200)
        self.assertNotIn("\n", p.images[0]["caption"])
        for _ in range(mi.COUNT_MOST - 1):
            self.assertEqual(p.add(tiny_png(size=2), "x"), "")
        self.assertIn("16", p.add(tiny_png(size=2), "x"))
        mi._pillow = lambda data, side: None
        self.assertIn("没法", mi.Pictures().add(b"GIF89a" + b"\x00" * 30, "x"))
        pixels = mi.Pictures()
        self.assertEqual(pixels.add(jpeg_header(2000, 2000), "a"), "")
        self.assertEqual(pixels.add(jpeg_header(2000, 2000), "b"), "")
        self.assertIn("够多", pixels.add(jpeg_header(10, 10), "c") or pixels.add(jpeg_header(2000, 1), "d"))
        heavy = mi.Pictures()
        for i in range(4):
            self.assertEqual(heavy.add(jpeg_header(10, 10, pad=mi.ONE_MOST - 200), str(i)), "")
        self.assertIn("够多", heavy.add(jpeg_header(10, 10, pad=1000), "5"), "4 MiB in all")


if __name__ == "__main__":
    unittest.main()
