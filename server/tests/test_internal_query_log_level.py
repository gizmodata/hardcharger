"""Port of GizmoSQL 1.x tests/integration/test_internal_query_log_level.cpp (3 cases).

The SQL a metadata request runs is logged only at the DEBUG query log level, at DEBUG;
an eager INSERT logs "Client SQL command execution succeeded" exactly once at INFO,
whatever the logger's own threshold (here WARNING), and reading its statement again
logs nothing. All 3 apply.
"""


def internal_lines(server) -> list[dict]:
    return [r for r in server.log_records() if r.get("is_internal") == "true"]


def test_metadata_sql_not_logged_at_info_threshold(launcher):
    server = launcher.start(env={"GIZMOSQL_LOG_FORMAT": "json", "PRINT_QUERIES": "true"})
    session = server.admin()
    session.execute(sql="CREATE TABLE log_test_table (id INT)")
    tables = session.metadata(kind="tables").column("table_name").to_pylist()
    assert "log_test_table" in tables
    assert internal_lines(server) == []


def test_eager_insert_logs_success_once_regardless_of_logger_threshold(launcher):
    server = launcher.start(env={"GIZMOSQL_LOG_FORMAT": "json", "PRINT_QUERIES": "true", "GIZMOSQL_LOG_LEVEL": "warning",
                                 "GIZMOSQL_QUERY_LOG_LEVEL": "info"})
    session = server.admin()
    session.execute(sql="CREATE TABLE eager_log_contract (n INT)")
    doc = session.execute(sql="INSERT INTO eager_log_contract VALUES (1)")

    def successes():
        return [r for r in server.log_records() if "eager_log_contract VALUES" in r.get("sql", "")
                and r.get("message") == "Client SQL command execution succeeded"]

    assert len(successes()) == 1 and successes()[0]["level"] == "info", successes()
    assert session.status(statement_id=doc["statement_id"])["state"] == "succeeded"
    assert len(successes()) == 1, "reading the statement again must log nothing"


def test_metadata_sql_logged_at_debug_threshold(launcher):
    server = launcher.start(env={"GIZMOSQL_LOG_FORMAT": "json", "PRINT_QUERIES": "true",
                                 "GIZMOSQL_QUERY_LOG_LEVEL": "debug"})
    session = server.admin()
    session.execute(sql="CREATE TABLE log_test_table (id INT)")
    assert "log_test_table" in session.metadata(kind="tables").column("table_name").to_pylist()
    lines = internal_lines(server)
    assert lines and all(r["level"] == "debug" for r in lines), lines
