"""Where a stored expression finds its functions, under access control (authz/name_scope.hpp).

A view's query, a column default, a CHECK constraint, a generated column, a trigger's body and
a row or masking policy are kept as written and bound again in the session of whoever reads or
writes. DuckDB finds a function through that session's search path (USE, SET search_path, the
current catalog) and reads `x.f()` as `f(x)`, so a user who could create a macro in any schema
could have it called in place of the one the expression meant: a masking policy that returns
the value, a row policy that is true, a CHECK that passes, a macro of his running in another
user's trigger with her rights. Released 2.13.0 behaves so.

Now each stored expression has a name scope, one schema fixed by its object (its table's, its
view's; a policy's is main), and finds a function, macro or type there and among DuckDB's own
only, whatever the session, an administrator's included; `schema.f` in the scope's catalog;
never as a method call of a macro. What is not found that way is refused, saying what to write.

Every probe of the two reviews of the earlier, route-by-route fixes is here: on a database a
2.13.0 server made (name_scope_legacy.py: the user's macros were already there), and on new
ones, each next to the twin that works. An attack that is silently closed is asserted by what the
statement returns (the genuine macro's result), a refusal by its words."""

import gzip
import pathlib

import pytest

from name_scope_legacy import (LEGACY, LEGACY_ACCESS, LEGACY_MIA_ACCESS, LEGACY_POLICIES, LEGACY_PRINCIPALS,
                               LEGACY_TRIGGERS_AND_VIEWS)
from rbac import STORE_KIND, ChargewireError, expect_allowed, expect_blocked, start_rbac_server

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
L = LEGACY

# The fixture's access-control store is a DuckDB one. With another store (PostgreSQL) the
# database is the fixture's and the principals, grants and policies are made again; who made
# which view and trigger is on record in the fixture's store alone, so there they read and
# write with the caller's rights, and the writers get what those need. (What each expression
# computes is the same either way, which is what these tests assert.) The policy 2.13.0 took
# with a method call can't be made again: 2.14 refuses it at CREATE.
OWNERS_ON_RECORD = STORE_KIND == "duckdb"
WITHOUT_OWNERS = [
    "GRANT SELECT ON main.people TO writers", "GRANT SELECT ON app.cards TO writers",
    "GRANT SELECT ON main.other_secret TO writers", "GRANT SELECT ON app.mvault TO writers",
    "GRANT SELECT ON app.mvault2 TO writers", "GRANT SELECT ON app.mvault3 TO writers",
    "GRANT INSERT ON app.mlabels TO writers", "GRANT INSERT ON app.mlabels2 TO writers",
    "GRANT INSERT ON app.mlog TO writers", "GRANT INSERT ON app.mnotes TO writers",
]

NOT_FOUND = "was not found. There a"
NO_SESSION = "is never looked up through the session's search path"
CHECK_FAILED = "CHECK constraint failed"


@pytest.fixture
def legacy(launcher, store, tmp_path):
    """A server on a copy of the database a 2.13.0 server made."""
    database = tmp_path / f"{L}.duckdb"
    database.write_bytes(gzip.decompress(data=(FIXTURES / f"{L}.duckdb.gz").read_bytes()))
    if OWNERS_ON_RECORD:
        rbac = tmp_path / f"{L}.rbac.duckdb"
        rbac.write_bytes(gzip.decompress(data=(FIXTURES / f"{L}.rbac.duckdb.gz").read_bytes()))
        rbac.chmod(mode=0o600)
        return start_rbac_server(launcher=launcher, store_uri=f"duckdb:{rbac}", args=["--database", str(database)])
    server = start_rbac_server(launcher=launcher, store_uri=store.new_database(), args=["--database", str(database)])
    admin = server.admin()
    for sql in LEGACY_PRINCIPALS + LEGACY_POLICIES + LEGACY_ACCESS + LEGACY_MIA_ACCESS + WITHOUT_OWNERS:
        expect_allowed(client=admin, sql=sql)
    # The trigger that copies deleted rows vouches for that only as an administrator's on record.
    copies = next(sql for sql in LEGACY_TRIGGERS_AND_VIEWS if sql.startswith("CREATE TRIGGER src_d "))
    expect_allowed(client=admin, sql=copies.replace("CREATE TRIGGER", "CREATE OR REPLACE TRIGGER", 1))
    return server


def search_paths(catalog: str) -> list[str]:
    """What a user may do to its session to be found first: its own schema ahead, by USE and by
    SET search_path, and another catalog as the current one (`main.f` is then no longer this
    database's main, and DuckDB falls back to the method call f(main, ...))."""
    return ["", "USE ws", f"SET search_path = '{catalog}.ws,{catalog}.main'",
            f"SET search_path = 'temp.main,{catalog}.ws,{catalog}.app,{catalog}.fin,{catalog}.util'",
            f"SET search_path = 'system.main,{catalog}.ws,{catalog}.app,{catalog}.fin,{catalog}.util'"]


def sessions(server, catalog: str, user: str = "wes"):
    """A session of `user` for each of those."""
    for path in search_paths(catalog=catalog):
        client = server.admin() if user == "admin" else server.user(name=user)
        if path:
            expect_allowed(client=client, sql=path)
        yield path, client


def at_home(path: str) -> bool:
    """Is the session's current catalog still the database's? Where it is not, a trigger whose
    body names a TABLE of main (`main.audit_log`: a table's name without its catalog follows the
    session's current catalog, triggers.hpp) does not fire: DuckDB finds no such table. That is
    the tables' rule, older than this file, and it fails closed."""
    return "temp.main" not in path and "system.main" not in path


