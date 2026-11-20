"""DuckLake's functions for store users: EXECUTE on the function, and on the catalog it names,
a privilege granted ON CATALOG: SELECT to inspect (ducklake_snapshots, ducklake_list_files,
...), MAINTAIN to maintain (ducklake_merge_adjacent_files, ducklake_expire_snapshots,
ducklake_set_option, ...). Before 2.13.4 an EXECUTE grant alone reached every DuckLake catalog,
and DuckLake's compaction and flush never ran for a store user. The catalog must be a string
literal; a view or macro over a DuckLake call is refused, as for catalog_access tokens
(test_tenant_isolation.py). Lakes: metadata in PostgreSQL, data on the S3 gateway
(docker-compose.yml), and one on local disk with a row policy."""

import secrets

import pytest

from rbac import expect_allowed, expect_blocked, expect_error, start_rbac_server
from services import PG_PORT, S3_PORT, pg_conninfo, require

# PostgreSQL and the S3 gateway run in Docker, on the Linux runners only.
pytestmark = [pytest.mark.services, pytest.mark.rbac_store]

READ = ["ducklake_snapshots", "ducklake_table_info", "ducklake_list_files", "ducklake_current_snapshot",
        "ducklake_last_committed_snapshot", "ducklake_options", "ducklake_settings", "ducklake_table_insertions",
        "ducklake_table_deletions"]
WRITE = ["ducklake_merge_adjacent_files", "ducklake_expire_snapshots", "ducklake_cleanup_old_files",
         "ducklake_delete_orphaned_files", "ducklake_flush_inlined_data", "ducklake_rewrite_data_files",
         "ducklake_set_option", "ducklake_set_commit_message"]
WRAPPED = "a view or macro cannot wrap a DuckLake function"


def needs(function: str, privilege: str, catalog: str, user: str) -> str:
    verb = "reads" if privilege == "SELECT" else "maintains"
    return (f"Hardcharger blocked {function}() on catalog {catalog}: it {verb} the whole catalog, which needs "
            f"{privilege} ON CATALOG {catalog}, and user {user} does not hold it")


@pytest.fixture(scope="module")
def world(module_launcher, store, tmp_path_factory):
    require(port=PG_PORT, what="PostgreSQL")
    require(port=S3_PORT, what="the S3 gateway")
    work = tmp_path_factory.mktemp("lakes")
    s3 = f"s3://ducklake/functions-{secrets.token_hex(nbytes=4)}"
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               args=["--database", str(work / "home.duckdb")],
                               env={"HARDCHARGER_INIT_SQL_COMMANDS": "INSTALL httpfs; LOAD httpfs; INSTALL ducklake; "
                                                                     "LOAD ducklake; INSTALL postgres; LOAD postgres;"})
    admin = server.admin()
    (work / "policy").mkdir()
    setup = [f"CREATE SECRET s3_functions (TYPE s3, KEY_ID 'minioadmin', SECRET 'minioadmin', "
             f"ENDPOINT '127.0.0.1:{S3_PORT}', USE_SSL false, URL_STYLE 'path', REGION 'us-east-1')"]
    for lake in ("lake_a", "lake_b"):
        setup.append(f"ATTACH 'ducklake:postgres:{pg_conninfo()}' AS {lake} "
                     f"(DATA_PATH '{s3}/{lake}/', METADATA_SCHEMA '{lake}_{secrets.token_hex(nbytes=4)}')")
    setup += [f"ATTACH 'ducklake:{work}/policy.ducklake' AS lake_p (DATA_PATH '{work}/policy/')",
              "CREATE TABLE lake_a.main.t AS SELECT range AS i FROM range(100)",
              "CREATE TABLE lake_b.main.secret_b AS SELECT 'b-' || range AS s FROM range(100)",
              # Every DuckLake function a role could be granted, so that what's missing is the
              # catalog privilege the test is about.
              "CREATE ROLE reader", "CREATE ROLE maintainer", "CREATE ROLE executor", "CREATE ROLE owner_only",
              "GRANT SELECT ON CATALOG lake_a TO reader",
              "GRANT SELECT ON CATALOG lake_a TO maintainer", "GRANT MAINTAIN ON CATALOG lake_a TO maintainer",
              "GRANT SELECT ON CATALOG lake_p TO maintainer", "GRANT MAINTAIN ON CATALOG lake_p TO maintainer",
              # A table grant, a schema grant: neither reaches the whole catalog.
              "GRANT SELECT ON lake_a.main.t TO executor", "GRANT SELECT ON SCHEMA lake_a.main TO executor",
              "GRANT ALL ON CATALOG lake_a TO owner_only",
              "CREATE USER rita", "CREATE USER max", "CREATE USER exe", "CREATE USER olga",
              "GRANT reader TO rita", "GRANT maintainer TO max", "GRANT executor TO exe", "GRANT owner_only TO olga"]
    for role in ("reader", "maintainer", "executor"):
        setup += [f"GRANT EXECUTE ON TABLE FUNCTION {f} TO {role}" for f in READ + WRITE]
    for sql in setup:
        expect_allowed(client=admin, sql=sql)
    yield {"server": server, "admin": admin, "rita": server.user(name="rita"), "max": server.user(name="max"),
           "exe": server.user(name="exe"), "olga": server.user(name="olga"), "s3": s3}
    for lake in ("lake_a", "lake_b"):
        expect_allowed(client=admin, sql=f"DETACH {lake}")


