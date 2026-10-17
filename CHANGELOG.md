# Changelog

All notable changes to the Hardcharger server are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The Python package keeps its
own changelog in `python/CHANGELOG.md`.

## [Unreleased]

### Added

- Role-based access control (Enterprise, license feature `rbac`), lifted from GizmoSQL
  1.x's RBAC: `CREATE USER/ROLE`, `GRANT`/`REVOKE` (roles, table, view, column, schema and
  catalog privileges, `WITH GRANT OPTION`, `WITH ADMIN OPTION`, cascading revokes),
  `SHOW GRANTS`, ownership (`ALTER ... OWNER TO`), views that read with their owner's
  rights, system privileges (`READ FILESYSTEM`, `MANAGE CATALOGS`, ...), row and masking
  policies, tenants, `SET ROLE`, identity-provider group mappings, scoped tokens and an
  Open Policy Agent service. Every statement is checked inside DuckDB against its own
  plan, when it is bound and again before it is optimized; anything the check cannot
  classify is refused. Turned on by `rbac_store_uri` (a PostgreSQL database every replica
  shares, reached through DuckDB's `postgres` extension); `rbac_opa_url`,
  `rbac_opa_timeout_ms` and `token_groups_claim` too, with GizmoSQL 1.x's environment
  names.
- Store users sign in with their password; their access tokens are checked against the
  store on every request, so dropping or disabling a user, changing their password or
  revoking a scoped token ends their sessions and tokens at once, on every replica, and a
  revoke stops a statement that is running.
- Statements that reach outside the database (files and URLs, `ATTACH`, extensions,
  secrets, instance-wide settings, `query()`, table functions not on the allow-list) are
  refused for store users before DuckDB binds them, unless they hold the system privilege
  that opens them. Settings that write files (`log_query_path`, `profiling_output`) need
  WRITE FILESYSTEM, and settings that hold credentials (`http_proxy_password`, ...) need
  MANAGE CONFIG to read.
- A tenant's user is told about another tenant's objects exactly what it is told about
  missing ones, in errors, metadata, `DESCRIBE`, `CREATE VIEW` and session options.
- Access-control audit records form a tamper-evident hash chain (HMAC-SHA256 with
  `audit_chain_key`, SHA-256 without), as in GizmoSQL 1.x; `hardcharger
  --verify-audit-log FILE` checks a JSON log.
- DuckDB is built with a small binder patch (`server/scripts/patch-duckdb.cmake`) that
  lets access control see which part of a plan came from which view.
- Tests for access control (`server/tests/test_rbac_*.py`, 262), including two replicas on
  one store and a gate that access control costs no concurrency. They need PostgreSQL and
  a test-license build (`-DHARDCHARGER_TRUST_TEST_LICENSES=ON`).

### Fixed

- Statement logs no longer show `CREATE SECRET` or `CREATE`/`ALTER USER` text, which can
  carry credentials.

## [0.1.2] - 2026-10-17

0.1.1 was tagged but never released: its release gate failed on a race in deleting a
running write (fixed here). 0.1.2 carries everything listed for it.

### Fixed

- A use-after-free: prepared statements kept parameter types that DuckDB had lent
  them, so closing a prepared statement or ending its session could crash the server or
  corrupt its heap (found by the JDBC driver's tests). Bulk ingest freed borrowed column
  types the same way. Both are fixed, and CI now runs the server tests and the
  conformance suite on an AddressSanitizer build that gates every release.
- Deleting a statement before the engine had said whether it writes could cancel a
  write; such a delete is now settled once it does (a write finishes, a read stops).
- A long poll on a finished statement answered only after its whole wait; it answers at
  once.
- A streamed query sent its Arrow schema only with its first batch, so an aggregate's
  response stayed silent until the query ended and proxies' idle timeouts could cut it;
  the schema now goes first.
- A client that hung up while a query's stream waited for its next batch went unnoticed
  until the query ended; the query is cancelled within a second. A statement that
  changes data (`INSERT … RETURNING`) is never undone because its reader left or deleted
  it: it runs to the end.
- Finished statements stayed in their session forever; they are forgotten 10 minutes
  after they finish (once any retained result has expired), or beyond 1,000 a session.
- Prepared statements describe their result (`result_schema`, `result_kind`) without
  running; executing a query for zero parameter rows returns no rows instead of a count.
- Table schemas from metadata report nullability, precision and scale; the session
  document follows SQL `USE`; a missing or existing table is SQLSTATE 42P01 / 42P07.

### Added

- `POST /warp/v1/sessions/{id}/cancel` stops whatever the session is running.
- Primary and imported keys of every table in one metadata request.

## [0.1.1] - 2026-10-17 [not released]

See 0.1.2.

## [0.1.0] - 2026-10-17

### Added

- The Hardcharger server (`server/`): DuckDB 2.0 behind the Warp SQL protocol, built on
  the Warp SQL server SDK (`third_party/warp-sql`). It passes the Warp SQL conformance
  suite.
- Configuration from flags, environment variables (`HARDCHARGER_*`, plus GizmoSQL 1.x's
  names, so GizmoData Cloud's settings work unchanged) and a YAML file
  (`--config`/`HARDCHARGER_CONFIG`), in that order of precedence; unknown keys are refused.
- Access tokens as JWTs signed with `secret_key` (HS256): valid across restarts and, with
  `allow_cross_instance_tokens`, across replicas. GizmoSQL 1.x-issued server tokens are
  accepted.
- Sign-in with RS256 tokens from a trusted issuer (`token_exchange`, or as the password of
  user `token` as in 1.x), with issuer and audience checks and the `admin`, `user` and
  `readonly` roles; `catalog_access` needs the `catalog_permissions` license feature.
- GizmoData Enterprise license keys, verified exactly as GizmoSQL 1.x does; a bad key
  stops the server.
- Init SQL (commands, then file; any failure stops the server), read-only databases,
  memory limit, storage version, unsigned extensions, a plaintext health port, a default
  statement timeout, statement logging, text or JSON logs, TLS and mTLS, graceful drain.
- Tests (`server/tests`, 27) for all of the above.
- Release artifacts: `hardcharger-linux-amd64`, `-linux-arm64` and `-macos-arm64`
  tarballs (server plus `libduckdb`), and the image `ghcr.io/gizmodata/hardcharger`.
- A TPC-H SF10 benchmark (`bench/tpch.py`, results in `docs/benchmarks.md`): 1.44× faster
  than GizmoSQL 1.41 over the 22 queries, and 1.6× faster at moving 10M rows.

### Known gaps

- The `readonly` role allows only SELECT and EXPLAIN until the authorization module
  brings DuckDB's own read-only analysis.
- Not yet ported from GizmoSQL 1.x: RBAC, instrumentation, metrics, the statement queue,
  KILL SESSION, OAuth sign-in, the admin and sensitive-path guards.
