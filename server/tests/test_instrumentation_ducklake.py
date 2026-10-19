"""Port of GizmoSQL 1.x tests/integration/test_instrumentation_ducklake.cpp (7 cases).

Instrumentation in a catalog the init SQL attaches (--instrumentation-catalog,
HARDCHARGER_INSTRUMENTATION_CATALOG): DuckLake on PostgreSQL metadata with data on S3
(server/tests/docker-compose.yml), or a plain PostgreSQL catalog. Several servers share
one catalog; a restart reuses the schema; the catalog is read-only even for an admin;
a legacy schema with naive timestamps is migrated. 1.x skipped without PostgreSQL or
MinIO; these fail.

PostgresCatalogLogging.ForkLogsToPostgres drove 1.x's log sink directly with synthetic
lines; here a real server logs into the PostgreSQL catalog (--log-catalog,
--log-schema), the table and its indexes are checked in PostgreSQL itself, and the same
runs against DuckLake, which 1.x left to shared code.
"""

import time
import uuid

import pytest

from services import PG_DATABASE, PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, S3_PORT, pg_conninfo, require

# Needs server/tests/docker-compose.yml; CI starts it where Docker runs (Linux).
pytestmark = pytest.mark.services


@pytest.fixture(autouse=True)
def services():
    require(port=PG_PORT, what="PostgreSQL")
    require(port=S3_PORT, what="the S3 gateway")


def lake_init_sql(catalog: str, metadata_schema: str) -> str:
    return f"""
INSTALL ducklake; INSTALL postgres; INSTALL httpfs; LOAD ducklake; LOAD postgres; LOAD httpfs;
CREATE OR REPLACE SECRET s3_secret (TYPE s3, KEY_ID 'minioadmin', SECRET 'minioadmin',
  ENDPOINT '127.0.0.1:{S3_PORT}', USE_SSL false, URL_STYLE 'path', REGION 'us-east-1');
CREATE OR REPLACE SECRET pg_instr_secret (TYPE postgres, HOST '{PG_HOST}', PORT {PG_PORT},
  DATABASE '{PG_DATABASE}', USER '{PG_USER}', PASSWORD '{PG_PASSWORD}');
CREATE OR REPLACE SECRET ducklake_instr_secret (TYPE DUCKLAKE, METADATA_PATH '',
  DATA_PATH 's3://ducklake/{metadata_schema}/', METADATA_SCHEMA '{metadata_schema}',
  METADATA_PARAMETERS MAP {{'TYPE': 'postgres', 'SECRET': 'pg_instr_secret'}});
ATTACH 'ducklake:ducklake_instr_secret' AS {catalog} (DATA_INLINING_ROW_LIMIT 0);
"""


def reset_schema(launcher, schema: str) -> None:
    admin = launcher.start().admin()
    admin.execute(sql="INSTALL postgres; LOAD postgres;")
    admin.execute(sql=f"ATTACH '{pg_conninfo()}' AS pg_reset (TYPE postgres)")
    admin.execute(sql=f"CALL postgres_execute('pg_reset', 'DROP SCHEMA IF EXISTS {schema} CASCADE')")
    admin.execute(sql="DETACH pg_reset")


@pytest.fixture
def lake(launcher):
    catalog = "instr_lake"
    schema = f"instr_{uuid.uuid4().hex[:8]}"
    reset_schema(launcher=launcher, schema=schema)
    return catalog, schema


def start(launcher, catalog: str, schema: str):
    return launcher.start(env={"HARDCHARGER_INIT_SQL_COMMANDS": lake_init_sql(catalog=catalog, metadata_schema=schema),
                               "HARDCHARGER_ENABLE_INSTRUMENTATION": "true", "HARDCHARGER_INSTRUMENTATION_CATALOG": catalog})


def eventually(session, sql: str, predicate, timeout: float = 30):
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = session.scalar(sql=sql)
        if predicate(value):
            return value
        time.sleep(0.1)
    raise AssertionError(f"{sql} -> {value!r}")


