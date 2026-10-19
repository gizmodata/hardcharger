"""A view in catalog x that reads a schema also named x (found by adbc-replicate).

DuckDB 2.0 drops a view's own catalog from the names in its query, so the view keeps
working under another alias (Binder::BindCreateViewInfo, RemotePushdownOptimizer::
StripCatalogName), and binds what is left as a two-part name, with the ambiguity check
for one: CREATE VIEW x.x.v AS SELECT * FROM x.x.t reads x.x.t as x.t and fails, "Ambiguous
reference to catalog or schema", though the name was fully qualified. DuckDB 1.5 creates
it. Plain DuckDB on the same build (the warp-duckdb reference server, none of
Hardcharger's checks) fails the same way, with access control on or off, so it is
DuckDB's; Hardcharger adds what really happens and what works to DuckDB's message.

When a DuckDB update creates the view, test_the_fully_qualified_view_is_refused_and_says_why
fails: remove the note (Session ExplainViewAmbiguity) and turn it into a test that the
view is created.
"""

import pytest

from rbac import start_rbac_server

NOTE = "DuckDB 2.0 drops the view's own catalog from the names in its query"


@pytest.fixture(params=["access_control_off", "access_control_on"])
def admin(request, launcher, tmp_path):
    if request.param == "access_control_off":
        session = launcher.start().admin()
        run = lambda sql: session.execute(sql=sql)  # noqa: E731
        fail = lambda sql: session.error(sql=sql)["detail"]  # noqa: E731
        rows = lambda sql: session.rows(sql=sql)  # noqa: E731
    else:
        store = request.getfixturevalue("store")
        server = start_rbac_server(launcher=launcher, store_uri=store.new_database())
        client = server.admin()
        from rbac import WarpError
        run = lambda sql: client.run(sql=sql)  # noqa: E731

        def fail(sql):
            with pytest.raises(WarpError) as raised:
                client.run(sql=sql)
            return raised.value.detail

        rows = lambda sql: client.run(sql=sql).to_pylist()  # noqa: E731
    for sql in [f"ATTACH '{tmp_path / 'x.duckdb'}' AS x", "CREATE SCHEMA x.x", "CREATE TABLE x.x.t AS SELECT 1 AS a",
                "CREATE TABLE x.main.t AS SELECT 2 AS a"]:
        run(sql)
    return run, fail, rows


def test_the_fully_qualified_view_is_refused_and_says_why(admin):
    run, fail, rows = admin
    for sql in ["CREATE VIEW x.x.v AS SELECT * FROM x.x.t", "CREATE OR REPLACE VIEW x.main.v AS SELECT * FROM x.x.t"]:
        detail = fail(sql)
        assert 'Ambiguous reference to catalog or schema "x"' in detail, detail
        assert NOTE in detail and "CREATE VIEW memory.main.v AS SELECT * FROM x.x.t" in detail, detail


def test_the_view_from_another_catalog_works(admin):
    run, fail, rows = admin
    run("CREATE VIEW memory.main.reads_x_x AS SELECT * FROM x.x.t")
    assert rows("SELECT a FROM memory.main.reads_x_x") == [{"a": 1}]


def test_other_ambiguity_errors_keep_duckdbs_message(admin):
    run, fail, rows = admin
    # A two-part name that really is ambiguous, outside a view: DuckDB's advice is right.
    detail = fail("USE x.x; SELECT * FROM x.t")
    assert 'Ambiguous reference to catalog or schema "x"' in detail and NOTE not in detail, detail
