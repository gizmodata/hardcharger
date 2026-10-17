# Changelog

All notable changes to the Hardcharger server are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The Python package keeps its
own changelog in `python/CHANGELOG.md`.

## [Unreleased]

### Added

- SQL functions for the session's context, as `HARDCHARGER_*` and GizmoSQL 1.x's
  `GIZMOSQL_*` names: `USER()`, `ROLE()`, `VERSION()`, `EDITION()`, `CURRENT_SESSION()`
  (a UUID), `CURRENT_INSTANCE()`, `CURRENT_CLUSTER()`, `INSTRUMENTATION_ENABLED()`,
  `INSTRUMENTATION_CATALOG()`, `INSTRUMENTATION_SCHEMA()`; table functions
  `gizmosql_settings()`, `gizmosql_sessions()` and `gizmosql_metrics()` (and
  `hardcharger_*`).
- Server settings in SQL: `SET [SESSION|GLOBAL] gizmosql.<name>` (or `hardcharger.<name>`)
  and `RESET`, with 1.x's scopes: `query_timeout`, `query_log_level`, `graceful_shutdown`,
  `shutdown_grace_period_seconds`, the statement queue's limits, `bypass_queue`, the
  session and query tags; startup settings say where to change them. `SET GLOBAL` needs
  an admin; Enterprise settings need their license feature.
- `KILL SESSION '<uuid>'` (license feature `kill_session`): ends another session at once,
  frees its `max_sessions` place, and its next request gets `session_ended`.
- `max_sessions` (admins are never capped; `too_many_sessions`), `session_idle_timeout`.
- The admin command guard: users without the admin role can't ATTACH/DETACH, INSTALL or
  LOAD, SET GLOBAL (or instance-wide settings), CHECKPOINT, COPY to or from local files,
  read local files (`read_csv('/x')`, `FROM 'x.csv'`), EXPORT/IMPORT DATABASE, CONNECT,
  manage secrets, or use `quack_*()`; only literal remote URLs pass where a path decides.
- The sensitive path guard (`block_sensitive_paths`, `sensitive_paths`): no statement may
  name credential or system files (`~/.ssh`, `/etc/shadow`, the server's TLS key and
  license, ...), however spelled; `secret_directory` can't change after startup. And
  `allow_unredacted_secrets` can't be turned on (`block_unredacted_secrets`); SQL with a
  NUL byte is refused.
- `_gizmosql_system`, a read-only catalog with `gizmosql_index_info` and
  `gizmosql_view_definition`.
- The statement queue (license feature `statement_queue`): `max_concurrent_statements`,
  `max_queued_statements`, `max_queue_wait`, `admin_bypass_queue_default`.
- Metrics (license feature `metrics`): `enable_metrics`, `GET /metrics` on `metrics_port`
  under 1.x's metric names, `gizmosql_metrics()`, the unclean-exit marker.
- The health check runs `health_check_query` in the background every
  `health_check_interval_seconds`; a probe answers from the last outcome, which goes stale
  after `health_check_staleness_seconds`.
- Graceful shutdown: `graceful_shutdown` (off: the first signal stops at once) and
  `shutdown_grace_period_seconds` (0: wait forever), both adjustable live with
  `SET GLOBAL`; a second signal stops at once.
- `token_authorized_emails`: which users a sign-in token may name.
- `auth_log_level`, `query_log_level`, `session_log_level`, `log_file`; a bearer token's
  first use logs at INFO, its repeats at DEBUG; JSON lines carry `instance_id` and
  `cluster_id`.
- `cluster_id`, `max_header_bytes` (1.x's `--max-metadata-size`), `result_batch_rows`
  (default 2048, as 1.x streamed), and GizmoSQL 1.x's flags as aliases (`--readonly`,
  `-D`, `--tls CERT KEY`, `--query-timeout`, ...).
- GizmoSQL 1.x options this version doesn't implement yet (instrumentation, catalog
  logging, OAuth, JWKS, OpenTelemetry) stop the server when set, rather than being ignored.
- The GizmoSQL 1.x test battery, ported (`server/tests`, scoreboard in
  `server/tests/BATTERY.md`), with C++ unit tests (`server/unit`, googletest 1.18.0) and a
  never-installed `hardcharger-license-test` build for the Enterprise features.

### Changed

- `drain_seconds` is now `shutdown_grace_period_seconds` (default 300, as in 1.x).
- Table schemas from `metadata/tables?include_schema=true` carry nullability, column
  comments, auto-increment and defaults (`GIZMOSQL:COLUMN_DEFAULT`).
- `reject_unknown_sessions` is accepted only as true (setting it false stops the
  server): Warp SQL never recreates a session implicitly.

### Fixed

- (Warp SQL SDK) A server stopped with a request in flight no longer crashes; a client
  that leaves before an unretained streamed result's first batch now cancels it; a
  long-poll on a finished statement answers at once; dictionary-encoded parameters bind;
  ingest turns null-typed columns into VARCHAR, converts day_time intervals and refuses
  types DuckDB can't store; listing catalogs no longer opens every attached catalog.

### Known gaps

- Not yet ported from GizmoSQL 1.x: instrumentation and catalog logging, OAuth sign-in,
  per-catalog `catalog_access` rules (only "no write access anywhere" is enforced), RBAC.
- Listing the schemas or tables of one catalog still opens every attached catalog: the v2
  C API has no catalog-scoped listing.
- VARIANT (and other struct or extension) Arrow parameters can't be bound yet.
- `glob()` over a directory holding a protected file is refused, not filtered; a path an
  admin computes at run time is not checked (no file-system hook in the v2 C API).

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
