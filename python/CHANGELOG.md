# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.1.3] - 2026-10-28

### Changed

- The package installs Hardcharger server 2.1.3, which fixes concurrent DDL under access
  control failing with "access control store unavailable" while the store was healthy (a
  multi-threaded dbt run), and refreshes the web UI editor's catalog and schema pickers on the
  schema browser's Refresh.

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
