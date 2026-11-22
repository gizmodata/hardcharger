"""The protocol connection property, independent of token roles and server file mode."""

import json

import pyarrow as pa
import pytest

from rbac import ChargewireError, rbac_env, start_rbac_server


def assert_read_only(problem):
    assert problem["code"] == "read_only", problem
    assert problem["sqlstate"] == "25006", problem


@pytest.fixture(scope="module")
def property_server(module_launcher):
    return module_launcher.start(args=["--enable-instrumentation"])


def test_reader_and_writer_share_database(property_server):
    writer = property_server.admin()
    writer.execute(sql="CREATE TABLE property_rows (n INTEGER)")
    writer.execute(sql="INSERT INTO property_rows VALUES (42)")
    reader = property_server.client().new_session(options={"read_only": True})
    assert reader.options()["read_only"] is True
    assert reader.scalar(sql="SELECT n FROM property_rows") == 42
    assert_read_only(reader.error(sql="INSERT INTO property_rows VALUES (43)"))
    response = reader.ingest(table=pa.table({"n": [44]}), name="property_rows", mode="append")
    assert_read_only(response.json())
    writer.execute(sql="INSERT INTO property_rows VALUES (45)")
    assert reader.scalar(sql="SELECT count(*) FROM property_rows") == 2
    reader.close()
    writer.close()


@pytest.mark.parametrize("function", [
    "hardcharger_settings", "hardcharger_sessions", "hardcharger_active_connections",
    "hardcharger_server_info", "hardcharger_monitor_info", "hardcharger_live_queries",
    "hardcharger_server_settings", "hardcharger_session_settings", "hardcharger_grants",
    "hardcharger_roles", "hardcharger_users", "hardcharger_role_members",
    "hardcharger_policies", "hardcharger_effective_privileges",
])
def test_introspection_remains_available(property_server, function):
    reader = property_server.client().new_session(options={"read_only": True})
    reader.query(sql=f"SELECT * FROM {function}()")
    reader.close()


def test_local_settings_and_batch_preflight(property_server):
    reader = property_server.client().new_session(options={"read_only": True})
    reader.execute(sql="SET hardcharger.query_timeout = 10")
    reader.execute(sql="""SET hardcharger.session_tag = '{"app":"reader"}'""")
    assert_read_only(reader.error(sql="SET GLOBAL hardcharger.query_timeout = 20"))
    assert_read_only(reader.error(sql="SET hardcharger.capture_query_profile = 'detailed'"))
    assert_read_only(reader.error(sql="""SET hardcharger.session_tag = '{"app":"changed"}'; """
                                      "SET GLOBAL hardcharger.query_timeout = 20"))
    tag = reader.scalar(sql="SELECT value FROM hardcharger_settings() "
                            "WHERE name = 'hardcharger.session_tag'")
    assert json.loads(s=tag) == {"app": "reader"}
    reader.execute(sql="RESET hardcharger.query_timeout")
    reader.close()


@pytest.mark.parametrize("sql", [
    "SELECT hardcharger_cancel('{target}')",
    "SELECT hardcharger_set_session_setting('{target}', 'query_timeout', '10')",
    "SELECT hardcharger_reset_session_setting('{target}', 'query_timeout')",
])
def test_administration_refused_after_transition(property_server, sql):
    target = property_server.admin()
    reader = property_server.admin()
    statement = sql.format(target=target.id)
    prepared = reader.prepare(sql=statement)
    response = reader.set_options(options={"read_only": True})
    assert response.status_code == 200, response.text
    assert_read_only(reader.error(sql=statement))
    response = reader.execute_prepared(prepared_id=prepared["prepared_id"])
    assert_read_only(response.json())
    assert_read_only(reader.error(sql=f"KILL SESSION '{target.id}'"))
    assert target.scalar(sql="SELECT 1") == 1
    assert reader.scalar(sql="SELECT 2") == 2
    reader.close()
    target.close()


def test_rbac_commands_follow_connection_mode(launcher, store):
    server = launcher.start(env=rbac_env(store_uri=store.new_database()))
    reader = server.admin()
    prepared = reader.prepare(sql="CREATE ROLE property_prepared_role")
    response = reader.set_options(options={"read_only": True})
    assert response.status_code == 200, response.text
    assert_read_only(reader.error(sql="CREATE ROLE property_direct_role"))
    response = reader.execute_prepared(prepared_id=prepared["prepared_id"])
    assert_read_only(response.json())
    reader.query(sql="SHOW GRANTS")
    reader.query(sql="SELECT * FROM hardcharger_roles()")
    writer = server.admin()
    writer.execute(sql="CREATE ROLE property_writer_role")
    writer.execute(sql="DROP ROLE property_writer_role")


