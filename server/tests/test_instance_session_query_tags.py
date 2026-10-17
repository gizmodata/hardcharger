"""Port of GizmoSQL 1.x tests/integration/test_instance_session_query_tags.cpp (15 cases; 11 server).

Tags: --instance-tag (HARDCHARGER_INSTANCE_TAG) is
recorded with the instance; SET hardcharger.session_tag and SET hardcharger.query_tag (JSON
objects; '' clears; anything else fails with "Invalid JSON") land in the sessions and
sql_statements tables and the views, and a query tag sticks to every later statement.
Writes are asynchronous, so the checks poll briefly. The 4 client cases run the `hardcharger`
client with --session-tag and --query-tag and their variables, HARDCHARGER_SESSION_TAG and
HARDCHARGER_QUERY_TAG (1.x: GIZMOSQL_*), then look the tag up with the client too. Added: a
tag that isn't JSON is refused with a warning, and the client goes on (as 1.x).
"""

import subprocess
import time

import pytest

from conftest import CLIENT_BINARY, clean_environment

INSTANCE_TAG = '{"env":"test","region":"us-east-1"}'


@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start(args=["--enable-instrumentation", "--instance-tag", INSTANCE_TAG])


@pytest.fixture
def admin(server):
    return server.admin()


def eventually(session, sql: str, predicate=lambda v: v is not None, timeout: float = 30):
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        rows = session.rows(sql=sql)
        value = rows[0][next(iter(rows[0]))] if rows else None
        if predicate(value):
            return value
        time.sleep(0.05)
    raise AssertionError(f"{sql} -> {value!r}")


def sid(session) -> str:
    return session.scalar(sql="SELECT HARDCHARGER_CURRENT_SESSION()")


def test_instance_tag_recorded_in_instrumentation_table(admin):
    tag = admin.scalar(sql="SELECT instance_tag::VARCHAR FROM _hardcharger_instr.instances WHERE instance_id = "
                           "HARDCHARGER_CURRENT_INSTANCE()::UUID")
    assert "us-east-1" in tag, tag


def test_set_session_tag_updates_sessions_table(admin):
    admin.execute(sql="""SET hardcharger.session_tag = '{"team":"data-eng","app":"etl"}'""")
    value = eventually(admin, f"SELECT session_tag::VARCHAR FROM _hardcharger_instr.sessions WHERE session_id = '{sid(admin)}'")
    assert "data-eng" in value


def test_set_session_tag_rejects_invalid_json(admin):
    assert "Invalid JSON" in admin.error(sql="SET hardcharger.session_tag = 'not-valid-json'")["detail"]


def test_set_query_tag_recorded_in_statements_table(admin):
    admin.execute(sql="""SET hardcharger.query_tag = '{"request_id":"abc-123"}'""")
    assert admin.scalar(sql="SELECT 42 AS tagged_query") == 42
    value = eventually(admin, "SELECT query_tag::VARCHAR FROM _hardcharger_instr.sql_statements "
                              "WHERE sql_text LIKE '%tagged_query%' AND sql_text NOT LIKE '%sql_statements%'")
    assert "abc-123" in value


def test_query_tag_persists_across_queries(admin):
    admin.execute(sql="""SET hardcharger.query_tag = '{"batch":"batch-42"}'""")
    admin.scalar(sql="SELECT 1 AS persist_query_one")
    admin.scalar(sql="SELECT 2 AS persist_query_two")
    eventually(admin, "SELECT count(*) FROM _hardcharger_instr.sql_statements WHERE sql_text LIKE '%persist_query_%' "
                      "AND sql_text NOT LIKE '%sql_statements%' AND query_tag->>'batch' = 'batch-42'", lambda n: n == 2)


