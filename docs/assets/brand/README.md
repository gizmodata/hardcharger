# Hardcharger™ brand assets

The Hardcharger bull identity (v3), as delivered: `README.txt` is the kit's own guide, and
`preview/asset-overview.png` shows the whole family. Hardcharger™ and GizmoData™ are
trademarks of GizmoData LLC: put ™ on the prominent brand mention (alt text, a page title),
never ®.

| Folder | What it is | Use it for |
| --- | --- | --- |
| `logos/` | The bull with the lettering: `mascot-*` (full charging bull with the database stack) and `head-*` (compact head), each `horizontal` or `stacked`, PNG (1600 px wide, the original raster art) and SVG (palette traces) | `-light` (navy lettering) on light backgrounds, `-dark` (white lettering) on dark. Horizontal for web headers and banners, stacked where height allows. |
| `wordmark/` | The lettering alone, `hardcharger-light`/`-dark`, PNG and SVG | Where the bull already appears nearby, or space is a single line |
| `standalone/` | Lettering-free marks: `head/`, `driver/` (head with the database), and the full mascot as WebP; PNGs from 16 to 1024 px | Avatars, illustrations, any place the name is printed beside the mark |
| `web/` | Favicons (`favicon.ico`, `favicon.svg`, 16/32 px PNGs), `apple-touch-icon.png`, PWA icons (regular and maskable), `site.webmanifest`, `head-snippet.html` | Websites; `head-snippet.html` shows the `<head>` tags |
| `favicon/` | The head at 16–256 px and a multi-size `favicon.ico` | Favicons at other sizes |
| `app-icons/` | Square tiles (mascot or head, light or dark), 16–1024 px; `Hardcharger-head.ico`/`.icns`, `Hardcharger-mascot.ico`/`.icns` | Desktop apps, installers, Windows and macOS binaries |
| `driver/` | Driver icons: `compact/` (the head, for 40 px and smaller) and `detailed/` (head with the database, for 48 px and larger) | DBeaver and other tools' driver lists |
| `social/` | Share images (`share-light`/`-dark`, 1200×630) and avatars | Open Graph/Twitter cards, profile pictures |
| `monochrome/` | One-color head in black, navy and white | Print, watermarks, places that take one color |
| `vector/` | Editable SVG traces of each mark (no embedded bitmaps) | A starting point for new artwork; inspect at final size |

Keep the artwork's proportions, and clear space of about one horn-tip height around it. Use
the full mascot from about 160 px wide; below that, the head.

## Where this repository uses them

- `README.md` (and `ui/README.md`, `python/README.md`): `docs/assets/hardcharger-banner-light.png`
  and `-dark.png`, `logos/mascot-horizontal-*.png` at 1200 px wide, losslessly optimized.
- The web UI (`ui/`): `ui/src/assets/brand/` holds `logos/head-horizontal-*.svg` (header),
  cropped to the artwork (viewBox only) and run through svgo, and `logos/mascot-stacked-*.png`
  (sign-in page and About) cropped to the artwork, 480 px wide, as WebP (quality 90): at that
  size the SVG trace looks flatter than the raster art; `ui/public/` holds `web/favicon.svg` (svgo), a `favicon.ico` of the kit's
  own 16, 32 and 48 px frames (`favicon/favicon-*.png`), and `web/apple-touch-icon.png`,
  `web/app-192.png`, `web/app-512.png` (losslessly optimized) for `manifest.json`.
- Windows: `installer/hardcharger.ico` is `app-icons/Hardcharger-head.ico`, the icon of
  `hardcharger-server.exe`, `hardcharger.exe` and the MSI; `installer/banner.bmp` and
  `installer/dialog.bmp` (WiX's dialog bitmaps) are made from `standalone/head/` and
  `logos/mascot-stacked-dark.png`.

The kit's generation sources, reference images, masters and local gallery are not included.
