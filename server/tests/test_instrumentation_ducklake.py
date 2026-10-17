"""Port of GizmoSQL 1.x tests/integration/test_instrumentation_ducklake.cpp (7 cases).

Instrumentation in a catalog the init SQL attaches (--instrumentation-catalog,
GIZMOSQL_INSTRUMENTATION_CATALOG): DuckLake on PostgreSQL metadata with data on S3
(server/tests/docker-compose.yml), or a plain PostgreSQL catalog. Several servers share
one catalog; a restart reuses the schema; the catalog is read-only even for an admin;
a legacy schema with naive timestamps is migrated. 1.x skipped without PostgreSQL or
MinIO; these fail.
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
    return launcher.start(features=["instrumentation"],
                          env={"INIT_SQL_COMMANDS": lake_init_sql(catalog=catalog, metadata_schema=schema),
                               "GIZMOSQL_ENABLE_INSTRUMENTATION": "true", "GIZMOSQL_INSTRUMENTATION_CATALOG": catalog})


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
    assert admin.scalar(sql="SELECT GIZMOSQL_INSTRUMENTATION_CATALOG()") == catalog
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


def test_auto_migrate_legacy_naive_timestamp_schema(launcher, lake):
    catalog, schema = lake
    maker = launcher.start(env={"INIT_SQL_COMMANDS": lake_init_sql(catalog=catalog, metadata_schema=schema)}).admin()
    maker.execute(sql=f"CREATE TABLE {catalog}.main.instances (instance_id UUID NOT NULL, gizmosql_version VARCHAR NOT NULL, "
                      "gizmosql_edition VARCHAR NOT NULL, duckdb_version VARCHAR NOT NULL, arrow_version VARCHAR NOT NULL, "
                      "hostname VARCHAR, hostname_arg VARCHAR, server_ip VARCHAR, port INTEGER, database_path VARCHAR, "
                      "tls_enabled BOOLEAN NOT NULL, tls_cert_path VARCHAR, tls_key_path VARCHAR, mtls_required BOOLEAN NOT NULL, "
                      "mtls_ca_cert_path VARCHAR, readonly BOOLEAN NOT NULL, os_platform VARCHAR, os_name VARCHAR, "
                      "os_version VARCHAR, cpu_arch VARCHAR, cpu_model VARCHAR, cpu_count INTEGER, memory_total_bytes BIGINT, "
                      "start_time TIMESTAMP NOT NULL, stop_time TIMESTAMP, status VARCHAR NOT NULL, stop_reason VARCHAR)")
    maker.execute(sql=f"INSERT INTO {catalog}.main.instances (instance_id, gizmosql_version, gizmosql_edition, "
                      "duckdb_version, arrow_version, tls_enabled, mtls_required, readonly, start_time, status) VALUES "
                      "('00000000-0000-0000-0000-000000000002', 'v1', 'Enterprise', 'v1', '20', false, false, false, "
                      "TIMESTAMP '2025-01-01 10:00:00', 'stopped')")
    maker.client.close()
    launcher.stop_all()
    admin = start(launcher, catalog, schema).admin()
    assert "WITH TIME ZONE" in admin.scalar(sql=f"SELECT data_type FROM information_schema.columns WHERE table_catalog = "
                                                f"'{catalog}' AND table_name = 'instances' AND column_name = 'start_time'")
    assert admin.scalar(sql=f"SELECT epoch(start_time) FROM {catalog}.main.instances "
                            "WHERE instance_id = '00000000-0000-0000-0000-000000000002'") == 1735725600.0


def test_postgres_schema_writes_and_cascade(launcher):
    schema = f"gizmosql_instr_{uuid.uuid4().hex[:6]}"
    reset_schema(launcher=launcher, schema=schema)
    init_sql = f"INSTALL postgres; LOAD postgres; ATTACH '{pg_conninfo()}' AS pg_instr_cat (TYPE postgres);"
    server = launcher.start(features=["instrumentation"],
                            env={"INIT_SQL_COMMANDS": init_sql, "GIZMOSQL_ENABLE_INSTRUMENTATION": "true",
                                 "GIZMOSQL_INSTRUMENTATION_CATALOG": "pg_instr_cat",
                                 "GIZMOSQL_INSTRUMENTATION_SCHEMA": schema})
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
    admin.query(sql=f"SELECT * FROM {p}execution_details LIMIT 1")
    admin.query(sql=f"SELECT * FROM {p}session_activity LIMIT 1")
    assert admin.scalar(sql=f"SELECT count(*) FROM {p}sessions") > 0
    admin.execute(sql=f"CALL postgres_execute('pgadmin', 'DELETE FROM {schema}.instances')")
    admin.execute(sql="CALL postgres_clear_cache()")
    assert admin.scalar(sql=f"SELECT count(*) FROM {p}sessions") == 0
    assert admin.scalar(sql=f"SELECT count(*) FROM {p}sql_executions") == 0
