# Releasing the server

A server release is a `v*` tag on a commit of `main`. The tag runs `.github/workflows/server.yml`
on that commit: the full matrix (Linux, macOS, Windows, the web UI, the AddressSanitizer and
ThreadSanitizer jobs) gates everything, and only then are the images pushed and the GitHub
Release created. The version arithmetic lives in `.github/scripts/release.sh`, which the
workflows call and `.github/scripts/test-release.sh` tests on every run.

The ThreadSanitizer job uses an unshipped DuckDB fixture with its optional extensions
compiled with TSan and linked in (`server/scripts/build-tsan-duckdb.sh`). Public extension
modules are not instrumented, so their atomic synchronization is invisible to TSan. Native
release builds and ASan retain runtime loading, including signed-module repository replacement
tests; only those two runtime replacement tests are omitted from the TSan fixture. All
extension workloads still run there, and any unsuppressed sanitizer report fails the job.

## Versions

Two forms, nothing else:

| Form | Example | Means |
|---|---|---|
| `X.Y.Z` | `2.0.0` | a release |
| `X.Y.Z-rc.N` | `2.0.0-rc.1` | release candidate N of `X.Y.Z` (N from 1, no leading zeros) |

The version lives in `server/CMakeLists.txt`, in two lines, because CMake's
`project(... VERSION)` takes numbers only:

```cmake
project(hardcharger VERSION 2.0.0 LANGUAGES C CXX)
set(HARDCHARGER_VERSION_SUFFIX "-rc.1")   # "" for a release
```

Everything reports the two together (`2.0.0-rc.1`): `hardcharger-server --version`,
`hardcharger --version`, `HARDCHARGER_VERSION()`, `hardcharger_settings()`, the
`hardcharger_build_info` metric, the `/warp` discovery document and server-info, the
instrumentation's `instances` row, the web UI's About dialog and its tarball
(`hardcharger-ui-2.0.0-rc.1.tar.gz`), and the Windows executable's version strings. CMake refuses
any other suffix.

Two places can't hold `-rc.N` and use numbers instead:

- **Windows version resources and the MSI.** The executable's numeric `FILEVERSION` and the MSI's
  `ProductVersion` are `2.0.0.1` for `2.0.0-rc.1` and `2.0.0` for `2.0.0` (`release.sh
  msi-version`). Windows Installer ignores the fourth field when it compares versions, so the
  candidates and the release of one `X.Y.Z` count as the same version. `AllowSameVersionUpgrades`
  in `installer/Hardcharger.wxs` makes each one replace whatever is installed: `rc.1`, then
  `rc.2`, then the release, all upgrade in place. The fourth field only tells the candidates apart
  in Programs and Features.
- **The Python package (`python/`).** Its own version is PEP 440, so its candidate of 2.0.0 is
  `2.0.0rc1` (tag `python-v2.0.0rc1`). Its `SERVER_VERSION` pin, and `HARDCHARGER_VERSION`, take the
  server's form (`2.0.0-rc.1`) or the PEP 440 one; both download the release tagged `v2.0.0-rc.1`.
  From 2.1.0 onward, the package release matches the server exactly: server `X.Y.Z` uses
  package `X.Y.Z` and tag `python-vX.Y.Z`. Python-only fixes use `X.Y.Z.postN` and
  `python-vX.Y.Z.postN`; `SERVER_VERSION` continues to point at server `X.Y.Z`.
  Keep package metadata, runtime `__version__`, and the download pin aligned, and run
  `.github/scripts/check_python_version.py`. Both server and Python CI enforce this;
  a server release tag requires the base package without a post suffix. Publish the
  server artifacts first, then the matching Python tag so its download tests can use them.

## What a tag publishes

