# Hardcharger for Android

The actual Hardcharger server on Android, its shared web workspace, and the native CLI
console through a small JNI bridge. Kotlin/Compose supplies the Android controls,
secure credential storage, document picker and share integration. See [PARITY.md](PARITY.md)
for the acceptance checklist and evidence; unchecked work is not release-ready.

Android joins the combined release in v2.12.0. Google Play availability is coming soon;
CI prepares internal-testing drafts for owner review. See [release CI/CD](play/CI.md).
The Android version follows `server/CMakeLists.txt`.

## Build

Prerequisites: Java 17, CMake 3.28+, Ninja, Node.js 24+, Android SDK, NDK
28.2.13676358. Android 9 / API 28 is the minimum; target and compile SDK are 36.
The native build supports ARM64 (including Apple silicon emulators) and x86_64.
CI builds both architectures and runs Play/local instrumentation on x86_64.

Install the SDK packages with Android Studio's SDK Manager or `sdkmanager`:

```bash
sdkmanager 'platform-tools' 'platforms;android-36' 'build-tools;36.0.0' \
  'ndk;28.2.13676358' 'emulator' 'system-images;android-36;google_apis;arm64-v8a'
echo no | avdmanager create avd --name Hardcharger_Phone \
  --package 'system-images;android-36;google_apis;arm64-v8a' --device pixel_7
android/scripts/build.sh
android/scripts/run-emulator.sh
```

On macOS the default SDK location is `$HOME/Library/Android/sdk`; elsewhere set
`ANDROID_HOME`. `ANDROID_NDK_HOME` can override the pinned NDK location. Native outputs
are isolated under `android/build/<abi>`, never mixed with desktop/iOS builds.

For native-only iteration:

```bash
android/scripts/build-native.sh
(cd android && ./gradlew :app:assembleDebug)
```

The debug APK is `android/app/build/outputs/apk/debug/app-debug.apk`. The Gradle wrapper
uses Gradle 8.13 with its official distribution checksum. The app uses the repository's
brand art and bundled Mozilla CA bundle. No credentials are embedded in build artifacts.

## Emulator

The launcher enables the Mac/host hardware keyboard in the AVD configuration. Click an
input field inside the emulator to type. If the emulator was already running with its
keyboard disabled, close it and run the launcher again; a cold boot applies the change
without erasing app data.

On this Mac, double-click **Launch Hardcharger.command** in this directory to open the
phone emulator with the locally built app installed. Build artifacts are kept under
`build/artifacts/`; use the version reported by the current build.

`run-emulator.sh` installs without clearing the database and forwards **Mac localhost
31437 → emulator 31337**. Enable TLS/sharing in the app to use HTTPS. To connect from
Android to a server running on the Mac, use the emulator's host address `10.0.2.2`.
For TLS, the remote certificate must cover that name/address.

The emulator's address is private to its virtual network. Do not use it as a Wi-Fi sharing
address. Physical Wi-Fi/mDNS reachability and vendor-specific battery/process management
still require a device. The application intentionally runs the local server in the
foreground: after 30 seconds away it stops gracefully; returning resumes it. An open
document picker receives a five-minute grace period. Keeping the
screen on while sharing is optional. There is no background-service or always-on claim.

## Tests

```bash
android/scripts/test.sh
```

`test.sh` uses a separate **Hardcharger_Test** emulator on port 5558, because the tests
exercise database, settings and credential changes. It preserves the product-evaluation
phone. Direct Gradle connected tests may uninstall their target APK, so use disposable
test emulators for those.

JVM tests cover endpoint validation, import/SQL quoting, and settings boundaries.
Instrumentation uses the real engine: start/stop/restart, SQL, UTF-8, console commands,
file import, persistence, TLS approval, encrypted credentials, and saved connections.
Check the actual reports under `android/app/build/reports` and `android/build/acceptance`
for executed results. To exercise remote connections with a disposable server/identity
provider (no real account required):

```bash
uv run --with pyjwt --with cryptography android/scripts/remote-fixture.py \
  /path/to/hardcharger-server /tmp/hardcharger-android-remote.json emulator-5558
```

Leave that process running while testing. For the actual browser UI test, finish Chrome's
first-run screens on the test emulator, then add `-e browserUI true` to `adb shell am
instrument -w` for `com.gizmodata.hardcharger.test/androidx.test.runner.AndroidJUnitRunner`.
The default test run skips browser UI; remote protocol tests skip if no fixture exists.

## Credentials, TLS, and files

Administrator credentials and saved connections are encrypted with AES-GCM under a
non-exportable Android Keystore key. Tokens remain in memory and are handed to the web
workspace only at document start on the exact selected origin, in sessionStorage.
The app disables backups and device transfers with explicit Android 12+ extraction rules
([Android backup documentation](https://developer.android.com/identity/data/autobackup)). TLS keys, credential ciphertext and WebView storage are in the
server's sensitive-path exclusions. Remote certificate approval pins the exact leaf;
validity and hostname checks remain enabled. Changing an endpoint clears its saved trust.

Files are imported through Android's document picker or ACTION_SEND. Import creates a
new table and fails if that name exists; it does not silently replace user tables. SQL
exports go into the displayed Exports directory and can be saved through the document
picker. Blob exports from the web UI use an origin-restricted message bridge (50 MB limit).
The native console can write larger exports directly to Exports.

## Google Play and signing

See [play/README.md](play/README.md) for the local release checklist, listing drafts,
privacy review and internal-testing steps. Prepare an unsigned review bundle with:

```bash
android/scripts/build-play.sh --unsigned-preview
```

The Play distribution includes the nine bundled extensions but omits DuckDB's external
native-extension installer/loader from the binary. Its native artifacts and Gradle
outputs are separate from local builds. Packaging verifies native hashes and distribution;
the app also checks its JNI distribution at startup. Local builds retain automatic
installation/loading of signed extensions and the normal non-iOS defaults.

Local builds use Android's debug key. For a signed release APK/AAB, supply
`HARDCHARGER_ANDROID_KEYSTORE`, `HARDCHARGER_ANDROID_STORE_PASSWORD`,
`HARDCHARGER_ANDROID_KEY_ALIAS`, and `HARDCHARGER_ANDROID_KEY_PASSWORD`, then run
`./gradlew :app:assembleRelease :app:bundleRelease` in `android/`.
Set `HARDCHARGER_ANDROID_BUILD_NUMBER` to a monotonically increasing version code.
For Google Play, use `android/scripts/build-play.sh` with those variables instead of the
plain Gradle command above. The bundle is under `android/build/app-play/outputs/bundle/release/`.
The [preview CI workflow](play/CI.md) prepares signed candidates and gated internal-draft
uploads; its remote setup is still pending. Never upload a local-distribution bundle or debug-signed
artifact to Play. Keep upload keys and passwords outside the repository and back them up.
