"""The functions a row or masking policy may call work in plain SQL, with the same answers.

is_role_active(name), session_attribute(name), current_user, session_user, user and
hardcharger_user() answer for the session in a plain SELECT (a view, a macro) exactly as
they do inside a policy; current_role() is the role SET ROLE made active, or NULL. Each
answer is worked out when the statement runs, never kept in a prepared plan; neither
is_role_active nor session_attribute says whether a role (or attribute) exists. Without
access control, every session answers as the administrator: every role active, no
attributes.
"""

import pytest

from rbac import expect_allowed, expect_blocked, expect_error, start_rbac_server

ISSUER = "https://idp.example.test"

# Each masked column of ctx shows what its masking policy's context call answered.
MASKS = {
    "who": "current_user",
    "who_called": "current_user()",
    "sess": "session_user",
    "usr": "user",
    "hc": "hardcharger_user()",
    "hr": "CAST(is_role_active('hr') AS VARCHAR)",
    "staff": "CAST(is_role_active('STAFF') AS VARCHAR)",
    "held_not": "CAST(is_role_active('secret_role') AS VARCHAR)",
    "ghost": "CAST(is_role_active('no_such_role') AS VARCHAR)",
    "region": "session_attribute('region')",
    "unset": "session_attribute('nope')",
}
# The same calls in plain SQL.
PLAIN = "SELECT " + ", ".join(f"{call} AS {column}" for column, call in MASKS.items())
IN_POLICY = "SELECT " + ", ".join(MASKS) + " FROM ctx"


@pytest.fixture(scope="module")
def server(module_launcher, store):
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database())
    admin = server.admin()
    setup = [
        "CREATE TABLE ctx (" + ", ".join(f"{c} VARCHAR" for c in MASKS) + ")",
        "INSERT INTO ctx VALUES (" + ", ".join("'raw'" for _ in MASKS) + ")",
        "GRANT SELECT ON ctx TO PUBLIC",
        "CREATE ROLE staff", "CREATE ROLE hr", "CREATE ROLE secret_role",
        "CREATE USER hal", "CREATE USER nora", "CREATE USER gina", "CREATE USER ann",
        "GRANT staff, hr TO hal", "GRANT staff TO ann",
        "ALTER USER hal SET ATTRIBUTE region = 'US'", "ALTER USER ann SET ATTRIBUTE region = 'EU'",
        f"CREATE GROUP MAPPING 'hr-team' ISSUER '{ISSUER}' TO ROLE hr",
        # docs: who sees what, decided by the same calls.
        "CREATE TABLE docs (id INT, region VARCHAR)",
        "INSERT INTO docs VALUES (1, 'EU'), (2, 'US'), (3, 'APAC')",
        "CREATE TABLE docs_raw AS SELECT * FROM docs",
        "GRANT SELECT ON docs TO PUBLIC", "GRANT SELECT ON docs_raw TO PUBLIC",
        "CREATE ROW POLICY by_region ON docs USING (region = session_attribute('region') OR is_role_active('hr'))",
        "CREATE VIEW whoami AS SELECT current_user AS u, is_role_active('hr') AS hr, current_role() AS r",
        "GRANT SELECT ON whoami TO PUBLIC",
    ]
    for column, call in MASKS.items():
        setup.append(f"CREATE MASKING POLICY show_{column} AS (v VARCHAR) RETURNS VARCHAR -> {call}")
        setup.append(f"ALTER TABLE ctx MODIFY COLUMN {column} SET MASKING POLICY show_{column}")
    for sql in setup:
        expect_allowed(client=admin, sql=sql)
    server.admin_client = admin
    return server


def plain(client) -> dict:
    return expect_allowed(client=client, sql=PLAIN).to_pylist()[0]


def in_policy(client) -> dict:
    return expect_allowed(client=client, sql=IN_POLICY).to_pylist()[0]