def fires(client, path: str, sql: str) -> bool:
    """Run a write that fires a trigger whose body names a table of main; did it fire?"""
    if at_home(path=path):
        expect_allowed(client=client, sql=sql)
        return True
    fails(client=client, sql=sql, why="does not exist")
    return False


def rows(client, sql: str) -> list[tuple]:
    table = expect_allowed(client=client, sql=sql)
    return [tuple(row.values()) for row in table.to_pylist()]


def fails(client, sql: str, why: str, parameters=None) -> str:
    """The statement fails, saying `why`, and tells nothing a macro of the user's would."""
    with pytest.raises(ChargewireError) as raised:
        client.run(sql=sql, parameters=parameters)
    detail = raised.value.detail
    assert why in detail, f"{sql}: expected '{why}' in: {detail}"
    assert "LEAK" not in detail and "secret-" not in detail, detail
    return detail


def not_found(client, sql: str, name: str, whose: str, remedy: str) -> str:
    """Refused because the stored expression's scope does not find `name`, saying what to do."""
    detail = fails(client=client, sql=sql, why=NOT_FOUND)
    assert f"{name}, which {whose} calls, {NOT_FOUND}" in detail and NO_SESSION in detail and remedy in detail, detail
    return detail


# ---- a database from before 2.14: the user's macros of those names already exist ----

def test_a_check_from_before_holds_whatever_the_session(legacy):
    # Reviews: first f01; second g05 A (a name with its schema, the current catalog changed).
    admin = legacy.admin()
    for user in ["wes", "admin"]:
        for path, client in sessions(server=legacy, catalog=L, user=user):
            for table in ["checked", "qchecked", "nested"]:
                fails(client=client, sql=f"INSERT INTO {L}.main.{table} VALUES (-1)", why=CHECK_FAILED)
                expect_allowed(client=client, sql=f"INSERT INTO {L}.main.{table} VALUES (1)")
            # Through an administrator's trigger that copies the rows into the checked table.
            fails(client=client, sql=f"INSERT INTO {L}.main.pay VALUES (-1)", why=CHECK_FAILED if at_home(path=path) else "does not exist")
            fires(client=client, path=path, sql=f"INSERT INTO {L}.main.pay VALUES (1)")
    assert rows(client=admin, sql="SELECT DISTINCT x FROM main.checked UNION ALL SELECT DISTINCT x FROM main.qchecked "
                                  "UNION ALL SELECT DISTINCT x FROM main.nested") == [(1,), (1,), (1,)]


def test_a_statement_prepared_before_or_after_use_binds_a_check_the_same(legacy):
    wes = legacy.user(name="wes")
    before = wes.prepare(sql=f"INSERT INTO {L}.main.checked VALUES (?)")
    expect_allowed(client=wes, sql="USE ws")
    after = wes.prepare(sql=f"INSERT INTO {L}.main.checked VALUES (?)")
    for prepared in [before, after]:
        with pytest.raises(ChargewireError) as raised:
            wes.execute_prepared(prepared_id=prepared, parameters=[-1])
        assert CHECK_FAILED in raised.value.detail, raised.value.detail
        wes.execute_prepared(prepared_id=prepared, parameters=[2])
    assert rows(client=legacy.admin(), sql="SELECT x FROM main.checked") == [(2,), (2,)]
    # A generated column read, and a default written into the statement, by statements prepared
    # before USE and run on both sides of it (DuckDB binds them again after USE).
    wes = legacy.user(name="wes")
    read = wes.prepare(sql=f"SELECT shown FROM {L}.rpt.people WHERE name = ?")
    write = wes.prepare(sql=f"INSERT INTO {L}.main.dflt_pk VALUES (?, DEFAULT)")
    for key, use in enumerate(["", "USE ws", f"SET search_path = '{L}.ws,{L}.main'"]):
        if use:
            expect_allowed(client=wes, sql=use)
        assert wes.execute_prepared(prepared_id=read, parameters=["ann"]).to_pylist() == [{"shown": "***11"}], use
        wes.execute_prepared(prepared_id=write, parameters=[900 + key])
    assert rows(client=legacy.admin(), sql="SELECT DISTINCT note FROM main.dflt_pk") == [("genuine default",)]