| | `v2.0.0-rc.1` (release candidate) | `v2.0.0` (release) |
|---|---|---|
| GitHub Release | pre-release, never "latest" | release, made "latest" |
| Release notes | CHANGELOG `## [2.0.0-rc.1]` | CHANGELOG `## [2.0.0]` |
| Images, ghcr.io and docker.io | `:2.0.0-rc.1`, `:sha-<short>` | `:2.0.0`, `:latest` |
| Tarballs and zips | `hardcharger-<platform>.tar.gz` / `.zip` | the same names |
| MSIs | `Hardcharger-windows-<arch>.msi`, ProductVersion `2.0.0.1` | the same names, `2.0.0` |
| Web UI tarball | `hardcharger-ui-2.0.0-rc.1.tar.gz` | `hardcharger-ui-2.0.0.tar.gz` |
| `SHA256SUMS` | every tarball, zip and MSI | the same |
| Code signatures | macOS binaries Developer ID signed and notarized, Windows executables and MSIs Authenticode-signed (see [Code signing](#code-signing)) | the same |

A release candidate never moves `:latest`, and no tag ever pushes a floating `:2.0` or `:2`.
Because GitHub never makes a pre-release the latest release, `releases/latest` and the
one-line installer's default keep resolving to the last release; a candidate installs only by
name (`HARDCHARGER_VERSION=2.0.0-rc.1`, `--version 2.0.0-rc.1`). The platform assets keep
version-free names, which the installers and the Python package download by.

## Code signing

Every `v*` tag signs what users run on macOS and Windows; a release can't be published unsigned.

| Platform | What is signed | How |
|---|---|---|
| macOS | `hardcharger-server` and `hardcharger` in `hardcharger-macos-arm64.tar.gz` | Developer ID Application: Gizmo Data LLC, hardened runtime, secure timestamp, notarized by Apple (`toitlang/action-macos-sign-notarize`, as GizmoSQL) |
| Windows | `hardcharger-server.exe` and `hardcharger.exe` in each zip, and each MSI | Authenticode, SHA-256 digests, RFC 3161 timestamp (DigiCert), with GizmoData's code-signing certificate in Azure Key Vault (`AzureSignTool`, `.github/scripts/azure-sign.ps1`; the key never leaves the vault) |

The Linux tarballs and the container images aren't code-signed; `SHA256SUMS` covers every asset.

**Order.** The tests run on the unsigned binaries, then they are signed, and only then packed:
the macOS build job signs and notarizes both binaries before it packs the tarball (and smoke-tests
the signed ones); windows.yml's `sign` job signs the two executables from each tested zip, checks
them and zips them again, the `msi` job builds each MSI from that signed zip, signs the MSI and
runs the install test on it. `SHA256SUMS` is computed over the signed files. The server is signed
with `server/macos/entitlements.plist`, which turns off only library validation, so that DuckDB
can load extensions a user `INSTALL`s (they aren't signed by our team); the client has no
entitlements. Bare executables can't be stapled: Gatekeeper finds their notarization ticket online.
Apple may accept a submission before that ticket is available to the runner. The mandatory
verifier retries its online lookup for up to 110 seconds, then fails if no ticket appears.
It does not retry or waive broken signatures, wrong signing identities or Gatekeeper rejection.

**Checks.** `.github/scripts/verify-macos-signature.sh` (codesign `--verify --strict`, the
Developer ID requirement for our Team ID, the hardened runtime, the timestamp, the notarization
ticket, Gatekeeper's `spctl` assessment, the exact entitlements) and
`.github/scripts/verify-windows-signature.ps1` (`Get-AuthenticodeSignature` Valid, a timestamp,
`signtool verify /pa`, one signer for every file) run in the signing jobs, the executables'
checks first shown to refuse an unsigned binary. Then `verify signatures (macos-arm64)` and `verify signatures (windows)`
check the very artifacts the release takes (each first shown to refuse a copy with one byte
changed) and run them, the Windows one on Windows on Arm, which runs both platforms' executables.
On a tag the image job and the release need both checks, and the release job fails, visibly,
unless every job it needs succeeded and the run signed.

**When.** Every `v*` tag, and a manual run with `sign`
(`gh workflow run server.yml --ref release/v2.0.0-rc.1 -f sign=true`), which signs and checks
everything a tag would and publishes nothing. Never on pull requests or pushes; every other run
leaves the artifacts unsigned, as before.

**Credentials.** All in the repository's `production` environment, none at repository level:
`APPLE_CERTIFICATE` (the Developer ID Application certificate and key, a base64 `.p12`; valid to
2030), `APPLE_CERTIFICATE_PASSWORD`, `APPLE_ID_USERNAME` and `APPLE_ID_PASSWORD` (the Apple ID and
app-specific password notarization uses), `APPLE_TEAM_ID`, and `AZURE_CLIENT_ID`,
`AZURE_TENANT_ID`, `AZURE_KEY_VAULT_URI` and `AZURE_CERT_NAME`. The environment admits only `v*`
tags and `release/*` branches, so only those runs can read them; only the jobs that sign or check
signatures enter it (the build job's macOS leg only on a signing run). Azure signs in by OIDC, no
stored secret: the existing code-signing app registration has a federated credential for the
subject `repo:gizmodata@181902052/hardcharger@1422890225:environment:production`. This
repository's OIDC tokens use GitHub's immutable subject format, owner and repository with their
numeric IDs, so the credential keeps working through a rename and can't be matched by another
repository that later takes the name.

To check a download yourself: `codesign --verify --strict --verbose=2 bin/hardcharger-server` and
`spctl --assess --type open --context context:primary-signature -v bin/hardcharger-server` on macOS
(`accepted`, `source=Notarized Developer ID`), `Get-AuthenticodeSignature .\bin\hardcharger-server.exe`
or the file's Properties > Digital Signatures on Windows.

## Cutting one

1. In `server/CMakeLists.txt`, set the numbers and the suffix (`-rc.N`, or `""` for the release).
2. In `CHANGELOG.md`, move `[Unreleased]` into `## [2.0.0-rc.1] - YYYY-MM-DD` (the release:
   `## [2.0.0] - YYYY-MM-DD`). The heading is matched as text, so it must be exactly that.
3. Check locally: `.github/scripts/release.sh version` prints the version,
   `.github/scripts/release.sh changelog 2.0.0-rc.1` prints the notes, and
   `bash .github/scripts/test-release.sh` passes.
4. Commit, tag the commit (`git tag v2.0.0-rc.1 <sha>`), and push both:
   `git push origin <sha>:refs/heads/main v2.0.0-rc.1`.

A tag that isn't `v` plus the version in `server/CMakeLists.txt`, or that has no (or an empty)
CHANGELOG section, fails in the workflow's first job (`changes`), before the matrix runs, and
nothing is published; the image job checks the tag again before it pushes, and the release job
checks both. The next candidate is a new tag (`-rc.2`); a
published tag is never moved.

## Bundled ADBC images

The `ADBC images` workflow (`adbc-images.yml`) publishes `:<server-version>-adbc`
to the same GHCR and Docker Hub repositories. It also updates `:latest-adbc` for
the current stable server release; older releases and candidates never move it.
Both native Linux architectures must build, load all 15 drivers and query the
running server through the `hardcharger` manifest before the multi-architecture
tags are published. The server image is resolved to a digest before building.

Run this **after the server and hardcharger-adbc releases have both published**.
The first GA driver needs the released server for its own tests, so this is a
separate workflow rather than a dependency of the server release:

```bash
# Build and test first without publishing.
gh workflow run adbc-images.yml --ref main \
  -f server_version=2.0.0 -f driver_tag=v2.0.0 -f publish=false
# After it succeeds, publish the same pinned versions.
gh workflow run adbc-images.yml --ref main \
  -f server_version=2.0.0 -f driver_tag=v2.0.0 -f publish=true
```

The native pins in `docker/prepare-adbc-drivers.py` are GizmoSQL 2.0.15, Oracle
1.0.1, Db2 0.2.1 and Quack 0.1.0-alpha.11; Hardcharger defaults to 2.0.0 and can
be selected with `driver_tag`. Every native release asset is checked against
GitHub's SHA-256 digest before it enters the Docker context. Credentials stay
outside the image. The other ten drivers are installed from the public dbc
registry using dbc 0.3.1; the installed version inventory is saved in the image.
Update these pins when refreshing the driver layer, then rerun this workflow.
The base server release does not need to be rebuilt or retagged.

For a local build, stage both architectures with
`python3 docker/prepare-adbc-drivers.py`, then build `docker/Dockerfile.adbc` with
`BASE_IMAGE` pointing at the desired released server image. Run
`bash docker/test-adbc.sh <image>` to check the drivers and manifest discovery.
The Python test tools are installed only in a disposable test image.