def read_calls(admin, lake: str = "lake_a") -> list[str]:
    last = admin.run(sql=f"SELECT max(snapshot_id) AS s FROM ducklake_snapshots('{lake}')").column("s").to_pylist()[0]
    return [f"SELECT * FROM ducklake_snapshots('{lake}')", f"SELECT * FROM ducklake_table_info('{lake}')",
            f"SELECT * FROM ducklake_list_files('{lake}', 't')", f"SELECT * FROM ducklake_current_snapshot('{lake}')",
            f"SELECT * FROM ducklake_last_committed_snapshot('{lake}')", f"SELECT * FROM ducklake_options('{lake}')",
            f"SELECT * FROM ducklake_settings('{lake}')",
            f"SELECT * FROM ducklake_table_insertions('{lake}', 'main', 't', 0, {last})",
            f"SELECT * FROM ducklake_table_deletions('{lake}', 'main', 't', 0, {last})",
            f"SELECT * FROM ducklake_table_changes('{lake}', 'main', 't', 0, {last})",
            f"CALL ducklake_snapshots('{lake}')", f"SELECT * FROM {lake}.snapshots()", f"SELECT * FROM {lake}.main.options()",
            f"SELECT * FROM {lake}.table_changes('t', 0, {last})"]


def maintenance_calls(lake: str = "lake_a") -> list[str]:
    return [f"CALL ducklake_expire_snapshots('{lake}', older_than => now() - INTERVAL 1 DAY, dry_run => true)",
            f"CALL ducklake_cleanup_old_files('{lake}', dry_run => true)",
            f"CALL ducklake_delete_orphaned_files('{lake}', dry_run => true)",
            f"CALL ducklake_set_option('{lake}', 'target_file_size', '512MB')",
            f"CALL ducklake_set_commit_message('{lake}', 'max', 'maintenance')",
            f"CALL ducklake_merge_adjacent_files('{lake}')", f"CALL ducklake_rewrite_data_files('{lake}')",
            f"CALL ducklake_flush_inlined_data('{lake}')", f"CALL {lake}.merge_adjacent_files()",
            f"CALL {lake}.set_option('target_file_size', '512MB')"]


def fragmented(admin, table: str, deleted: bool) -> None:
    """Four data files (and, if `deleted`, deletes in each): something to compact."""
    expect_allowed(client=admin, sql=f"CREATE OR REPLACE TABLE {table} (i INT, region VARCHAR)")
    for k in range(4):
        expect_allowed(client=admin, sql=f"INSERT INTO {table} SELECT range, CASE WHEN range % 2 = 0 THEN 'EU' "
                                         f"ELSE 'US' END FROM range({k * 1000}, {k * 1000 + 1000})")
    if deleted:
        expect_allowed(client=admin, sql=f"DELETE FROM {table} WHERE i % 3 = 0")


