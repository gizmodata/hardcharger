# Changelog

All notable changes to the Hardcharger server are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The Python package keeps its
own changelog in `python/CHANGELOG.md`.

## [Unreleased]

### Added

- `hardcharger`, the command-line SQL client: GizmoSQL 1.x's `gizmosql_client` on Warp
  SQL, with the same options (in Hardcharger's names: `HARDCHARGER_HOST`, `_PORT`, `_USER`,
  `_PASSWORD`, `_TLS`, `_TLS_ROOTS`, `_OAUTH_PORT`, `_SESSION_TAG`, `_QUERY_TAG`;
  `hardcharger://` URIs; `~/.hardchargerrc`, `~/.hardcharger_history`), dot commands, output
  modes, heredoc/pipe/`-c`/`-f` scripting and exit codes, the pager, history, completion,
  syntax highlighting, query and session tags, TLS and mutual TLS, and the server's
  browser sign-in (`--auth-type external`). Ctrl-C cancels the running statement through
  Warp SQL's cancel and the shell goes on. It ships in the tarballs' `bin/` next to
  `hardcharger-server`, and the image has both on `PATH` (the entrypoint stays the server).
  docs/migrating-from-gizmosql.md maps the names and lists what behaves differently.
- GizmoSQL 1.x's client tests, ported: `test_interactive_client.cpp` (115 cases, as C++
  against the client's classes), `test_client_cli.cpp` (18), `test_client_config.cpp` (5),
  the 4 client cases of `test_instance_session_query_tags.cpp`, and `test_client_shell.sh`
  (64 assertions), all against a real hardcharger-server; plus the shell at a terminal
  (pexpect), TLS, mutual TLS, browser sign-in and session-recovery cases.
- replxx (release-0.0.4), the client's line editor, built by `scripts/build-deps.sh` and
  cached with DuckDB and Arrow.
- Results split into partitions that a client reads at once over separate connections
  (Warp SQL §6.10, capability `result_partitions`): `max_result_partitions` (default 32,
  1 never splits) caps the split. With Warp SQL's drivers' `result_partitions` option,
  one large result uses several network flows and client cores.

- `hardcharger-server --print-options-json`: every option with its group, flag,
  `HARDCHARGER_*` variable, YAML key, type, default and description, as JSON. The
  documentation's configuration reference is generated from it, so it can't drift from the
  server (`test_server_cli.py` checks it lists exactly what `--help` lists).

- `hardcharger --result-partitions N` (`HARDCHARGER_RESULT_PARTITIONS`): the command-line
  client reads a large result over up to N connections at once, in the same order.

### Changed

- `result_batch_rows` defaults to 122,880 (one DuckDB row group) instead of 2,048:
  SF10 lineitem over TLS 4.6 s -> 3.5 s, with zstd 26.4 s -> 6.3 s; small results are
  unchanged (one batch), a full scan's first batch takes 21 ms instead of 2 ms.
- Data transfer, from the Warp SQL SDK: TLS result streams encrypt whole gathered writes
  and overlap encrypting, encoding and producing (a TLS stream went from about a third
  of plaintext to the same speed on one AWS connection); uploads are read in 1 MiB pieces (plaintext
  ingest was slower than TLS); bulk ingest inserts on many DuckDB threads in one
  transaction, keeping the upload's row order. See docs/benchmarks.md.

### Documentation

- README: the client examples use the Hardcharger drivers, `adbc-driver-hardcharger`
  (`from adbc_driver_hardcharger import dbapi as hardcharger`, `hardcharger://` URIs) and
  `hardcharger-jdbc` (`jdbc:hardcharger://`); a Python quick start with `pip install hardcharger`
  (the server) beside `pip install adbc-driver-hardcharger` (the client); badges for both.

## [0.2.0] - 2026-10-17

Role-based access control and GizmoSQL 1.x's server features (instrumentation, catalog
logging, catalog access rules, statement queue, metrics, KILL SESSION, the OAuth server,
JWKS tokens and more), checked against 404 of the 423 server-side cases of GizmoSQL's
test battery, plus two security reviews of the result (see Fixed).

### Added

- Role-based access control, lifted from GizmoSQL 1.x's RBAC: `CREATE USER/ROLE`,
  `GRANT`/`REVOKE` (roles, table, view, column, schema and catalog privileges, `WITH GRANT OPTION`, `WITH ADMIN OPTION`, cascading revokes),
  `SHOW GRANTS`, ownership (`ALTER ... OWNER TO`), views that read with their owner's
  rights, system privileges (`READ FILESYSTEM`, `MANAGE CATALOGS`, ...), row and masking
  policies, tenants, `SET ROLE`, identity-provider group mappings, scoped tokens and an
  Open Policy Agent service. Every statement is checked inside DuckDB against its own
  plan, when it is bound and again before it is optimized; anything the check cannot
  classify is refused. Turned on by `rbac_store_uri` (a PostgreSQL database every replica
  shares, reached through DuckDB's `postgres` extension); `rbac_opa_url`,
  `rbac_opa_timeout_ms` and `token_groups_claim` too.
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
  `audit_chain_key`, SHA-256 without); `hardcharger
  --verify-audit-log FILE` checks a JSON log.
