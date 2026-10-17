"""Port of GizmoSQL 1.x tests/integration/test_read_only_mode.cpp (1 case).

A database opened read-only (--readonly / -O / READONLY): the server starts (1.x's
regression was attaching its system catalog under a read-only parent), reads work, the
system views are queryable, and writes are refused. Applies as-is.
"""


def test_server_starts_and_system_catalog_is_queryable(launcher, tmp_path):
    database = tmp_path / "read_only.duckdb"
    writer = launcher.start(env={"HARDCHARGER_DATABASE": str(database),
                                 "HARDCHARGER_INIT_SQL_COMMANDS": "CREATE TABLE sentinel (n INTEGER); "
                                                                  "INSERT INTO sentinel VALUES (1);"})
    writer.stop()
    server = launcher.start(args=["--database", str(database), "--read-only"])
    session = server.admin()
    assert session.scalar(sql="SELECT n FROM sentinel") == 1
    session.query(sql="SELECT * FROM _hardcharger_system.main.hardcharger_index_info LIMIT 1")
    session.query(sql="SELECT * FROM _hardcharger_system.main.hardcharger_view_definition LIMIT 1")
    problem = session.error(sql="INSERT INTO sentinel VALUES (2)")
    assert "read-only" in problem["detail"].lower() or "read only" in problem["detail"].lower(), problem
