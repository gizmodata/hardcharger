"""Render WiX's fixed-size bitmaps from the existing Hardcharger brand assets.

Rebuild: uv run --no-project --with pillow python desktop/scripts/create-installer-artwork.py
The right-hand part of the welcome image stays white for WiX's dialog text.
"""

from pathlib import Path

from PIL import Image, ImageDraw


root = Path(__file__).resolve().parents[2]
out = root / "desktop/src-tauri/installer"
out.mkdir(parents=True, exist_ok=True)
head = Image.open(root / "docs/assets/brand/standalone/head/hardcharger-head-512.png").convert("RGBA")
wordmark = Image.open(root / "docs/assets/brand/wordmark/hardcharger-dark.png").convert("RGBA")

dialog = Image.new("RGB", (493, 312), "white")
draw = ImageDraw.Draw(dialog)
top, bottom = (7, 24, 59), (0, 95, 243)
for y in range(312):
    color = tuple(round(a + (b - a) * y / 311) for a, b in zip(top, bottom))
    draw.line((0, y, 163, y), fill=color)
draw.rounded_rectangle((26, 64, 138, 176), radius=22, fill="white")
mark = head.resize((100, 100), Image.Resampling.LANCZOS)
dialog.paste(mark, (32, 70), mark)
letters = wordmark.resize((132, 22), Image.Resampling.LANCZOS)
dialog.paste(letters, (16, 210), letters)
draw.rounded_rectangle((54, 252, 110, 255), radius=2, fill=(84, 190, 255))

banner = Image.new("RGB", (493, 58), "white")
mark = head.resize((44, 44), Image.Resampling.LANCZOS)
banner.paste(mark, (436, 6), mark)
ImageDraw.Draw(banner).rectangle((0, 56, 492, 57), fill=(0, 95, 243))
for name, artwork in (("dialog", dialog), ("banner", banner)):
    # WiX requires Windows BMPs; RGB produces portable, uncompressed 24-bit files.
    artwork.save(out / f"{name}.bmp")
    artwork.save(out / f"{name}.png")
    print(f"{name}: {artwork.width}x{artwork.height}, 24-bit BMP")
