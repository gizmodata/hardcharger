"""Row-level security and column masking: CREATE ROW POLICY and CREATE MASKING POLICY,
applied to the plan of every store user's statement."""

import base64
import time

import pyarrow as pa
import pyarrow.ipc  # noqa: F401 (pa.ipc)
import pytest

from rbac import WarpError, expect_allowed, expect_blocked, start_rbac_server


@pytest.fixture(scope="module")
def server(module_launcher, store, tmp_path_factory):
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               args=["--database", str(tmp_path_factory.mktemp("db") / "rbac_policies.duckdb")])
    admin = server.admin()
    for sql in [
        "CREATE TABLE emp (id INT, name VARCHAR, region VARCHAR, salary INT, email VARCHAR, ssn VARCHAR)",
        "INSERT INTO emp VALUES (1, 'ann', 'EU', 100, 'ann@x.com', '111'), (2, 'bob', 'US', 200, 'bob@x.com', '222'), "
        "(3, 'cat', 'EU', 300, 'cat@x.com', '333'), (4, 'dan', 'APAC', 400, 'dan@x.com', '444')",
        "CREATE TABLE notes (owner VARCHAR, note VARCHAR)",
        "INSERT INTO notes VALUES ('ann', 'ann-note'), ('bob', 'bob-note'), ('duckdb', 'nobody')",
        "CREATE VIEW emp_v AS SELECT * FROM emp",
        "CREATE ROLE staff", "CREATE ROLE hr", "CREATE ROLE auditors",
        "GRANT SELECT ON emp TO staff", "GRANT SELECT ON emp TO auditors", "GRANT SELECT ON emp_v TO staff",
        "GRANT SELECT ON notes TO staff", "GRANT DELETE ON emp TO staff", "GRANT INSERT, UPDATE ON emp TO staff",
        "GRANT CREATE TABLE ON SCHEMA main TO staff",
        "CREATE USER ann", "CREATE USER bob", "CREATE USER hal", "CREATE USER dan", "CREATE USER aud",
        "GRANT staff TO ann", "GRANT staff TO bob", "GRANT staff TO hal", "GRANT hr TO hal", "GRANT staff TO dan",
        "GRANT auditors TO aud",
        "ALTER USER ann SET ATTRIBUTE region = 'EU'", "ALTER USER bob SET ATTRIBUTE region = 'US'",
        "ALTER USER hal SET ATTRIBUTE region = 'US'",
        "CREATE ROW POLICY region_rows ON emp TO staff USING (region = session_attribute('region'))",
        "CREATE ROW POLICY hr_all ON emp TO hr USING (true)",
        "CREATE ROW POLICY not_apac ON emp AS RESTRICTIVE USING (region <> 'APAC')",
        "CREATE ROW POLICY own_notes ON notes USING (owner = current_user)",
        "CREATE MASKING POLICY email_mask AS (val VARCHAR) RETURNS VARCHAR -> "
        "CASE WHEN is_role_active('hr') THEN val ELSE regexp_replace(val, '.+@', '****@') END",
        "ALTER TABLE emp MODIFY COLUMN email SET MASKING POLICY email_mask",
        "CREATE MASKING POLICY hide AS (val VARCHAR) RETURNS VARCHAR -> 'XXX'",
        "ALTER TABLE emp MODIFY COLUMN ssn SET MASKING POLICY hide EXEMPT ROLE hr",
    ]:
        expect_allowed(client=admin, sql=sql)
    server.admin_client = admin
    return server


@pytest.fixture
def admin(server):
    return server.admin_client


@pytest.fixture
def ann(server):
    return server.user(name="ann")


@pytest.fixture
def bob(server):
    return server.user(name="bob")


@pytest.fixture
def hal(server):
    return server.user(name="hal")


def text(value) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def values(client, sql: str, parameters=None) -> list[str]:
    """The first column of the result, as text, sorted."""
    table = expect_allowed(client=client, sql=sql, parameters=parameters)
    return sorted(text(value) for value in table.column(0).to_pylist())


def one(client, sql: str) -> str:
    found = values(client=client, sql=sql)
    assert len(found) == 1, f"{sql}: {found}"
    return found[0]