- DuckDB is built with a small binder patch (`server/scripts/patch-duckdb.cmake`) that
  lets access control see which part of a plan came from which view.
- Tests for access control (`server/tests/test_rbac_*.py`, 262), including two replicas on
  one store and a gate that access control costs no concurrency. They need PostgreSQL.
- SQL functions for the session's context: `HARDCHARGER_USER()`, `_ROLE()`, `_VERSION()`,
  `_CURRENT_SESSION()` (a UUID), `_CURRENT_INSTANCE()`, `_CURRENT_CLUSTER()`,
  `_INSTRUMENTATION_ENABLED()`, `_INSTRUMENTATION_CATALOG()`, `_INSTRUMENTATION_SCHEMA()`;
  table functions `hardcharger_settings()`, `hardcharger_sessions()` and
  `hardcharger_metrics()`.
- Server settings in SQL: `SET [SESSION|GLOBAL] hardcharger.<name>` and `RESET`, with
  1.x's scopes: `query_timeout`, `query_log_level`, `graceful_shutdown`,
  `shutdown_grace_period_seconds`, the statement queue's limits, `bypass_queue`, the
  session and query tags; startup settings say where to change them. `SET GLOBAL` needs
  an admin.
- `KILL SESSION '<uuid>'`: ends another session at once,
  frees its `max_sessions` place, and its next request gets `session_ended`.
- `max_sessions` (admins are never capped; `too_many_sessions`), `session_idle_timeout`.
- The admin command guard (servers without access control): users without the admin role can't ATTACH/DETACH, INSTALL or
  LOAD, SET GLOBAL (or instance-wide settings), CHECKPOINT, COPY to or from local files,
  read local files (`read_csv('/x')`, `FROM 'x.csv'`), EXPORT/IMPORT DATABASE, CONNECT,
  manage secrets, or use `quack_*()`; only literal remote URLs pass where a path decides.
