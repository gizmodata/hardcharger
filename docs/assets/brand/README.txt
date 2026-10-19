HARDCHARGER — BULL IDENTITY v3
Created October 19, 2026.

START HERE
preview/asset-overview.png shows the family. Open index.html for a local gallery.

MASTERS
Original generated transparent PNG artwork in masters/. Four assets: full bull and
foreground database, compact database-driver emblem, color head, monochrome head.
v3 replaces the substitute wordmark with the user-selected reference lettering.
CORRECTIONS RETAINED FROM v2: restored the far-side rear leg, giving the full mascot four visible
legs; removed the stray tail from the cropped driver emblem. Both fixes were made
with localized edits. All exports are derived from these fixed master files without
additional generation. The compact and monochrome head designs are unchanged.
The earlier full-mascot reference is preserved in references/.

ICONS
standalone/: transparent PNG symbols, 16–1024 px, plus lossless WebP.
app-icons/: light/dark full-mascot and head tiles, PNG, Windows ICO, macOS ICNS.
driver/: detailed emblem and compact head at 16,20,24,32,40,48,64,128 px.
For tiny DBeaver UI rows prefer driver/compact; use detailed at 48 px and above.
favicon/: head PNG sizes and multi-resolution ICO. No steam or database at tiny sizes.
monochrome/: black, navy, white head PNGs with transparent cutouts.

VECTOR AND LOGOS
vector/: genuine editable SVG PATHS, automatically traced from raster masters with
a limited palette. These contain NO embedded bitmap. Palette quantization and path
simplification can change subtle shading/edges. They are useful editable starting
points; not hand-drawn vector originals. Inspect at final print/manufacturing size.
logos/: horizontal and stacked layouts using full mascot or head. PNG layouts use
the original raster artwork; SVG layouts use the simplified palette traces.
wordmark/: the user-selected reference lettering, traced directly into custom SVG
outlines with PNG exports. This preserves the heavy, tightly spaced character of
the original lettering. No font name is claimed; no font installation is required.
source/trace_wordmark.py reproduces the trace from references/preferred-wordmark.png.
The reference letter contours are slightly smoothed into reusable vector paths.

WEB AND SOCIAL
web/: favicon, touch icon, square regular and maskable PWA icons, web manifest,
pinned-tab SVG and copyable HTML snippet. Adjust public paths for your website.
social/: transparent and solid avatars, 1200x630 share graphics.
No app, website or driver configuration has been modified.

USE
Keep artwork proportions intact. Use full mascot at generous sizes (about 160 px+
wide); use the head for compact identification. Keep clear space about one horn-tip
height around the mark. White steam and gold horns should remain visible on light
and dark backgrounds. The red database remains in front of the rear legs.

SOURCE / REBUILD
source/ includes palette tokens, outline wordmark data, build scripts and image
generation prompts. Artwork generated with the built-in image tool. Resizing and
packaging use Pillow; SVG tracing uses local pixel-boundary extraction; SVG rendering
uses Sharp. Original bundled runtime paths in scripts may need changing elsewhere.
