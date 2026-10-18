"""Access control on query plans, attacked over Warp SQL.

Every negative test asserts why the statement failed (Hardcharger's refusal and the
privilege it names), and has a positive twin showing the same user can do the
legitimate version."""

import base64
import time

import pyarrow as pa
import pyarrow.ipc  # noqa: F401 (pa.ipc)
import pytest

from rbac import (ARROW_STREAM, WarpError, arrow_bytes, expect_allowed, expect_blocked, expect_error,
                  start_rbac_server)

SECRET = "no SELECT privilege on table"


@pytest.fixture(scope="module")
def server(module_launcher, store, tmp_path_factory):
    database = tmp_path_factory.mktemp("db") / "rbac_enforce.duckdb"
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               args=["--database", str(database)])
    admin = server.admin()
    for sql in [
        "CREATE TABLE orders (id INT, amount INT, customer VARCHAR)",
        "INSERT INTO orders VALUES (1, 10, 'ann'), (2, 20, 'bob')",
        "CREATE TABLE customers (id INT, email VARCHAR, ssn VARCHAR)",
        "INSERT INTO customers VALUES (1, 'ann@x', '111'), (2, 'bob@x', '222')",
        "CREATE TABLE secrets (id INT, value VARCHAR)",
        "INSERT INTO secrets VALUES (1, 'top-secret-value')",
        "CREATE VIEW orders_over_secrets AS SELECT o.id, s.value FROM orders o JOIN secrets s USING (id)",
        "CREATE ROLE reader", "CREATE ROLE colreader", "CREATE ROLE writer", "CREATE ROLE builder",
        "GRANT SELECT ON orders TO reader",
        "GRANT SELECT (id) ON customers TO colreader",
        "GRANT INSERT, UPDATE, DELETE ON orders TO writer",
        "GRANT CREATE TABLE, CREATE VIEW ON SCHEMA main TO builder",
        "CREATE USER joe", "CREATE USER eve", "CREATE USER wanda", "CREATE USER bob",
        "GRANT reader, colreader TO joe", "GRANT writer TO wanda", "GRANT builder TO bob",
    ]:
        expect_allowed(client=admin, sql=sql)
    server.admin_client = admin
    return server


@pytest.fixture
def admin(server):
    return server.admin_client


@pytest.fixture
def joe(server):
    return server.user(name="joe")


@pytest.fixture
def eve(server):
    return server.user(name="eve")


@pytest.fixture
def wanda(server):
    return server.user(name="wanda")


@pytest.fixture
def bob(server):
    return server.user(name="bob")


# ---- Reads ----

def test_granted_reads_work(joe, admin):
    assert expect_allowed(client=joe, sql="SELECT * FROM orders ORDER BY id").num_rows == 2
    expect_allowed(client=joe, sql="SELECT id FROM customers")
    expect_allowed(client=joe, sql="SELECT count(*) FROM customers")  # any column grant allows count(*)
    expect_allowed(client=joe, sql="SELECT o.id, c.id FROM orders o JOIN customers c ON o.id = c.id")
    expect_allowed(client=joe, sql="SELECT * FROM range(3)")
    expect_allowed(client=joe, sql="SELECT table_name FROM information_schema.tables")
    expect_allowed(client=admin, sql="SELECT * FROM secrets")  # the administrator is unaffected


def test_ungranted_tables_are_denied(joe, eve):
    expect_blocked(client=eve, sql="SELECT * FROM orders", why=SECRET)
    expect_blocked(client=joe, sql="SELECT * FROM secrets", why=SECRET)
    expect_blocked(client=joe, sql='SELECT * FROM "SECRETS"', why="no SELECT")
    expect_blocked(client=joe, sql="SELECT * FROM main.secrets", why="no SELECT")
    expect_blocked(client=joe, sql="SELECT count(*) FROM secrets", why="no SELECT")


def test_column_grants_cover_every_use_of_a_column(joe):
    expect_blocked(client=joe, sql="SELECT email FROM customers", why="no SELECT on column(s) email")
    expect_blocked(client=joe, sql="SELECT * FROM customers", why="no SELECT on column(s)")
    # Columns used only to filter, sort, group or aggregate are reads too.
    expect_blocked(client=joe, sql="SELECT id FROM customers WHERE ssn = '111'", why="ssn")
    expect_blocked(client=joe, sql="SELECT id FROM customers ORDER BY email", why="email")
    expect_blocked(client=joe, sql="SELECT max(ssn) FROM customers", why="ssn")
    expect_blocked(client=joe, sql="SELECT id, count(*) FROM customers GROUP BY id, email", why="email")
    expect_blocked(client=joe, sql="SELECT id FROM customers WHERE length(email) > 100", why="email")