def test_a_method_call_of_a_macro_from_before_is_refused_not_guessed(legacy):
    # Second review g02 A and D, g03 shape 3: `x.is_valid()` was read as is_valid(x), the
    # session's. Only DuckDB's own functions are called that way in a stored expression now.
    admin = legacy.admin()
    table = "a CHECK constraint, generated column, column default or index of table"
    for user in ["wes", "admin"]:
        for path, client in sessions(server=legacy, catalog=L, user=user):
            for value in [1, -1]:
                detail = not_found(client=client, sql=f"INSERT INTO {L}.main.mchecked VALUES ({value})",
                                   name="function x.is_valid", whose=f"{table} {L}.main.mchecked",
                                   remedy="Re-create the table with the function named with its schema")
                assert "is read as a method call for DuckDB's own functions only" in detail, detail
    assert rows(client=admin, sql="SELECT count(*) FROM main.mchecked") == [(0,)]
    # A trigger's own action (mia's, with her rights).
    for path, wes in sessions(server=legacy, catalog=L):
        not_found(client=wes, sql=f"INSERT INTO {L}.app.mine3 VALUES (1)", name="function secret.fmt",
                  whose=f"a trigger on table {L}.app.mine3", remedy="CREATE OR REPLACE TRIGGER")
    assert rows(client=admin, sql="SELECT count(*) FROM app.mnotes") == [(0,)]
    # Its owner writes it the way the refusal says: it fires for every session, with the macro meant.
    mia = legacy.user(name="mia") if OWNERS_ON_RECORD else admin
    expect_allowed(client=mia, sql="CREATE OR REPLACE TRIGGER mine3_ins AFTER INSERT ON app.mine3 FOR EACH STATEMENT "
                                   "INSERT INTO app.mnotes SELECT main.fmt(secret) FROM app.mvault3")
    for path, wes in sessions(server=legacy, catalog=L):
        expect_allowed(client=wes, sql=f"INSERT INTO {L}.app.mine3 VALUES (1)")
    assert rows(client=admin, sql="SELECT DISTINCT note FROM app.mnotes") == [("genuine vault3-secret-1",)]
    if OWNERS_ON_RECORD:
        # A row policy 2.13.0 took with a method call: the table is not readable through it
        # until an administrator writes the policy as a call.
        for path, wes in sessions(server=legacy, catalog=L):
            not_found(client=wes, sql=f"SELECT name FROM {L}.main.emp3", name="function region.in_region2",
                      whose=f"a row or masking policy on table {L}.main.emp3", remedy="re-create the policy")
        expect_allowed(client=admin, sql="DROP ROW POLICY not_latam ON main.emp3")
        expect_allowed(client=admin, sql="CREATE ROW POLICY not_latam ON main.emp3 TO writers USING (in_region2(region))")
        for path, wes in sessions(server=legacy, catalog=L):
            assert rows(client=wes, sql=f"SELECT name FROM {L}.main.emp3 ORDER BY name") == [("ann",), ("bob",)], path


def test_a_generated_column_from_before_is_its_tables_in_views_and_trigger_bodies_too(legacy):
    # Reviews: first f02; second g01 (through a view: a macro in the view's schema, or one the
    # session's path finds first, with the view owner's rights), g05 B.
    admin = legacy.admin()
    for user in ["wes", "admin"]:
        for path, client in sessions(server=legacy, catalog=L, user=user):
            masked = [("ann", "***11"), ("bob", "***22")]
            assert rows(client=client, sql=f"SELECT * FROM {L}.rpt.people ORDER BY name") == masked, path
            assert rows(client=client, sql=f"SELECT * FROM {L}.main.people_v ORDER BY name") == masked, path
            assert rows(client=client, sql=f"SELECT * FROM {L}.main.cards_v") == [("ann", "###11")], path
            assert rows(client=client, sql=f"SELECT mm FROM {L}.main.moods") == [("ok",)], path
            fires(client=client, path=path, sql=f"INSERT INTO {L}.main.req VALUES (1)")
            # Computed again by DuckDB when a trigger's body appends the rows.
            fires(client=client, path=path, sql=f"INSERT INTO {L}.main.gen_src VALUES ('abc')")
    assert rows(client=admin, sql="SELECT DISTINCT line FROM main.outbox ORDER BY line") == [("ann ***11",), ("bob ***22",)]
    assert rows(client=admin, sql="SELECT DISTINCT label FROM main.gen_sink") == [("genuine abc",)]
    # mia's trigger reads her table's generated column. With the macro named with its schema:
    for path, wes in sessions(server=legacy, catalog=L):
        expect_allowed(client=wes, sql=f"INSERT INTO {L}.app.mine2 VALUES (1)")
    assert rows(client=admin, sql="SELECT DISTINCT label FROM app.mlabels2") == [("genuine vault2-secret-1",)]
    # Named bare, it was main's by her search path; her table is in app. Not found for any
    # session (never the session's own fmt), with the two ways out.
    for path, wes in sessions(server=legacy, catalog=L):
        detail = not_found(client=wes, sql=f"INSERT INTO {L}.app.mine VALUES (1)", name="function fmt",
                           whose=f"a CHECK constraint, generated column, column default or index of table {L}.app.mvault",
                           remedy=f"an administrator can create a macro of that name in {L}.app that calls the one meant")
        assert f"a name without a schema is looked up in {L}.app and among DuckDB's own only" in detail, detail
    assert rows(client=admin, sql="SELECT count(*) FROM app.mlabels") == [(0,)]
    expect_allowed(client=admin, sql="CREATE MACRO app.fmt(x) AS main.fmt(x)")
    for path, wes in sessions(server=legacy, catalog=L):
        expect_allowed(client=wes, sql=f"INSERT INTO {L}.app.mine VALUES (1)")
    assert rows(client=admin, sql="SELECT DISTINCT label FROM app.mlabels ORDER BY label") == [
        ("genuine vault-secret-1",), ("genuine vault-secret-2",)]


def test_rows_a_trigger_copies_pass_through_the_tables_own_check(legacy):
    # Reviews: first f04; second g02 C, g05 C. DELETE without SELECT: the copied values went
    # through the writer's macro, which raised them in an error.
    admin = legacy.admin()
    expect_allowed(client=admin, sql="INSERT INTO fin.src_t SELECT i, 'src-secret-' || i FROM range(4, 9) t(i)")
    deleted = 0
    for path, wes in sessions(server=legacy, catalog=L):
        fails(client=wes, sql=f"SELECT secret FROM {L}.fin.src_t", why="no SELECT privilege")
        # (rowid: a write that reads no column takes no SELECT)
        expect_allowed(client=wes, sql=f"DELETE FROM {L}.fin.src_t WHERE rowid = {deleted}")
        deleted += 1
        # An UPDATE that carries a column with a CHECK that calls a macro takes SELECT on it.
        update = f"UPDATE {L}.main.trg_t SET balance = 2 WHERE rowid = 0"
        if at_home(path=path):
            assert "calls more than DuckDB's own functions" in fails(client=wes, sql=update, why="no SELECT privilege")
        else:
            fails(client=wes, sql=update, why="does not exist")  # its trigger writes a table of main
    assert rows(client=admin, sql="SELECT count(*) FROM fin.arch") == [(deleted,)]
    assert rows(client=admin, sql="SELECT count(*) FROM fin.arch WHERE secret LIKE 'src-secret-%'") == [(deleted,)]


