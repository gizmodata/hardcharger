"""Port of GizmoSQL 1.x tests/integration/test_instance_session_query_tags.cpp (15 cases; 11 server).

Tags: --instance-tag (HARDCHARGER_INSTANCE_TAG) is
recorded with the instance; SET hardcharger.session_tag and SET hardcharger.query_tag (JSON
objects; '' clears; anything else fails with "Invalid JSON") land in the sessions and
sql_statements tables and the views, and a query tag sticks to every later statement.
Writes are asynchronous, so the checks poll briefly. Not yet: the 4 client cases
(--session-tag, --query-tag and their HARDCHARGER_* variables) need a Warp SQL client binary.
"""

import time

import pytest

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