def test_indirect_reads_are_checked(joe):
    for sql in ["SELECT o.id FROM orders o JOIN secrets s ON o.id = s.id",
                "SELECT * FROM orders WHERE id IN (SELECT id FROM secrets)",
                "SELECT * FROM orders WHERE EXISTS (SELECT 1 FROM secrets)",
                "SELECT (SELECT value FROM secrets LIMIT 1) AS v",
                "WITH x AS (SELECT * FROM secrets) SELECT * FROM x",
                "WITH x AS MATERIALIZED (SELECT * FROM secrets) SELECT * FROM x",
                "SELECT id FROM orders UNION ALL SELECT id FROM secrets",
                "SELECT id FROM orders EXCEPT SELECT id FROM secrets",
                "WITH RECURSIVE r(n) AS (SELECT id FROM secrets UNION ALL SELECT n + 1 FROM r WHERE n < 3) "
                "SELECT * FROM r",
                "FROM secrets SELECT value"]:
        expect_blocked(client=joe, sql=sql, why=SECRET)
    expect_blocked(client=joe,
                   sql="SELECT * FROM orders o, LATERAL (SELECT ssn FROM customers c WHERE c.id = o.id)",
                   why="ssn")
    # SQL from a string is bound inside DuckDB before any check: refused up front.
    expect_blocked(client=joe, sql="SELECT * FROM query('SELECT * FROM secrets')", why="query()")
    expect_blocked(client=joe, sql="SELECT * FROM query_table('secrets')", why="query_table()")
    expect_blocked(client=joe, sql="SELECT * FROM query('SELECT 1')", why="query()")
    expect_blocked(client=joe, sql="SUMMARIZE secrets")


def test_wrapping_a_statement_does_not_hide_it(joe):
    expect_blocked(client=joe, sql="EXPLAIN SELECT * FROM secrets", why=SECRET)
    expect_blocked(client=joe, sql="EXPLAIN ANALYZE SELECT * FROM secrets", why=SECRET)
    # A batch: the denied statement stops it.
    expect_blocked(client=joe, sql="SELECT 1; SELECT * FROM secrets", why=SECRET)
    # Client-side prepared statements are checked when prepared.
    error = expect_error(call=lambda: joe.prepare(sql="SELECT * FROM secrets WHERE id = ?"),
                         code="permission_denied", why=SECRET)
    assert error.sqlstate == "42501"
    # A session-local wrapper doesn't hide the protected table: a temporary view's query is
    # checked as joe's own SELECT when he creates it (test_rbac_view_creation.py).
    expect_blocked(client=joe, sql="CREATE TEMP VIEW tv AS SELECT * FROM secrets", why=SECRET)
    expect_blocked(client=joe, sql="SELECT * FROM tv", code="sql_error", why="Table with name tv does not exist")
    # A session's macro would be found before the catalog's functions, inside a view
    # too, so users may not create even temporary ones.
    expect_blocked(client=joe, sql="CREATE TEMP MACRO tm() AS TABLE SELECT * FROM secrets",
                   why="creating macros needs an administrator")


def test_prepared_statements_with_parameters_are_checked_when_prepared_and_run(joe):
    # Positive twin: a granted table, read with a bound parameter.
    ok = joe.prepare(sql="SELECT * FROM orders WHERE id = ?")
    assert joe.execute_prepared(prepared_id=ok, parameters=[2]).num_rows == 1
    assert joe.execute_prepared(prepared_id=ok, rows=pa.table({"p": pa.array([1], type=pa.int32())})).num_rows == 1
    # The same statement on a table joe may not read fails when prepared, before its
    # columns are described.
    expect_error(call=lambda: joe.prepare(sql="SELECT value FROM secrets WHERE id = ?"),
                 code="permission_denied", why=SECRET)
    # A parameter whose type DuckDB can only learn from the value: refused when
    # prepared or, at the latest, when run.
    try:
        open_type = joe.prepare(sql="SELECT value FROM secrets WHERE ? IS NOT NULL")
    except WarpError as error:
        assert error.code == "permission_denied" and SECRET in error.detail, str(error)
    else:
        expect_error(call=lambda: joe.execute_prepared(prepared_id=open_type, parameters=[1]),
                     code="permission_denied", why=SECRET)
    # Positive twin of that shape.
    open_ok = joe.prepare(sql="SELECT id FROM orders WHERE ? IS NOT NULL")
    assert joe.execute_prepared(prepared_id=open_ok, parameters=[1]).num_rows == 2


