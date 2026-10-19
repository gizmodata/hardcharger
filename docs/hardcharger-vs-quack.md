# Hardcharger and Quack: how the comparison was checked

The README's [Why Hardcharger instead of Quack?](../README.md#-why-hardcharger-instead-of-quack)
table was checked on 2026-10-19 against the source of both projects and against running servers.
This page gives the versions and the checks, so anyone can repeat them. It compares features
only; no throughput figures.

## Versions

- **Quack:** extension commit `e7a9be3f8c`, the one DuckDB's
  `.github/config/extensions/quack.cmake` pins (on DuckDB's `main` and in the DuckDB 2.0 build
  Hardcharger uses). `INSTALL quack` on the newest DuckDB 2.0 wheel,
  `duckdb==2.0.0.dev2610162032` (v2.0.0-alpha48730), installs that same commit. The
  [duckdb-quack](https://github.com/duckdb/duckdb-quack) `main` branch was 11 commits further on
  (`f96420ed`, catalog snapshots and transaction cleanup); none of them change a row of the table.
- **Hardcharger:** the release build (`hardcharger-macos-arm64.tar.gz`).

## The Quack server

One DuckDB process serves an in-memory database, plain HTTP on localhost:

```python
import duckdb

server = duckdb.connect()
server.execute(query="LOAD quack")
server.execute(query="CREATE TABLE t AS SELECT range AS i, 'row ' || range AS s FROM range(10)")
server.execute(query="CALL quack_serve('quack:localhost:9494', token => 'super_secret')")
```

The client is a second DuckDB process:

```python
import duckdb

client = duckdb.connect()
client.execute(query="LOAD quack")
client.execute(query="ATTACH 'quack:localhost:9494' AS remote (TYPE quack, token 'super_secret')")
```

## What each row rests on

**`ATTACH` and joins with local tables.** Through Quack, `SELECT l.name, r.s FROM local_names l
JOIN remote.t r USING (i)` returns the joined rows. Hardcharger gets the same through an ADBC
extension; see [DuckDB to Hardcharger through ADBC](#duckdb-to-hardcharger-through-adbc) below.

**Drivers, result format.** Quack's messages (`src/include/quack_message.json`) carry DuckDB's
own serialized data chunks, and DuckDB Labs ships no driver other than the extension. Hardcharger's
results and uploads are Arrow IPC streams, read by
[hardcharger-adbc](https://github.com/gizmodata/hardcharger-adbc),
[hardcharger-jdbc](https://github.com/gizmodata/hardcharger-jdbc),
[`@gizmodata/hardcharger`](https://github.com/gizmodata/hardcharger-js) and plain HTTP.

**Browsers.** Quack ships DuckDB-Wasm builds of the extension (`wasm_eh`, `wasm_mvp`), and its
endpoint answers a CORS preflight from any origin with `Access-Control-Allow-Origin: *`.
Hardcharger's JavaScript client runs in browsers, for the origins listed in `--cors-origins`.

**Bind parameters.** Quack's `PrepareRequestMessage` holds `sql_query`, `query_uuid` and
`inline_rows`, with no field for values. On the attached catalog, with DuckDB's log on
(`CALL enable_logging('Quack')`):

| Client statement | What the client sent the server | Result |
|---|---|---|
| `SELECT s FROM remote.t WHERE i = 3` | `SELECT s FROM t WHERE (i = 3)` | `row 3` |
| `SELECT s FROM remote.t WHERE i = ?` with `[3]` | `SELECT s FROM t WHERE (i = $1)` | Invalid Input Error: Values were not provided for the following parameters: 1 |
| the same, after `SET disabled_optimizers = 'remote_pushdown'` | `SELECT #1, #2 FROM main.t` | `row 3`, filtered by the client |

`quack_query(uri, 'SELECT ? AS x')` fails the same way and has no argument for values.
Hardcharger takes values beside the SQL (Warp SQL §6.1): `{"sql": "SELECT s FROM t WHERE i = $i",
"parameters": {"i": 3}}` returns `row 3`.

**Bulk ingest, cancellation.** Quack streams `CREATE TABLE remote.t AS SELECT …` and `INSERT`
rows to the server (`SEND_DATA` messages; [docs/benchmarks.md](benchmarks.md) loads TPC-H
lineitem this way), and has a `CANCEL` message: interrupting the client stops the server's
query.

**TLS.** With `disable_ssl => false, ssl_cert_file => …, ssl_key_file => …`, the Quack server
negotiated TLS 1.3. A client that pins the certificate's SHA-256 fingerprint (`ssl_fingerprint`)
connects and one with a wrong pin is refused; without a pin, the client checks the certificate
against the system roots, or against a private CA after `SET ca_cert_file = '…'`. Without
certificate files the server generates a self-signed pair.

**Users and sign-in.** Quack's default `quack_check_token` compares the client's token with the
server's one token. Pointing `quack_authentication_function` at a macro over a table of tokens
let two users in and refused a third. The callback returns only allow or refuse, and DuckDB's
[security page](https://duckdb.org/docs/current/quack/security) says per-user access lists need
an extension that records each session's user.

**Client certificates.** The Quack server is an `SSLServer` built from a certificate and a key
only. `openssl s_client` against it prints "No client certificate CA names sent"; against
Hardcharger with `--mtls-ca-cert` it prints "Acceptable client certificate CA names".

**Access control.** For each statement Quack runs `SELECT <quack_authorization_function>(connection
id, statement text)`; the default, `quack_nop_authorization`, allows everything, so an
authenticated client read every table and `read_text('/etc/hosts')` on the server. DuckDB's docs
call their read-only example "illustrative, not a robust read-only filter". `CREATE ROLE` and
`GRANT` are syntax errors in DuckDB. On Hardcharger, a user granted `SELECT` on one table got
`permission_denied` for another table and for `read_text()`.

**Observability.** Quack's log type records each message's type, connection, SQL, duration and
error; `quack_active_connections()` lists connections and their current SQL. The source has no
metrics endpoint or audit log. Hardcharger served `hardcharger_statements_total` on
`/metrics` (`--enable-metrics --metrics-port …`) and recorded statements in
`_hardcharger_instr.sql_statements` (`--enable-instrumentation`).

**Limits.** Quack's server settings are `quack_server_max_connections` (1024),
`quack_server_keep_alive_timeout`, buffer sizes and the result cache; `quack_cancel(catalog,
connection_id)` cancels another connection's query. Hardcharger cancelled a statement at
`--query-timeout-seconds 2` with `statement_timeout`, and `KILL SESSION` ended another user's
session (`session_ended`).

**Packaging.** Quack is served by `CALL quack_serve(...)` inside a DuckDB process; the repository
has no server binary or image. DuckDB's extension repository has the extension for Linux, macOS
and Windows (amd64 and arm64).

**Protocol.** Client and server agree on a version at connection (`QUACK_VERSION = 3`; the server
accepts exactly its own). DuckDB's docs say "the protocol, function names, settings, and defaults
are still subject to change". Warp SQL has a written specification
(`third_party/warp-sql/spec/warp-sql.md`, version 1.0.0, changed since only by addition) and a conformance suite that
CI runs against every Hardcharger build.

**Licenses.** Quack: MIT. Hardcharger: Apache 2.0.

## DuckDB to Hardcharger through ADBC

DuckDB reaches Hardcharger through either of two community extensions, each loading the
[hardcharger-adbc](https://github.com/gizmodata/hardcharger-adbc) driver library
(`libadbc_driver_hardcharger`, entrypoint `AdbcDriverHardchargerInit`, from a release tarball or
`scripts/build_lib.sh`). Checked with hardcharger-adbc `9a087be` against Hardcharger:

| | Columnar's `adbc` | Query.Farm's `adbc_scanner` |
|---|---|---|
| Checked on | DuckDB 1.5.6 (`INSTALL adbc FROM community`, `d4b19a7`); DuckDB 2.0 (`v2.0.0-alpha48624`, the extension built from its `main`, `b1b13c0`) | DuckDB 1.5.6 (`INSTALL adbc_scanner FROM community`, `9723c3c`) |
| `ATTACH`, tables listed, join with local tables | yes | yes |
| A filter on an attached table | DuckDB applies it; the server gets `SELECT "i", "s" FROM "main"."t"` | sent to the server: `SELECT * FROM "t" WHERE "i" = ?`, with the value bound |
| SQL passed through | `read_adbc(profile, sql)`, no parameters | `adbc_scan(db, sql, params := row(…))` |
| Writes | `CREATE TABLE hc.main.x AS …` | `INSERT INTO hc.main.x VALUES (?, ?)` (no `CREATE TABLE`) |

Neither extension is in DuckDB's community repository for DuckDB 2.0 yet (both answer 404 for
`v2.0.0-alpha48624` and `v2.0.0-alpha48730`); Query.Farm's has a `v2.0` branch, not tried here.

Both find the driver by name through its manifest: fill in
[hardcharger.toml.in](https://github.com/gizmodata/hardcharger-adbc/blob/main/packaging/hardcharger.toml.in)
with the library's directory and install it as `hardcharger.toml` where the hardcharger-adbc README
says (or in a directory on `ADBC_DRIVER_PATH`). With `adbc_scanner`:

```sql
INSTALL adbc_scanner FROM community;
LOAD adbc_scanner;
ATTACH 'hardcharger://localhost?tls=false' AS hc
    (TYPE adbc, driver 'hardcharger', username 'hardcharger', password 'change-me');
CREATE TABLE local_names AS SELECT * FROM (VALUES (1, 'one'), (2, 'two')) v(i, name);
SELECT l.name, r.s FROM local_names l JOIN hc.main.t r USING (i) ORDER BY i;
PREPARE q AS SELECT s FROM hc.main.t WHERE i = $1;
EXECUTE q(3);                                              -- the filter runs on the server
SELECT * FROM adbc_scan('hc', 'SELECT s FROM t WHERE i = ?', params := row(3));
```

Columnar's `adbc` takes an ADBC connection profile instead, `hc.toml` in the profiles directory its
README names (or on `ADBC_PROFILE_PATH`):

```toml
profile_version = 1
driver = "hardcharger"

[Options]
uri = "hardcharger://localhost?tls=false"
username = "hardcharger"
password = "change-me"
```

```sql
INSTALL adbc FROM community;
LOAD adbc;
ATTACH 'profile://hc' AS hc (TYPE adbc);
CREATE TABLE local_names AS SELECT * FROM (VALUES (1, 'one'), (2, 'two')) v(i, name);
SELECT l.name, r.s FROM local_names l JOIN hc.main.t r USING (i) ORDER BY i;
SELECT * FROM read_adbc('profile://hc', 'SELECT s FROM t WHERE i = 3');
```

DuckDB's Quack security page also says "The server does not use TLS itself". The extension's
source and the running server above do use TLS, so the table follows them.
