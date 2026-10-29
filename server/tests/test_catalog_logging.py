"""Port of GizmoSQL 1.x tests/integration/test_catalog_logging.cpp (3 cases).

Catalog logging (--enable-catalog-logging,
HARDCHARGER_ENABLE_CATALOG_LOGGING): every log line also lands in _hardcharger_logs.logs, a
DuckDB file next to the database (hardcharger_logs.db, or --log-catalog-db-path /
HARDCHARGER_LOG_CATALOG_DB_PATH), with 1.x's columns. The catalog is read-only to every client and
readable by admins only. The PostgreSQL and DuckLake backends are in
test_instrumentation_ducklake.py, as in 1.x.

Strengthened: 1.x checked that some rows carry an instance id; here every row carries
this server's own instance id, the session's own lines carry its session id and user,
non-admins can neither read nor list the catalog, it can't be made current, it needs no
license (1.x's gate is not applicable), the file's location is checked, and the writer's column mapping is a C++ unit
test (CatalogLogTest).
"""

import time

import pytest

from conftest import run_unit_tests

CLUSTER_ID = "abcdef01-2345-6789-abcd-ef0123456789"
LEVELS = "('DEBUG', 'INFO', 'WARN', 'ERROR', 'FATAL')"


@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start(args=["--enable-catalog-logging", "--cluster-id", CLUSTER_ID,
                                       "--log-catalog-db-path", "catalog_log_test_logs.db"])


@pytest.fixture
def admin(server):
    return server.admin()


def eventually(session, sql: str, predicate, timeout: float = 10):
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = session.scalar(sql=sql)
        if predicate(value):
            return value
        time.sleep(0.05)
    raise AssertionError(f"{sql} -> {value!r}")


def generate_logs(session) -> None:
    for _ in range(3):
        assert session.scalar(sql="SELECT 42 AS catalog_log_probe") == 42


def test_logs_forked_to_catalog(admin):
    generate_logs(session=admin)
    sid = admin.scalar(sql="SELECT HARDCHARGER_CURRENT_SESSION()")
    instance = admin.scalar(sql="SELECT HARDCHARGER_CURRENT_INSTANCE()")
    # The session's own "session opened" line, with its id and user in their columns.
    eventually(admin, f"SELECT count(*) FROM _hardcharger_logs.logs WHERE session_id = '{sid}' "
                      "AND username = 'admin' AND message = 'session opened'", lambda n: n == 1)
    # One statement, one snapshot: every query adds lines of its own.
    row = admin.rows(sql=f"SELECT count(*) AS total, "
                         f"count(*) FILTER (WHERE log_time IS NOT NULL AND level IN {LEVELS}) AS well_formed, "
                         f"count(*) FILTER (WHERE instance_id::VARCHAR = '{instance}') AS ours "
                         f"FROM _hardcharger_logs.logs")[0]
    assert row["total"] > 0 and row["well_formed"] == row["total"] and row["ours"] == row["total"], row


def test_cluster_id_in_logs_and_function(admin):
    assert admin.scalar(sql="SELECT HARDCHARGER_CURRENT_CLUSTER() AS c") == CLUSTER_ID
    generate_logs(session=admin)
    eventually(admin, "SELECT count(*) FROM _hardcharger_logs.logs", lambda n: n > 0)
    row = admin.rows(sql=f"SELECT count(*) AS total, count(*) FILTER "
                         f"(WHERE CAST(cluster_id AS VARCHAR) = '{CLUSTER_ID}') AS clustered FROM _hardcharger_logs.logs")[0]
    assert row["clustered"] == row["total"] > 0, row


