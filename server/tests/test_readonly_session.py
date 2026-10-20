"""A readonly principal (sign-in token role "readonly") reads, and changes only its own
session, as GizmoSQL 1.x allowed: transactions (BEGIN / START TRANSACTION, COMMIT / END,
ROLLBACK / ABORT), SET VARIABLE / RESET VARIABLE, SET / RESET of a setting DuckDB keeps
for the session (SET SESSION, or a plain SET of a session-scoped setting such as
TimeZone), USE, and the PRAGMAs that read. Everything that modifies state is refused
with "User '<user>' has a readonly session and cannot run statements that modify state.":
writes, DDL (temporary objects included, as in 1.x), COPY, EXPORT / IMPORT, CHECKPOINT,
VACUUM, ATTACH / DETACH, INSTALL / LOAD, GLOBAL settings and settings that reach the
instance or the host, also inside a transaction, in a batch, as a prepared statement,
and hidden inside a read: DuckDB's own analysis of each statement decides
(WITH c AS (INSERT ... RETURNING *) SELECT ... is a write, which rc.1 let through).
The same holds for a store user signed in with a readonly token under access control
(where access control refuses SET VARIABLE to every store user).
"""

import pytest

from rbac import WarpError, expect_allowed, start_rbac_server

CATALOG = "readonly_session_test"
REFUSED = "has a readonly session and cannot run statements that modify state"
ADMIN_ONLY = "which requires the 'admin' role"


@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start(args=["--database", f"{CATALOG}.db"])


@pytest.fixture(scope="module")
def admin(server):
    session = server.admin()
    for sql in ["CREATE TABLE IF NOT EXISTS ro_t (id INTEGER)", "DELETE FROM ro_t", "INSERT INTO ro_t VALUES (1), (2)",
                "CREATE SCHEMA IF NOT EXISTS other_schema",
                "CREATE OR REPLACE TABLE other_schema.only_here AS SELECT 'found' AS v",
                "CREATE OR REPLACE TABLE pivot_src AS SELECT * FROM (VALUES ('a', 1), ('b', 2), ('a', 3)) t(k, v)"]:
        session.execute(sql=sql)
    return session


def readonly(server, name: str = "reader"):
    return server.as_role(role="readonly", subject=name)


def refused(session, sql: str, why: str = REFUSED) -> dict:
    problem = session.error(sql=sql)
    assert problem.get("code") == "permission_denied", f"{sql}: {problem}"
    assert why in problem["detail"], f"{sql}: {problem}"
    return problem


def rows_of(admin) -> list[int]:
    return sorted(row["id"] for row in admin.rows(sql="SELECT id FROM ro_t"))


# ---- What a readonly session may do ----

def test_transactions(server, admin):
    session = readonly(server=server)
    for begin, end in [("BEGIN", "COMMIT"), ("BEGIN TRANSACTION", "ROLLBACK"), ("START TRANSACTION", "END"),
                       ("BEGIN TRANSACTION READ ONLY", "ABORT")]:
        session.execute(sql=begin)
        assert session.scalar(sql="SELECT count(*) FROM ro_t") == 2
        session.execute(sql=end)
        # The transaction was real: a second end has none to end.
        assert "no transaction is active" in session.error(sql=end)["detail"].lower(), (begin, end)


def test_writes_inside_a_transaction_are_refused(server, admin):
    session = readonly(server=server)
    session.execute(sql="BEGIN")
    refused(session=session, sql="INSERT INTO ro_t VALUES (3)")
    refused(session=session, sql="WITH c AS (INSERT INTO ro_t VALUES (4) RETURNING *) SELECT * FROM c")
    session.execute(sql="ROLLBACK")
    assert rows_of(admin=admin) == [1, 2]


def test_set_variable(server, admin):
    session = readonly(server=server)
    session.execute(sql="SET VARIABLE answer = 42")
    session.execute(sql="SET VARIABLE total = (SELECT sum(id) FROM ro_t)")
    assert session.scalar(sql="SELECT getvariable('answer')") == 42
    assert session.scalar(sql="SELECT getvariable('total')") == 3
    # The session's own: another session doesn't see it.
    assert readonly(server=server, name="other").scalar(sql="SELECT getvariable('answer')") is None
    session.execute(sql="RESET VARIABLE answer")
    assert session.scalar(sql="SELECT getvariable('answer')") is None


def test_session_settings_change_only_the_session(server, admin):
    session = readonly(server=server)
    other = readonly(server=server, name="other")
    zone = other.scalar(sql="SELECT current_setting('TimeZone')")
    session.execute(sql="SET TimeZone = 'Asia/Tokyo'")
    session.execute(sql="SET SESSION default_order = 'desc'")
    assert session.scalar(sql="SELECT current_setting('TimeZone')") == "Asia/Tokyo"
    assert session.scalar(sql="SELECT current_setting('default_order')") == "DESC"
    assert other.scalar(sql="SELECT current_setting('TimeZone')") == zone
    assert other.scalar(sql="SELECT current_setting('default_order')") == "ASCENDING"
    session.execute(sql="RESET TimeZone")
    session.execute(sql="RESET SESSION default_order")
    assert session.scalar(sql="SELECT current_setting('TimeZone')") == zone
    assert session.scalar(sql="SELECT current_setting('default_order')") == "ASCENDING"