def test_each_user_sees_the_rows_their_policies_allow(server, admin, ann, bob, hal):
    assert values(client=ann, sql="SELECT id FROM emp") == ["1", "3"]
    assert values(client=bob, sql="SELECT id FROM emp") == ["2"]
    # hr's permissive policy OR-ed in; the restrictive one still removes APAC.
    assert values(client=hal, sql="SELECT id FROM emp") == ["1", "2", "3"]
    # No attribute: the predicate is NULL, so nothing.
    assert values(client=server.user(name="dan"), sql="SELECT id FROM emp") == []
    # A role with SELECT but no permissive policy that applies sees nothing.
    assert values(client=server.user(name="aud"), sql="SELECT id FROM emp") == []
    # The administrator is not subject to policies.
    assert values(client=admin, sql="SELECT id FROM emp") == ["1", "2", "3", "4"]


@pytest.mark.parametrize(("sql", "expected"), [
    ("SELECT count(*) FROM emp", ["2"]),
    ("SELECT id FROM emp WHERE id = 2", []),
    ("SELECT max(salary) FROM emp", ["300"]),
    ("SELECT count(*) FROM (SELECT * FROM emp) t", ["2"]),
    ("WITH e AS (SELECT * FROM emp) SELECT count(*) FROM e", ["2"]),
    ("WITH e AS MATERIALIZED (SELECT * FROM emp) SELECT count(*) FROM e", ["2"]),
    ("SELECT count(*) FROM emp a JOIN emp b ON a.id = b.id", ["2"]),
    ("SELECT count(*) FROM emp a, emp b", ["4"]),
    ("SELECT id FROM emp UNION SELECT id FROM emp", ["1", "3"]),
    ("SELECT count(*) FROM emp WHERE id IN (SELECT id FROM emp WHERE region = 'US')", ["0"]),
    ("SELECT EXISTS (SELECT 1 FROM emp WHERE name = 'bob')", ["false"]),
    ("SELECT (SELECT max(salary) FROM emp)", ["300"]),
    ("SELECT count(*) FROM emp_v", ["2"]),
    ("SELECT count(*) FROM emp_v WHERE region = 'APAC'", ["0"]),
    ("SELECT count(*) FROM emp e, LATERAL (SELECT e.id AS x) l", ["2"]),
    ("SELECT region, count(*) FROM emp GROUP BY region", ["EU"]),
])
def test_no_spelling_of_the_query_gets_around_the_filter(ann, sql, expected):
    assert values(client=ann, sql=sql) == expected


def test_prepared_statements_are_filtered_the_same_way(ann):
    prepared = ann.prepare(sql="SELECT count(*) FROM emp")
    assert ann.execute_prepared(prepared_id=prepared).column(0).to_pylist() == [2]


def test_prepared_statements_with_parameters_are_filtered_and_masked(ann, hal):
    # DuckDB plans a statement with parameters again for each run: the policies apply
    # to that plan.
    by_id = ann.prepare(sql="SELECT id, ssn FROM emp WHERE id >= ? ORDER BY id")
    rows = ann.execute_prepared(prepared_id=by_id, rows=pa.table({"p": pa.array([0], type=pa.int32())}))
    assert rows.to_pylist() == [{"id": 1, "ssn": "XXX"}, {"id": 3, "ssn": "XXX"}]
    # Running it again with another value: still filtered.
    again = ann.execute_prepared(prepared_id=by_id, parameters=[2])
    assert again.column("id").to_pylist() == [3]
    # A parameter is no oracle for the masked value.
    by_ssn = ann.prepare(sql="SELECT count(*) FROM emp WHERE ssn = ?")
    assert ann.execute_prepared(prepared_id=by_ssn, parameters=["111"]).column(0).to_pylist() == [0]
    # Positive twin: the exempt user finds it.
    hal_ssn = hal.prepare(sql="SELECT count(*) FROM emp WHERE ssn = ?")
    assert hal.execute_prepared(prepared_id=hal_ssn, parameters=["111"]).column(0).to_pylist() == [1]