def test_server_owned_profiling_remains_available(launcher):
    server = launcher.start(args=["--enable-instrumentation", "--capture-query-profile", "standard"])
    reader = server.client().new_session(options={"read_only": True})
    assert reader.scalar(sql="SELECT 42") == 42
    # Server-owned profiling must still turn off before credential-bearing reads,
    # then resume for ordinary queries on the same read-only connection.
    rows = reader.rows(sql="SELECT current_setting('enable_profiling') AS mode, "
                           "struct_pack(token := 'readonly-profile-fixture') AS credentials")
    assert rows[0]["mode"] is None
    prepared = reader.prepare(sql="SELECT struct_pack(token := 'readonly-prepared-profile-fixture')")
    result = reader.execute_prepared(prepared_id=prepared["prepared_id"])
    assert result.status_code == 200, result.text
    assert_read_only(reader.error(sql="CREATE TABLE forbidden_profile_write (n INTEGER)"))
    assert reader.scalar(sql="SELECT 43") == 43
    assert "readonly-profile-fixture" not in server.log()
    assert "readonly-prepared-profile-fixture" not in server.log()


def test_builtin_index_view_and_computed_metadata_arguments(property_server):
    writer = property_server.admin()
    writer.execute(sql="CREATE TABLE property_index_columns (a INTEGER, b INTEGER, PRIMARY KEY (a, b))")
    writer.execute(sql="CREATE INDEX property_index_ba ON property_index_columns (b, a)")
    reader = property_server.client().new_session(options={"read_only": True})
    assert reader.rows(sql='SELECT "ORDINAL_POSITION", "COLUMN_NAME" '
                           'FROM _hardcharger_system.main.hardcharger_index_info '
                           "WHERE \"TABLE_NAME\" = 'property_index_columns' AND \"INDEX_NAME\" <> 'property_index_ba' ORDER BY 1") == [
        {"ORDINAL_POSITION": 1, "COLUMN_NAME": "a"},
        {"ORDINAL_POSITION": 2, "COLUMN_NAME": "b"},
    ]
    assert reader.rows(sql='SELECT "ORDINAL_POSITION", "COLUMN_NAME" '
                           'FROM _hardcharger_system.main.hardcharger_index_info '
                           "WHERE \"INDEX_NAME\" = 'property_index_ba' ORDER BY 1") == [
        {"ORDINAL_POSITION": 1, "COLUMN_NAME": "b"},
        {"ORDINAL_POSITION": 2, "COLUMN_NAME": "a"},
    ]
    assert reader.scalar(sql="SELECT count(*) FROM pragma_table_info('property_' || 'index_columns')") == 2
    assert_read_only(reader.error(sql="SET secret_directory = '/unused-readonly-path'"))
    assert reader.scalar(sql="SELECT 42") == 42
    reader.close()
    writer.close()


# ---- As an ordinary user, with access control on ----
#
# The tests above run as the unrestricted administrator. A read-only session's own statements
# (its READ ONLY transactions, the definitions its screen reads) run on a connection that
# access control checks for everyone else, and the refusals must come before any effect
# whoever asks. Each test here runs as a store user with a handful of grants, and as the
# administrator of the same server.

READER_PASSWORD = "reader-Password-123456"


