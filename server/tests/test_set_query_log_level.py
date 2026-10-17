"""Port of GizmoSQL 1.x tests/integration/test_set_query_log_level.cpp (2 cases).

With the query log level at INFO, a client statement is logged ("is_internal": "false")
and the SQL a metadata request runs (1.x: DoGetTables; Warp SQL: metadata/tables) is
not; after SET hardcharger.query_log_level = DEBUG in the session it is, at DEBUG. SET
GLOBAL from another session changes an existing session's level at once. Both apply;
log lines are read from the server's JSON log.
"""

import pytest


@pytest.fixture
def server(launcher):
    return launcher.start(env={"HARDCHARGER_LOG_FORMAT": "json", "HARDCHARGER_PRINT_QUERIES": "true", "HARDCHARGER_LOG_LEVEL": "info"})


def lines(server, since: int, session_id: str | None = None) -> list[dict]:
    records = server.log_records()[since:]
    return [r for r in records if session_id is None or r.get("session_id") == session_id]


def internal(records: list[dict]) -> list[dict]:
    return [r for r in records if r.get("is_internal") == "true"]


def test_session_set_changes_log_threshold(server):
    session = server.admin()
    sid = session.scalar(sql="SELECT HARDCHARGER_CURRENT_SESSION()")
    mark = len(server.log_records())
    session.scalar(sql="SELECT 1")
    session.metadata(kind="tables")
    phase1 = lines(server=server, since=mark, session_id=sid)
    assert any(r.get("is_internal") == "false" for r in phase1), phase1
    assert not internal(phase1), "a metadata request's SQL must not be logged at INFO"

    session.execute(sql="SET hardcharger.query_log_level = DEBUG")
    mark = len(server.log_records())
    session.scalar(sql="SELECT 2")
    session.metadata(kind="tables")
    phase2 = lines(server=server, since=mark, session_id=sid)
    assert any(r.get("is_internal") == "false" for r in phase2), phase2
    assert internal(phase2) and all(r["level"] == "debug" for r in internal(phase2)), phase2


def test_set_global_propagates_immediately_to_existing_sessions(server):
    a = server.admin()
    a_id = a.scalar(sql="SELECT HARDCHARGER_CURRENT_SESSION()")
    b = server.admin()
    mark = len(server.log_records())
    a.metadata(kind="tables")
    assert not internal(lines(server=server, since=mark, session_id=a_id))
    b.execute(sql="SET GLOBAL hardcharger.query_log_level = DEBUG")
    mark = len(server.log_records())
    a.metadata(kind="tables")
    assert internal(lines(server=server, since=mark, session_id=a_id)), "SET GLOBAL did not reach session A"
    b.execute(sql="SET GLOBAL hardcharger.query_log_level = INFO")
    mark = len(server.log_records())
    a.metadata(kind="tables")
    assert not internal(lines(server=server, since=mark, session_id=a_id))