def test_a_default_from_before_is_its_tables(legacy):
    # Second review g04: a default's macro in a third schema was the writer's to choose.
    admin = legacy.admin()
    for user in ["wes", "admin"]:
        for path, client in sessions(server=legacy, catalog=L, user=user):
            expect_allowed(client=client, sql=f"INSERT INTO {L}.main.dflt_t (id) VALUES (1)")
    assert rows(client=admin, sql="SELECT DISTINCT note FROM main.dflt_t") == [("genuine default",)]
    for path, wes in sessions(server=legacy, catalog=L):
        not_found(client=wes, sql=f"INSERT INTO {L}.app.msrc VALUES (1)", name="function stamp",
                  whose=f"a CHECK constraint, generated column, column default or index of table {L}.app.mlog",
                  remedy="a column default can be changed with ALTER TABLE")
    assert rows(client=admin, sql="SELECT count(*) FROM app.mlog") == [(0,)]
    expect_allowed(client=admin, sql="ALTER TABLE app.mlog ALTER COLUMN approved_by SET DEFAULT util.stamp()")
    for path, wes in sessions(server=legacy, catalog=L):
        expect_allowed(client=wes, sql=f"INSERT INTO {L}.app.msrc VALUES (1)")
    assert rows(client=admin, sql="SELECT DISTINCT approved_by FROM app.mlog") == [("genuine stamp",)]


def test_a_default_written_into_the_statement_is_its_tables(legacy):
    # Not in either review. DuckDB writes a copy of the default into the statement itself for
    # VALUES (..., DEFAULT), for a MERGE's INSERT, and for the columns an INSERT ... ON CONFLICT
    # with a column list leaves out, and binds it with the statement.
    admin = legacy.admin()
    written = 0
    for user in ["wes", "admin"]:
        for path, client in sessions(server=legacy, catalog=L, user=user):
            for sql in [f"INSERT INTO {L}.main.dflt_pk VALUES ({written + 1}, DEFAULT)",
                        f"INSERT INTO {L}.main.dflt_pk (id) VALUES ({written + 2}) ON CONFLICT DO NOTHING",
                        f"INSERT OR IGNORE INTO {L}.main.dflt_pk (id) VALUES ({written + 3})",
                        f"MERGE INTO {L}.main.dflt_pk t USING (SELECT {written + 4} AS id) s ON t.id = s.id "
                        "WHEN NOT MATCHED THEN INSERT VALUES (s.id, DEFAULT)",
                        f"INSERT INTO {L}.main.dflt_pk (id) VALUES ({written + 5})"]:
                expect_allowed(client=client, sql=sql)
            written += 5
    assert rows(client=admin, sql="SELECT note, count(*) FROM main.dflt_pk GROUP BY note") == [("genuine default", written)]


def test_an_update_carries_the_columns_the_tables_own_check_needs(legacy):
    # Not in either review. For an UPDATE DuckDB binds the table's CHECKs a second time, to
    # learn which columns it must carry for them. Bound through the session, a macro of the
    # writer's that uses no column carried none: the CHECK over (i, j) was then not checked for
    # an UPDATE of i (and, once the CHECK itself had its table's scope, DuckDB failed with a
    # fatal error that invalidated the database for everyone).
    admin = legacy.admin()
    for user in ["wes", "admin"]:
        for path, client in sessions(server=legacy, catalog=L, user=user):
            fails(client=client, sql=f"UPDATE {L}.main.pairs SET i = 100", why=CHECK_FAILED)
            expect_allowed(client=client, sql=f"UPDATE {L}.main.pairs SET i = 2")
            fails(client=client, sql=f"UPDATE {L}.main.pairs SET j = 0", why=CHECK_FAILED)
            assert rows(client=admin, sql="SELECT i, j FROM main.pairs") == [(2, 5)], path


def test_policies_from_before_compute_the_same_for_every_reader(legacy):
    # Second review g03 (the reader's macro existed before the policy named it) and g05 D, E
    # (policies written with `main.`). Released 2.13.0 returned every row, unmasked.
    for path, wes in sessions(server=legacy, catalog=L):
        for table in ["emp", "emp2"]:
            assert rows(client=wes, sql=f"SELECT name, region, ssn FROM {L}.main.{table} ORDER BY name") == [
                ("ann", "EMEA", "***-**-1111")], (path, table)


def test_a_view_from_before_finds_its_functions_in_its_own_schema(legacy):
    admin = legacy.admin()
    genuine = [("genuine other-secret-A",), ("genuine other-secret-B",)]
    for user in ["wes", "admin"]:
        for path, client in sessions(server=legacy, catalog=L, user=user):
            assert rows(client=client, sql=f"SELECT f FROM {L}.main.fmt_v ORDER BY f") == genuine, path
            # A view in rpt whose macro DuckDB found in main: no longer, for anyone.
            not_found(client=client, sql=f"SELECT f FROM {L}.rpt.fmt_v", name="function fmt",
                      whose=f"view {L}.rpt.fmt_v", remedy="Run CREATE OR REPLACE VIEW with the function named with its schema")
    expect_allowed(client=admin, sql="CREATE OR REPLACE VIEW rpt.fmt_v AS SELECT main.fmt(name) AS f FROM main.people")
    for path, wes in sessions(server=legacy, catalog=L):
        assert rows(client=wes, sql=f"SELECT f FROM {L}.rpt.fmt_v ORDER BY f") == [("genuine ann",), ("genuine bob",)], path