class Controlled:
    """A server under access control: `reader` holds SELECT on a table and two views, INSERT on
    the table, and may use the sequence; `admin` is the administrator."""

    def __init__(self, launcher, store):
        self.rbac = start_rbac_server(launcher=launcher, store_uri=store.new_database())
        self.admin = self.rbac.admin()
        for sql in ("CREATE SEQUENCE ro_ids", "CREATE TABLE ro_orders (id BIGINT, item VARCHAR)",
                    "INSERT INTO ro_orders VALUES (1, 'a'), (2, 'b'), (3, 'c')",
                    "CREATE VIEW ro_orders_view AS SELECT id FROM ro_orders WHERE id > 1",
                    "CREATE ROLE ro_reader_role", f"CREATE USER ro_reader WITH PASSWORD '{READER_PASSWORD}'",
                    "GRANT ro_reader_role TO ro_reader", "GRANT SELECT ON ro_orders TO ro_reader_role",
                    "GRANT INSERT ON ro_orders TO ro_reader_role", "GRANT SELECT ON ro_orders_view TO ro_reader_role",
                    "GRANT USAGE ON SEQUENCE ro_ids TO ro_reader_role",
                    # setval() resets the sequence, which needs DROP SEQUENCE on its schema: the
                    # read-write twin below may then move it with setval() too (2.13.4 checks a
                    # sequence call DuckDB evaluates while binding as the plan check would).
                    "GRANT DROP SEQUENCE ON SCHEMA main TO ro_reader_role"):
            self.admin.run(sql=sql)

    def session(self, who: str, read_only: bool):
        token = self.admin.token if who == "administrator" else self.rbac.password_token(user="ro_reader", password=READER_PASSWORD)
        return self.rbac.client(token=token, options={"read_only": True} if read_only else None)

    def sequence(self):
        """The sequence's last value, as the administrator's own session reads it."""
        return self.admin.run(sql="SELECT last_value FROM system.main.duckdb_sequences() WHERE sequence_name = 'ro_ids'").to_pylist()[0]["last_value"]

    def rows(self) -> int:
        return self.admin.run(sql="SELECT count(*) AS n FROM ro_orders").to_pylist()[0]["n"]


@pytest.fixture
def controlled(launcher, store):
    return Controlled(launcher=launcher, store=store)


def refusal(session, sql: str, parameters=None, prepare: bool = False) -> ChargewireError:
    with pytest.raises(ChargewireError) as caught:
        if prepare:
            session.prepare(sql=sql)
        else:
            session.run(sql=sql, parameters=parameters)
    return caught.value


# Direct requests in a read-only session, during prepare and execution.
BOUND = [
    "EXPLAIN CALL range({f})", "EXPLAIN (FORMAT JSON) CALL range({f})",
    "EXPLAIN INSERT INTO ro_orders SELECT range, 'x' FROM range({f})",
    "EXPLAIN UPDATE ro_orders SET item = 'q' FROM range({f}) r WHERE ro_orders.id = r.range",
    "EXPLAIN DELETE FROM ro_orders USING range({f}) r WHERE ro_orders.id = r.range",
    "EXPLAIN MERGE INTO ro_orders USING range({f}) s ON ro_orders.id = s.range WHEN MATCHED THEN DELETE",
    "EXPLAIN PREPARE ro_never AS SELECT * FROM range({f})",
    "WITH c AS (INSERT INTO ro_orders SELECT range, 'x' FROM range({f}) RETURNING *) SELECT * FROM c",
    "SELECT * FROM ro_orders PIVOT (count(*) FOR (SELECT item || count(*) FROM range({f})) IN ('a1'))",
    "SELECT json_serialize_plan('SELECT * FROM range({q})')",
    "SELECT * FROM range({f})",  # (the control: refused before 2.13.0 was fixed too)
]