def files_of(admin, lake: str, table: str) -> int:
    return admin.run(sql=f"SELECT count(*) AS n FROM ducklake_list_files('{lake}', '{table}')").column("n").to_pylist()[0]


def test_inspection_with_select_on_the_catalog(world):
    for client in (world["rita"], world["max"]):
        for sql in read_calls(admin=world["admin"]):
            expect_allowed(client=client, sql=sql)
        assert client.run(sql="SELECT count(*) AS n FROM ducklake_snapshots('lake_a')").column("n").to_pylist()[0] > 0
        assert client.run(sql="SELECT count(*) AS n FROM ducklake_table_insertions('lake_a', 'main', 't', 0, 1)"
                          ).column("n").to_pylist() == [100]


def test_execute_without_the_catalog_privilege_is_refused(world):
    # exe holds EXECUTE on every DuckLake function, and SELECT on the table and its schema.
    exe = world["exe"]
    assert exe.run(sql="SELECT count(*) AS n FROM lake_a.main.t").column("n").to_pylist() == [100]
    for sql in read_calls(admin=world["admin"]):
        expect_blocked(client=exe, sql=sql, why="needs SELECT ON CATALOG lake_a, and user exe does not hold it")
    expect_blocked(client=exe, sql="SELECT * FROM ducklake_snapshots('lake_a')",
                   why=needs(function="ducklake_snapshots", privilege="SELECT", catalog="lake_a", user="exe"))
    for sql in maintenance_calls():
        expect_blocked(client=exe, sql=sql, why="needs MAINTAIN ON CATALOG lake_a, and user exe does not hold it")


def test_maintenance_needs_maintain_on_the_catalog(world):
    rita = world["rita"]  # EXECUTE on everything, SELECT ON CATALOG lake_a, no MAINTAIN
    for sql in maintenance_calls():
        expect_blocked(client=rita, sql=sql, why="needs MAINTAIN ON CATALOG lake_a, and user rita does not hold it")
    expect_blocked(client=rita, sql="CALL ducklake_expire_snapshots('lake_a', older_than => now())",
                   why=needs(function="ducklake_expire_snapshots", privilege="MAINTAIN", catalog="lake_a", user="rita"))
    options = world["admin"].run(sql="SELECT option_name, value FROM ducklake_options('lake_a')").to_pylist()
    assert not any(o["option_name"] == "target_file_size" for o in options), options


def test_maintenance_with_maintain_on_the_catalog(world):
    admin, max_ = world["admin"], world["max"]
    for sql in maintenance_calls():
        expect_allowed(client=max_, sql=sql)
    # The ones that rewrite files, with files to rewrite: DuckLake's own operators.
    fragmented(admin=admin, table="lake_a.main.merge_me", deleted=False)
    assert files_of(admin=admin, lake="lake_a", table="merge_me") == 4
    expect_allowed(client=max_, sql="CALL ducklake_merge_adjacent_files('lake_a')")
    assert files_of(admin=admin, lake="lake_a", table="merge_me") == 1
    fragmented(admin=admin, table="lake_a.main.rewrite_me", deleted=True)
    rewritten = max_.run(sql="CALL ducklake_rewrite_data_files('lake_a', delete_threshold => 0.1)").to_pylist()
    assert any(r["table_name"] == "rewrite_me" and r["files_created"] == 1 for r in rewritten), rewritten
    assert files_of(admin=admin, lake="lake_a", table="rewrite_me") == 1
    expect_allowed(client=admin, sql="CREATE OR REPLACE TABLE lake_a.main.inlined (i INT)")
    expect_allowed(client=admin, sql="CALL ducklake_set_option('lake_a', 'data_inlining_row_limit', 100, "
                                     "table_name => 'inlined')")
    expect_allowed(client=admin, sql="INSERT INTO lake_a.main.inlined SELECT range FROM range(5)")
    assert files_of(admin=admin, lake="lake_a", table="inlined") == 0
    flushed = max_.run(sql="CALL ducklake_flush_inlined_data('lake_a')").to_pylist()
    assert any(r["table_name"] == "inlined" for r in flushed), flushed
    assert files_of(admin=admin, lake="lake_a", table="inlined") == 1
    for table, rows in (("merge_me", 4000), ("rewrite_me", 2666), ("inlined", 5)):
        assert admin.run(sql=f"SELECT count(*) AS n FROM lake_a.main.{table}").column("n").to_pylist() == [rows]