def described_and_executed(client, sql: str, parameters=None) -> tuple[pa.Schema, pa.Schema]:
    response = client.http.post(url=f"{client.url}/prepared", json={"sql": sql}, headers=client.headers())
    assert response.status_code == 201, response.text
    doc = response.json()
    assert doc["result_kind"] == "rows" and doc["result_schema"], doc
    described = pa.ipc.open_stream(source=base64.b64decode(doc["result_schema"])).schema
    executed = client.execute_prepared(prepared_id=doc["prepared_id"], parameters=parameters).schema
    return described, executed


def test_a_prepare_describes_its_result_without_running_it(joe):
    # The result schema comes from binding the statement and converting its columns
    # with warp_result_schema() (bound, never run), which access control lets through:
    # the same schema the execution then sends, for a user with grants.
    described, executed = described_and_executed(client=joe, sql="SELECT *, uuid() AS u FROM orders WHERE id = ?",
                                                 parameters=[1])
    assert described.equals(executed, check_metadata=True), f"{described}\n!=\n{executed}"
    assert described.names == ["id", "amount", "customer", "u"]
    # Called directly, it describes nothing (positive twin above).
    expect_blocked(client=joe, sql="SELECT * FROM warp_result_schema()", code="sql_error",
                   why="only describes a prepared statement's result")


def test_binding_a_statement_never_describes_a_denied_table(joe):
    # JSON null parameters make the backend bind the statement first, to learn the
    # parameter types (duckdb_v2_statement_bind): that bind is checked too.
    with pytest.raises(WarpError) as raised:
        joe.run(sql="SELECT * FROM secrets WHERE id = ? OR ? IS NULL", parameters=[1, None])
    assert raised.value.code == "permission_denied" and SECRET in raised.value.detail
    assert "value" not in raised.value.detail.replace("value FROM", "")  # no column names leak
    # Positive twin.
    assert joe.run(sql="SELECT * FROM orders WHERE id = ? OR ? IS NULL", parameters=[1, None]).num_rows == 2


def test_store_users_can_browse_metadata(joe):
    assert joe.metadata(kind="catalogs").num_rows >= 1
    assert joe.metadata(kind="db-schemas").num_rows >= 1
    assert joe.metadata(kind="tables").num_rows >= 1
    assert joe.metadata(kind="tables", include_schema="true").num_rows >= 1
    filtered = joe.metadata(kind="tables", table_name_filter_pattern="orders", include_schema="true")
    assert filtered.num_rows == 1


def test_the_check_cannot_be_switched_off(joe):
    expect_blocked(client=joe, sql="SET enable_optimizer = false", why="changing enable_optimizer")
    expect_blocked(client=joe, sql="SET disabled_optimizers = 'extension'", why="disabled_optimizers")
    expect_blocked(client=joe, sql="PRAGMA disable_optimizer")
    # Positive twin: ordinary session settings still work.
    expect_allowed(client=joe, sql="SET search_path = 'main'")
    expect_allowed(client=joe, sql="SET TimeZone = 'UTC'")
    expect_allowed(client=joe, sql="BEGIN TRANSACTION")
    expect_allowed(client=joe, sql="SELECT * FROM orders")
    expect_allowed(client=joe, sql="COMMIT")


def test_settings_for_the_whole_server_need_manage_config(joe):
    # A bare SET of a setting DuckDB keeps only globally changes it for every session.
    for sql in ["SET memory_limit = '1GB'", "SET threads = 1", "SET GLOBAL TimeZone = 'UTC'",
                "SET autoinstall_extension_repository = 'http://example.invalid'",
                "RESET GLOBAL memory_limit"]:
        expect_blocked(client=joe, sql=sql, why="MANAGE CONFIG")
    expect_allowed(client=joe, sql="SET SESSION TimeZone = 'UTC'")