@pytest.mark.parametrize("function", ["nextval('ro_ids')", "setval('ro_ids', 4242)"])
@pytest.mark.parametrize("who", ["administrator", "ordinary user"])
def test_nothing_is_evaluated_while_binding_whoever_asks(controlled, who, function):
    reader = controlled.session(who=who, read_only=True)
    assert controlled.sequence() is None
    for template in BOUND:
        sql = template.format(f=function, q=function.replace("'", "''"))
        for prepare in (False, True):  # sent to run, and only prepared (never executed)
            problem = refusal(reader, sql, prepare=prepare)
            assert (problem.status, problem.code, problem.sqlstate) == (403, "read_only", "25006"), (sql, prepare, problem)
            assert controlled.sequence() is None, (sql, prepare)
    # A file is the administrator's to write under access control: refused one way or the other, before anything.
    problem = refusal(reader, f"EXPLAIN COPY (SELECT * FROM range({function})) TO 'ro_never_written.csv'")
    assert problem.code in ("read_only", "permission_denied") and controlled.sequence() is None, problem
    # A serialized statement: as a literal, as a bind parameter (what a prepared execution binds), and as a pragma.
    serialized = controlled.admin.run(sql="SELECT json_serialize_sql(?) AS j", parameters=[f"SELECT * FROM range({function})"]).to_pylist()[0]["j"]
    literal = "'" + serialized.replace("'", "''") + "'"
    for sql, parameters in ((f"SELECT * FROM json_execute_serialized_sql({literal})", None), ("SELECT * FROM json_execute_serialized_sql(?)", [serialized]),
                            (f"PRAGMA json_execute_serialized_sql({literal})", None)):
        # (to an ordinary user access control refuses the function itself, before the read-only screen is asked)
        refusals = [(403, "read_only", "25006")] if who == "administrator" else [(403, "read_only", "25006"), (403, "permission_denied", "42501")]
        problem = refusal(reader, sql, parameters=parameters)
        assert (problem.status, problem.code, problem.sqlstate) in refusals, (sql, problem)
        assert controlled.sequence() is None, sql
        problem = refusal(reader, sql, prepare=True)
        assert (problem.status, problem.code, problem.sqlstate) in refusals and controlled.sequence() is None, (sql, problem)
    # The twins, in the same read-only session: what binds nothing of the kind is answered.
    assert reader.run(sql="SELECT count(*) AS n FROM ro_orders").to_pylist() == [{"n": 3}]
    assert reader.run(sql="SELECT id FROM ro_orders_view ORDER BY id").to_pylist() == [{"id": 2}, {"id": 3}]
    for sql in ("EXPLAIN SELECT * FROM ro_orders", "EXPLAIN SELECT * FROM ro_orders_view", "EXPLAIN CALL range(3)", "SELECT * FROM range(3)",
                "PRAGMA table_info('ro_orders')", "SHOW TABLES", "SELECT json_serialize_sql('SELECT 1')"):
        assert reader.run(sql=sql).num_rows >= 1, sql
    assert reader.execute_prepared(prepared_id=reader.prepare(sql="SELECT count(*) AS n FROM ro_orders WHERE id > ?"), parameters=[1]).to_pylist() == [{"n": 2}]
    assert reader.metadata(kind="tables").num_rows >= 1
    # What changes a catalog is refused under an EXPLAIN as without one, with plain values too.
    assert refusal(reader, "EXPLAIN INSERT INTO ro_orders VALUES (9, 'x')").code == "read_only"
    assert refusal(reader, "INSERT INTO ro_orders VALUES (9, 'x')").code == "read_only"
    assert controlled.rows() == 3 and controlled.sequence() is None
    # And the same principal's read-write session: the statements are ones it may run, and
    # binding them moves the sequence (so it is the read-only session that kept it still).
    writer = controlled.session(who=who, read_only=False)
    assert writer.run(sql="EXPLAIN INSERT INTO ro_orders VALUES (9, 'x')").num_rows >= 1  # a plan, nothing inserted
    writer.run(sql=f"EXPLAIN CALL range({function})")
    moved = controlled.sequence()
    assert moved is not None
    writer.prepare(sql="SELECT 1")  # (a prepare that binds nothing of the kind)
    assert controlled.rows() == 3
    assert controlled.admin.run(sql="SELECT nextval('ro_ids') AS n").to_pylist()[0]["n"] == moved + 1


@pytest.mark.parametrize("who", ["administrator", "ordinary user"])
def test_a_session_with_a_temporary_sequence_is_not_made_read_only_and_a_read_only_one_has_none(controlled, who):
    session = controlled.session(who=who, read_only=False)
    session.run(sql="CREATE TEMP SEQUENCE ro_temp_ids")
    assert session.run(sql="SELECT nextval('ro_temp_ids') AS n").to_pylist() == [{"n": 1}]
    answer = session.http.patch(url=session.url, headers=session.headers(), json={"options": {"read_only": True}}).json()
    assert answer["errors"]["read_only"]["code"] == "invalid_option_value" and "temporary sequence" in answer["errors"]["read_only"]["detail"], answer
    assert answer["options"]["read_only"] is False
    assert session.run(sql="SELECT nextval('ro_temp_ids') AS n").to_pylist() == [{"n": 2}]  # untouched, and still the session's to use
    session.run(sql="DROP SEQUENCE ro_temp_ids")
    answer = session.http.patch(url=session.url, headers=session.headers(), json={"options": {"read_only": True}}).json()
    assert answer["errors"] == {} and answer["options"]["read_only"] is True, answer
    # None can be made now, and a statement that names one where DuckDB evaluates while binding
    # is refused before DuckDB looks the name up (403, not "no such sequence").
    assert refusal(session, "CREATE TEMP SEQUENCE ro_temp_ids").code == "read_only"
    for sql in ("EXPLAIN CALL range(nextval('ro_temp_ids'))", "EXPLAIN INSERT INTO ro_orders SELECT range, 'x' FROM range(setval('ro_temp_ids', 9))",
                "SELECT * FROM range(nextval('ro_temp_ids'))"):
        for prepare in (False, True):
            problem = refusal(session, sql, prepare=prepare)
            assert (problem.status, problem.code) == (403, "read_only"), (sql, prepare, problem)
    assert session.run(sql="SELECT count(*) AS n FROM system.main.duckdb_sequences() WHERE temporary").to_pylist() == [{"n": 0}]


