"""NVIDIA's Sirius extension (`LOAD sirius`, GPU execution) and the server's checks, on a server
without Sirius: the refusals are by name, before DuckDB binds anything, so they hold whether
Sirius is loaded or not (authz/sirius.hpp). With the real extension pair:
test_sirius_live.py (marker `sirius`).

- gpu_execution('<sql>') replays its SQL on a connection of its own, with none of the server's
  checks; pin_table, unpin_table, sirius_knn_search and the ANN-index functions take a table by
  name: the administrator's only, and no grant opens them.
- sirius_read_parquet() reads with Sirius's own I/O, past the file guard: one s3:// literal
  only, for everyone (wherever DuckDB parses SQL: query()'s text too).
- Every Sirius setting needs MANAGE CONFIG, in any scope; the ones that reach the host (log
  directory and sink, the compression-plan directory, the exchange) only in the startup SQL.
"""

import pytest

from rbac import expect_allowed, expect_blocked, start_rbac_server

pytestmark = pytest.mark.rbac_store

ADMIN_ONLY = ["gpu_execution", "sirius_stream_source", "pin_table", "unpin_table", "sirius_knn_search",
              "sirius_create_ann_index", "sirius_drop_ann_index", "reset_sirius_cache", "profiler_start",
              "profiler_stop"]
CALLS = {"gpu_execution": "gpu_execution('SELECT * FROM t')",
         "sirius_stream_source": "sirius_stream_source(1)",
         "pin_table": "pin_table(format = 'duckdb', name = 't', tier = 'gpu')",
         "unpin_table": "unpin_table('t')",
         "sirius_knn_search": "sirius_knn_search('t', 'v', [1.0], 3)",
         "sirius_create_ann_index": "sirius_create_ann_index('t', 'v')",
         "sirius_drop_ann_index": "sirius_drop_ann_index('t', 'v')",
         "reset_sirius_cache": "reset_sirius_cache()",
         "profiler_start": "profiler_start()",
         "profiler_stop": "profiler_stop()"}
STARTUP_ONLY = {"sirius_log_backend": "'spdlog'", "sirius_log_dir": "'/tmp/joe-made-this-dir'",
                "sirius_log_level": "'trace'", "pin_table_input_compression_plan_dir": "'/etc'",
                "sirius_exchange_peers": "'10.0.0.1:9000,10.0.0.2:9000'", "sirius_exchange_rank": "1",
                "sirius_exchange_host_staging": "'auto'"}
TUNING = {"gpu_execution": "false", "enable_duckdb_fallback": "false", "spill_compression": "true",
          "sirius_log_flush_seconds": "1"}
STARTUP_ONLY_WHY = "can be set only in the server's startup SQL"
READ_PARQUET_WHY = "sirius_read_parquet() is Sirius's internal target"


@pytest.fixture(scope="module")
def world(module_launcher, store, tmp_path_factory):
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database())
    admin = server.admin()
    for sql in ["CREATE TABLE t AS SELECT range AS id, 'row' || range AS v FROM range(5)",
                "CREATE USER joe", "CREATE ROLE analyst", "GRANT analyst TO joe",
                "GRANT SELECT ON t TO analyst",
                "CREATE ROW POLICY only_small ON t TO analyst USING (id < 2)",
                "CREATE USER cfg", "CREATE ROLE configurer", "GRANT configurer TO cfg",
                "GRANT MANAGE CONFIG TO configurer"]:
        expect_allowed(client=admin, sql=sql)
    yield {"server": server, "admin": admin, "joe": server.user(name="joe"), "cfg": server.user(name="cfg")}


@pytest.mark.parametrize("function", ADMIN_ONLY)
def test_store_users_never_call_sirius_functions_that_run_sql_or_name_tables(world, function):
    joe = world["joe"]
    call = CALLS[function]
    for sql in (f"SELECT * FROM {call}", f"CALL {call}",
                f"SELECT * FROM (SELECT * FROM {call})", f"WITH x AS (SELECT * FROM {call}) SELECT * FROM x"):
        expect_blocked(client=joe, sql=sql, why=f"{function}(): under access control it is available to the "
                                                "administrator only")
    # Not even with a grant: EXECUTE on it can't be given.
    expect_blocked(client=world["admin"], sql=f"GRANT EXECUTE ON TABLE FUNCTION {function} TO analyst",
                   why="stays the administrator's", code="sql_error")


