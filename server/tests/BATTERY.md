# The GizmoSQL 1.x test battery on Hardcharger

Every GizmoSQL 1.x test file (gizmosql `main` at v1.42.0), what became of it here, and
the counts. "Failing" cases run as strict expected failures (`xfail(strict=True)`): they
run every time, and the suite turns red the day one starts to pass, so this page gets
updated. Nothing is skipped; a missing dependency fails its test.

## Totals

- **59 C++ test files, 565 gtest cases:** 361 passing, 4 failing, 16 not applicable, 184 not yet.
  - by file: 46 ported & passing, 3 passing except their not-applicable cases, 1 partly ported (the rest not yet), 3 ported & partly failing, 2 not applicable, 4 not yet.
  - server-side cases (all but the 142 that test the `gizmosql_client` binary): 423, of which
    361 pass (85%) and 4 fail.
- **Scripts and driver suites (12 files, 134 cases or checks):** 22 passing, 0 failing, 17 not applicable, 95 not yet.
- Hardcharger's own tests (`test_server.py`, 27) and the Warp SQL conformance suite (76) pass too.

Ported files keep the 1.x file name (`test_x.cpp` → `test_x.py`); each docstring cites
the 1.x file and lists what doesn't apply and why. 1.x cases that unit-tested an internal
component run as C++ googletest cases (`server/unit/`, binary `hardcharger-unit-tests`)
through the same-named pytest file.

## C++ integration tests (`tests/integration/`)