def answers(user: str, hr: bool, staff: bool, region: str | None) -> dict:
    return {"who": user, "who_called": user, "sess": user, "usr": user, "hc": user, "hr": str(hr).lower(),
            "staff": str(staff).lower(), "held_not": "false", "ghost": "false", "region": region, "unset": None}


@pytest.mark.parametrize(("who", "expected"), [
    ("hal", answers(user="hal", hr=True, staff=True, region="US")),        # roles and an attribute
    ("ann", answers(user="ann", hr=False, staff=True, region="EU")),       # a role, an attribute
    ("nora", answers(user="nora", hr=False, staff=False, region=None)),    # no roles, no attributes
    ("gina", answers(user="gina", hr=True, staff=False, region=None)),     # a token whose group maps to hr
])
def test_the_same_answer_in_a_policy_and_in_plain_sql(server, who, expected):
    claims = {"idp": {"iss": ISSUER, "groups": ["hr-team"]}} if who == "gina" else {}
    session = server.user(name=who, **claims)
    assert in_policy(client=session) == expected
    assert plain(client=session) == expected
    assert expect_allowed(client=session, sql="SELECT current_role() AS a, current_role AS b").to_pylist() == [
        {"a": None, "b": None}]


def test_the_administrator_has_every_role_active_and_no_attributes(server):
    admin = server.admin_client
    expected = {**answers(user="admin", hr=True, staff=True, region=None), "held_not": "true", "ghost": "true"}
    assert plain(client=admin) == expected
    assert expect_allowed(client=admin, sql="SELECT current_role() AS r").to_pylist() == [{"r": None}]
    # Policies don't apply to the administrator: ctx's raw values.
    assert set(in_policy(client=admin).values()) == {"raw"}


def test_set_role_changes_the_answer_at_the_next_statement_prepared_ones_included(server):
    hal = server.user(name="hal")
    calls = "SELECT CAST(is_role_active('hr') AS VARCHAR) AS hr, current_role() AS r, current_role AS r2"
    prepared_plain = hal.prepare(sql=calls)
    prepared_policy = hal.prepare(sql="SELECT hr, staff FROM ctx")
    with_parameter = hal.prepare(sql="SELECT is_role_active(?) AS active")

    def now() -> tuple:
        return (hal.execute_prepared(prepared_id=prepared_plain).to_pylist()[0],
                hal.execute_prepared(prepared_id=prepared_policy).to_pylist()[0],
                hal.execute_prepared(prepared_id=with_parameter, parameters=["hr"]).to_pylist()[0])

    everything = ({"hr": "true", "r": None, "r2": None}, {"hr": "true", "staff": "true"}, {"active": True})
    assert now() == everything
    expect_allowed(client=hal, sql="SET ROLE staff")
    staff_only = ({"hr": "false", "r": "staff", "r2": "staff"}, {"hr": "false", "staff": "true"}, {"active": False})
    assert now() == staff_only
    assert expect_allowed(client=hal, sql=calls).to_pylist()[0] == staff_only[0]
    assert expect_allowed(client=hal, sql="SELECT count(*) AS n FROM docs").to_pylist() == [{"n": 1}]  # US only now
    expect_allowed(client=hal, sql="SET ROLE hr")
    assert now()[0] == {"hr": "true", "r": "hr", "r2": "hr"}
    expect_allowed(client=hal, sql="RESET ROLE")
    assert now() == everything
    assert expect_allowed(client=hal, sql="SELECT count(*) AS n FROM docs").to_pylist() == [{"n": 3}]


def test_a_store_change_reaches_the_next_statement_prepared_ones_included(server):
    admin = server.admin_client
    expect_allowed(client=admin, sql="CREATE USER pia")
    pia = server.user(name="pia")
    prepared = pia.prepare(sql="SELECT CAST(is_role_active('hr') AS VARCHAR) AS hr, session_attribute('region') AS region")
    assert pia.execute_prepared(prepared_id=prepared).to_pylist() == [{"hr": "false", "region": None}]
    expect_allowed(client=admin, sql="GRANT hr TO pia")
    expect_allowed(client=admin, sql="ALTER USER pia SET ATTRIBUTE region = 'APAC'")
    assert pia.execute_prepared(prepared_id=prepared).to_pylist() == [{"hr": "true", "region": "APAC"}]
    assert in_policy(client=pia)["region"] == "APAC"
    expect_allowed(client=admin, sql="REVOKE hr FROM pia")
    assert pia.execute_prepared(prepared_id=prepared).to_pylist() == [{"hr": "false", "region": "APAC"}]