def test_the_other_catalog_is_refused(world):
    for name in ("rita", "max"):
        for sql in read_calls(admin=world["admin"], lake="lake_b"):
            expect_blocked(client=world[name], sql=sql,
                           why=f"needs SELECT ON CATALOG lake_b, and user {name} does not hold it")
        for sql in maintenance_calls(lake="lake_b"):
            expect_blocked(client=world[name], sql=sql,
                           why=f"needs MAINTAIN ON CATALOG lake_b, and user {name} does not hold it")


def test_the_execute_hint_names_the_function_that_runs(world):
    # olga holds ALL ON CATALOG lake_a, but EXECUTE on no DuckLake function.
    olga = world["olga"]
    for sql, function in [("SELECT * FROM lake_a.snapshots()", "ducklake_snapshots"),
                          ("SELECT * FROM ducklake_snapshots('lake_a')", "ducklake_snapshots"),
                          ("CALL lake_a.merge_adjacent_files()", "ducklake_merge_adjacent_files"),
                          ("SELECT * FROM lake_a.table_changes('t', 0, 1)", "ducklake_table_insertions")]:
        expect_blocked(client=olga, sql=sql, why=f"Hardcharger blocked the table function {function}: under access "
                                                 f"control it is available to the administrator only, or to a role "
                                                 f"granted EXECUTE ON TABLE FUNCTION {function}")


def test_the_catalog_must_be_a_literal(world):
    rita = world["rita"]
    for sql in ["SELECT * FROM ducklake_snapshots('lake_' || 'a')", "SELECT * FROM ducklake_snapshots(current_database())",
                "SELECT * FROM ducklake_snapshots(?)"]:
        expect_blocked(client=rita, sql=sql, why="its catalog must be a string literal")
    expect_allowed(client=rita, sql="SELECT * FROM ducklake_snapshots('LAKE_A')")  # DuckDB's spelling rules apply


def test_views_and_macros_over_ducklake_calls_are_refused(world):
    admin, rita = world["admin"], world["rita"]
    expect_blocked(client=rita, sql="CREATE VIEW lake_a.main.v AS SELECT * FROM ducklake_snapshots('lake_a')",
                   why=f"Hardcharger blocked ducklake_snapshots() in a view: {WRAPPED}")
    for sql in ["CREATE OR REPLACE VIEW lake_a.main.admin_view AS SELECT * FROM ducklake_snapshots('lake_a')",
                "CREATE OR REPLACE MACRO lake_a.main.admin_macro() AS TABLE SELECT * FROM ducklake_snapshots('lake_a')",
                "CREATE OR REPLACE MACRO lake_a.main.admin_count() AS (SELECT count(*) FROM ducklake_snapshots('lake_a'))",
                "GRANT SELECT ON lake_a.main.admin_view TO reader"]:
        expect_allowed(client=admin, sql=sql)
    # rita reads lake_a and holds EXECUTE: the plain call works (the positive twin) ...
    expect_allowed(client=rita, sql="SELECT * FROM ducklake_snapshots('lake_a')")
    # ... and the administrator's view and macros over it do not.
    expect_blocked(client=rita, sql="SELECT * FROM lake_a.main.admin_view",
                   why=f"Hardcharger blocked ducklake_snapshots() in a view: {WRAPPED}")
    # (A macro's body is walked before binding wherever it is stored, a DuckLake catalog included: the
    # call in it is refused there as wrapped, before DuckDB binds it.)
    expect_blocked(client=rita, sql="SELECT * FROM lake_a.main.admin_macro()",
                   why=f"Hardcharger blocked ducklake_snapshots() in a macro: {WRAPPED}")
    expect_blocked(client=rita, sql="SELECT lake_a.main.admin_count()",
                   why=f"Hardcharger blocked ducklake_snapshots() in a macro: {WRAPPED}")
    # SET VARIABLE's query is read by the gate: its DuckLake call is held to the rule (lake_b refused before binding),
    # and SET VARIABLE itself is not available to a store user under access control.
    expect_blocked(client=rita, sql="SET VARIABLE n = (SELECT count(*) FROM ducklake_snapshots('lake_b'))",
                   why=needs(function="ducklake_snapshots", privilege="SELECT", catalog="lake_b", user="rita"))
    expect_blocked(client=rita, sql="SET VARIABLE n = (SELECT count(*) FROM ducklake_snapshots('lake_a'))",
                   why="SET VARIABLE is not available under access control")
    expect_blocked(client=rita, sql="SELECT * FROM query('SELECT * FROM ducklake_snapshots(''lake_a'')')",
                   why="query()")
    # A maintenance call in a macro, with files to compact: nothing is compacted.
    fragmented(admin=admin, table="lake_a.main.wrapped", deleted=False)
    expect_allowed(client=admin, sql="CREATE OR REPLACE MACRO lake_a.main.admin_merge() AS TABLE "
                                     "SELECT * FROM ducklake_merge_adjacent_files('lake_a')")
    expect_blocked(client=world["max"], sql="SELECT * FROM lake_a.main.admin_merge()",
                   why=f"Hardcharger blocked ducklake_merge_adjacent_files() in a macro: {WRAPPED}")
    assert files_of(admin=admin, lake="lake_a", table="wrapped") == 4
    for sql in ["DROP MACRO TABLE lake_a.main.admin_merge", "DROP MACRO TABLE lake_a.main.admin_macro",
                "DROP MACRO lake_a.main.admin_count", "DROP VIEW lake_a.main.admin_view", "DROP TABLE lake_a.main.wrapped"]:
        expect_allowed(client=admin, sql=sql)


