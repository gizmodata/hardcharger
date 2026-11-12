# Google Play preparation

The intended release path is [Android preview CI/CD](CI.md): signed builds from
`release/android-preview`, then internal-testing draft uploads after the initial Console
bootstrap. Local build commands below are for development and release troubleshooting.

The owner has created the organization account and app in Play Console. Build work stays
local: no bundle or listing has been uploaded by the agent. Organization verification
and pricing are still to be confirmed. `release-setup.json` records unresolved choices.

## Before the first upload

1. Complete organization verification and any account tasks shown in Play Console.
2. Create **Hardcharger**, English (United States), **App**, suggested category **Tools**.
   Confirm free/paid pricing with the owner. The package ID is
   `com.gizmodata.hardcharger`; confirm it before the first upload.
3. Review `listing/en-US/` and the privacy policy at
   `../app/src/main/assets/PrivacyPolicy.txt`. Confirm the legal entity/contact address,
   host the approved policy at a public URL, and enter that URL in Play Console.
   The same policy is accessible under Settings → About → Privacy Policy in the app.
4. Review `data-safety-review.md`. Complete Data safety, ads, content rating, target
   audience, app access and any declarations requested by the actual Console. Draft
   repository material is not a submitted declaration.
5. Prepare and review store graphics: 512×512 store icon, 1024×500 feature graphic and
   phone screenshots of the **Play** build. Include tablet screenshots if advertising
   tablet support. Use synthetic data, never a real server password or customer data.
6. Set up Play App Signing and a dedicated upload key. Keep its keystore/passwords
   outside Git and back them up securely. A dedicated upload key has now been generated
   locally; it has not been registered with Play. The local signing directory contains
   the keystore, public certificate and an owner-readable `credentials.json`.
7. Choose the next unused positive Android version code. Version name follows the
   server (currently 2.11.0); this preparation does not bump server or Python versions.

## Build locally

```bash
# Review only: this AAB cannot be uploaded until rebuilt with signing.
android/scripts/build-play.sh --unsigned-preview

# Signed build: configure these securely in the shell, without writing secrets here.
# HARDCHARGER_ANDROID_KEYSTORE / HARDCHARGER_ANDROID_STORE_PASSWORD
# HARDCHARGER_ANDROID_KEY_ALIAS / HARDCHARGER_ANDROID_KEY_PASSWORD
# HARDCHARGER_ANDROID_BUILD_NUMBER
android/scripts/build-play.sh
```

To use the local credential file without putting passwords in shell history:

```bash
python3 android/scripts/build-play-with-key.py \
  --signing-directory "$HOME/LocalOnly/signing/hardcharger-android" \
  --build-number 1
```

Use the next unused build number for subsequent uploads. Keep `upload-keystore.p12`
and `credentials.json` in a password manager or encrypted backup; neither belongs in
Git or a Play upload. Only the `.aab` is uploaded in the normal first-release workflow.

Output: `android/build/app-play/outputs/bundle/release/app-release.aab`.
The verified first signed candidate is also copied to
`android/build/artifacts/Hardcharger-2.11.0-play-arm64-build1.aab`.
Build scripts never upload. An ARM64 build is appropriate for this first preview;
x86_64 needs its own native build and acceptance run before inclusion.

The Play library statically includes autocomplete, tpch, httpfs, ducklake,
postgres_scanner, core_functions, parquet, json and icu. It excludes the separate
DuckDB loadable-extension and httplib capability archives and registers only bundled
extensions. Turning extension settings back on through SQL cannot restore the omitted
loader. This addresses Play's restriction on downloaded native executable code; store
acceptance still depends on Google's review. Ordinary local Android builds keep the
repository's non-iOS extension defaults and signature verification.

The build verifies native symbols and hashes, and checks the packaged AAB against those
verified artifacts. Runtime instrumentation additionally exercises denied INSTALL/LOAD
and working bundled JSON functionality. The app rejects a mismatched JNI distribution.
An unsigned bundle can also be validated with Google's `bundletool validate`; structural
validation does not imply policy approval.

## First test release

After explicit authorization to upload, use **Internal testing**, enable Play App
Signing, upload the signed AAB and review Play's processing warnings/pre-launch results.
Invite selected testers and distribute the opt-in link. Use `reviewer-notes.txt` as the
starting point for review instructions. Local database features require no account.
Remote-server tests need a disposable server/identity provider with synthetic data.

Before production, test the Play-delivered build on physical Android hardware (a borrowed
device or device lab is sufficient), especially Wi-Fi/mDNS, sharing/TLS, file providers,
keyboard/back navigation, background/resume and low-memory behavior. Emulator testing
does not establish these device behaviors. Review the remaining differences in
`../PARITY.md`; a successful bundle build is not a complete product sign-off.

Official references: [create an app](https://support.google.com/googleplay/android-developer/answer/9859152),
[testing tracks](https://support.google.com/googleplay/android-developer/answer/9845334),
[device and network abuse](https://support.google.com/googleplay/android-developer/answer/16559646),
[user data/privacy](https://support.google.com/googleplay/android-developer/answer/10144311).