def test_no_answer_says_whether_a_role_or_attribute_exists(server):
    nora = server.user(name="nora")
    asked = ("SELECT is_role_active('secret_role') AS held_by_others, is_role_active('no_such_role') AS missing, "
             "is_role_active('') AS empty, is_role_active(NULL) AS null_name, session_attribute('region') AS attr, "
             "session_attribute(NULL) AS null_attr")
    assert expect_allowed(client=nora, sql=asked).to_pylist() == [
        {"held_by_others": False, "missing": False, "empty": False, "null_name": None, "attr": None,
         "null_attr": None}]
    # Row by row, over names the caller chose: the same answer for a role that exists and one that doesn't.
    by_row = expect_allowed(client=nora, sql="SELECT name, is_role_active(name) AS a FROM (VALUES ('hr'), "
                                             "('no_such_role'), ('PUBLIC')) t(name) ORDER BY name").to_pylist()
    assert by_row == [{"name": "PUBLIC", "a": True}, {"name": "hr", "a": False}, {"name": "no_such_role", "a": False}]


def test_policies_still_filter_exactly_as_before(server):
    # hr sees every row; an attribute picks its region; neither: nothing. The same rows as
    # the policy's predicate over an unprotected copy, in plain SQL.
    for who, expected in (("hal", [1, 2, 3]), ("ann", [1]), ("nora", []),):
        session = server.user(name=who)
        filtered = [r["id"] for r in expect_allowed(client=session, sql="SELECT id FROM docs ORDER BY id").to_pylist()]
        same = [r["id"] for r in expect_allowed(
            client=session, sql="SELECT id FROM docs_raw WHERE region = session_attribute('region') OR "
                                "is_role_active('hr') ORDER BY id").to_pylist()]
        assert filtered == same == expected, who


def test_views_answer_for_the_reader(server):
    for who, hr in (("hal", True), ("nora", False)):
        assert expect_allowed(client=server.user(name=who), sql="SELECT u, hr, r FROM whoami").to_pylist() == [
            {"u": who, "hr": hr, "r": None}]


@pytest.mark.parametrize(("sql", "why"), [
    ("CREATE ROW POLICY r1 ON docs USING (current_role() = 'hr')", "a policy cannot call current_role()"),
    ("CREATE ROW POLICY r2 ON docs USING (current_role = 'hr')", "a policy cannot call current_role()"),
    ("CREATE ROW POLICY r3 ON docs USING (hardcharger_active_role() = 'hr')",
     "a policy cannot call hardcharger_active_role()"),
    ("CREATE MASKING POLICY m1 AS (v VARCHAR) RETURNS VARCHAR -> current_role()", "a policy cannot call current_role()"),
])
def test_current_role_stays_out_of_policies(server, sql, why):
    expect_error(call=lambda: server.admin_client.run(sql=sql), code="sql_error", why=why)


