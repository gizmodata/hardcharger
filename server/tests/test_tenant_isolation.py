"""Catalog isolation between tenants on one server, as GizmoData Cloud runs it: each app has
its own DuckLake catalog (metadata in PostgreSQL, data in object storage) and a token whose
catalog_access rules give it its own catalog only. A customer's security review of GizmoSQL
(2026-10-27) found four ways for one app's token to reach another's catalog there; this suite
replays each of them, and the paths around them, against both a `user` token and an
administrator's token with the same rules. Every one must be refused, while the token keeps
full read-write on its own catalog."""

import secrets

import pytest

from conftest import mint_server_token
from services import PG_PORT, S3_PORT, pg_conninfo, require

A, B = "tenant_a", "tenant_b"
RULES = [{"catalog": A, "access": "write"}, {"catalog": "*", "access": "none"}]

# PostgreSQL and the S3 gateway run in Docker, on the Linux runners only.
pytestmark = pytest.mark.services


@pytest.fixture(scope="module")
def world(module_launcher):
    require(port=PG_PORT, what="PostgreSQL")
    require(port=S3_PORT, what="the S3 gateway")
    s3 = f"s3://ducklake/tenants-{secrets.token_hex(nbytes=4)}"
    server = module_launcher.start(
        args=["--database", "tenants.duckdb"],
        env={"HARDCHARGER_INIT_SQL_COMMANDS": "INSTALL httpfs; LOAD httpfs; INSTALL ducklake; LOAD ducklake; "
                                              "INSTALL postgres; LOAD postgres;"})
    admin = server.admin()
    admin.execute(sql=f"CREATE SECRET s3_tenants (TYPE s3, KEY_ID 'minioadmin', SECRET 'minioadmin', "
                      f"ENDPOINT '127.0.0.1:{S3_PORT}', USE_SSL false, URL_STYLE 'path', REGION 'us-east-1')")
    for tenant in (A, B):
        schema = f"{tenant}_{secrets.token_hex(nbytes=4)}"
        admin.execute(sql=f"ATTACH 'ducklake:postgres:{pg_conninfo()}' AS {tenant} "
                          f"(DATA_PATH '{s3}/{tenant}/', METADATA_SCHEMA '{schema}')")
    admin.execute(sql=f"CREATE TABLE {A}.main.mine AS SELECT range AS i FROM range(100)")
    admin.execute(sql=f"CREATE TABLE {B}.main.secret_b AS SELECT 'b-secret-' || range AS s FROM range(100)")
    b_file = admin.query(sql=f"SELECT data_file FROM ducklake_list_files('{B}', 'secret_b')").column(0).to_pylist()[0]
    yield {"server": server, "s3": s3, "b_file": b_file}
    for tenant in (A, B):
        admin.execute(sql=f"DETACH {tenant}")


@pytest.fixture(params=["user", "admin"])
def tenant_a(request, world):
    """Tenant A's session: a `user` token, or an administrator's token, with A's rules."""
    return world["server"].as_role(role=request.param, subject=f"app_a_{request.param}", catalog_access=RULES)


def refused(session, sql: str, why: str) -> None:
    detail = session.error(sql=sql)["detail"]
    assert why in detail, f"{sql}: expected {why!r} in: {detail}"


def test_the_token_keeps_its_own_catalog(tenant_a):
    for sql in [f"INSERT INTO {A}.main.mine VALUES (1000)", f"UPDATE {A}.main.mine SET i = i WHERE i = 1000",
                f"CREATE TABLE {A}.main.t2 AS SELECT 1 AS x", f"ALTER TABLE {A}.main.t2 ADD COLUMN y INT",
                f"CREATE VIEW {A}.main.v AS SELECT * FROM {A}.main.t2", f"DROP VIEW {A}.main.v",
                f"DROP TABLE {A}.main.t2", f"DELETE FROM {A}.main.mine WHERE i = 1000"]:
        tenant_a.execute(sql=sql)
    assert tenant_a.query(sql=f"SELECT count(*) AS n FROM {A}.main.mine").column("n").to_pylist() == [100]
    seen = set(tenant_a.query(sql="SELECT database_name FROM duckdb_databases()").column(0).to_pylist())
    assert A in seen and B not in seen, seen