def test_an_index_over_a_macro_is_its_tables_when_bound_after_a_start(launcher, store, tmp_path):
    # Not in either review: an index's expression is kept as written and bound again when the
    # index is first needed after a start, in the session that needs it. A unique index over a
    # macro; the first writer after the start has a macro of that name ahead in its search path.
    # (Without a scope his macro was bound into the index, for every session until the next start.)
    database, uri = tmp_path / "ix.duckdb", store.new_database()
    first = start_rbac_server(launcher=launcher, store_uri=uri, args=["--database", str(database)])
    admin = first.admin()
    for sql in ["CREATE SCHEMA ws", "CREATE ROLE writers", "CREATE USER wes", "GRANT writers TO wes",
                "CREATE MACRO main.norm(x) AS lower(x)",
                "CREATE MACRO ws.norm(x) AS error('LEAK: ' || x), (a, b) AS error('LEAK: ' || b)",
                "CREATE TABLE main.ix_bare (id INT, name VARCHAR)", "CREATE UNIQUE INDEX ix_bare_norm ON main.ix_bare (norm(name))",
                "CREATE TABLE main.ix_full (id INT, name VARCHAR)", "CREATE UNIQUE INDEX ix_full_norm ON main.ix_full (main.norm(name))",
                "INSERT INTO main.ix_bare VALUES (0, 'Zed')", "INSERT INTO main.ix_full VALUES (0, 'Zed')",
                "GRANT INSERT ON main.ix_bare TO writers", "GRANT INSERT ON main.ix_full TO writers", "CHECKPOINT"]:
        expect_allowed(client=admin, sql=sql)
    first.running.stop()
    second = start_rbac_server(launcher=launcher, store_uri=uri, args=["--database", str(database)])
    for table, path in [("ix_full", "SET search_path = 'temp.main,ix.ws'"), ("ix_bare", "USE ix.ws")]:
        wes = second.user(name="wes")
        expect_allowed(client=wes, sql=path)
        expect_allowed(client=wes, sql=f"INSERT INTO ix.main.{table} VALUES (1, 'Ann')")
        fails(client=wes, sql=f"INSERT INTO ix.main.{table} VALUES (2, 'ANN')", why='Duplicate key "lower(name): ann"')
    assert rows(client=second.admin(), sql="SELECT (SELECT count(*) FROM main.ix_bare), (SELECT count(*) FROM main.ix_full)") == [(2, 2)]


def test_a_nested_schemas_macro_is_found_from_any_session(legacy):
    # A name with a nested schema path and no catalog (DuckDB 2.0) is in its table's catalog,
    # not wherever the session's search path finds a schema of the first name.
    admin = legacy.admin()
    for sql in ["CREATE SCHEMA app.deep", "CREATE MACRO app.deep.is_valid(x) AS x > 0",
                "CREATE TABLE app.deep.t (x INT CHECK (app.deep.is_valid(x)), y INT CHECK (is_valid(y)))",
                "GRANT INSERT ON app.deep.t TO writers"]:
        expect_allowed(client=admin, sql=sql)
    for path, wes in sessions(server=legacy, catalog=L):
        fails(client=wes, sql=f"INSERT INTO {L}.app.deep.t VALUES (-1, 1)", why=CHECK_FAILED)
        fails(client=wes, sql=f"INSERT INTO {L}.app.deep.t VALUES (1, -1)", why=CHECK_FAILED)
        expect_allowed(client=wes, sql=f"INSERT INTO {L}.app.deep.t VALUES (1, 1)")


# ---- new databases ----

@pytest.fixture(scope="module")
def server(module_launcher, store, tmp_path_factory):
    database = tmp_path_factory.mktemp("db") / "ns.duckdb"
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(), args=["--database", str(database)])
    admin = server.admin()
    for sql in ["CREATE SCHEMA app", "CREATE SCHEMA ws", "CREATE SCHEMA rpt", "CREATE SCHEMA util", "CREATE SCHEMA fin",
                "CREATE ROLE writers", "CREATE ROLE makers", "CREATE USER wes", "CREATE USER mia",
                "GRANT writers TO wes", "GRANT makers TO mia",
                "GRANT CREATE TABLE, CREATE VIEW, CREATE TRIGGER, CREATE MACRO, DROP MACRO, CREATE TYPE ON SCHEMA ws TO writers",
                "GRANT CREATE TABLE, CREATE TRIGGER, CREATE VIEW, USAGE, SELECT, INSERT, UPDATE, DELETE ON SCHEMA app TO makers",
                "CREATE MACRO main.is_valid(x) AS x > 0", "CREATE MACRO main.fmt(x) AS 'genuine ' || x",
                "CREATE MACRO main.mask_ssn(x) AS '***-**-' || right(x, 4)", "CREATE MACRO main.in_region(x) AS x = 'EMEA'",
                "CREATE MACRO util.stamp() AS 'genuine stamp'",
                "CREATE MACRO main.inner_ok(x) AS x > 0", "CREATE MACRO main.outer_ok(x) AS inner_ok(x)",
                "CREATE TYPE main.mood AS ENUM ('ok', 'sad')",
                "CREATE TABLE main.other_secret (note VARCHAR)", "INSERT INTO main.other_secret VALUES ('other-secret-A')",
                # The user's macros of the same names exist FIRST: nothing names them yet.
                ]:
        expect_allowed(client=admin, sql=sql)
    wes = server.user(name="wes")
    for sql in ["CREATE MACRO ws.is_valid(x) AS true, (a, b) AS true",
                "CREATE MACRO ws.fmt(x) AS error('LEAK: ' || x), (a, b) AS error('LEAK: ' || b)",
                "CREATE MACRO ws.mask_ssn(x) AS x, (a, b) AS b", "CREATE MACRO ws.in_region(x) AS true, (a, b) AS true",
                "CREATE MACRO ws.stamp() AS 'FORGED BY WES'", "CREATE MACRO ws.inner_ok(x) AS true",
                "CREATE TYPE ws.mood AS ENUM ('ok', 'LEAK')"]:
        expect_allowed(client=wes, sql=sql)
    server.admin_client = admin
    server.catalog = rows(client=admin, sql="SELECT current_database()")[0][0]
    return server


