"""Port of GizmoSQL 1.x tests/integration/test_ducklake_catalog_fanout.cpp (2 cases).

With 10 DuckLake catalogs on PostgreSQL metadata attached, working with one of them
must not open PostgreSQL connections to the other nine: USE, the catalogs, db-schemas
and tables metadata requests (Chargewire §10; 1.x: Flight SQL GetCatalogs, GetDbSchemas,
GetTables with and without schemas) and a qualified SELECT each open at most 3 new
backends (pg_stat_database.sessions), once with local data and once with data on S3.
Needs server/tests/docker-compose.yml (PostgreSQL and an S3 gateway); fails without it.

Each step is its own test here, for both data locations. USE, the catalogs list and a
qualified SELECT stay within bounds (the catalogs list now comes from duckdb_databases(),
1.x's fix, in the SDK). Listing the schemas or tables of one catalog still opens all of
them: those three steps run as strict expected failures (see LISTING_GAP).
"""

import secrets
import time

import psycopg
import pytest

from services import PG_PORT, S3_PORT, pg_conninfo, require

# Needs server/tests/docker-compose.yml; CI starts it where Docker runs (Linux).
pytestmark = pytest.mark.services


CATALOGS = 10
MAX_SESSIONS_PER_STEP = 3


def sessions_established(session) -> int:
    return session.scalar(sql="SELECT sessions FROM postgres_query('pgmon', "
                              "'SELECT sessions FROM pg_stat_database WHERE datname = current_database()')")


def live_backends(session) -> int:
    return session.scalar(sql="SELECT n FROM postgres_query('pgmon', 'SELECT count(*) - 1 AS n FROM pg_stat_activity "
                              "WHERE datname = current_database() AND pid <> pg_backend_pid()')")


def drain(session) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if live_backends(session) <= 0:
            return
        time.sleep(0.25)
    pytest.fail(reason="PostgreSQL connection pools did not drain in time")


@pytest.fixture(scope="module", params=["local", "s3"])
def lake(request, module_launcher, tmp_path_factory):
    """A server with 10 DuckLake catalogs attached, data local or on S3, their metadata in
    a PostgreSQL database of their own: the steps count that database's sessions, which
    no other test (or pytest-xdist worker) touches."""
    require(port=PG_PORT, what="PostgreSQL")
    database = f"fanout_{request.param}_{secrets.token_hex(nbytes=4)}"
    with psycopg.connect(conninfo=pg_conninfo(), autocommit=True) as connection:
        connection.execute(query=f'CREATE DATABASE "{database}"')
    yield from attach_lakes(request=request, module_launcher=module_launcher, tmp_path_factory=tmp_path_factory,
                            database=database)
    with psycopg.connect(conninfo=pg_conninfo(), autocommit=True) as connection:
        connection.execute(query=f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


def attach_lakes(request, module_launcher, tmp_path_factory, database: str):
    session = module_launcher.start().admin()
    for sql in ["INSTALL ducklake", "INSTALL postgres", "LOAD ducklake", "LOAD postgres",
                "SET pg_pool_idle_timeout_millis = 500", "SET pg_pool_max_lifetime_millis = 500",
                "SET pg_pool_enable_reaper_thread = true"]:
        session.execute(sql=sql)
    if request.param == "s3":
        require(port=S3_PORT, what="the S3 gateway")
        session.execute(sql="INSTALL httpfs; LOAD httpfs;")
        session.execute(sql=f"CREATE OR REPLACE SECRET s3_fanout (TYPE s3, KEY_ID 'minioadmin', SECRET 'minioadmin', "
                            f"ENDPOINT '127.0.0.1:{S3_PORT}', USE_SSL false, URL_STYLE 'path', REGION 'us-east-1')")
        data_root = f"s3://ducklake/{database}/"
    else:
        data_root = f"{tmp_path_factory.mktemp('lake')}/"
    conninfo = pg_conninfo(dbname=database)
    session.execute(sql=f"ATTACH '{conninfo}' AS pgmon (TYPE postgres)")
    prefix = f"fanout_{request.param}"
    for i in range(CATALOGS):
        name = f"{prefix}_{i}"
        session.execute(sql=f"ATTACH 'ducklake:postgres:{conninfo}' AS {name} "
                            f"(DATA_PATH '{data_root}{name}/', METADATA_SCHEMA '{name}')")
        session.execute(sql=f"CREATE TABLE {name}.main.t0 AS SELECT {i} AS x")
    yield session, f"{prefix}_{CATALOGS // 2}"


def opened(session, step) -> None:
    drain(session)
    before = sessions_established(session)
    step()
    delta = sessions_established(session) - before - 1  # less this reading's own
    assert delta <= MAX_SESSIONS_PER_STEP, f"opened {delta} PostgreSQL sessions"


LISTING_GAP = ("the v2 C API lists schemas and tables only through SQL (duckdb_schemas(), information_schema), "
               "which opens every attached catalog; listing one needs DuckDB's catalog API (what 1.x used)")


def test_use_does_not_fan_out(lake):
    session, target = lake
    opened(session=session, step=lambda: session.execute(sql=f"USE {target}"))
    assert session.scalar(sql="SELECT current_catalog()") == target
    assert session.scalar(sql="SELECT x FROM t0") == CATALOGS // 2
    session.execute(sql="USE memory")


def test_catalogs_do_not_fan_out(lake):
    session, _ = lake
    opened(session=session, step=lambda: session.metadata(kind="catalogs"))
    assert len(session.metadata(kind="catalogs")) >= CATALOGS


@pytest.mark.xfail(strict=True, reason=LISTING_GAP)
def test_db_schemas_of_one_catalog_do_not_fan_out(lake):
    session, target = lake
    opened(session=session, step=lambda: session.metadata(kind="db-schemas", catalog=target))


@pytest.mark.xfail(strict=True, reason=LISTING_GAP)
def test_tables_of_one_catalog_do_not_fan_out(lake):
    session, target = lake
    opened(session=session, step=lambda: session.metadata(kind="tables", catalog=target))


@pytest.mark.xfail(strict=True, reason=LISTING_GAP)
def test_tables_with_schemas_of_one_catalog_do_not_fan_out(lake):
    session, target = lake
    opened(session=session, step=lambda: session.metadata(kind="tables", catalog=target, include_schema="true"))


def test_metadata_answers_are_right(lake):
    session, target = lake
    schemas = session.metadata(kind="db-schemas", catalog=target).to_pylist()
    assert schemas and all(r["catalog_name"] == target for r in schemas), schemas
    tables = session.metadata(kind="tables", catalog=target, db_schema_filter_pattern="main").to_pylist()
    assert [(r["table_name"], r["table_type"]) for r in tables] == [("t0", "BASE TABLE")], tables


def test_qualified_select_does_not_fan_out(lake):
    session, target = lake
    opened(session=session, step=lambda: session.scalar(sql=f"SELECT x FROM {target}.main.t0"))