def test_no_token_sees_where_a_database_lives(tenant_a):
    # A DuckLake catalog's path is its metadata's connection string, password included.
    rows = tenant_a.query(sql="SELECT database_name, path FROM duckdb_databases()").to_pylist()
    assert rows and all(row["path"] is None for row in rows), rows
    for sql in ["SELECT count(*) AS n FROM duckdb_databases() WHERE path LIKE '%password%'",
                "SELECT count(*) AS n FROM duckdb_databases() WHERE upper(path) IS NOT NULL"]:
        assert tenant_a.query(sql=sql).column("n").to_pylist() == [0], sql


def test_finding_1_no_sql_reaches_another_catalogs_metadata_database(tenant_a):
    meta_b = f"__ducklake_metadata_{B}"
    for sql, why in [
        (f"SELECT * FROM postgres_query('{meta_b}', 'select table_id, table_name, path from ducklake_table')",
         "the table function postgres_query"),
        (f"SELECT * FROM postgres_query('{meta_b}', 'select datname from pg_database')", "the table function postgres_query"),
        (f"CALL postgres_execute('{meta_b}', 'select 1')", "the table function postgres_execute"),
        (f"SELECT * FROM postgres_scan('{pg_conninfo()}', 'public', 'ducklake_table')", "the table function postgres_scan"),
        (f"SELECT * FROM {meta_b}.ducklake_table", f"read access to catalog '{meta_b}'"),
        # Its own metadata too: DuckLake's tables are DuckLake's (inlined rows live there).
        (f"SELECT * FROM __ducklake_metadata_{A}.ducklake_table", f"read access to catalog '__ducklake_metadata_{A}'"),
        (f"SELECT * FROM query('SELECT * FROM {B}.main.secret_b')", "query()"),
        (f"SELECT * FROM query_table('{B}.main.secret_b')", "query_table()"),
    ]:
        refused(session=tenant_a, sql=sql, why=why)


def test_finding_2_no_write_reaches_another_catalogs_storage(tenant_a, world):
    s3 = world["s3"]
    for sql, why in [
        (f"COPY (SELECT 1 AS i) TO '{s3}/{B}/main/secret_b/injected-by-a.parquet' (FORMAT parquet)",
         f"reaches into the storage of DuckLake catalog {B}"),
        # Its own catalog's storage too: only DuckLake writes there.
        (f"COPY (SELECT 1 AS i) TO '{s3}/{A}/main/mine/own-prefix.parquet' (FORMAT parquet)",
         f"reaches into the storage of DuckLake catalog {A}"),
        (f"COPY (SELECT 1 AS i) TO '{s3}/elsewhere/x.parquet' (FORMAT parquet)", "WRITE OBJECT STORAGE"),
    ]:
        refused(session=tenant_a, sql=sql, why=why)


def test_finding_3_ducklake_functions_dont_reach_another_catalog(tenant_a):
    for sql, why in [(f"SELECT * FROM ducklake_list_files('{B}', 'secret_b')", READ_B),
                     (f"SELECT * FROM ducklake_snapshots('{B}')", READ_B),
                     (f"SELECT * FROM ducklake_table_info('{B}')", READ_B),
                     (f"SELECT * FROM ducklake_table_changes('{B}', 'main', 'secret_b', 0, 1)", READ_B),
                     (f"CALL ducklake_expire_snapshots('{B}', older_than => now())", WRITE_B),
                     (f"CALL ducklake_merge_adjacent_files('{B}')", WRITE_B),
                     (f"CALL ducklake_set_option('{B}', 'target_file_size', '1MB')", WRITE_B),
                     (f"SELECT * FROM {B}.snapshots()", READ_B),
                     (f"SELECT * FROM {B}.main.settings()", READ_B),
                     (f"CALL {B}.merge_adjacent_files()", WRITE_B),
                     # Spelled as DuckDB resolves it: the rules match the catalog's own name.
                     (f"SELECT * FROM ducklake_snapshots('{B.upper()}')", READ_B),
                     # Not a DuckLake catalog, nor one the rules allow.
                     (f"SELECT * FROM ducklake_snapshots('__ducklake_metadata_{A}')",
                      f"read access to catalog '__ducklake_metadata_{A}'")]:
        refused(session=tenant_a, sql=sql, why=why)