def test_a_macro_in_a_policy_answers_for_the_policys_principal(server):
    admin = server.admin_client
    for sql in ["CREATE TABLE notes (id INT, region VARCHAR)",
                "INSERT INTO notes VALUES (1, 'EU'), (2, 'US')",
                "CREATE MACRO main.my_region(r) AS r = session_attribute('region') OR is_role_active('hr')",
                "CREATE MACRO main.whose_role() AS current_role()",
                "GRANT SELECT ON notes TO PUBLIC",
                "CREATE ROW POLICY via_macro ON notes USING (main.my_region(region))",
                "CREATE SCHEMA app", "CREATE ROLE builders", "CREATE USER mia",
                "GRANT CREATE TABLE, CREATE TRIGGER, SELECT, INSERT ON SCHEMA app TO builders", "GRANT builders TO mia",
                "ALTER USER mia SET ATTRIBUTE region = 'EU'", "GRANT INSERT ON SCHEMA app TO staff"]:
        expect_allowed(client=admin, sql=sql)
    hal, ann = server.user(name="hal"), server.user(name="ann")
    assert [r["id"] for r in expect_allowed(client=hal, sql="SELECT id FROM notes ORDER BY id").to_pylist()] == [1, 2]
    assert [r["id"] for r in expect_allowed(client=ann, sql="SELECT id FROM notes ORDER BY id").to_pylist()] == [1]
    # mia's trigger counts the notes she sees (EU only), whoever writes: hal sees both.
    mia = server.user(name="mia")
    for sql in ["CREATE TABLE app.feed (id INT)", "CREATE TABLE app.counted (n BIGINT)",
                "CREATE TRIGGER count_notes AFTER INSERT ON app.feed FOR EACH STATEMENT "
                "INSERT INTO app.counted SELECT count(*) FROM main.notes"]:
        expect_allowed(client=mia, sql=sql)
    expect_allowed(client=hal, sql="INSERT INTO app.feed VALUES (1)")
    assert expect_allowed(client=admin, sql="SELECT n FROM app.counted").to_pylist() == [{"n": 1}]
    # A macro can't carry current_role() into a policy either.
    expect_allowed(client=admin, sql="CREATE ROW POLICY role_macro ON notes AS RESTRICTIVE USING "
                                     "(main.whose_role() IS NULL)")
    expect_blocked(client=hal, sql="SELECT id FROM notes",
                   why="a row or masking policy on a table it reads calls hardcharger_active_role()")
    expect_allowed(client=admin, sql="DROP ROW POLICY role_macro ON notes")
    assert [r["id"] for r in expect_allowed(client=hal, sql="SELECT id FROM notes ORDER BY id").to_pylist()] == [1, 2]


def test_a_user_macro_cannot_take_their_names(server):
    admin = server.admin_client
    for sql in ["CREATE SCHEMA tools", "CREATE ROLE macro_makers", "GRANT CREATE MACRO ON SCHEMA tools TO macro_makers",
                "CREATE USER max", "GRANT macro_makers TO max"]:
        expect_allowed(client=admin, sql=sql)
    max_ = server.user(name="max")
    for name in ("is_role_active", "session_attribute", "current_user", "current_role"):
        expect_blocked(client=max_, sql=f"CREATE MACRO tools.{name}(x) AS true",
                       why=f"would replace the built-in function {name}")
    expect_allowed(client=max_, sql="CREATE MACRO tools.is_role_active_too(x) AS true")  # the twin


def test_without_access_control_every_session_answers_as_the_administrator(launcher):
    running = launcher.start()
    session = running.admin()
    row = session.query(sql=PLAIN + ", current_role() AS r").to_pylist()[0]
    assert row == {**answers(user=running.username, hr=True, staff=True, region=None), "held_not": "true",
                   "ghost": "true", "r": None}
    # No roles to choose from: SET ROLE says why.
    with pytest.raises(Exception) as refused:
        session.query(sql="SET ROLE staff")
    assert "Hardcharger access control is not enabled on this server, so SET ROLE is not available" in \
        str(refused.value), refused.value


def test_outside_a_client_session_they_answer_nothing(launcher, tmp_path):
    # The init SQL runs on the server's own connection, which no session binds.
    init = tmp_path / "init.sql"
    init.write_text("CREATE TABLE boot AS SELECT current_user AS u, is_role_active('x') AS a, "
                    "session_attribute('x') AS s;")
    running = launcher.start(args=["--init-sql-commands-file", str(init)])
    assert running.admin().query(sql="SELECT u, a, s FROM boot").to_pylist() == [{"u": None, "a": None, "s": None}]
