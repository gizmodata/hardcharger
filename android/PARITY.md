# Android acceptance record

Local implementation on branch `android-app`, based on the v2.11.0 source. Verified on
2026-11-12 on this Apple silicon Mac. No branch, issue, PR, package or app was published.
The server/Python versions remain unchanged. The APK is a local debug-signed ARM64 build.

## Core iOS feature coverage

- [x] Actual embedded Hardcharger server, DuckDB, Arrow, Chargewire, CLI console and web UI.
- [x] All nine iOS built-in extensions; native smoke verifies extension registration.
- [x] Start, stop, restart, readiness, occupied-port error and subsequent recovery.
- [x] Persistent database; native restart, background stop/resume and process-death recovery.
- [x] Web workspace automatic sign-in, SQL editor, real results and schema/table workflows.
- [x] Android document picker upload; exported CSV selected, previewed and loaded into a table.
- [x] Web CSV download to Android's Save dialog; saved bytes checked against query results.
- [x] Native console, statement completion, dot commands, history, Unicode and cancellation.
- [x] Native file import, import-name validation, SQL quoting and save/export integration.
- [x] Persistent settings, limits, logs, version and third-party notices.
- [x] Saved remote connections, edit/delete, encrypted credentials and password sign-in.
- [x] Browser SSO through Chrome Custom Tabs and the mock identity provider; reconnect.
- [x] Self-signed TLS explicit approval and exact certificate pinning, without expiry/name bypass.
- [x] TLS sharing controls, random password, password/certificate regeneration, QR links and NSD.
- [x] Connection-link parsing, review before saving, fingerprint validation and matching.
- [x] iOS-inspired four-tab phone navigation, tablet rail, grouped settings, sharing sheet,
  light/dark palettes, activity recreation and console keyboard actions.
- [x] ARM64 native ELF and APK 16 KB alignment; functional tests on a 16 KB emulator.
- [x] Repeatable native/APK build, emulator launcher and local APK.
- [x] Optional release signing configuration; no publishing configured.

## Executed evidence

- Native `hardcharger-embedded-smoke`: PASS (start/query/extensions/CSV/restart/persistence).
- JVM validation includes marked/plain and multiline log parsing, automatic resource
  bounds, explicit resource overrides and fresh-install defaults.
- Common app instrumentation: five workflows passed on API 36 phone and API 36
  `google_apis_ps16k` tablet (`getconf PAGE_SIZE` = 16384).
- Additional real Custom Tab SSO workflow passed on the phone. The browser test is opt-in
  on other emulators because Chrome's first-run screens need completing.
- Remote fixture uses the existing desktop server and mock IdP. It also exercised legacy
  `/warp` discovery; on-device tests exercise current `/chargewire` discovery.
- Actual debug WebView via DevTools: automatic sign-in, editor input, query result and CSS
  viewport checks passed on both emulators. CSV save/upload round trip passed on the phone.
- Background server stopped after 32 seconds and resumed on return; force-stop/relaunch
  restored service. Activity recreation retained navigation; the database survived restarts.
- Shared UI: typecheck passed and 397 tests passed. A regression test covers off-screen menus.
- Android lint: no errors. Remaining warnings include newer dependency versions, deliberate
  exact-certificate TLS handling, ARM64-only packaging and minor icon/style suggestions.
- APK signature verification and `zipalign -c -P 16 4` passed.

Local evidence is retained under `android/build/acceptance/`. Generated reports and APKs
are intentionally ignored by Git. `android/scripts/test-webview.mjs` reruns the actual
workspace query checks after opening the Web tab.

## Explicit differences and release follow-up

The core app is ready for local emulator evaluation; this is not a Play release approval.

- Physical-device checks remain: Wi-Fi/mDNS reachability, camera scanning of QR codes,
  OEM process management, battery/thermal behavior and broader accessibility testing.
  Emulator NAT is clearly identified in the sharing screen.
- This build is ARM64. The x86_64 build path exists but has not been built or validated.
  Android 9 is the declared minimum; this session tested API 36, not the full OS matrix.
- iPad-specific independent windows, the full iOS keyboard-shortcut set, drag/drop import
  and the destructive erase-database control are not ported. Native import/share, styled
  live logs and the main console shortcuts are available. Log-level filtering and sharing
  the log file remain separate follow-up work. Settings and Logs share a persisted
  Emoji / Plain preference, defaulting to Emoji and applying without a server restart.
- Android connection links understand the iOS format. Android uses administrator name
  `admin`; the current iOS link importer defaults to `hardcharger`, so an iOS user adding
  this Android server must change that username. QR codes never contain a password.