def test_setup_and_query(launcher, lake):
    catalog, schema = lake
    admin = start(launcher, catalog, schema).admin()
    admin.scalar(sql="SELECT 1")
    admin.scalar(sql="SELECT 2")
    assert admin.scalar(sql="SELECT HARDCHARGER_INSTRUMENTATION_CATALOG()") == catalog
    eventually(admin, f"SELECT count(*) FROM {catalog}.main.instances", lambda n: n >= 1)
    eventually(admin, f"SELECT count(*) FROM {catalog}.main.sessions", lambda n: n >= 1)
    eventually(admin, f"SELECT count(*) FROM {catalog}.main.sql_statements", lambda n: n >= 2)


def test_multiple_instances_concurrent(launcher, lake):
    catalog, schema = lake
    servers = [start(launcher, catalog, schema) for _ in range(3)]
    for server in servers:
        session = server.admin()
        for i in range(3):
            session.scalar(sql=f"SELECT {i}")
    admin = servers[0].admin()
    eventually(admin, f"SELECT count(DISTINCT instance_id) FROM {catalog}.main.instances WHERE status = 'running'",
               lambda n: n == 3)
    eventually(admin, f"SELECT count(DISTINCT instance_id) FROM {catalog}.main.sessions", lambda n: n >= 3)
    eventually(admin, f"SELECT count(*) FROM {catalog}.main.sql_statements", lambda n: n >= 9)


def test_schema_validation(launcher, lake):
    catalog, schema = lake
    first = start(launcher, catalog, schema)
    assert first.admin().scalar(sql=f"SELECT count(*) FROM {catalog}.main.instances") >= 1
    first.stop()
    second = start(launcher, catalog, schema)
    assert eventually(second.admin(), f"SELECT count(*) FROM {catalog}.main.instances", lambda n: n >= 2) >= 2


def test_catalog_is_read_only(launcher, lake):
    catalog, schema = lake
    admin = start(launcher, catalog, schema).admin()
    assert admin.scalar(sql=f"SELECT count(*) FROM {catalog}.main.instances") >= 1
    for sql in [f"INSERT INTO {catalog}.main.instances (instance_id) VALUES (uuid())",
                f"UPDATE {catalog}.main.instances SET hostname = 'x'", f"DELETE FROM {catalog}.main.sessions WHERE 1=0",
                f"DROP TABLE {catalog}.main.sql_statements"]:
        detail = admin.error(sql=sql)["detail"]
        assert "read-only" in detail or "Access denied" in detail, (sql, detail)
    assert "Cannot DETACH" in admin.error(sql=f"DETACH {catalog}")["detail"]


def test_postgres_schema_writes_and_cascade(launcher):
    schema = f"hardcharger_instr_{uuid.uuid4().hex[:6]}"
    reset_schema(launcher=launcher, schema=schema)
    init_sql = f"INSTALL postgres; LOAD postgres; ATTACH '{pg_conninfo()}' AS pg_instr_cat (TYPE postgres);"
    server = launcher.start(env={"HARDCHARGER_INIT_SQL_COMMANDS": init_sql, "HARDCHARGER_ENABLE_INSTRUMENTATION": "true",
                                 "HARDCHARGER_INSTRUMENTATION_CATALOG": "pg_instr_cat",
                                 "HARDCHARGER_INSTRUMENTATION_SCHEMA": schema})
    session = server.admin()
    for i in range(3):
        assert session.scalar(sql=f"SELECT {i} AS c") == i
    session.close()
    server.stop()
    admin = launcher.start().admin()
    admin.execute(sql="INSTALL postgres; LOAD postgres;")
    admin.execute(sql=f"ATTACH '{pg_conninfo()}' AS pgadmin (TYPE postgres)")
    p = f"pgadmin.{schema}."
    assert admin.scalar(sql=f"SELECT count(*) FROM {p}instances") >= 1
    assert admin.scalar(sql=f"SELECT count(*) FROM {p}sql_executions WHERE status = 'executing'") == 0
    assert admin.scalar(sql=f"SELECT count(*) FROM {p}sql_executions WHERE status = 'success'") >= 3
    # Keyed by the Warp SQL ids clients hold: each plain statement's execution shares its q_... id.
    assert admin.scalar(sql=f"SELECT count(*) FROM {p}sql_executions WHERE execution_id = statement_id "
                            "AND starts_with(execution_id, 'q_') AND length(execution_id) = 34") >= 3
    assert admin.scalar(sql=f"SELECT count(*) FROM {p}sql_executions WHERE NOT starts_with(execution_id, 'q_')") == 0
    admin.query(sql=f"SELECT * FROM {p}execution_details LIMIT 1")
    admin.query(sql=f"SELECT * FROM {p}session_activity LIMIT 1")
    assert admin.scalar(sql=f"SELECT count(*) FROM {p}sessions") > 0
    admin.execute(sql=f"CALL postgres_execute('pgadmin', 'DELETE FROM {schema}.instances')")
    admin.execute(sql="CALL postgres_clear_cache()")
    assert admin.scalar(sql=f"SELECT count(*) FROM {p}sessions") == 0
    assert admin.scalar(sql=f"SELECT count(*) FROM {p}sql_executions") == 0


