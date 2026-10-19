# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Documentation

- README, License: the package itself has no third-party code; the server it downloads carries
  `THIRD_PARTY_NOTICES.md` and `THIRD_PARTY_NOTICES-web-ui.txt`, which the install keeps beside
  `bin/` (a unit test checks they arrive), and prints them with `--licenses`.

### Changed

- The package runs `hardcharger-server` 0.3.0 by default (`SERVER_VERSION`; it was 0.2.0).
  `HARDCHARGER_VERSION` still picks another release. The unit tests now follow
  `SERVER_VERSION` instead of naming the pinned release, so the next bump changes one line.
- The server version to download (`SERVER_VERSION`, `HARDCHARGER_VERSION`, `ensure_server(version=...)`)
  may be a release candidate in the server's form (`2.0.0-rc.1`, tag `v2.0.0-rc.1`) or in
  PEP 440's (`2.0.0rc1`, how this package spells its own candidates); both fetch `v2.0.0-rc.1`.
- A `python-v*` GitHub Release is never made the repository's latest release, which stays the
  server's (what `releases/latest` and the installer resolve), and a PEP 440 pre-release
  (`python-v2.0.0rc1`) is marked a pre-release.
- The README's banner is the Hardcharger™ bull logo (the horizontal mascot lockup).

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
