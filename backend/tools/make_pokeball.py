"""Draws a Pokeball as a PNG and prints it as a base64 data URI.

`app/images.py` accepts only raster data URIs, so the seed file cannot carry an
SVG. This script writes the ball with `zlib` alone, so the seed's cover art
needs no image library and no build step.

Run it, then paste `pokeball_uri.txt` into the seed file's `image` field:

    python make_pokeball.py
"""
import base64
import math
import struct
import zlib

SIZE = 192          # Drawn at `SCALE` times this size, then averaged down.
SCALE = 4
N = SIZE * SCALE

RED = (238, 27, 44)
WHITE = (247, 247, 247)
BLACK = (26, 26, 26)


def sample(x: float, y: float):
    """Returns the `(r, g, b, a)` of the ball at one point of the canvas."""
    cx = cy = N / 2
    dx, dy = x - cx, y - cy
    dist = math.hypot(dx, dy)
    radius = N / 2 - N * 0.02

    if dist > radius:
        return (0, 0, 0, 0)
    if dist > radius - N * 0.05:           # Outer rim.
        return (*BLACK, 255)
    if dist < N * 0.10:                    # Center button.
        return (*WHITE, 255)
    if dist < N * 0.15:                    # Ring around the button.
        return (*BLACK, 255)
    if abs(dy) < N * 0.04:                 # Belt across the middle.
        return (*BLACK, 255)
    return (*(RED if dy < 0 else WHITE), 255)


def render():
    """Returns `SIZE` rows of `SIZE` RGBA pixels, box filtered from `SCALE`."""
    rows = []
    for py in range(SIZE):
        row = bytearray()
        for px in range(SIZE):
            r = g = b = a = 0
            for sy in range(SCALE):
                for sx in range(SCALE):
                    pr, pg, pb, pa = sample(px * SCALE + sx + 0.5, py * SCALE + sy + 0.5)
                    # Premultiply, so a transparent sample adds no color.
                    r += pr * pa
                    g += pg * pa
                    b += pb * pa
                    a += pa
            if a:
                row += bytes((r // a, g // a, b // a, a // (SCALE * SCALE)))
            else:
                row += b"\0\0\0\0"
        rows.append(bytes(row))
    return rows


def png(rows) -> bytes:
    raw = b"".join(b"\0" + r for r in rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


if __name__ == "__main__":
    data = png(render())
    uri = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
    with open("pokeball.png", "wb") as fh:
        fh.write(data)
    with open("pokeball_uri.txt", "w", encoding="utf-8") as fh:
        fh.write(uri)
    print(len(data), "bytes;", len(uri), "chars")
