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
  <a href="https://pypi.org/project/hardcharger/"><img src="https://img.shields.io/badge/PyPI%20server-hardcharger-blue.svg?logo=PyPI" alt="PyPI: hardcharger (server)"></a>
  <a href="https://pypi.org/project/adbc-driver-hardcharger/"><img src="https://img.shields.io/badge/PyPI%20client-adbc--driver--hardcharger-blue.svg?logo=PyPI" alt="PyPI: adbc-driver-hardcharger (client)"></a>
  <a href="https://github.com/gizmodata/hardcharger-adbc"><img src="https://img.shields.io/badge/ADBC%20driver-hardcharger--adbc-blue.svg?logo=Python" alt="ADBC driver"></a>
  <a href="https://github.com/gizmodata/hardcharger-jdbc"><img src="https://img.shields.io/badge/JDBC%20driver-hardcharger--jdbc-red.svg?logo=openjdk" alt="JDBC driver"></a>
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

Over a real network: two AWS c8gn.8xlarge machines, TLS on both servers, median of five runs
([details and method](docs/benchmarks.md#remote-throughput-after-the-transfer-work-2026-10-17-featurethroughput);
the loads: [partitioned uploads](docs/benchmarks.md#partitioned-uploads-and-split-results-by-default-2026-10-17-featureingest-partitions)):

| Workload | Hardcharger | GizmoSQL 1.42 | |
|---|---:|---:|---:|
| Fetch TPC-H SF10 lineitem (60M rows, 10.5 GB of Arrow) | **0.97 s** | 15.2 s | **15.7× faster** |
| Fetch TPC-H SF1 lineitem (6M rows) | **0.148 s** | 1.74 s | **11.7× faster** |
| Fetch 100M narrow rows | **0.45 s** | 5.43 s | **12.0× faster** |
| Fetch SF1 lineitem across regions (46 ms away) | **1.96 s** | 12.26 s | **6.3× faster** |
| Load SF10 lineitem (bulk ingest, 8 upload parts) | **3.00 s** | 9.44 s | **3.1× faster** |
| Load SF1 lineitem across regions (8 upload parts) | **2.91 s** | 13.94 s | **4.8× faster** |

## ⚙️ How it's fast

Every result travels through an **asynchronous pipeline**: no stage waits for another
unless a queue is full.

```mermaid
flowchart LR
  D[(DuckDB 2.0<br/>parallel execution)] --> P[Produce<br/>Arrow batches,<br/>122,880 rows each]
  P --> Z[Compress<br/>zstd worker pool,<br/>when asked for]
  Z --> T[Encrypt and send<br/>TLS in 1 MiB writes,<br/>overlapped with the next]
  T --> C1[connection 1]
  T --> C2[connection 2]
  T --> CN[connection N]
```

- 🔁 **Overlapped stages.** DuckDB keeps producing while earlier batches are being compressed
  and encrypted, and the socket never waits for the next batch to be built. With TLS on, one
  stream runs as fast as plain HTTP.
- 🧵 **Parallel compression.** zstd runs on a pool of threads, several batches at once,
  and the batches still leave in order.
- 🔀 **Partitioned results.** A client can ask for a large result as several partitions and
  read them over separate connections at once, past the limits of a single TCP stream and a
  single core. The order is kept when the query asks for one. The drivers and the
  `hardcharger` CLI read large results as 8 partitions by default (`result_partitions`,
  `--result-partitions` change it); small results still come as one stream.
- 📦 **Big batches.** Results go out in 122,880-row batches, so per-batch overhead is
  negligible; small queries still answer in one round trip.
- 📥 **Parallel ingest.** Uploads are read in 1 MiB pieces and inserted on all of DuckDB's
  threads, in one transaction: a failed load leaves nothing behind.
- 📤 **Partitioned uploads.** The drivers send a large load as 8 parts over separate
  connections at once (`adbc.warp.ingest.parts`), inserted from all of them in parallel and
  committed as one: all or nothing.

Correctness is never traded for speed: a result is complete only when every partition has
arrived with its end-of-stream marker, so a query that fails mid-stream can never look like
a short, successful one.

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

`ghcr.io/gizmodata/hardcharger:main` (and `:sha-<commit>`) is the development build: the
newest commit on `main` that passed every CI gate, built exactly as a release. Use
`:latest` or a version tag for anything else.

### Python

```bash
pip install hardcharger
```

```python
import hardcharger

with hardcharger.Server(password="change-me") as server:
    print(server.uri)  # hardcharger://127.0.0.1:<port>?tls=false
```

The `hardcharger` package is the **server**: it downloads `hardcharger-server` for your
platform on first use and runs it. The **client** is `adbc-driver-hardcharger` (below);
`pip install "hardcharger[adbc]"` installs both, and `server.connect()` then returns a
connection. See [python/README.md](python/README.md).

### Binaries

Linux (x86-64 and arm64), macOS (Apple silicon) and Windows (x64 and arm64) builds are
attached to every [release](https://github.com/gizmodata/hardcharger/releases/latest):

```bash
tar -xzf hardcharger-macos-arm64.tar.gz
HARDCHARGER_ADMIN_PASSWORD='change-me' ./hardcharger-macos-arm64/bin/hardcharger-server --database my.duckdb
```

On Windows (PowerShell), unzip `hardcharger-windows-amd64.zip` or `-arm64.zip`, or run
`Hardcharger-windows-amd64.msi` / `-arm64.msi`, which installs under Program Files and
puts `hardcharger-server.exe` and the `hardcharger.exe` client on the PATH. Ctrl+C or
Ctrl+Break stops the server as SIGINT or SIGTERM does elsewhere.

```powershell
Expand-Archive hardcharger-windows-amd64.zip -DestinationPath .
$env:HARDCHARGER_ADMIN_PASSWORD = 'change-me'
.\hardcharger-windows-amd64\bin\hardcharger-server.exe --database my.duckdb
```

## 🔌 Connect

### Python (ADBC)

```bash
pip install adbc-driver-hardcharger
```

```python
from adbc_driver_hardcharger import dbapi as hardcharger

with hardcharger.connect(uri="hardcharger://localhost?tls=false", username="hardcharger", password="change-me") as conn:
    with conn.cursor() as cur:
        cur.execute(operation="SELECT 42 AS answer, ? AS greeting", parameters=["hello"])
        print(cur.fetch_arrow_table())
```

`hardcharger://host` is HTTPS on port 31337; `?tls=false` is plain HTTP for local
development. [hardcharger-adbc](https://github.com/gizmodata/hardcharger-adbc) is a native
Go driver, on the [Warp SQL ADBC driver](https://github.com/gizmodata/warp-sql-adbc), with
query and session tags, `hardcharger.*` settings and browser sign-in. Its library loads into
any ADBC driver manager (C/C++, R, Rust, ...) as `driver = "hardcharger"`, with the same
`hardcharger://` URIs, and Go programs import it directly.

### Java (JDBC)

```java
Properties properties = new Properties();
properties.setProperty("user", "hardcharger");
properties.setProperty("password", "change-me");
properties.setProperty("useEncryption", "false"); // plain HTTP on localhost
try (Connection connection = DriverManager.getConnection("jdbc:hardcharger://localhost", properties);
     ResultSet rows = connection.createStatement().executeQuery("SELECT 42 AS answer")) {
  rows.next();
  System.out.println(rows.getInt("answer"));
}
```

[hardcharger-jdbc](https://github.com/gizmodata/hardcharger-jdbc) is a single jar for Java 17+,
with no gRPC, Netty or protobuf: `jdbc:hardcharger://host[:port]` (port 31337 by default) on
the [Warp SQL JDBC driver](https://github.com/gizmodata/warp-sql-jdbc). Its README shows how
to add it to DBeaver and other BI tools.

### JavaScript

```js
import { connect } from "@gizmodata/warp-sql";

const conn = await connect({ url: "warp://localhost:31337?tls=false", username: "hardcharger", password: "change-me" });
const table = await conn.query("SELECT 42 AS answer, ? AS greeting", ["hello"]);
console.log(table.toArray().map((row) => row.toJSON()));
await conn.close();
```

[`@gizmodata/warp-sql`](https://github.com/gizmodata/warp-sql-js) speaks Warp SQL over plain `fetch`
and returns Arrow JS Tables or streams of record batches, in Node, browsers (allow the page's
origin with `--cors-origins`), Deno, Bun and Cloudflare Workers. No native code, no gRPC.

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

## 💻 Command-line client

`hardcharger`, the command-line client, ships beside `hardcharger-server` in every tarball
and in the image. Run it with no statement for an interactive shell (it asks for the
password, or reads `HARDCHARGER_PASSWORD`):

```console
$ hardcharger --host localhost --username hardcharger
Password:
Hardcharger Client 0.2.0
Connected to localhost:31337
Type '.help' for help, '.quit' to exit.

my.main> CREATE OR REPLACE TABLE trips AS SELECT * FROM (VALUES (1, 'Boston', 12.5), (2, 'Denver', 30.0)) t(id, city, miles);
OK, 2 rows affected
my.main> SELECT city, miles FROM trips ORDER BY miles DESC;
┌─────────┬──────────────┐
│  city   │    miles     │
│ varchar │ decimal(3,1) │
├─────────┼──────────────┤
│ Denver  │         30.0 │
│ Boston  │         12.5 │
├─────────┴──────────────┤
│ 2 rows  2 columns      │
└────────────────────────┘
my.main> .quit
```

History, tab completion, syntax highlighting, a pager for long results, dot commands
(`.help`, `.tables`, `.mode`, ...), and Ctrl-C cancels the running statement while the
shell goes on. A large result is read over up to 8 connections at once, in the same order;
`--result-partitions N` (or `HARDCHARGER_RESULT_PARTITIONS`) changes that, and `1` reads every
result over one connection.

One statement with `--command` (`-c`), in any output mode (`--csv`, `--json`,
`--markdown`, `--table`, or `.mode` for the rest):

```console
$ HARDCHARGER_PASSWORD='change-me' hardcharger --host localhost --username hardcharger --csv \
    --command "SELECT city, miles FROM trips ORDER BY id"
city,miles
Boston,12.5
Denver,30.0
```

A script from a heredoc, a pipe or `--file`; the exit status is 1 when a statement fails:

```console
$ export HARDCHARGER_PASSWORD='change-me'
$ hardcharger --host localhost --username hardcharger --quiet <<'SQL'
INSERT INTO trips VALUES (3, 'Austin', 8.5);
SELECT count(*) AS trips, sum(miles) AS miles FROM trips;
SQL
OK, 1 row affected
┌────────┬───────────────┐
│ trips  │     miles     │
│ bigint │ decimal(38,1) │
├────────┼───────────────┤
│      3 │          51.0 │
├────────┴───────────────┤
│ 1 row  2 columns       │
└────────────────────────┘
```

TLS is `--tls` (with `--tls-roots`, or mutual TLS with `--mtls-cert` and `--mtls-key`), and
`--auth-type external` signs in through the server's browser sign-in. A URI takes the place of
`--host`, `--port` and `--tls`, with the Python driver's rules: `hardcharger://db.example.com` is
HTTPS to port 31337, and `hardcharger 'hardcharger://localhost?tls=false' --username hardcharger`
is plain HTTP to a local server. It is GizmoSQL's
`gizmosql_client`, on Warp SQL:
[docs/migrating-from-gizmosql.md](docs/migrating-from-gizmosql.md#the-command-line-client)
maps its options and lists what changed.

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

In SQL, `SET SESSION name = value` changes a DuckDB setting for your session only. A plain
`SET name = value` takes the setting's own default scope, which for about half of DuckDB's
settings (`arrow_lossless_conversion` among them) is `GLOBAL`: then only an administrator
may run it, and it changes the default of every session that has no value of its own.

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

Use PostgreSQL in production, and for any cluster of more than one replica. For
development, or a server that will only ever run one replica, the store can be a DuckDB
file instead:

```yaml
rbac_store_uri: duckdb:/var/lib/hardcharger/rbac.duckdb   # or any path ending in .duckdb
```

The file is created `0600` (one that other users can read or write is refused), only one
server may open it (a second one on the same file stops at startup), and no SQL statement
reaches it, the administrator's included. There is no migration between the two stores:
moving to PostgreSQL later means creating the users, roles and grants there again.

## 🔏 Client certificates as identity

With mutual TLS (`mtls_ca_cert`), a client can sign in with its certificate and no
password: Warp SQL's `client_certificate` grant, `--auth-type mtls` in the `hardcharger`
client. The certificate proves who the user is; grants decide what they may do.

```yaml
tls_cert: /etc/hardcharger/server.pem
tls_key: /etc/hardcharger/server.key
mtls_ca_cert: /etc/hardcharger/client-ca.pem      # client certificates are verified against it
mtls_certificate_sign_in: true                    # off by default: certificates only admit connections
mtls_identity_source: san_email                   # or san_uri (SPIFFE IDs), subject_cn
mtls_admin_identities: [ops@example.com]          # sign in as the administrator
mtls_crl_file: /etc/hardcharger/client-ca.crl     # revocation, reloaded when it changes
```

| Option | Default | Meaning |
|---|---|---|
| `mtls_certificate_sign_in` | `false` | Offer certificate sign-in at all. Off, `mtls_ca_cert` only admits connections, and the grant is answered `unsupported_grant_type` |
| `mtls_require_client_cert` | `true` | Refuse connections without a certificate at the handshake; `false` lets them connect and sign in another way |
| `mtls_identity_source` | `san_email` | Which part of the certificate is the identity. A certificate with none, or several, values of that kind is refused |
| `mtls_admin_identities` | none | Identities that sign in as the administrator. Under access control, any other identity must be a user of the store (`CREATE USER "joe@example.com"`, a password optional); without it, no other identity signs in |
| `mtls_crl_file` | none | PEM CRLs, one for every CA of a client's chain (the root's too): every certificate of the chain is checked, so a revoked intermediate revokes what it issued, and a CA without a CRL is refused. A revoked certificate is refused at the handshake. The file is looked at every 5 seconds and reloaded when it changes, and open connections are checked again then and when a CRL passes its nextUpdate (an expired CRL refuses everything) |
| `mtls_crl_allow_missing_ca_crls` | `false` | Accept a CA above the leaf that has no CRL in the file |
| `mtls_require_identity_match` | `false` | A password or token sign-in over a client certificate must be for the certificate's identity |

A browser page on another origin (one of `cors_origins`) can sign in with the user's
certificate too: allowed origins get `Access-Control-Allow-Credentials: true`, and the page
calls with `credentials: "include"` (`@gizmodata/warp-sql` does with `authType:
"client_certificate"`). The browser's CORS preflight carries no credentials and may come
without a certificate, so for such pages set `mtls_require_client_cert: false`: the
handshake then admits the preflight, and the sign-in still needs the certificate. Pages on
the server's own origin (the embedded UI) send no preflight and need neither.

Tokens from a certificate sign-in are bound to it (RFC 8705's `cnf` claim): they work only on
connections presenting the same certificate, so a copied token is no use without the private
key. A session opened with such a token, and any scoped token minted from it, stay bound to the
same certificate (a session takes only tokens bound as its opener's was, and a bound session
can't mint scoped tokens `FOR USER` someone else). Identities are printable ASCII. Every refusal
(a bound token on the wrong connection, another identity's sign-in, a revoked certificate, an
unknown identity) is a security warning in the log and an audit record, with the user, the
token's id, the certificate presented and the client's address. They are rate-limited per source (ten a minute
in full, then one summary that counts the rest), a connection is closed after five failed
sign-ins, and certificates the TLS handshake refuses (revoked, expired, untrusted) are
recorded too. Identities match user names as user names compare (ignoring ASCII case), except URI identities such as
SPIFFE IDs, whose paths are case-sensitive: they must match exactly, case included. Under access
control a SPIFFE ID is a user name like any other:
`CREATE USER "spiffe://example.org/ns/etl/sa/loader"` (always system scope, never a tenant's).
Of several CRLs from one CA the newest is used; delta CRLs are refused. TLS has to end at Hardcharger (a TCP or TLS-passthrough load balancer) for this; a
proxy that ends TLS takes the certificate with it. Sign-ins with a certificate are written to
the audit log (`kind: authentication`, `method: client_certificate`, the identity and the
certificate's thumbprint).

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

The access-control suites run on PostgreSQL by default; `HARDCHARGER_TEST_RBAC_STORE=duckdb
.venv/bin/python -m pytest -q -m rbac_store` runs them again on the DuckDB file store, as CI does.
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
