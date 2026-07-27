"""Build the 1280x640 GitHub social preview card from the real mark.

Uses tools/make_icon.glyph so the card cannot drift from the app icon.
GitHub spec: PNG/JPG/GIF, under 1 MB, at least 640x320, 1280x640 preferred.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from PIL import Image, ImageDraw, ImageFont
import make_icon

W, H = 1280, 640
BG, FG = make_icon.BG, make_icon.FG
MUTED = (150, 148, 143)

card = Image.new("RGB", (W, H), BG)


def font(size, bold=False):
    for p in ([
        "/System/Library/Fonts/SFNSDisplay.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ] if not bold else [
        "/System/Library/Fonts/SFNSDisplay.ttf",
        "/System/Library/Fonts/Supplemental/Helvetica.ttc",
    ]):
        try:
            return ImageFont.truetype(p, size, index=1 if bold and p.endswith("ttc") else 0)
        except Exception:
            continue
    return ImageFont.load_default()


# The mark, drawn at high res then downsampled so the strokes stay clean.
MARK = 300
mark = make_icon.glyph(MARK * 3, FG + (255,), fit=0.92).resize(
    (MARK, MARK), Image.LANCZOS)
card.paste(mark, (110, (H - MARK) // 2), mark)

d = ImageDraw.Draw(card)
x = 110 + MARK + 78

name_f = font(104, bold=True)
d.text((x, 232), "Compartment", font=name_f, fill=FG)

sub_f = font(40)
d.text((x, 366), "Memory that arrives full.", font=sub_f, fill=FG)

line_f = font(31)
d.text((x, 424), "6,718 facts pre-loaded. Encrypted. Offline.",
       font=line_f, fill=MUTED)
d.text((x, 466), "On your machine, for Claude Code and any MCP client.",
       font=line_f, fill=MUTED)

out = pathlib.Path(__file__).resolve().parent.parent / "docs/images/social-preview.png"
print(f"wrote {out}  {card.size[0]}x{card.size[1]}  "
      f"{out.stat().st_size/1024:.0f} KB")
