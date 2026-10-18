# Migrating from GizmoSQL 1.x

Hardcharger is a new product. It takes only its own names: every option is its
`--flag`, its `HARDCHARGER_*` environment variable or its YAML key, and nothing else.
GizmoSQL 1.x's environment variables (`GIZMOSQL_*` and the unprefixed ones such as
`TLS_CERT` or `SECRET_KEY`), its flags, its SQL settings and functions, its catalogs,
metrics and store schema are not read or answered to. A GizmoSQL variable left in the
environment does nothing, and a 1.x flag on the command line is an unknown option (the
server refuses to start, as for any typo).

There is no license either: every feature that was Enterprise in 1.x is in every build,
turned on by its own option.

This page maps 1.x's names to Hardcharger's.

## Options

Every option also has a YAML key: the flag without its leading `--`, with `_` for `-`
(`--admin-password` is `admin_password`).

| GizmoSQL 1.x | Hardcharger flag | Hardcharger environment variable |
|---|---|---|
| `--hostname`, `-H`, `GIZMOSQL_HOSTNAME` | `--host` | `HARDCHARGER_HOST` |
| `-R`, `GIZMOSQL_PORT` | `--port` | `HARDCHARGER_PORT` |
| `HEALTH_PORT`, `GIZMOSQL_HEALTH_PORT` | `--health-port` | `HARDCHARGER_HEALTH_PORT` |
| `--max-metadata-size`, `GIZMOSQL_MAX_METADATA_SIZE` | `--max-header-bytes` | `HARDCHARGER_MAX_HEADER_BYTES` |
| `TLS_CERT`, `GIZMOSQL_TLS_CERT` | `--tls-cert` | `HARDCHARGER_TLS_CERT` |
| `TLS_KEY`, `GIZMOSQL_TLS_KEY` | `--tls-key` | `HARDCHARGER_TLS_KEY` |
| `--mtls-ca-cert-filename`, `-M`, `MTLS_CA_CERT_FILENAME`, `GIZMOSQL_MTLS_CA_CERT_FILENAME` | `--mtls-ca-cert` (it needs `--tls-cert` and `--tls-key`; without them Hardcharger refuses to start) | `HARDCHARGER_MTLS_CA_CERT` |
| (none: in 1.x a client certificate only let a connection in, and every user still needed a password) | `--mtls-certificate-sign-in` (off by default: without it a certificate only admits the connection, as in 1.x), then `--mtls-identity-source`, `--mtls-admin-identities`, `--mtls-require-client-cert`, `--mtls-crl-file`, `--mtls-crl-allow-missing-ca-crls`, `--mtls-require-identity-match`: the certificate signs the user in (README, "Client certificates as identity") | `HARDCHARGER_MTLS_*` |
| `-B`, `DATABASE_BACKEND`, `GIZMOSQL_BACKEND` | (none: DuckDB is the only engine) | (none) |
| `--database-filename`, `-D`, `DATABASE_FILENAME`, `GIZMOSQL_DATABASE_FILENAME` | `--database` | `HARDCHARGER_DATABASE` |
| `--readonly`, `-O`, `READONLY`, `GIZMOSQL_READONLY` | `--read-only` | `HARDCHARGER_READ_ONLY` |
| `GIZMOSQL_MEMORY_LIMIT` | `--memory-limit` | `HARDCHARGER_MEMORY_LIMIT` |
| `GIZMOSQL_STORAGE_VERSION` | `--storage-version` | `HARDCHARGER_STORAGE_VERSION` |
| `GIZMOSQL_ALLOW_UNSIGNED_EXTENSIONS` | `--allow-unsigned-extensions` | `HARDCHARGER_ALLOW_UNSIGNED_EXTENSIONS` |
| `-I`, `INIT_SQL_COMMANDS`, `GIZMOSQL_INIT_SQL_COMMANDS` | `--init-sql-commands` | `HARDCHARGER_INIT_SQL_COMMANDS` |
| `-F`, `INIT_SQL_COMMANDS_FILE`, `GIZMOSQL_INIT_SQL_COMMANDS_FILE` | `--init-sql-commands-file` | `HARDCHARGER_INIT_SQL_COMMANDS_FILE` |
| `-U`, `GIZMOSQL_USERNAME` | `--admin-username` | `HARDCHARGER_ADMIN_USERNAME` |
| `-P`, `GIZMOSQL_PASSWORD` | `--admin-password` | `HARDCHARGER_ADMIN_PASSWORD` |
| `-S`, `SECRET_KEY`, `GIZMOSQL_SECRET_KEY` | `--secret-key` | `HARDCHARGER_SECRET_KEY` |
| `GIZMOSQL_ALLOW_CROSS_INSTANCE_TOKENS` | `--allow-cross-instance-tokens` | `HARDCHARGER_ALLOW_CROSS_INSTANCE_TOKENS` |
| `TOKEN_SIGNATURE_VERIFY_CERT_PATH`, `GIZMOSQL_TOKEN_SIGNATURE_VERIFY_CERT_PATH` | `--token-signature-verify-cert-path` | `HARDCHARGER_TOKEN_SIGNATURE_VERIFY_CERT_PATH` |
| `TOKEN_ALLOWED_ISSUER`, `GIZMOSQL_TOKEN_ALLOWED_ISSUER` | `--token-allowed-issuer` | `HARDCHARGER_TOKEN_ALLOWED_ISSUER` |
| `TOKEN_ALLOWED_AUDIENCE`, `GIZMOSQL_TOKEN_ALLOWED_AUDIENCE` | `--token-allowed-audience` | `HARDCHARGER_TOKEN_ALLOWED_AUDIENCE` |
| `GIZMOSQL_TOKEN_DEFAULT_ROLE` | `--token-default-role` | `HARDCHARGER_TOKEN_DEFAULT_ROLE` |
| `GIZMOSQL_TOKEN_GROUPS_CLAIM` | `--token-groups-claim` | `HARDCHARGER_TOKEN_GROUPS_CLAIM` |
| `GIZMOSQL_TOKEN_AUTHORIZED_EMAILS` | `--token-authorized-emails` | `HARDCHARGER_TOKEN_AUTHORIZED_EMAILS` |
| `GIZMOSQL_TOKEN_JWKS_URI` | `--token-jwks-uri` | `HARDCHARGER_TOKEN_JWKS_URI` |
| `GIZMOSQL_OAUTH_CLIENT_ID` | `--oauth-client-id` | `HARDCHARGER_OAUTH_CLIENT_ID` |
| `GIZMOSQL_OAUTH_CLIENT_SECRET` | `--oauth-client-secret` | `HARDCHARGER_OAUTH_CLIENT_SECRET` |
| `GIZMOSQL_OAUTH_SCOPES` | `--oauth-scopes` | `HARDCHARGER_OAUTH_SCOPES` |
| `GIZMOSQL_OAUTH_PORT` | `--oauth-port` | `HARDCHARGER_OAUTH_PORT` |
| `GIZMOSQL_OAUTH_BASE_URL` | `--oauth-base-url` | `HARDCHARGER_OAUTH_BASE_URL` |
| `GIZMOSQL_OAUTH_REDIRECT_URI` | `--oauth-redirect-uri` | `HARDCHARGER_OAUTH_REDIRECT_URI` |
| `GIZMOSQL_OAUTH_INSTANCE_ID` | `--oauth-instance-id` | `HARDCHARGER_OAUTH_INSTANCE_ID` |
| `GIZMOSQL_OAUTH_DISABLE_TLS` | `--oauth-disable-tls` | `HARDCHARGER_OAUTH_DISABLE_TLS` |
| `GIZMOSQL_MAX_SESSIONS` | `--max-sessions` | `HARDCHARGER_MAX_SESSIONS` |
| `GIZMOSQL_SESSION_IDLE_TIMEOUT` | `--session-idle-timeout` | `HARDCHARGER_SESSION_IDLE_TIMEOUT` |
| `GIZMOSQL_REJECT_UNKNOWN_SESSIONS` | (none: always so) | (none) |
| `--query-timeout`, `QUERY_TIMEOUT`, `GIZMOSQL_QUERY_TIMEOUT` | `--query-timeout-seconds` | `HARDCHARGER_QUERY_TIMEOUT_SECONDS` |
| `-Q`, `PRINT_QUERIES`, `GIZMOSQL_PRINT_QUERIES` | `--print-queries` | `HARDCHARGER_PRINT_QUERIES` |
| `GIZMOSQL_RBAC_STORE_URI` | `--rbac-store-uri` | `HARDCHARGER_RBAC_STORE_URI` |
| `GIZMOSQL_RBAC_OPA_URL` | `--rbac-opa-url` | `HARDCHARGER_RBAC_OPA_URL` |
| `GIZMOSQL_RBAC_OPA_TIMEOUT_MS` | `--rbac-opa-timeout-ms` | `HARDCHARGER_RBAC_OPA_TIMEOUT_MS` |
| `GIZMOSQL_AUDIT_CHAIN_KEY` | `--audit-chain-key` | `HARDCHARGER_AUDIT_CHAIN_KEY` |
| `GIZMOSQL_MAX_CONCURRENT_STATEMENTS` | `--max-concurrent-statements` | `HARDCHARGER_MAX_CONCURRENT_STATEMENTS` |
| `GIZMOSQL_MAX_QUEUED_STATEMENTS` | `--max-queued-statements` | `HARDCHARGER_MAX_QUEUED_STATEMENTS` |
| `GIZMOSQL_MAX_QUEUE_WAIT` | `--max-queue-wait` | `HARDCHARGER_MAX_QUEUE_WAIT` |
| `GIZMOSQL_ADMIN_BYPASS_QUEUE_DEFAULT` | `--admin-bypass-queue-default` | `HARDCHARGER_ADMIN_BYPASS_QUEUE_DEFAULT` |
| `GIZMOSQL_BLOCK_SENSITIVE_PATHS` | `--block-sensitive-paths` | `HARDCHARGER_BLOCK_SENSITIVE_PATHS` |
| `GIZMOSQL_SENSITIVE_PATHS` | `--sensitive-paths` | `HARDCHARGER_SENSITIVE_PATHS` |
| `GIZMOSQL_BLOCK_UNREDACTED_SECRETS` | `--block-unredacted-secrets` | `HARDCHARGER_BLOCK_UNREDACTED_SECRETS` |
| `GIZMOSQL_LICENSE_KEY` | (none: no license) | (none) |
| `-L`, `GIZMOSQL_LICENSE_KEY_FILE` | (none: no license) | (none) |
| `GIZMOSQL_CLUSTER_ID` | `--cluster-id` | `HARDCHARGER_CLUSTER_ID` |
| `GIZMOSQL_LOG_LEVEL` | `--log-level` | `HARDCHARGER_LOG_LEVEL` |
| `GIZMOSQL_LOG_FORMAT` | `--log-format` | `HARDCHARGER_LOG_FORMAT` |
| `GIZMOSQL_LOG_FILE` | `--log-file` | `HARDCHARGER_LOG_FILE` |
| `GIZMOSQL_QUERY_LOG_LEVEL` | `--query-log-level` | `HARDCHARGER_QUERY_LOG_LEVEL` |
| `GIZMOSQL_AUTH_LOG_LEVEL` | `--auth-log-level` | `HARDCHARGER_AUTH_LOG_LEVEL` |
| `GIZMOSQL_SESSION_LOG_LEVEL` | `--session-log-level` | `HARDCHARGER_SESSION_LOG_LEVEL` |
| `GIZMOSQL_GRACEFUL_SHUTDOWN` | `--graceful-shutdown` | `HARDCHARGER_GRACEFUL_SHUTDOWN` |
| `GIZMOSQL_SHUTDOWN_GRACE_PERIOD_SECONDS` | `--shutdown-grace-period-seconds` | `HARDCHARGER_SHUTDOWN_GRACE_PERIOD_SECONDS` |
| `GIZMOSQL_HEALTH_CHECK_QUERY` | `--health-check-query` | `HARDCHARGER_HEALTH_CHECK_QUERY` |
| `GIZMOSQL_HEALTH_CHECK_INTERVAL_SECONDS` | `--health-check-interval-seconds` | `HARDCHARGER_HEALTH_CHECK_INTERVAL_SECONDS` |
| `GIZMOSQL_HEALTH_CHECK_STALENESS_SECONDS` | `--health-check-staleness-seconds` | `HARDCHARGER_HEALTH_CHECK_STALENESS_SECONDS` |
| `GIZMOSQL_ENABLE_INSTRUMENTATION` | `--enable-instrumentation` | `HARDCHARGER_ENABLE_INSTRUMENTATION` |
| `GIZMOSQL_INSTRUMENTATION_DB_PATH` | `--instrumentation-db-path` | `HARDCHARGER_INSTRUMENTATION_DB_PATH` |
| `GIZMOSQL_INSTRUMENTATION_CATALOG` | `--instrumentation-catalog` | `HARDCHARGER_INSTRUMENTATION_CATALOG` |
| `GIZMOSQL_INSTRUMENTATION_SCHEMA` | `--instrumentation-schema` | `HARDCHARGER_INSTRUMENTATION_SCHEMA` |
| `GIZMOSQL_ENABLE_CATALOG_LOGGING` | `--enable-catalog-logging` | `HARDCHARGER_ENABLE_CATALOG_LOGGING` |
| `GIZMOSQL_LOG_DB_PATH` | `--log-catalog-db-path` | `HARDCHARGER_LOG_CATALOG_DB_PATH` |
| `GIZMOSQL_LOG_CATALOG` | `--log-catalog` | `HARDCHARGER_LOG_CATALOG` |
| `GIZMOSQL_LOG_SCHEMA` | `--log-schema` | `HARDCHARGER_LOG_SCHEMA` |
| `GIZMOSQL_INSTANCE_TAG` | `--instance-tag` | `HARDCHARGER_INSTANCE_TAG` |
| `GIZMOSQL_CAPTURE_QUERY_PROFILE` | `--capture-query-profile` | `HARDCHARGER_CAPTURE_QUERY_PROFILE` |
| `GIZMOSQL_ENABLE_METRICS` | `--enable-metrics` | `HARDCHARGER_ENABLE_METRICS` |
| `GIZMOSQL_METRICS_PORT` | `--metrics-port` | `HARDCHARGER_METRICS_PORT` |
| `GIZMOSQL_METRICS_BIND_ADDRESS` | `--metrics-bind-address` | `HARDCHARGER_METRICS_BIND_ADDRESS` |