def test_settings_that_write_files_need_write_filesystem(server, joe, tmp_path):
    # DuckDB writes every statement's text to log_query_path, and profiles to
    # profiling_output: wherever they point, as COPY TO would.
    target = tmp_path / "written-by-joe.log"
    for sql in [f"SET log_query_path = '{target}'", f"SET profiling_output = '{target}'",
                f"PRAGMA profile_output = '{target}'"]:
        expect_blocked(client=joe, sql=sql, why="needs the WRITE FILESYSTEM privilege")
    expect_allowed(client=joe, sql="SELECT 1")
    assert not target.exists()
    # Positive twin: a role that holds WRITE FILESYSTEM may.
    admin = server.admin_client
    expect_allowed(client=admin, sql="CREATE ROLE log_writer")
    expect_allowed(client=admin, sql="GRANT WRITE FILESYSTEM TO log_writer")
    expect_allowed(client=admin, sql="GRANT log_writer TO joe")
    expect_allowed(client=joe, sql=f"SET log_query_path = '{target}'")
    expect_allowed(client=joe, sql="SELECT 'logged' AS marker")
    expect_allowed(client=admin, sql="REVOKE log_writer FROM joe")
    assert target.exists()


def test_table_functions_are_allow_listed(joe):
    # Files and URLs are refused before DuckDB binds them (binding already reaches the
    # network), so no request leaves the server on the user's behalf.
    expect_blocked(client=joe, sql="SELECT * FROM read_parquet('s3://bucket/x.parquet')", why="files and URLs")
    expect_blocked(client=joe, sql="SELECT * FROM read_csv('https://example.invalid/x.csv')",
                   why="files and URLs")
    expect_blocked(client=joe, sql="SELECT * FROM 'https://example.invalid/x.parquet'", why="files and URLs")
    expect_blocked(client=joe, sql="COPY (SELECT * FROM orders) TO 's3://bucket/out.parquet'",
                   why="files and URLs")
    expect_blocked(client=joe, sql="SELECT * FROM duckdb_secrets()", why="duckdb_secrets()")
    # Refused before DuckDB binds them: binding a table function runs it (a remote
    # database's scan connects; an unknown name autoloads an extension).
    expect_blocked(client=joe, sql="SELECT * FROM test_vector_types(NULL::INT)",
                   why="the table function test_vector_types")
    expect_blocked(client=joe, sql="SELECT * FROM sqlite_scan('/etc/hosts', 't')",
                   why="the table function sqlite_scan")
    expect_blocked(client=joe, sql="CALL pragma_metadata_info()", why="the table function pragma_metadata_info")
    expect_allowed(client=joe, sql="SELECT * FROM generate_series(1, 3)")
    expect_allowed(client=joe, sql="SELECT * FROM duckdb_tables()")


# ---- Writes ----

def test_writes_need_their_own_privilege(joe, wanda):
    expect_blocked(client=joe, sql="INSERT INTO orders VALUES (9, 9, 'x')", why="no INSERT privilege")
    expect_blocked(client=joe, sql="UPDATE orders SET amount = 0", why="no UPDATE privilege")
    expect_blocked(client=joe, sql="DELETE FROM orders", why="no DELETE privilege")
    # wanda may write but not read.
    expect_allowed(client=wanda, sql="INSERT INTO orders VALUES (3, 30, 'cat')")
    # SET amount = amount reads amount, so it needs SELECT on it (as in Postgres).
    expect_blocked(client=wanda, sql="UPDATE orders SET amount = amount", why="no SELECT")
    expect_blocked(client=wanda, sql="SELECT * FROM orders", why="no SELECT")


def test_writes_that_read_need_select(joe, wanda):
    expect_blocked(client=wanda, sql="DELETE FROM orders WHERE customer = 'ann'", why="no SELECT")
    expect_blocked(client=wanda, sql="UPDATE orders SET amount = 1 RETURNING *", why="no SELECT")
    expect_blocked(client=wanda, sql="INSERT INTO orders VALUES (5, 5, 'e') RETURNING id", why="no SELECT")
    expect_blocked(client=wanda, sql="INSERT INTO orders SELECT id, 0, value FROM secrets", why="no SELECT")
    expect_blocked(client=joe, sql="MERGE INTO orders USING (SELECT 1 AS id) s ON orders.id = s.id "
                                   "WHEN MATCHED THEN DELETE")
    # Positive twin: an unfiltered bulk write needs no SELECT.
    expect_allowed(client=wanda, sql="UPDATE orders SET amount = 0")


