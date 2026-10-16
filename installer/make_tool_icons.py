"""Generates each tool's own app icon in Logo/tools/ (<tool>.ico for Windows
taskbar/exe icons, <tool>.png for macOS) from the glyph its launcher tile
uses, drawn as a white silhouette on a rounded brand-colored square - this
matches the launcher's line-art tiles and stays legible at 24px, where the
full-color emoji versions lost contrast against the purple.

One-time/occasional build helper, not part of the app: needs Pillow and
Windows' Segoe UI Emoji font, so run it on Windows and commit the output.
To use custom artwork for a tool instead, replace its files in Logo/tools/.

    python installer/make_tool_icons.py
"""

import os
import sys

from PIL import Image, ImageDraw, ImageFont

PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_DIR)

from pandora_home import TOOL_ICONS  # noqa: E402
from pandora_theme import light_palette  # noqa: E402

OUT_DIR = os.path.join(PROJECT_DIR, "Logo", "tools")
EMOJI_FONT = r"C:\Windows\Fonts\seguiemj.ttf"
CANVAS = 1024
CORNER_RADIUS = int(CANVAS * 0.22)
GLYPH_SIZE = int(CANVAS * 0.64)
ICO_SIZES = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]
PNG_SIZE = 512


def render(glyph: str) -> Image.Image:
    image = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, CANVAS - 1, CANVAS - 1), radius=CORNER_RADIUS, fill=light_palette.accent)

    font = ImageFont.truetype(EMOJI_FONT, GLYPH_SIZE)
    glyph = glyph.replace("\ufe0f", "")  # drop emoji variation selectors
    # Center on the glyph's actual ink, not its character cell - each emoji
    # pads its cell differently (the launcher needs hand-tuned offsets for
    # exactly this reason).
    left, top, right, bottom = draw.textbbox((0, 0), glyph, font=font)
    x = (CANVAS - (right - left)) / 2 - left
    y = (CANVAS - (bottom - top)) / 2 - top
    draw.text((x, y), glyph, font=font, fill="white")
    return image


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    for script, glyph in TOOL_ICONS.items():
        tool = os.path.splitext(script)[0]
        image = render(glyph)
        image.save(os.path.join(OUT_DIR, f"{tool}.ico"), sizes=ICO_SIZES)
        image.resize((PNG_SIZE, PNG_SIZE), Image.LANCZOS).save(os.path.join(OUT_DIR, f"{tool}.png"))
        print(f"wrote {tool}.ico / {tool}.png")


if __name__ == "__main__":
    main()