@pytest.fixture
def admin(server):
    return server.admin_client


def test_a_new_table_says_which_macro_it_means_and_no_session_changes_it(server, admin):
    # Second review g04 and g05 on a new database: the creator's search path finds the macros;
    # the table stores them with their schema; a writer's session decides nothing.
    c = server.catalog
    expect_allowed(client=admin, sql="SET search_path = 'main,util'")
    expect_allowed(client=admin, sql="CREATE TABLE app.t (x INT CHECK (is_valid(x)), label VARCHAR AS (fmt(x)), "
                                     "note VARCHAR DEFAULT stamp())")
    expect_allowed(client=admin, sql="SET search_path = 'main'")
    stored = rows(client=admin, sql="SELECT sql FROM duckdb_tables() WHERE table_name = 't' AND schema_name = 'app'")[0][0]
    assert "CHECK(main.is_valid(x))" in stored and "main.fmt(x)" in stored and "DEFAULT(util.stamp())" in stored, stored
    expect_allowed(client=admin, sql="GRANT SELECT, INSERT ON app.t TO writers")
    for user in ["wes", "admin"]:
        for path, client in sessions(server=server, catalog=c, user=user):
            fails(client=client, sql=f"INSERT INTO {c}.app.t (x) VALUES (-1)", why=CHECK_FAILED)
            expect_allowed(client=client, sql=f"INSERT INTO {c}.app.t (x) VALUES (1)")
            assert rows(client=client, sql=f"SELECT DISTINCT x, label, note FROM {c}.app.t") == [
                (1, "genuine 1", "genuine stamp")], path
    # The type could not be given its schema in the stored text: named bare, it would not be
    # found in app. So the definition is refused, saying what to write.
    detail = fails(client=admin, sql="CREATE TABLE app.t_type (m VARCHAR, mm VARCHAR AS (CAST(CAST(m AS mood) AS VARCHAR)))",
                   why="names type mood")
    assert f"which is {c}.main.mood in this session but would not be found" in detail and "main.mood" in detail, detail
    expect_allowed(client=admin, sql="CREATE TABLE app.t_type (m VARCHAR, mm VARCHAR AS (CAST(CAST(m AS main.mood) AS VARCHAR)))")
    expect_allowed(client=admin, sql="INSERT INTO app.t_type VALUES ('ok')")
    expect_allowed(client=admin, sql="GRANT SELECT ON app.t_type TO writers")
    for path, wes in sessions(server=server, catalog=c):
        assert rows(client=wes, sql=f"SELECT mm FROM {c}.app.t_type") == [("ok",)], path


