# Google Play graphics — Hardcharger 2.11.0

Ready-to-upload English graphics, prepared locally. Nothing in this directory has been
uploaded to Play Console. `preview.png` / `preview.html` show the complete collection;
these previews and the `source/` directory are not store upload assets.

| Play Console field | Files | Dimensions | Format |
| --- | --- | --- | --- |
| App icon | `app-icon.png` | 512 × 512 | 32-bit RGBA PNG |
| Feature graphic | `feature-graphic.png` | 1024 × 500 | 24-bit RGB PNG |
| Phone screenshots | `phone/01-…` through `05-…` | 1080 × 1920 | 24-bit RGB PNG |
| 7-inch tablet screenshots | `tablet-7-inch/01-…` through `05-…` | 1080 × 1920 | 24-bit RGB PNG |
| 10-inch tablet screenshots | `tablet-10-inch/01-…` through `05-…` | 1920 × 1080 | 24-bit RGB PNG |

Upload each screenshot set in numeric order: SQL workspace, local server, native console
(dark theme), settings, About. Tablet sets are separate captures of the tablet layout,
not enlarged phone screenshots. No captions or device frames are added to screenshots.

The feature graphic follows the owner's iOS header reference: navy background, the
existing full mascot / wordmark, Android copy, and overlapping actual Android app captures.
Its rounded panels are a graphic composition, not an additional app feature or a screenshot
for the device-specific upload fields. The icon preserves the app's existing enlarged
mascot and square background; Google Play applies its own corner mask.

## Capture provenance

- App package: `com.gizmodata.hardcharger`, version **2.11.0 (1501)**, Play distribution.
- Source AAB: `Hardcharger-2.11.0-play-arm64-x86_64-build1501.aab`.
- AAB SHA-256: `dd024023553094b8814613fa54daf381c80cec2a8fd623cd105192c0612737f8`.
- Candidate source commit: `7f7c9140c53b65a666c0d238d4ce8a72ddf42ff1`.
- Installed using bundletool 1.18.3 `build-apks --mode=universal`. The installation APK
  was signed with the local Android debug key; the application code is the release
  Play variant from the signed AAB. These are emulator captures, not Play-delivered
  physical-device test evidence.
- Android 16 / API 36 ARM64, isolated AVD `Hardcharger_Store_Capture`, serial
  `emulator-5558`. The owner's emulator `emulator-5554` was not changed.
- Capture viewports: phone 1080×1920 at 360 dpi; compact tablet 1080×1920 at 240 dpi;
  large tablet 1920×1080 at 220 dpi. These are virtual form-factor layouts for the
  corresponding Play fields, not photographs of specific physical tablet models.
- Sample sales data is entirely synthetic; `source/sample-data.sql` contains it.
- All SQL results came from the app's embedded server. No customer data, remote
  credentials, tokens, or private host names are included.

## Recreate

Use a disposable emulator and the matching Play AAB. Set the viewport with
`adb -s <capture-serial> shell wm size WIDTHxHEIGHT` and `wm density DPI` before launch.
Seed the data through the native console. In the web workspace, run the corresponding
query, expand the schema on tablets, hide the side panel on phones, and resize the SQL
editor using its divider to leave the results visible. Switch Android's dark theme on
for the native console; use light theme for the remaining listing screenshots.
Before each capture, exit and re-enter Android System UI demo mode with clock 09:41,
full battery and hidden notifications. Reset demo mode after each theme or density change
to avoid stale duplicate system icons on the emulator. Hide the keyboard and wait for
query results before capturing. Capture the actual display using
`adb -s <capture-serial> exec-out screencap -p`.
Never clear, resize, reinstall, or change settings on the owner's working emulator.

From the repository root, with UI dependencies and Playwright Chromium installed:

```bash
node android/play/graphics/source/render.mjs
python3 android/play/graphics/source/validate.py
```

The renderer creates the feature graphic from editable HTML, resizes the existing app
icon, losslessly normalizes screenshot PNGs to RGB, and generates the previews. It does
not alter app content or fabricate screenshots. The font is the repository's Inter
font. `source/screens/` holds the additional light-console and dark-server captures
used only in the feature graphic. `manifest.json` records the upload assets and hashes.

## Suggested alt text

- Icon: Hardcharger blue bull mascot with red mane and gold horns.
- Feature graphic: Hardcharger for Android, with local server controls and SQL query results on a navy background.
- 01: SQL workspace querying synthetic sales data, with results grouped by region.
- 02: Hardcharger's embedded server running locally, with database and connection controls.
- 03: Native SQL console showing regional sales totals in dark mode.
- 04: Server settings with automatic memory, one thread per core, and logging preferences.
- 05: About Hardcharger, with its charging bull mascot and bundled software versions.

Requirements checked against [Google Play preview asset guidelines](https://support.google.com/googleplay/android-developer/answer/9866151).
