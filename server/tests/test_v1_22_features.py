"""Port of GizmoSQL 1.x tests/test_v1_22_features.py (2 cases).

1.x ran these through the ADBC Flight SQL driver against a server on port 31337; here
they run over Chargewire against a server the test starts. Both apply as-is.
"""

import pytest


@pytest.fixture(scope="module")
def session(module_launcher):
    return module_launcher.start().admin()


def test_index_info(session):
    session.execute(sql="CREATE TABLE v122_ix(a INT, b INT, c VARCHAR)")
    session.execute(sql="CREATE INDEX v122_ix_ab ON v122_ix(a, b)")
    session.execute(sql="CREATE UNIQUE INDEX v122_ix_c ON v122_ix(c)")
    rows = session.query(sql='SELECT "TABLE_NAME", "NON_UNIQUE", "INDEX_NAME", "ORDINAL_POSITION", "COLUMN_NAME", '
                             '"ASC_OR_DESC", "TYPE" FROM _hardcharger_system.main.hardcharger_index_info '
                             "WHERE \"TABLE_NAME\" = 'v122_ix' ORDER BY \"INDEX_NAME\", \"ORDINAL_POSITION\"").to_pylist()
    assert len(rows) == 3, rows
    ab = [r for r in rows if r["INDEX_NAME"] == "v122_ix_ab"]
    assert [(r["COLUMN_NAME"], r["ORDINAL_POSITION"]) for r in ab] == [("a", 1), ("b", 2)]
    assert all(r["NON_UNIQUE"] is True for r in ab), "a compound non-unique index has NON_UNIQUE=true"
    c = [r for r in rows if r["INDEX_NAME"] == "v122_ix_c"]
    assert [(r["COLUMN_NAME"], r["ORDINAL_POSITION"], r["NON_UNIQUE"]) for r in c] == [("c", 1, False)]
    assert all(r["TYPE"] == 3 for r in rows), "TYPE is tableIndexOther (3)"
    assert all(r["ASC_OR_DESC"] == "A" for r in rows)


def test_view_definition(session):
    session.execute(sql="CREATE TABLE v122_vt(x INT)")
    session.execute(sql="CREATE OR REPLACE VIEW v122_vv AS SELECT x + 1 AS x1 FROM v122_vt")
    rows = session.rows(sql='SELECT "VIEW_DEFINITION" FROM _hardcharger_system.main.hardcharger_view_definition '
                            "WHERE \"TABLE_NAME\" = 'v122_vv'")
    assert len(rows) == 1
    ddl = rows[0]["VIEW_DEFINITION"]
    assert "CREATE" in ddl.upper() and "v122_vv" in ddl, ddl
