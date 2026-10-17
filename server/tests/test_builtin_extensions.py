"""The DuckDB extensions built into the server (server/scripts/duckdb-extensions.cmake)
work with no extension repository: every feature the suite uses one for runs on a server
whose extension repositories are unreachable and whose extension directory is empty, so
nothing can come from DuckDB's repository or from this machine's extension cache.

Each built-in extension is loaded at startup and statically linked; parquet, json, icu,
httpfs (S3 on the test gateway), the postgres scanner (the test PostgreSQL), DuckLake on
both, autocomplete and tpch each do real work. An extension that is not built in can't be
installed there, which proves the repository really is out of reach, and the server never
installs or loads one by itself. (DuckDB's INSTALL always downloads, even an extension
that is built in; offline, LOAD alone is what works.) Needs server/tests/docker-compose.yml.
"""

import uuid

import pytest

from services import PG_DATABASE, PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, S3_PORT, pg_conninfo, require

pytestmark = pytest.mark.services

BUILT_IN = ["core_functions", "parquet", "json", "icu", "httpfs", "ducklake", "postgres_scanner", "autocomplete",
            "tpch"]
# Port 9 (discard) on the loopback: nothing answers, so a download fails at once.
NOWHERE = "http://127.0.0.1:9/no-extensions"


@pytest.fixture(scope="module")
def server(module_launcher, tmp_path_factory):
    require(port=PG_PORT, what="PostgreSQL")
    require(port=S3_PORT, what="the S3 gateway")
    empty = tmp_path_factory.mktemp("no-extensions")
    init = (f"SET GLOBAL custom_extension_repository = '{NOWHERE}'; "
            f"SET GLOBAL autoinstall_extension_repository = '{NOWHERE}'; "
            f"SET GLOBAL extension_directory = '{empty}';")
    return module_launcher.start(env={"HARDCHARGER_INIT_SQL_COMMANDS": init})


def test_repositories_are_out_of_reach_and_nothing_autoinstalls(server):
    session = server.admin()
    settings = session.rows(sql="SELECT name, value FROM duckdb_settings() WHERE name IN "
                                "('custom_extension_repository', 'autoinstall_extension_repository', "
                                "'autoinstall_known_extensions', 'autoload_known_extensions')")
    assert {r["name"]: r["value"] for r in settings} == {
        "custom_extension_repository": NOWHERE, "autoinstall_extension_repository": NOWHERE,
        "autoinstall_known_extensions": "false", "autoload_known_extensions": "false"}
    # An extension that isn't built in can't come from anywhere: the download itself fails.
    detail = session.error(sql="INSTALL spatial")["detail"]
    assert "127.0.0.1:9" in detail, detail


def test_every_built_in_extension_is_linked_and_loaded(server):
    rows = server.admin().rows(sql="SELECT extension_name, loaded, install_mode FROM duckdb_extensions()")
    by_name = {r["extension_name"]: r for r in rows}
    for name in BUILT_IN:
        assert by_name[name]["loaded"] and by_name[name]["install_mode"] == "STATICALLY_LINKED", by_name.get(name)


def test_load_of_a_built_in_extension_needs_no_repository(server):
    session = server.admin()
    for name in ["httpfs", "ducklake", "postgres", "tpch", "parquet", "json", "icu", "autocomplete"]:
        session.execute(sql=f"LOAD {name}")
    # INSTALL is DuckDB's download, built in or not: offline, it fails (and isn't needed).
    detail = session.error(sql="INSTALL httpfs")["detail"]
    assert "127.0.0.1:9" in detail, detail


def test_parquet_json_and_icu(server, tmp_path):
    session = server.admin()
    path = tmp_path / "answer.parquet"
    session.execute(sql=f"COPY (SELECT 42 AS answer) TO '{path}' (FORMAT parquet)")
    assert session.scalar(sql=f"SELECT answer FROM read_parquet('{path}')") == 42
    assert session.scalar(sql="""SELECT json_extract_string('{"a": {"b": "json"}}', '$.a.b')""") == "json"
    assert session.scalar(sql="SELECT (TIMESTAMPTZ '2026-01-01 12:00:00+00' AT TIME ZONE 'Asia/Tokyo')::VARCHAR") \
        == "2026-01-01 21:00:00"


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