- File-picker grace is five minutes; ordinary background grace is 30 seconds. The local
  server is not an always-on background service. A process recreation during a web file
  selection can require selecting the file again.
- Web Blob downloads are capped at 50 MB. Larger exports can be written from the console
  into Exports, then saved with the native file picker.
- Configure production signing, audit distribution notices and complete store graphics,
  public privacy policy and declarations before publishing. The separate Play distribution
  omits external native-extension loading; see `play/README.md`.
  Local Android retains the repository's non-iOS extension defaults and signature checks.

## Native design review

The visual reference is the actual SwiftUI implementation and the checked-in iOS
screenshots under `docs/assets/ios-*`. Android reuses the mascot artwork, iOS accent
colors, grouped sections and typography hierarchy. Rounded gradient surfaces and a
floating tab bar approximate the material; Apple Liquid Glass itself is not available
on Android. Logs are reached from Server and saved servers from Settings on phones.

The navigation/console/settings/recreation/sharing workflow passed on phone and 16 KB
tablet after the redesign, and on the phone at 130% font size. Android lint has no errors.
Visual checks cover light/dark server screens, grouped settings, the sharing sheet and
tablet layout. Screenshots and test logs are in `android/build/acceptance/design/`.

The follow-up interface sweep adds the same About mascot (including the dark variant),
GizmoData footer, trademark notice, website link and dedicated third-party notices page
as iOS. Version rows query all four components from the native embedded server, including
the bundled web UI; App also shows the Android build number. Server reuses those rows.
Copy controls, the platform address share sheet, QR framing, console previous/next/clear
and hide-keyboard controls, and styled searchable log rows follow the iOS presentation.
Larger text stacks detail rows and log entries. Android retains its native back behavior,
file picker, share sheet, tablet rail and explicit Save settings action.

Both updated UI workflows passed on the phone and 16 KB tablet at normal and 130% font
sizes (eight runs total), covering About/notices navigation, activity recreation, console
history, log filtering and sharing. Visual review covers About in light/dark mode, its
footer, tablet layout and the styled log view. Lint reports zero errors and 26 warnings.
Evidence is in `android/build/acceptance/about/`; the local APK was refreshed and its
signature and 16 KB ZIP alignment verified. No credentials, database or sharing settings
were reset during this sweep.

## Control-by-control follow-up audit

The earlier design review did not establish complete UI parity. This audit compares the
actual SwiftUI controls in `ios/Hardcharger/Views/`, `SQLConsole/ConsoleScreen.swift` and
`Server/ServerSettings.swift` with the Android implementation. “Gap” means still missing;
it is not a platform limitation or a claim of completed parity.

| Area / control | Android behavior and remaining differences |
| --- | --- |
| Settings: startup and port | Same auto-start default (on), port 31337 and valid range 1024–65535. Android has an explicit Save settings action; iOS saves edits immediately. |
| Settings: memory | Automatic plus the same 512 MB / 1 / 2 / 4 / 8 GB presets. Automatic uses 30% of physical RAM, bounded to 256–4096 MB. Android additionally retains custom 128–8192 MB limits. |
| Settings: threads | One per core is the fresh-install default; fixed counts remain selectable. The actual core count is shown in explanatory text and Server resources. Android uses a picker instead of an iOS stepper. |
| Settings: logging | Same info level, statement logging enabled and Emoji view defaults on fresh installs. Emoji/Plain is shared with Logs and persists immediately without restarting the server. Existing saved choices are retained. |
| Settings: network | Sharing and local TLS default off; sharing requires TLS. Keep-awake defaults on. Android places keep-awake in Settings; iOS places it in the sharing screen. |
| Settings: administrator | Generated credentials, reveal/copy, password rotation and certificate rotation are present. Android opens password details in the sharing sheet; iOS shows them inline. Storage uses Android Keystore. |
| Settings: Data | Import and database details are available from Server. **Gap:** the extra Settings import shortcut and destructive Erase Database control. |
| Settings: links and About | Saved Servers and About links; same mascot/footer, app/component versions, website and software notices, plus an Android privacy policy. Android supports native Back from Notices or Privacy → About → Settings. |
| Server: header and controls | Status, uptime, port, TLS, start/stop/restart and Web UI controls are present. **Gaps:** iOS's inline restart-needed/failure recovery controls and Use Another Port action. Android currently uses dialogs. |
| Server: details | Address copy, sharing, database file/size/import, resolved memory/threads, logs and component versions. **Gap:** App memory in use metric. Android also exposes export files. |
| Logs | Emoji/Plain, text search, follow, timestamps/levels, expand and copy line. **Gaps:** minimum-level filter, Share Log File and Clear View. |
| Console | Execute/cancel, statement completion, history previous/next, clear input/transcript and hide keyboard. **Gaps:** iOS's catalog/schema prompt, in-console server picker, Copy/Share Transcript and explicit Reconnect menu action. Remote connections can be selected through Saved Servers. |
| Saved Servers | Add/edit/delete, password or browser sign-in, certificate trust, connection links, web and console access. **Gaps:** connection test/discovery inside the editor, reachability indicators and separate Open Web UI / Open in Console row actions. |
| Sharing | TLS, address copy/share, password reveal/copy, certificate fingerprint and QR codes. Emulator-specific addresses replace unusable LAN QR codes. iOS also lists every reachable address and asks for confirmation before enabling sharing. |
| Web workspace | Same embedded web application; Android-native file selection/export and browser sign-in integration. See the existing WebView acceptance evidence above. |
| Platform presentation | Android back navigation, tablet rail, native pickers and share sheet; adaptive launcher icon uses the existing mascot without the legacy square-icon padding. Apple Liquid Glass and independent iPad windows remain platform differences. |

