"""Draw engRAM's app icon and build the .icns.

Deliberately geometric: the mark is a sealed ring holding a small constellation
of linked memories. It has to survive being 16 pixels wide in System Settings'
login-items list, so there is no fine detail and no text - only shapes that
stay legible when they are four pixels across.

    python tools/make_icon.py [OUT.icns]
"""
from __future__ import annotations

import math
import pathlib
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw

# macOS icons are drawn inside a rounded square with generous padding; the
# "squircle" corner is ~22% of the side.
SIZES = [16, 32, 64, 128, 256, 512, 1024]
BG_TOP = (58, 41, 122)          # deep indigo
BG_BOTTOM = (26, 20, 58)        # near-black violet
ACCENT = (140, 205, 255)        # cold blue, high contrast on the indigo
NODE = (255, 255, 255)


def _rounded_mask(size: int, radius_ratio: float = 0.225) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1],
                        radius=int(size * radius_ratio), fill=255)
    return m


def _gradient(size: int) -> Image.Image:
    g = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(1, size - 1)
        g.putpixel((0, y), tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t)
                                 for i in range(3)))
    return g.resize((size, size))


def draw(size: int) -> Image.Image:
    # Draw big, then downsample: cheap anti-aliasing that keeps the small
    # sizes clean without hand-hinting each one.
    S = max(size * 4, 256)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(_gradient(S).convert("RGBA"), (0, 0), _rounded_mask(S))
    d = ImageDraw.Draw(img)

    cx = cy = S / 2
    ring_r = S * 0.30
    ring_w = max(2, int(S * 0.055))
    d.ellipse([cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r],
              outline=ACCENT + (255,), width=ring_w)

    # three linked memories inside the ring: a triangle of nodes
    node_r = S * 0.062
    pts = [(cx + ring_r * 0.52 * math.cos(math.radians(a)),
            cy + ring_r * 0.52 * math.sin(math.radians(a)))
           for a in (-90, 30, 150)]
    link_w = max(2, int(S * 0.030))
    for i in range(3):
        d.line([pts[i], pts[(i + 1) % 3]], fill=ACCENT + (230,), width=link_w)
    for (x, y) in pts:
        d.ellipse([x - node_r, y - node_r, x + node_r, y + node_r],
                  fill=NODE + (255,))

    # the seal: a gap in the ring closed by a bar, reading as "locked"
    bar_w, bar_h = S * 0.22, S * 0.075
    d.rounded_rectangle([cx - bar_w / 2, cy + ring_r - bar_h / 2,
                         cx + bar_w / 2, cy + ring_r + bar_h / 2],
                        radius=bar_h / 2, fill=NODE + (255,))
    return img.resize((size, size), Image.LANCZOS)


def build(out: pathlib.Path) -> pathlib.Path:
    with tempfile.TemporaryDirectory() as td:
        iconset = pathlib.Path(td) / "engram.iconset"
        iconset.mkdir()
        for s in SIZES:
            img = draw(s)
            if s <= 512:
                img.save(iconset / f"icon_{s}x{s}.png")
            if s >= 32:
                draw(s).save(iconset / f"icon_{s // 2}x{s // 2}@2x.png")
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["iconutil", "-c", "icns", str(iconset),
                        "-o", str(out)], check=True)
    return out


def main() -> int:
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                       else "build/engRAM.icns")
    build(out)
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
    preview = out.with_suffix(".preview.png")
    draw(512).save(preview)
    print(f"preview {preview}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