def test_a_masked_columns_described_type_is_the_one_sent(ann):
    # A prepare describes its result by binding it, before the policies rewrite the plan:
    # a mask keeps its column's type, so the described schema is the executed one.
    response = ann.http.post(url=f"{ann.url}/prepared", json={"sql": "SELECT id, ssn, email FROM emp"},
                             headers=ann.headers())
    assert response.status_code == 201, response.text
    doc = response.json()
    assert doc["result_kind"] == "rows" and doc["result_schema"], doc
    described = pa.ipc.open_stream(source=base64.b64decode(doc["result_schema"])).schema
    rows = ann.execute_prepared(prepared_id=doc["prepared_id"])
    assert rows.column("ssn").to_pylist() == ["XXX", "XXX"]  # masked
    assert described.equals(rows.schema, check_metadata=True), f"{described}\n!=\n{rows.schema}"


def test_an_owners_view_does_not_lift_the_callers_policies(ann, bob):
    # emp_v is the administrator's, but policies follow the caller.
    assert values(client=ann, sql="SELECT id FROM emp_v") == ["1", "3"]
    assert values(client=bob, sql="SELECT id FROM emp_v") == ["2"]


def test_context_functions_are_the_sessions_not_duckdbs(ann, bob):
    # DuckDB's own current_user is the constant 'duckdb'; the policy must not see it.
    assert values(client=ann, sql="SELECT note FROM notes") == ["ann-note"]
    assert values(client=bob, sql="SELECT note FROM notes") == ["bob-note"]


def test_a_masked_column_is_masked_wherever_it_is_used(ann, hal):
    assert one(client=ann, sql="SELECT email FROM emp WHERE id = 1") == "****@x.com"
    assert one(client=ann, sql="SELECT ssn FROM emp WHERE id = 1") == "XXX"
    # Not an oracle for the real value: filters, grouping and joins see the mask.
    assert one(client=ann, sql="SELECT count(*) FROM emp WHERE email = 'ann@x.com'") == "0"
    assert one(client=ann, sql="SELECT count(*) FROM emp WHERE ssn = '111'") == "0"
    assert one(client=ann, sql="SELECT count(*) FROM emp WHERE ssn LIKE '1%'") == "0"
    assert one(client=ann, sql="SELECT count(DISTINCT ssn) FROM emp") == "1"
    assert one(client=ann, sql="SELECT count(*) FROM emp a JOIN emp b ON a.ssn = b.ssn") == "4"
    assert values(client=ann, sql="SELECT max(email) FROM emp") == ["****@x.com"]
    assert values(client=ann, sql="SELECT upper(ssn) FROM emp") == ["XXX", "XXX"]
    assert values(client=ann, sql="SELECT ssn FROM (SELECT * FROM emp) t") == ["XXX", "XXX"]
    assert values(client=ann, sql="WITH c AS (SELECT ssn FROM emp) SELECT * FROM c") == ["XXX", "XXX"]
    assert values(client=ann, sql="SELECT ssn FROM emp_v") == ["XXX", "XXX"]
    assert values(client=ann, sql="SELECT ssn FROM emp UNION ALL SELECT ssn FROM emp") == ["XXX"] * 4
    assert values(client=ann, sql="SELECT id FROM emp ORDER BY ssn, id LIMIT 1") == ["1"]
    # A copy carries the mask, not the value.
    expect_allowed(client=ann, sql="CREATE TABLE ann_copy AS SELECT * FROM emp")
    assert values(client=ann, sql="SELECT ssn FROM ann_copy") == ["XXX", "XXX"]
    # Exempt and unmasking roles see the real values.
    assert one(client=hal, sql="SELECT ssn FROM emp WHERE id = 2") == "222"
    assert one(client=hal, sql="SELECT email FROM emp WHERE id = 2") == "bob@x.com"


def test_writes_to_a_governed_table_are_refused(admin, ann):
    why = "which has row or masking policies"
    expect_blocked(client=ann, sql="INSERT INTO emp VALUES (9, 'eve', 'US', 1, 'e@x', '999')", why=why)
    expect_blocked(client=ann, sql="UPDATE emp SET salary = 0 WHERE id = 1", why=why)
    expect_blocked(client=ann, sql="DELETE FROM emp WHERE id = 1 RETURNING *", why="masked columns")
    # DELETE removes only rows the user can see.
    expect_allowed(client=ann, sql="DELETE FROM emp WHERE id = 2")
    assert values(client=admin, sql="SELECT count(*) FROM emp") == ["4"], "a hidden row was deleted"
    # The administrator is not restricted.
    expect_allowed(client=admin, sql="UPDATE emp SET salary = salary WHERE id = 1")