# GizmoSQL 1.42.3's DuckLake rules for catalog_access tokens: each ducklake_* call, and each
# call of a DuckLake catalog's own macros (lake.snapshots()), is checked against the token's
# access to the catalog it names: read to inspect, write to maintain. These are the positive
# twins of finding 3, which a blanket refusal of every DuckLake function also passed.
RULES_READ = [{"catalog": A, "access": "read"}, {"catalog": "*", "access": "none"}]
READ_B = f"You do not have read access to catalog '{B}'"
WRITE_B = f"You do not have write access to catalog '{B}'"
WRITE_A = f"You do not have write access to catalog '{A}'"


@pytest.fixture(params=["user", "admin"])
def tenant_a_reader(request, world):
    """Tenant A's session with read access only: a `user` token, or an administrator's token."""
    return world["server"].as_role(role=request.param, subject=f"app_a_reader_{request.param}",
                                   catalog_access=RULES_READ)


def read_calls(admin) -> list[str]:
    """Every inspection call DuckDB 2.0's DuckLake has, in each spelling, on catalog A."""
    last = admin.query(sql=f"SELECT max(snapshot_id) AS s FROM ducklake_snapshots('{A}')").column("s").to_pylist()[0]
    return [f"SELECT * FROM ducklake_snapshots('{A}')", f"SELECT * FROM ducklake_table_info('{A}')",
            f"SELECT * FROM ducklake_list_files('{A}', 'mine')", f"SELECT * FROM ducklake_current_snapshot('{A}')",
            f"SELECT * FROM ducklake_last_committed_snapshot('{A}')", f"SELECT * FROM ducklake_options('{A}')",
            f"SELECT * FROM ducklake_settings('{A}')",
            f"SELECT * FROM ducklake_table_insertions('{A}', 'main', 'mine', 0, {last})",
            f"SELECT * FROM ducklake_table_deletions('{A}', 'main', 'mine', 0, {last})",
            f"SELECT * FROM ducklake_table_changes('{A}', 'main', 'mine', 0, {last})",
            f"CALL ducklake_snapshots('{A}')",
            f"SELECT * FROM {A}.snapshots()", f"SELECT * FROM {A}.main.snapshots()", f"SELECT * FROM {A}.table_info()",
            f"SELECT * FROM {A}.options()", f"SELECT * FROM {A}.settings()", f"SELECT * FROM {A}.current_snapshot()",
            f"SELECT * FROM {A}.last_committed_snapshot()", f"SELECT * FROM {A}.table_changes('mine', 0, {last})",
            f"SELECT * FROM {A}.table_insertions('mine', 0, {last})", f"SELECT * FROM {A}.table_deletions('mine', 0, {last})",
            # In the statement forms the gate walks.
            f"WITH s AS (SELECT * FROM ducklake_snapshots('{A}')) SELECT count(*) FROM s",
            f"SELECT * FROM {A}.main.mine WHERE i < (SELECT count(*) FROM ducklake_snapshots('{A}'))",
            f"EXPLAIN SELECT * FROM ducklake_snapshots('{A}')", f"DESCRIBE SELECT * FROM ducklake_snapshots('{A}')",
            f"SELECT * FROM ducklake_snapshots /* a comment */ ('{A}')", f"SELECT * FROM \"ducklake_snapshots\"('{A}')"]


