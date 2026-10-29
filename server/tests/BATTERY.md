# The GizmoSQL 1.x test battery on Hardcharger

Every GizmoSQL 1.x test file (gizmosql `main` at v1.42.0), what became of it here, and
the counts. "Failing" cases run as strict expected failures (`xfail(strict=True)`): they
run every time, and the suite turns red the day one starts to pass, so this page gets
updated. Nothing is skipped; a missing dependency fails its test.

## Totals

- **59 C++ test files, 565 gtest cases:** 525 passing, 3 failing, 37 not applicable, 0 not yet.
  - by file: 47 ported & passing, 6 passing except their not-applicable cases, 0 partly ported (the rest not yet), 2 ported & partly failing, 4 not applicable, 0 not yet.
  - server-side cases (all but the 142 that test the client): 423, of which
    383 pass (91%) and 3 fail.
  - client cases (1.x's `gizmosql_client`, now `hardcharger`, against a real
    hardcharger-server): 142, of which 142 pass.
- **Scripts and driver suites (12 files, 131 cases or checks):** 81 passing, 0 failing, 22 not applicable, 28 not yet.
- Hardcharger's own tests (`test_server.py`, `test_no_license.py`, the security and
  redaction suites, `test_metadata_privileges.py`, `test_wide_integers.py`,
  `test_token_refusal_reasons.py`) and the Chargewire conformance suite pass too.
- **Access control** (Hardcharger's own; GizmoSQL 1.x has none: `test_rbac_*.py`, 19
  files, 296 tests): all pass, on the same build, including `test_rbac_battery.py`,
  which checks the battery's features under access control (the server's own catalogs, SQL
  functions, settings, KILL SESSION, system privileges, credentials kept out of logs and
  instrumentation).
  - **Creating a view needs SELECT on what it reads:** `test_rbac_views.py`
    (`test_a_view_cannot_reach_what_its_owner_cannot`) refuses at `CREATE VIEW` a view
    over a column or a table its creator can't read (a view's query is checked as its
    creator's own SELECT of it), because metadata follows privileges and the creator,
    owning the view, could otherwise list its columns, and so the column names of a table
    it can't read. It keeps the read-time check through a REVOKE after the view was
    created; `test_rbac_enforcement.py` covers a temporary view over a protected table the
    same way, and `test_rbac_view_creation.py` (23 tests) covers the rule.
- **Security regression suites** (from two security reviews, all pass):
  `test_rules_security.py` — rules-only access control and plain-server non-admin token
  principals hold no system privilege: no file/URL reads (also through `query()`,
  `query_table()`, `json_execute_serialized_sql()`), no secrets, no settings reads/writes, no
  table function off the allow-list, no catalog they can't see (through DESCRIBE or sequences),
  and no macro/type creation (whose body runs unchecked). `test_redaction.py` and
  `redaction_test.cc` — credential redaction survives comments, string literals,
  dollar-quoting, DuckDB parse errors, ATTACH connection strings and credential settings, in the
  log output, instrumentation, the log catalog and the audit chain (redacted before sealing).
  Each negative test has a positive twin, and each was proven to fail on the pre-fix commit.
- **No license** (`test_no_license.py`): every former Enterprise feature works on a server
  started with no license, turned on by its own option.

Ported files keep the 1.x file name (`test_x.cpp` → `test_x.py`); each docstring cites
the 1.x file and lists what doesn't apply and why. Two reasons recur: Hardcharger has no
license (every 1.x licensing case is not applicable), and it takes only its own names
(1.x's environment variables, flags, SQL settings, functions, catalogs and metrics are
not accepted, so a 1.x name-compatibility case is not applicable; a case that tested a
feature through a 1.x name runs with Hardcharger's name). 1.x cases that unit-tested an internal
component run as C++ googletest cases (`server/unit/`, binary `hardcharger-unit-tests`)
through the same-named pytest file; 1.x's client tests, which test the client's classes,
run the same way against the `hardcharger` client's (`server/client/tests/`, binary
`hardcharger-client-tests`).

## C++ integration tests (`tests/integration/`)