| 1.x file | 1.x cases | passing | failing | N/A | not yet | Hardcharger port | note |
|---|---:|---:|---:|---:|---:|---|---|
| `integration/test_authentication.cpp` | 30 | 30 | 0 | 0 | 0 | `test_authentication.py` | ported & passing; every rejection asserts its problem code |
| `integration/test_credential_parsing.cpp` | 5 | 4 | 0 | 1 | 0 | `test_credential_parsing.py` | mixed; N/A: the ':'-in-user-name startup check (Warp sends JSON fields) |
| `integration/test_cross_instance_tokens.cpp` | 7 | 7 | 0 | 0 | 0 | `test_cross_instance_tokens.py` | ported & passing; wrong-secret case now asserts invalid_token |
| `integration/test_token_authorized_emails.cpp` | 7 | 7 | 0 | 0 | 0 | `test_token_authorized_emails.py` | ported & passing |
| `integration/test_oauth_server.cpp` | 9 | 8 | 0 | 1 | 0 | `test_oauth_server.py` | mixed; N/A: the non-Enterprise stub; mock provider in Python; JWKS (RSA and EC) cases; +6 |
| `integration/test_auth_log_level.cpp` | 2 | 2 | 0 | 0 | 0 | `test_auth_log_level.py` | ported & passing; +1 case |
| `integration/test_catalog_permissions_enterprise.cpp` | 8 | 8 | 0 | 0 | 0 | `test_catalog_permissions_enterprise.py` | ported & passing; each case licensed and unlicensed |
| `integration/test_system_ca_certs.cpp` | 3 | 3 | 0 | 0 | 0 | `test_system_ca_certs.py` | ported & passing; C++ unit tests; --verify-tls end to end; +2 |
| `integration/test_active_session_count.cpp` | 2 | 2 | 0 | 0 | 0 | `test_active_session_count.py` | ported & passing; read through gizmosql_sessions() |
| `integration/test_session_idle.cpp` | 5 | 5 | 0 | 0 | 0 | `test_session_idle.py` | ported & passing; against servers, not a unit |
| `integration/test_max_sessions.cpp` | 4 | 4 | 0 | 0 | 0 | `test_max_sessions.py` | ported & passing |
| `integration/test_reject_unknown_sessions.cpp` | 6 | 4 | 0 | 2 | 0 | `test_reject_unknown_sessions.py` | mixed; N/A: the two implicit-recreation cases (Warp never recreates) |
| `integration/test_kill_session.cpp` | 4 | 4 | 0 | 0 | 0 | `test_kill_session.py` | ported & passing; a real non-admin case; +2 cases |
| `integration/test_graceful_shutdown.cpp` | 5 | 5 | 0 | 0 | 0 | `test_graceful_shutdown.py` | ported & passing; real SIGTERM every case; +1 |
| `integration/test_health_check.cpp` | 8 | 8 | 0 | 0 | 0 | `test_health_check.py` | ported & passing |
| `integration/test_server_cli.cpp` | 2 | 2 | 0 | 0 | 0 | `test_server_cli.py` | ported & passing |
| `integration/test_graceful_shutdown_settings.cpp` | 4 | 4 | 0 | 0 | 0 | `test_graceful_shutdown_settings.py` | ported & passing; +1 |
| `integration/test_eager_execution.cpp` | 15 | 15 | 0 | 0 | 0 | `test_eager_execution.py` | ported & passing; 2 re-expressed for Warp SQL |
| `integration/test_result_streaming.cpp` | 2 | 2 | 0 | 0 | 0 | `test_result_streaming.py` | ported & passing |
| `integration/test_client_disconnect_cancel.cpp` | 3 | 3 | 0 | 0 | 0 | `test_client_disconnect_cancel.py` | ported & passing; +1 |
| `integration/test_prepared_statement_params.cpp` | 9 | 9 | 0 | 0 | 0 | `test_prepared_statement_params.py` | ported & passing; 3 re-expressed for Warp SQL |
| `integration/test_bulk_ingest.cpp` | 8 | 8 | 0 | 0 | 0 | `test_bulk_ingest.py` | ported & passing; day_time cases as C++ unit tests |
| `integration/test_table_column_metadata.cpp` | 3 | 3 | 0 | 0 | 0 | `test_table_column_metadata.py` | ported & passing |
| `integration/test_session_options_probe.cpp` | 3 | 3 | 0 | 0 | 0 | `test_session_options_probe.py` | ported & passing |
| `integration/test_max_metadata_size.cpp` | 4 | 4 | 0 | 0 | 0 | `test_max_metadata_size.py` | ported & passing; 431 headers_too_large |
| `integration/test_tpch_benchmark.cpp` | 2 | 2 | 0 | 0 | 0 | `test_tpch_benchmark.py` | ported & passing; checked against tpch_answers() |
| `integration/test_admin_command_guard.cpp` | 42 | 42 | 0 | 0 | 0 | `test_admin_command_guard.py` | ported & passing; 36 as C++ unit tests |
| `integration/test_sensitive_path_guard.cpp` | 10 | 10 | 0 | 0 | 0 | `test_sensitive_path_guard.py` | ported & passing; glob() refuses instead of hiding |
| `integration/test_allow_unsigned_extensions.cpp` | 2 | 2 | 0 | 0 | 0 | `test_allow_unsigned_extensions.py` | ported & passing |
| `integration/test_storage_version.cpp` | 3 | 3 | 0 | 0 | 0 | `test_storage_version.py` | ported & passing |
| `integration/test_set_query_log_level.cpp` | 2 | 2 | 0 | 0 | 0 | `test_set_query_log_level.py` | ported & passing |
| `integration/test_internal_query_log_level.cpp` | 3 | 3 | 0 | 0 | 0 | `test_internal_query_log_level.py` | ported & passing |
| `integration/test_log_level_filtering.cpp` | 14 | 14 | 0 | 0 | 0 | `test_log_level_filtering.py` | ported & passing; C++ unit tests |
| `integration/test_query_profile_capture.cpp` | 6 | 6 | 0 | 0 | 0 | `test_query_profile_capture.py` | ported & passing; DuckDB's profiler output file |
| `integration/test_enterprise_gating.cpp` | 7 | 7 | 0 | 0 | 0 | `test_enterprise_gating.py` | ported & passing |
| `integration/test_instance_session_query_tags.cpp` | 15 | 11 | 0 | 0 | 4 | `test_instance_session_query_tags.py` | mixed; not yet: the 4 client cases (needs a Warp SQL client binary); +1 |
| `integration/test_metrics.cpp` | 11 | 11 | 0 | 0 | 0 | `test_metrics.py` | ported & passing; unit cases in C++ |
| `integration/test_admission_controller.cpp` | 14 | 14 | 0 | 0 | 0 | `test_admission_controller.py` | ported & passing; C++ unit tests |
| `integration/test_statement_queue.cpp` | 8 | 8 | 0 | 0 | 0 | `test_statement_queue.py` | ported & passing; +1 |
| `integration/test_admission_queue_telemetry.cpp` | 2 | 0 | 0 | 2 | 0 | — | not applicable; N/A: no OTLP export (decision pending); gizmosql_queue_depth on /metrics |
| `integration/test_catalog_access.cpp` | 42 | 0 | 0 | 0 | 42 | — | not yet; not yet: needs the catalogs a statement touches (authorization module) |
| `integration/test_catalog_wildcards.cpp` | 12 | 12 | 0 | 0 | 0 | `test_catalog_wildcards.py` | ported & passing; C++ unit tests; +2 |
| `integration/test_system_managed_catalog_visibility.cpp` | 2 | 1 | 1 | 0 | 0 | `test_system_managed_catalog_visibility.py` | ported, partly failing; hiding it from SQL's own catalog functions needs DuckDB internals |
| `integration/test_system_catalog.cpp` | 4 | 4 | 0 | 0 | 0 | `test_system_catalog.py` | ported & passing; +1 (DETACH) |
| `integration/test_instrumentation.cpp` | 17 | 17 | 0 | 0 | 0 | `test_instrumentation.py` | ported & passing; manager cases against real servers; failed write as a C++ unit test; +2 |
| `integration/test_instrumentation_ducklake.cpp` | 7 | 7 | 0 | 0 | 0 | `test_instrumentation_ducklake.py` | ported & passing; catalog logging through a real server, also on DuckLake; +1 |
| `integration/test_catalog_logging.cpp` | 3 | 3 | 0 | 0 | 0 | `test_catalog_logging.py` | ported & passing; non-admin, license and file-location cases; writer as a C++ unit test; +5 |
| `integration/test_ducklake.cpp` | 2 | 2 | 0 | 0 | 0 | `test_ducklake.py` | ported & passing |
| `integration/test_ducklake_catalog_fanout.cpp` | 2 | 0 | 2 | 0 | 0 | `test_ducklake_catalog_fanout.py` | ported, partly failing; per step: 4 of 7 pass; schema/table listing fans out |
| `integration/test_geoarrow.cpp` | 3 | 3 | 0 | 0 | 0 | `test_geoarrow.py` | ported & passing; extension name required |
| `integration/test_variant.cpp` | 4 | 3 | 1 | 0 | 0 | `test_variant.py` | ported, partly failing; binding a VARIANT parameter: SDK gap |
| `integration/test_sqlite_backend.cpp` | 10 | 0 | 0 | 10 | 0 | — | not applicable; N/A: Hardcharger is DuckDB only |
| `integration/test_read_only_mode.cpp` | 1 | 1 | 0 | 0 | 0 | `test_read_only_mode.py` | ported & passing |
| `integration/test_memory_limit.cpp` | 2 | 2 | 0 | 0 | 0 | `test_memory_limit.py` | ported & passing |
| `integration/test_init_sql_splitting.cpp` | 7 | 7 | 0 | 0 | 0 | `test_init_sql_splitting.py` | ported & passing; splitter cases as C++ unit tests |
| `integration/test_inline_license_key.cpp` | 10 | 10 | 0 | 0 | 0 | `test_inline_license_key.py` | ported & passing; re-inits become restarts; +4 |
| `integration/test_interactive_client.cpp` | 115 | 0 | 0 | 0 | 115 | — | not yet; not yet: needs a Warp SQL client binary |
| `integration/test_client_cli.cpp` | 18 | 0 | 0 | 0 | 18 | — | not yet; not yet: needs a Warp SQL client binary |
| `integration/test_client_config.cpp` | 5 | 0 | 0 | 0 | 5 | — | not yet; not yet: needs a Warp SQL client binary |

