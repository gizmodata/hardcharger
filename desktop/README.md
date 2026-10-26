# Hardcharger™ desktop preview

The existing Hardcharger server and its embedded web UI in a Tauri desktop app. The server
and command-line client remain independent tools; the desktop app bundles both. The bull
head from the existing brand kit is the application and tray icon. The application icon
uses a white tile so the bull's navy outline stays clear in the Dock and taskbar; the macOS
menu-bar icon uses the kit's monochrome head. The controller uses
GizmoData's blue palette and the web UI's Inter typography and Hardcharger artwork.

## Build a local preview

Use Node.js 24, Rust stable, and the platform's [Tauri prerequisites](https://v2.tauri.app/start/prerequisites/).
Supply a trusted release or static local build of **both** `hardcharger-server` and
`hardcharger`, with the web UI embedded. Dynamic developer builds are not portable.

```bash
cd desktop
npm ci
HARDCHARGER_DESKTOP_SERVER=/absolute/path/to/bin/hardcharger-server npm run prepare
npm run desktop:dev
```

`prepare` copies the repo's existing branding. With `HARDCHARGER_DESKTOP_SERVER`, it also
stages the server and the adjacent CLI, named for Rust's host target. Override
`HARDCHARGER_DESKTOP_CLIENT` or `HARDCHARGER_DESKTOP_TARGET` when necessary. It never
downloads executables. Generated assets, staged binaries, installers, and screenshots
are ignored by Git. Versioning comes from `server/CMakeLists.txt`.

On macOS, build an app and a disk image without scripting Finder:

```bash
npm run desktop:build -- --bundles app
uv run --with ds-store==1.3.3 python scripts/create-dmg.py \
  src-tauri/target/release/bundle/macos/Hardcharger.app \
  artifacts/Hardcharger-desktop-macos-arm64.dmg
```

On Windows, `npm run desktop:build -- --bundles msi` builds the installer. Run native builds
and tests on each architecture; the first preview targets macOS Apple silicon and Windows
x64/Arm64. Linux packaging and testing are follow-up work.

## Behavior and boundaries

- The local server starts by default, bound to `127.0.0.1:31337`. The app refuses a port
  collision and never attaches to or stops an existing server.
- **Settings → Network access** switches between **Only this computer** (the default)
  and **Allow network connections**. Stop the server, choose a listen IP (all IPv4:
  `0.0.0.0`, all IPv6: `::`, or a specific interface), enter this computer's client-facing
  DNS name/IP, and set the administrator user name and password. The password persists
  in the OS credential store. Save and start the server; the dashboard shows the actual
  endpoint, transport, listen address, and URLs for other computers.
- TLS is the default transport for network mode. Supply the PEM certificate-chain and
  private-key paths; the certificate must match the server DNS name/IP. The controller
  trusts the supplied chain and verifies its name and validity. The native SQL workspace
  and other browsers also need the certificate/CA trusted in their operating system.
  No certificate checks are bypassed. **HTTP — unencrypted private network** is an
  explicit alternative; it sends credentials and SQL without encryption. The app does
  not change firewall/router rules. Clients must be able to reach the configured TCP
  port, and this computer must stay awake.
- **Open SQL workspace** opens the server's actual `/ui/` in its own window, automatically
  signed in using the same token handoff contract as iOS. The query interface is not
  duplicated. Its native privileges are restricted: it cannot call desktop commands.
  Query inspectors can request closing their own window through a dedicated navigation
  action; this grants no controller or general native access.
- Closing the dashboard hides it and leaves the server and bull-head menu-bar/tray icon
  running. The icon and Dock/Start-menu shortcut reopen the app. **Quit Hardcharger** drains
  running queries and stops the child before exiting. Stop/restart closes the query window.
- Launch at login is opt-in and starts quietly using `--background`.
- The preview has its own application identifier and data directory:
  `com.gizmodata.hardcharger.desktop.preview`. On macOS, its database, log, and preferences
  live under `~/Library/Application Support/com.gizmodata.hardcharger.desktop.preview/`.
  It never adopts an existing CLI database automatically.
