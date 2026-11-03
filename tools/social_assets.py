"""Generate the GS Allude / Guía Salud organic-social image set from the site's own palette.

Why a generator instead of three PNGs in a folder: the profile picture, the Facebook cover and the
story-highlight tiles must never drift from the site's wordmark colours (`#123B33` pine, `#FBF5EA`
cream, `#EE5A3C` coral — the same three values as `static/favicon.svg` and `<meta name="theme-color">`).
Re-run this after any brand change and the whole set matches again.

Usage
    python tools/social_assets.py                 # writes out/social_kit/images/
    python tools/social_assets.py --out DIR

Fonts: Fraunces/Figtree ship as woff2 (PIL cannot read woff2), so the closest system serif/sans
(Georgia Bold, Segoe UI Semibold) stand in. The wordmark shape, colour and casing are what carry the
brand at the 110px size a profile picture is actually seen at.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

PINE = (18, 59, 51)
CREAM = (251, 245, 234)
CORAL = (238, 90, 60)
# slightly lifted pine for the cover, so the cream text has room without a second hue
PINE_SOFT = (24, 71, 61)

SERIF_BOLD = "/Windows/Fonts/georgiab.ttf"
SANS = "/Windows/Fonts/segoeui.ttf"
SANS_SEMI = "/Windows/Fonts/segoeuib.ttf"


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _text(d: ImageDraw.ImageDraw, xy, s: str, font, fill, anchor="mm", tracking=0):
    """Draw text with optional letterspacing (PIL has no native tracking)."""
    if not tracking:
        d.text(xy, s, font=font, fill=fill, anchor=anchor)
        return
    widths = [d.textlength(ch, font=font) for ch in s]
    total = sum(widths) + tracking * (len(s) - 1)
    x, y = xy
    if anchor.startswith("l"):          # left-aligned: start at x
        pass
    elif anchor.startswith("r"):        # right-aligned: end at x
        x -= total
    else:
        x -= total / 2
    for ch, w in zip(s, widths):
        d.text((x, y), ch, font=font, fill=fill, anchor="lm")
        x += w + tracking


def _mark(d: ImageDraw.ImageDraw, cx: int, cy: int, h: int, ink=CREAM, accent=CORAL):
    """The favicon mark: a rounded cream 'pillar' with a coral flag leaning to the right."""
    w = int(h * 0.46)
    d.rounded_rectangle([cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2], radius=int(w * 0.3), fill=ink)
    flag_h = int(h * 0.42)
    flag_w = int(h * 0.3)
    flag = Image.new("RGBA", (flag_w, flag_h), (0, 0, 0, 0))
    fd = ImageDraw.Draw(flag)
    fd.rounded_rectangle([0, 0, flag_w - 1, flag_h - 1], radius=int(flag_w * 0.45), fill=accent)
    flag = flag.rotate(24, expand=True, resample=Image.BICUBIC)
    px = cx + int(h * 0.06)
    py = cy - int(h * 0.62)
    d._image.paste(flag, (px, py), flag)


def profile_square(size: int = 1080) -> Image.Image:
    """Instagram / TikTok profile picture: mark + two-line wordmark, readable as a 110px circle."""
    img = Image.new("RGB", (size, size), PINE)
    d = ImageDraw.Draw(img)
    _mark(d, size // 2, int(size * 0.245), int(size * 0.19))
    _text(d, (size // 2, int(size * 0.545)), "Guía", _font(SERIF_BOLD, int(size * 0.215)), CREAM)
    _text(d, (size // 2, int(size * 0.735)), "Salud", _font(SERIF_BOLD, int(size * 0.215)), CREAM)
    return img


def cover(width: int = 1640, height: int = 624) -> Image.Image:
    """Facebook cover (1640x624 renders 820x312 on desktop, 640x360 on phones)."""
    img = Image.new("RGB", (width, height), PINE_SOFT)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width, height], fill=PINE_SOFT)
    # a wide coral rule on the left edge, mirroring the wordmark's flag
    d.rectangle([0, 0, int(width * 0.012), height], fill=CORAL)
    _mark(d, int(width * 0.095), int(height * 0.30), int(height * 0.26))
    _text(d, (int(width * 0.175), int(height * 0.42)), "Guía Salud", _font(SERIF_BOLD, int(height * 0.20)),
          CREAM, anchor="lm")
    _text(d, (int(width * 0.178), int(height * 0.60)), "Entiende tu cobertura médica. Gratis y sin compromiso.",
          _font(SANS, int(height * 0.055)), CREAM, anchor="lm")
    _text(d, (int(width * 0.178), int(height * 0.71)), "Understand your health coverage — free, no obligation.",
          _font(SANS, int(height * 0.049)), (168, 202, 188), anchor="lm")
    _text(d, (int(width * 0.178), int(height * 0.845)), "NO SOMOS UNA ASEGURADORA NI EL GOBIERNO",
          _font(SANS_SEMI, int(height * 0.036)), (205, 226, 215), anchor="lm", tracking=2)
    return img


def highlight(label: str, size: int = 1080) -> Image.Image:
    """Story-highlight cover tile. IG crops the centre circle, so the label sits mid-frame."""
    img = Image.new("RGB", (size, size), PINE)
    d = ImageDraw.Draw(img)
    d.ellipse([size * 0.14, size * 0.14, size * 0.86, size * 0.86], outline=(40, 92, 80), width=int(size * 0.012))
    _text(d, (size // 2, int(size * 0.46)), label, _font(SANS_SEMI, int(size * 0.085)), CREAM)
    _text(d, (size // 2, int(size * 0.57)), "GUÍA SALUD", _font(SANS_SEMI, int(size * 0.030)),
          (150, 190, 175), tracking=int(size * 0.007))
    return img


HIGHLIGHTS = ["Cobertura", "Costos", "Inscripción", "Despidos", "Ayuda gratis"]


def main() -> None:
    ap = argparse.ArgumentParser(description="Render the social image set from the site palette.")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "out" / "social_kit" / "images"))
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    profile_square().save(out / "profile_1080.png", optimize=True)
    cover().save(out / "facebook_cover_1640x624.png", optimize=True)
    for i, label in enumerate(HIGHLIGHTS, 1):
        highlight(label).save(out / f"highlight_{i}_{label.lower().replace(' ', '_')}.png", optimize=True)
    for p in sorted(out.glob("*.png")):
        print(f"{p.relative_to(out.parent.parent.parent)}  {Image.open(p).size}")


if __name__ == "__main__":
    main()