- The sensitive path guard (`block_sensitive_paths`, `sensitive_paths`): no statement may
  name credential or system files (`~/.ssh`, `/etc/shadow`, the server's TLS key, ...), however spelled; `secret_directory` can't change after startup. And
  `allow_unredacted_secrets` can't be turned on (`block_unredacted_secrets`); SQL with a
  NUL byte is refused.
- `_hardcharger_system`, a read-only catalog with `hardcharger_index_info` and
  `hardcharger_view_definition`.
- The statement queue: `max_concurrent_statements`, `max_queued_statements`,
  `max_queue_wait`, `admin_bypass_queue_default`.
- Metrics: `enable_metrics`, `GET /metrics` on `metrics_port` (`hardcharger_*` metrics),
  `hardcharger_metrics()`, the unclean-exit marker.
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
- `cluster_id`, `max_header_bytes` (what 1.x's `--max-metadata-size` capped),
  `result_batch_rows` (default 2048, as 1.x streamed).
- Instrumentation: `enable_instrumentation` records instances, sessions, statements and
  executions (with their queue wait, fetch timeline and status) in `_hardcharger_instr`,
  with 1.x's tables, views and columns (the version column is `hardcharger_version`), in a DuckDB
  file next to the database (`instrumentation_db_path`) or in a DuckLake or PostgreSQL
  catalog the init SQL attaches (`instrumentation_catalog`, `instrumentation_schema`). A
  killed server's open rows are closed at the next start. Queued statements show as `queued` while they wait.
- `instance_tag`, `SET hardcharger.session_tag` and `SET hardcharger.query_tag`, recorded with
  the instance, the session and each statement; `capture_query_profile` (`off`,
  `standard`, `detailed`; also `SET [GLOBAL]`) stores DuckDB's JSON query profile with
  each execution.
- Catalog logging: `enable_catalog_logging` also writes every log line to
  `_hardcharger_logs.logs` (1.x's columns), in its own file
  (`log_catalog_db_path`) or a DuckLake or PostgreSQL catalog (`log_catalog`,
  `log_schema`), in batches from a background writer.
- The instrumentation and log catalogs are the server's: admins may read them, nobody may
  write, detach or `USE` them, and non-admins don't see them in metadata.
- Identity provider sign-in: ID tokens verified with the
  provider's JWKS (`token_jwks_uri`, or the one `token_allowed_issuer`'s OpenID
  configuration names), RS256/384/512 and ES256/384/512, through `token_exchange` or as
  the password of user `token`.
- Browser sign-in: `oauth_client_id` runs GizmoSQL 1.x's OAuth server on `oauth_port`
  (default 31339) with its routes (`/oauth/initiate`,
  `/oauth/start`, `/oauth/callback`, `/oauth/token/<uuid>`), as a confidential client
  (`oauth_client_secret`, `oauth_scopes`, `oauth_base_url`, `oauth_redirect_uri`,
  `oauth_instance_id` for a shared callback proxy, `oauth_disable_tls` behind a TLS proxy).
- `--verify-tls URL`: fetches a URL with the system's CA bundle (`SSL_CERT_FILE`, else the
  first well-known bundle), as outbound HTTPS does, and exits 0 or 1.
- The GizmoSQL 1.x test battery, ported (`server/tests`, scoreboard in
  `server/tests/BATTERY.md`), with C++ unit tests (`server/unit`, googletest 1.18.0), on
  the one build that ships.
- Per-catalog permissions: a sign-in token's
  `catalog_access` rules (`read`, `write`, `none` per catalog name or glob, first match
  wins, no match is no access) are enforced on every statement's plan by access control,
  without a store: reads and writes per catalog (temp included), and what the user sees
  in SHOW DATABASES, `information_schema`, `duckdb_*()`, the `_hardcharger_system` views and
  the metadata requests; refusals read as in GizmoSQL 1.x ("Access denied: You do not have
  write access to catalog ..."). Under RBAC the rules grant nothing.
- The instrumentation and log catalogs are now also left out of SQL's own catalog
  functions (`duckdb_databases()`, `information_schema`, SHOW ...) for non-administrators.
- Instrumentation writes to a shared DuckLake catalog that lose a commit race to another
  server are tried again (a few times, backing off) instead of being dropped.
- `PIVOT` without an `IN` list runs (it expands into several statements), also for
  read-only principals.
- Access control and the battery's features work together: store users never see or
  write the instrumentation and log catalogs (admins read them, nobody writes them), may
  call the server's SQL functions (`hardcharger_settings()`, ...), and are refused files,
  URLs, ATTACH and the like by access control's system privileges rather than by the
  admin command guard, which stays for servers without access control. Access-control
  statements are counted, logged and recorded like other statements
  (`test_rbac_battery.py`).

### Changed

- The server's executable is `hardcharger-server` (the Docker image's entrypoint too); the
  plain `hardcharger` name is kept for a command-line client.
- No license: every feature that was Enterprise in GizmoSQL 1.x is available to everyone,
  each turned on by its own option. The license options (`license_key`,
  `license_key_file`) and their environment variables are gone: a license flag is an
  unknown option like any other. There is one build, and it has everything: no
  test-license build, no Enterprise/Core build switch, no edition (`EDITION()`, the
  `edition` setting, server-info and metrics label, and the instances table's
  `gizmosql_edition` column are gone).
- Hardcharger takes no GizmoSQL 1.x names: no `GIZMOSQL_*` environment variables, no 1.x
  flags (`--readonly`, `-D`, `--tls CERT KEY`, ...) or unprefixed environment names
  (`TLS_CERT`, `SECRET_KEY`, `INIT_SQL_COMMANDS`, ...), no `SET gizmosql.*`, no
  `GIZMOSQL_*()` or `gizmosql_*()` functions, no `_gizmosql_*` catalogs, no `gizmosql_*`
  metrics, no `gizmosql_rbac` store schema, no `GIZMOSQL:COLUMN_DEFAULT` metadata key,
  and no server tokens with `iss=gizmosql`. Every option is its `--flag`, its
  `HARDCHARGER_*` variable or its YAML key; leftover GizmoSQL variables are not read. The
  1.x-only options `backend` and `reject_unknown_sessions` are gone. See
  `docs/migrating-from-gizmosql.md` for the name mapping.
- The administrator's credentials are `--admin-username` / `--admin-password`
  (`HARDCHARGER_ADMIN_USERNAME` / `HARDCHARGER_ADMIN_PASSWORD`, YAML `admin_username` /
  `admin_password`); 0.1.x's `--username` / `--password` and `HARDCHARGER_USERNAME` /
  `HARDCHARGER_PASSWORD` are no longer read.
- Release images also go to Docker Hub (`gizmodata/hardcharger`), with the same tags as
  ghcr, signed in by OIDC (no stored token); the release refuses to push to, and fails
  after pushing to, a repository anyone can pull.

- `drain_seconds` is now `shutdown_grace_period_seconds` (default 300, as in 1.x).
- Table schemas from `metadata/tables?include_schema=true` carry nullability, column
  comments, auto-increment and defaults (`WARP:SQL:COLUMN_DEFAULT`).
- Warp SQL never recreates a session implicitly: an unknown session id is
  `session_not_found`.

### Fixed

- Security review of rules-only access control (catalog_access rules without an RBAC
  store): a non-administrator principal no longer holds every system privilege. It is
  held to a GizmoSQL 1.x non-administrator's limits, checked on each statement's own
  plan, so nested SQL is caught where it runs: no file or URL reads (also through
  `query('...')`), no secrets, no extensions, no table function outside the allow-list
  (`destroy_external_resource`, `pragma_storage_info`, ...), no server-wide or credential
  settings, and no reading a credential setting. Creating a macro or type (temporary
  ones included, except PIVOT's own enum) needs an administrator, since a macro's or
  type's body runs unchecked when it is used; nested SQL through `query()`,
  `query_table()` and `json_execute_serialized_sql()` is refused because those table
  functions are off the non-administrator allow-list. Sequence functions (`nextval`, `setval`,
  `currval`) need write (currval: read) access to their catalog. `DESCRIBE` of a catalog
  the user may not see is refused. A plain server (no store) now routes every non-admin
  token principal through the same plan check.
- Credential redaction: SQL comments
  (`CREATE /* */ SECRET`), string literals and dollar-quoting no longer hide the keyword,
  so `CREATE [OR REPLACE] [PERSISTENT|TEMP] SECRET`, `CREATE`/`ALTER USER` and
  `SET`/`PRAGMA` of a credential setting are redacted wherever they appear, with a
  raw-word backstop. DuckDB parse errors that echo such a statement are dropped, ATTACH
  connection strings and credential-setting values are masked (idempotently, the 1.x key
  list plus `x-amz-*` and `user:***@` URLs), and all of it reaches the log output,
  `_hardcharger_instr` (`sql_text`, `prepare_error`, `error_message`), the `_hardcharger_logs`
  catalog and the access-control audit records, which are redacted before they are sealed
  so the hash chain still verifies.
- Statement logs, instrumentation's `sql_statements` and the catalog logs no longer keep
  `CREATE SECRET` or `CREATE`/`ALTER USER` text, which can carry credentials.
- The server could crash while stopping: a thread finishing after OpenSSL's exit cleanup
  touched freed state. OpenSSL no longer frees its global state at exit.
- A NULL bound through an Arrow parameter keeps its Arrow column's type when DuckDB can't
  infer the parameter's (`SELECT ? + 1`); LIST and interval Arrow parameters bind (an
  interval with sub-microsecond precision is refused, not truncated); prepared
  statements describe DECIMAL, UUID, LIST and INTERVAL parameters.
- Opening a session with an unknown option says `unknown_option`.
- Metadata `type-info` (JDBC `getTypeInfo`, Flight SQL `GetXdbcTypeInfo`) answered 404; it
  lists DuckDB's types, and a tenant's users see all of them.
- Bulk ingest of a column with no values (Arrow null type) creates a VARCHAR column;
  half floats and nested day-time intervals are refused with the reason; dictionary-
  encoded parameters bind; listing catalogs no longer opens every attached catalog; a
  stop with a request in flight no longer crashes.
- Table schemas from metadata give PRECISION in decimal digits for every numeric type,
  column comments (REMARKS) and defaults (`WARP:SQL:COLUMN_DEFAULT`).
- (Warp SQL SDK) A server stopped with a request in flight no longer crashes;
  dictionary-encoded parameters bind; ingest turns null-typed columns into VARCHAR,
  converts day_time intervals and refuses types DuckDB can't store; parameter schemas
  carry DuckDB's own Arrow types (VARIANT, GEOMETRY and UUID extensions); listing
  catalogs no longer opens every attached catalog.
- The `hardcharger_*()` table functions no longer destroy a column type DuckDB only lent them.
- (Warp SQL SDK) Cancelling a statement just after submitting it no longer leaves it running:
  an interrupt that reached DuckDB before its query began was forgotten, so the cancel is
  now repeated until the statement ends.

### Known gaps

- Not yet ported from GizmoSQL 1.x: OpenTelemetry export.
- Browser sign-in has only 1.x's routes on `oauth_port`; Warp SQL's `oauth_browser` flow
  (`/warp/v1/auth/oauth/flows`) needs the Warp SQL SDK to offer it.
- Instrumentation's fetch times are when the server took each batch from DuckDB, not when
  the client received it (the Warp SQL SDK has no delivery callback); its `peer` column
  is empty (the SDK's principal carries no peer address).
- Listing the schemas or tables of one catalog still opens every attached catalog: the v2
  C API has no catalog-scoped listing.
- VARIANT (and other struct or extension) Arrow parameters can't be bound yet.
- `glob()` over a directory holding a protected file is refused, not filtered; a path an
  admin computes at run time is not checked (no file-system hook in the v2 C API).

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