- Settings groups, option names, defaults, and descriptions come from the bundled server's
  `--print-options-json`. Custom values are type-checked before saving; the server performs
  semantic validation at its next start. Settings can change while the server is stopped.
- Import YAML shows a review before replacing custom values. Unknown keys, duplicate keys,
  wrong types, oversized documents, and desktop-managed options are rejected. Exports
  include the port and custom public server options, and omit secrets. Importing an export
  retains existing saved secrets. Desktop-only preferences such as launch at login are not
  server YAML options.
- Sensitive options use macOS Keychain or Windows Credential Manager. Native APIs return
  a placeholder, not a stored secret. Secret values enter the child environment, not its
  command line or generated YAML. Preferences and generated server YAML use private files.
- The preview owns the local listener, built-in web UI, local administrator credential,
  log destination, sensitive-path protections, and shutdown lifecycle. These options are
  visible as managed; listener/TLS/administrator fields are edited in Network access.
  YAML imports cannot change network sharing or its credentials, and exports leave
  desktop-managed networking/authentication out. mTLS and saved remote connections remain
  follow-up work. Returning to local-only mode restores loopback HTTP and a freshly
  generated private administrator password, while retaining the saved network configuration.
- Normal close/quit is covered; crash recovery and OS logout/reboot behavior still need
  platform acceptance testing before release. Background residency does not keep a sleeping
  computer awake or make this a system service.

## Verification

```bash
npm run build
npx playwright install chromium
npm test
cd src-tauri
cargo fmt --all -- --check
cargo clippy --locked --all-targets -- -D warnings
HARDCHARGER_DESKTOP_TEST_SERVER=/absolute/path/to/bin/hardcharger-server cargo test --locked
```

The native lifecycle test uses a real server and CLI: port collisions, custom configuration,
unknown/managed options, authenticated SQL, a write surviving shutdown/restart, and port
release. Browser tests cover the blue controller, themes, navigation, minimum width, and
typed YAML import/export with secret omission. `npm run dev` with `?preview=1` is a clearly
labeled **design preview**, with sample status and native controls disabled; it generates
`artifacts/desktop-light.png` and `desktop-dark.png` during tests.
Network tests also cover an IPv6 listener, persistent credential redaction, authenticated
HTTP/TLS startup with a freshly generated short-lived test certificate, wrong passwords,
and rejection of a mismatched TLS hostname. Test certificates never enter the OS trust store.

## Signing and release artifacts

`.github/workflows/desktop.yml` produces Actions artifacts for review; it never publishes a
release. A feature-branch push builds unsigned/ad hoc previews. The dedicated
`release/desktop-preview` branch requests signed previews using the existing `production`
environment, whose branch rules already allow `release/*`. It does not merge into main,
create a tag, submit to an app store, or publish a GitHub release.

The server workflow calls the desktop workflow with the native binaries from **the same
run**. A future release tag includes the desktop packages only after the desktop build,
signature checks, notarization/stapling, and shared checksums pass.

- macOS reuses `APPLE_CERTIFICATE`, `APPLE_CERTIFICATE_PASSWORD`, `APPLE_ID_USERNAME`,
  `APPLE_ID_PASSWORD`, and `APPLE_TEAM_ID`. Signed builds override the preview's ad hoc
  identity with GizmoData's Developer ID, notarize/staple the app, then sign/notarize/staple
  the DMG. Verification requires our team, Gatekeeper approval, and valid stapled tickets.
- Windows reuses the existing Azure OIDC login, Key Vault certificate, and
  `.github/scripts/azure-sign.ps1`. The application and MSI receive Authenticode signatures;
  the bundled server/client signatures are checked too.
- Standalone previews use the published native archive matching the repository's server
  version, verified against that release's `SHA256SUMS`. Their manifest records this source
  separately from the desktop commit. They do not claim those native binaries were rebuilt
  from the preview branch.
- Installers have distinct `Hardcharger-desktop-*` filenames so the current CLI MSIs and
  tarballs remain available. Each platform artifact includes checksums and provenance.

No new certificates are required if the existing repository credentials remain valid.
