# Android release CI/CD

Android is part of `.github/workflows/server.yml` from v2.12.0. Every `v*` tag calls
`android.yml`; manual server runs can opt in with `android`, and `sign` enables signing.
Pull requests affecting Android still run its unsigned checks. The former
`release/android-preview` push trigger is retired.

## Build and release gates

1. Reuse the combined run's web workspace artifact from the same commit.
2. Cross-build ARM64 and x86_64 Play libraries, verifying bundled-only extensions.
   Cache dependency installs by ABI, NDK and dependency recipes. A successful release
   fast-forward to main warms those caches on the default branch, making them available
   to subsequent release tags. A cold cache remains a supported build path.
3. Run JVM tests, release lint and real-engine instrumentation for both Play and local
   distributions in a disposable x86_64 emulator. Include tablet navigation from About
   at 100%/200% font scale in short landscape windows. ARM64 and 16 KB emulator coverage
   is recorded separately in `../PARITY.md`.
   Separately install the non-debuggable, R8-optimized release and run the isolated
   `release-tests` UI harness against it. This harness uses no app classes, so the
   production optimizer does not need test-only keep rules. It checks real SQL,
   Unicode, bundled extensions, rejected external installs, restart persistence,
   About and short-tablet navigation.
4. Package the exact tested native artifacts. The protected `android-signing`
   environment provides the upload key. Validate the bundle, manifest, native hashes,
   signing certificate and every signed payload entry.
   Require R8 optimization/shrinking/obfuscation metadata and the embedded
   deobfuscation mapping; reject coverage below 25% in any category.
5. Retain `android-play-candidate` as a CI artifact with AAB, SHA-256, source
   commit, version code and notes. Publish `Hardcharger-android.aab` and
   `android-candidate.json` through the combined GitHub release, with the AAB covered
   by the release's `SHA256SUMS`. Android must pass before the release or images publish.
6. After the GitHub release succeeds, `android-play` automatically authenticates through
   the tag-restricted `google-play-internal` environment with a short-lived Google token
   and uploads the exact candidate as an **internal-testing draft**. There is no manual
   GitHub approval gate. The owner controls testing rollout and production publication
   in Play Console, matching the iOS build-upload/store-publication workflow.

The version name follows the server. Version codes are
`1000 + server-workflow run_number * 100 + run_attempt`; the first combined release
counter exceeds preview build 1501. Only the server workflow creates signed candidates.
Use a new run after attempt 99. Upload-only retries do not rebuild or change the code.

## Upload-only retry

Dispatch `android.yml` **on the original release tag**, setting `candidate_run_id` to
that server run. The retry checks repository identity, matching version tag, source
commit, checksum, signing metadata, both native jobs and the successful GitHub release
before authenticating. It uses the latest attempt's job results. The Play environment
restricts uploads to version tags without a manual approval gate. Existing bundles are reused only when both
version code and SHA-256 match. An existing internal draft must be reviewed/discarded
in Console before another upload; active releases are retained.

The uploader requests `changesNotSentForReview=true`, retrying without that optional
flag only for Google's specific rejection of it. Both attempts use
`changesInReviewBehavior=ERROR_IF_IN_REVIEW`. A separate read-back edit verifies the
draft, bundle checksum and retained release statuses. The uploader never changes
production or promotes the draft. Do not edit the app in Console during an API upload.

## Credentials and protections

- `android-signing` holds `ANDROID_UPLOAD_KEYSTORE_B64`, `ANDROID_UPLOAD_STORE_PASSWORD`,
  `ANDROID_UPLOAD_KEY_PASSWORD` and `ANDROID_UPLOAD_KEY_ALIAS`; tags `v*` and approved
  release branches may access it. Keystore/passwords never belong in Git or artifacts.
- `google-play-internal` admits version tags with no required reviewer or wait timer. Its variables
  are `GOOGLE_PLAY_WIF_PROVIDER` and `GOOGLE_PLAY_SERVICE_ACCOUNT`.
- Terraform in `gizmodata-infrastructure-terraform/gcp/core` manages the dedicated
  `hardcharger-play@gizmodata-core.iam.gserviceaccount.com` identity. Trust checks numeric
  repository/owner IDs, GitHub's immutable environment subject, the version tag and
  either a server release push or an Android upload-retry manual run. It grants no
  project roles or long-lived service-account keys. Play Console access is app-specific
  and restricted to testing releases.

## Preview history

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
admits only `release/android-preview`. Automatic uploads were disabled during bootstrap.
The owner uploaded build 1401 in Play Console; its preview
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
SHA-256 and all 141 payload signatures were verified after download. Its initial upload
attempts exposed two integration issues: GitHub uses the immutable subject prefix
`repo:gizmodata@181902052/hardcharger@1422890225`, and Google rejects the optional
`changesNotSentForReview` flag for this internal edit. Terraform now matches that exact
subject, and the uploader handles only that specific Google error as described above.

Upload-only [run 43336845636](https://github.com/gizmodata/hardcharger/actions/runs/43336845636)
passed all 16 release-control tests, verified the original candidate and build gates,
authenticated through federation, and saved the unchanged build **1501** as an internal
draft. A separate API read-back verified draft status, the original bundle SHA-256 and
retained previous release statuses. Build 1401 remains active; no rollout was requested.
The preview bootstrap is complete. The combined release workflow supersedes its
branch upload trigger and bootstrap variable.

For v2.12.0, the owner requested automatic draft uploads to match iOS. The GitHub
required-reviewer gate was removed; tag restrictions, keyless federation, signature
checks and draft-only uploads remain. Publication is still controlled in Play Console.