@pytest.mark.parametrize("between", [{"autocommit": False}, {"schema": "main"}, {"session_tag": {"app": "pool"}}, {"no_such_option": 1}])
@pytest.mark.parametrize("who", ["administrator", "ordinary user"])
def test_an_option_between_the_work_and_the_switch_does_not_hide_the_work(controlled, who, between):
    # 2.13.0: {"autocommit": false} sent again to a session in manual commit made the server
    # forget the INSERT, accept the switch, roll the INSERT back, and answer 204 to the commit.
    session = controlled.session(who=who, read_only=False)

    def patch(options):
        response = session.http.patch(url=session.url, headers=session.headers(), json={"options": options})
        assert response.status_code == 200, response.text
        return response.json()

    assert patch({"autocommit": False})["errors"] == {}
    session.run(sql="INSERT INTO ro_orders VALUES (4, 'd')")
    patch(between)
    answer = patch({"read_only": True})
    assert answer["errors"]["read_only"]["code"] == "invalid_option_value", answer
    assert "commit or roll back first" in answer["errors"]["read_only"]["detail"], answer
    assert answer["options"]["read_only"] is False
    assert controlled.rows() == 3  # not committed by any of it either
    assert session.http.post(url=f"{session.url}/commit", headers=session.headers()).status_code == 204
    assert controlled.rows() == 4
    # The twin: with nothing pending, the same option and then the switch are accepted.
    patch(between)
    answer = patch({"read_only": True})
    assert answer["errors"] == {} and answer["options"]["read_only"] is True, answer
    assert refusal(session, "INSERT INTO ro_orders VALUES (5, 'e')").code == "read_only"
    assert controlled.rows() == 4



def test_read_only_session_reads_a_view_after_its_definition_changes(controlled):
    admin = controlled.admin
    admin.run(sql="CREATE VIEW ro_totals AS SELECT sum(id) AS total FROM ro_orders")
    admin.run(sql="GRANT SELECT ON ro_totals TO ro_reader_role")
    reader = controlled.session(who="ordinary user", read_only=True)
    assert reader.run(sql="SELECT total FROM ro_totals").to_pylist() == [{"total": 6}]
    admin.run(sql="CREATE OR REPLACE VIEW ro_totals AS SELECT count(*) AS total FROM ro_orders")
    assert reader.run(sql="SELECT total FROM ro_totals").to_pylist() == [{"total": 3}]


