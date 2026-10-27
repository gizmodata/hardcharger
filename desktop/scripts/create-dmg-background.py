"""Render the macOS installer window's background: an arrow from the app to Applications.

Rebuild (after `npm ci` in desktop/, for the Inter font the app itself uses):
  uv run --no-project --with pillow python desktop/scripts/create-dmg-background.py
It writes dmg-background.png (660x400) and dmg-background@2x.png; dmgbuild combines the two
into one Retina TIFF. The geometry must match create-dmg.py: a 660x400 window with 104-point
icons centred at (180, 180) and (480, 180).
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


root = Path(__file__).resolve().parents[2]
out = root / "desktop/src-tauri/installer"
font_file = root / "desktop/node_modules/@fontsource-variable/inter/files/inter-latin-wght-normal.woff2"
if not font_file.is_file():
    raise SystemExit(f"{font_file} is missing: run `npm ci` in desktop/ first")

WIDTH, HEIGHT = 660, 400
APP_X, APPLICATIONS_X, ICON_Y, ICON = 180, 480, 180, 104
BACKGROUND = (245, 250, 255)  # the window colour the installer used before it had a picture
NAVY, BLUE, MUTED = (7, 24, 59), (0, 95, 243), (84, 99, 128)


def font(size: int, weight: int) -> ImageFont.FreeTypeFont:
    face = ImageFont.truetype(str(font_file), size)
    face.set_variation_by_axes([weight])
    return face


def render(scale: int) -> Image.Image:
    image = Image.new("RGB", (WIDTH * scale, HEIGHT * scale), BACKGROUND)
    draw = ImageDraw.Draw(image)

    def s(value: float) -> int:
        return round(value * scale)

    # The arrow runs between the two icons, clear of both and of their labels.
    start, end = APP_X + ICON / 2 + 26, APPLICATIONS_X - ICON / 2 - 26
    head = 22
    draw.line((s(start), s(ICON_Y), s(end - head + 4), s(ICON_Y)), fill=BLUE, width=s(7))
    draw.ellipse((s(start - 3.5), s(ICON_Y - 3.5), s(start + 3.5), s(ICON_Y + 3.5)), fill=BLUE)
    draw.polygon([(s(end), s(ICON_Y)), (s(end - head), s(ICON_Y - 15)), (s(end - head), s(ICON_Y + 15))], fill=BLUE)

    for text, size, weight, colour, y in (
        ("Drag Hardcharger to Applications", 20, 650, NAVY, 300),
        ("Then open it from your Applications folder or Launchpad.", 13, 450, MUTED, 332),
    ):
        face = font(s(size), weight)
        width = draw.textlength(text, font=face)
        draw.text(((image.width - width) / 2, s(y)), text, font=face, fill=colour)
    return image


out.mkdir(parents=True, exist_ok=True)
for scale, name in ((1, "dmg-background.png"), (2, "dmg-background@2x.png")):
    image = render(scale)
    # tiffutil -cathidpicheck pairs the two by resolution: 72 and 144 dpi.
    image.save(out / name, dpi=(72 * scale, 72 * scale), optimize=True)
    print(f"{name}: {image.width}x{image.height}")
