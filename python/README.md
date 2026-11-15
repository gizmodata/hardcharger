<p align="center">
  <a href="https://gizmodata.com">
    <img src="https://raw.githubusercontent.com/gizmodata/hardcharger/main/docs/assets/hardcharger-banner-light.png" alt="Hardcharger™" width="600">
  </a>
</p>

<p align="center">
  <a href="https://pypi.org/project/hardcharger/"><img src="https://img.shields.io/pepy/dt/hardcharger.svg" alt="PyPI Downloads"></a>
  <a href="https://pypi.org/project/hardcharger/"><img src="https://img.shields.io/badge/PyPI-hardcharger-blue.svg?logo=PyPI" alt="PyPI: hardcharger"></a>
  <a href="https://pypi.org/project/adbc-driver-hardcharger/"><img src="https://img.shields.io/badge/PyPI-adbc--driver--hardcharger-blue.svg?logo=PyPI" alt="PyPI: adbc-driver-hardcharger"></a>
  <a href="https://github.com/gizmodata/hardcharger"><img src="https://img.shields.io/badge/server-Hardcharger-f15a24.svg" alt="Hardcharger"></a>
  <a href="https://github.com/gizmodata/chargewire"><img src="https://img.shields.io/badge/protocol-Chargewire-31a8db.svg" alt="Chargewire protocol"></a>
  <a href="https://github.com/gizmodata/hardcharger/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-brightgreen.svg" alt="Apache 2.0 license"></a>
  <a href="https://gizmodata.com"><img src="https://img.shields.io/badge/by-GizmoData-f15a24.svg" alt="GizmoData"></a>
</p>

# hardcharger