def test_tables_with_row_or_masking_policies(world):
    """DuckLake's compaction would read a governed table through the user's policies and write
    only the rows they see back as the table's files, for everyone: refused for such a table,
    which keeps every row. Its change feed is a read of the table, filtered as any."""
    admin, max_ = world["admin"], world["max"]
    fragmented(admin=admin, table="lake_p.main.governed", deleted=False)
    expect_allowed(client=admin, sql="CREATE ROW POLICY eu_only ON lake_p.main.governed USING (region = 'EU')")
    assert max_.run(sql="SELECT count(*) AS n FROM lake_p.main.governed").column("n").to_pylist() == [2000]
    last = admin.run(sql="SELECT max(snapshot_id) AS s FROM ducklake_snapshots('lake_p')").column("s").to_pylist()[0]
    expect_blocked(client=max_, sql="CALL ducklake_merge_adjacent_files('lake_p')",
                   why="DuckLake's rewrite of the files of lake_p.main.governed, which has row or masking policies, "
                       "is not available under access control yet")
    # The change feed binds to a scan of the table, which the policies filter like any read.
    feed = max_.run(sql=f"SELECT count(*) AS n, count(*) FILTER (WHERE region <> 'EU') AS other "
                        f"FROM ducklake_table_insertions('lake_p', 'main', 'governed', 0, {last})").to_pylist()
    assert feed == [{"n": 2000, "other": 0}], feed
    feed = max_.run(sql=f"SELECT count(*) AS n FROM lake_p.table_changes('governed', 0, {last})").to_pylist()
    assert feed == [{"n": 2000}], feed
    assert files_of(admin=admin, lake="lake_p", table="governed") == 4
    assert admin.run(sql="SELECT count(*) AS n FROM lake_p.main.governed").column("n").to_pylist() == [4000]
    # The positive twin: the same table without its policy compacts, every row kept.
    expect_allowed(client=admin, sql="DROP ROW POLICY eu_only ON lake_p.main.governed")
    expect_allowed(client=max_, sql="CALL ducklake_merge_adjacent_files('lake_p')")
    assert files_of(admin=admin, lake="lake_p", table="governed") == 1
    assert admin.run(sql="SELECT count(*) AS n FROM lake_p.main.governed").column("n").to_pylist() == [4000]