def test_use_and_search_path(server, admin):
    session = readonly(server=server)
    session.execute(sql=f"USE {CATALOG}.other_schema")
    assert session.scalar(sql="SELECT v FROM only_here") == "found"
    session.execute(sql=f"USE {CATALOG}.main")
    assert "only_here" in session.error(sql="SELECT v FROM only_here")["detail"]
    session.execute(sql=f"SET search_path = '{CATALOG}.other_schema'")
    assert session.scalar(sql="SELECT v FROM only_here") == "found"
    session.execute(sql="SET schema = 'main'")
    assert session.scalar(sql="SELECT count(*) FROM ro_t") == 2


def test_reading_pragmas_and_show(server, admin):
    session = readonly(server=server)
    assert [row["name"] for row in session.rows(sql="PRAGMA table_info('ro_t')")] == ["id"]
    assert session.query(sql="PRAGMA version").num_rows == 1
    assert CATALOG in [row["name"] for row in session.rows(sql="PRAGMA database_list")]
    assert "ro_t" in [row["name"] for row in session.rows(sql="SHOW TABLES")]
    assert session.query(sql="DESCRIBE ro_t").num_rows == 1


def test_explain_shows_a_write_without_running_it(server, admin):
    session = readonly(server=server)
    plan = session.rows(sql="EXPLAIN INSERT INTO ro_t VALUES (9)")
    assert "INSERT" in " ".join(row["explain_value"] for row in plan).upper(), plan
    assert rows_of(admin=admin) == [1, 2]


def test_batches_of_session_statements(server, admin):
    session = readonly(server=server)
    assert session.scalar(sql="BEGIN; SET VARIABLE v = 3; COMMIT; SELECT getvariable('v')") == 3
    # A batch with a write runs none of it: no transaction is left open.
    refused(session=session, sql="BEGIN; INSERT INTO ro_t VALUES (5); COMMIT")
    session.execute(sql="BEGIN")
    session.execute(sql="COMMIT")
    assert rows_of(admin=admin) == [1, 2]


def test_pivot_still_reads(server, admin):
    assert readonly(server=server).query(sql="PIVOT pivot_src ON k USING sum(v)").num_rows == 1


def test_prepared_reads_run(server, admin):
    session = readonly(server=server)
    prepared = session.prepare(sql="SELECT count(*) AS n FROM ro_t WHERE id > ?")
    response = session.execute_prepared(prepared_id=prepared["prepared_id"], parameters=[1])
    assert response.status_code == 200, response.text
    assert session.table_of(response=response).column("n").to_pylist() == [1]


# ---- What stays refused ----

@pytest.mark.parametrize("sql", [
    "INSERT INTO ro_t VALUES (9)",
    "UPDATE ro_t SET id = 9",
    "DELETE FROM ro_t",
    "MERGE INTO ro_t USING (SELECT 9 AS id) s ON ro_t.id = s.id WHEN NOT MATCHED THEN INSERT VALUES (s.id)",
    "CREATE TABLE ro_new (id INTEGER)",
    "CREATE TEMP TABLE ro_tmp AS SELECT 1 AS x",
    "CREATE TEMPORARY VIEW ro_tmp_v AS SELECT 1 AS x",
    "CREATE TEMP MACRO ro_m() AS 1",
    "CREATE VIEW ro_v AS SELECT 1 AS x",
    "CREATE SEQUENCE ro_seq",
    "ALTER TABLE ro_t ADD COLUMN extra INTEGER",
    "DROP TABLE ro_t",
    "COPY ro_t TO 's3://no-such-bucket/ro.csv'",
    "COPY ro_t FROM 's3://no-such-bucket/ro.csv'",
    "VACUUM",
    "PREPARE ro_p AS SELECT 1",
    "CALL pragma_version()",
    "PRAGMA enable_profiling",
    # Settings that would reach every session or the host.
    "SET default_order = 'desc'",
    "RESET default_order",
    "SET SESSION threads = 1",
    "SET SESSION enable_profiling = 'json'",
    "SET SESSION profiling_output = 'profile.json'",
    "SET current_dialect = 'duckdb'",
    # A write hidden inside a read: DuckDB's analysis of the statement refuses it.
    "WITH c AS (INSERT INTO ro_t VALUES (9) RETURNING *) SELECT * FROM c",
    "WITH c AS (UPDATE ro_t SET id = 9 RETURNING *) SELECT * FROM c",
    "WITH c AS (DELETE FROM ro_t RETURNING *) SELECT * FROM c",
    "EXPLAIN ANALYZE WITH c AS (INSERT INTO ro_t VALUES (9) RETURNING *) SELECT * FROM c",
    "EXPLAIN ANALYZE INSERT INTO ro_t VALUES (9)",
])
def test_statements_that_modify_state_are_refused(server, admin, sql):
    session = readonly(server=server)
    refused(session=session, sql=sql)
    assert rows_of(admin=admin) == [1, 2]
    assert admin.scalar(sql="SELECT current_setting('default_order')") == "ASCENDING"


