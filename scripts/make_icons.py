#!/usr/bin/env python3
"""Generate the extension icons as PNGs (pure stdlib, no Pillow needed).

Draws the hub glyph: a central node with three satellites, on a rounded
dark square.  Supersampled 4x for smooth edges.

    python3 scripts/make_icons.py
"""

from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "extension" / "icons"

BG_TOP = (26, 32, 44)
BG_BOTTOM = (12, 15, 20)
ACCENT = (43, 127, 224)
NODE = (116, 192, 252)
LINE = (61, 140, 232)


def rounded_square(x: float, y: float, size: float, radius: float) -> bool:
    if x < 0 or y < 0 or x > size or y > size:
        return False
    cx = min(max(x, radius), size - radius)
    cy = min(max(y, radius), size - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius**2


def dist(ax, ay, bx, by) -> float:
    return math.hypot(ax - bx, ay - by)


def point_segment_distance(px, py, ax, ay, bx, by) -> float:
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    length_sq = vx * vx + vy * vy
    t = 0.0 if length_sq == 0 else max(0.0, min(1.0, (wx * vx + wy * vy) / length_sq))
    return dist(px, py, ax + t * vx, ay + t * vy)


def render(size: int) -> bytes:
    scale = 4
    big = size * scale
    radius = big * 0.22
    center = big / 2
    orbit = big * 0.30
    node_r = big * 0.085
    core_r = big * 0.135
    line_w = big * 0.035
    satellites = [
        (center + orbit * math.cos(angle), center + orbit * math.sin(angle))
        for angle in (-math.pi / 2, math.pi / 6, math.pi * 5 / 6)
    ]

    rows = []
    for y in range(size):
        row = bytearray([0])  # filter type 0
        for x in range(size):
            r_total = g_total = b_total = a_total = 0
            for sy in range(scale):
                for sx in range(scale):
                    px = x * scale + sx + 0.5
                    py = y * scale + sy + 0.5
                    if not rounded_square(px, py, big, radius):
                        continue
                    t = py / big
                    color = tuple(
                        int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)
                    )
                    on_line = any(
                        point_segment_distance(px, py, center, center, nx, ny) <= line_w
                        for nx, ny in satellites
                    )
                    if on_line:
                        color = LINE
                    if any(dist(px, py, nx, ny) <= node_r for nx, ny in satellites):
                        color = NODE
                    if dist(px, py, center, center) <= core_r:
                        color = ACCENT
                    if dist(px, py, center, center) <= core_r * 0.45:
                        color = (233, 236, 239)
                    r_total += color[0]
                    g_total += color[1]
                    b_total += color[2]
                    a_total += 255
            samples = scale * scale
            row += bytes(
                (r_total // samples, g_total // samples, b_total // samples, a_total // samples)
            )
        rows.append(bytes(row))

    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (16, 32, 48, 96, 128):
        path = OUT / f"icon-{size}.png"
        path.write_bytes(render(size))
        print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