def test_tables_named_like_ducklake_functions_are_ordinary_tables(world):
    """A store user with the grants creates and fills tables named settings, options, snapshots or
    ducklake_*, with column lists, as on 2.13.3 (review finding 07): none of them is a call."""
    admin = world["admin"]
    for sql in ["CREATE SCHEMA IF NOT EXISTS home.s", "CREATE OR REPLACE TABLE home.s.settings (k VARCHAR, v VARCHAR)",
                "CREATE OR REPLACE TABLE home.s.ducklake_events (id INT)",
                "CREATE OR REPLACE TABLE lake_a.main.options (k VARCHAR)",
                "CREATE ROLE IF NOT EXISTS worker", "CREATE USER IF NOT EXISTS wanda", "GRANT worker TO wanda",
                "GRANT ALL ON SCHEMA home.s TO worker", "GRANT ALL ON CATALOG lake_a TO worker"]:
        expect_allowed(client=admin, sql=sql)
    wanda = world["server"].user(name="wanda")
    for sql in ["INSERT INTO home.s.settings(k, v) VALUES ('a', 'b')", "INSERT INTO s.settings(k, v) VALUES ('c', 'd')",
                "CREATE TABLE home.s.options (k VARCHAR)", "CREATE INDEX ix ON home.s.settings(k)",
                "INSERT INTO home.s.ducklake_events(id) VALUES (1)", "CREATE TABLE home.s.ducklake_more (id INT)",
                "INSERT INTO lake_a.main.options(k) VALUES ('x')", "CREATE TABLE lake_a.main.snapshots (id INT)",
                "INSERT INTO lake_a.main.snapshots(id) VALUES (7)",
                "CREATE TABLE lake_a.main.ducklake_copy(i) AS SELECT i FROM lake_a.main.t"]:
        expect_allowed(client=wanda, sql=sql)
    assert wanda.run(sql="SELECT count(*) AS n FROM home.s.settings").column("n").to_pylist() == [2]
    assert wanda.run(sql="SELECT id FROM lake_a.main.snapshots").column("id").to_pylist() == [7]
    assert wanda.run(sql="SELECT count(*) AS n FROM lake_a.main.ducklake_copy").column("n").to_pylist() == [100]
    for sql in ["DROP TABLE lake_a.main.snapshots", "DROP TABLE lake_a.main.options", "DROP TABLE lake_a.main.ducklake_copy",
                "DROP SCHEMA home.s CASCADE"]:
        expect_allowed(client=admin, sql=sql)


def test_maintain_on_the_catalog_alone_compacts(world):
    """DuckLake's compaction reads the tables it rewrites and hands back counts, not rows: with
    MAINTAIN ON CATALOG (and EXECUTE) and no SELECT, a maintenance role compacts and still can't
    read a row (review finding 08). A table with row or masking policies stays refused."""
    admin = world["admin"]
    setup = ["CREATE ROLE IF NOT EXISTS m_only", "CREATE USER IF NOT EXISTS mona", "GRANT m_only TO mona",
             "GRANT MAINTAIN ON CATALOG lake_a TO m_only", "GRANT MAINTAIN ON CATALOG lake_p TO m_only"]
    setup += [f"GRANT EXECUTE ON TABLE FUNCTION {f} TO m_only" for f in WRITE]
    for sql in setup:
        expect_allowed(client=admin, sql=sql)
    mona = world["server"].user(name="mona")
    fragmented(admin=admin, table="lake_a.main.mona_merge", deleted=False)
    fragmented(admin=admin, table="lake_a.main.mona_rewrite", deleted=True)
    expect_allowed(client=mona, sql="CALL ducklake_merge_adjacent_files('lake_a')")
    assert files_of(admin=admin, lake="lake_a", table="mona_merge") == 1
    rewritten = mona.run(sql="CALL ducklake_rewrite_data_files('lake_a', delete_threshold => 0.1)").to_pylist()
    assert any(r["table_name"] == "mona_rewrite" for r in rewritten), rewritten
    assert files_of(admin=admin, lake="lake_a", table="mona_rewrite") == 1
    expect_allowed(client=admin, sql="CREATE OR REPLACE TABLE lake_a.main.mona_inlined (i INT)")
    expect_allowed(client=admin, sql="CALL ducklake_set_option('lake_a', 'data_inlining_row_limit', 100, "
                                     "table_name => 'mona_inlined')")
    expect_allowed(client=admin, sql="INSERT INTO lake_a.main.mona_inlined SELECT range FROM range(5)")
    expect_allowed(client=mona, sql="CALL ducklake_flush_inlined_data('lake_a')")
    assert files_of(admin=admin, lake="lake_a", table="mona_inlined") == 1
    # ... and still reads nothing: the rewrite's own scans are the only reads MAINTAIN covers.
    for table in ("mona_merge", "mona_rewrite", "mona_inlined"):
        expect_blocked(client=mona, sql=f"SELECT count(*) FROM lake_a.main.{table}",
                       why=f"user mona has no SELECT privilege on table lake_a.main.{table}")
    for table, rows in (("mona_merge", 4000), ("mona_rewrite", 2666), ("mona_inlined", 5)):
        assert admin.run(sql=f"SELECT count(*) AS n FROM lake_a.main.{table}").column("n").to_pylist() == [rows]
    # A table with a row policy: refused, files and rows kept.
    fragmented(admin=admin, table="lake_p.main.mona_governed", deleted=False)
    expect_allowed(client=admin, sql="CREATE ROW POLICY mona_eu ON lake_p.main.mona_governed USING (region = 'EU')")
    expect_blocked(client=mona, sql="CALL ducklake_merge_adjacent_files('lake_p')",
                   why="DuckLake's rewrite of the files of lake_p.main.mona_governed, which has row or masking policies")
    assert files_of(admin=admin, lake="lake_p", table="mona_governed") == 4
    for sql in ["DROP ROW POLICY mona_eu ON lake_p.main.mona_governed", "DROP TABLE lake_p.main.mona_governed",
                "DROP TABLE lake_a.main.mona_merge", "DROP TABLE lake_a.main.mona_rewrite",
                "DROP TABLE lake_a.main.mona_inlined"]:
        expect_allowed(client=admin, sql=sql)