def test_a_definition_whose_names_its_scope_would_not_find_is_refused_at_create(server, admin):
    c = server.catalog
    method = "a method-style call (value.function())"
    # A method call of a macro: in a CHECK, a generated column, a trigger's action, a row policy.
    for sql in ["CREATE TABLE main.m1 (x INT CHECK (x.is_valid()))", "CREATE TABLE main.m2 (x INT, label VARCHAR AS (x.fmt()))"]:
        detail = fails(client=admin, sql=sql, why=method)
        assert "write it as main." in detail and "(value)" in detail, detail
    expect_allowed(client=admin, sql="CREATE TABLE main.src (id INT, region VARCHAR)")
    expect_allowed(client=admin, sql="CREATE TABLE main.sink (note VARCHAR)")
    fails(client=admin, sql="CREATE TRIGGER src_m AFTER INSERT ON main.src REFERENCING NEW TABLE AS n FOR EACH STATEMENT "
                            "INSERT INTO main.sink SELECT region.fmt() FROM n", why=method)
    fails(client=admin, sql="CREATE ROW POLICY p_m ON main.src USING (region.in_region())", why=method)
    # Twins: DuckDB's own functions are called that way, and a macro as a call.
    expect_allowed(client=admin, sql="CREATE TABLE main.m3 (s VARCHAR CHECK (s.length() > 0 AND main.is_valid(s.length())), "
                                     "u VARCHAR AS (s.upper()))")
    fails(client=admin, sql="INSERT INTO main.m3 VALUES ('')", why=CHECK_FAILED)
    expect_allowed(client=admin, sql="INSERT INTO main.m3 VALUES ('abc')")
    assert rows(client=admin, sql="SELECT u FROM main.m3") == [("ABC",)]
    # A view outside main that calls a macro of main by a bare name (the session finds it; the
    # view's own schema would not).
    expect_allowed(client=admin, sql="CREATE TABLE app.src (id INT, region VARCHAR)")
    expect_allowed(client=admin, sql="CREATE TABLE app.sink (note VARCHAR)")
    detail = fails(client=admin, sql="CREATE VIEW rpt.v AS SELECT fmt(note) AS f FROM main.other_secret",
                   why=f"which is {c}.main.fmt in this session but would not be found where")
    assert f"view {c}.rpt.v calls function fmt" in detail and "name it with its schema (schema.name): main.fmt" in detail, detail
    expect_allowed(client=admin, sql="CREATE VIEW rpt.v AS SELECT main.fmt(note) AS f FROM main.other_secret")
    # A trigger's action is stored with the macros it calls given their schema, like its tables.
    expect_allowed(client=admin, sql="CREATE TRIGGER src_t AFTER INSERT ON app.src REFERENCING NEW TABLE AS n FOR EACH STATEMENT "
                                     "INSERT INTO app.sink SELECT fmt(region) FROM n")
    assert "main.fmt(region)" in rows(client=admin, sql="SELECT sql FROM duckdb_triggers() WHERE trigger_name = 'src_t'")[0][0]
    # A row trigger's body has its table's scope too.
    expect_allowed(client=admin, sql="CREATE TABLE app.rsrc (id INT, region VARCHAR)")
    expect_allowed(client=admin, sql="CREATE TRIGGER rsrc_r AFTER INSERT ON app.rsrc FOR EACH ROW "
                                     "INSERT INTO app.sink VALUES (fmt(NEW.region) || ' row')")
    expect_allowed(client=admin, sql="GRANT INSERT ON app.rsrc TO writers")
    # What they compute for a session that has a macro of that name ahead.
    expect_allowed(client=admin, sql="GRANT SELECT ON rpt.v TO writers")
    expect_allowed(client=admin, sql="GRANT INSERT ON app.src TO writers")
    expect_allowed(client=admin, sql="GRANT INSERT ON app.sink TO writers")
    for path, wes in sessions(server=server, catalog=c):
        assert rows(client=wes, sql=f"SELECT f FROM {c}.rpt.v") == [("genuine other-secret-A",)], path
        expect_allowed(client=wes, sql=f"INSERT INTO {c}.app.src VALUES (1, 'EMEA')")
        expect_allowed(client=wes, sql=f"INSERT INTO {c}.app.rsrc VALUES (1, 'EMEA')")
    assert rows(client=admin, sql="SELECT DISTINCT note FROM app.sink ORDER BY note") == [("genuine EMEA",), ("genuine EMEA row",)]
    # An index over a macro of another schema, named bare: it would not be found when the index
    # is bound again after the next start.
    detail = fails(client=admin, sql="CREATE INDEX src_fmt ON app.src (fmt(region))",
                   why=f"an index on table {c}.app.src calls function fmt, which is {c}.main.fmt in this session but would not be found")
    assert "name it with its schema (schema.name): main.fmt" in detail, detail
    expect_allowed(client=admin, sql="CREATE INDEX src_fmt ON app.src (main.fmt(region))")
    # A macro that calls a macro: its body is bound where it is used. Named bare in there, the
    # inner one is main's only for an object of main.
    expect_allowed(client=admin, sql="CREATE TABLE main.n1 (x INT CHECK (outer_ok(x)))")
    detail = fails(client=admin, sql="CREATE TABLE app.n2 (x INT CHECK (main.outer_ok(x)))",
                   why="calls macro outer_ok, which calls function inner_ok")
    assert "name it with its schema (schema.name) in macro outer_ok: main.inner_ok" in detail, detail
    # (DuckDB lets no one replace a macro a table's CHECK depends on: a second one, written in full.)
    expect_allowed(client=admin, sql="CREATE MACRO main.outer_ok2(x) AS main.inner_ok(x)")
    expect_allowed(client=admin, sql="CREATE TABLE app.n2 (x INT CHECK (main.outer_ok2(x)))")
    expect_allowed(client=admin, sql="GRANT INSERT ON main.n1 TO writers")
    expect_allowed(client=admin, sql="GRANT INSERT ON app.n2 TO writers")
    for path, wes in sessions(server=server, catalog=c):
        for table in ["main.n1", "app.n2"]:
            fails(client=wes, sql=f"INSERT INTO {c}.{table} VALUES (-1)", why=CHECK_FAILED)
            expect_allowed(client=wes, sql=f"INSERT INTO {c}.{table} VALUES (1)")


