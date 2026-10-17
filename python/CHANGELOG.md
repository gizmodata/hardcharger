# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
