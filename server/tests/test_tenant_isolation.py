"""Catalog isolation between tenants on one server, as GizmoData Cloud runs it: each app has
its own DuckLake catalog (metadata in PostgreSQL, data in object storage) and a token whose
catalog_access rules give it its own catalog only. A customer's security review of GizmoSQL
(2026-10-27) found four ways for one app's token to reach another's catalog there; this suite
replays each of them, and the paths around them, against both a `user` token and an
administrator's token with the same rules. Every one must be refused, while the token keeps
full read-write on its own catalog."""

import secrets

import pytest

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
    for function, sql in [("ducklake_list_files", f"SELECT * FROM ducklake_list_files('{B}', 'secret_b')"),
                          ("ducklake_snapshots", f"SELECT * FROM ducklake_snapshots('{B}')"),
                          ("ducklake_table_info", f"SELECT * FROM ducklake_table_info('{B}')"),
                          ("ducklake_expire_snapshots", f"CALL ducklake_expire_snapshots('{B}', older_than => now())"),
                          ("snapshots", f"SELECT * FROM {B}.snapshots()")]:
        refused(session=tenant_a, sql=sql, why=f"the table function {function}")


def test_finding_4_no_outbound_http(tenant_a):
    for sql in ["SELECT * FROM read_csv('https://example.invalid/')", "FROM 'https://example.invalid/x.csv'",
                "SELECT * FROM read_text('http://127.0.0.1:9/')", "SELECT * FROM read_json('hf://datasets/x/y/z.json')"]:
        refused(session=tenant_a, sql=sql, why="READ HTTP")


def test_no_read_reaches_another_catalogs_files(tenant_a, world):
    s3, b_file = world["s3"], world["b_file"]
    for sql in [f"SELECT * FROM read_parquet('{b_file}')", f"SELECT * FROM glob('{s3}/{B}/**')",
                f"SELECT * FROM read_parquet('{s3}/{B}/main/*/*.parquet')", f"SELECT * FROM parquet_metadata('{b_file}')"]:
        refused(session=tenant_a, sql=sql, why=f"reaches into the storage of DuckLake catalog {B}")


# ---- GizmoSQL 1.42.3: object storage for a catalog-scoped session ----
#
# Port of test_catalog_access.cpp's RemotePathScopeFixture. 1.42.3 limits a catalog-scoped session
# to object storage under the data paths of the DuckLake catalogs its token grants, its own data
# path included. Hardcharger is stricter by design: no principal but the administrator reads or
# writes a DuckLake catalog's data path directly (only DuckLake does, for statements on its
# tables), because a direct write can overwrite or inject data files behind DuckLake's snapshots,
# and a catalog_access token holds no storage privilege for anything else. Its own catalog's data
# stays reachable through the catalog.

@pytest.fixture
def reader_a(world):
    return world["server"].as_role(role="user", subject="app_a_reader",
                                   catalog_access=[{"catalog": A, "access": "read"}, {"catalog": "*", "access": "none"}])


def test_scoped_token_cannot_read_another_catalogs_storage(tenant_a, world):
    s3, b_file = world["s3"], world["b_file"]
    for sql, why in [
        (f"SELECT * FROM read_parquet('{b_file}')", f"reaches into the storage of DuckLake catalog {B}"),
        (f"SELECT * FROM glob('{s3}/{B}/*')", f"reaches into the storage of DuckLake catalog {B}"),
        (f"SELECT * FROM read_csv('{s3}/{B}/x.csv')", f"reaches into the storage of DuckLake catalog {B}"),
        (f"SELECT * FROM '{b_file}'", f"reaches into the storage of DuckLake catalog {B}"),
        (f"COPY (SELECT 1 AS a) TO '{s3}/{B}/x.parquet' (FORMAT parquet)", f"reaches into the storage of DuckLake catalog {B}"),
        # Indirect: the statement's text does not hold the path.
        (f"SELECT * FROM query('SELECT * FROM read_parquet(''{b_file}'')')", "Hardcharger blocked query()"),
        (f"SELECT * FROM read_parquet(concat('s3', '://{s3[5:]}/{B}/x.parquet'))", "Hardcharger blocked read_parquet()"),
        (f"SELECT * FROM read_parquet(chr(115) || '3://{s3[5:]}/{B}/x.parquet')", "Hardcharger blocked read_parquet()"),
        # Beside a data path on a name boundary, out of it through '..', a glob from above.
        (f"SELECT * FROM read_parquet('{s3}/{A}x/x.parquet')", "READ OBJECT STORAGE"),
        (f"SELECT * FROM read_parquet('{s3}/{A}/../{B}/x.parquet')", "'..'"),
        (f"SELECT * FROM glob('{s3}/*/main/*')", "reaches into the storage of DuckLake catalog"),
        # A macro hides the path from the statement that uses it: refused at its creation.
        (f"CREATE TEMP MACRO scope_m() AS TABLE SELECT * FROM read_parquet('{b_file}')",
         f"reaches into the storage of DuckLake catalog {B}"),
    ]:
        refused(session=tenant_a, sql=sql, why=why)


def test_own_catalogs_storage_is_reached_through_the_catalog_only(tenant_a, reader_a, world):
    # 1.42.3's ScopedTokenKeepsItsOwnCatalogsStorage and ReadAccessDoesNotAllowWriting...: stricter here.
    s3 = world["s3"]
    for session in (tenant_a, reader_a):
        for sql, why in [
            (f"SELECT * FROM glob('{s3}/{A}/**')", f"reaches into the storage of DuckLake catalog {A}"),
            (f"SELECT count(*) FROM read_parquet('{s3}/{A}/**/*.parquet')", f"reaches into the storage of DuckLake catalog {A}"),
            (f"COPY (SELECT 7 AS a) TO '{s3}/{A}/exports/e.parquet' (FORMAT parquet)",
             f"reaches into the storage of DuckLake catalog {A}"),
        ]:
            refused(session=session, sql=sql, why=why)
        # The twin: the catalog's data, through the catalog.
        assert session.scalar(sql=f"SELECT count(*) AS n FROM {A}.main.mine") >= 100
    refused(session=reader_a, sql=f"INSERT INTO {A}.main.mine VALUES (1)", why=f"write access to catalog '{A}'")


def test_unscoped_sessions_and_object_storage(world):
    s3, b_file = world["s3"], world["b_file"]
    # The administrator keeps object storage, the DuckLake data paths included.
    assert world["server"].admin().scalar(sql=f"SELECT count(*) FROM read_parquet('{b_file}')") == 100
    # 1.x's single-tenant non-admin token keeps object storage; Hardcharger's holds no storage
    # privilege (object storage is a store user's grant, READ / WRITE OBJECT STORAGE).
    plain = world["server"].as_role(role="user", subject="unscoped_user")
    refused(session=plain, sql=f"SELECT * FROM read_parquet('{s3}/elsewhere/x.parquet')", why="READ OBJECT STORAGE")
    refused(session=plain, sql=f"SELECT * FROM read_parquet('{b_file}')", why=f"reaches into the storage of DuckLake catalog {B}")
