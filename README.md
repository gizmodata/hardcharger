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

## Access control (Enterprise)

With a GizmoData Enterprise license that includes the `rbac` feature, `rbac_store_uri`
turns on role-based access control: users, roles and grants in SQL, kept in PostgreSQL
(every replica of a cluster names the same database), and checked inside DuckDB against
each statement's plan.

```bash
HARDCHARGER_PASSWORD='…' HARDCHARGER_LICENSE_KEY_FILE=license.jwt \
HARDCHARGER_RBAC_STORE_URI='postgresql://hardcharger:…@db.internal/hardcharger' \
  ./server/build/hardcharger --database my.duckdb
```

```sql
CREATE ROLE analyst;
GRANT SELECT ON SCHEMA analytics TO analyst;              -- every table, now and later
GRANT SELECT (email, signup_ts) ON crm.users TO analyst;  -- two columns only
CREATE USER joe WITH PASSWORD '…';                        -- signs in with it
GRANT analyst TO joe;
CREATE VIEW published.daily AS SELECT … FROM raw.events;  -- read with its owner's rights
GRANT SELECT ON published.daily TO analyst;
REVOKE analyst FROM joe;                                  -- reaches joe's open sessions too
```

The administrator (`username`/`password`, or a sign-in token with role `admin`) holds
every right; everyone else must be a user in the store. Changes reach every replica
within about a second (each polls the store). The store's tables live in the schema
`gizmosql_rbac`, as GizmoSQL 1.x keeps them, so a 1.x store carries over.
`rbac_opa_url` also asks an Open Policy Agent decision URL, which can only refuse.

## Test

The access-control tests need PostgreSQL (they fail, never skip, without it) and a
build that trusts the tests' license key, which must never be shipped:

```bash
docker run -d --name hardcharger-test-pg -e POSTGRES_USER=hardcharger \
  -e POSTGRES_PASSWORD=hardcharger -e POSTGRES_DB=hardcharger \
  -p 127.0.0.1:55433:5432 postgres:17-alpine       # or HARDCHARGER_TEST_RBAC_STORE_URI
cmake -S server -B server/build-test -G Ninja -DHARDCHARGER_TRUST_TEST_LICENSES=ON
cmake --build server/build-test
```

```bash
cd server/tests && uv venv .venv && uv pip install --python .venv/bin/python -r pyproject.toml
HARDCHARGER_TEST_BINARY=../build-test/hardcharger HARDCHARGER_TEST_UNIT_BINARY=../build-test/hardcharger-unit-tests \
  .venv/bin/python -m pytest -q                     # everything, with the 1.x battery
#   needs: docker compose -f docker-compose.yml up -d --wait (PostgreSQL + S3 for the
#   DuckLake suites; -m "not services" without Docker), and ../build/hardcharger (the
#   release build) for the test that it refuses test-signed licenses
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
