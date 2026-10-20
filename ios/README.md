# Hardcharger™ for iPhone and iPad

The Hardcharger server running on the device itself, with its web UI and a native SQL
console, and a client for Hardcharger servers anywhere else.

- **Web UI**: Hardcharger's own web UI (query editor, results grid, schema browser, sessions,
  API page, uploads) from the server in the app, signed in by the app; or a saved remote
  server's.
- **Console**: the `hardcharger` command-line client inside the app: the same statement
  endings, dot commands (`.help`, `.tables`, `.schema`, `.mode table|csv|json|markdown|line|…`,
  `.timer`, `.maxrows`, …) and output formatting, against this device's server or a saved one.
- **Server**: start, stop, restart; address, uptime, versions, database size, memory; the log
  (with level marks or plain); import CSV, TSV, JSON and Parquet from Files, the share sheet,
  or a drop on iPad.
- **Share on Local Network** (off by default): the server listens on the network with TLS (a
  certificate the device makes for itself) and shows its address as a QR code and its
  certificate fingerprint, so a computer on the same Wi-Fi opens the web UI and signs in with
  the administrator's password; a second QR code adds the server to another iPhone or iPad's
  Hardcharger app.
- **Saved servers** (in the Keychain): user name and password, or the server's single sign-on
  (Warp SQL browser sign-in, in an `ASWebAuthenticationSession`); a self-signed server is
  trusted by its fingerprint only when the person says so.

iOS 18 or later. On iPad: a sidebar, keyboard shortcuts (⌘1–⌘6 screens, ⌘I import, ⌘⇧L
sharing, ⌘R reload, ⌘⇧R / ⌘. start and stop, and in the console ⌘↩ run, ↑/↓ history, Esc clear,
⌘. cancel, ⌘K clear), multiple windows, Stage Manager. On iPhone: a tab bar.

## Build and run

You need Xcode 26 or later (with an iOS simulator runtime), CMake, Ninja, xcodegen and Node.js
24 (`brew install cmake ninja xcodegen node`).

```bash
git submodule update --init                         # third_party/warp-sql
(cd ui && npm ci && npm run build && npm run package)    # the web UI the server embeds
ios/scripts/build-server-ios.sh                     # the server, for the simulator and devices
ios/scripts/generate-project.sh                     # ios/Hardcharger.xcodeproj
open ios/Hardcharger.xcodeproj                      # run the Hardcharger scheme
```

`build-server-ios.sh` cross-compiles the server and every library it links for iOS devices
(`iphoneos`, arm64) and the simulator on Apple silicon (`iphonesimulator`, arm64), with the
same recipes, pins and patches as every other platform (`server/scripts`, run with
`HARDCHARGER_IOS_SDK` and `ios/cmake/ios.toolchain.cmake`), merges each platform's archives
into one static library, checks that every object in it was built for that platform, and
packages both as `ios/build/HardchargerServer.xcframework` (module `HardchargerServer`). The
first build takes a while (DuckDB, twice); after that only what changed is rebuilt. Pass
`iphonesimulator` or `iphoneos` to build one. It then runs the server's smoke test inside a
simulator (`server/embedded/smoke_test.cc`, through `xcrun simctl spawn`).

The project is generated (`ios/project.yml`) and not checked in. `generate-project.sh` takes
the version from `server/CMakeLists.txt`: `MARKETING_VERSION` X.Y.Z, `CURRENT_PROJECT_VERSION`
N for a release candidate X.Y.Z-rc.N (100 for the release; `HARDCHARGER_IOS_BUILD_NUMBER`
overrides it, as CI does with its run number).

Launch arguments for tests and screenshots (none changes what a person's app does):
`-HCResetState YES` (no settings, database, imports, logs or saved servers), `-HCPort 0` (any
free port; a Mac running Hardcharger may hold 31337, which the simulator shares),
`-HCInitialTab web|console|server|logs|servers|settings`, `-HCInitialSheet share`,
`-HCSplashMinimum <seconds>`, `-HCSplashOnly YES`.

## Test

```bash
xcodebuild test -project ios/Hardcharger.xcodeproj -scheme Hardcharger \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro'
```

- `HardchargerTests` (unit, hosted in the app): the server through the C API (start, sign in,
  run, stop, start again on the same database, a second server and bad options refused, TLS
  with the device's certificate trusted only by its fingerprint), the launch arguments
  (secrets never in them), settings, the Keychain credential, the self-signed certificate,
  the console's readiness rules (a `;` in a string, quoted name, comment or dollar quote ends
  nothing; several statements; dot commands) and its output, byte for byte the desktop
  client's.
- `HardchargerUITests`: the splash, then the web UI signed in by the app running
  `SELECT 42 AS answer`; the console running `SELECT 42 AS answer` (a statement over two
  lines runs at its `;`) and `.tables`; settings that persist; stop and start; Share on Local
  Network answering over TLS on the LAN address with the certificate the app shows, and the
  web UI and console over TLS.
- `HardchargerRemoteTests` (`ios/scripts/test-remote.sh <hardcharger-server>`): a desktop
  server on the Mac with a mock identity provider; a saved server's web UI and console with a
  password, and browser sign-in end to end. Not in the CI gate (it needs the desktop build).

CI (`.github/workflows/server.yml`): the `ios` job builds the server for the simulator, runs
its smoke test there and runs the unit and UI tests, on `v*` tags and on a manual run with
`ios: true` only (macOS runners are expensive; never on a push or pull request). On a `v*`
tag, `ios-archive` then builds for devices and exports an App Store-signed `.ipa` as the
run's artifact and uploads it to TestFlight (App Store Connect API key; a failed upload fails
the release gate, and the `.ipa` artifact can then be uploaded by hand with Transporter).

