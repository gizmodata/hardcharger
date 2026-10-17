"""Port of GizmoSQL 1.x tests/integration/test_query_profile_capture.cpp (6 cases).

SET [GLOBAL] gizmosql.capture_query_profile = 'off' | 'standard' | 'detailed' (server
default --capture-query-profile, GIZMOSQL_CAPTURE_QUERY_PROFILE; license feature
instrumentation) records DuckDB's own JSON query profile in
_gizmosql_instr.sql_executions.query_profile; off leaves it NULL; a bad value names the
setting. 1.x read the profiler in-process; Hardcharger turns on DuckDB's profiler for
the session (enable_profiling = 'json') and stores the file it writes. All 6 apply.
"""

import time

import pytest


@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start(features=["instrumentation"], args=["--enable-instrumentation"])


def profile_of(session, marker: str) -> tuple[bool, int]:
    sql = ("SELECT e.query_profile IS NOT NULL AS has_profile, coalesce(length(e.query_profile::VARCHAR), 0) AS size "
           "FROM _gizmosql_instr.sql_executions e JOIN _gizmosql_instr.sql_statements s USING (statement_id) "
           f"WHERE s.sql_text LIKE '%{marker}%' AND s.sql_text NOT LIKE '%sql_executions%' AND e.cursor_close_time IS NOT NULL")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        rows = session.rows(sql=sql)
        if rows:
            return rows[0]["has_profile"], rows[0]["size"]
        time.sleep(0.05)
    raise AssertionError(f"no finished execution for {marker}")


def test_capture_off_by_default_yields_null_profile(server):
    session = server.admin()
    session.scalar(sql="SELECT 1 AS profile_off_probe")
    assert profile_of(session, "profile_off_probe") == (False, 0)


def test_session_standard_captures_profile_json(server):
    session = server.admin()
    session.execute(sql="SET gizmosql.capture_query_profile = 'standard'")
    session.scalar(sql="SELECT 42 AS profile_standard_probe")
    has, size = profile_of(session, "profile_standard_probe")
    assert has and size > 0
    assert session.scalar(sql="SELECT bool_and(json_valid(query_profile::VARCHAR)) FROM _gizmosql_instr.sql_executions "
                              "WHERE query_profile IS NOT NULL") is True


def test_session_detailed_captures_profile_json(server):
    session = server.admin()
    session.execute(sql="SET gizmosql.capture_query_profile = 'detailed'")
    session.scalar(sql="SELECT 7 AS profile_detailed_probe")
    has, size = profile_of(session, "profile_detailed_probe")
    assert has and size > 0


def test_global_scope_applies_to_new_session(server):
    admin = server.admin()
    admin.execute(sql="SET GLOBAL gizmosql.capture_query_profile = 'standard'")
    try:
        fresh = server.admin()
        fresh.scalar(sql="SELECT 5 AS profile_global_probe")
        assert profile_of(fresh, "profile_global_probe")[0] is True
    finally:
        admin.execute(sql="SET GLOBAL gizmosql.capture_query_profile = 'off'")


def test_toggle_off_stops_capture(server):
    session = server.admin()
    session.execute(sql="SET gizmosql.capture_query_profile = 'standard'")
    session.scalar(sql="SELECT 11 AS profile_toggle_on_probe")
    session.execute(sql="SET gizmosql.capture_query_profile = 'off'")
    session.scalar(sql="SELECT 12 AS profile_toggle_off_probe")
    assert profile_of(session, "profile_toggle_on_probe")[0] is True
    assert profile_of(session, "profile_toggle_off_probe")[0] is False


def test_invalid_value_rejected(server):
    assert "capture_query_profile" in server.admin().error(sql="SET gizmosql.capture_query_profile = 'bogus'")["detail"]