| `--tls CERT KEY`, `-T CERT KEY` | `--tls-cert CERT --tls-key KEY` | `HARDCHARGER_TLS_CERT`, `HARDCHARGER_TLS_KEY` |
| `TLS_ENABLED` | (none: `--tls-cert` and `--tls-key` turn TLS on) | (none) |
| `--otel-enabled`, `GIZMOSQL_OTEL_*` | (none: no OpenTelemetry export yet) | (none) |

Hardcharger 0.1.x's own `--username` / `--password` (`HARDCHARGER_USERNAME` /
`HARDCHARGER_PASSWORD`) are `--admin-username` / `--admin-password` from 0.2.0.

## The command-line client

GizmoSQL 1.x's `gizmosql_client` is `hardcharger`. It speaks Warp SQL (HTTP), so it
connects to `hardcharger-server` (or any Warp SQL server), not to a GizmoSQL 1.x server.
Its options mean what they meant in 1.x; only the names of the environment variables,
files and URI scheme change. It reads no `GIZMOSQL_*` variable.

| gizmosql_client | hardcharger | Environment variable |
|---|---|---|
| `--host`, `-h` | `--host`, `-h` | `GIZMOSQL_HOST` → `HARDCHARGER_HOST` |
| `--port`, `-p` | `--port`, `-p` (default 31337) | `GIZMOSQL_PORT` → `HARDCHARGER_PORT` |
| `--username`, `-u` | `--username`, `-u` | `GIZMOSQL_USER` → `HARDCHARGER_USER`; the fallback `GIZMOSQL_USERNAME` (the server's) → `HARDCHARGER_ADMIN_USERNAME` |
| `--password`, `-W` (forces the prompt) | `--password`, `-W` | `GIZMOSQL_PASSWORD` → `HARDCHARGER_PASSWORD` (the user's own password; never falls back to the server's `HARDCHARGER_ADMIN_PASSWORD`) |
| `--tls` | `--tls` (HTTPS) | `GIZMOSQL_TLS` → `HARDCHARGER_TLS` |
| `--tls-roots` | `--tls-roots` | `GIZMOSQL_TLS_ROOTS` → `HARDCHARGER_TLS_ROOTS` |
| `--tls-skip-verify` | `--tls-skip-verify` | |
| `--mtls-cert`, `--mtls-key` | `--mtls-cert`, `--mtls-key` (also with `--tls-skip-verify`, which 1.x refused: Arrow Flight dropped the certificate then) | |
| `--auth-type password\|external` | `--auth-type password\|external` (the server's browser sign-in, `--oauth-port` routes) | |
| `--oauth-port` | `--oauth-port` (default 31339) | `GIZMOSQL_OAUTH_PORT` → `HARDCHARGER_OAUTH_PORT` |
| (discovered over Flight) | `--oauth-base-url` (default `http(s)://HOST:OAUTH_PORT`) | `HARDCHARGER_OAUTH_BASE_URL` (the server's variable of the same meaning) |
| `--command`, `-c`; `--file`, `-f`; `--output`, `-o` | the same | |
| `--init` (default `~/.gizmosqlrc`), `--no-init` | `--init` (default `~/.hardchargerrc`), `--no-init` | |
| `--session-tag`, `--query-tag` | the same (sent as `SET hardcharger.session_tag` / `query_tag`) | `GIZMOSQL_SESSION_TAG`, `GIZMOSQL_QUERY_TAG` → `HARDCHARGER_SESSION_TAG`, `HARDCHARGER_QUERY_TAG` |
| `--csv`, `--json`, `--table`, `--box`, `--markdown`, `--no-header` | the same | |
| `--quiet`, `-q`; `--echo`, `-e`; `--bail`; `--null` | the same | |
| `--help`, `-?`; `--version`, `-v` | the same | |
| `--uri`, or the positional URI: `gizmosql://HOST:PORT?username=…&useEncryption=…&disableCertificateVerification=…&tlsRoots=…&authType=…` | `hardcharger://HOST:PORT?…` with the same parameters | |
| (none) | the browser `HARDCHARGER_BROWSER` opens for `--auth-type external` (default: `open`, `xdg-open`) | `HARDCHARGER_BROWSER` |

Files: `~/.gizmosql_history` is `~/.hardcharger_history`, `~/.gizmosqlrc` is
`~/.hardchargerrc`, and `.export_last`'s default `~/.gizmosql_last_result.arrow` is
`~/.hardcharger_last_result.arrow`. The prompt is `hardcharger>` until connected, then the
current `catalog.schema`, as in 1.x.

The dot commands and output modes are 1.x's. What behaves differently, and why:

- **Statements with rows show them.** Warp SQL answers rows or a statement document, so
  the server decides, not the statement's first word: `INSERT … RETURNING` shows its rows
  (1.x printed `OK`).
- **No row-count line in machine-readable output.** 1.x printed `N rows (M columns)` after
  CSV, JSON and the other non-box modes even into files and pipes, which broke them
  (`--json | jq`). It is printed only at an interactive terminal now; box and table
  modes keep theirs inside the box.
- **`.once FILE`** redirects only the next result (1.x kept redirecting, like `.output`),
  and **`.read FILE`** runs the file (1.x answered that it was "handled by the shell loop").
- **A dot command that fails** (unknown, or `.read` of a missing file) makes a script exit
  with status 1, as a failed statement does.
- **Ctrl-C** cancels the running statement through Warp SQL's cancel, and the shell (or
  the script) goes on, as in 1.x. In a script that isn't running a statement, Ctrl-C stops
  the client (status 130); 1.x ignored it there.
- **A lost session is reopened.** When the server no longer has the session (idle too
  long, restarted) or ended it (`KILL SESSION`), the client opens a new one, says so, and
  goes on; a statement that never ran is run in it. Temporary tables and session settings
  don't survive this, as the warning says.
- **`.show`** reads the engine and Arrow versions from Warp SQL's server-info document and
  has no `edition` line (Hardcharger has no editions).

## SQL

| GizmoSQL 1.x | Hardcharger |
|---|---|
| `SET [SESSION\|GLOBAL] gizmosql.<name>`, `RESET gizmosql.<name>` | `SET [SESSION\|GLOBAL] hardcharger.<name>`, `RESET hardcharger.<name>` |
| `GIZMOSQL_USER()`, `GIZMOSQL_ROLE()`, `GIZMOSQL_VERSION()` | `HARDCHARGER_USER()`, `HARDCHARGER_ROLE()`, `HARDCHARGER_VERSION()` |
| `GIZMOSQL_CURRENT_SESSION()`, `_CURRENT_INSTANCE()`, `_CURRENT_CLUSTER()` | `HARDCHARGER_CURRENT_SESSION()`, `_CURRENT_INSTANCE()`, `_CURRENT_CLUSTER()` |
| `GIZMOSQL_INSTRUMENTATION_ENABLED()`, `_CATALOG()`, `_SCHEMA()` | `HARDCHARGER_INSTRUMENTATION_ENABLED()`, `_CATALOG()`, `_SCHEMA()` |
| `GIZMOSQL_EDITION()`, `gizmosql.edition` | (none: no editions) |
| `gizmosql.backend` | (none: DuckDB is the only engine) |
| `gizmosql_settings()`, `gizmosql_sessions()`, `gizmosql_metrics()` | `hardcharger_settings()`, `hardcharger_sessions()`, `hardcharger_metrics()` (`hardcharger_settings()` has no `enterprise` column) |
| `_gizmosql_system` (`gizmosql_index_info`, `gizmosql_view_definition`) | `_hardcharger_system` (`hardcharger_index_info`, `hardcharger_view_definition`) |
| `_gizmosql_instr` | `_hardcharger_instr` |
| `_gizmosql_logs` | `_hardcharger_logs` |
| `gizmosql_instrumentation.db`, `gizmosql_logs.db` (default files) | `hardcharger_instrumentation.db`, `hardcharger_logs.db` |
| `instances.gizmosql_version`, `instances.gizmosql_edition` | `instances.hardcharger_version` (no edition column) |

Hardcharger creates its own instrumentation and log tables; it does not migrate tables a
GizmoSQL 1.x server created.

## Access control, metadata, metrics and tokens

| GizmoSQL 1.x | Hardcharger |
|---|---|
| RBAC store schema `gizmosql_rbac` | `hardcharger_rbac` (a new store; 1.x's is not read) |
| table metadata key `GIZMOSQL:COLUMN_DEFAULT` | `WARP:SQL:COLUMN_DEFAULT` (Warp SQL's key) |
| `gizmosql_*` Prometheus metrics (`gizmosql_statements_total`, ...) | `hardcharger_*` (`hardcharger_statements_total`, ...); `gizmosql_license_expiry_seconds` is gone |
| server tokens with `iss=gizmosql` | only tokens this server signs (`iss=hardcharger`) |
| the `.gizmosql-metrics-state` exit marker | `.hardcharger-metrics-state` |
| license keys (`GIZMOSQL_LICENSE_KEY`, `--license-key-file`) | (none: no license) |

### `catalog_access` rules on an administrator's token

Both apply a token's `catalog_access` rules whatever its role: an `admin` token with rules
reads and writes only where they allow (temp included) and sees only those catalogs; an
administrator without rules (the password administrator, or a token without the claim) is
unrestricted. Both let a restricted administrator read, never write, the server's own
catalogs (instrumentation, logs) whatever its rules, and keep SET GLOBAL, CHECKPOINT, KILL
SESSION and every session in `hardcharger_sessions()`. Hardcharger differs on purpose where
1.x's administrator rights reached data around the rules:

| A restricted administrator (`role: admin` with `catalog_access`) | GizmoSQL 1.x | Hardcharger |
|---|---|---|
| ATTACH / DETACH | allowed | refused (DETACH a denied catalog, ATTACH its file under a name the rules allow, read it) |
| files: `read_csv()`, `read_blob()`, `COPY ... FROM/TO` a file, `FROM 'file'` | allowed | refused (the database files themselves are files) |
| INSTALL / LOAD, CREATE / DROP SECRET, EXPORT / IMPORT DATABASE | allowed | refused |
| read the instrumentation / log catalogs | allowed (read-only) | refused (a read exposes every session's SQL text, with denied-catalog literals); hidden, as for a non-administrator |
| `hardcharger_sessions()`, `KILL SESSION` | every session | only its own session; KILL needs an administrator without rules |
| CREATE MACRO / TYPE | allowed | refused (as for every non-superuser under the plan check) |
| table functions such as `pragma_storage_info('c.t')` | allowed | only those any rules principal gets |
| `nextval()` / `currval()` on a sequence | any | by the rules of the sequence's catalog |
| access-control statements under RBAC (CREATE USER, GRANT, ...) | allowed (1.x drops the rules under RBAC) | refused, and the rules hold under RBAC |
| a claim that isn't a non-empty list (`[]`, a string, an object, `null`) | no rules: unrestricted | grants nothing |
