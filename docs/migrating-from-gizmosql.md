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
| `--mtls-ca-cert-filename`, `-M`, `MTLS_CA_CERT_FILENAME`, `GIZMOSQL_MTLS_CA_CERT_FILENAME` | `--mtls-ca-cert` | `HARDCHARGER_MTLS_CA_CERT` |
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
