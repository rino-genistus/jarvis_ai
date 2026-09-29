"""
Draws the Jarvis app icon: a cyan HUD reticle — tick-marked outer ring, broken
arcs and a glowing core — on a deep navy rounded square, after the blue
holographic interface JARVIS shows in the films.

Pure numpy, no image libraries. Writes a 1024x1024 PNG; build_app.sh turns it
into Jarvis.icns with sips and iconutil.

    python assets/make_icon.py assets/jarvis_icon.png
"""

import struct
import sys
import zlib

import numpy as np

SIZE = 1024
TAU = 2 * np.pi

# Palette
NAVY_CENTER = np.array([0.055, 0.137, 0.251])    # #0e2340
NAVY_EDGE = np.array([0.016, 0.031, 0.059])      # #04080f
CYAN = np.array([0.227, 0.847, 1.0])             # #3ad8ff
ICE = np.array([0.62, 0.918, 1.0])               # #9eeaff
WHITE_HOT = np.array([0.94, 0.99, 1.0])


def coverage(distance):
    """Anti-aliased coverage from a signed distance in px (negative = inside)."""
    return np.clip(0.5 - distance, 0.0, 1.0)


def rounded_square_sdf(x, y, center, half, radius):
    qx = np.abs(x - center) - half + radius
    qy = np.abs(y - center) - half + radius
    outside = np.hypot(np.maximum(qx, 0), np.maximum(qy, 0))
    inside = np.minimum(np.maximum(qx, qy), 0)
    return outside + inside - radius


def arc(r, ang, radius, width, start_deg, span_deg):
    """Coverage of a ring segment. Angles in degrees, clockwise from 12 o'clock."""
    radial = coverage(np.abs(r - radius) - width / 2)
    a = (ang - np.radians(start_deg)) % TAU
    span = np.radians(span_deg)
    # Signed angular distance to the nearest end: negative inside the arc
    signed = np.where(a <= span, -np.minimum(a, span - a), np.minimum(a - span, TAU - a))
    return radial * coverage(signed * radius)


def ticks(r, ang, inner, outer, count, width):
    """Coverage of `count` radial tick marks between two radii."""
    radial = coverage(np.maximum(inner - r, r - outer))
    phase = ang / TAU * count
    off_centre = np.abs(phase - np.round(phase)) * TAU / count * r      # px from nearest tick
    return radial * coverage(off_centre - width / 2)


def paint(rgb, mask, color, strength=1.0):
    m = (mask * strength)[..., None]
    return rgb * (1 - m) + m * color


def draw():
    y, x = np.mgrid[0:SIZE, 0:SIZE].astype(np.float64) + 0.5
    c = SIZE / 2
    r = np.hypot(x - c, y - c)
    # 0 at 12 o'clock, increasing clockwise (y grows downward)
    ang = (np.arctan2(x - c, -(y - c))) % TAU

    # macOS icon grid: 824 px body with a ~185 px corner radius
    body = coverage(rounded_square_sdf(x, y, c, 412, 185))

    # Background: radial navy, brightest behind the reticle
    t = np.clip(r / 560, 0, 1)[..., None]
    rgb = (1 - t) * NAVY_CENTER + t * NAVY_EDGE

    # Faint glow under the whole reticle
    rgb = rgb + np.exp(-((r - 290) / 90) ** 2)[..., None] * 0.10 * CYAN

    # Outer hairline and tick ring — 72 ticks, every sixth one long
    rgb = paint(rgb, coverage(np.abs(r - 332) - 1.5), CYAN, 0.55)
    rgb = paint(rgb, ticks(r, ang, 345, 362, 72, 3.0), CYAN, 0.45)
    rgb = paint(rgb, ticks(r, ang, 340, 378, 12, 5.0), ICE, 0.85)

    # Main broken ring: three bright arcs, with a soft bloom
    main = sum(arc(r, ang, 290, 24, start, 96) for start in (12, 132, 252))
    bloom = np.exp(-((r - 290) / 30) ** 2) * sum(arc(r, ang, 290, 80, start, 96) for start in (12, 132, 252))
    rgb = rgb + bloom[..., None] * 0.18 * CYAN
    rgb = paint(rgb, main, CYAN)

    # Inner thin arc, open at the lower right, like a gauge
    rgb = paint(rgb, arc(r, ang, 232, 6, 170, 280), ICE, 0.8)

    # Ring hugging the core
    rgb = paint(rgb, coverage(np.abs(r - 158) - 3), CYAN, 0.7)

    # Core: white-hot centre fading to cyan, with a halo
    rgb = rgb + np.exp(-(r / 170) ** 2)[..., None] * 0.35 * CYAN
    heat = np.clip(1 - r / 118, 0, 1)[..., None] ** 0.8
    core_rgb = heat * WHITE_HOT + (1 - heat) * CYAN
    core = coverage(r - 118)[..., None]
    rgb = rgb * (1 - core) + core * core_rgb

    rgba = np.dstack([np.clip(rgb, 0, 1), body])
    return (rgba * 255 + 0.5).astype(np.uint8)


def write_png(path, rgba):
    height, width, _ = rgba.shape
    raw = b"".join(b"\x00" + rgba[row].tobytes() for row in range(height))

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(raw, 9)))
        f.write(chunk(b"IEND", b""))


if __name__ == "__main__":
    write_png(sys.argv[1] if len(sys.argv) > 1 else "jarvis_icon.png", draw())