def test_policy_changes_reach_the_next_statement_and_prepared_ones(admin, bob):
    prepared = bob.prepare(sql="SELECT count(*) FROM emp")
    assert bob.execute_prepared(prepared_id=prepared).column(0).to_pylist() == [1]
    expect_allowed(client=admin, sql="CREATE OR REPLACE ROW POLICY region_rows ON emp TO staff USING (true)")
    assert bob.execute_prepared(prepared_id=prepared).column(0).to_pylist() == [3]
    expect_allowed(client=admin, sql="CREATE OR REPLACE ROW POLICY region_rows ON emp TO staff "
                                     "USING (region = session_attribute('region'))")
    assert one(client=bob, sql="SELECT count(*) FROM emp") == "1"


@pytest.mark.parametrize(("sql", "why"), [
    ("CREATE ROW POLICY p1 ON emp USING (id IN (SELECT id FROM notes))", "subquery"),
    ("CREATE ROW POLICY p2 ON emp USING (region = getvariable('r'))", "getvariable"),
    ("CREATE ROW POLICY p3 ON emp USING (region = current_setting('threads'))", "current_setting"),
    ("CREATE ROW POLICY p4 ON emp USING (no_such_column = 1)", "does not fit"),
    ("CREATE ROW POLICY p5 ON emp USING (session_attribute(region))", "string constant"),
    ("CREATE ROW POLICY p6 ON emp_v USING (true)", "is a view"),
    ("CREATE ROW POLICY region_rows ON emp USING (true)", "already exists"),
    ("CREATE MASKING POLICY m1 AS (val VARCHAR) RETURNS VARCHAR -> name", "only its parameter"),
    ("CREATE MASKING POLICY m2 AS (val VARCHAR) RETURNS VARCHAR -> (SELECT 'x')", "subquery"),
    ("DROP MASKING POLICY hide", "unset it there first"),
])
def test_policy_sql_is_checked_when_it_is_created(admin, sql, why):
    with pytest.raises(WarpError) as raised:
        admin.run(sql=sql)
    assert raised.value.code == "sql_error", str(raised.value)
    assert why in raised.value.detail, str(raised.value)


def test_only_the_administrator_manages_policies(admin, ann):
    expect_blocked(client=ann, sql="CREATE ROW POLICY mine ON emp USING (true)",
                   why="only administrators manage row and masking policies")
    expect_blocked(client=ann, sql="ALTER TABLE emp MODIFY COLUMN ssn UNSET MASKING POLICY",
                   why="only administrators manage row and masking policies")
    expect_blocked(client=ann, sql="SHOW ROW POLICIES", why="only administrators")
    assert expect_allowed(client=admin, sql="SHOW ROW POLICIES ON emp").num_rows == 3


def test_an_error_in_the_users_own_predicate_does_not_reveal_hidden_rows(admin, ann):
    # Rows 2 (bob, US) and 4 (dan, APAC) are hidden from ann. Each predicate fails only
    # on a hidden row, with the row's name in the error, so it leaks if DuckDB evaluates
    # it before the policy's filter. emp2's policy cannot be pushed into the scan, so
    # the two filters really are separate operators.
    expect_allowed(client=admin, sql="CREATE TABLE IF NOT EXISTS emp2 AS SELECT * FROM emp")
    expect_allowed(client=admin, sql="GRANT SELECT ON emp2 TO staff")
    expect_allowed(client=admin, sql="CREATE OR REPLACE ROW POLICY emp2_rows ON emp2 TO staff USING "
                                     "(lower(region) || '' = lower(session_attribute('region')))")
    for table in ["emp", "emp2"]:
        for where in ["CASE WHEN id = 2 THEN CAST(name AS INTEGER) = 1 ELSE false END",
                      "CASE WHEN region <> 'EU' THEN error('row ' || name) IS NULL ELSE true END",
                      "id IN (2, 4) AND CAST(name AS INTEGER) = 1",
                      "CAST(CASE WHEN region = 'EU' THEN '1' ELSE name END AS INTEGER) > 0"]:
            sql = f"SELECT id FROM {table} WHERE {where}"
            # The control: with the row visible, the predicate does reach it.
            with pytest.raises(WarpError) as control:
                admin.run(sql=sql)
            assert "bob" in control.value.detail or "dan" in control.value.detail, sql
            try:
                ann.run(sql=sql)
            except WarpError as error:
                assert "bob" not in error.detail and "dan" not in error.detail, f"{sql} revealed a hidden row: {error}"
        having = (f"SELECT region FROM {table} GROUP BY region "
                  "HAVING max(CASE WHEN region <> 'EU' THEN CAST(name AS INTEGER) END) > 0")
        try:
            ann.run(sql=having)
        except WarpError as error:
            assert "bob" not in error.detail, f"{having}: {error}"


