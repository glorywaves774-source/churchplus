"""Draw the installable-app icons (PNG) from the ChurchPlus mark: navy-to-purple tile, gold cross.

    python scripts/build_app_icons.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "static" / "images" / "icons"
TOP, BOTTOM, GOLD = (27, 44, 94), (91, 63, 209), (201, 162, 39)


def gradient(size: int) -> Image.Image:
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * (size - 1))
            px[x, y] = tuple(round(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3))
    return img


def draw(size: int, *, rounded: bool, inset: float) -> Image.Image:
    scale = 4                                    # draw large, shrink for smooth edges
    big = size * scale
    tile = gradient(big)
    if rounded:
        mask = Image.new("L", (big, big), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, big - 1, big - 1], radius=int(big * 0.28), fill=255)
        out = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        out.paste(tile, (0, 0), mask)
    else:
        out = tile.convert("RGBA")
    d = ImageDraw.Draw(out)
    c = big / 2
    span = big * (1 - 2 * inset)                # cross size inside the safe area
    w = int(span * 0.14)
    d.rounded_rectangle([c - w / 2, c - span * 0.42, c + w / 2, c + span * 0.42], radius=w // 2, fill=GOLD)
    d.rounded_rectangle([c - span * 0.30, c - span * 0.16 - w / 2, c + span * 0.30, c - span * 0.16 + w / 2], radius=w // 2, fill=GOLD)
    return out.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    draw(192, rounded=True, inset=0.12).save(OUT / "icon-192.png", optimize=True)
    draw(512, rounded=True, inset=0.12).save(OUT / "icon-512.png", optimize=True)
    draw(512, rounded=False, inset=0.22).save(OUT / "maskable-512.png", optimize=True)     # Android adaptive (safe zone)
    draw(180, rounded=False, inset=0.16).convert("RGB").save(OUT / "apple-touch-icon.png", optimize=True)
    draw(32, rounded=True, inset=0.08).save(OUT / "favicon-32.png", optimize=True)
    print("Icons written to", OUT)


if __name__ == "__main__":
    main()
