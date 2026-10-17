"""Port of GizmoSQL 1.x tests/integration/test_ducklake.cpp (2 cases).

DuckLake on PostgreSQL metadata (server/tests/docker-compose.yml): INSTALL/LOAD ducklake
and postgres, a postgres secret and a DUCKLAKE secret with METADATA_PARAMETERS, ATTACH
'ducklake:ducklake_secret', USE, DDL, DML and queries; then 10 concurrent sessions each
do the setup and read, and instrumentation recorded the session's DuckLake statements
and their executions. 1.x skipped without PostgreSQL; these fail.
"""

import concurrent.futures
import time
import uuid

import pytest

from services import PG_DATABASE, PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, pg_conninfo, require

# Needs server/tests/docker-compose.yml; CI starts it where Docker runs (Linux).
pytestmark = pytest.mark.services



@pytest.fixture(scope="module")
def server(module_launcher):
    require(port=PG_PORT, what="PostgreSQL")
    return module_launcher.start(features=["instrumentation"], args=["--enable-instrumentation"])


def setup(session, data_path: str, metadata_schema: str, alias: str) -> None:
    for sql in ["INSTALL ducklake", "INSTALL postgres", "LOAD ducklake", "LOAD postgres"]:
        session.execute(sql=sql)
    session.execute(sql=f"""CREATE SECRET IF NOT EXISTS pg_secret (TYPE postgres, HOST '{PG_HOST}', PORT {PG_PORT},
                            DATABASE '{PG_DATABASE}', USER '{PG_USER}', PASSWORD '{PG_PASSWORD}')""")
    session.execute(sql=f"""CREATE SECRET IF NOT EXISTS {alias}_secret (TYPE DUCKLAKE, METADATA_PATH '',
                            DATA_PATH '{data_path}', METADATA_SCHEMA '{metadata_schema}',
                            METADATA_PARAMETERS MAP {{'TYPE': 'postgres', 'SECRET': 'pg_secret'}})""")
    # Secrets and catalogs belong to the instance: the first session attaches, the others find it.
    session.execute(sql=f"ATTACH IF NOT EXISTS 'ducklake:{alias}_secret' AS {alias}")
    session.execute(sql=f"USE {alias}")


def fresh_schema(session) -> str:
    schema = f"ducklake_{uuid.uuid4().hex[:8]}"
    session.execute(sql="INSTALL postgres; LOAD postgres;")
    session.execute(sql=f"ATTACH '{pg_conninfo()}' AS pg_cleanup (TYPE postgres)")
    session.execute(sql=f"CALL postgres_execute('pg_cleanup', 'CREATE SCHEMA {schema}')")
    session.execute(sql="DETACH pg_cleanup")
    return schema


def test_ducklake_setup_and_query(server, tmp_path):
    session = server.admin()
    setup(session=session, data_path=f"{tmp_path}/lake/", metadata_schema=fresh_schema(session=session),
          alias="my_ducklake")
    session.execute(sql="CREATE TABLE test_cities (name VARCHAR, country VARCHAR, population INTEGER)")
    session.execute(sql="INSERT INTO test_cities VALUES ('Tokyo', 'Japan', 37400068), ('Delhi', 'India', 28514000), "
                        "('Shanghai', 'China', 25582000), ('Sao Paulo', 'Brazil', 21650000), "
                        "('Mexico City', 'Mexico', 21581000)")
    rows = session.rows(sql="SELECT * FROM test_cities ORDER BY population DESC")
    assert len(rows) == 5 and rows[0]["name"] == "Tokyo"
    aggregate = session.rows(sql="SELECT count(*) AS n, sum(population) AS total FROM test_cities")
    assert aggregate == [{"n": 5, "total": 37400068 + 28514000 + 25582000 + 21650000 + 21581000}]
    assert session.scalar(sql="SELECT current_database()") == "my_ducklake"
    # Instrumentation recorded them: CREATE, INSERT and both SELECTs, each executed and
    # succeeded (the instrumentation catalog is named in full: the session uses the lake).
    sid = session.scalar(sql="SELECT GIZMOSQL_CURRENT_SESSION()")
    query = (f"SELECT count(DISTINCT s.statement_id) AS statements, count(e.execution_id) AS executions, "
             f"count(*) FILTER (WHERE e.status = 'success') AS succeeded "
             f"FROM _gizmosql_instr.main.sql_statements s JOIN _gizmosql_instr.main.sql_executions e USING (statement_id) "
             f"WHERE s.session_id = '{sid}' AND s.sql_text LIKE '%test' || '_cities%' AND NOT s.is_internal "
             f"AND e.cursor_close_time IS NOT NULL")
    deadline = time.monotonic() + 10
    while (counts := session.rows(sql=query)[0])["executions"] < 4 and time.monotonic() < deadline:
        time.sleep(0.1)
    assert counts == {"statements": 4, "executions": 4, "succeeded": 4}, counts


def test_ducklake_concurrent_queries(server, tmp_path):
    first = server.admin()
    schema = fresh_schema(session=first)
    data_path = f"{tmp_path}/concurrent/"
    setup(session=first, data_path=data_path, metadata_schema=schema, alias="concurrent_lake")
    first.execute(sql="CREATE TABLE concurrent_test AS SELECT range AS id FROM range(100)")

    def client(_):
        session = server.admin()
        setup(session=session, data_path=data_path, metadata_schema=schema, alias="concurrent_lake")
        return session.scalar(sql="SELECT count(*) FROM concurrent_test")

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        counts = list(pool.map(client, range(10)))
    assert counts == [100] * 10
