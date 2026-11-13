# Android preview CI/CD

The workflow is `.github/workflows/android.yml`. Its initial release branch is
**release/android-preview**. A push to that
branch triggers Android only: server/Python push triggers are limited to main/tags, and
the desktop preview uses other branch names. Opening a PR to main deliberately brings
back the repository's broader checks. Do not create a global `v*` tag for this preview.

## What the pipeline does

1. Build the web workspace from the same commit.
2. Cross-build ARM64 and x86_64 Play libraries, checking that external native-extension
   loading is absent. Cache dependency installs by ABI, NDK and dependency recipes.
3. Run JVM tests and release lint; on a disposable x86_64 emulator, exercise both Play
   and regular local distributions using the real engine, UI and TLS/persistence tests.
   ARM64/16 KB emulator evidence currently comes from local testing; this workflow
   does not claim physical-device or ARM64 emulator coverage on the Linux runner.
4. After both native jobs pass, package their exact artifacts into one release AAB.
   The `android-signing` environment supplies the upload key only to this job.
5. Validate the bundle, manifest, native hashes and signatures. Retain the AAB, SHA-256,
   source commit, version code and notes as `hardcharger-android-candidate`.
6. Once bootstrapped, authenticate with a short-lived Google token and upload that same
   candidate through the `google-play-internal` environment. Only an **internal draft**
   is created; no production rollout or review submission is requested. An existing
   internal draft must be reviewed/discarded before another can replace it.

Preview pushes sign automatically. Uploads stay disabled until repository variable
`ANDROID_PLAY_BOOTSTRAPPED=true` is configured. A manual run can request signing and/or
an internal draft explicitly; signing is restricted to release branches or matching
version tags, and uploads require signing. PRs never receive these credentials.

Version codes are `1000 + github.run_number * 100 + github.run_attempt`. This leaves the
local build 1 behind, increases on retries and is checked against Play's limit. Use a
new workflow run after retry 99. Release runs are serialized. A future transition to a
different workflow must preserve monotonic codes against the last uploaded Play code.

## One-time setup before the first branch push

- Create GitHub environment **android-signing**, restricted to
  `release/android-preview` (and later approved release branches/tags). Store:
  `ANDROID_UPLOAD_KEYSTORE_B64`, `ANDROID_UPLOAD_STORE_PASSWORD`,
  `ANDROID_UPLOAD_KEY_PASSWORD`, `ANDROID_UPLOAD_KEY_ALIAS`.
  Reuse the already-created local upload key. Transfer secret values directly to
  GitHub's secret store; do not commit, print or attach the keystore/credential JSON.
- Create **google-play-internal**, with an owner approval rule and the same branch
  restriction. The workflow references that environment, but YAML alone does not create
  its protection rules.
- Enable the Google Play Android Developer API in a Google Cloud project. Create a
  dedicated service account and grant it access to this app in Play Console, with the
  permissions needed to manage testing releases, not production releases.
- Configure GitHub OIDC → Google Workload Identity Federation for that service account.
  Restrict the trust to this repository's numeric owner/repository identity and the
  `google-play-internal` environment, with release-branch protection in GitHub. Grant
  the principal `roles/iam.workloadIdentityUser` on the service account. No long-lived
  Google service-account JSON key is needed.
- Set environment variables **GOOGLE_PLAY_WIF_PROVIDER** (full provider resource name)
  and **GOOGLE_PLAY_SERVICE_ACCOUNT** (service-account email) in `google-play-internal`.

Keep `ANDROID_PLAY_BOOTSTRAPPED` unset or `false` for the first run. After the first successful
CI run, download its signed candidate and use it for the initial Console upload/app
signing enrollment. Complete the Console's required initial setup. Google's Edits API
documentation requires an existing app with an initial Console upload. Then enable
the bootstrap variable. Subsequent branch pushes can upload after environment approval.
The local build is a validation artifact, not the normal release source.

Do not edit this app in Console during a CI upload: Console changes can invalidate
the API edit. A failed validation does not trigger a fallback rollout. The signed
candidate remains available even if Google authentication or upload fails.

## v2.12.0 integration

After the Android-only workflow passes on GitHub, make the build reusable and call it
from the server's all-platform release workflow. Gate the combined release on Android
tests/signature verification, reuse that run's web artifact, preserve the Play version
counter, and promote the tested candidate through the agreed store track. Remove the
standalone branch upload trigger when it is superseded. This integration and a version
bump are intentionally not part of the preview work; server/Python remain 2.11.0.

## Validation status

Local validation covers workflow syntax, shell syntax, signed bundle verification and
offline tests for branch/signing gates, version-code progression, checksum rejection,
preserving existing active releases, draft-only upload and aborting failed edits.

[Run 43318844453](https://github.com/gizmodata/hardcharger/actions/runs/43318844453)
passed on 2026-11-12 at source commit `bcacbc45ee2fce16e06041ef1e851162aafab702`.
Both Linux native builds, JVM tests, release lint and the x86_64 Play/local emulator
suites passed. Dependency-install caches were saved for both ABIs (about 500 MB each).
The signed candidate is version 2.11.0, code **1401**, with ARM64 and x86_64 libraries.
The downloaded bundle's SHA-256, source metadata and all 141 signed payload entries
were independently verified against the local upload certificate.

The GitHub `android-signing` environment now contains the four upload-key secrets and
admits only `release/android-preview`; `ANDROID_PLAY_BOOTSTRAPPED=false` explicitly
disables automatic uploads. The owner uploaded build 1401 in Play Console; its preview
showed only tester/deobfuscation/native-symbol warnings. A subsequent Console screenshot
confirmed build 1401 is active on the internal-testing track.
The successful CI run skipped `play-draft` and did not upload anything to Google Play.

On 2026-11-12, `gizmodata-infrastructure-terraform/gcp/core` provisioned the dedicated
`hardcharger-play@gizmodata-core.iam.gserviceaccount.com` identity and GitHub federation
in project `gizmodata-core` (8 additions, no changes/destroys; subsequent plan clean).
The provider is
`projects/462472414356/locations/global/workloadIdentityPools/hardcharger-play/providers/github`.
Its trust checks Hardcharger's numeric repository and owner IDs, the exact Android
workflow and preview branch, the Play environment and push/manual events. No private
key was created. GitHub's `google-play-internal` environment now has the two variables,
reviewer `prmoore77`, no administrator bypass and only the preview branch allowed.
The owner confirmed sending the Play Console service-account invitation. Manual
[run 43330352240](https://github.com/gizmodata/hardcharger/actions/runs/43330352240)
produced build **1501** from `7f7c9140c53b65a666c0d238d4ce8a72ddf42ff1`. Both caches
hit, both native jobs and emulator suites passed, and the signed bundle's metadata,
SHA-256 and all 141 payload signatures were verified after download. The run is
waiting for the `google-play-internal` environment approval; the first federated
upload and actual Play API permission check remain pending.

References: [Google API setup](https://developers.google.com/android-publisher/getting_started),
[initial upload and edit lifecycle](https://developers.google.com/android-publisher/edits),
[OIDC authentication](https://github.com/google-github-actions/auth),
[draft releases](https://developers.google.com/android-publisher/tracks).
