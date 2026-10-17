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
- Instrumentation (license feature `instrumentation`): `enable_instrumentation` records
  instances, sessions, statements and executions (with their queue wait, fetch timeline
  and status) in `_gizmosql_instr`, under 1.x's tables, views and columns, in a DuckDB
  file next to the database (`instrumentation_db_path`) or in a DuckLake or PostgreSQL
  catalog the init SQL attaches (`instrumentation_catalog`, `instrumentation_schema`). A
  killed server's open rows are closed at the next start; a legacy schema with naive
  timestamps is migrated. Queued statements show as `queued` while they wait.
- `instance_tag`, `SET gizmosql.session_tag` and `SET gizmosql.query_tag`, recorded with
  the instance, the session and each statement; `capture_query_profile` (`off`,
  `standard`, `detailed`; also `SET [GLOBAL]`) stores DuckDB's JSON query profile with
  each execution.
- Catalog logging (license feature `instrumentation`): `enable_catalog_logging` also
  writes every log line to `_gizmosql_logs.logs` (1.x's columns), in its own file
  (`log_catalog_db_path`) or a DuckLake or PostgreSQL catalog (`log_catalog`,
  `log_schema`), in batches from a background writer.
- The instrumentation and log catalogs are the server's: admins may read them, nobody may
  write, detach or `USE` them, and non-admins don't see them in metadata.
- Identity provider sign-in (license feature `external_auth`): ID tokens verified with the
  provider's JWKS (`token_jwks_uri`, or the one `token_allowed_issuer`'s OpenID
  configuration names), RS256/384/512 and ES256/384/512, through `token_exchange` or as
  the password of user `token`.
- Browser sign-in (license feature `external_auth`): `oauth_client_id` runs GizmoSQL 1.x's
  OAuth server on `oauth_port` (default 31339) with its routes (`/oauth/initiate`,
  `/oauth/start`, `/oauth/callback`, `/oauth/token/<uuid>`), as a confidential client
  (`oauth_client_secret`, `oauth_scopes`, `oauth_base_url`, `oauth_redirect_uri`,
  `oauth_instance_id` for a shared callback proxy, `oauth_disable_tls` behind a TLS proxy).
- `--verify-tls URL`: fetches a URL with the system's CA bundle (`SSL_CERT_FILE`, else the
  first well-known bundle), as outbound HTTPS does, and exits 0 or 1.
- GizmoSQL 1.x's OpenTelemetry options stop the server when set, rather than being
  ignored (this version has no OpenTelemetry export).
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

- Not yet ported from GizmoSQL 1.x: per-catalog `catalog_access` rules (only "no write
  access anywhere" is enforced), RBAC, OpenTelemetry.
- Browser sign-in has only 1.x's routes on `oauth_port`; Warp SQL's `oauth_browser` flow
  (`/warp/v1/auth/oauth/flows`) needs the Warp SQL SDK to offer it.
- Instrumentation's fetch times are when the server took each batch from DuckDB, not when
  the client received it (the Warp SQL SDK has no delivery callback); its `peer` column
  is empty (the SDK's principal carries no peer address).
- SQL's own catalog functions (`duckdb_databases()`, `information_schema`, `SHOW
  DATABASES`) still list the instrumentation and log catalogs to non-admins (reading
  them is refused).
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
