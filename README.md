<p align="center">
  <a href="https://gizmodata.com">
    <img src="docs/assets/hardcharger-banner.svg" alt="Hardcharger by GizmoData: the DuckDB 2.0 server that speaks Warp SQL" width="100%">
  </a>
</p>

<p align="center">
  <a href="https://hub.docker.com/r/gizmodata/hardcharger"><img src="https://img.shields.io/badge/dockerhub-image-green.svg?logo=Docker" alt="Docker Hub image"></a>
  <a href="https://github.com/gizmodata/hardcharger/pkgs/container/hardcharger"><img src="https://img.shields.io/badge/github--package-container--image-green.svg?logo=Docker" alt="GitHub container image"></a>
  <a href="https://github.com/gizmodata/hardcharger/releases/latest"><img src="https://img.shields.io/badge/release-binaries-orange.svg?logo=GitHub" alt="Release binaries"></a>
  <a href="https://github.com/gizmodata/warp-sql"><img src="https://img.shields.io/badge/protocol-Warp%20SQL-31a8db.svg" alt="Warp SQL protocol"></a>
  <a href="https://duckdb.org"><img src="https://img.shields.io/badge/engine-DuckDB%202.0-fff000.svg?logo=duckdb&logoColor=black" alt="DuckDB 2.0"></a>
  <a href="https://arrow.apache.org"><img src="https://img.shields.io/badge/wire%20format-Apache%20Arrow-f15a24.svg" alt="Apache Arrow"></a>
  <br>
  <a href="https://github.com/gizmodata/warp-sql-adbc"><img src="https://img.shields.io/badge/ADBC%20driver-Go%20%7C%20Python-blue.svg?logo=Go" alt="ADBC driver"></a>
  <a href="https://github.com/gizmodata/warp-sql-jdbc"><img src="https://img.shields.io/badge/JDBC%20driver-Java-red.svg?logo=openjdk" alt="JDBC driver"></a>
  <a href="https://pypi.org/project/hardcharger/"><img src="https://img.shields.io/badge/PyPI-hardcharger-blue.svg?logo=PyPI" alt="PyPI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-brightgreen.svg" alt="Apache 2.0 license"></a>
  <a href="https://join.slack.com/t/gizmodatacommunity/shared_invite/zt-42jk7kdti-SJp7qk7d6XYCIKfBG~4HrA"><img src="https://img.shields.io/badge/Slack-GizmoData%20Community-4A154B?logo=slack" alt="Slack community"></a>
  <a href="https://gizmodata.com"><img src="https://img.shields.io/badge/by-GizmoData-f15a24.svg" alt="GizmoData"></a>
</p>

<h3 align="center">Fast, secure, remote DuckDB. Every feature included.</h3>

---

## 🏎️ What is Hardcharger?