## Scripts and driver suites (`tests/`)

| 1.x file | 1.x cases | passing | failing | N/A | not yet | Hardcharger port | note |
|---|---:|---:|---:|---:|---:|---|---|
| `test_autocommit_false.py` | 3 | 3 | 0 | 0 | 0 | `test_autocommit_false.py` | ported & passing |
| `test_bulk_ingest.py` | 1 | 1 | 0 | 0 | 0 | `test_bulk_ingest_py.py` | ported & passing; sums asserted |
| `test_geoarrow.py` | 1 | 1 | 0 | 0 | 0 | `test_geoarrow.py` | ported & passing |
| `test_v1_22_features.py` | 2 | 2 | 0 | 0 | 0 | `test_v1_22_features.py` | ported & passing |
| `test_v1_22_1_features.py` | 2 | 2 | 0 | 0 | 0 | `test_v1_22_1_features.py` | ported & passing |
| `test_client_shell.sh` | 67 | 0 | 0 | 0 | 67 | — | not yet; not yet: needs a Warp SQL client binary (67 assertions) |
| `drivers/test_metrics_configuration.py` | 13 | 13 | 0 | 0 | 0 | `test_metrics_configuration.py` | ported & passing; 1.x never ran it in CI |
| `drivers/test_eager_adbc.py` | 16 | 0 | 0 | 8 | 8 | — | mixed; N/A: the Flight SQL driver leg; not yet: the Warp SQL ADBC driver leg |
| `drivers/EagerJdbc.java` | 20 | 0 | 0 | 0 | 20 | — | not yet; not yet: needs a Warp SQL JDBC driver (~20 checks) |
| `drivers/test_eager_odbc.py` | 5 | 0 | 0 | 5 | 0 | — | not applicable; N/A until a Warp SQL ODBC driver exists |
| `drivers/run_compatibility.py` | 0 | 0 | 0 | 0 | 0 | — | not yet; N/A as written (runs Flight drivers); harness only |
| `quality/test_quality_scope.py` | 4 | 0 | 0 | 4 | 0 | — | not applicable; N/A: 1.x repository tooling |

## How the tests run

```bash
cd server && cmake --build build                    # hardcharger, hardcharger-license-test, hardcharger-unit-tests
docker compose -f tests/docker-compose.yml up -d --wait   # PostgreSQL + S3 for the DuckLake suites
cd tests && .venv/bin/python -m pytest -q           # everything; -m "not services" without Docker
```

Enterprise features: with `HARDCHARGER_TEST_LICENSE_KEY_FILE` (a real GizmoData key with
every feature) licensed tests run the shipped binary; without it they run
`hardcharger-license-test`, which also trusts a key named by
`HARDCHARGER_TEST_LICENSE_PUBLIC_KEY`, with licenses the run signs itself. That build is
never installed or packaged (CI checks the package holds only `hardcharger`), and
`test_inline_license_key.py` proves the shipped binary refuses test-signed keys.