def test_a_policy_change_stops_a_statement_that_is_running(server, admin, hal):
    runner = server.user(name="hal")
    running = runner.submit(sql="SELECT sum(e.salary * r.range) FROM emp e, range(1000000000000) r")
    try:
        time.sleep(1)
        # A policy that does not concern hal leaves it running...
        expect_allowed(client=admin, sql="CREATE OR REPLACE ROW POLICY unrelated ON notes TO auditors USING (true)")
        time.sleep(2.5)
        assert runner.wait(statement_id=running, timeout=0.5)["state"] == "running"
        # ...one that narrows what he sees stops it.
        expect_allowed(client=admin, sql="CREATE OR REPLACE ROW POLICY hr_all ON emp TO hr USING (region = 'EU')")
        doc = runner.wait(statement_id=running, timeout=30)
        assert doc["state"] in ("failed", "cancelled"), "a statement kept running after its user's row policy changed"
    finally:
        runner.http.delete(url=f"{runner.url}/statements/{running}", headers=runner.headers())
        expect_allowed(client=admin, sql="CREATE OR REPLACE ROW POLICY hr_all ON emp TO hr USING (true)")
        expect_allowed(client=admin, sql="DROP ROW POLICY IF EXISTS unrelated ON notes")
    assert values(client=hal, sql="SELECT id FROM emp") == ["1", "2", "3"]


def test_context_functions_in_every_form(admin, ann, bob):
    # current_user() and session_user() as calls (DuckDB's would return 'duckdb'), and
    # Hardcharger's own hardcharger_user().
    expect_allowed(client=admin, sql="CREATE TABLE IF NOT EXISTS notes2 AS SELECT * FROM notes")
    expect_allowed(client=admin, sql="GRANT SELECT ON notes2 TO staff")
    expect_allowed(client=admin, sql="CREATE OR REPLACE ROW POLICY own2 ON notes2 USING "
                                     "(owner = current_user() OR owner = session_user() OR owner = hardcharger_user())")
    assert values(client=ann, sql="SELECT note FROM notes2") == ["ann-note"]
    assert values(client=bob, sql="SELECT note FROM notes2") == ["bob-note"]


def test_a_row_policy_decides_on_the_real_value_of_a_masked_column(admin, ann):
    # ssn is masked for ann ('XXX'), but the policy hiding ssn 333 must see 333.
    expect_allowed(client=admin, sql="CREATE TABLE IF NOT EXISTS emp3 AS SELECT * FROM emp")
    expect_allowed(client=admin, sql="GRANT SELECT ON emp3 TO staff")
    expect_allowed(client=admin, sql="CREATE OR REPLACE ROW POLICY all3 ON emp3 USING (true)")
    expect_allowed(client=admin, sql="CREATE OR REPLACE ROW POLICY not333 ON emp3 AS RESTRICTIVE USING (ssn <> '333')")
    expect_allowed(client=admin, sql="ALTER TABLE emp3 MODIFY COLUMN ssn SET MASKING POLICY hide")
    assert values(client=ann, sql="SELECT id FROM emp3") == ["1", "2", "4"]
    assert values(client=ann, sql="SELECT ssn FROM emp3") == ["XXX", "XXX", "XXX"]