def test_dml_inside_a_cte_is_a_write(joe, eve):
    expect_blocked(client=joe, sql="WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d",
                   why="no DELETE privilege")
    expect_blocked(client=eve, sql="WITH i AS (INSERT INTO orders VALUES (7, 7, 'g') RETURNING *) SELECT 1",
                   why="no INSERT privilege")


def test_bulk_ingest_is_checked_like_insert(server, admin, wanda, joe, bob):
    rows = pa.table({"id": pa.array([40], type=pa.int32()), "amount": pa.array([4], type=pa.int32()),
                     "customer": ["ing"]})
    # Appending needs INSERT; wanda holds it, joe does not.
    assert wanda.ingest(table="orders", rows=rows, mode="append") == 1
    error = expect_error(call=lambda: joe.ingest(table="orders", rows=rows, mode="append"),
                         code="permission_denied", why="no INSERT privilege on table")
    assert error.sqlstate == "42501"
    # Creating needs CREATE TABLE; bob holds it, and then owns what he made.
    assert bob.ingest(table="bob_ingested", rows=rows, mode="create") == 1
    assert expect_allowed(client=bob, sql="SELECT * FROM bob_ingested").num_rows == 1
    expect_blocked(client=joe, sql="SELECT * FROM bob_ingested", why=SECRET)
    expect_error(call=lambda: joe.ingest(table="joes_ingested", rows=rows, mode="create"),
                 code="permission_denied", why="no CREATE TABLE privilege")
    # Replacing a table another user owns is dropping it.
    expect_error(call=lambda: bob.ingest(table="orders", rows=rows, mode="replace"),
                 code="permission_denied", why="may not drop table")
    # The upload function reads only an ingest's own rows.
    expect_blocked(client=joe, sql="SELECT * FROM warp_upload()", code="sql_error",
                   why="only reads the rows of a bulk ingest")
    expect_allowed(client=bob, sql="DROP TABLE bob_ingested")


def test_partitioned_ingest_is_checked_like_insert(server, admin, wanda, joe, bob):
    # A partitioned ingest (Warp SQL §8.3) is one ingest: checked like one, all or nothing.
    rows = pa.table({"id": pa.array([41, 42, 43, 44], type=pa.int32()),
                     "amount": pa.array([4, 4, 4, 4], type=pa.int32()), "customer": ["p1", "p2", "p3", "p4"]})
    before = expect_allowed(client=admin, sql="SELECT count(*) AS n FROM orders").column("n")[0].as_py()
    assert wanda.ingest_in_parts(table="orders", rows=rows, mode="append") == 4
    error = expect_error(call=lambda: joe.ingest_in_parts(table="orders", rows=rows, mode="append"),
                         code="permission_denied", why="no INSERT privilege on table")
    assert error.sqlstate == "42501"
    expect_error(call=lambda: joe.ingest_in_parts(table="joes_parts", rows=rows, mode="create"),
                 code="permission_denied", why="no CREATE TABLE privilege")
    after = expect_allowed(client=admin, sql="SELECT count(*) AS n FROM orders").column("n")[0].as_py()
    assert after == before + 4  # wanda's four rows, none of joe's
    # A part is bound to its session: joe cannot add rows to wanda's open ingest.
    url = wanda.open_ingest(table="orders", mode="append")
    stolen = joe.http.post(url=url.replace(wanda.session_id, joe.session_id) + "/parts/0",
                           content=arrow_bytes(table=rows), headers=joe.headers(**{"Content-Type": ARROW_STREAM}))
    assert stolen.status_code == 404 and stolen.json()["code"] == "ingest_not_found", stolen.text
    forged = joe.http.post(url=url + "/parts/0", content=arrow_bytes(table=rows),
                           headers=joe.headers(**{"Content-Type": ARROW_STREAM}))
    assert forged.status_code in (403, 404), forged.text
    assert wanda.http.delete(url=url, headers=wanda.headers()).status_code == 204
    expect_allowed(client=admin, sql="DELETE FROM orders WHERE customer LIKE 'p_'")


# ---- DDL and ownership ----