def test_log_catalog_is_read_only_to_clients(server, admin):
    admin.query(sql="SELECT * FROM _hardcharger_logs.logs LIMIT 1")
    detail = admin.error(sql="DELETE FROM _hardcharger_logs.logs")["detail"]
    assert "read-only" in detail and "log catalog" in detail, detail
    detail = admin.error(sql="DETACH _hardcharger_logs")["detail"]
    assert "Cannot DETACH" in detail, detail
    assert "read-only" in admin.error(sql="USE _hardcharger_logs")["detail"]
    # Non-admins: no reads, and the catalog isn't listed.
    user = server.as_role(role="user", subject="reader")
    problem = user.error(sql="SELECT count(*) FROM _hardcharger_logs.logs")
    assert problem["code"] == "permission_denied", problem
    assert "Only administrators can read the log catalog '_hardcharger_logs'" in problem["detail"], problem
    catalogs = user.metadata(kind="catalogs").column(0).to_pylist()
    assert "_hardcharger_logs" not in catalogs and catalogs, catalogs
    assert "_hardcharger_logs" in admin.metadata(kind="catalogs").column(0).to_pylist()


def test_settings_report_the_log_catalog(admin):
    rows = {r["name"]: r["value"] for r in admin.rows(
        sql="SELECT name, value FROM hardcharger_settings() WHERE name IN ('hardcharger.enable_catalog_logging', "
            "'hardcharger.log_catalog', 'hardcharger.log_schema')")}
    assert rows == {"hardcharger.enable_catalog_logging": "true", "hardcharger.log_catalog": "_hardcharger_logs",
                    "hardcharger.log_schema": "main"}, rows


def test_log_file_location(launcher, tmp_path):
    # Default: hardcharger_logs.db next to the database file.
    (tmp_path / "data").mkdir()
    server = launcher.start(args=["--enable-catalog-logging", "--database", "data/app.duckdb"])
    eventually(server.admin(), "SELECT count(*) FROM _hardcharger_logs.logs", lambda n: n > 0)
    assert (tmp_path / "data" / "hardcharger_logs.db").exists()
    server.stop()
    # HARDCHARGER_LOG_CATALOG_DB_PATH, with a read-only database: the log file is still written.
    server = launcher.start(args=["--database", "data/app.duckdb", "--read-only"],
                            env={"HARDCHARGER_ENABLE_CATALOG_LOGGING": "true",
                                 "HARDCHARGER_LOG_CATALOG_DB_PATH": str(tmp_path / "elsewhere.db")})
    eventually(server.admin(), "SELECT count(*) FROM _hardcharger_logs.logs WHERE message = 'session opened'",
               lambda n: n > 0)
    assert (tmp_path / "elsewhere.db").exists()


def test_needs_no_license(launcher):
    # 1.x refused catalog logging without its license feature; Hardcharger has no license.
    server = launcher.start(args=["--enable-catalog-logging"])
    assert server.admin().scalar(sql="SELECT count(*) > 0 FROM _hardcharger_logs.main.logs") is True


def test_unattached_log_catalog_stops_the_server(launcher):
    server = launcher.start(args=["--enable-catalog-logging", "--log-catalog", "no_such_catalog"], expect_up=False)
    assert server.process.wait(timeout=20) != 0
    assert "log_catalog 'no_such_catalog' is not attached" in server.log(), server.log()


def test_writer_unit():
    assert run_unit_tests(gtest_filter="CatalogLogTest.*") == {"CatalogLogTest.LinesLandInTheirColumns": True}


def test_a_log_table_from_0_3_0_is_refused_with_what_to_do(launcher, tmp_path):
    """0.3.0's logs table typed session_id UUID; session ids are now Chargewire session ids
    (s_...): startup says so instead of dropping every session's lines."""
    path = tmp_path / "old_logs.db"
    maker = launcher.start(args=["--database", str(path)])
    maker.admin().execute(sql="CREATE TABLE logs (log_time TIMESTAMP WITH TIME ZONE NOT NULL, level VARCHAR NOT NULL, "
                              "instance_id UUID, cluster_id UUID, session_id UUID, username VARCHAR, role VARCHAR, "
                              "peer VARCHAR, component VARCHAR, trace_id VARCHAR, span_id VARCHAR, pid INTEGER, "
                              "tid VARCHAR, source_file VARCHAR, source_line INTEGER, func VARCHAR, message VARCHAR, "
                              "fields JSON)")
    maker.stop()
    server = launcher.start(args=["--enable-catalog-logging", "--log-catalog-db-path", str(path)], expect_up=False)
    assert server.process.wait(timeout=60) != 0
    assert "from Hardcharger 0.3.0, whose session ids were UUIDs" in server.log(), server.log()
