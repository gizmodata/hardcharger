# Android-only optimization validation

Branch: `release/v2.12.0-android-r8`, based on published v2.12.0
`c10b04a336945e6ed5f45c8a3d3da68e4616b74b`. Main and the published tag are unchanged.

The temporary `android-r8-oneoff.yml` workflow reuses the verified Play native
libraries from release run 43354192490. It checks the source commit, successful
native/bundle/release jobs, and that native/server/UI sources have not changed.
It builds and tests the minified release on a phone and short tablets, then signs
a bundle whose DEX and native-library hashes must match the tested build. The
bundle includes its R8 report and deobfuscation mapping. The upload is an internal
draft; production review is a separate authorized Console action.

Version name remains 2.12.0. This one-off reserves version codes strictly between
33501 and 33600, below the next normal server workflow's code allocation. Every
push gets a fresh code; exhausted reservations fail closed.

Temporary access consists only of an exact-branch GitHub environment policy and
an exact branch/workflow/event exception in the existing GCP federation provider.
Remove both after the upload, restoring the Terraform provider's normal tag-only
condition. No new service-account key or broader IAM role is needed.

For the later main integration, retain the app Gradle configuration, narrow JNI
keep rule, isolated release UI test module, R8 bundle validator and
tests, and optimized-release coverage in the regular Android workflow. Omit this
document and the temporary one-off workflow. Do not merge until approved.