def test_clear_query_tag_with_empty_string(admin):
    admin.execute(sql="""SET hardcharger.query_tag = '{"x":"y"}'""")
    admin.execute(sql="SET hardcharger.query_tag = ''")
    admin.scalar(sql="SELECT 1 AS cleared_tag_query")
    eventually(admin, "SELECT count(*) FROM _hardcharger_instr.sql_statements WHERE sql_text LIKE '%cleared_tag_query%' "
                      "AND sql_text NOT LIKE '%sql_statements%' AND query_tag IS NULL", lambda n: n == 1)


def test_set_query_tag_rejects_invalid_json(admin):
    assert "Invalid JSON" in admin.error(sql="SET hardcharger.query_tag = 'not-valid-json'")["detail"]


def test_doc_example_filter_sessions_by_tag(admin):
    admin.execute(sql="""SET hardcharger.session_tag = '{"team":"data-eng"}'""")
    team = eventually(admin, f"SELECT session_tag->>'team' AS team FROM _hardcharger_instr.sessions "
                             f"WHERE session_tag->>'team' = 'data-eng' AND session_id = '{sid(admin)}'")
    assert team == "data-eng"


def test_doc_example_filter_queries_by_request_id(admin):
    admin.execute(sql="""SET hardcharger.query_tag = '{"request_id":"req-doc-test"}'""")
    admin.scalar(sql="SELECT 99 AS doc_example_query")
    value = eventually(admin, "SELECT query_tag->>'request_id' AS request_id FROM _hardcharger_instr.sql_statements "
                              "WHERE query_tag->>'request_id' = 'req-doc-test' AND sql_text LIKE '%doc_example_query%' "
                              "AND sql_text NOT LIKE '%sql_statements%'")
    assert value == "req-doc-test"


def test_doc_example_aggregate_by_instance_tag(admin):
    admin.scalar(sql="SELECT 1 AS agg_test_query")
    env = eventually(admin, "SELECT i.instance_tag->>'env' AS environment FROM _hardcharger_instr.instances i "
                            "JOIN _hardcharger_instr.sessions s ON s.instance_id = i.instance_id "
                            "WHERE i.instance_tag->>'env' = 'test' GROUP BY 1")
    assert env == "test"


def test_tags_visible_in_instrumentation_views(admin):
    admin.execute(sql="""SET hardcharger.session_tag = '{"view":"session"}'""")
    admin.execute(sql="""SET hardcharger.query_tag = '{"view":"query"}'""")
    admin.scalar(sql="SELECT 1 AS view_tag_test")
    me = sid(admin)
    eventually(admin, f"SELECT session_tag->>'view' FROM _hardcharger_instr.active_sessions WHERE session_id = '{me}'",
               lambda v: v == "session")
    assert admin.scalar(sql=f"SELECT instance_tag->>'env' FROM _hardcharger_instr.active_sessions WHERE session_id = '{me}'") == "test"
    eventually(admin, "SELECT query_tag->>'view' FROM _hardcharger_instr.session_activity WHERE sql_text LIKE '%view_tag_test%' "
                      "AND sql_text NOT LIKE '%session_activity%'", lambda v: v == "query")
    eventually(admin, "SELECT session_tag->>'view' FROM _hardcharger_instr.execution_details WHERE sql_text LIKE '%view_tag_test%' "
                      "AND sql_text NOT LIKE '%execution_details%'", lambda v: v == "session")


def test_instance_tag_needs_no_license(launcher):
    # 1.x refused an instance tag without its license feature; Hardcharger has no license.
    server = launcher.start(args=["--enable-instrumentation", "--instance-tag", INSTANCE_TAG])
    assert server.admin().scalar(sql="SELECT instance_tag::VARCHAR FROM _hardcharger_instr.instances") is not None


# ---- The client's tag options (1.x: ClientSessionTagFlag, ClientQueryTagFlag,
# ClientSessionTagEnvVar, ClientQueryTagEnvVar) ----


