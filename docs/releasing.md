# Releasing the server

A server release is a `v*` tag on a commit of `main`. The tag runs `.github/workflows/server.yml`
on that commit: the full matrix (Linux, macOS, Windows, the web UI, the AddressSanitizer and
ThreadSanitizer jobs) gates everything, and only then are the images pushed and the GitHub
Release created. The version arithmetic lives in `.github/scripts/release.sh`, which the
workflows call and `.github/scripts/test-release.sh` tests on every run.

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

A release candidate never moves `:latest`, and no tag ever pushes a floating `:2.0` or `:2`.
Because GitHub never makes a pre-release the latest release, `releases/latest` and the
one-line installer's default keep resolving to the last release; a candidate installs only by
name (`HARDCHARGER_VERSION=2.0.0-rc.1`, `--version 2.0.0-rc.1`). The platform assets keep
version-free names, which the installers and the Python package download by.

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
