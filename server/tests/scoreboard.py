"""Writes BATTERY.md, the scoreboard of the GizmoSQL 1.x test battery on Hardcharger.

Edit the rows below when a port changes, then run: python3 scoreboard.py BATTERY.md
"""

rows = [
 # Authentication and tokens
 ("integration/test_authentication.cpp", 30, 30, 0, 0, 0, "test_authentication.py", "every rejection asserts its problem code"),
 ("integration/test_credential_parsing.cpp", 5, 4, 0, 1, 0, "test_credential_parsing.py", "N/A: the ':'-in-user-name startup check (Warp sends JSON fields)"),
 ("integration/test_cross_instance_tokens.cpp", 7, 7, 0, 0, 0, "test_cross_instance_tokens.py", "wrong-secret case now asserts invalid_token"),
 ("integration/test_token_authorized_emails.cpp", 7, 7, 0, 0, 0, "test_token_authorized_emails.py", ""),
 ("integration/test_oauth_server.cpp", 9, 8, 0, 1, 0, "test_oauth_server.py", "N/A: the non-Enterprise stub (one build, no license); mock provider in Python; JWKS (RSA and EC) cases; +6"),
 ("integration/test_auth_log_level.cpp", 2, 2, 0, 0, 0, "test_auth_log_level.py", "+1 case"),
 ("integration/test_catalog_permissions_enterprise.cpp", 8, 8, 0, 0, 0, "test_catalog_permissions_enterprise.py", "no license: the \"requires enterprise\" cases check that the token signs in"),
 ("integration/test_system_ca_certs.cpp", 3, 3, 0, 0, 0, "test_system_ca_certs.py", "C++ unit tests; --verify-tls end to end; +2"),
 # Sessions, limits and lifecycle
 ("integration/test_active_session_count.cpp", 2, 2, 0, 0, 0, "test_active_session_count.py", "read through hardcharger_sessions()"),
 ("integration/test_session_idle.cpp", 5, 5, 0, 0, 0, "test_session_idle.py", "against servers, not a unit"),
 ("integration/test_max_sessions.cpp", 4, 4, 0, 0, 0, "test_max_sessions.py", ""),
 ("integration/test_reject_unknown_sessions.cpp", 6, 4, 0, 2, 0, "test_reject_unknown_sessions.py", "N/A: the two implicit-recreation cases (Warp never recreates); no option, it is always so"),
 ("integration/test_kill_session.cpp", 4, 4, 0, 0, 0, "test_kill_session.py", "a real non-admin case; +4 cases (session_ended's reason on every request, GET of the session included)"),
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
 ("integration/test_storage_version.cpp", 3, 3, 0, 0, 0, "test_storage_version.py", "+1 (a new ATTACH file)"),
 ("integration/test_set_query_log_level.cpp", 2, 2, 0, 0, 0, "test_set_query_log_level.py", ""),
 ("integration/test_internal_query_log_level.cpp", 3, 3, 0, 0, 0, "test_internal_query_log_level.py", ""),
 ("integration/test_log_level_filtering.cpp", 14, 14, 0, 0, 0, "test_log_level_filtering.py", "C++ unit tests"),
 ("integration/test_query_profile_capture.cpp", 6, 6, 0, 0, 0, "test_query_profile_capture.py", "DuckDB's profiler output file"),
 ("integration/test_enterprise_gating.cpp", 7, 0, 0, 7, 0, "", "N/A: Hardcharger has no license; every feature is available (test_no_license.py)"),
 ("integration/test_instance_session_query_tags.cpp", 15, 15, 0, 0, 0, "test_instance_session_query_tags.py", "the 4 client cases run the hardcharger client; +2"),
 ("integration/test_metrics.cpp", 11, 10, 0, 1, 0, "test_metrics.py", "unit cases in C++; N/A: the license-feature case (no license)"),
 # Admission control
 ("integration/test_admission_controller.cpp", 14, 14, 0, 0, 0, "test_admission_controller.py", "C++ unit tests"),
 ("integration/test_statement_queue.cpp", 8, 8, 0, 0, 0, "test_statement_queue.py", "+1"),
 ("integration/test_admission_queue_telemetry.cpp", 2, 0, 0, 2, 0, "", "N/A: no OTLP export (decision pending); hardcharger_queue_depth on /metrics"),
 # Catalog access and system catalog
 ("integration/test_catalog_access.cpp", 42, 42, 0, 0, 0, "test_catalog_access.py", "enforced by access control's plan check (rules only, no store); 7 SQL forms as one parametrized case"),
 ("integration/test_catalog_wildcards.cpp", 12, 12, 0, 0, 0, "test_catalog_wildcards.py", "C++ unit tests; +2"),
 ("integration/test_system_managed_catalog_visibility.cpp", 2, 2, 0, 0, 0, "test_system_managed_catalog_visibility.py", "SQL's catalog functions filtered by access control's plan rewrite"),
 ("integration/test_system_catalog.cpp", 4, 4, 0, 0, 0, "test_system_catalog.py", "+1 (DETACH)"),
 # Instrumentation and catalog logging
 ("integration/test_instrumentation.cpp", 17, 15, 0, 2, 0, "test_instrumentation.py", "manager cases against real servers; failed write and the batching writer's cases (full queue, backlog at shutdown) as C++ unit tests; N/A: the license gate (no license) and the 1.x-schema migration (Hardcharger takes only its own names); +11"),
 ("integration/test_instrumentation_ducklake.cpp", 7, 6, 0, 1, 0, "test_instrumentation_ducklake.py", "on PostgreSQL: DuckLake is refused for instrumentation (a case asserts the refusal); catalog logging through a real server, also on DuckLake; N/A: the 1.x-schema migration (Hardcharger takes only its own names); +5"),
 ("integration/test_catalog_logging.cpp", 3, 3, 0, 0, 0, "test_catalog_logging.py", "non-admin, no-license and file-location cases; writer as a C++ unit test; +5"),
 # Engine features
 ("integration/test_ducklake.cpp", 2, 2, 0, 0, 0, "test_ducklake.py", ""),
 ("integration/test_ducklake_catalog_fanout.cpp", 2, 0, 2, 0, 0, "test_ducklake_catalog_fanout.py", "per step: 4 of 7 pass; schema/table listing fans out"),
 ("integration/test_geoarrow.cpp", 3, 3, 0, 0, 0, "test_geoarrow.py", "extension name required"),
 ("integration/test_variant.cpp", 4, 3, 1, 0, 0, "test_variant.py", "binding a VARIANT parameter: SDK gap"),
 ("integration/test_sqlite_backend.cpp", 10, 0, 0, 10, 0, "", "N/A: Hardcharger is DuckDB only"),
 # Settings, startup
 ("integration/test_read_only_mode.cpp", 1, 1, 0, 0, 0, "test_read_only_mode.py", ""),
 ("integration/test_memory_limit.cpp", 2, 2, 0, 0, 0, "test_memory_limit.py", ""),
 ("integration/test_init_sql_splitting.cpp", 7, 7, 0, 0, 0, "test_init_sql_splitting.py", "splitter cases as C++ unit tests"),
 ("integration/test_inline_license_key.cpp", 10, 0, 0, 10, 0, "", "N/A: Hardcharger has no license (a license flag is an unknown option)"),
 # Client binary
 ("integration/test_interactive_client.cpp", 115, 115, 0, 0, 0, "test_interactive_client.py", "C++ against the client's classes (client/tests/interactive_client_test.cc); 8 re-expressed: --tls-skip-verify with mTLS is allowed (Warp SQL sends the certificate), .show has no edition, and the 6 URI cases follow the ADBC driver's URIs (HTTPS unless ?tls=false, only tls as a parameter)"),
 ("integration/test_client_cli.cpp", 18, 18, 0, 0, 0, "test_client_cli.py", "the hardcharger binary; +21: the shell at a terminal (pexpect: prompt, history, completion, pager, password prompt, Ctrl-C), Ctrl-C in scripts, TLS, mutual TLS, browser sign-in, session recovery, .once/.read, option errors"),
 ("integration/test_client_config.cpp", 5, 5, 0, 0, 0, "test_client_config.py", "HARDCHARGER_* names (C++, client/tests/client_config_test.cc); +2"),
]
scripts = [
 ("test_autocommit_false.py", 3, 3, 0, 0, 0, "test_autocommit_false.py", ""),
 ("test_bulk_ingest.py", 1, 1, 0, 0, 0, "test_bulk_ingest_py.py", "sums asserted"),
 ("test_geoarrow.py", 1, 1, 0, 0, 0, "test_geoarrow.py", ""),
 ("test_v1_22_features.py", 2, 2, 0, 0, 0, "test_v1_22_features.py", ""),
 ("test_v1_22_1_features.py", 2, 2, 0, 0, 0, "test_v1_22_1_features.py", ""),
 ("test_client_shell.sh", 64, 64, 0, 0, 0, "test_client_shell.py", "ported as a script (test_client_shell.sh, also its own CI step), one case per assertion; 64 assertions (once miscounted as 67)"),
 ("drivers/test_metrics_configuration.py", 13, 8, 0, 5, 0, "test_metrics_configuration.py", "1.x never ran it in CI; N/A: the 5 license cases (no license)"),
 ("drivers/test_eager_adbc.py", 16, 0, 0, 8, 8, "", "N/A: the Flight SQL driver leg; not yet: the Warp SQL ADBC driver leg"),
 ("drivers/EagerJdbc.java", 20, 0, 0, 0, 20, "", "not yet: needs a Warp SQL JDBC driver (~20 checks)"),
 ("drivers/test_eager_odbc.py", 5, 0, 0, 5, 0, "", "N/A until a Warp SQL ODBC driver exists"),
 ("drivers/run_compatibility.py", 0, 0, 0, 0, 0, "", "N/A as written (runs Flight drivers); harness only"),
 ("quality/test_quality_scope.py", 4, 0, 0, 4, 0, "", "N/A: 1.x repository tooling"),
]

