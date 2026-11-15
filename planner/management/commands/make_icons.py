"""Draw the app icons: a rounded square in the accent colour with "MF" on it.

    python manage.py make_icons

Writes static/icons/icon-192.png and icon-512.png, which the web manifest
and the apple-touch-icon link point at. No image library needed: the PNG is
written by hand, and the letters come from a five-by-seven bitmap font. Run
it again only if the colours change; the files are committed.
"""

import struct
import zlib
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

ACCENT = (0x6E, 0xA8, 0xFE)
INK = (0x0B, 0x10, 0x20)

# Five columns by seven rows, as in the brand mark in the navigation bar.
GLYPHS = {
    "M": [
        "X...X",
        "XX.XX",
        "X.X.X",
        "X.X.X",
        "X...X",
        "X...X",
        "X...X",
    ],
    "F": [
        "XXXXX",
        "X....",
        "X....",
        "XXXX.",
        "X....",
        "X....",
        "X....",
    ],
}


def icon_pixels(size, text="MF"):
    """Rows of (r, g, b, a) for a `size` square icon."""
    radius = round(size * 0.2)
    columns = len(text) * 5 + (len(text) - 1)  # one blank column between letters
    scale = max(1, int(min(size * 0.62 / columns, size * 0.5 / 7)))
    glyph_w, glyph_h = columns * scale, 7 * scale
    left, top = (size - glyph_w) // 2, (size - glyph_h) // 2

    lit = set()
    for index, letter in enumerate(text):
        for row, line in enumerate(GLYPHS[letter]):
            for col, cell in enumerate(line):
                if cell == "X":
                    lit.add((index * 6 + col, row))

    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            # Rounded corners: outside the corner circles is transparent.
            cx = radius if x < radius else size - 1 - radius if x >= size - radius else x
            cy = radius if y < radius else size - 1 - radius if y >= size - radius else y
            if (x - cx) ** 2 + (y - cy) ** 2 > radius**2:
                row.append((0, 0, 0, 0))
                continue
            gx, gy = (x - left) // scale, (y - top) // scale
            if 0 <= x - left < glyph_w and 0 <= y - top < glyph_h and (gx, gy) in lit:
                row.append((*INK, 255))
            else:
                row.append((*ACCENT, 255))
        rows.append(row)
    return rows


def png_bytes(rows):
    """Encode rows of RGBA tuples as a PNG."""
    height, width = len(rows), len(rows[0])

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(value for pixel in row for value in pixel) for row in rows)
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


class Command(BaseCommand):
    help = "Write the app icons (static/icons/icon-192.png and icon-512.png)."

    def handle(self, *args, **options):
        folder = Path(settings.BASE_DIR) / "static" / "icons"
        folder.mkdir(parents=True, exist_ok=True)
        for size in (192, 512):
            path = folder / f"icon-{size}.png"
            path.write_bytes(png_bytes(icon_pixels(size)))
            self.stdout.write(f"wrote {path.relative_to(settings.BASE_DIR)}")