Run [Hardcharger](https://github.com/gizmodata/hardcharger), GizmoData's DuckDB 2.0
server, from Python. The package has no dependencies: on first use it downloads the
`hardcharger-server` release for your platform (Linux x86-64 and arm64, macOS on Apple
silicon), checks it, caches it, and runs it as a subprocess. That makes a real server one
line away in tests, notebooks and scripts.

Python package 2.13.0 downloads Hardcharger server 2.13.0 by default.

| Package | What it is |
|---|---|
| `pip install hardcharger` | this package: the **server** |
| `pip install adbc-driver-hardcharger` | the **client**, an ADBC/DB-API driver |
| `pip install "hardcharger[adbc]"` | both, so `Server.connect()` returns a connection |

## Quick start

```python
import hardcharger

with hardcharger.Server() as server, server.connect() as conn, conn.cursor() as cur:
    cur.execute(operation="SELECT 42 AS answer, HARDCHARGER_VERSION() AS version")
    print(cur.fetch_arrow_table())
```

`Server()` listens on a free port on 127.0.0.1, with an in-memory database and an
administrator `hardcharger` whose password it generates (`server.password`). The
credentials reach the server through its environment, so they don't show in `ps`.

Hand the server to any other client by its URI:

```python
import hardcharger

with hardcharger.Server(database="analytics.duckdb", password="change-me") as server:
    print(server.uri)  # hardcharger://127.0.0.1:<port>?tls=false
    print(server.username, server.port, server.health_port)
```

TLS is `tls_cert=` and `tls_key=` (the URI drops `?tls=false`, and `connect()` trusts
that certificate); any other server option goes in `extra_args=[...]` or, as
`HARDCHARGER_*` variables, in `env={...}`. `hardcharger-server --help` lists them all.

## The command line

The package also installs `hardcharger-server`, which runs the downloaded server:

```console
$ hardcharger-server --version
Hardcharger 2.13.0
$ python -m hardcharger download       # download (if needed) and print the binary's path
/home/me/.cache/hardcharger/v2.13.0/linux-amd64/bin/hardcharger-server
```

## The download

Starting with **2.1.0**, the `hardcharger` PyPI package version matches the server it
downloads: `hardcharger==2.1.0` uses server `2.1.0`. A Python-only fix uses a post-release,
such as `2.1.0.post1`, and still downloads server `2.1.0`. Release candidates use PEP 440
(`2.1.0rc1`) for Python and `2.1.0-rc.1` for the server. The separate ADBC driver has its
own release cycle.

The server comes from a public source. No account, token or Docker is needed:

1. **GitHub Releases** of [gizmodata/hardcharger](https://github.com/gizmodata/hardcharger/releases).
   The release tarball is fetched from its direct download URL
   (`https://github.com/gizmodata/hardcharger/releases/download/v<version>/hardcharger-<platform>.tar.gz`),
   which uses neither the GitHub API nor its rate limit, and its SHA-256 must match the
   release's `SHA256SUMS`.
2. **The container image** `ghcr.io/gizmodata/hardcharger:<version>`, on Linux, when GitHub
   can't be reached or refuses: a sandbox that allows PyPI and ghcr.io but not github.com,
   for example. The launcher reads the image through the registry's HTTP API (an anonymous
   pull token, the manifest for this architecture), checks every layer against its digest
   and takes the server's files out of the layers. They are the files of the release
   tarball, byte for byte, and run outside the image as the tarball's do. The image holds
   Linux binaries, so on macOS the server comes from GitHub only.

Whatever the source, the unpacked server must report the expected version, or nothing is
installed. A download that doesn't match its checksum or digest stops the install. A source
that can't be reached or refuses is passed over, with a line on stderr saying why, and when
every source fails the error names each one and its reason: the HTTP status, a rate limit
and when it resets, the connection error, the host a redirect led to.

`HARDCHARGER_INSTALL_SOURCE` picks one source: `github`, or `ghcr` for the container image
alone (Linux):

```bash
HARDCHARGER_INSTALL_SOURCE=ghcr python -m hardcharger download
```

`HARDCHARGER_INSTALL_REGISTRY_IMAGE` names another copy of the image, without a tag (the
server version is the tag): `docker.io/gizmodata/hardcharger`, or a mirror in an air-gapped
network such as `registry.example.com/mirror/hardcharger` (`http://` in front for a registry
without TLS). The image must be one that can be pulled without signing in.

A GitHub token is optional: `HARDCHARGER_INSTALL_TOKEN`, else `GH_TOKEN`, else the token of
a signed-in [`gh`](https://cli.github.com) (`gh auth token`). With one, the GitHub API is
asked first, which is how a GitHub Enterprise Server or a private copy of the repository is
read (`HARDCHARGER_RELEASE_API_URL`, `HARDCHARGER_RELEASE_REPO`). A token the API refuses
(expired, or one for something else) is not the end: the launcher says so and downloads
without it. `HARDCHARGER_INSTALL_TOKEN=none` never sends a token. The token is sent only
to the GitHub API, never to the host a download is redirected to nor to the container
registry, and is never printed.

| Variable | Meaning |
|---|---|
| `HARDCHARGER_SERVER_BINARY` | run this `hardcharger-server` instead of downloading one |
| `HARDCHARGER_VERSION` | the server version to download (default: the one this package pins) |
| `HARDCHARGER_CACHE_DIR` | where downloads are kept (default `~/.cache/hardcharger`) |
| `HARDCHARGER_INSTALL_SOURCE` | `auto` (default: GitHub, then on Linux the container image), `github` or `ghcr` |
| `HARDCHARGER_INSTALL_REGISTRY_IMAGE` | the container image, without a tag (default `ghcr.io/gizmodata/hardcharger`) |
| `HARDCHARGER_INSTALL_TOKEN` | a GitHub token for the GitHub API (`none`: never send one) |
| `HARDCHARGER_RELEASE_API_URL` | the GitHub API's base URL (default `https://api.github.com`), e.g. GitHub Enterprise Server |
| `HARDCHARGER_RELEASE_DOWNLOAD_URL` | the base of the direct download URLs (default `https://github.com`); with only the API URL set, the API is used |
| `HARDCHARGER_RELEASE_REPO` | the repository the releases come from (default `gizmodata/hardcharger`) |

## Tests

```bash
uv venv && uv pip install -e . --group dev
.venv/bin/python -m pytest -m "not integration and not download"      # no server needed
HARDCHARGER_PY_TEST_SERVER_BINARY=/path/to/hardcharger-server \
  .venv/bin/python -m pytest -m "integration and not client"           # the real server
.venv/bin/python -m pytest -m download                                 # real downloads
```

The unit tests use a fake GitHub and a fake container registry on 127.0.0.1 and never the
network. The `client` tests also need `adbc-driver-hardcharger` installed. The integration,
client and download tests fail, never skip, without their server, client or access to
GitHub and ghcr.io.

## License

Apache License 2.0.

This package is Python only and contains no third-party code. The `hardcharger-server` it
downloads does: its release archive's `THIRD_PARTY_NOTICES.md` and
`THIRD_PARTY_NOTICES-web-ui.txt`, which the install keeps beside `bin/`
(`~/.cache/hardcharger/v<version>/<platform>/`), list that software and its licences, and
`hardcharger-server --licenses` prints them.

---

<p align="center">
  <sub>Built by <a href="https://gizmodata.com">GizmoData</a></sub>
</p>