def check_forked_logs(admin, table: str, cluster_id: str) -> None:
    sid = admin.scalar(sql="SELECT HARDCHARGER_CURRENT_SESSION()")
    for i in range(3):
        assert admin.scalar(sql=f"SELECT {i} AS log_probe") == i
    eventually(admin, f"SELECT count(*) FROM {table} WHERE session_id = '{sid}' AND username = 'admin' "
                      # Spelled in two parts, so this query's own log line doesn't match.
                      "AND message = 'Client SQL command execution succeeded' AND fields LIKE '%log' || '_probe%'",
               lambda n: n == 3)
    # One statement, one snapshot: every query adds lines of its own.
    row = admin.rows(sql=f"SELECT count(*) AS total, "
                         f"count(*) FILTER (WHERE CAST(cluster_id AS VARCHAR) = '{cluster_id}') AS clustered, "
                         f"count(*) FILTER (WHERE log_time IS NOT NULL AND level IN ('DEBUG', 'INFO', 'WARN', 'ERROR')) "
                         f"AS well_formed FROM {table}")[0]
    assert row["total"] > 0 and row["clustered"] == row["total"] and row["well_formed"] == row["total"], row
    detail = admin.error(sql=f"DELETE FROM {table}")["detail"]
    assert "read-only" in detail and "log catalog" in detail, detail


def test_postgres_catalog_logging(launcher):
    schema = f"hardcharger_logs_{uuid.uuid4().hex[:6]}"
    cluster_id = "abcdef01-2345-6789-abcd-ef0123456789"
    reset_schema(launcher=launcher, schema=schema)
    init_sql = f"INSTALL postgres; LOAD postgres; ATTACH '{pg_conninfo()}' AS pg_log_cat (TYPE postgres);"
    server = launcher.start(args=["--enable-catalog-logging", "--log-catalog", "pg_log_cat", "--log-schema", schema,
                                  "--cluster-id", cluster_id],
                            env={"HARDCHARGER_INIT_SQL_COMMANDS": init_sql})
    check_forked_logs(admin=server.admin(), table=f"pg_log_cat.{schema}.logs", cluster_id=cluster_id)
    server.stop()
    # In PostgreSQL itself: the native table, its indexes, and every line written.
    admin = launcher.start().admin()
    admin.execute(sql="INSTALL postgres; LOAD postgres;")
    admin.execute(sql=f"ATTACH '{pg_conninfo()}' AS pgadmin (TYPE postgres)")
    indexes = admin.scalar(sql=f"SELECT count(*) FROM postgres_query('pgadmin', 'SELECT indexname FROM pg_indexes "
                               f"WHERE schemaname = ''{schema}'' AND tablename = ''logs''')")
    assert indexes == 8, indexes
    assert admin.scalar(sql=f"SELECT count(*) FROM pgadmin.{schema}.logs WHERE message = 'stopped'") == 1
    admin.execute(sql=f"CALL postgres_execute('pgadmin', 'DROP SCHEMA IF EXISTS {schema} CASCADE')")


def test_ducklake_catalog_logging(launcher, lake):
    catalog, schema = lake
    cluster_id = "abcdef01-2345-6789-abcd-ef0123456789"
    server = launcher.start(args=["--enable-catalog-logging", "--log-catalog", catalog, "--cluster-id", cluster_id],
                            env={"HARDCHARGER_INIT_SQL_COMMANDS": lake_init_sql(catalog=catalog, metadata_schema=schema)})
    admin = server.admin()
    assert admin.scalar(sql="SELECT value FROM hardcharger_settings() WHERE name = 'hardcharger.log_catalog'") == catalog
    check_forked_logs(admin=admin, table=f"{catalog}.main.logs", cluster_id=cluster_id)