def test_new_policies_compute_the_same_for_every_reader_and_through_a_view(server, admin):
    # Second review g03 shapes 1 and 2 (the reader's macros are older than the policies) and
    # g05 D, E, on a new database; and a view over the masked table.
    c = server.catalog
    for sql in ["CREATE TABLE main.emp (name VARCHAR, region VARCHAR, ssn VARCHAR)",
                "INSERT INTO main.emp VALUES ('ann', 'EMEA', '111-11-1111'), ('bob', 'APAC', '222-22-2222')",
                "CREATE TABLE main.emp2 AS SELECT * FROM main.emp",
                "CREATE MASKING POLICY ssn_mask AS (val VARCHAR) RETURNS VARCHAR -> mask_ssn(val)",
                "ALTER TABLE main.emp MODIFY COLUMN ssn SET MASKING POLICY ssn_mask",
                "CREATE ROW POLICY emea_only ON main.emp TO writers USING (in_region(region))",
                "CREATE MASKING POLICY ssn_mask2 AS (val VARCHAR) RETURNS VARCHAR -> main.mask_ssn(val)",
                "ALTER TABLE main.emp2 MODIFY COLUMN ssn SET MASKING POLICY ssn_mask2",
                "CREATE ROW POLICY emea_only ON main.emp2 TO writers USING (main.in_region(region) AND region.lower() = 'emea')",
                "CREATE VIEW main.emp_v AS SELECT name, ssn FROM main.emp",
                "GRANT SELECT ON main.emp TO writers", "GRANT SELECT ON main.emp2 TO writers",
                "GRANT SELECT ON main.emp_v TO writers"]:
        expect_allowed(client=admin, sql=sql)
    through_the_view = None
    for path, wes in sessions(server=server, catalog=c):
        for table in ["emp", "emp2"]:
            assert rows(client=wes, sql=f"SELECT name, region, ssn FROM {c}.main.{table} ORDER BY name") == [
                ("ann", "EMEA", "***-**-1111")], (path, table)
        seen = rows(client=wes, sql=f"SELECT name, ssn FROM {c}.main.emp_v ORDER BY name")
        through_the_view = through_the_view or seen
        assert seen == through_the_view, path
    # A policy on a table outside main finds a bare name in main too: a policy has no schema.
    for sql in ["CREATE TABLE app.emp AS SELECT * FROM main.emp",
                "CREATE ROW POLICY emea_only ON app.emp TO writers USING (in_region(region))",
                "ALTER TABLE app.emp MODIFY COLUMN ssn SET MASKING POLICY ssn_mask", "GRANT SELECT ON app.emp TO writers"]:
        expect_allowed(client=admin, sql=sql)
    for path, wes in sessions(server=server, catalog=c):
        assert rows(client=wes, sql=f"SELECT name, ssn FROM {c}.app.emp ORDER BY name") == [("ann", "***-**-1111")], path
    # One that names a macro of another schema bare is refused, saying what to write.
    expect_allowed(client=admin, sql="CREATE MACRO util.in_apac(x) AS x = 'APAC'")
    expect_allowed(client=admin, sql="SET search_path = 'main,util'")
    with pytest.raises(ChargewireError) as raised:
        admin.run(sql="CREATE ROW POLICY apac ON main.emp2 TO makers USING (in_apac(region))")
    expect_allowed(client=admin, sql="SET search_path = 'main'")
    assert f"calls function in_apac, which is {c}.util.in_apac in this session but would not be found" in raised.value.detail
    assert "name it with its schema (schema.name): util.in_apac" in raised.value.detail, raised.value.detail
    expect_allowed(client=admin, sql="CREATE ROW POLICY apac ON main.emp2 TO makers USING (util.in_apac(region))")
    assert rows(client=server.user(name="mia"), sql="SELECT 1") == [(1,)]


def test_a_users_own_table_with_own_macros_still_works(server, admin):
    # The other side of every refusal above: a user's own macros in the user's own schema, in a
    # CHECK, a generated column, a default, a trigger and (the administrator's) a policy, named
    # bare from that schema and with the schema, for the owner, another user and the administrator.
    c = server.catalog
    wes = server.user(name="wes")
    for sql in ["USE ws", "CREATE MACRO w_ok(x) AS x > 0", "CREATE MACRO w_fmt(x) AS 'w ' || x",
                "CREATE MACRO w_dflt() AS 'w default'", "CREATE MACRO w_twice(x) AS w_fmt(w_fmt(x))",
                "CREATE TABLE own (x INT CHECK (w_ok(x)), label VARCHAR AS (w_fmt(x)), note VARCHAR DEFAULT w_dflt())",
                "CREATE TABLE own_log (v VARCHAR)",
                "CREATE TRIGGER own_ins AFTER INSERT ON own REFERENCING NEW TABLE AS n FOR EACH STATEMENT "
                "INSERT INTO own_log SELECT w_twice(x) FROM n",
                "CREATE VIEW own_v AS SELECT x, w_fmt(x) AS f FROM own",
                "CREATE TABLE own_rows AS SELECT * FROM (VALUES (-5), (5)) t(x)", "USE main"]:
        expect_allowed(client=wes, sql=sql)
    for sql in ["CREATE ROW POLICY positive ON ws.own_rows USING (ws.w_ok(x))",
                "GRANT SELECT, INSERT ON ws.own TO makers", "GRANT SELECT ON ws.own_v TO makers",
                "GRANT INSERT ON ws.own_log TO makers", "GRANT SELECT ON ws.own_rows TO makers"]:
        expect_allowed(client=admin, sql=sql)
    written = 0
    for user in ["wes", "mia"]:
        for path, client in sessions(server=server, catalog=c, user=user):
            fails(client=client, sql=f"INSERT INTO {c}.ws.own (x) VALUES (-1)", why=CHECK_FAILED)
            expect_allowed(client=client, sql=f"INSERT INTO {c}.ws.own (x) VALUES (3)")
            written += 1
            assert rows(client=client, sql=f"SELECT DISTINCT x, label, note FROM {c}.ws.own") == [(3, "w 3", "w default")], path
            assert rows(client=client, sql=f"SELECT DISTINCT f FROM {c}.ws.own_v") == [("w 3",)], path
            assert rows(client=client, sql=f"SELECT x FROM {c}.ws.own_rows") == [(5,)], path
    assert rows(client=admin, sql="SELECT v, count(*) FROM ws.own_log GROUP BY v") == [("w w 3", written)]