def test_checkpoint_of_a_ducklake_catalog_needs_maintain(world):
    """CHECKPOINT of a DuckLake catalog runs DuckLake's maintenance on it: the CHECKPOINT privilege
    and MAINTAIN ON CATALOG (review finding 05). Other databases' checkpoints keep needing only
    CHECKPOINT."""
    admin = world["admin"]
    for sql in ["CREATE ROLE IF NOT EXISTS cp", "CREATE ROLE IF NOT EXISTS cp_maint", "CREATE USER IF NOT EXISTS chip",
                "CREATE USER IF NOT EXISTS cleo", "GRANT CHECKPOINT TO cp", "GRANT CHECKPOINT TO cp_maint",
                "GRANT MAINTAIN ON CATALOG lake_a TO cp_maint", "GRANT cp TO chip", "GRANT cp_maint TO cleo"]:
        expect_allowed(client=admin, sql=sql)
    chip, cleo = world["server"].user(name="chip"), world["server"].user(name="cleo")
    fragmented(admin=admin, table="lake_a.main.cp_frag", deleted=False)
    for sql in ["CHECKPOINT lake_a", "FORCE CHECKPOINT lake_a", "CALL checkpoint('lake_a')",
                "SELECT * FROM force_checkpoint('lake_a')"]:
        expect_blocked(client=chip, sql=sql,
                       why="Hardcharger blocked CHECKPOINT on catalog lake_a: it maintains the whole catalog, which needs "
                           "MAINTAIN ON CATALOG lake_a, and user chip does not hold it")
    expect_blocked(client=cleo, sql="CHECKPOINT lake_b", why="needs MAINTAIN ON CATALOG lake_b, and user cleo does not hold it")
    assert files_of(admin=admin, lake="lake_a", table="cp_frag") == 4
    # The twins: with MAINTAIN on the lake, CHECKPOINT compacts it; any holder of CHECKPOINT checkpoints home.
    expect_allowed(client=cleo, sql="CHECKPOINT lake_a")
    assert files_of(admin=admin, lake="lake_a", table="cp_frag") == 1
    assert admin.run(sql="SELECT count(*) AS n FROM lake_a.main.cp_frag").column("n").to_pylist() == [4000]
    expect_allowed(client=chip, sql="CHECKPOINT home")
    expect_allowed(client=admin, sql="DROP TABLE lake_a.main.cp_frag")