def test_creating_needs_the_privilege_and_makes_the_creator_owner(eve, bob):
    expect_blocked(client=eve, sql="CREATE TABLE eve_table (x INT)", why="no CREATE TABLE privilege on schema")
    expect_blocked(client=eve, sql="CREATE VIEW eve_view AS SELECT 1", why="no CREATE VIEW privilege")
    expect_allowed(client=bob, sql="CREATE TABLE bob_table (x INT)")
    expect_allowed(client=bob, sql="INSERT INTO bob_table VALUES (1)")
    expect_allowed(client=bob, sql="SELECT * FROM bob_table")  # the owner holds every privilege
    expect_blocked(client=eve, sql="SELECT * FROM bob_table", why="no SELECT")
    expect_blocked(client=eve, sql="DROP TABLE bob_table", why="may not drop table")
    expect_blocked(client=eve, sql="ALTER TABLE bob_table RENAME TO stolen", why="may not alter table")
    expect_allowed(client=bob, sql="DROP TABLE bob_table")


def test_a_prepared_create_makes_the_creator_owner_when_it_runs(bob, eve):
    prepared = bob.prepare(sql="CREATE TABLE bob_prepared AS SELECT 1 AS x")
    bob.execute_prepared(prepared_id=prepared)
    expect_allowed(client=bob, sql="SELECT * FROM bob_prepared")
    expect_blocked(client=eve, sql="SELECT * FROM bob_prepared", why="no SELECT")
    expect_allowed(client=bob, sql="DROP TABLE bob_prepared")


def test_revoking_stops_a_statement_that_is_running(server, admin):
    for sql in ["CREATE ROLE runner", "CREATE USER rob", "GRANT runner TO rob",
                "GRANT SELECT ON orders TO runner"]:
        expect_allowed(client=admin, sql=sql)
    rob = server.user(name="rob")
    rob_runner = server.user(name="rob")
    running = rob_runner.submit(sql="SELECT sum(o.id * r.range) FROM orders o, range(1000000000000) r")
    try:
        time.sleep(1)
        # Gaining a right leaves it running...
        expect_allowed(client=admin, sql="GRANT SELECT (id) ON customers TO runner")
        time.sleep(2.5)  # the store's listener has seen the change
        assert rob_runner.wait(statement_id=running, timeout=0.5)["state"] == "running"
        # ...losing one stops it.
        expect_allowed(client=admin, sql="REVOKE SELECT ON orders FROM runner")
        doc = rob_runner.wait(statement_id=running, timeout=30)
        assert doc["state"] in ("failed", "cancelled"), doc
        assert "nterrupt" in doc["error"]["detail"], doc
    finally:
        rob_runner.http.delete(url=f"{rob_runner.url}/statements/{running}", headers=rob_runner.headers())
    # Both sessions live on; their next statements are checked against the new grants.
    expect_allowed(client=rob, sql="SELECT 1")
    expect_allowed(client=rob_runner, sql="SELECT 1")
    expect_blocked(client=rob_runner, sql="SELECT * FROM orders", why="no SELECT privilege")
    expect_allowed(client=rob, sql="SELECT id FROM customers")


def test_an_owner_shares_what_it_created(bob, joe):
    expect_allowed(client=bob, sql="CREATE TABLE bob_shared AS SELECT 1 AS id, 'b' AS note")
    expect_blocked(client=joe, sql="SELECT * FROM bob_shared", why=SECRET)
    expect_allowed(client=bob, sql="GRANT SELECT (id) ON bob_shared TO reader")
    expect_allowed(client=joe, sql="SELECT id FROM bob_shared")
    expect_blocked(client=joe, sql="SELECT note FROM bob_shared", why="no SELECT on column(s) note")
    # Not on what bob does not own.
    expect_blocked(client=bob, sql="GRANT SELECT ON secrets TO builder", why="may not grant SELECT")
    expect_allowed(client=bob, sql="REVOKE SELECT (id) ON bob_shared FROM reader")
    expect_blocked(client=joe, sql="SELECT id FROM bob_shared", why="no SELECT")