def fragmented(admin, name: str, deleted: bool) -> None:
    """A table of A with four data files (and, if `deleted`, a delete file on each), so that
    compaction has something to do."""
    admin.execute(sql=f"CREATE OR REPLACE TABLE {A}.main.{name} (i INT)")
    for k in range(4):
        admin.execute(sql=f"INSERT INTO {A}.main.{name} SELECT range FROM range({k * 1000}, {k * 1000 + 1000})")
    if deleted:
        admin.execute(sql=f"DELETE FROM {A}.main.{name} WHERE i % 2 = 0")


def files_of(admin, name: str) -> int:
    return admin.query(sql=f"SELECT count(*) AS n FROM ducklake_list_files('{A}', '{name}')").column("n").to_pylist()[0]


def test_ducklake_inspection_works_on_the_tokens_own_catalog_with_read_access(world, tenant_a_reader, tenant_a):
    for session in (tenant_a_reader, tenant_a):
        for sql in read_calls(admin=world["server"].admin()):
            session.query(sql=sql)
        assert session.query(sql=f"SELECT count(*) AS n FROM ducklake_snapshots('{A}')").column("n").to_pylist()[0] > 0
        assert session.query(sql=f"SELECT count(*) AS n FROM {A}.snapshots()").column("n").to_pylist()[0] > 0
        files = session.query(sql=f"SELECT data_file FROM ducklake_list_files('{A}', 'mine')").column(0).to_pylist()
        assert files and all(f.startswith(f"{world['s3']}/{A}/") for f in files), files
        # The session's current catalog's macros, unqualified.
        session.execute(sql=f"USE {A}")
        assert session.query(sql="SELECT count(*) AS n FROM snapshots()").column("n").to_pylist()[0] > 0
        assert session.query(sql="SELECT count(*) AS n FROM main.snapshots()").column("n").to_pylist()[0] > 0


def test_ducklake_inspection_of_another_catalog_is_refused_with_read_access(tenant_a_reader):
    for sql in [f"SELECT * FROM ducklake_snapshots('{B}')", f"SELECT * FROM ducklake_list_files('{B}', 'secret_b')",
                f"SELECT * FROM {B}.snapshots()", f"SELECT * FROM {B}.table_info()"]:
        refused(session=tenant_a_reader, sql=sql, why=READ_B)


def maintenance_calls() -> list[str]:
    """Every maintenance call on catalog A that changes nothing the other tests read."""
    return [f"CALL ducklake_expire_snapshots('{A}', older_than => now() - INTERVAL 1 DAY, dry_run => true)",
            f"CALL ducklake_cleanup_old_files('{A}', dry_run => true)",
            f"CALL ducklake_delete_orphaned_files('{A}', dry_run => true)",
            f"CALL ducklake_set_option('{A}', 'target_file_size', '512MB')",
            f"CALL ducklake_set_commit_message('{A}', 'app_a', 'maintenance')",
            f"CALL ducklake_flush_inlined_data('{A}')", f"CALL ducklake_merge_adjacent_files('{A}')",
            f"CALL ducklake_rewrite_data_files('{A}')",
            f"CALL {A}.set_option('target_file_size', '512MB')", f"CALL {A}.set_commit_message('app_a', 'maintenance')",
            f"CALL {A}.merge_adjacent_files()",
            f"SELECT * FROM ducklake_expire_snapshots('{A}', older_than => now() - INTERVAL 1 DAY, dry_run => true)"]


