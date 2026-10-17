"""Port of GizmoSQL 1.x tests/integration/test_system_catalog.cpp (4 cases).

_gizmosql_system holds the server's read-only views (gizmosql_index_info,
gizmosql_view_definition). Every write to it is refused, for every user: Hardcharger
attaches it READ_ONLY, so DuckDB itself refuses (its message says "read-only"). Added:
an admin can't DETACH it either. All 4 cases apply.
"""

import pytest


@pytest.fixture(scope="module")
def session(module_launcher):
    return module_launcher.start().admin()


def test_reads_are_allowed(session):
    session.query(sql="SELECT * FROM _gizmosql_system.main.gizmosql_index_info LIMIT 1")
    session.query(sql="SELECT * FROM _gizmosql_system.main.gizmosql_view_definition LIMIT 1")


@pytest.mark.parametrize("sql", [
    "CREATE TABLE _gizmosql_system.main.t_should_fail (id INTEGER)",   # CreateInSystemCatalogIsRejected
    "DROP VIEW _gizmosql_system.main.gizmosql_index_info",             # DropViewInSystemCatalogIsRejected
    "CREATE OR REPLACE VIEW _gizmosql_system.main.evil AS SELECT 1",   # InsertIntoSystemCatalogIsRejected
], ids=["create_table", "drop_view", "create_or_replace_view"])
def test_writes_to_system_catalog_are_rejected(session, sql):
    problem = session.error(sql=sql)
    assert "read-only" in problem["detail"], problem


def test_system_catalog_cannot_be_detached(session):
    problem = session.error(sql="DETACH _gizmosql_system")
    assert problem["code"] == "permission_denied", problem
    assert "Cannot DETACH" in problem["detail"], problem