def test_a_schemas_creator_owns_it(admin, joe, bob):
    expect_allowed(client=admin, sql="CREATE ROLE IF NOT EXISTS schema_maker")
    expect_allowed(client=admin, sql="GRANT CREATE SCHEMA ON CATALOG rbac_enforce TO schema_maker")
    expect_allowed(client=admin, sql="GRANT schema_maker TO bob")
    expect_blocked(client=joe, sql="CREATE SCHEMA joes", why="no CREATE SCHEMA privilege")
    expect_allowed(client=bob, sql="CREATE SCHEMA bobs")
    # The owner of a schema may create in it and drop it; nobody else may.
    expect_allowed(client=bob, sql="CREATE TABLE bobs.t AS SELECT 1 AS x")
    expect_allowed(client=bob, sql="SELECT * FROM bobs.t")
    for sql in ["DROP SCHEMA bobs CASCADE", "DROP SCHEMA IF EXISTS bobs CASCADE", "DROP SCHEMA BOBS CASCADE",
                "DROP SCHEMA rbac_enforce.bobs CASCADE", "DROP SCHEMA IF EXISTS rbac_enforce.bobs CASCADE"]:
        expect_blocked(client=joe, sql=sql, why="may not drop schema")
    expect_allowed(client=bob, sql="SELECT * FROM bobs.t")  # still there
    expect_allowed(client=bob, sql="DROP TABLE bobs.t")
    expect_allowed(client=bob, sql="DROP SCHEMA bobs")
    expect_allowed(client=admin, sql="REVOKE schema_maker FROM bob")


def test_drop_if_exists_of_another_owners_object_is_refused(joe):
    for sql in ["DROP TABLE IF EXISTS orders", "DROP TABLE IF EXISTS main.ORDERS",
                "DROP TABLE IF EXISTS rbac_enforce.main.orders", "DROP VIEW IF EXISTS orders_over_secrets",
                "DROP SCHEMA IF EXISTS main CASCADE"]:
        expect_blocked(client=joe, sql=sql, why="may not drop")
    expect_allowed(client=joe, sql="SELECT count(*) FROM orders")  # still there
    expect_allowed(client=joe, sql="DROP TABLE IF EXISTS no_such_table")  # nothing to drop


def test_replacing_or_ignoring_another_owners_object(bob):
    expect_blocked(client=bob, sql="CREATE OR REPLACE TABLE orders AS SELECT 1 AS id", why="may not drop table")
    # IF NOT EXISTS on an existing table changes nothing and grants nothing.
    expect_allowed(client=bob, sql="CREATE TABLE IF NOT EXISTS orders (id INT)")
    expect_blocked(client=bob, sql="SELECT * FROM orders", why="no SELECT")
    expect_blocked(client=bob, sql="DROP TABLE orders", why="may not drop table")


def test_administrative_statements_stay_denied(joe):
    expect_blocked(client=joe, sql="CREATE SEQUENCE seq1", why="needs an administrator")
    expect_blocked(client=joe, sql="CREATE MACRO m1(a) AS a + 1", why="needs an administrator")
    # SQL-level PREPARE is checked on the statement it prepares.
    expect_blocked(client=joe, sql="PREPARE p_secret AS SELECT * FROM secrets", why="no SELECT")
    expect_blocked(client=joe, sql="ATTACH ':memory:' AS mine", why="MANAGE CATALOGS")
    expect_blocked(client=joe, sql="INSTALL httpfs", why="MANAGE EXTENSIONS")
    expect_blocked(client=joe, sql="CREATE SECRET s (TYPE s3, KEY_ID 'x', SECRET 'y')", why="MANAGE SECRETS")
    expect_blocked(client=joe, sql="CHECKPOINT", why="CHECKPOINT")
    expect_blocked(client=joe, sql="SELECT * FROM read_text('/etc/hosts')", why="READ FILESYSTEM")
    # Temporary objects are the session's own.
    expect_allowed(client=joe, sql="CREATE TEMP TABLE scratch (x INT)")
    expect_allowed(client=joe, sql="INSERT INTO scratch VALUES (1)")
    expect_allowed(client=joe, sql="SELECT * FROM scratch")


def test_views_read_with_their_owners_rights(admin, joe):
    expect_blocked(client=joe, sql="SELECT * FROM orders_over_secrets", why="no SELECT privilege on view")
    expect_allowed(client=admin, sql="GRANT SELECT ON orders_over_secrets TO reader")
    assert expect_allowed(client=joe, sql="SELECT * FROM orders_over_secrets").num_rows == 1
    expect_blocked(client=joe, sql="SELECT * FROM secrets", why=SECRET)
    expect_allowed(client=admin, sql="REVOKE SELECT ON orders_over_secrets FROM reader")


