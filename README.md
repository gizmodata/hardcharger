# Hardcharger

Hardcharger is GizmoData's analytics server: DuckDB 2.0 behind the
[Warp SQL protocol](https://github.com/gizmodata/warp-sql) (HTTP + Arrow, no gRPC). It is the
successor to GizmoSQL.

| Path | What |
|---|---|
| `server/` | The server (C++20) and its tests |
| `third_party/warp-sql` | The Warp SQL spec, server SDK, DuckDB backend and conformance suite (submodule) |
| `python/` | The `hardcharger` Python package (placeholder) |

## Build

```bash
git submodule update --init
cd server
./scripts/build-deps.sh                      # DuckDB 2.0 and Arrow C++ (pinned)
OPENSSL_ROOT_DIR=$(brew --prefix openssl@3) cmake -S . -B build -G Ninja
cmake --build build
```

## Run

```bash
HARDCHARGER_PASSWORD='…' ./server/build/hardcharger --database my.duckdb
```

Every option is a flag (`--read-only`), an environment variable (`HARDCHARGER_READ_ONLY`)
or a key in a YAML file passed with `--config` (`read_only: true`); `hardcharger --help`
lists them. GizmoSQL 1.x's environment variable names work too.

```yaml
# hardcharger.yaml
database: /data/analytics.duckdb
port: 31337
health_port: 31338
log_level: info
print_queries: true
init_sql_commands_file: /etc/hardcharger/init.sql
```

## Test

```bash
cd server/tests && uv venv .venv && uv pip install --python .venv/bin/python -r pyproject.toml
docker compose -f docker-compose.yml up -d --wait   # PostgreSQL + S3 for the DuckLake suites
.venv/bin/python -m pytest -q                       # Hardcharger's tests and the ported 1.x battery
.venv/bin/python -m pytest -q -m "not services"     #   ... without Docker
cd ../../third_party/warp-sql/conformance           # the Warp SQL conformance suite
WARP_SQL_TEST_SERVER_BINARY=../../../server/build/hardcharger \
WARP_SQL_TEST_SERVER_PASSWORD_ENV=HARDCHARGER_PASSWORD \
WARP_SQL_TEST_SERVER_USERNAME=warp HARDCHARGER_USERNAME=warp \
  .venv/bin/python -m pytest -q
```

The build also makes `hardcharger-unit-tests` (googletest; the pytest suite runs it) and
`hardcharger-license-test`, a server that also trusts a license key named by its
environment, so the tests can exercise Enterprise features with keys they sign. It is
never installed or shipped. `server/tests/BATTERY.md` tracks the GizmoSQL 1.x test
battery file by file.
