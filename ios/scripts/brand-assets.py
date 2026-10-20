#!/usr/bin/env python3
"""Makes the iOS app's brand images from the official artwork, into its asset catalog.

    python3 -m venv /tmp/v && /tmp/v/bin/pip install pillow
    /tmp/v/bin/python ios/scripts/brand-assets.py

Sources (nothing is redrawn; images are only cropped, scaled and, for the app icon,
flattened onto the tile's own background colour):

  docs/assets/brand/  the Hardcharger bull identity v3 (README.md there)
    logos/mascot-stacked-{light,dark}.png   the full charging bull with the lettering
                                            (1600 px raster originals): splash, launch
                                            screen, About
    logos/head-horizontal-{light,dark}.png  the head and lettering: sidebar header
    app-icons/head-{light,dark}/hardcharger-1024.png  the app icon, light and dark
    monochrome/white/hardcharger-1024.png   the tinted app icon (iOS 18)
    standalone/head/hardcharger-head-512.png  the head alone: small marks
  docs/assets/gizmodata-logo.png  the GizmoData mark (gizmodata.com's own logo file)

The kit's tiles have rounded, transparent corners; iOS draws its own mask and fills
transparency with black, so the light and dark icons are flattened onto the colour at the
tile's edge. The tinted icon is the white head on transparency, as iOS tints it.
"""
import json
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
BRAND = ROOT / "docs/assets/brand"
ASSETS = ROOT / "ios/Hardcharger/Resources/Assets.xcassets"

# The brand palette (docs/assets/brand, source/palette.json in the kit).
PALETTE = {"navy": "#061B49", "cobalt": "#0865E8", "azure": "#39A5F8", "paper": "#F6F9FF"}

# The mascot's width on the launch screen and splash, in points (a launch screen shows its
# image at the image's own size, so each scale gets its own file).
MASCOT_POINTS = 260


def crop_to_art(im: Image.Image) -> Image.Image:
    return im.crop(im.getchannel("A").getbbox())


def save_png(im: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, optimize=True)


def contents(path: Path, data: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "Contents.json").write_text(json.dumps(data, indent=2) + "\n")


INFO = {"author": "xcode", "version": 1}
DARK = [{"appearance": "luminosity", "value": "dark"}]


def themed_scaled_imageset(name: str, light: Image.Image, dark: Image.Image, points: int) -> None:
    """An image set with @1x/@2x/@3x files `points` wide, light and dark."""
    folder = ASSETS / f"{name}.imageset"
    images = []
    for variant, im in (("light", light), ("dark", dark)):
        for scale in (1, 2, 3):
            width = points * scale
            height = round(im.height * width / im.width)
            file = f"{name}-{variant}@{scale}x.png"
            save_png(im.resize((width, height), Image.LANCZOS), folder / file)
            entry = {"filename": file, "idiom": "universal", "scale": f"{scale}x"}
            if variant == "dark":
                entry["appearances"] = DARK
            images.append(entry)
    contents(folder, {"images": images, "info": INFO})


def single_imageset(name: str, im: Image.Image, dark: Image.Image | None = None) -> None:
    """A single-scale image set (drawn resizable), optionally with a dark variant."""
    folder = ASSETS / f"{name}.imageset"
    images = [{"filename": f"{name}.png", "idiom": "universal"}]
    save_png(im, folder / f"{name}.png")
    if dark is not None:
        save_png(dark, folder / f"{name}-dark.png")
        images.append({"filename": f"{name}-dark.png", "idiom": "universal", "appearances": DARK})
    contents(folder, {"images": images, "info": INFO, "properties": {"preserves-vector-representation": False}})


def flatten(tile: Image.Image) -> Image.Image:
    """The tile on its own edge colour (the middle of its top edge), opaque."""
    edge = tile.getpixel((tile.width // 2, 12))
    background = Image.new("RGBA", tile.size, edge[:3] + (255,))
    background.alpha_composite(tile)
    return background.convert("RGB")


def hex_rgb(value: str) -> tuple[float, float, float]:
    return tuple(int(value[i : i + 2], 16) / 255 for i in (1, 3, 5))


def colorset(name: str, light: str, dark: str) -> None:
    def color(value: str) -> dict:
        r, g, b = hex_rgb(value)
        return {"color-space": "srgb", "components": {"red": f"{r:.3f}", "green": f"{g:.3f}", "blue": f"{b:.3f}",
                                                      "alpha": "1.000"}}

    contents(ASSETS / f"{name}.colorset", {
        "colors": [{"idiom": "universal", "color": color(light)},
                   {"idiom": "universal", "appearances": DARK, "color": color(dark)}],
        "info": INFO,
    })


def main() -> None:
    contents(ASSETS, {"info": INFO})

    mascot_light = crop_to_art(Image.open(BRAND / "logos/mascot-stacked-light.png").convert("RGBA"))
    mascot_dark = crop_to_art(Image.open(BRAND / "logos/mascot-stacked-dark.png").convert("RGBA"))
    themed_scaled_imageset("Mascot", mascot_light, mascot_dark, MASCOT_POINTS)

    head_light = crop_to_art(Image.open(BRAND / "logos/head-horizontal-light.png").convert("RGBA"))
    head_dark = crop_to_art(Image.open(BRAND / "logos/head-horizontal-dark.png").convert("RGBA"))
    themed_scaled_imageset("HeadHorizontal", head_light, head_dark, 180)

    head = crop_to_art(Image.open(BRAND / "standalone/head/hardcharger-head-512.png").convert("RGBA"))
    single_imageset("HeadMark", head)

    gizmodata = crop_to_art(Image.open(ROOT / "docs/assets/gizmodata-logo.png").convert("RGBA"))
    single_imageset("GizmoDataMark", gizmodata)

    # The app icon: light, dark and tinted, one 1024 px image each (iOS 18 and later).
    icon = ASSETS / "AppIcon.appiconset"
    save_png(flatten(Image.open(BRAND / "app-icons/head-light/hardcharger-1024.png").convert("RGBA")),
             icon / "AppIcon-light.png")
    save_png(flatten(Image.open(BRAND / "app-icons/head-dark/hardcharger-1024.png").convert("RGBA")),
             icon / "AppIcon-dark.png")
    mono = crop_to_art(Image.open(BRAND / "monochrome/white/hardcharger-1024.png").convert("RGBA"))
    # As large in the tile as the colour icons' head (about 82% of its width).
    width = round(1024 * 0.82)
    mono = mono.resize((width, round(mono.height * width / mono.width)), Image.LANCZOS)
    tinted = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    tinted.alpha_composite(mono, ((1024 - mono.width) // 2, (1024 - mono.height) // 2 + 20))
    save_png(tinted, icon / "AppIcon-tinted.png")
    contents(icon, {
        "images": [
            {"filename": "AppIcon-light.png", "idiom": "universal", "platform": "ios", "size": "1024x1024"},
            {"filename": "AppIcon-dark.png", "idiom": "universal", "platform": "ios", "size": "1024x1024",
             "appearances": DARK},
            {"filename": "AppIcon-tinted.png", "idiom": "universal", "platform": "ios", "size": "1024x1024",
             "appearances": [{"appearance": "luminosity", "value": "tinted"}]},
        ],
        "info": INFO,
    })

    colorset("AccentColor", PALETTE["cobalt"], PALETTE["azure"])
    colorset("LaunchBackground", PALETTE["paper"], PALETTE["navy"])
    colorset("BrandNavy", PALETTE["navy"], PALETTE["paper"])
    print(f"wrote {ASSETS.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