def test_ducklake_maintenance_works_on_the_tokens_own_catalog_with_write_access(world, tenant_a, request):
    admin = world["server"].admin()
    for sql in maintenance_calls():
        tenant_a.execute(sql=sql)
    # The ones that rewrite files, with files to rewrite: DuckLake plans them as its own
    # operators (compaction, flush), which write into the catalog's storage.
    suffix = request.node.callspec.id.replace("-", "_")
    fragmented(admin=admin, name=f"merge_{suffix}", deleted=False)
    tenant_a.execute(sql=f"CALL ducklake_merge_adjacent_files('{A}')")
    assert files_of(admin=admin, name=f"merge_{suffix}") == 1
    fragmented(admin=admin, name=f"rewrite_{suffix}", deleted=True)
    rewritten = tenant_a.query(sql=f"CALL ducklake_rewrite_data_files('{A}', delete_threshold => 0.1)").to_pylist()
    assert any(r["table_name"] == f"rewrite_{suffix}" and r["files_created"] == 1 for r in rewritten), rewritten
    assert files_of(admin=admin, name=f"rewrite_{suffix}") == 1
    admin.execute(sql=f"CREATE OR REPLACE TABLE {A}.main.inlined_{suffix} (i INT)")
    admin.execute(sql=f"CALL ducklake_set_option('{A}', 'data_inlining_row_limit', 100, table_name => 'inlined_{suffix}')")
    admin.execute(sql=f"INSERT INTO {A}.main.inlined_{suffix} SELECT range FROM range(5)")
    assert files_of(admin=admin, name=f"inlined_{suffix}") == 0
    flushed = tenant_a.query(sql=f"CALL ducklake_flush_inlined_data('{A}')").to_pylist()
    assert any(r["table_name"] == f"inlined_{suffix}" for r in flushed), flushed
    assert files_of(admin=admin, name=f"inlined_{suffix}") == 1
    for name, rows in ((f"merge_{suffix}", 4000), (f"rewrite_{suffix}", 2000), (f"inlined_{suffix}", 5)):
        assert tenant_a.query(sql=f"SELECT count(*) AS n FROM {A}.main.{name}").column("n").to_pylist() == [rows]


def test_ducklake_maintenance_is_refused_with_read_access(world, tenant_a_reader):
    for sql in maintenance_calls():
        refused(session=tenant_a_reader, sql=sql, why=WRITE_A)
    # Refused before it runs: the option did not change.
    refused(session=tenant_a_reader, sql=f"CALL ducklake_set_option('{A}', 'target_file_size', '1KB')", why=WRITE_A)
    options = world["server"].admin().query(sql=f"SELECT option_name, value FROM ducklake_options('{A}')").to_pylist()
    assert not any(o["option_name"] == "target_file_size" and o["value"] == "1KB" for o in options), options


def test_ducklake_maintenance_is_refused_in_readonly_sessions(world):
    """A readonly token changes nothing, whatever its catalog_access rules say, and DuckDB does
    not count DuckLake's maintenance in FROM as a change: before 2.13.4 a readonly token
    could not call DuckLake functions at all, and it must not gain their writes now."""
    server, admin = world["server"], world["server"].admin()
    readonly = server.as_role(role="readonly", subject="app_a_readonly", catalog_access=RULES)
    token = mint_server_token(secret_key=server.secret_key, subject="app_a_read_only_session", role="user",
                              catalog_access=RULES)
    read_only_session = server.client(token=token).new_session(options={"read_only": True})
    before = admin.query(sql=f"SELECT snapshot_id FROM ducklake_snapshots('{A}') ORDER BY 1").column(0).to_pylist()
    writes = maintenance_calls() + [f"SELECT * FROM ducklake_set_option('{A}', 'target_file_size', '3KB')",
                                    f"SELECT * FROM {A}.set_option('target_file_size', '3KB')",
                                    f"SELECT * FROM ducklake_expire_snapshots('{A}', versions => [{before[0]}])",
                                    f"SELECT * FROM ducklake_rewrite_data_files('{A}')"]
    for sql in writes:
        refused(session=readonly, sql=sql,
                why="User 'app_a_readonly' has a readonly session and cannot run statements that modify state")
        detail = read_only_session.error(sql=sql)["detail"]
        assert "is not allowed in a read-only session" in detail or "list of what a read-only session may run" in detail, \
            f"{sql}: {detail}"
    after = admin.query(sql=f"SELECT snapshot_id FROM ducklake_snapshots('{A}') ORDER BY 1").column(0).to_pylist()
    assert after[:len(before)] == before, (before, after)
    options = admin.query(sql=f"SELECT option_name, value FROM ducklake_options('{A}')").to_pylist()
    assert not any(o["option_name"] == "target_file_size" and o["value"] in ("3000", "3KB") for o in options), options
    # Their positive twins: the readonly token still inspects its catalog (in FROM: a readonly
    # session runs no CALL, of any function).
    for sql in read_calls(admin=admin):
        if not sql.startswith("CALL "):
            readonly.query(sql=sql)
    assert read_only_session.query(sql=f"SELECT count(*) AS n FROM ducklake_snapshots('{A}')").column("n").to_pylist()[0] > 0