Resource defaults apply to fresh installations; saved numeric limits are preserved on
upgrade. UI tests exercise Automatic / preset / Custom and One per core, including
activity recreation without saving draft changes. The isolated tablet server test reads
DuckDB's actual thread count and memory limit after starting with automatic settings.
The Emoji/Plain workflow checks rendering, shared state, recreation and unchanged server
uptime; a separate process-relaunch check confirms the saved Plain selection loads again.
Follow-up evidence and launcher screenshots are under `android/build/acceptance/preferences/`.

Validation for these follow-up changes: 10 JVM tests; four phone UI workflows; four
tablet UI workflows at 130% text size; and seven combined tablet UI/native workflows
passed. Native checks verify the calculated limits against DuckDB's effective settings.
Lint has zero errors (28 warnings, including the adaptive-icon monochrome suggestion).
APK signature and 16 KB ZIP alignment checks passed. The launcher image was inspected
in the tablet app drawer to confirm the enlarged mascot fits the circular crop.

## Local Play preparation (November 12, 2026)

The separate ARM64 Play distribution excludes DuckDB's external native-extension
capability archives. Its eight UI/native tests passed on the 16 KB tablet, including
bundled extension availability, denied external INSTALL/LOAD even after enabling their
settings, SQL, persistence, import, cancellation, TLS and About/privacy navigation.
The regular local distribution passed eight applicable tests (one Play-only test skipped),
including actual automatic install/load defaults and enabled signature verification.
Both distributions passed ten JVM tests; lint has zero errors and 28 warnings each.
The unsigned Play AAB passed bundletool structural validation and packaged-native hash
verification. Its configuration requests 16 KB native alignment; ELF LOAD segments are
also aligned to 16 KB. The refreshed local APK passed signature and ZIP alignment checks.

Evidence is under `android/build/acceptance/play/`. The review bundle is
`android/build/artifacts/Hardcharger-2.11.0-play-arm64-UNSIGNED.aab`; it is not upload-ready.
Play signing enrollment, owner-reviewed public privacy policy, store graphics/declarations,
account verification and physical-device testing remain. No Play upload or Git publication was
performed, and the user's phone emulator/data were not changed by these checks.

The owner subsequently created the Console app. A dedicated RSA upload key was created
outside Git, and the full wrapper-based build produced the signed internal-test candidate
`android/build/artifacts/Hardcharger-2.11.0-play-arm64-build1.aab`. The wrapper's pinned
checksum had an extra trailing character; it was corrected against Gradle's official
distribution checksum before the build passed. JVM tests and release lint passed again.
Bundletool validates the signed AAB. Manifest inspection confirms the expected package,
version 2.11.0/code 1 and non-debuggable release. All 139 payload entries have valid
digests and the expected upload certificate when read with Java's verifying JarFile API.
The upload key and bundle have not been registered/uploaded to Play.

## Android-only CI preparation

The local `release/android-preview` branch now includes `.github/workflows/android.yml`.
Actionlint (including shell checks), shell syntax, nine offline release-control tests,
and positive/negative bundle-signature verification passed locally. The workflow builds
ARM64/x86_64 libraries, tests Play/local distributions on an x86_64 emulator, signs only
after those gates pass, and optionally uploads the same candidate as an internal draft.
It is not yet pushed or exercised on GitHub. Remote signing/OIDC configuration and the
initial Console bootstrap remain pending; see `play/CI.md`. All-platform v2.12.0 release
integration is deferred until this standalone pipeline has passed in CI.
