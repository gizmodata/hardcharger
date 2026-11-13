# Additional capture observations

The store images show Play build 1501.

## Compact-landscape navigation — fixed locally

Build 1501 clipped the native navigation rail in a compact landscape viewport
(1920×1080 at 288 dpi, approximately 1067×600 dp), particularly on screens with
a top app bar such as About.

The local source now makes the rail vertically scrollable and avoids adding system
insets a second time inside Scaffold. Touch targets retain their normal size.
A regression test opens all six destinations from About with real touch input,
including after activity recreation. It passed at 600 dp height with 100%, 130%,
and 200% font sizes, and at 400 dp height with 200% fonts. The debug Play build
and Android lint also passed (zero lint errors), as did the existing phone About
and back-navigation workflow.

Evidence is retained in `android/build/acceptance/tablet-rail/`. The change will
be included when the next Android candidate is built; the existing store screenshots
and their source-build provenance remain unchanged.
