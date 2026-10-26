"""Optional modules do real S3/PostgreSQL/DuckLake work as signed runtime extensions."""

import uuid

import pytest

from conftest import TSAN_EXTENSIONS

from services import PG_DATABASE, PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, S3_PORT, pg_conninfo, require

pytestmark = pytest.mark.services
OPTIONAL = ["httpfs", "ducklake", "postgres_scanner", "autocomplete", "tpch"]


@pytest.fixture(scope="module")
def server(module_launcher, tmp_path_factory):
    require(port=PG_PORT, what="PostgreSQL")
    require(port=S3_PORT, what="the S3 gateway")
    cache = tmp_path_factory.mktemp("loadable-extensions")
    init = f"SET extension_directory = '{cache}'; "
    init += " ".join(f"INSTALL {name}; LOAD {name};" for name in OPTIONAL)
    return module_launcher.start(env={"HARDCHARGER_INIT_SQL_COMMANDS": init}, startup_seconds=180)


def test_optional_extensions_are_loaded_from_the_repository(server):
    session = server.admin()
    assert session.scalar(sql="SELECT current_setting('allow_unsigned_extensions')") is False
    rows = session.rows(sql="SELECT extension_name, loaded, install_mode FROM duckdb_extensions()")
    by_name = {r["extension_name"]: r for r in rows}
    for name in OPTIONAL:
        assert by_name[name]["loaded"], by_name[name]
        assert (by_name[name]["install_mode"] == "STATICALLY_LINKED") is TSAN_EXTENSIONS, by_name[name]


def test_httpfs_reads_and_writes_s3(server):
    session = server.admin()
    session.execute(sql=f"CREATE OR REPLACE SECRET s3_builtin (TYPE s3, KEY_ID 'minioadmin', SECRET 'minioadmin', "
                        f"ENDPOINT '127.0.0.1:{S3_PORT}', USE_SSL false, URL_STYLE 'path', REGION 'us-east-1')")
    url = f"s3://ducklake/builtin_{uuid.uuid4().hex[:8]}/answer.parquet"
    session.execute(sql=f"COPY (SELECT 'from s3' AS v) TO '{url}' (FORMAT parquet)")
    assert session.scalar(sql=f"SELECT v FROM read_parquet('{url}')") == "from s3"


def test_postgres_scanner(server):
    session = server.admin()
    alias = f"pg_{uuid.uuid4().hex[:8]}"
    session.execute(sql=f"ATTACH '{pg_conninfo()}' AS {alias} (TYPE postgres, READ_ONLY)")
    assert session.scalar(sql=f"SELECT * FROM postgres_query('{alias}', 'SELECT current_database()')") == PG_DATABASE
    session.execute(sql=f"DETACH {alias}")


def test_ducklake_on_postgres_and_s3(server):
    session = server.admin()
    schema = f"builtin_{uuid.uuid4().hex[:8]}"
    session.execute(sql=f"ATTACH '{pg_conninfo()}' AS pg_setup (TYPE postgres)")
    session.execute(sql=f"CALL postgres_execute('pg_setup', 'CREATE SCHEMA {schema}')")
    session.execute(sql="DETACH pg_setup")
    session.execute(sql=f"CREATE OR REPLACE SECRET s3_lake (TYPE s3, KEY_ID 'minioadmin', SECRET 'minioadmin', "
                        f"ENDPOINT '127.0.0.1:{S3_PORT}', USE_SSL false, URL_STYLE 'path', REGION 'us-east-1')")
    session.execute(sql=f"""CREATE OR REPLACE SECRET pg_lake (TYPE postgres, HOST '{PG_HOST}', PORT {PG_PORT},
                            DATABASE '{PG_DATABASE}', USER '{PG_USER}', PASSWORD '{PG_PASSWORD}')""")
    session.execute(sql=f"""CREATE OR REPLACE SECRET lake_secret (TYPE DUCKLAKE, METADATA_PATH '',
                            DATA_PATH 's3://ducklake/{schema}/', METADATA_SCHEMA '{schema}',
                            METADATA_PARAMETERS MAP {{'TYPE': 'postgres', 'SECRET': 'pg_lake'}})""")
    session.execute(sql="ATTACH 'ducklake:lake_secret' AS builtin_lake")
    session.execute(sql="CREATE TABLE builtin_lake.cities AS SELECT * FROM (VALUES ('Tokyo'), ('Delhi')) t(name)")
    assert session.scalar(sql="SELECT count(*) FROM builtin_lake.cities") == 2
    session.execute(sql="DETACH builtin_lake")


def test_autocomplete_and_tpch(server):
    session = server.admin()
    suggestions = session.rows(sql="SELECT suggestion FROM sql_auto_complete('SELE')")
    assert any(r["suggestion"].strip().upper() == "SELECT" for r in suggestions), suggestions
    session.execute(sql="CALL dbgen(sf = 0.01)")
    assert session.scalar(sql="SELECT count(*) FROM nation") == 25