rbac = [
 ("test_rbac_attached.py", 2),
 ("test_rbac_audit_chain.py", 6),
 ("test_rbac_battery.py", 7),
 ("test_rbac_concurrency.py", 1),
 ("test_rbac_ddl.py", 53),
 ("test_rbac_delegation.py", 14),
 ("test_rbac_enforcement.py", 31),
 ("test_rbac_engine.py", 15),
 ("test_rbac_group_mapping.py", 7),
 ("test_rbac_opa.py", 8),
 ("test_rbac_policies.py", 39),
 ("test_rbac_scoped_tokens.py", 7),
 ("test_rbac_server.py", 14),
 ("test_rbac_set_role.py", 7),
 ("test_rbac_store.py", 7),
 ("test_rbac_system_privileges.py", 13),
 ("test_rbac_tenants.py", 28),
 ("test_rbac_view_creation.py", 23),
 ("test_rbac_views.py", 14),
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
f_pna = by(rows, lambda e: e[2] and e[3] == 0 and e[5] == 0 and e[4] and e[2] + e[4] == e[1])
f_part = by(rows, lambda e: e[2] and e[3] == 0 and e[5] and e[5] < e[1])
client = 115 + 18 + 5 + 4  # + the 4 CLI cases of test_instance_session_query_tags.cpp
client_pass = 115 + 18 + 5 + 4  # every client case passes
server_cases = t[0] - client
server_pass = t[1] - client_pass
doc = f"""# The GizmoSQL 1.x test battery on Hardcharger

Every GizmoSQL 1.x test file (gizmosql `main` at v1.42.0), what became of it here, and
the counts. "Failing" cases run as strict expected failures (`xfail(strict=True)`): they
run every time, and the suite turns red the day one starts to pass, so this page gets
updated. Nothing is skipped; a missing dependency fails its test.

## Totals

- **59 C++ test files, {t[0]} gtest cases:** {t[1]} passing, {t[2]} failing, {t[3]} not applicable, {t[4]} not yet.
  - by file: {f_pass} ported & passing, {f_pna} passing except their not-applicable cases, {f_part} partly ported (the rest not yet), {f_fail} ported & partly failing, {f_na} not applicable, {f_ny} not yet.
  - server-side cases (all but the {client} that test the client): {server_cases}, of which
    {server_pass} pass ({100 * server_pass / server_cases:.0f}%) and {t[2]} fail.
  - client cases (1.x's `gizmosql_client`, now `hardcharger`, against a real
    hardcharger-server): {client}, of which {client_pass} pass.
- **Scripts and driver suites ({len(scripts)} files, {s[0]} cases or checks):** {s[1]} passing, {s[2]} failing, {s[3]} not applicable, {s[4]} not yet.
- Hardcharger's own tests (`test_server.py`, `test_no_license.py`, the security and
  redaction suites, `test_metadata_privileges.py`, `test_wide_integers.py`,
  `test_token_refusal_reasons.py`) and the Warp SQL conformance suite pass too.
- **Access control** (Hardcharger's own; GizmoSQL 1.x has none: `test_rbac_*.py`, {len(rbac)}
  files, {sum(n for _, n in rbac)} tests): all pass, on the same build, including `test_rbac_battery.py`,
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

{table(rows)}

## Scripts and driver suites (`tests/`)

{table(scripts)}

## How the tests run

```bash
cmake -S server -B server/build -G Ninja -DCMAKE_BUILD_TYPE=Release   # the one build
cmake --build server/build
docker compose -f server/tests/docker-compose.yml up -d --wait  # PostgreSQL + S3 for the DuckLake suites
docker run -d --name hardcharger-test-pg -e POSTGRES_USER=hardcharger -e POSTGRES_PASSWORD=hardcharger \
  -e POSTGRES_DB=hardcharger -p 127.0.0.1:55433:5432 postgres:17-alpine   # the RBAC store
cd server/tests && .venv/bin/python -m pytest -q   # binaries default to ../build
```

There is no license and one build: the suite runs on the build that ships.
"""
import pathlib, sys
pathlib.Path(sys.argv[1]).write_text(doc)
print(f"{sys.argv[1]}: {server_pass} of {server_cases} server-side cases and {client_pass} of {client} client cases pass")
