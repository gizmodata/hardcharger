"""Writes BATTERY.md, the scoreboard of the GizmoSQL 1.x test battery on Hardcharger.

Edit the rows below when a port changes, then run: python3 scoreboard.py BATTERY.md
"""

rows = [
 # Authentication and tokens
 ("integration/test_authentication.cpp", 30, 30, 0, 0, 0, "test_authentication.py", "every rejection asserts its problem code"),
 ("integration/test_credential_parsing.cpp", 5, 4, 0, 1, 0, "test_credential_parsing.py", "N/A: the ':'-in-user-name startup check (Warp sends JSON fields)"),
 ("integration/test_cross_instance_tokens.cpp", 7, 7, 0, 0, 0, "test_cross_instance_tokens.py", "wrong-secret case now asserts invalid_token"),
 ("integration/test_token_authorized_emails.cpp", 7, 7, 0, 0, 0, "test_token_authorized_emails.py", ""),
 ("integration/test_oauth_server.cpp", 9, 0, 0, 0, 9, "", "not yet: the OAuth server (external_auth)"),
 ("integration/test_auth_log_level.cpp", 2, 2, 0, 0, 0, "test_auth_log_level.py", "+1 case"),
 ("integration/test_catalog_permissions_enterprise.cpp", 8, 8, 0, 0, 0, "test_catalog_permissions_enterprise.py", "each case licensed and unlicensed"),
 ("integration/test_system_ca_certs.cpp", 3, 0, 0, 0, 3, "", "not yet: no outbound HTTPS client yet (JWKS, OAuth)"),
 # Sessions, limits and lifecycle
 ("integration/test_active_session_count.cpp", 2, 2, 0, 0, 0, "test_active_session_count.py", "read through gizmosql_sessions()"),
 ("integration/test_session_idle.cpp", 5, 5, 0, 0, 0, "test_session_idle.py", "against servers, not a unit"),
 ("integration/test_max_sessions.cpp", 4, 4, 0, 0, 0, "test_max_sessions.py", ""),
 ("integration/test_reject_unknown_sessions.cpp", 6, 4, 0, 2, 0, "test_reject_unknown_sessions.py", "N/A: the two implicit-recreation cases (Warp never recreates)"),
 ("integration/test_kill_session.cpp", 4, 4, 0, 0, 0, "test_kill_session.py", "a real non-admin case; +2 cases"),
 ("integration/test_graceful_shutdown.cpp", 5, 5, 0, 0, 0, "test_graceful_shutdown.py", "real SIGTERM every case; +1"),
 ("integration/test_health_check.cpp", 8, 8, 0, 0, 0, "test_health_check.py", ""),
 ("integration/test_server_cli.cpp", 2, 2, 0, 0, 0, "test_server_cli.py", ""),
 ("integration/test_graceful_shutdown_settings.cpp", 4, 4, 0, 0, 0, "test_graceful_shutdown_settings.py", "+1"),
 # Statements, results and protocol
 ("integration/test_eager_execution.cpp", 15, 15, 0, 0, 0, "test_eager_execution.py", "2 re-expressed for Warp SQL"),
 ("integration/test_result_streaming.cpp", 2, 2, 0, 0, 0, "test_result_streaming.py", ""),
 ("integration/test_client_disconnect_cancel.cpp", 3, 3, 0, 0, 0, "test_client_disconnect_cancel.py", "+1"),
 ("integration/test_prepared_statement_params.cpp", 9, 9, 0, 0, 0, "test_prepared_statement_params.py", "3 re-expressed for Warp SQL"),
 ("integration/test_bulk_ingest.cpp", 8, 8, 0, 0, 0, "test_bulk_ingest.py", "day_time cases as C++ unit tests"),
 ("integration/test_table_column_metadata.cpp", 3, 3, 0, 0, 0, "test_table_column_metadata.py", ""),
 ("integration/test_session_options_probe.cpp", 3, 3, 0, 0, 0, "test_session_options_probe.py", ""),
 ("integration/test_max_metadata_size.cpp", 4, 4, 0, 0, 0, "test_max_metadata_size.py", "431 headers_too_large"),
 ("integration/test_tpch_benchmark.cpp", 2, 2, 0, 0, 0, "test_tpch_benchmark.py", "checked against tpch_answers()"),
 # SQL surface
 ("integration/test_admin_command_guard.cpp", 42, 42, 0, 0, 0, "test_admin_command_guard.py", "36 as C++ unit tests"),
 ("integration/test_sensitive_path_guard.cpp", 10, 10, 0, 0, 0, "test_sensitive_path_guard.py", "glob() refuses instead of hiding"),
 ("integration/test_allow_unsigned_extensions.cpp", 2, 2, 0, 0, 0, "test_allow_unsigned_extensions.py", ""),
 ("integration/test_storage_version.cpp", 3, 3, 0, 0, 0, "test_storage_version.py", ""),
 ("integration/test_set_query_log_level.cpp", 2, 2, 0, 0, 0, "test_set_query_log_level.py", ""),
 ("integration/test_internal_query_log_level.cpp", 3, 3, 0, 0, 0, "test_internal_query_log_level.py", ""),
 ("integration/test_log_level_filtering.cpp", 14, 14, 0, 0, 0, "test_log_level_filtering.py", "C++ unit tests"),
 ("integration/test_query_profile_capture.cpp", 6, 0, 0, 0, 6, "", "not yet: needs instrumentation"),
 ("integration/test_enterprise_gating.cpp", 7, 7, 0, 0, 0, "test_enterprise_gating.py", ""),
 ("integration/test_instance_session_query_tags.cpp", 15, 0, 0, 0, 15, "", "not yet: 11 need instrumentation, 4 the client"),
 ("integration/test_metrics.cpp", 11, 11, 0, 0, 0, "test_metrics.py", "unit cases in C++"),
 # Admission control
 ("integration/test_admission_controller.cpp", 14, 14, 0, 0, 0, "test_admission_controller.py", "C++ unit tests"),
 ("integration/test_statement_queue.cpp", 8, 8, 0, 0, 0, "test_statement_queue.py", "2 re-expressed without instrumentation; +1"),
 ("integration/test_admission_queue_telemetry.cpp", 2, 0, 0, 2, 0, "", "N/A: no OTLP export (decision pending); gizmosql_queue_depth on /metrics"),
 # Catalog access and system catalog
 ("integration/test_catalog_access.cpp", 42, 0, 0, 0, 42, "", "not yet: needs the catalogs a statement touches (authorization module)"),
 ("integration/test_catalog_wildcards.cpp", 12, 12, 0, 0, 0, "test_catalog_wildcards.py", "C++ unit tests; +2"),
 ("integration/test_system_managed_catalog_visibility.cpp", 2, 0, 0, 0, 2, "", "not yet: needs instrumentation"),
 ("integration/test_system_catalog.cpp", 4, 4, 0, 0, 0, "test_system_catalog.py", "+1 (DETACH)"),
 # Instrumentation and catalog logging
 ("integration/test_instrumentation.cpp", 17, 0, 0, 0, 17, "", "not yet: instrumentation"),
 ("integration/test_instrumentation_ducklake.cpp", 7, 0, 0, 0, 7, "", "not yet: instrumentation"),
 ("integration/test_catalog_logging.cpp", 3, 0, 0, 0, 3, "", "not yet: catalog logging"),
 # Engine features
 ("integration/test_ducklake.cpp", 2, 2, 0, 0, 0, "test_ducklake.py", "instrumentation sub-check dropped"),
 ("integration/test_ducklake_catalog_fanout.cpp", 2, 0, 2, 0, 0, "test_ducklake_catalog_fanout.py", "per step: 4 of 7 pass; schema/table listing fans out"),
 ("integration/test_geoarrow.cpp", 3, 3, 0, 0, 0, "test_geoarrow.py", "extension name required"),
 ("integration/test_variant.cpp", 4, 2, 2, 0, 0, "test_variant.py", "VARIANT parameters: SDK and DuckDB bind gaps"),
 ("integration/test_sqlite_backend.cpp", 10, 0, 0, 10, 0, "", "N/A: Hardcharger is DuckDB only"),
 # Settings, startup
 ("integration/test_read_only_mode.cpp", 1, 1, 0, 0, 0, "test_read_only_mode.py", ""),
 ("integration/test_memory_limit.cpp", 2, 2, 0, 0, 0, "test_memory_limit.py", ""),
 ("integration/test_init_sql_splitting.cpp", 7, 7, 0, 0, 0, "test_init_sql_splitting.py", "splitter cases as C++ unit tests"),
 ("integration/test_inline_license_key.cpp", 10, 10, 0, 0, 0, "test_inline_license_key.py", "re-inits become restarts; +4"),
 # Client binary
 ("integration/test_interactive_client.cpp", 115, 0, 0, 0, 115, "", "not yet: needs a Warp SQL client binary"),
 ("integration/test_client_cli.cpp", 18, 0, 0, 0, 18, "", "not yet: needs a Warp SQL client binary"),
 ("integration/test_client_config.cpp", 5, 0, 0, 0, 5, "", "not yet: needs a Warp SQL client binary"),
]
scripts = [
 ("test_autocommit_false.py", 3, 3, 0, 0, 0, "test_autocommit_false.py", ""),
 ("test_bulk_ingest.py", 1, 1, 0, 0, 0, "test_bulk_ingest_py.py", "sums asserted"),
 ("test_geoarrow.py", 1, 1, 0, 0, 0, "test_geoarrow.py", ""),
 ("test_v1_22_features.py", 2, 2, 0, 0, 0, "test_v1_22_features.py", ""),
 ("test_v1_22_1_features.py", 2, 2, 0, 0, 0, "test_v1_22_1_features.py", ""),
 ("test_client_shell.sh", 67, 0, 0, 0, 67, "", "not yet: needs a Warp SQL client binary (67 assertions)"),
 ("drivers/test_metrics_configuration.py", 13, 13, 0, 0, 0, "test_metrics_configuration.py", "1.x never ran it in CI"),
 ("drivers/test_eager_adbc.py", 16, 0, 0, 8, 8, "", "N/A: the Flight SQL driver leg; not yet: the Warp SQL ADBC driver leg"),
 ("drivers/EagerJdbc.java", 20, 0, 0, 0, 20, "", "not yet: needs a Warp SQL JDBC driver (~20 checks)"),
 ("drivers/test_eager_odbc.py", 5, 0, 0, 5, 0, "", "N/A until a Warp SQL ODBC driver exists"),
 ("drivers/run_compatibility.py", 0, 0, 0, 0, 0, "", "N/A as written (runs Flight drivers); harness only"),
 ("quality/test_quality_scope.py", 4, 0, 0, 4, 0, "", "N/A: 1.x repository tooling"),
]