def test_ducklake_add_data_files_stays_the_administrators(tenant_a, world):
    for sql in [f"CALL ducklake_add_data_files('{A}', 'mine', '{world['b_file']}')",
                f"CALL ducklake_add_data_files('{A}', 'mine', '{world['s3']}/{A}/x.parquet')"]:
        refused(session=tenant_a, sql=sql, why="Hardcharger blocked ducklake_add_data_files(): it reaches files or "
                                               "metadata by path")
    # DuckLake's internal reader and commit take a path or a metadata schema, not a catalog.
    refused(session=tenant_a, sql=f"SELECT * FROM ducklake_scan('{world['b_file']}')",
            why="Hardcharger blocked ducklake_scan(): it reaches files or metadata by path")
    refused(session=tenant_a, sql=f"SELECT * FROM ducklake_something_new('{A}')",
            why="Hardcharger blocked ducklake_something_new(): access control does not know which catalog")


def test_ducklake_catalog_must_be_a_literal(tenant_a):
    for sql in [f"SELECT * FROM ducklake_snapshots('tenant_' || 'a')", "SELECT * FROM ducklake_snapshots(current_database())",
                f"SELECT * FROM ducklake_snapshots(lower('{A.upper()}'))", "CALL ducklake_merge_adjacent_files(getvariable('c'))"]:
        refused(session=tenant_a, sql=sql, why="Hardcharger blocked ducklake_")
        refused(session=tenant_a, sql=sql, why="(): its catalog must be a string literal")
    # A bound parameter is not a literal either: the gate checks the text, before binding.
    refused(session=tenant_a, sql="SELECT * FROM ducklake_snapshots(?)", why="its catalog must be a string literal")
    # Its positive twin: the same call with the literal.
    assert tenant_a.query(sql=f"SELECT count(*) AS n FROM ducklake_snapshots('{A}')").column("n").to_pylist()[0] > 0


def test_ducklake_prepared_statements(tenant_a):
    # A prepared statement over a DuckLake call (the catalog written out, a parameter elsewhere).
    prepared = tenant_a.prepare(sql=f"SELECT table_name FROM ducklake_table_info('{A}') WHERE table_name = ?")
    response = tenant_a.execute_prepared(prepared_id=prepared["prepared_id"], parameters=["mine"])
    assert tenant_a.table_of(response=response).column("table_name").to_pylist() == ["mine"]
    # SQL's PREPARE / EXECUTE: EXECUTE runs what PREPARE checked.
    tenant_a.execute(sql=f"PREPARE lake_snapshots AS SELECT * FROM ducklake_snapshots('{A}')")
    assert tenant_a.query(sql="EXECUTE lake_snapshots").num_rows > 0
    refused(session=tenant_a, sql=f"PREPARE other_snapshots AS SELECT * FROM ducklake_snapshots('{B}')", why=READ_B)