@pytest.mark.parametrize("sql", [
    "SET GLOBAL TimeZone = 'UTC'",
    "SET threads = 1",
    "ATTACH ':memory:' AS ro_attached",
    f"DETACH {CATALOG}",
    "INSTALL httpfs",
    "LOAD httpfs",
    "CHECKPOINT",
    "COPY ro_t TO 'ro.csv'",
    "EXPORT DATABASE 'ro_export'",
    "WITH c AS (COPY (SELECT 1) TO 'ro.csv') SELECT 1",
])
def test_instance_and_file_statements_stay_refused(server, admin, sql):
    # These were refused to every user but an administrator before; that refusal speaks
    # first (it says what the role allows).
    refused(session=readonly(server=server), sql=sql, why=ADMIN_ONLY)


def test_prepared_writes_are_refused(server, admin):
    session = readonly(server=server)
    response = session.http.post(url=f"{session.url}/prepared", headers=session.headers(),
                                 json={"sql": "INSERT INTO ro_t VALUES (?)"})
    assert response.status_code == 403 and REFUSED in response.json()["detail"], response.text
    # A write DuckDB can only see once the parameters are bound: it may be prepared, and
    # its execution (DuckDB plans it again with the values) is refused.
    for sql in ["WITH c AS (INSERT INTO ro_t VALUES (?) RETURNING *) SELECT * FROM c",
                "WITH c AS (DELETE FROM ro_t WHERE id = ? RETURNING *) SELECT * FROM c"]:
        response = session.http.post(url=f"{session.url}/prepared", headers=session.headers(), json={"sql": sql})
        if response.status_code == 201:
            response = session.execute_prepared(prepared_id=response.json()["prepared_id"], parameters=[1])
        assert response.status_code == 403, f"{sql}: {response.text}"
        assert REFUSED in response.json()["detail"], f"{sql}: {response.text}"
    assert rows_of(admin=admin) == [1, 2]


def test_ingest_is_refused(server, admin):
    import pyarrow as pa
    session = readonly(server=server)
    response = session.ingest(table=pa.table(data={"id": [7]}), name="ro_t", mode="append")
    assert response.status_code == 403, response.text
    assert "has a readonly session and cannot ingest data" in response.json()["detail"], response.text
    assert rows_of(admin=admin) == [1, 2]


def test_a_user_role_may_still_write(server, admin):
    """The positive twin: the same writes from a "user" token go through, so the refusals
    above are the readonly role's."""
    user = server.as_role(role="user", subject="writer")
    user.execute(sql="WITH c AS (INSERT INTO ro_t VALUES (3) RETURNING *) SELECT * FROM c")
    user.execute(sql="BEGIN")
    user.execute(sql="DELETE FROM ro_t WHERE id = 3")
    user.execute(sql="ROLLBACK")
    assert rows_of(admin=admin) == [1, 2, 3]
    admin.execute(sql="DELETE FROM ro_t WHERE id = 3")


# ---- Under access control ----

def test_readonly_store_user_under_access_control(launcher, store):
    server = start_rbac_server(launcher=launcher, store_uri=store.new_database())
    admin = server.admin()
    for sql in ["CREATE TABLE ac_t (id INTEGER)", "INSERT INTO ac_t VALUES (1)", "CREATE USER IF NOT EXISTS ana",
                "CREATE ROLE ac_writer", "GRANT SELECT, INSERT ON ac_t TO ac_writer", "GRANT ac_writer TO USER ana"]:
        expect_allowed(client=admin, sql=sql)
    writer = server.user(name="ana")  # the same store user with a "user" token: the grant works
    expect_allowed(client=writer, sql="INSERT INTO ac_t VALUES (2)")
    reader = server.user(name="ana", role="readonly")
    expect_allowed(client=reader, sql="BEGIN")
    assert reader.run(sql="SELECT count(*) AS n FROM ac_t").column("n").to_pylist() == [2]
    expect_allowed(client=reader, sql="COMMIT")
    expect_allowed(client=reader, sql="SET TimeZone = 'Asia/Tokyo'")
    assert reader.run(sql="SELECT current_setting('TimeZone') AS z").column("z").to_pylist() == ["Asia/Tokyo"]
    # (SET VARIABLE is access control's to refuse, for every store user.)
    for sql in ["INSERT INTO ac_t VALUES (3)", "WITH c AS (INSERT INTO ac_t VALUES (3) RETURNING *) SELECT * FROM c"]:
        with pytest.raises(WarpError) as raised:
            reader.run(sql=sql)
        assert raised.value.code == "permission_denied" and REFUSED in raised.value.detail, f"{sql}: {raised.value}"
    assert admin.run(sql="SELECT count(*) AS n FROM ac_t").column("n").to_pylist() == [2]
