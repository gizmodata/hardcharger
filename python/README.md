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
  <a href="https://github.com/gizmodata/warp-sql"><img src="https://img.shields.io/badge/protocol-Warp%20SQL-31a8db.svg" alt="Warp SQL protocol"></a>
  <a href="https://github.com/gizmodata/hardcharger/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-brightgreen.svg" alt="Apache 2.0 license"></a>
  <a href="https://gizmodata.com"><img src="https://img.shields.io/badge/by-GizmoData-f15a24.svg" alt="GizmoData"></a>
</p>

# hardcharger

Run [Hardcharger](https://github.com/gizmodata/hardcharger), GizmoData's DuckDB 2.0
server, from Python. The package has no dependencies: on first use it downloads the
`hardcharger-server` release for your platform (Linux x86-64 and arm64, macOS on Apple
silicon), checks it, caches it, and runs it as a subprocess. That makes a real server one
line away in tests, notebooks and scripts.

Python package 2.0.1 downloads Hardcharger server 2.0.0 by default.

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
Hardcharger 0.3.0
$ python -m hardcharger download       # download (if needed) and print the binary's path
/home/me/.cache/hardcharger/v0.3.0/linux-amd64/bin/hardcharger-server
```

## The download

The release comes from [gizmodata/hardcharger](https://github.com/gizmodata/hardcharger/releases)
through the GitHub API. Its SHA-256 must match the digest GitHub recorded for the asset,
and the unpacked server must report the expected version, or nothing is installed.

Public releases download anonymously. An optional GitHub token raises the API rate limit:
`HARDCHARGER_INSTALL_TOKEN`, else `GH_TOKEN`, else the token of a signed-in
[`gh`](https://cli.github.com) (`gh auth token`). Set `HARDCHARGER_INSTALL_TOKEN=none` to
force an anonymous download. The token is sent only to the GitHub API, never to the
storage host the download is redirected to, and is never printed.

| Variable | Meaning |
|---|---|
| `HARDCHARGER_SERVER_BINARY` | run this `hardcharger-server` instead of downloading one |
| `HARDCHARGER_VERSION` | the server version to download (default: the one this package pins) |
| `HARDCHARGER_CACHE_DIR` | where downloads are kept (default `~/.cache/hardcharger`) |
| `HARDCHARGER_INSTALL_TOKEN` | the GitHub token for the download (`none`: anonymous) |

## Tests

```bash
uv venv && uv pip install -e . --group dev
.venv/bin/python -m pytest -m "not integration and not download"      # no server needed
HARDCHARGER_PY_TEST_SERVER_BINARY=/path/to/hardcharger-server \
  .venv/bin/python -m pytest -m "integration and not client"           # the real server
.venv/bin/python -m pytest -m download                                 # a real download
```

The `client` tests also need `adbc-driver-hardcharger` installed. The integration, client
and download tests fail, never skip, without their server, client or GitHub access.

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
