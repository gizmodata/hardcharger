# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **The baseline Linux x86_64 server, for CPUs without AVX2**
  ([#62](https://github.com/gizmodata/hardcharger/issues/62)). On linux-amd64 the launcher
  installs the optimized build as before, unless this CPU lacks an instruction it needs
  (`/proc/cpuinfo`, x86-64-v3): then it installs `hardcharger-linux-amd64-baseline.tar.gz`
  (released from server 2.13.2 on) and says why. `HARDCHARGER_CPU_TARGET=optimized` or
  `=baseline` chooses the build; the default is `auto`. The baseline build is cached apart
  (`~/.cache/hardcharger/v<version>/linux-amd64-baseline/`) and comes from GitHub Releases only,
  as the container image holds the optimized build.

## [2.13.1] - 2026-11-15

### Added

- On Linux the server can come from the public container image
  (`ghcr.io/gizmodata/hardcharger:<version>`) when GitHub can't be reached or refuses, as
  in a sandbox that allows PyPI and ghcr.io but not github.com. The launcher reads the
  image through the registry's HTTP API with the standard library alone (no Docker): an
  anonymous pull token, the manifest for this architecture, every layer checked against
  its digest, and the server's files (`/opt/hardcharger` in the image, the same files as
  the release tarball's) taken out of the layers. The same `--version` check follows.
- `HARDCHARGER_INSTALL_SOURCE`: `auto` (the default: GitHub, then on Linux the container
  image), `github` or `ghcr`. Another value is an error that names these three.
- `HARDCHARGER_INSTALL_REGISTRY_IMAGE` names another copy of the image, such as
  `docker.io/gizmodata/hardcharger` or a mirror in an air-gapped network, and
  `HARDCHARGER_RELEASE_DOWNLOAD_URL` the base of the direct download URLs.

### Changed

- The download needs no GitHub token and no GitHub API. The release tarball comes from its
  direct download URL
  (`https://github.com/gizmodata/hardcharger/releases/download/v<version>/...`) and must
  match its SHA-256 in the release's `SHA256SUMS`, so GitHub's limit of 60 anonymous API
  requests an hour per address no longer applies. With a token configured the API is still
  asked first (GitHub Enterprise Server, a private copy), and without one it is asked when
  the direct download fails.
- When every source fails, the error names each one tried and why: the HTTP status, a rate
  limit and when it resets, the connection error, and the host a redirect led to. Each
  failed attempt is also reported on stderr as it happens. The hint to set a token "while
  the releases are internal" is gone: the releases are public.

### Fixed

- A GitHub token that the API refuses (an expired `GH_TOKEN`, a token for something else)
  no longer ends the install with HTTP 401, 403 or 404: the launcher says so on stderr and
  downloads without the token.
- A release whose API answer records no digest for the tarball is checked against the
  release's `SHA256SUMS`, and not installed when there is neither. Before, such a download
  was installed without any check.
- A download that breaks off partway is reported as that, and the next source is tried,
  where it used to be reported as a wrong SHA-256.
- Two processes installing the same release at once (parallel test workers on a cold
  cache) no longer step on each other: the one that finishes second keeps the first one's
  install, which may already be running, where it used to delete and replace it.
- A cache directory that can't be created, a tarball that isn't one and a server whose
  `--version` hangs are reported as install errors (`hardcharger: ...`, exit status 1),
  not as Python tracebacks.

## [2.13.0] - 2026-11-14

### Changed

- Install Hardcharger server 2.13.0, with server-confirmed read-only connections,
  the CLI's `--read-only` option, improved credential masking in statement logs,
  and Android performance and document-screen improvements.

## [2.12.1] - 2026-11-14

### Changed

- Install Hardcharger server 2.12.1, in which a finished statement no longer answers
  `"state": "running"` with its result attached.
- Require `adbc-driver-hardcharger>=2.5.0` for the `[adbc]` extra. 2.5.0 opens a connection in
  the catalog and schema it is given: earlier releases set the two in no fixed order, so
  `connect(catalog=..., db_schema=...)` often failed with "No catalog + schema named ..." or
  opened in the catalog's default schema. It also installs on Python 3.10 with pyarrow 16 and
  adbc-driver-manager 1.8, as this package does.

## [2.12.0] - 2026-11-12

### Changed

- Install Hardcharger server 2.12.0, the release introducing the native Android app.

## [2.11.0] - 2026-11-12

### Changed

- Install Hardcharger server 2.11.0, with access-control fixes and SQL Monitor improvements.

- Require `adbc-driver-hardcharger>=2.4.9` for the `[adbc]` extra, including recovery
  from cached revoked tokens after a cluster Stop, ADBC conformance fixes, and restricted
  shared-library exports.

## [2.10.0] - 2026-11-07

### Changed

- Install Hardcharger server 2.10.0 (the server is unchanged; the release is for the CLI and the
  web UI, which connect lazily to a stopped auto-resume server).
- The `[adbc]` extra needs `adbc-driver-hardcharger` 2.4.2 or later: connecting to a stopped
  GizmoData Cloud cluster signs in at the first request that does work, not at connect, and closing
  a connection never signs in (2.4.1), so neither starts a stopped cluster for nobody.

## [2.9.0] - 2026-11-06

### Changed

- Install Hardcharger server 2.9.0, with standalone query workflow improvements and Chargewire 2.3.0.

## [2.8.0] - 2026-11-05

### Changed

- The package installs Hardcharger server 2.8.0 (Chargewire 2.2.3).

## [2.7.0] - 2026-11-04

### Changed

- The package installs Hardcharger server 2.7.0 (Chargewire 2.1.0).

## [2.6.0] - 2026-11-04

### Changed

- The package installs Hardcharger server 2.6.0 (Chargewire 2.0.2).

## [2.5.0] - 2026-11-03

### Changed

- The package installs Hardcharger server 2.5.0 (Chargewire 2.0.2).

## [2.4.1] - 2026-10-29

### Changed

- The package installs Hardcharger server 2.4.1 (Chargewire 2.0.1).

## [2.4.0] - 2026-10-29

### Changed

- The package installs Hardcharger server 2.4.0, whose init SQL can create users and grant access.

## [2.3.0] - 2026-10-29

### Changed

- The package installs Hardcharger server 2.3.0, whose protocol is now named Chargewire.
- `Server` treats any HTTP answer at the protocol root as the server being up, so it also starts
  servers released before the rename (`version="2.2.0"`), which answer that root with 404.

## [2.2.0] - 2026-10-28

### Changed

- The package installs Hardcharger server 2.2.0 (2.1.5 was tagged but never released; 2.2.0
  includes it): storage privileges by kind (`READ` / `WRITE
  OBJECT STORAGE`, `READ` / `WRITE HTTP`) and on locations, DuckLake storage that only DuckLake
  reads and writes, `GRANT ALL` on a schema or catalog including its DDL, and more refused for an
  administrator's token with catalog access rules (see the server's CHANGELOG, "Upgrading").

## [2.1.5] - 2026-10-28

### Changed

- The package installs Hardcharger server 2.1.5 (2.1.3 and 2.1.4 were tagged but never
  released), which fixes concurrent DDL under access control failing with "access control store
  unavailable" while the store was healthy (a multi-threaded dbt run), refuses no `CHECKPOINT`
  right after a statement while instrumentation is on, and refreshes the web UI editor's catalog
  and schema pickers on the schema browser's Refresh.

## [2.1.2] - 2026-10-27

### Changed

- The package installs Hardcharger server 2.1.2. The server itself is unchanged from 2.1.1
  apart from its web UI's DOMPurify update; 2.1.2's other changes are in the desktop app.

## [2.1.1] - 2026-10-27

### Changed

- The package installs Hardcharger server 2.1.1, which raises its open-file soft limit at
  startup so queries over DuckLake tables with many partition files no longer fail with
  "Too many open files" (gizmodata/hardcharger#8).

## [2.1.0] - 2026-10-26

### Changed

- The package installs Hardcharger server 2.1.0, including the fix for name-based bulk
  ingestion reported in hardcharger-adbc#1. The ADBC driver does not need an update.
- Python package versions match server releases. Python-only fixes use `.postN`
  releases and continue downloading the same server version.

## [2.0.1] - 2026-10-22

### Documentation

- Add a total PyPI downloads badge to the package and repository READMEs.
- Use absolute logo image URLs in the repository README.
- This Python package still downloads Hardcharger server 2.0.0 by default.

## [2.0.0] - 2026-10-20

### Changed

- The package's version now tracks the server's: 2.0.0 installs Hardcharger 2.0.0 (the Python
  package was 0.1.0 while the server was pre-release). Both the runtime `__version__` and
  the default downloaded server are 2.0.0; the `[adbc]` extra requires the 2.0.0 driver.
  PyPI metadata identifies the package as stable, and the download documentation uses
  anonymous access with an optional token to raise the API rate limit.

- The server version to download (`SERVER_VERSION`, `HARDCHARGER_VERSION`, `ensure_server(version=...)`)
  may be a release candidate in the server's form (`2.0.0-rc.1`, tag `v2.0.0-rc.1`) or in
  PEP 440's (`2.0.0rc1`, how this package spells its own candidates); both fetch `v2.0.0-rc.1`.
- A `python-v*` GitHub Release is never made the repository's latest release, which stays the
  server's (what `releases/latest` and the installer resolve), and a PEP 440 pre-release
  (`python-v2.0.0rc1`) is marked a pre-release.
- The README's banner is the Hardcharger™ bull logo (the horizontal mascot lockup).

### Documentation

- README, License: the package itself has no third-party code; the server it downloads carries
  `THIRD_PARTY_NOTICES.md` and `THIRD_PARTY_NOTICES-web-ui.txt`, which the install keeps beside
  `bin/` (a unit test checks they arrive), and prints them with `--licenses`.

### Fixed

- CI (`.github/workflows/workflow.yml`) failed on every push to main: the `client` job installs
  the `[adbc]` extra from PyPI, where `adbc-driver-hardcharger` isn't published yet. It runs on
  `python-v*` tags only now, where it still gates the publish.

## [0.1.0] - 2026-10-17

### Added

- `hardcharger.Server` runs `hardcharger-server` as a subprocess: free ports, an
  in-memory or file database, a generated administrator password passed through the
  environment, TLS, extra options; it waits for the server to answer and reports the
  server's output (its first and last lines) when it exits while starting.
- The server binary is downloaded on first use from the GitHub release this package pins
  (0.2.0), checked against GitHub's SHA-256 digest and its own `--version`, unpacked
  safely and cached. While the releases are internal the download uses
  `HARDCHARGER_INSTALL_TOKEN`, `GH_TOKEN` or `gh auth token`, and sends it only to the
  GitHub API. `HARDCHARGER_SERVER_BINARY` runs a local binary instead.
- `Server.connect()` with the `[adbc]` extra (`adbc-driver-hardcharger`).
- The `hardcharger-server` command and `python -m hardcharger download`.

## [0.0.1] - 2026-10-16

### Added

- First release, holding the `hardcharger` name on PyPI. No functionality yet.