@pytest.mark.parametrize("definition", ["SELECT * FROM range(nextval('ro_ids'))", "SELECT * FROM range(setval('ro_ids', 4242))"])
def test_a_view_the_reader_holds_no_privilege_on_moves_nothing(controlled, definition):
    # The reader may use the sequence and read one table; the administrator owns a view over
    # the sequence that the reader has no grant on, so access control hides its definition
    # from the reader's listing. 2.13.0 answered 403 permission_denied after the sequence had moved.
    controlled.admin.run(sql=f"CREATE VIEW ro_next_ids AS {definition}")  # (creating it binds it once, in the administrator's session)
    before = controlled.sequence()
    reader = controlled.session(who="ordinary user", read_only=True)
    assert reader.run(sql="SELECT count(*) AS n FROM ro_orders").to_pylist() == [{"n": 3}]
    for prepare in (False, True):
        problem = refusal(reader, "SELECT * FROM ro_next_ids", prepare=prepare)
        assert problem.status == 403 and problem.code in ("permission_denied", "read_only"), problem
        assert controlled.sequence() == before, prepare
    # A grant made after the session's first statement: the view is the reader's to read now,
    # and reading it would move the sequence. Refused as read_only, nothing moved.
    controlled.admin.run(sql="GRANT SELECT ON ro_next_ids TO ro_reader_role")
    before = controlled.sequence()  # (the GRANT is the administrator's statement)
    for sql in ("SELECT * FROM ro_next_ids", "SELECT count(*) FROM ro_next_ids", "SELECT id FROM ro_orders WHERE id IN (SELECT * FROM ro_next_ids)"):
        problem = refusal(reader, sql)
        assert (problem.status, problem.code, problem.sqlstate) == (403, "read_only", "25006"), (sql, problem)
        assert controlled.sequence() == before, sql
        try:  # prepared: refused then, or accepted and refused when executed
            prepared = reader.prepare(sql=sql)
        except ChargewireError as refused_at_prepare:
            assert refused_at_prepare.code == "read_only", refused_at_prepare
        else:
            with pytest.raises(ChargewireError) as caught:
                reader.execute_prepared(prepared_id=prepared)
            assert caught.value.code == "read_only", caught.value
        assert controlled.sequence() == before, "prepared " + sql
    # The twins: the reader still reads what it could, and the same principal's read-write
    # session reads the view, which moves the sequence.
    assert reader.run(sql="SELECT id FROM ro_orders_view ORDER BY id").to_pylist() == [{"id": 2}, {"id": 3}]
    writer = controlled.session(who="ordinary user", read_only=False)
    assert writer.run(sql="SELECT count(*) AS n FROM ro_next_ids").num_rows == 1  # answered, where the reader was refused
    if "nextval" in definition:
        assert controlled.sequence() == before + 1


def test_a_view_named_like_one_of_duckdbs_own_stops_nobody_elses_connection(controlled):
    # `tables`, `columns`, `views` are views of DuckDB's own and ordinary names for a schema of
    # metadata. A user who may create views in one schema makes them there, with a definition
    # a read-only connection may not run itself (a table function over a function call). No
    # other user's read-only connection is answered differently for it: the administrator's,
    # a reader's that can see the schema, one opened before and one opened after.
    admin = controlled.admin
    for sql in ("CREATE SCHEMA ro_shared", "CREATE TABLE ro_shared.facts AS SELECT 1 AS i", "CREATE ROLE ro_maker",
                f"CREATE USER ro_mallory WITH PASSWORD '{READER_PASSWORD}'", "GRANT ro_maker TO ro_mallory",
                "GRANT CREATE VIEW, DROP VIEW ON SCHEMA ro_shared TO ro_maker", "GRANT SELECT ON SCHEMA ro_shared TO ro_reader_role"):
        admin.run(sql=sql)
    before = [controlled.session(who=who, read_only=True) for who in ("ordinary user", "administrator")]
    for session in before:
        assert session.run(sql="SELECT 1 AS one").to_pylist() == [{"one": 1}]
    mallory = controlled.rbac.client(token=controlled.rbac.password_token(user="ro_mallory", password=READER_PASSWORD))
    for view in ("tables", "columns", "views"):
        mallory.run(sql=f"CREATE VIEW ro_shared.{view} AS SELECT * FROM unnest(string_split('a,b', ','))")
    after = [controlled.session(who=who, read_only=True) for who in ("ordinary user", "administrator")]
    for session in before + after:
        assert session.run(sql="SELECT 1 AS one").to_pylist() == [{"one": 1}]
        assert session.run(sql="SELECT count(*) AS n FROM ro_orders").to_pylist() == [{"n": 3}]
        assert session.run(sql="SELECT count(*) AS n FROM ro_shared.facts").to_pylist() == [{"n": 1}]
        assert session.run(sql="SELECT count(*) AS n FROM range(?)", parameters=[2]).to_pylist() == [{"n": 2}]
        for sql in ("SHOW TABLES", "SELECT count(*) FROM information_schema.tables", "SELECT count(*) FROM information_schema.columns",
                    "PRAGMA table_info('ro_orders')", "EXPLAIN SELECT 1"):
            assert session.run(sql=sql).num_rows >= 1, sql
        assert session.metadata(kind="tables").num_rows >= 1
        assert session.execute_prepared(prepared_id=session.prepare(sql="SELECT count(*) AS n FROM ro_orders")).to_pylist() == [{"n": 3}]
        # The view itself is a view like any other.
        assert session.run(sql="SELECT count(*) AS n FROM ro_shared.tables").to_pylist() == [{"n": 2}]
    assert controlled.rows() == 3 and controlled.sequence() is None
