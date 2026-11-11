"""Draw the dashboard's icons: the SVG favicon and the PNGs of the web app manifest.

The mark is the dashboard's "logo" glyph (three nodes joined like a small tree) in white on
the accent colour, made bolder so it still reads at 16 px. One description of it below feeds
both the SVG and a small rasteriser (signed distances, one sample per pixel), so the files
need nothing beyond the standard library and come out byte-identical on every run:

    uv run python scripts/make_icons.py [OUT_DIR]

OUT_DIR defaults to src/orchestrator/web/static; the files there are committed. Development
only: the package never runs this.
"""

from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "orchestrator" / "web" / "static"

# The mark in a 24-unit square (the size of the dashboard's glyphs).
ACCENT = (0x50, 0x46, 0xE5)  # --accent of the light theme
GLYPH = (0xFF, 0xFF, 0xFF)
SIDE = 24.0
CORNER = 5.5  # about the corner of a macOS app icon (22.5 % of its side)
NODE = 2.7  # radius of the three nodes
STROKE = 2.4  # width of the lines joining them
TOP, LEFT, RIGHT, JOINT = (12.0, 5.5), (5.5, 18.5), (18.5, 18.5), (12.0, 12.0)
NODES = (TOP, LEFT, RIGHT)
LINES = ((TOP, JOINT), (JOINT, LEFT), (JOINT, RIGHT))

# name -> (pixels, share of the side the coloured square takes, share the glyph's 24-unit
# square takes, corners rounded). "any" icons keep a margin like the system's own app icons;
# the touch and maskable icons fill the image (the platform cuts the shape). A maskable icon
# may be cut to its safe zone, a centred circle of 40 % of the side: its glyph reaches 35 %.
PNGS = {
    "icon-192.png": (192, 0.8, 0.8, True),
    "icon-512.png": (512, 0.8, 0.8, True),
    "icon-maskable-512.png": (512, 1.0, 0.7, False),
    "icon-apple-180.png": (180, 1.0, 0.8, False),
}


def hex_colour(rgb: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{c:02x}" for c in rgb)


def svg() -> str:
    """The mark as a favicon: the rounded square filling the view box."""
    (tx, ty), (jx, jy) = TOP, JOINT
    path = f"M{tx:g} {ty:g}V{jy:g}" + "".join(
        f"M{jx:g} {jy:g}L{x:g} {y:g}" for x, y in (LEFT, RIGHT)
    )
    glyph = hex_colour(GLYPH)
    nodes = "".join(f'<circle cx="{x:g}" cy="{y:g}" r="{NODE:g}"/>' for x, y in NODES)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIDE:g} {SIDE:g}">'
        f'<rect width="{SIDE:g}" height="{SIDE:g}" rx="{CORNER:g}" fill="{hex_colour(ACCENT)}"/>'
        f'<path d="{path}" fill="none" stroke="{glyph}" stroke-width="{STROKE:g}"'
        f' stroke-linecap="round"/><g fill="{glyph}">{nodes}</g></svg>\n'
    )


def box_distance(x: float, y: float, half: float, corner: float) -> float:
    """Signed distance from a square of half-side `half` centred on 0, corners `corner`."""
    qx, qy = abs(x) - half + corner, abs(y) - half + corner
    outside = math.sqrt(max(qx, 0.0) ** 2 + max(qy, 0.0) ** 2)
    return outside + min(max(qx, qy), 0.0) - corner


def segment_distance(x: float, y: float, a: tuple[float, float], b: tuple[float, float]) -> float:
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
    px, py = x - ax - t * dx, y - ay - t * dy
    return math.sqrt(px * px + py * py)


def glyph_distance(x: float, y: float) -> float:
    """Signed distance in units from the glyph, in the 24-unit square."""
    nodes = min(math.sqrt((x - cx) ** 2 + (y - cy) ** 2) - NODE for cx, cy in NODES)
    lines = min(segment_distance(x, y, a, b) for a, b in LINES) - STROKE / 2
    return min(nodes, lines)


def coverage(distance_px: float) -> float:
    return max(0.0, min(1.0, 0.5 - distance_px))


def raster(size: int, share: float, glyph: float, rounded: bool) -> tuple[list[bytes], bool]:
    """The image's rows (RGBA, or RGB when every pixel is opaque) and whether it has alpha."""
    unit = size * glyph / SIDE  # pixels per unit of the glyph
    square = size * share / 2  # half-side of the coloured square in pixels
    corner = CORNER * size * share / SIDE if rounded else 0.0
    centre = size / 2
    alpha = share < 1.0 or rounded
    rows = []
    for row in range(size):
        y = row + 0.5 - centre
        line = bytearray()
        for column in range(size):
            x = column + 0.5 - centre
            ground = coverage(box_distance(x, y, square, corner)) if alpha else 1.0
            mark = coverage(glyph_distance(x / unit + SIDE / 2, y / unit + SIDE / 2) * unit)
            pixel = [round(a + (g - a) * mark) for a, g in zip(ACCENT, GLYPH, strict=True)]
            if alpha:
                pixel.append(round(255 * ground))
            line.extend(pixel)
        rows.append(bytes(line))
    return rows, alpha


def png(size: int, rows: list[bytes], alpha: bool) -> bytes:
    """A minimal PNG: IHDR, one IDAT, IEND; no text or time chunks."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 6 if alpha else 2, 0, 0, 0)
    raw = b"".join(b"\x00" + row for row in rows)  # filter type 0 (none) on every row
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def write_all(out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    written = [out / "icon.svg"]
    written[0].write_text(svg(), encoding="utf-8")
    for name, (size, share, glyph, rounded) in PNGS.items():
        rows, alpha = raster(size, share, glyph, rounded)
        (out / name).write_bytes(png(size, rows, alpha))
        written.append(out / name)
    return written


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else STATIC
    for path in write_all(out):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
