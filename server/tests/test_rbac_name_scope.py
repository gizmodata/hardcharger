"""Where a stored expression finds its functions, under access control (authz/name_scope.hpp).

A view's query, a column default, a CHECK constraint, a generated column, a trigger's body and
a row or masking policy are kept as written and bound again in the session of whoever reads or
writes. DuckDB finds a function through that session's search path (USE, SET search_path, the
current catalog) and reads `x.f()` as `f(x)`, so a user who could create a macro in any schema
could have it called in place of the one the expression meant: a masking policy that returns
the value, a row policy that is true, a CHECK that passes, a macro of his running in another
user's trigger with her rights. Released 2.13.0 behaves so.

Now each stored expression has a name scope, one schema fixed by its object (its table's, its
view's; a policy's is main), and finds a function, macro or type there, then in main of its own
database, then among DuckDB's own, whatever the session, an administrator's included; `schema.f`
in the scope's catalog; never as a method call of a macro. What is not found that way is refused, saying what to write.

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
    # Named bare: her table is in app, which has no fmt, so it is main's of the table's own
    # database, for every session (never the session's own fmt).
    for path, wes in sessions(server=legacy, catalog=L):
        expect_allowed(client=wes, sql=f"INSERT INTO {L}.app.mine VALUES (1)")
    assert rows(client=admin, sql="SELECT DISTINCT label FROM app.mlabels ORDER BY label") == [
        ("genuine vault-secret-1",), ("genuine vault-secret-2",)]


def test_a_bare_name_is_the_objects_own_schemas_before_mains(legacy):
    # The order is fixed: the object's own schema, then main of its database, then DuckDB's own.
    # app.mvault's generated column calls fmt bare; app gets a fmt of its own (an
    # administrator's: the name pin lets no one else create it, test_rbac_macros.py).
    admin = legacy.admin()
    expect_allowed(client=admin, sql="CREATE MACRO app.fmt(x) AS 'app ' || x")
    for path, wes in sessions(server=legacy, catalog=L):
        expect_allowed(client=wes, sql=f"INSERT INTO {L}.app.mine VALUES (1)")
    assert rows(client=admin, sql="SELECT DISTINCT label FROM app.mlabels ORDER BY label") == [
        ("app vault-secret-1",), ("app vault-secret-2",)]
    # A view of main that reads main's own column of the same shape still gets main's.
    for path, wes in sessions(server=legacy, catalog=L):
        assert rows(client=wes, sql=f"SELECT f FROM {L}.main.fmt_v ORDER BY f") == [
            ("genuine other-secret-A",), ("genuine other-secret-B",)], path


def test_main_is_the_objects_own_databases_not_the_sessions(legacy):
    # A second database with macros of the same names in ITS main, and a session whose current
    # catalog it is: a bare name in an object of the first database is still that database's main's.
    admin = legacy.admin()
    for sql in ["ATTACH ':memory:' AS other", "CREATE MACRO other.main.fmt(x) AS 'other ' || x",
                "CREATE MACRO other.main.mask(x) AS x", "CREATE MACRO other.main.is_valid(x) AS true",
                "CREATE MACRO other.main.dflt() AS 'other default'", "USE other"]:
        expect_allowed(client=admin, sql=sql)
    assert rows(client=admin, sql="SELECT current_database()") == [("other",)]
    assert rows(client=admin, sql=f"SELECT f FROM {L}.rpt.fmt_v ORDER BY f") == [("genuine ann",), ("genuine bob",)]
    assert rows(client=admin, sql=f"SELECT * FROM {L}.rpt.people ORDER BY name") == [("ann", "***11"), ("bob", "***22")]
    fails(client=admin, sql=f"INSERT INTO {L}.main.checked VALUES (-1)", why=CHECK_FAILED)
    expect_allowed(client=admin, sql=f"INSERT INTO {L}.main.dflt_pk VALUES (1, DEFAULT)")
    # A table of schema app whose generated column calls fmt bare: app has none, so the main of
    # ITS database, not of the session's.
    assert rows(client=admin, sql=f"SELECT label FROM {L}.app.mvault ORDER BY label") == [
        ("genuine vault-secret-1",), ("genuine vault-secret-2",)]
    assert rows(client=legacy.admin(), sql="SELECT note FROM main.dflt_pk") == [("genuine default",)]


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
        # (its macro is in a third schema, util: neither the table's nor main)
    detail = fails(client=legacy.user(name="wes"), sql=f"INSERT INTO {L}.app.msrc VALUES (1)", why=NOT_FOUND)
    assert (f"a name without a schema is looked up in {L}.app, then in schema main of catalog {L} and among "
            "DuckDB's own only") in detail, detail
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


def test_a_view_from_before_finds_its_functions_in_its_own_schema_then_main(legacy):
    for user in ["wes", "admin"]:
        for path, client in sessions(server=legacy, catalog=L, user=user):
            assert rows(client=client, sql=f"SELECT f FROM {L}.main.fmt_v ORDER BY f") == [
                ("genuine other-secret-A",), ("genuine other-secret-B",)], path
            # A view in rpt that calls main's macro bare (rpt has none of that name).
            assert rows(client=client, sql=f"SELECT f FROM {L}.rpt.fmt_v ORDER BY f") == [("genuine ann",), ("genuine bob",)], path


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


def test_a_new_index_stores_its_macro_and_a_later_macro_of_that_name_changes_nothing(launcher, store, tmp_path):
    # Review of round 3, finding 04. An index over main's macro, written bare, on a table of
    # app; a user who may create macros in app then makes one of that name there. A bare name
    # is looked up in the table's own schema before main, so after the next start the unique
    # index was bound over HIS macro: inserts collided, and a DELETE of a row indexed before
    # ended in a DuckDB fatal error that invalidated the database for every session.
    # CREATE INDEX stores the macro with its schema now, so it can only ever mean that one.
    database, uri = tmp_path / "nx.duckdb", store.new_database()
    first = start_rbac_server(launcher=launcher, store_uri=uri, args=["--database", str(database)])
    admin = first.admin()
    for sql in ["CREATE SCHEMA app", "CREATE SCHEMA ws", "CREATE ROLE makers", "CREATE ROLE writers",
                "CREATE USER mia", "CREATE USER wes", "GRANT makers TO mia", "GRANT writers TO wes",
                "GRANT CREATE MACRO ON SCHEMA app TO makers", "GRANT CREATE TABLE, CREATE MACRO ON SCHEMA ws TO writers",
                "CREATE MACRO main.norm(x) AS lower(x)", "CREATE TABLE app.people (id INT, name VARCHAR)",
                "CREATE UNIQUE INDEX people_name ON app.people (norm(name))", "INSERT INTO app.people VALUES (1, 'Ann')",
                "GRANT SELECT, INSERT, DELETE ON app.people TO makers"]:
        expect_allowed(client=admin, sql=sql)
    stored = rows(client=admin, sql="SELECT sql FROM duckdb_indexes() WHERE index_name = 'people_name'")[0][0]
    assert "main.norm(" in stored, stored
    mia = first.user(name="mia")
    fails(client=mia, sql="INSERT INTO app.people VALUES (2, 'ANN')", why='Duplicate key "lower(name): ann"')
    # The index says main.norm: a macro of that name in app is no one's to worry about.
    expect_allowed(client=mia, sql="CREATE MACRO app.norm(x) AS 'everyone'")
    # Twin: a user's own index over his own macro, named bare from its own schema.
    wes = first.user(name="wes")
    for sql in ["USE ws", "CREATE MACRO w_norm(x) AS upper(x)", "CREATE TABLE mine (id INT, name VARCHAR)",
                "CREATE UNIQUE INDEX mine_name ON mine (w_norm(name))", "INSERT INTO mine VALUES (1, 'a')"]:
        expect_allowed(client=wes, sql=sql)
    assert "ws.w_norm(" in rows(client=admin, sql="SELECT sql FROM duckdb_indexes() WHERE index_name = 'mine_name'")[0][0]
    fails(client=wes, sql="INSERT INTO mine VALUES (2, 'A')", why="Duplicate key")
    expect_allowed(client=admin, sql="CHECKPOINT")
    first.running.stop()
    for start in range(2):  # the index is bound again after every start
        again = start_rbac_server(launcher=launcher, store_uri=uri, args=["--database", str(database)])
        admin, mia, wes = again.admin(), again.user(name="mia"), again.user(name="wes")
        expect_allowed(client=admin, sql=f"INSERT INTO app.people VALUES ({10 + start}, 'Cid{start}')")
        expect_allowed(client=admin, sql=f"INSERT INTO app.people VALUES ({20 + start}, 'Dee{start}')")
        fails(client=mia, sql="INSERT INTO app.people VALUES (6, 'ANN')" if start == 0 else "INSERT INTO app.people VALUES (6, 'CID0')",
              why='Duplicate key "lower(name): ')
        # A row indexed before the start goes, and the table is still there for everyone.
        expect_allowed(client=admin, sql="DELETE FROM app.people WHERE id = 1" if start == 0 else "DELETE FROM app.people WHERE id = 20")
        assert rows(client=mia, sql="SELECT count(*) FROM app.people") == [(2 + start,)]
        fails(client=wes, sql="INSERT INTO ws.mine VALUES (3, 'A')", why="Duplicate key")
        expect_allowed(client=wes, sql=f"INSERT INTO ws.mine VALUES ({30 + start}, 'b{start}')")
        expect_allowed(client=wes, sql=f"DELETE FROM ws.mine WHERE id = {30 + start}")
        expect_allowed(client=admin, sql="CHECKPOINT")
        again.running.stop()


def test_an_index_from_before_is_bound_to_one_macro_beyond_doubt_or_not_used(legacy, launcher, tmp_path):
    # Indexes a 2.13.0 server made keep their macro as written, bare. Bound under the table's
    # name scope (its schema, then main; never the session), and only where that is one macro
    # beyond doubt: with two of the name in the database nobody knows which one computed the
    # entries on disk, and an index kept with the other is wrong (DuckDB ends a DELETE on it with
    # a fatal error that invalidates the database). Such a table is refused, saying how to mend it.
    admin = legacy.admin()
    two = "of that name (in "
    mend = "Drop the index and create it again"

    def refused(server, table: str, why: str):
        for client in [server.user(name="wes"), server.admin()]:
            for sql in [f"INSERT INTO {L}.{table} VALUES (50, 'Zed')", f"DELETE FROM {L}.{table} WHERE id = 1",
                        f"SELECT count(*) FROM {L}.{table}"]:
                error = expect_blocked(client=client, sql=sql, why=why)
                assert mend in error.detail and "FATAL" not in error.detail, error.detail
        assert rows(client=server.admin(), sql="SELECT 1") == [(1,)]  # and the database is still there

    def sound(server, table: str, key: int):
        for path, wes in sessions(server=server, catalog=L):
            fails(client=wes, sql=f"INSERT INTO {L}.{table} VALUES (99, 'ANN')", why='Duplicate key "lower(name): ann"')
        wes = server.user(name="wes")
        expect_allowed(client=wes, sql=f"INSERT INTO {L}.{table} VALUES ({key}, 'Bob{key}')")
        expect_allowed(client=wes, sql=f"DELETE FROM {L}.{table} WHERE id = {key}")
        assert rows(client=server.admin(), sql=f"SELECT name FROM {L}.{table} WHERE id = 1") == [("Ann",)]

    # One macro of that name (main's): bound to it, from any session.
    sound(server=legacy, table="app.people", key=2)
    # A user who may create macros in the table's schema may not create one of that name ...
    expect_allowed(client=admin, sql="GRANT CREATE MACRO ON SCHEMA app TO writers")
    error = expect_blocked(client=legacy.user(name="wes"), sql="CREATE MACRO app.norm(x) AS 'everyone'",
                           why="would change what a view, a trigger, a row or masking policy")
    assert "generated column or index" in error.detail, error.detail
    # ... and where an administrator does, or one was there from before (rpt.norm_r), the table
    # is not used until its index says which.
    expect_allowed(client=admin, sql="CREATE MACRO app.norm(x) AS 'everyone'")
    refused(server=legacy, table="app.people", why=f"index people_name on table {L}.app.people calls macro norm without its schema")
    assert two in expect_blocked(client=admin, sql="DELETE FROM app.people", why="has 2 of that name").detail
    refused(server=legacy, table="rpt.items", why=f"index items_name on table {L}.rpt.items calls macro norm_r without its schema")
    # One that only its creator's search path found, in a third schema.
    refused(server=legacy, table="app.tagged", why="it is in neither the table's own schema nor main of its database")
    # CHECKPOINT (the statement) binds every index first, and DuckDB invalidates the database
    # when one cannot be bound: refused too, with the same words, and the database is still there.
    error = expect_blocked(client=admin, sql="CHECKPOINT", why="CHECKPOINT binds every index, and index ")
    assert mend in error.detail, error.detail
    assert rows(client=admin, sql="SELECT count(*) FROM main.emp") == [(3,)]
    # The same after a start: refused, not bound.
    legacy.running.stop()
    database = tmp_path / f"{L}.duckdb"
    second = start_rbac_server(launcher=launcher, store_uri=legacy.store_uri, args=["--database", str(database)])
    refused(server=second, table="app.people", why="has 2 of that name")
    refused(server=second, table="rpt.items", why="has 2 of that name")
    refused(server=second, table="app.tagged", why="neither the table's own schema nor main")
    # Mended as the refusal says. CREATE INDEX stores the macro the creating session means with
    # its schema (main's here, though the table's own schema has one of that name too).
    admin = second.admin()
    for schema, index, table, written, macro in [("app", "people_name", "app.people", "norm", "main.norm"),
                                                 ("rpt", "items_name", "rpt.items", "norm_r", "main.norm_r"),
                                                 ("app", "tagged_name", "app.tagged", "util.norm_u", "util.norm_u")]:
        expect_allowed(client=admin, sql=f"DROP INDEX {schema}.{index}")
        expect_allowed(client=admin, sql=f"CREATE UNIQUE INDEX {index} ON {table} ({written}(name))")
        stored = rows(client=admin, sql=f"SELECT sql FROM duckdb_indexes() WHERE index_name = '{index}'")[0][0]
        assert f"{macro}(" in stored, stored
    expect_allowed(client=admin, sql="CHECKPOINT")
    for key, table in enumerate(["app.people", "rpt.items", "app.tagged"]):
        sound(server=second, table=table, key=60 + key)
    expect_allowed(client=admin, sql="CHECKPOINT")
    second.running.stop()
    third = start_rbac_server(launcher=launcher, store_uri=legacy.store_uri, args=["--database", str(database)])
    for key, table in enumerate(["app.people", "rpt.items", "app.tagged"]):
        sound(server=third, table=table, key=70 + key)


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
    # A type of main named bare from a table of app is main's; one the creator's search path
    # found in a third schema would not be found, and the definition is refused, saying what to write.
    expect_allowed(client=admin, sql="CREATE TABLE app.t_type (m VARCHAR, mm VARCHAR AS (CAST(CAST(m AS mood) AS VARCHAR)))")
    expect_allowed(client=admin, sql="INSERT INTO app.t_type VALUES ('ok')")
    expect_allowed(client=admin, sql="GRANT SELECT ON app.t_type TO writers")
    for path, wes in sessions(server=server, catalog=c):
        assert rows(client=wes, sql=f"SELECT mm FROM {c}.app.t_type") == [("ok",)], path
    expect_allowed(client=admin, sql="CREATE TYPE util.grade AS ENUM ('a', 'b')")
    expect_allowed(client=admin, sql="SET search_path = 'main,util'")
    detail = fails(client=admin, sql="CREATE TABLE app.t_grade (g VARCHAR, gg VARCHAR AS (CAST(CAST(g AS grade) AS VARCHAR)))",
                   why="names type grade")
    expect_allowed(client=admin, sql="SET search_path = 'main'")
    assert f"which is {c}.util.grade in this session but would not be found" in detail and "util.grade" in detail, detail
    expect_allowed(client=admin, sql="CREATE TABLE app.t_grade (g VARCHAR, gg VARCHAR AS (CAST(CAST(g AS util.grade) AS VARCHAR)))")


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
    # A view outside main that calls a macro of main by a bare name: its schema has none, so
    # main's of its own database (the pattern 2.13 documents). One that calls a macro of a
    # third schema bare, which only the creator's search path finds, is refused.
    expect_allowed(client=admin, sql="CREATE TABLE app.src (id INT, region VARCHAR)")
    expect_allowed(client=admin, sql="CREATE TABLE app.sink (note VARCHAR)")
    expect_allowed(client=admin, sql="CREATE VIEW rpt.v AS SELECT fmt(note) AS f FROM main.other_secret")
    expect_allowed(client=admin, sql="CREATE MACRO util.tag(x) AS 'u ' || x")
    expect_allowed(client=admin, sql="SET search_path = 'main,util'")
    detail = fails(client=admin, sql="CREATE VIEW rpt.v_tag AS SELECT tag(note) AS f FROM main.other_secret",
                   why=f"which is {c}.util.tag in this session but would not be found where")
    assert f"view {c}.rpt.v_tag calls function tag" in detail and "name it with its schema (schema.name): util.tag" in detail, detail
    assert f"there a name is looked up in {c}.rpt, then in schema main of catalog {c} and among DuckDB's own" in detail, detail
    # An index over such a macro is stored with the macro's schema, like a CHECK; a macro called
    # as a method is refused, as everywhere.
    expect_allowed(client=admin, sql="CREATE INDEX src_tag ON app.src (tag(region))")
    fails(client=admin, sql="CREATE INDEX src_tag_m ON app.src (region.tag())", why=method)
    expect_allowed(client=admin, sql="SET search_path = 'main'")
    expect_allowed(client=admin, sql="CREATE VIEW rpt.v_tag AS SELECT util.tag(note) AS f FROM main.other_secret")
    expect_allowed(client=admin, sql="CREATE INDEX src_fmt ON app.src (fmt(region))")
    assert rows(client=admin, sql="SELECT index_name, sql LIKE '%util.tag(%' OR sql LIKE '%main.fmt(%' FROM duckdb_indexes() "
                                  "WHERE index_name IN ('src_tag', 'src_fmt') ORDER BY 1") == [("src_fmt", True), ("src_tag", True)]
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
    # A macro that calls a macro: its body is bound where it is used. main's inner one, named
    # bare, is found from an object of any schema of the database; a third schema's is not.
    expect_allowed(client=admin, sql="CREATE TABLE main.n1 (x INT CHECK (outer_ok(x)))")
    expect_allowed(client=admin, sql="CREATE TABLE app.n2 (x INT CHECK (main.outer_ok(x)))")
    expect_allowed(client=admin, sql="CREATE MACRO util.inner_u(x) AS x > 0")
    expect_allowed(client=admin, sql="SET search_path = 'main,util'")
    expect_allowed(client=admin, sql="CREATE MACRO util.outer_u(x) AS inner_u(x)")
    detail = fails(client=admin, sql="CREATE TABLE app.n3 (x INT CHECK (util.outer_u(x)))",
                   why="calls macro outer_u, which calls function inner_u")
    expect_allowed(client=admin, sql="SET search_path = 'main'")
    assert "name it with its schema (schema.name) in macro outer_u: util.inner_u" in detail, detail
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


def test_a_macro_named_like_one_an_object_finds_in_main_is_the_administrators_to_create(server, admin):
    # What "own schema, then main" leaves to guard: a macro made later in the object's own
    # schema would be found before main's, and one who may replace main's would change it for
    # everyone. The creation-time name pin (macros.cc) refuses both to all but an administrator.
    c = server.catalog
    pinned = "would change what a view, a trigger, a row or masking policy"
    for sql in ["CREATE MACRO main.pin_fmt(x) AS 'main ' || x", "CREATE MACRO main.pin_ok(x) AS x > 0",
                "CREATE MACRO main.pin_region(x) AS x = 'EU'",
                "CREATE TABLE main.pin_t (x INT CHECK (pin_ok(x)), region VARCHAR)", "INSERT INTO main.pin_t VALUES (1, 'EU'), (2, 'US')",
                "CREATE VIEW rpt.pin_v AS SELECT pin_fmt(region) AS f FROM main.pin_t",
                "CREATE ROW POLICY pin_eu ON main.pin_t TO writers USING (pin_region(region))",
                "GRANT SELECT ON main.pin_t TO writers", "GRANT SELECT ON rpt.pin_v TO writers",
                "GRANT CREATE MACRO, DROP MACRO ON SCHEMA rpt TO writers", "GRANT CREATE MACRO, DROP MACRO ON SCHEMA main TO writers"]:
        expect_allowed(client=admin, sql=sql)
    wes = server.user(name="wes")
    try:
        # In the view's own schema, where it would be found before main's.
        expect_blocked(client=wes, sql="CREATE MACRO rpt.pin_fmt(x) AS 'mine'", why=pinned)
        # In main: the ones the table's CHECK, the view and the policy call there.
        for name, body in [("pin_ok", "true"), ("pin_fmt", "'mine'"), ("pin_region", "true")]:
            expect_blocked(client=wes, sql=f"CREATE OR REPLACE MACRO main.{name}(x) AS {body}", why=pinned)
        # Twin: a name nothing calls, in both schemas.
        for schema in ["rpt", "main"]:
            expect_allowed(client=wes, sql=f"CREATE MACRO {schema}.pin_spare(x) AS x")
            expect_allowed(client=wes, sql=f"DROP MACRO {schema}.pin_spare")
        # And everything computes what it did.
        assert rows(client=wes, sql=f"SELECT f FROM {c}.rpt.pin_v") == [("main EU",)]
        assert rows(client=wes, sql=f"SELECT x FROM {c}.main.pin_t") == [(1,)]
        fails(client=admin, sql="INSERT INTO main.pin_t VALUES (-1, 'EU')", why=CHECK_FAILED)
    finally:
        expect_allowed(client=admin, sql="REVOKE CREATE MACRO, DROP MACRO ON SCHEMA rpt FROM writers")
        expect_allowed(client=admin, sql="REVOKE CREATE MACRO, DROP MACRO ON SCHEMA main FROM writers")


def test_a_new_view_whose_own_schema_has_the_name_means_that_one_and_says_so(server, admin):
    # The macro is in the view's schema before the view is made. A creator whose session finds
    # main's by that name is told the view would mean the other one; one whose session finds the
    # view's own gets a view that means it for every reader.
    c = server.catalog
    expect_allowed(client=admin, sql="CREATE MACRO rpt.fmt(x) AS 'rpt ' || x")
    try:
        detail = fails(client=admin, sql="CREATE VIEW rpt.own_v AS SELECT fmt(note) AS f FROM main.other_secret",
                       why=f"which is {c}.main.fmt in this session but would be {c}.rpt.fmt where")
        assert "name it with its schema (schema.name): main.fmt" in detail, detail
        expect_allowed(client=admin, sql="USE rpt")
        expect_allowed(client=admin, sql=f"CREATE VIEW rpt.own_v AS SELECT fmt(note) AS f FROM {c}.main.other_secret")
        expect_allowed(client=admin, sql="USE main")
        expect_allowed(client=admin, sql="GRANT SELECT ON rpt.own_v TO writers")
        for user in ["wes", "admin"]:
            for path, client in sessions(server=server, catalog=c, user=user):
                assert rows(client=client, sql=f"SELECT f FROM {c}.rpt.own_v") == [("rpt other-secret-A",)], path
    finally:
        expect_allowed(client=admin, sql="DROP VIEW IF EXISTS rpt.own_v")
        expect_allowed(client=admin, sql="DROP MACRO rpt.fmt")


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