def test_ducklake_calls_in_places_the_walk_does_not_read_are_refused(world):
    rita = world["rita"]  # SELECT ON CATALOG lake_a, nothing on lake_b
    on_b = "(SELECT count(*) FROM ducklake_snapshots('lake_b'))"
    denied = needs(function="ducklake_snapshots", privilege="SELECT", catalog="lake_b", user="rita")
    for sql, why in [(f"SELECT {on_b}", denied), (f"VALUES ({on_b})", denied),
                     (f"SET VARIABLE v = {on_b}", denied),
                     (f"SELECT * FROM lake_a.main.t WHERE i IN (SELECT snapshot_id FROM ducklake_snapshots('lake_b'))",
                      denied)]:
        expect_blocked(client=rita, sql=sql, why=why)
    for sql, why in [(f"SELECT * FROM lake_a.main.t AT (VERSION => {on_b})", "AT clause cannot contain subqueries"),
                     (f"CREATE TABLE lake_a.main.x2 (i BIGINT DEFAULT {on_b})", "DEFAULT value cannot contain subqueries")]:
        error = expect_error(call=lambda sql=sql: rita.run(sql=sql), code="sql_error")
        assert why in error.detail, error.detail
    assert rita.run(sql="SELECT (SELECT count(*) FROM ducklake_snapshots('lake_a')) > 0 AS ok").column(0).to_pylist() == [True]


# Review finding 14: an administrator's ordinary macro stored in a DuckLake catalog under a name GizmoSQL
# gives a DuckLake method (list_files, branches, get_option: DuckLake defines no catalog macro by those names)
# is a user macro: its body is walked with the caller's arguments before DuckDB binds it.
LAKE_NAMED = ["list_files", "branches", "get_option"]


def test_a_lake_macro_named_like_a_ducklake_method_is_walked(world):
    admin, rita = world["admin"], world["rita"]
    for sql in ["CREATE SEQUENCE IF NOT EXISTS home.main.seq14", "SELECT setval('home.main.seq14', 1)",
                "CREATE OR REPLACE MACRO lake_a.main.ordinary(s) AS TABLE SELECT * FROM query(s)",
                *[f"CREATE OR REPLACE MACRO lake_a.main.{name}(s) AS TABLE SELECT * FROM query(s)" for name in LAKE_NAMED]]:
        expect_allowed(client=admin, sql=sql)
    seq = lambda: admin.run(sql="SELECT last_value FROM duckdb_sequences() WHERE sequence_name = 'seq14'").column(0).to_pylist()[0]
    try:
        for name in ["ordinary", *LAKE_NAMED]:
            expect_blocked(client=rita, sql=f"SELECT * FROM lake_a.main.{name}('SELECT * FROM range(setval(''home.main.seq14'', 7))')",
                           why="user rita may not setval() of sequence")
            assert seq() == 1, name
        # A table of a catalog it holds nothing on answers through the macro exactly as the statement itself
        # does (a store user is told a table does not exist, and refused one it may not read).
        for table in ("secret_b", "no_such_table"):
            def answer(sql):
                try:
                    rita.run(sql=sql)
                except Exception as error:  # noqa: BLE001 - the answer is what is compared
                    return error.code, error.detail.split("\n")[0]
                return None
            direct = answer(f"SELECT * FROM lake_b.main.{table}")
            assert direct is not None
            assert answer(f"SELECT * FROM lake_a.main.list_files('SELECT * FROM lake_b.main.{table}')") == direct, table
        # The twins: DuckLake's own catalog macro, and what rita may run herself through the macros.
        assert rita.run(sql="SELECT count(*) AS n FROM lake_a.snapshots()").column("n").to_pylist()[0] > 0
        assert rita.run(sql="SELECT * FROM lake_a.main.ordinary('SELECT 42 AS answer')").to_pylist() == [{"answer": 42}]
        assert rita.run(sql="SELECT * FROM lake_a.main.list_files('SELECT 42 AS answer')").to_pylist() == [{"answer": 42}]
    finally:
        for name in ["ordinary", *LAKE_NAMED]:
            expect_allowed(client=admin, sql=f"DROP MACRO TABLE lake_a.main.{name}")