def test_ducklake_calls_in_views_and_macros_are_refused(world, tenant_a, request):
    admin = world["server"].admin()
    wrapped = "a view or macro cannot wrap a DuckLake function"
    unchecked = "it is called where access control cannot check its catalog before it runs"
    # A view of the token's own would run the call with the access of whoever reads it.
    refused(session=tenant_a, sql=f"CREATE VIEW {A}.main.v_lake AS SELECT * FROM ducklake_snapshots('{A}')",
            why=f"Hardcharger blocked ducklake_snapshots() in a view: {wrapped}")
    refused(session=tenant_a, sql=f"CREATE VIEW {A}.main.v_lake AS SELECT * FROM {A}.snapshots()",
            why=f"Hardcharger blocked ducklake_snapshots() in a view: {wrapped}")
    # Positive twin: a view over the catalog's tables.
    tenant_a.execute(sql=f"CREATE OR REPLACE VIEW {A}.main.v_mine AS SELECT * FROM {A}.main.mine")
    assert tenant_a.query(sql=f"SELECT count(*) AS n FROM {A}.main.v_mine").column("n").to_pylist() == [100]
    tenant_a.execute(sql=f"DROP VIEW {A}.main.v_mine")
    # Views and macros an administrator made: refused too, on the token's own catalog as well.
    admin.execute(sql=f"CREATE OR REPLACE VIEW {A}.main.admin_lake_view AS SELECT * FROM ducklake_snapshots('{A}')")
    admin.execute(sql=f"CREATE OR REPLACE MACRO {A}.main.admin_lake_macro() AS TABLE SELECT * FROM ducklake_snapshots('{A}')")
    admin.execute(sql=f"CREATE OR REPLACE MACRO {A}.main.admin_lake_count() AS "
                      f"(SELECT count(*) FROM ducklake_snapshots('{A}'))")
    admin.execute(sql=f"CREATE OR REPLACE MACRO {A}.main.admin_other_macro() AS TABLE SELECT * FROM ducklake_snapshots('{B}')")
    refused(session=tenant_a, sql=f"SELECT * FROM {A}.main.admin_lake_view",
            why=f"Hardcharger blocked ducklake_snapshots() in a view: {wrapped}")
    refused(session=tenant_a, sql=f"SELECT * FROM {A}.main.admin_lake_macro()", why=unchecked)
    refused(session=tenant_a, sql=f"SELECT {A}.main.admin_lake_count()", why=unchecked)
    refused(session=tenant_a, sql=f"SELECT * FROM {A}.main.admin_other_macro()", why=READ_B)
    # ... while the administrator's own use of them works (their positive twin).
    assert admin.query(sql=f"SELECT count(*) AS n FROM {A}.main.admin_lake_view").column("n").to_pylist()[0] > 0
    assert admin.query(sql=f"SELECT count(*) AS n FROM {A}.main.admin_lake_macro()").column("n").to_pylist()[0] > 0
    # query() runs SQL from a string: refused for every non-administrator, whatever it calls.
    refused(session=tenant_a, sql=f"SELECT * FROM query('SELECT * FROM ducklake_snapshots(''{A}'')')", why="query()")
    refused(session=tenant_a, sql=f"CALL query('SELECT * FROM ducklake_snapshots(''{A}'')')", why="query()")
    # A statement form the gate's walk does not read: refused (SET VARIABLE's query).
    refused(session=tenant_a, sql=f"SET VARIABLE n = (SELECT count(*) FROM ducklake_snapshots('{A}'))", why=unchecked)
    refused(session=tenant_a, sql=f"SET VARIABLE n = (SELECT count(*) FROM ducklake_snapshots('{B}'))", why=unchecked)
    # CALL is held to the same check as FROM: positive on A, refused on B.
    assert tenant_a.query(sql=f"CALL ducklake_snapshots('{A}')").num_rows > 0
    refused(session=tenant_a, sql=f"CALL ducklake_snapshots('{B}')", why=READ_B)
    # A maintenance call in a macro, with files to compact: DuckLake plans it as its own
    # operator, which runs only for a call the statement makes itself. Nothing is compacted.
    suffix = request.node.callspec.id.replace("-", "_")
    fragmented(admin=admin, name=f"wrapped_{suffix}", deleted=False)
    admin.execute(sql=f"CREATE OR REPLACE MACRO {A}.main.admin_merge() AS TABLE "
                      f"SELECT * FROM ducklake_merge_adjacent_files('{A}')")
    refused(session=tenant_a, sql=f"SELECT * FROM {A}.main.admin_merge()",
            why=f"Hardcharger blocked DuckLake's rewrite of files of catalog {A}: it runs only for a "
                f"ducklake_merge_adjacent_files(), ducklake_rewrite_data_files() or ducklake_flush_inlined_data() call "
                f"that the statement makes itself")
    assert files_of(admin=admin, name=f"wrapped_{suffix}") == 4
    admin.execute(sql=f"DROP MACRO TABLE {A}.main.admin_merge")
    admin.execute(sql=f"DROP TABLE {A}.main.wrapped_{suffix}")
    for name in ("admin_lake_macro", "admin_lake_count", "admin_other_macro"):
        admin.execute(sql=f"DROP MACRO {'TABLE ' if name != 'admin_lake_count' else ''}{A}.main.{name}")
    admin.execute(sql=f"DROP VIEW {A}.main.admin_lake_view")