| 1.x file | 1.x cases | passing | failing | N/A | not yet | Hardcharger port | note |
|---|---:|---:|---:|---:|---:|---|---|
| `integration/test_authentication.cpp` | 30 | 30 | 0 | 0 | 0 | `test_authentication.py` | ported & passing; every rejection asserts its problem code |
| `integration/test_credential_parsing.cpp` | 5 | 4 | 0 | 1 | 0 | `test_credential_parsing.py` | mixed; N/A: the ':'-in-user-name startup check (Chargewire sends JSON fields) |
| `integration/test_cross_instance_tokens.cpp` | 7 | 7 | 0 | 0 | 0 | `test_cross_instance_tokens.py` | ported & passing; wrong-secret case now asserts invalid_token |
| `integration/test_token_authorized_emails.cpp` | 7 | 7 | 0 | 0 | 0 | `test_token_authorized_emails.py` | ported & passing |
| `integration/test_oauth_server.cpp` | 9 | 8 | 0 | 1 | 0 | `test_oauth_server.py` | mixed; N/A: the non-Enterprise stub (one build, no license); mock provider in Python; JWKS (RSA and EC) cases; +6 |
| `integration/test_auth_log_level.cpp` | 2 | 2 | 0 | 0 | 0 | `test_auth_log_level.py` | ported & passing; +1 case |
| `integration/test_catalog_permissions_enterprise.cpp` | 8 | 8 | 0 | 0 | 0 | `test_catalog_permissions_enterprise.py` | ported & passing; no license: the "requires enterprise" cases check that the token signs in |
| `integration/test_system_ca_certs.cpp` | 3 | 3 | 0 | 0 | 0 | `test_system_ca_certs.py` | ported & passing; C++ unit tests; --verify-tls end to end; +2 |
| `integration/test_active_session_count.cpp` | 2 | 2 | 0 | 0 | 0 | `test_active_session_count.py` | ported & passing; read through hardcharger_sessions() |
| `integration/test_session_idle.cpp` | 5 | 5 | 0 | 0 | 0 | `test_session_idle.py` | ported & passing; against servers, not a unit |
| `integration/test_max_sessions.cpp` | 4 | 4 | 0 | 0 | 0 | `test_max_sessions.py` | ported & passing |
| `integration/test_reject_unknown_sessions.cpp` | 6 | 4 | 0 | 2 | 0 | `test_reject_unknown_sessions.py` | mixed; N/A: the two implicit-recreation cases (Chargewire never recreates); no option, it is always so |
| `integration/test_kill_session.cpp` | 4 | 4 | 0 | 0 | 0 | `test_kill_session.py` | ported & passing; a real non-admin case; +4 cases (session_ended's reason on every request, GET of the session included) |
| `integration/test_graceful_shutdown.cpp` | 5 | 5 | 0 | 0 | 0 | `test_graceful_shutdown.py` | ported & passing; real SIGTERM every case; +1 |
| `integration/test_health_check.cpp` | 8 | 8 | 0 | 0 | 0 | `test_health_check.py` | ported & passing |
| `integration/test_server_cli.cpp` | 2 | 2 | 0 | 0 | 0 | `test_server_cli.py` | ported & passing |
| `integration/test_graceful_shutdown_settings.cpp` | 4 | 4 | 0 | 0 | 0 | `test_graceful_shutdown_settings.py` | ported & passing; +1 |
| `integration/test_eager_execution.cpp` | 15 | 15 | 0 | 0 | 0 | `test_eager_execution.py` | ported & passing; 2 re-expressed for Chargewire |
| `integration/test_result_streaming.cpp` | 2 | 2 | 0 | 0 | 0 | `test_result_streaming.py` | ported & passing |
| `integration/test_client_disconnect_cancel.cpp` | 3 | 3 | 0 | 0 | 0 | `test_client_disconnect_cancel.py` | ported & passing; +1 |
| `integration/test_prepared_statement_params.cpp` | 9 | 9 | 0 | 0 | 0 | `test_prepared_statement_params.py` | ported & passing; 3 re-expressed for Chargewire |
| `integration/test_bulk_ingest.cpp` | 8 | 8 | 0 | 0 | 0 | `test_bulk_ingest.py` | ported & passing; day_time cases as C++ unit tests |
| `integration/test_table_column_metadata.cpp` | 3 | 3 | 0 | 0 | 0 | `test_table_column_metadata.py` | ported & passing |
| `integration/test_session_options_probe.cpp` | 3 | 3 | 0 | 0 | 0 | `test_session_options_probe.py` | ported & passing |
| `integration/test_max_metadata_size.cpp` | 4 | 4 | 0 | 0 | 0 | `test_max_metadata_size.py` | ported & passing; 431 headers_too_large |
| `integration/test_tpch_benchmark.cpp` | 2 | 2 | 0 | 0 | 0 | `test_tpch_benchmark.py` | ported & passing; checked against tpch_answers() |
| `integration/test_admin_command_guard.cpp` | 42 | 42 | 0 | 0 | 0 | `test_admin_command_guard.py` | ported & passing; 36 as C++ unit tests |
| `integration/test_sensitive_path_guard.cpp` | 10 | 10 | 0 | 0 | 0 | `test_sensitive_path_guard.py` | ported & passing; glob() refuses instead of hiding |
| `integration/test_allow_unsigned_extensions.cpp` | 2 | 2 | 0 | 0 | 0 | `test_allow_unsigned_extensions.py` | ported & passing |
| `integration/test_storage_version.cpp` | 3 | 3 | 0 | 0 | 0 | `test_storage_version.py` | ported & passing; +1 (a new ATTACH file) |
| `integration/test_set_query_log_level.cpp` | 2 | 2 | 0 | 0 | 0 | `test_set_query_log_level.py` | ported & passing |
| `integration/test_internal_query_log_level.cpp` | 3 | 3 | 0 | 0 | 0 | `test_internal_query_log_level.py` | ported & passing |
| `integration/test_log_level_filtering.cpp` | 14 | 14 | 0 | 0 | 0 | `test_log_level_filtering.py` | ported & passing; C++ unit tests |
| `integration/test_query_profile_capture.cpp` | 6 | 6 | 0 | 0 | 0 | `test_query_profile_capture.py` | ported & passing; DuckDB's profiler output file |
| `integration/test_enterprise_gating.cpp` | 7 | 0 | 0 | 7 | 0 | — | not applicable; N/A: Hardcharger has no license; every feature is available (test_no_license.py) |
| `integration/test_instance_session_query_tags.cpp` | 15 | 15 | 0 | 0 | 0 | `test_instance_session_query_tags.py` | ported & passing; the 4 client cases run the hardcharger client; +2 |
| `integration/test_metrics.cpp` | 11 | 10 | 0 | 1 | 0 | `test_metrics.py` | mixed; unit cases in C++; N/A: the license-feature case (no license) |
| `integration/test_admission_controller.cpp` | 14 | 14 | 0 | 0 | 0 | `test_admission_controller.py` | ported & passing; C++ unit tests |
| `integration/test_statement_queue.cpp` | 8 | 8 | 0 | 0 | 0 | `test_statement_queue.py` | ported & passing; +1 |
| `integration/test_admission_queue_telemetry.cpp` | 2 | 0 | 0 | 2 | 0 | — | not applicable; N/A: no OTLP export (decision pending); hardcharger_queue_depth on /metrics |
| `integration/test_catalog_access.cpp` | 42 | 42 | 0 | 0 | 0 | `test_catalog_access.py` | ported & passing; enforced by access control's plan check (rules only, no store); 7 SQL forms as one parametrized case |
| `integration/test_catalog_wildcards.cpp` | 12 | 12 | 0 | 0 | 0 | `test_catalog_wildcards.py` | ported & passing; C++ unit tests; +2 |
| `integration/test_system_managed_catalog_visibility.cpp` | 2 | 2 | 0 | 0 | 0 | `test_system_managed_catalog_visibility.py` | ported & passing; SQL's catalog functions filtered by access control's plan rewrite |
| `integration/test_system_catalog.cpp` | 4 | 4 | 0 | 0 | 0 | `test_system_catalog.py` | ported & passing; +1 (DETACH) |
| `integration/test_instrumentation.cpp` | 17 | 15 | 0 | 2 | 0 | `test_instrumentation.py` | mixed; manager cases against real servers; failed write and the batching writer's cases (full queue, backlog at shutdown) as C++ unit tests; N/A: the license gate (no license) and the 1.x-schema migration (Hardcharger takes only its own names); +11 |
| `integration/test_instrumentation_ducklake.cpp` | 7 | 6 | 0 | 1 | 0 | `test_instrumentation_ducklake.py` | mixed; on PostgreSQL: DuckLake is refused for instrumentation (a case asserts the refusal); catalog logging through a real server, also on DuckLake; N/A: the 1.x-schema migration (Hardcharger takes only its own names); +5 |
| `integration/test_catalog_logging.cpp` | 3 | 3 | 0 | 0 | 0 | `test_catalog_logging.py` | ported & passing; non-admin, no-license and file-location cases; writer as a C++ unit test; +5 |
| `integration/test_ducklake.cpp` | 2 | 2 | 0 | 0 | 0 | `test_ducklake.py` | ported & passing |
| `integration/test_ducklake_catalog_fanout.cpp` | 2 | 0 | 2 | 0 | 0 | `test_ducklake_catalog_fanout.py` | ported, partly failing; per step: 4 of 7 pass; schema/table listing fans out |
| `integration/test_geoarrow.cpp` | 3 | 3 | 0 | 0 | 0 | `test_geoarrow.py` | ported & passing; extension name required |
| `integration/test_variant.cpp` | 4 | 3 | 1 | 0 | 0 | `test_variant.py` | ported, partly failing; binding a VARIANT parameter: SDK gap |
| `integration/test_sqlite_backend.cpp` | 10 | 0 | 0 | 10 | 0 | — | not applicable; N/A: Hardcharger is DuckDB only |
| `integration/test_read_only_mode.cpp` | 1 | 1 | 0 | 0 | 0 | `test_read_only_mode.py` | ported & passing |
| `integration/test_memory_limit.cpp` | 2 | 2 | 0 | 0 | 0 | `test_memory_limit.py` | ported & passing |
| `integration/test_init_sql_splitting.cpp` | 7 | 7 | 0 | 0 | 0 | `test_init_sql_splitting.py` | ported & passing; splitter cases as C++ unit tests |
| `integration/test_inline_license_key.cpp` | 10 | 0 | 0 | 10 | 0 | — | not applicable; N/A: Hardcharger has no license (a license flag is an unknown option) |
| `integration/test_interactive_client.cpp` | 115 | 115 | 0 | 0 | 0 | `test_interactive_client.py` | ported & passing; C++ against the client's classes (client/tests/interactive_client_test.cc); 2 re-expressed: --tls-skip-verify with mTLS is allowed (Chargewire sends the certificate), .show has no edition |
| `integration/test_client_cli.cpp` | 18 | 18 | 0 | 0 | 0 | `test_client_cli.py` | ported & passing; the hardcharger binary; +21: the shell at a terminal (pexpect: prompt, history, completion, pager, password prompt, Ctrl-C), Ctrl-C in scripts, TLS, mutual TLS, browser sign-in, session recovery, .once/.read, option errors |
| `integration/test_client_config.cpp` | 5 | 5 | 0 | 0 | 0 | `test_client_config.py` | ported & passing; HARDCHARGER_* names (C++, client/tests/client_config_test.cc); +2 |

## Scripts and driver suites (`tests/`)

| 1.x file | 1.x cases | passing | failing | N/A | not yet | Hardcharger port | note |
|---|---:|---:|---:|---:|---:|---|---|
| `test_autocommit_false.py` | 3 | 3 | 0 | 0 | 0 | `test_autocommit_false.py` | ported & passing |
| `test_bulk_ingest.py` | 1 | 1 | 0 | 0 | 0 | `test_bulk_ingest_py.py` | ported & passing; sums asserted |
| `test_geoarrow.py` | 1 | 1 | 0 | 0 | 0 | `test_geoarrow.py` | ported & passing |
| `test_v1_22_features.py` | 2 | 2 | 0 | 0 | 0 | `test_v1_22_features.py` | ported & passing |
| `test_v1_22_1_features.py` | 2 | 2 | 0 | 0 | 0 | `test_v1_22_1_features.py` | ported & passing |
| `test_client_shell.sh` | 64 | 64 | 0 | 0 | 0 | `test_client_shell.py` | ported & passing; ported as a script (test_client_shell.sh, also its own CI step), one case per assertion; 64 assertions (once miscounted as 67) |
| `drivers/test_metrics_configuration.py` | 13 | 8 | 0 | 5 | 0 | `test_metrics_configuration.py` | mixed; 1.x never ran it in CI; N/A: the 5 license cases (no license) |
| `drivers/test_eager_adbc.py` | 16 | 0 | 0 | 8 | 8 | — | mixed; N/A: the Flight SQL driver leg; not yet: the Chargewire ADBC driver leg |
| `drivers/EagerJdbc.java` | 20 | 0 | 0 | 0 | 20 | — | not yet; not yet: needs a Chargewire JDBC driver (~20 checks) |
| `drivers/test_eager_odbc.py` | 5 | 0 | 0 | 5 | 0 | — | not applicable; N/A until a Chargewire ODBC driver exists |
| `drivers/run_compatibility.py` | 0 | 0 | 0 | 0 | 0 | — | not yet; N/A as written (runs Flight drivers); harness only |
| `quality/test_quality_scope.py` | 4 | 0 | 0 | 4 | 0 | — | not applicable; N/A: 1.x repository tooling |

## How the tests run

```bash
cmake -S server -B server/build -G Ninja -DCMAKE_BUILD_TYPE=Release   # the one build
cmake --build server/build
docker compose -f server/tests/docker-compose.yml up -d --wait  # PostgreSQL + S3 for the DuckLake suites
docker run -d --name hardcharger-test-pg -e POSTGRES_USER=hardcharger -e POSTGRES_PASSWORD=hardcharger   -e POSTGRES_DB=hardcharger -p 127.0.0.1:55433:5432 postgres:17-alpine   # the RBAC store
cd server/tests && .venv/bin/python -m pytest -q   # binaries default to ../build
```

There is no license and one build: the suite runs on the build that ships.