## How it works

```
┌──────────────────────── Hardcharger.app ─────────────────────────┐
│ SwiftUI: Web UI · Console · Server · Settings · Logs · Servers    │
│   WKWebView ── http(s)://127.0.0.1:<port>/ui/ ──┐                 │
│   Console (ConsoleEngine) ── Warp SQL ──────────┤                 │
│   ServerController / EmbeddedServer             │                 │
│        │ C API: hardcharger_embedded.h          ▼                 │
│ ┌──────▼──── HardchargerServer.xcframework ───────────────────┐   │
│ │ hardcharger-server's main() on a thread, the Warp SQL SDK,  │   │
│ │ DuckDB 2.0 + built-in extensions, Arrow IPC, OpenSSL,       │   │
│ │ the web UI (embedded), the `hardcharger` client's library   │   │
│ └─────────────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────────┘
```

- **The server is `hardcharger-server` itself.** `server/CMakeLists.txt` with
  `HARDCHARGER_EMBEDDED=ON` compiles the server's own `main.cc` as `hardcharger_server_main`
  into `hardcharger_embedded`, behind a small C API (`server/embedded/hardcharger_embedded.h`:
  start with the server's command line, port, request stop, wait, free, versions, notices).
  `server/src/embedding.hpp` holds `main()`'s only three hooks: it reports readiness and a
  fatal error to the app, and takes the app's stop requests instead of SIGINT/SIGTERM (an app
  can't take a signal on one thread; the server's own `StopSignals::Wake()` sends SIGTERM to
  the process). One server runs at a time in a process; it stops and starts again cleanly.
  DuckDB's built-in extensions are registered on the first start (an app links them from an
  archive, where DuckDB's own before-`main` registration wouldn't be pulled in).
- **iOS-only differences**, each guarded where it is: DuckDB is built with
  `ENABLE_EXTENSION_LOAD=OFF` (iOS runs only the code the app was signed with; the extensions
  are built in) and its platform named; libcurl is built for iOS (it has none) and linked from
  the build; OpenSSL uses its own iOS targets; the client's `.shell` is refused (`system()` is
  prohibited on iOS); warp-sql's build-time OpenAPI generator runs as a Mac build
  (`ios/scripts/run-host-tool.sh`). OpenSSL has no CA bundle on iOS, so the app bundles
  Mozilla's (`ios/scripts/update-ca-bundle.sh`) and names it in `SSL_CERT_FILE`.
- **Files**: the database is `Documents/Hardcharger.duckdb`, with `Imports/` and `Exports/`
  beside it, all visible in the Files app (On My iPhone › Hardcharger). The log, the TLS key
  and certificate and DuckDB's spill directory are under `Library`. The server's
  `sensitive_paths` adds the app's preferences, web view storage, cookies and its own key
  directory to its built-in list, so no SQL statement reads them.
- **Credentials**: on first launch the app makes the administrator's password (24 letters and
  digits) and the token-signing key and keeps them in the Keychain (this device only). The
  server gets them through its environment while it reads its options, never in its
  arguments or a file. The web UI is signed in by writing the access token where the UI keeps
  it (`sessionStorage`, `hardcharger-ui:sign-in`, `ui/src/lib/auth.ts`) on the server's
  origin only, once per token; never in a URL, a cookie or `localStorage`.
- **The console** is the desktop client: `server/embedded/console.cc` drives its
  `WarpConnection`, `CommandProcessor`, `SqlProcessor` and renderers exactly as
  `hardcharger -c` does, and hands what it printed to the app. Return runs input that is
  complete as the client reads it; anything else gets a new line and the continuation prompt.
- **Lifecycle**: iOS suspends apps that leave the screen; the app asks for the usual grace
  time so running statements finish, and on return restarts a server that no longer answers.
  While sharing, the screen can stay on (Settings › Share on Local Network).

## Signing and releases

The bundle ID is `com.gizmodata.hardcharger`, team `XYB42X6NFX` (Gizmo Data LLC). Local runs
on the simulator need no signing. CI's `ios-archive` job signs manually in a keychain of its
own, with these secrets of the `production` environment:

| Secret | What |
| --- | --- |
| `IOS_DIST_CERT_B64` | The "Apple Distribution: Gizmo Data LLC (XYB42X6NFX)" certificate with its private key, a base64 `.p12` |
| `IOS_DIST_CERT_PASSWORD` | Its password |
| `IOS_PROVISIONING_PROFILE_B64` | The App Store profile "Hardcharger App Store" for `XYB42X6NFX.com.gizmodata.hardcharger`, base64 |
| `ASC_KEY_B64`, `ASC_KEY_ID`, `ASC_ISSUER_ID` | An App Store Connect API key (base64 `.p8`, key id, issuer id): the TestFlight upload on `v*` tags |

The certificate and the profile expire on 2027-04-27. `ios/scripts/ci-signing.sh` installs
them, archives, exports and checks the `.ipa` (team and bundle in the signature, the profile
inside), and removes the keychain and profile afterwards.

## Brand assets

`ios/scripts/brand-assets.py` (Pillow) makes the asset catalog from the official artwork in
`docs/assets/brand` and the GizmoData mark (`docs/assets/gizmodata-logo.png`): the splash and
launch screen's stacked bull with the lettering, the head for small places, and the app icon
(light, dark and tinted) from the kit's head tiles. Images are cropped, scaled and, for the
icon, flattened onto the tile's own colour; nothing is redrawn.