@pytest.fixture(params=["user", "admin"])
def tenant_a_at_home(request, world):
    """Tenant A's session that may also read the server's own database (`tenants`)."""
    return world["server"].as_role(role=request.param, subject=f"app_a_home_{request.param}",
                                   catalog_access=[{"catalog": A, "access": "write"},
                                                   {"catalog": "tenants", "access": "read"},
                                                   {"catalog": "*", "access": "none"}])


def test_settings_is_a_ducklake_call_only_on_a_ducklake_catalog(world, tenant_a_at_home):
    admin = world["server"].admin()
    admin.execute(sql="CREATE SCHEMA IF NOT EXISTS tenants.tools")
    admin.execute(sql="CREATE OR REPLACE MACRO tenants.tools.settings() AS TABLE SELECT 42 AS answer")
    admin.execute(sql="CREATE OR REPLACE MACRO tenants.tools.snapshots() AS TABLE SELECT 43 AS answer")
    # A user's macro in a schema called tools: not DuckLake's, and not refused as one.
    assert tenant_a_at_home.query(sql="SELECT answer FROM tenants.tools.settings()").column(0).to_pylist() == [42]
    assert tenant_a_at_home.query(sql="SELECT answer FROM tools.snapshots()").column(0).to_pylist() == [43]
    # DuckLake's, on the DuckLake catalog: checked as ducklake_settings('tenant_a').
    assert tenant_a_at_home.query(sql=f"SELECT * FROM {A}.settings()").num_rows > 0
    refused(session=tenant_a_at_home, sql=f"SELECT * FROM {B}.settings()", why=READ_B)
    # tenants is not a DuckLake catalog: DuckLake says so, after the rules let the call through.
    refused(session=tenant_a_at_home, sql="SELECT * FROM ducklake_snapshots('tenants')",
            why='Attached database "tenants" does not refer to a DuckLake database')
    refused(session=tenant_a_at_home, sql="CALL ducklake_merge_adjacent_files('tenants')",
            why="You do not have write access to catalog 'tenants'")


def test_finding_4_no_outbound_http(tenant_a):
    for sql in ["SELECT * FROM read_csv('https://example.invalid/')", "FROM 'https://example.invalid/x.csv'",
                "SELECT * FROM read_text('http://127.0.0.1:9/')", "SELECT * FROM read_json('hf://datasets/x/y/z.json')"]:
        refused(session=tenant_a, sql=sql, why="READ HTTP")


def test_no_read_reaches_another_catalogs_files(tenant_a, world):
    s3, b_file = world["s3"], world["b_file"]
    for sql in [f"SELECT * FROM read_parquet('{b_file}')", f"SELECT * FROM glob('{s3}/{B}/**')",
                f"SELECT * FROM read_parquet('{s3}/{B}/main/*/*.parquet')", f"SELECT * FROM parquet_metadata('{b_file}')"]:
        refused(session=tenant_a, sql=sql, why=f"reaches into the storage of DuckLake catalog {B}")