# ---- Revocation ----

def test_revoking_stops_the_next_statement_and_prepared_ones(server, admin):
    for sql in ["CREATE ROLE temp_reader", "CREATE USER IF NOT EXISTS rita", "GRANT SELECT ON orders TO temp_reader",
                "GRANT temp_reader TO rita"]:
        expect_allowed(client=admin, sql=sql)
    rita = server.user(name="rita")
    expect_allowed(client=rita, sql="SELECT * FROM orders")
    prepared = rita.prepare(sql="SELECT * FROM orders")
    rita.execute_prepared(prepared_id=prepared)

    expect_allowed(client=admin, sql="REVOKE SELECT ON orders FROM temp_reader")
    expect_blocked(client=rita, sql="SELECT * FROM orders", why="no SELECT")
    expect_error(call=lambda: rita.execute_prepared(prepared_id=prepared), code="permission_denied",
                 why="no SELECT privilege")


def test_access_control_statements_run_alone(joe, admin):
    # SHOW GRANTS parses as a SELECT and RESET ROLE as a SET: in a batch they would reach
    # DuckDB as those.
    for sql in ["SELECT 1; RESET ROLE", "SELECT 1; SHOW GRANTS", "SELECT 1;\n  /* c */ show grants"]:
        expect_blocked(client=joe, sql=sql, code="sql_error", why="run on their own")
    # Ones DuckDB cannot parse at all fail as the syntax errors they are to DuckDB.
    expect_blocked(client=joe, sql="SELECT 1; SET ROLE reader", code="sql_error", why="syntax error")
    expect_blocked(client=admin, sql="SHOW GRANTS; SELECT 1", code="sql_error", why="one at a time")
    assert expect_allowed(client=joe, sql="SHOW GRANTS").num_rows >= 1
    # Bind parameters make no sense for them.
    with pytest.raises(WarpError) as raised:
        joe.run(sql="SHOW GRANTS", parameters=[1])
    assert raised.value.code == "sql_error" and "takes no bind parameters" in raised.value.detail


def test_access_control_statements_can_be_prepared(server, admin):
    prepared = admin.prepare(sql="CREATE ROLE prepared_role")
    # Preparing runs nothing; executing does, once per execution.
    assert "prepared_role" not in str(admin.run(sql="SHOW GRANTS FOR ROLE PUBLIC").to_pylist())
    assert admin.execute_prepared(prepared_id=prepared).to_pylist() == [{"result": "Role prepared_role created"}]
    expect_error(call=lambda: admin.execute_prepared(prepared_id=prepared), code="sql_error",
                 why="already exists")
    expect_allowed(client=admin, sql="DROP ROLE prepared_role")


def test_a_concurrent_grant_reaches_a_session_at_once(server, admin):
    # The store's change is seen by the session's very next statement (no polling delay
    # on the node that made it).
    expect_allowed(client=admin, sql="CREATE USER IF NOT EXISTS gus")
    gus = server.user(name="gus")
    expect_blocked(client=gus, sql="SELECT * FROM secrets", why=SECRET)
    expect_allowed(client=admin, sql="CREATE ROLE IF NOT EXISTS secret_reader")
    expect_allowed(client=admin, sql="GRANT SELECT ON secrets TO secret_reader")
    expect_allowed(client=admin, sql="GRANT secret_reader TO gus")
    expect_allowed(client=gus, sql="SELECT * FROM secrets")
    expect_allowed(client=admin, sql="REVOKE secret_reader FROM gus")
    expect_blocked(client=gus, sql="SELECT * FROM secrets", why=SECRET)


def test_table_macros_are_checked_as_what_they_read(server, admin, joe):
    # A table macro is no table function of its own: DuckDB expands it, and the check
    # sees what it reads, with the caller's rights.
    expect_allowed(client=admin, sql="CREATE OR REPLACE MACRO orders_since(n) AS TABLE SELECT * FROM orders WHERE id >= n")
    expect_allowed(client=admin, sql="CREATE OR REPLACE MACRO all_secrets() AS TABLE SELECT * FROM secrets")
    assert expect_allowed(client=joe, sql="SELECT * FROM orders_since(2)").num_rows >= 1
    expect_blocked(client=joe, sql="SELECT * FROM all_secrets()", why=SECRET)