@pytest.mark.parametrize("setting", sorted(TUNING))
def test_a_sirius_setting_needs_manage_config_in_every_scope(world, setting):
    joe, cfg = world["joe"], world["cfg"]
    value = TUNING[setting]
    for scope in ("", "SESSION ", "GLOBAL "):
        expect_blocked(client=joe, sql=f"SET {scope}{setting} = {value}", why="MANAGE CONFIG")
    expect_blocked(client=joe, sql=f"PRAGMA {setting} = {value}", why="MANAGE CONFIG")
    expect_blocked(client=joe, sql=f"RESET {setting}", why="MANAGE CONFIG")
    # The twin: a user who holds MANAGE CONFIG gets past the check (this server has no Sirius,
    # so DuckDB then says the setting isn't there).
    error = cfg_error(client=cfg, sql=f"SET {setting} = {value}")
    assert "Hardcharger blocked" not in error, error


def cfg_error(client, sql: str) -> str:
    try:
        client.run(sql=sql)
    except Exception as error:  # noqa: BLE001 - DuckDB's own error, whatever its class
        return str(error)
    return ""


@pytest.mark.parametrize("setting", sorted(STARTUP_ONLY))
def test_sirius_settings_that_reach_the_host_are_for_the_startup_sql_only(world, setting):
    value = STARTUP_ONLY[setting]
    # The administrator too, in every form.
    for sql in (f"SET {setting} = {value}", f"SET GLOBAL {setting} = {value}", f"SET SESSION {setting} = {value}",
                f"PRAGMA {setting} = {value}", f"RESET {setting}", f"RESET GLOBAL {setting}"):
        expect_refused(client=world["admin"], sql=sql, why=STARTUP_ONLY_WHY)
    expect_blocked(client=world["joe"], sql=f"SET {setting} = {value}", why="MANAGE CONFIG")
    expect_refused(client=world["cfg"], sql=f"SET {setting} = {value}", why=STARTUP_ONLY_WHY)


def expect_refused(client, sql: str, why: str) -> None:
    error = cfg_error(client=client, sql=sql)
    assert why in error, f"{sql}: expected '{why}' in: {error!r}"


def test_sirius_read_parquet_reads_s3_only_for_everyone(world):
    admin = world["admin"]
    for sql in ("SELECT * FROM sirius_read_parquet('/etc/passwd')",
                "SELECT * FROM sirius_read_parquet('file:///etc/passwd')",
                "SELECT * FROM \"SIRIUS_READ_PARQUET\"('/etc/passwd')",
                "SELECT * FROM sirius_read_parquet('s3:' || '//b/k')",
                # Where DuckDB parses text the statement only carries: query()'s.
                "SELECT * FROM query('SELECT * FROM sirius_read_parquet(''/etc/passwd'')')",
                "CREATE VIEW v_srp AS SELECT * FROM sirius_read_parquet('/etc/passwd')"):
        expect_refused(client=admin, sql=sql, why=READ_PARQUET_WHY)
    # The s3:// form parses (this server has no Sirius: DuckDB then says there's no such function).
    error = cfg_error(client=admin, sql="SELECT * FROM sirius_read_parquet('s3://bucket/k.parquet')")
    assert READ_PARQUET_WHY not in error and "sirius_read_parquet" in error, error


def test_without_access_control_non_administrators_are_refused_too(launcher):
    running = launcher.start()
    user = running.as_role(role="user", subject="app")
    for function in ADMIN_ONLY:
        problem = user.error(sql=f"SELECT * FROM {CALLS[function]}")
        assert problem["code"] == "permission_denied", problem
        assert f"{function}()" in problem["detail"], problem
    for setting, value in TUNING.items():
        for scope in ("", "SESSION "):
            problem = user.error(sql=f"SET {scope}{setting} = {value}")
            assert problem["code"] == "permission_denied", problem
    admin = running.admin()
    for setting, value in STARTUP_ONLY.items():
        problem = admin.error(sql=f"SET {setting} = {value}")
        assert STARTUP_ONLY_WHY in problem["detail"], problem
    problem = admin.error(sql="SELECT * FROM sirius_read_parquet('/etc/passwd')")
    assert READ_PARQUET_WHY in problem["detail"], problem


def test_read_only_sessions_change_no_sirius_setting(launcher):
    running = launcher.start()
    reader = running.client().new_session(options={"read_only": True})
    for setting, value in {**TUNING, **STARTUP_ONLY}.items():
        for scope in ("", "SESSION "):
            problem = reader.error(sql=f"SET {scope}{setting} = {value}")
            assert problem["code"] in ("read_only", "permission_denied"), (setting, problem)
