# App Store screenshots — Hardcharger 2.14.0

Ready-to-upload screenshots for App Store Connect, prepared locally. Nothing in this directory
has been uploaded.

| App Store Connect field | Files | Dimensions | Format |
| --- | --- | --- | --- |
| iPhone 6.9" display | `iphone-6.9-inch/01-…` through `06-…` | 1320 × 2868 | 24-bit RGB PNG |
| iPad 13" display | `ipad-13-inch/01-…` through `06-…` | 2064 × 2752 | 24-bit RGB PNG |

App Store Connect scales the 6.9" and 13" sets down for the smaller displays. Upload each set in
numeric order: web UI with a query's results, the native console, the server (dark), Share on
Local Network, settings, and the splash screen (dark). No captions or device frames are added.
The Logs screen is left out (it lists session ids).

## Capture provenance

- Hardcharger 2.14.0 (build 100), built for the simulator from tag `v2.14.0`
  (`d27832ee19874e76fbc7ddbda3ba8c782c1dacac`) with `ios/scripts/build-server-ios.sh
  iphonesimulator` and `ios/scripts/generate-project.sh`.
- Captured by `ios/scripts/screenshots.sh` (the `HardchargerScreenshots` UI test) on the
  iPhone 17 Pro Max and iPad Pro 13-inch (M5) simulators, iOS 26.4, Xcode 27.2, with the status
  bar overridden (9:41, full battery).
- Data: TPC-H scale factor 0.01, generated in the app by `CALL dbgen(sf => 0.01)`. No customer
  data, remote credentials or tokens. The Share screens show the capturing Mac's private LAN
  address, as a phone on that network would.
- Port 31337 must be free on the Mac while capturing (the simulator shares the Mac's network;
  quit a running Hardcharger desktop app first), or the server screens show a fallback port and
  a note about it.

## Recreate

```bash
ios/scripts/screenshots.sh /tmp/hc-shots "iPhone 17 Pro Max" "iPad Pro 13-inch (M5)"
```

then copy `<device>-light-webui`, `-light-console`, `-dark-server`, `-light-share`,
`-light-settings` and `-dark-splash` into the numbered files here. The script picks the newest
runtime that has a simulator of each name; for 2.14.0 that was iOS 27.2 for the iPad but iOS
26.4 for the iPhone, so the iPad was pinned to its iOS 26.4 simulator to keep both sets alike.
