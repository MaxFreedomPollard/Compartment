"""Draw Compartment's mark and build the .icns and the menu bar template.

The mark is a single letter: Phoenician **resh**, the ancestor of R. Resh
means "head", which is the whole product in one glyph - and its form, a
closed bowl feeding a descending stroke, happens to read as a neuron. Compartment
capitalises the R for the same reason.

It is drawn rather than typeset, because no Phoenician font can be relied on
to exist on a build machine, and drawing it means the curve, the stroke
weight and the join can be tuned for a 16-pixel icon instead of hoping a
display face survives being shrunk that far.

One symbol, monoline, two flat colours. No gradient ramp, no primary
palette, no second element competing with the letter.

    python tools/make_icon.py [OUT.icns]
"""
from __future__ import annotations

import math
import pathlib
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw

SIZES = [16, 32, 64, 128, 256, 512, 1024]
BG = (14, 15, 18)               # near-black, very slightly cool
FG = (240, 234, 224)            # warm bone - archaic, not clinical white
CORNER = 0.225                  # macOS squircle, as a share of the side

# The letterform, in a unit square with y running downward. Kept as data so
# the proportions can be read and adjusted without touching drawing code.
HEAD_C = (0.505, 0.340)         # centre of the bowl
HEAD_R = (0.198, 0.186)         # its radii - a touch wider than tall
STEM_TOP = (0.352, 0.486)       # where the stem leaves the bowl, lower left
STEM_BOT = (0.309, 0.848)       # and where it ends, leaning slightly left
STROKE = 0.112                  # monoline weight


def _rounded_mask(size: int) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(size * CORNER), fill=255)
    return m


def _resh(d: ImageDraw.ImageDraw, S: float, colour: tuple) -> None:
    """Stroke the glyph at scale S with a round-capped monoline."""
    w = STROKE * S
    cx, cy = HEAD_C[0] * S, HEAD_C[1] * S
    rx, ry = HEAD_R[0] * S, HEAD_R[1] * S

    # The bowl. PIL has no round join, so the ring is laid down as a run of
    # discs along the ellipse - which also gives it genuinely round ends.
    steps = max(180, int(S))
    for i in range(steps):
        a = 2 * math.pi * i / steps
        x, y = cx + rx * math.cos(a), cy + ry * math.sin(a)
        d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=colour)

    # The descender, drawn the same way so the join into the bowl is seamless.
    x0, y0 = STEM_TOP[0] * S, STEM_TOP[1] * S
    x1, y1 = STEM_BOT[0] * S, STEM_BOT[1] * S
    span = max(2, int(math.hypot(x1 - x0, y1 - y0)))
    for i in range(span + 1):
        t = i / span
        x, y = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
        d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=colour)


def draw(size: int) -> Image.Image:
    """The full app icon: the mark on its near-black squircle."""
    S = max(size * 4, 512)                      # draw big, downsample sharp
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    plate = Image.new("RGBA", (S, S), BG + (255,))
    img.paste(plate, (0, 0), _rounded_mask(S))
    _resh(ImageDraw.Draw(img), S, FG + (255,))
    return img.resize((size, size), Image.LANCZOS)


def draw_template(size: int) -> Image.Image:
    """The menu bar mark: glyph only, opaque black on transparent.

    A template image carries no colour of its own - macOS tints it for the
    light or dark menu bar and inverts it when the item is highlighted, so
    the icon matches every other icon up there instead of fighting them.
    """
    S = size * 8
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    _resh(ImageDraw.Draw(img), S, (0, 0, 0, 255))
    return img.resize((size, size), Image.LANCZOS)


def build_template(out: pathlib.Path, pt: int = 18) -> pathlib.Path:
    """Write the @1x/@2x pair the status item loads."""
    out.parent.mkdir(parents=True, exist_ok=True)
    draw_template(pt).save(out)
    draw_template(pt * 2).save(out.with_name(out.stem + "@2x" + out.suffix))
    return out


def build(out: pathlib.Path) -> pathlib.Path:
    with tempfile.TemporaryDirectory() as td:
        iconset = pathlib.Path(td) / "compartment.iconset"
        iconset.mkdir()
        for s in SIZES:
            if s <= 512:
                draw(s).save(iconset / f"icon_{s}x{s}.png")
            if s >= 32:
                draw(s).save(iconset / f"icon_{s // 2}x{s // 2}@2x.png")
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["iconutil", "-c", "icns", str(iconset),
                        "-o", str(out)], check=True)
    return out


def main() -> int:
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                       else "build/Compartment.icns")
    build(out)
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
    tmpl = build_template(
        pathlib.Path(__file__).resolve().parents[1]
        / "src" / "compartment" / "data" / "menubar.png")
    print(f"wrote {tmpl} and its @2x")
    preview = out.with_suffix(".preview.png")
    draw(512).save(preview)
    print(f"preview {preview}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
