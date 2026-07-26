"""Draw Compartment's mark and build the .icns and the menu bar template.

The mark is a single letter: Phoenician **heth**, which means an enclosure -
a walled courtyard. That is the product in one glyph. Its form is two posts
crossed by two rungs, so it also reads as what it names: cells, divided and
sealed off from one another.

It is drawn rather than typeset, because no Phoenician font can be relied on
to exist on a build machine, and drawing it means the stroke weight and the
proportions can be tuned for a 16-pixel icon instead of hoping a display face
survives being shrunk that far.

The glyph is laid out on its own ink and then optically centred, so it always
sits square in the plate at every size. Drawing straight into a fixed box is
how the first attempt at this ended up overflowing its own icon.

One symbol, monoline, two flat colours. No gradient ramp, no primary palette,
no second element competing with the letter.

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
STROKE = 0.088                  # monoline weight
FIT = 0.60                      # how much of the plate the glyph occupies

# Phoenician heth, as polylines in a unit square with y running downward:
# two posts, two rungs.
HETH = [
    [(0.30, 0.18), (0.30, 0.82)],
    [(0.70, 0.18), (0.70, 0.82)],
    [(0.30, 0.38), (0.70, 0.38)],
    [(0.30, 0.62), (0.70, 0.62)],
]


def _rounded_mask(size: int) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(size * CORNER), fill=255)
    return m


def _stroke(d: ImageDraw.ImageDraw, S: int, colour: tuple) -> None:
    """Lay the glyph down as overlapping discs.

    PIL has no round cap or round join, and a butt-ended monoline at this
    weight shows every corner. Walking a disc along each segment gives both
    for free, and the overlaps at the junctions fuse cleanly.
    """
    r = STROKE * S / 2
    for path in HETH:
        for (x0, y0), (x1, y1) in zip(path, path[1:]):
            steps = max(2, int(math.hypot(x1 - x0, y1 - y0) * S))
            for i in range(steps + 1):
                t = i / steps
                x, y = (x0 + (x1 - x0) * t) * S, (y0 + (y1 - y0) * t) * S
                d.ellipse([x - r, y - r, x + r, y + r], fill=colour)


def glyph(size: int, colour: tuple, fit: float = FIT) -> Image.Image:
    """The mark alone, scaled to `fit` and centred on its own ink."""
    S = max(size * 4, 512)
    raw = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    _stroke(ImageDraw.Draw(raw), S, colour)
    box = raw.getbbox()
    w, h = box[2] - box[0], box[3] - box[1]
    k = fit * S / max(w, h)
    inked = raw.crop(box).resize((max(1, int(w * k)), max(1, int(h * k))),
                                 Image.LANCZOS)
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(inked, ((S - inked.width) // 2, (S - inked.height) // 2), inked)
    return out.resize((size, size), Image.LANCZOS)


def draw(size: int) -> Image.Image:
    """The app icon: the mark on its near-black squircle."""
    S = max(size * 4, 512)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(Image.new("RGBA", (S, S), BG + (255,)), (0, 0), _rounded_mask(S))
    img.alpha_composite(glyph(S, FG + (255,)))
    return img.resize((size, size), Image.LANCZOS)


def draw_template(size: int) -> Image.Image:
    """The menu bar mark: glyph only, opaque black on transparent.

    A template image carries no colour of its own - macOS tints it for the
    light or dark menu bar and inverts it while the item is highlighted, so it
    matches every other icon up there instead of fighting them. It also gets a
    little more of its box than the app icon does, because the menu bar
    supplies its own padding.
    """
    return glyph(size, (0, 0, 0, 255), fit=0.74)


def build_template(out: pathlib.Path, pt: int = 18) -> pathlib.Path:
    """Write the @1x/@2x pair the status item loads."""
    out.parent.mkdir(parents=True, exist_ok=True)
    draw_template(pt).save(out)
    draw_template(pt * 2).save(out.with_name(f"{out.stem}@2x{out.suffix}"))
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
    tmpl = build_template(pathlib.Path(__file__).resolve().parents[1]
                          / "src" / "compartment" / "data" / "menubar.png")
    print(f"wrote {tmpl} and its @2x")
    preview = out.with_suffix(".preview.png")
    draw(512).save(preview)
    print(f"preview {preview}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
