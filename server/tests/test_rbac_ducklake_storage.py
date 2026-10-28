"""DuckLake's storage is DuckLake's: no principal but the administrator reads, writes, lists or
globs anything in an attached DuckLake catalog's data path, whatever storage privileges it
holds. Statements on the catalog's tables still read and write there (DuckLake does), under
the tables' grants. A file planted there could be registered as a table's rows; a listing
shows another catalog's files. Both lakes here: one on local disk, one on the S3 gateway
with its metadata in PostgreSQL (docker-compose.yml)."""

import secrets

import pytest

from rbac import expect_allowed, expect_blocked, start_rbac_server
from services import PG_PORT, S3_PORT, pg_conninfo, require

REFUSED = "only DuckLake itself reads and writes"

# PostgreSQL and the S3 gateway run in Docker, on the Linux runners only.
pytestmark = pytest.mark.services


@pytest.fixture(scope="module")
def world(module_launcher, store, tmp_path_factory):
    require(port=PG_PORT, what="PostgreSQL")
    require(port=S3_PORT, what="the S3 gateway")
    work = tmp_path_factory.mktemp("lakes")
    s3 = f"s3://ducklake/lakes-{secrets.token_hex(nbytes=4)}"
    schema = f"lake_meta_{secrets.token_hex(nbytes=4)}"
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               args=["--database", str(work / "lakes.duckdb")],
                               env={"HARDCHARGER_INIT_SQL_COMMANDS": "INSTALL httpfs; LOAD httpfs; INSTALL ducklake; "
                                                                     "LOAD ducklake; INSTALL postgres; LOAD postgres;"})
    admin = server.admin()
    (work / "lakes" / "local").mkdir(parents=True)
    (work / "lakes" / "beside.csv").write_text("x\n1\n")
    for sql in [
        f"CREATE SECRET s3_lakes (TYPE s3, KEY_ID 'minioadmin', SECRET 'minioadmin', ENDPOINT '127.0.0.1:{S3_PORT}', "
        "USE_SSL false, URL_STYLE 'path', REGION 'us-east-1')",
        f"ATTACH 'ducklake:{work}/lakes/local.ducklake' AS lake_local (DATA_PATH '{work}/lakes/local/')",
        f"ATTACH 'ducklake:postgres:{pg_conninfo()}' AS lake_s3 (DATA_PATH '{s3}/lake/', METADATA_SCHEMA '{schema}')",
        # More rows than DuckLake inlines, so each table has a data file.
        "CREATE TABLE lake_local.main.t AS SELECT range AS i FROM range(100)",
        "CREATE TABLE lake_s3.main.t AS SELECT range AS i FROM range(100)",
        f"COPY (SELECT 1 AS x) TO '{s3}/beside/b.csv'",
        "CREATE ROLE eng", "CREATE USER erin", "GRANT eng TO erin",
        "GRANT READ FILESYSTEM, WRITE FILESYSTEM, READ OBJECT STORAGE, WRITE OBJECT STORAGE TO eng",
        "GRANT ALL ON CATALOG lake_local TO eng", "GRANT ALL ON CATALOG lake_s3 TO eng",
    ]:
        expect_allowed(client=admin, sql=sql)
    files = {lake: admin.run(sql=f"SELECT data_file FROM ducklake_list_files('{lake}', 't')").column(0).to_pylist()[0]
             for lake in ("lake_local", "lake_s3")}
    yield {"server": server, "admin": admin, "erin": server.user(name="erin"), "work": work, "s3": s3, "files": files}
    expect_allowed(client=admin, sql="DETACH lake_s3")


def test_the_tables_still_read_and_write_through_ducklake(world):
    erin = world["erin"]
    for lake in ("lake_local", "lake_s3"):
        expect_allowed(client=erin, sql=f"INSERT INTO {lake}.main.t SELECT range FROM range(100, 120)")
        assert erin.run(sql=f"SELECT count(*) AS n FROM {lake}.main.t").column("n").to_pylist()[0] >= 120
        expect_allowed(client=erin, sql=f"CREATE TABLE {lake}.main.mine AS SELECT range AS i FROM range(50)")
        expect_allowed(client=erin, sql=f"DROP TABLE {lake}.main.mine")


def test_no_user_reads_a_lake_file_directly(world):
    erin, work, s3, files = world["erin"], world["work"], world["s3"], world["files"]
    for sql in [
        f"SELECT * FROM read_parquet('{files['lake_local']}')",
        f"SELECT * FROM read_parquet('{files['lake_s3']}')",
        f"SELECT * FROM read_parquet('{files['lake_s3'].replace('s3://', 's3a://')}')",  # an alias
        f"SELECT * FROM parquet_metadata('{files['lake_s3']}')",
        f"SELECT * FROM '{files['lake_local']}'",
        # A listing, from inside or from above.
        f"SELECT * FROM glob('{work}/lakes/local/**')",
        f"SELECT * FROM glob('{work}/lakes/**')",
        f"SELECT * FROM glob('{s3}/**')",
        f"SELECT * FROM read_parquet('{s3}/lake/main/*/*.parquet')",
    ]:
        expect_blocked(client=erin, sql=sql, why=REFUSED)
    # What lies beside a lake stays the privileges' business.
    expect_allowed(client=erin, sql=f"SELECT * FROM read_csv('{work}/lakes/beside.csv')")
    expect_allowed(client=erin, sql=f"SELECT * FROM read_csv('{s3}/beside/b.csv')")


def test_no_user_writes_into_a_lake(world):
    erin, work, s3 = world["erin"], world["work"], world["s3"]
    for sql in [
        f"COPY (SELECT 1 AS i) TO '{work}/lakes/local/main/t/planted.parquet' (FORMAT parquet)",
        f"COPY (SELECT 1 AS i) TO '{s3}/lake/main/t/planted.parquet' (FORMAT parquet)",
        # A directory of files written above a lake could land in it, or overwrite it.
        f"COPY (SELECT 1 AS i, 'local' AS p) TO '{work}/lakes' (FORMAT parquet, PARTITION_BY (p), OVERWRITE_OR_IGNORE)",
        f"COPY (SELECT 1 AS i, 'x' AS p) TO '{s3}' (FORMAT parquet, PARTITION_BY (p))",
    ]:
        expect_blocked(client=erin, sql=sql, why=REFUSED)
    expect_allowed(client=erin, sql=f"COPY (SELECT 1 AS i) TO '{work}/lakes/out.csv'")
    expect_allowed(client=erin, sql=f"COPY (SELECT 1 AS i) TO '{s3}/beside/out.csv'")


def test_the_administrator_still_reaches_lake_files(world):
    admin, files = world["admin"], world["files"]
    assert admin.run(sql=f"SELECT count(*) AS n FROM read_parquet('{files['lake_s3']}')").column("n").to_pylist()[0] > 0