**Hardcharger** puts [DuckDB 2.0](https://duckdb.org) on the network. Clients connect over
[**Warp SQL**](https://github.com/gizmodata/warp-sql), an open HTTP protocol that moves
[Apache Arrow](https://arrow.apache.org) record batches straight from DuckDB to your
application: no gRPC, no Flight, no sidecars.

It is the successor to [GizmoSQL](https://github.com/gizmodata/gizmosql), rebuilt from
scratch for DuckDB 2.0, and it is free: no editions, no license keys. Everything that was
Enterprise in GizmoSQL is in every Hardcharger build.

- 🦆 **DuckDB 2.0 inside.** A standalone server on DuckDB's v2 C API, not an extension.
- ⚡ **Warp SQL on the wire.** A query and its first rows come back in one HTTP round trip.
  Large results stream as Arrow IPC, with optional zstd, and can be resumed. Bulk ingest
  streams Arrow straight into a table.
- 🔐 **Security in the core, not bolted on.** Token sign-in, TLS and mTLS, and role-based
  access control checked inside DuckDB against each statement's own plan. Also row
  policies, column masking, tenants, SSO (OIDC/JWKS) and a tamper-evident audit log.
- 🔭 **Observable.** Instrumentation and catalog logging into DuckDB or DuckLake, a
  Prometheus `/metrics` endpoint, query profiles and `KILL SESSION`.
- 🧩 **Configured your way.** Flags, `HARDCHARGER_*` environment variables, or a YAML file.
- 🌐 **Ready to scale out.** Results are already described as partitions with locations,
  so distributed query execution can arrive without a protocol change.

## 🚀 Performance

Measured on one machine (Apple M5 Max) against GizmoSQL 1.41 with the same data
([details and method](docs/benchmarks.md)):

| Workload | Hardcharger | GizmoSQL 1.41 | |
|---|---:|---:|---:|
| TPC-H SF10, all 22 queries | **832 ms** | 1,201 ms | **1.44× faster** |
| Fetch 10M rows (1.73 GB of Arrow) | **0.83 s** | 1.32 s | **1.59× faster** |
| 10 concurrent clients | **39.0 queries/s** | 21.9 queries/s | **1.8× the throughput** |
| 10 concurrent clients, p95 latency | **471 ms** | 1,328 ms | **2.8× lower** |

## ⚡ Quick start

### Docker

```bash
docker run --rm -p 31337:31337 -p 31338:31338 \
  -e HARDCHARGER_ADMIN_PASSWORD='change-me' \
  ghcr.io/gizmodata/hardcharger:latest
```

Images are multi-architecture (`linux/amd64`, `linux/arm64`) and published to both
`ghcr.io/gizmodata/hardcharger` and `docker.io/gizmodata/hardcharger`. Port 31337 speaks
Warp SQL; 31338 answers `/health`.

### Binaries

Linux (x86-64 and arm64) and macOS (Apple silicon) builds are attached to every
[release](https://github.com/gizmodata/hardcharger/releases/latest):

```bash
tar -xzf hardcharger-macos-arm64.tar.gz
HARDCHARGER_ADMIN_PASSWORD='change-me' ./hardcharger-macos-arm64/bin/hardcharger-server --database my.duckdb
```

## 🔌 Connect

### Command line

`hardcharger`, the command-line client, ships beside `hardcharger-server` in every tarball
and in the image:

```bash
HARDCHARGER_PASSWORD='change-me' hardcharger --host localhost --username hardcharger \
  --command "SELECT 42 AS answer"
```

```text
┌─────────┐
│ answer  │
│ integer │
├─────────┤
│      42 │
├─────────┤
│ 1 row   │
└─────────┘
```

Without `--command` (or `--file`, or a pipe) it is an interactive shell: history, tab
completion, syntax highlighting, a pager, dot commands (`.help`), and Ctrl-C cancels the
running statement. Output modes: `--csv`, `--json`, `--markdown`, `--table`, or `.mode` for
the rest. It is GizmoSQL's `gizmosql_client`, on Warp SQL
([migrating](docs/migrating-from-gizmosql.md#the-command-line-client)).

### Python (ADBC)

```python
from adbc_driver_warpsql import dbapi

with dbapi.connect(uri="warp://localhost:31337?tls=false", username="hardcharger", password="change-me") as conn:
    with conn.cursor() as cur:
        cur.execute(operation="SELECT 42 AS answer, ? AS greeting", parameters=["hello"])
        print(cur.fetch_arrow_table())
```

The [Warp SQL ADBC driver](https://github.com/gizmodata/warp-sql-adbc) is written in Go and
also loads into any ADBC driver manager (C/C++, R, Rust, ...).

### Java (JDBC)

```java
Properties properties = new Properties();
properties.setProperty("user", "hardcharger");
properties.setProperty("password", "change-me");
properties.setProperty("useEncryption", "false"); // plain HTTP on localhost
try (Connection connection = DriverManager.getConnection("jdbc:warp://localhost:31337", properties);
     ResultSet rows = connection.createStatement().executeQuery("SELECT 42 AS answer")) {
  rows.next();
  System.out.println(rows.getInt("answer"));
}
```

The [Warp SQL JDBC driver](https://github.com/gizmodata/warp-sql-jdbc) is a single jar for Java 17+,
with no gRPC, Netty or protobuf.

### Plain HTTP

Warp SQL is plain HTTP, so `curl` works:

```bash
TOKEN=$(curl -s localhost:31337/warp/v1/auth/token -H 'Content-Type: application/json' \
  -d '{"grant_type": "password", "username": "hardcharger", "password": "change-me"}' | jq -r .access_token)
SESSION=$(curl -s localhost:31337/warp/v1/sessions -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{}' | jq -r .session_id)
curl -s localhost:31337/warp/v1/sessions/$SESSION/statements -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"sql": "SELECT 42 AS answer"}' | jq .result
```

Ask for `Accept: application/vnd.apache.arrow.stream` and the rows arrive as an Arrow IPC
stream in that same response.

## 📦 Everything included

| | |
|---|---|
| 🦆 DuckDB 2.0, DuckLake and DuckDB's extensions | ✅ |
| ⚡ Warp SQL: streamed and resumable results, bind parameters, prepared statements, bulk ingest, zstd | ✅ |
| 🔐 TLS and mTLS, password and token sign-in, SSO via OIDC/JWKS, browser sign-in, authorized e-mail filters | ✅ |
| 👥 Role-based access control: users, roles, grants, ownership, system privileges | ✅ |
| 🛡️ Row policies, column masking, tenants, per-catalog access rules, Open Policy Agent | ✅ |
| 🧾 Tamper-evident audit log | ✅ |
| 🔭 Session and statement instrumentation, catalog logging, query profiles | ✅ |
| 📈 Prometheus `/metrics` and SQL metrics | ✅ |
| 🚦 Statement queue, timeouts, `KILL SESSION`, graceful shutdown | ✅ |
| 💳 License key | Not needed |

## ⚙️ Configuration

Every option is a flag (`--read-only`), an environment variable (`HARDCHARGER_READ_ONLY`)
or a key in a YAML file passed with `--config` (`read_only: true`). Flags win over
environment variables, which win over the file. `hardcharger-server --help` lists them all.

```yaml
# hardcharger.yaml
database: /data/analytics.duckdb
port: 31337
health_port: 31338
log_level: info
print_queries: true
init_sql_commands_file: /etc/hardcharger/init.sql
```

## 👥 Access control

Point `rbac_store_uri` at a PostgreSQL database, the one every replica of a cluster
shares, and manage access in SQL:

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

Each statement is checked inside DuckDB against its own plan, after binding and again
before optimizing, so nested SQL, views and macros can't route around a grant. Anything the
check can't classify is refused. Changes reach every replica within about a second.

## 🏗️ Architecture

```mermaid
flowchart LR
  subgraph Clients
    A[ADBC: Go, Python, C, R, Rust]
    J[JDBC: Java, BI tools]
    H[Any HTTP client]
  end
  subgraph Hardcharger
    W[Warp SQL SDK<br/>HTTP/1.1 + TLS, sessions,<br/>Arrow IPC streaming]
    S[Security<br/>sign-in, RBAC, audit]
    D[(DuckDB 2.0)]
  end
  A & J & H -- "Warp SQL: HTTP + Arrow IPC" --> W
  W --> S --> D
  S -. users, roles, grants .-> P[(PostgreSQL)]
  D -. instrumentation, logs .-> L[(DuckDB / DuckLake)]
```

## 🛠️ Build from source

```bash
git clone --recurse-submodules https://github.com/gizmodata/hardcharger.git
cd hardcharger/server
./scripts/build-deps.sh                      # DuckDB 2.0, Arrow C++ and replxx, pinned
OPENSSL_ROOT_DIR=$(brew --prefix openssl@3) cmake -S . -B build -G Ninja
cmake --build build
```

## 🧪 Test

```bash
docker compose -f server/tests/docker-compose.yml up -d --wait   # PostgreSQL + S3 for the DuckLake suites
docker run -d --name hardcharger-test-pg -e POSTGRES_USER=hardcharger \
  -e POSTGRES_PASSWORD=hardcharger -e POSTGRES_DB=hardcharger \
  -p 127.0.0.1:55433:5432 postgres:17-alpine                       # the access-control store
cd server/tests
uv venv .venv && uv pip install --python .venv/bin/python -r pyproject.toml
.venv/bin/python -m pytest -q                                    # -m "not services" without Docker
```

The Warp SQL conformance suite runs against the same build:

```bash
cd third_party/warp-sql/conformance
uv venv .venv && uv pip install --python .venv/bin/python -e .
WARP_SQL_TEST_SERVER_BINARY=../../../server/build/hardcharger-server \
WARP_SQL_TEST_SERVER_PASSWORD_ENV=HARDCHARGER_ADMIN_PASSWORD \
WARP_SQL_TEST_SERVER_USERNAME=warp HARDCHARGER_ADMIN_USERNAME=warp \
  .venv/bin/python -m pytest -q
```

The client's 1.x shell script runs on its own too (`bash server/tests/test_client_shell.sh`).
Suites that need PostgreSQL or the S3 gateway fail, never skip, when those aren't there.
[BATTERY.md](server/tests/BATTERY.md) tracks GizmoSQL's test battery as ported, file by file.

## 🔁 Coming from GizmoSQL

Hardcharger takes only its own names: `HARDCHARGER_*` variables, `hardcharger_*` SQL
functions, `_hardcharger_*` catalogs. [docs/migrating-from-gizmosql.md](docs/migrating-from-gizmosql.md)
maps every GizmoSQL name to its Hardcharger name.

## 💬 Community and support

- Chat with us in the [GizmoData Community Slack](https://join.slack.com/t/gizmodatacommunity/shared_invite/zt-42jk7kdti-SJp7qk7d6XYCIKfBG~4HrA).
- Report bugs and ask for features in [GitHub issues](https://github.com/gizmodata/hardcharger/issues).
- For support plans, or to have GizmoData run it for you on [GizmoData Cloud](https://gizmodata.com),
  write to [sales@gizmodata.com](mailto:sales@gizmodata.com).

## 📄 License

Apache License 2.0; see [LICENSE](LICENSE).

---

<p align="center">
  <a href="https://gizmodata.com"><img src="docs/assets/gizmodata-logo.png" alt="GizmoData" width="56"></a><br>
  <sub>Built by <a href="https://gizmodata.com">GizmoData</a></sub>
</p>