def table(entries):
    out = ["| 1.x file | 1.x cases | passing | failing | N/A | not yet | Hardcharger port | note |",
           "|---|---:|---:|---:|---:|---:|---|---|"]
    for f, n, p, x, na, ny, port, note in entries:
        assert p + x + na + ny == n, f
        status = "ported & passing" if n and p == n else ("ported, partly failing" if x else
                 ("not applicable" if na == n and n else ("not yet" if ny == n else "mixed")))
        out.append(f"| `{f}` | {n} | {p} | {x} | {na} | {ny} | {('`'+port+'`') if port else '—'} | {status}{'; ' + note if note else ''} |")
    return "\n".join(out)

def totals(entries):
    return [sum(e[i] for e in entries) for i in range(1, 6)]

t = totals(rows); s = totals(scripts)
files = len(rows)
by = lambda entries, pred: sum(1 for e in entries if pred(e))
f_pass = by(rows, lambda e: e[1] and e[2] == e[1]); f_fail = by(rows, lambda e: e[3] > 0)
f_na = by(rows, lambda e: e[4] == e[1]); f_ny = by(rows, lambda e: e[5] == e[1] and e[1])
client = 115 + 18 + 5 + 4  # + the 4 CLI cases of test_instance_session_query_tags.cpp
server_cases = t[0] - client
doc = f"""# The GizmoSQL 1.x test battery on Hardcharger

Every GizmoSQL 1.x test file (gizmosql `main` at v1.42.0), what became of it here, and
the counts. "Failing" cases run as strict expected failures (`xfail(strict=True)`): they
run every time, and the suite turns red the day one starts to pass, so this page gets
updated. Nothing is skipped; a missing dependency fails its test.

## Totals

- **59 C++ test files, {t[0]} gtest cases:** {t[1]} passing, {t[2]} failing, {t[3]} not applicable, {t[4]} not yet.
  - by file: {f_pass} ported & passing, {f_fail} ported & partly failing, {f_na} not applicable, {f_ny} not yet{'' if f_pass + f_fail + f_na + f_ny == files else ', ' + str(files - f_pass - f_fail - f_na - f_ny) + ' passing except their not-applicable cases'}.
  - server-side cases (all but the {client} that test the `gizmosql_client` binary): {server_cases}, of which
    {t[1]} pass ({100 * t[1] / server_cases:.0f}%) and {t[2]} fail.
- **Scripts and driver suites ({len(scripts)} files, {s[0]} cases or checks):** {s[1]} passing, {s[2]} failing, {s[3]} not applicable, {s[4]} not yet.
- Hardcharger's own tests (`test_server.py`, 27) and the Warp SQL conformance suite (76) pass too.

Ported files keep the 1.x file name (`test_x.cpp` → `test_x.py`); each docstring cites
the 1.x file and lists what doesn't apply and why. 1.x cases that unit-tested an internal
component run as C++ googletest cases (`server/unit/`, binary `hardcharger-unit-tests`)
through the same-named pytest file.

## C++ integration tests (`tests/integration/`)

{table(rows)}

## Scripts and driver suites (`tests/`)

{table(scripts)}

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
"""
import pathlib, sys
pathlib.Path(sys.argv[1]).write_text(doc)
print(f"{sys.argv[1]}: {t[1]} of {server_cases} server-side cases pass")