def run_client(server, *args: str, **env: str) -> subprocess.CompletedProcess:
    if not CLIENT_BINARY.exists():
        pytest.fail(reason=f"{CLIENT_BINARY} is missing: build the server first")
    return subprocess.run(args=[str(CLIENT_BINARY), "--host", "127.0.0.1", "--port", str(server.port),
                                "--username", server.username, "--csv", "--no-header", *args],
                          env={**clean_environment(), "HARDCHARGER_PASSWORD": server.password, **env},
                          capture_output=True, text=True, timeout=60)


def run_client_until(server, sql: str, needle: str, timeout: float = 30) -> subprocess.CompletedProcess:
    """Repeats a lookup until its output has `needle` (instrumentation writes are asynchronous)."""
    deadline = time.monotonic() + timeout
    while True:
        result = run_client(server, "-c", sql)
        if (result.returncode == 0 and needle in result.stdout) or time.monotonic() > deadline:
            return result
        time.sleep(0.2)


def test_client_session_tag_flag(server):
    first = run_client(server, "--session-tag", '{"client_flag":"test"}', "-c", "SELECT 1")
    assert first.returncode == 0, first.stderr
    lookup = run_client_until(server, "SELECT session_tag::VARCHAR FROM _hardcharger_instr.sessions "
                                      "WHERE session_tag->>'client_flag' = 'test' LIMIT 1", "client_flag")
    assert lookup.returncode == 0, lookup.stderr
    assert "client_flag" in lookup.stdout, lookup.stdout


def test_client_query_tag_flag(server):
    first = run_client(server, "--query-tag", '{"pipeline":"client-test"}', "-c", "SELECT 1 AS client_tag_probe")
    assert first.returncode == 0, first.stderr
    lookup = run_client_until(server, "SELECT query_tag::VARCHAR FROM _hardcharger_instr.sql_statements "
                                      "WHERE sql_text LIKE '%client_tag_probe%' AND sql_text NOT LIKE '%sql_statements%' "
                                      "ORDER BY created_time DESC LIMIT 1", "client-test")
    assert lookup.returncode == 0, lookup.stderr
    assert "client-test" in lookup.stdout, lookup.stdout


def test_client_session_tag_env_var(server):
    first = run_client(server, "-c", "SELECT 1", HARDCHARGER_SESSION_TAG='{"env_var":"session_test"}')
    assert first.returncode == 0, first.stderr
    lookup = run_client_until(server, "SELECT session_tag::VARCHAR FROM _hardcharger_instr.sessions "
                                      "WHERE session_tag->>'env_var' = 'session_test' LIMIT 1", "env_var")
    assert lookup.returncode == 0, lookup.stderr
    assert "session_test" in lookup.stdout, lookup.stdout


def test_client_query_tag_env_var(server):
    first = run_client(server, "-c", "SELECT 1 AS env_query_tag_probe", HARDCHARGER_QUERY_TAG='{"env_var":"query_test"}')
    assert first.returncode == 0, first.stderr
    lookup = run_client_until(server, "SELECT query_tag::VARCHAR FROM _hardcharger_instr.sql_statements "
                                      "WHERE sql_text LIKE '%env_query_tag_probe%' AND sql_text NOT LIKE '%sql_statements%' "
                                      "ORDER BY created_time DESC LIMIT 1", "query_test")
    assert lookup.returncode == 0, lookup.stderr
    assert "query_test" in lookup.stdout, lookup.stdout


def test_client_invalid_tag_is_a_warning(server):
    result = run_client(server, "--query-tag", "not-json", "-c", "SELECT 'went on'")
    assert result.returncode == 0, result.stderr
    assert result.stderr.startswith("Warning: failed to set query_tag: "), result.stderr
    assert "Invalid JSON" in result.stderr, result.stderr
    assert result.stdout.strip() == "went on"
    # A GizmoSQL 1.x variable does nothing.
    ignored = run_client(server, "-c", "SELECT 1 AS gizmo_env_probe", GIZMOSQL_QUERY_TAG='{"from":"gizmosql"}')
    assert ignored.returncode == 0 and ignored.stderr == "", ignored.stderr
