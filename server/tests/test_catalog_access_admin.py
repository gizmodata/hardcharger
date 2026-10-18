"""catalog_access rules on an administrator's token, as GizmoSQL 1.x applies them.

1.x's GetCatalogAccess (catalog_permissions_handler.cpp) applies a token's catalog_access
rules whatever its role: the role matters only for the server's own catalogs
(instrumentation, logs), which an administrator may read and no one may write. 1.x's
test_catalog_access.cpp covers the administrator only there
(AdminCanReadInstrumentationWithToken, AdminCannotWriteInstrumentationWithToken; ported
in test_catalog_access.py); the cases below are the rest of what 1.x does with an
administrator's rules, and Hardcharger's deliberate deviations, each named.

- An administrator token WITH rules is restricted by them: reads and writes per catalog,
  temp included, and what it sees (SHOW DATABASES, duckdb_databases(), metadata).
- An administrator WITHOUT rules (the password administrator, or a token without the
  claim) is unrestricted.
- A restricted administrator keeps the administration that reaches no data, as in 1.x:
  SET GLOBAL, CHECKPOINT, KILL SESSION, every session in hardcharger_sessions(), reading
  the server's own catalogs. Deviations from 1.x, for security, because each reaches data
  around the rules: no ATTACH/DETACH, files, extensions, secrets, EXPORT/IMPORT; no
  macros or types; only the table functions other rules principals get; sequences per the
  rules; a claim that isn't a non-empty list grants nothing; and under access control
  (RBAC) the rules narrow an administrator's token too, and it runs no access-control
  statements.
"""

import pyarrow as pa
import pytest

from rbac import mint, start_rbac_server

DEFAULT = "catalog_access_admin"
X, Y = "ca_x", "ca_y"


def write_denied(catalog: str) -> str:
    # 1.x's words ("Access denied: You do not have write access to catalog 'x'."), which
    # Hardcharger follows with whose rules they were.
    return f"Access denied: You do not have write access to catalog '{catalog}'"


def read_denied(catalog: str) -> str:
    return f"Access denied: You do not have read access to catalog '{catalog}'"


def x_read_rest_none():
    return [{"catalog": X, "access": "read"}, {"catalog": "*", "access": "none"}]


@pytest.fixture(scope="module")
def server(module_launcher):
    running = module_launcher.start(args=["--database", f"{DEFAULT}.db", "--enable-instrumentation"])
    admin = running.admin()
    for sql in [f"ATTACH IF NOT EXISTS ':memory:' AS {X}", f"ATTACH IF NOT EXISTS ':memory:' AS {Y}",
                f"CREATE TABLE IF NOT EXISTS {X}.main.t (id INTEGER)", f"CREATE TABLE IF NOT EXISTS {Y}.main.t (id INTEGER)",
                f"INSERT INTO {X}.main.t VALUES (1)", f"INSERT INTO {Y}.main.t VALUES (2)",
                f"CREATE SEQUENCE IF NOT EXISTS {Y}.main.seq", f"CREATE OR REPLACE VIEW {Y}.main.yv AS SELECT 1 AS one"]:
        admin.execute(sql=sql)
    return running


def admin_token(server, name: str, rules=None, **claims):
    """A session of an administrator token (role admin), with these rules if any."""
    if rules is not None:
        claims["catalog_access"] = rules
    return server.as_role(role="admin", subject=name, **claims)


def denied(session, sql: str, why: str) -> str:
    detail = session.error(sql=sql)["detail"]
    assert why in detail, f"{sql}: expected {why!r} in: {detail}"
    return detail


def databases(session) -> set[str]:
    return set(session.query(sql="SELECT database_name FROM duckdb_databases()").column(0).to_pylist())


# ---- Restricted by the rules ----

def test_admin_token_with_rules_cannot_write_a_read_catalog_or_read_a_none_catalog(server):
    session = admin_token(server=server, name="admin_restricted", rules=x_read_rest_none())
    assert session.scalar(sql=f"SELECT id FROM {X}.main.t") == 1  # read is allowed
    denied(session=session, sql=f"INSERT INTO {X}.main.t VALUES (9)", why=write_denied(catalog=X))
    denied(session=session, sql=f"CREATE TABLE {X}.main.made (id INTEGER)", why=write_denied(catalog=X))
    denied(session=session, sql=f"SELECT * FROM {Y}.main.t", why=read_denied(catalog=Y))
    denied(session=session, sql=f"INSERT INTO {Y}.main.t VALUES (9)", why=read_denied(catalog=Y))
    denied(session=session, sql=f"SELECT * FROM {DEFAULT}.main.nothing_here", why=read_denied(catalog=DEFAULT))
    # temp follows the rules too ({*: none}).
    denied(session=session, sql="CREATE TEMP TABLE scratch (id INTEGER)", why=write_denied(catalog="temp"))
    # Nothing was written.
    assert server.admin().scalar(sql=f"SELECT count(*) FROM {X}.main.t WHERE id = 9") == 0


def test_every_path_holds_the_restricted_admin(server):
    session = admin_token(server=server, name="admin_paths", rules=x_read_rest_none())
    # A prepared statement, and a batch.
    response = session.http.post(url=f"{session.url}/prepared", headers=session.headers(),
                                 json={"sql": f"INSERT INTO {X}.main.t VALUES (8)"})
    assert response.status_code == 403 and write_denied(catalog=X) in response.text, response.text
    denied(session=session, sql=f"SELECT 1; INSERT INTO {X}.main.t VALUES (8)", why=write_denied(catalog=X))
    # Bulk ingest.
    response = session.ingest(table=pa.table({"id": [8]}), name="t", mode="append", catalog=X, schema="main")
    assert response.status_code >= 400 and write_denied(catalog=X) in response.text, response.text
    # USE and the session's catalog option.
    denied(session=session, sql=f"USE {Y}", why=read_denied(catalog=Y))
    body = session.set_options(options={"catalog": Y}).json()
    assert read_denied(catalog=Y) in body["errors"]["catalog"]["detail"], body
    assert body["options"]["catalog"] != Y, body
    assert server.admin().scalar(sql=f"SELECT count(*) FROM {X}.main.t WHERE id = 8") == 0


def test_listings_are_filtered_for_a_restricted_admin(server):
    session = admin_token(server=server, name="admin_listing", rules=x_read_rest_none())
    seen = databases(session=session)
    assert X in seen and Y not in seen and DEFAULT not in seen, seen
    shown = set(session.query(sql="SHOW DATABASES").column(0).to_pylist())
    assert X in shown and Y not in shown, shown
    catalogs = set(session.metadata(kind="catalogs").column(0).to_pylist())
    assert X in catalogs and Y not in catalogs, catalogs
    tables = {r["catalog_name"] for r in session.metadata(kind="tables").to_pylist()}
    assert Y not in tables, tables
    # The twin: an unrestricted administrator sees both.
    assert {X, Y, DEFAULT} <= databases(session=server.admin())


@pytest.mark.parametrize("sql", [
    "SELECT table_catalog FROM information_schema.tables",
    'SELECT table_catalog FROM "system"."information_schema"."tables"',
    "SELECT catalog_name FROM information_schema.schemata",
    "SELECT database_name FROM duckdb_tables()",
    "SELECT database_name FROM duckdb_schemas()",
    'SELECT "TABLE_CAT" FROM _hardcharger_system.main.hardcharger_view_definition',
])
def test_catalog_functions_are_filtered_for_a_restricted_admin(server, sql):
    session = admin_token(server=server, name="admin_functions", rules=x_read_rest_none())
    seen = set(session.query(sql=sql).column(0).to_pylist())
    assert Y not in seen and DEFAULT not in seen, (sql, seen)
    # The twin: the unrestricted administrator sees Y there.
    assert Y in set(server.admin().query(sql=sql).column(0).to_pylist()), sql


# ---- Unrestricted without rules ----

def test_admin_token_without_rules_is_unrestricted(server):
    for session in (admin_token(server=server, name="admin_free"),
                    admin_token(server=server, name="admin_all", rules=[{"catalog": "*", "access": "write"}])):
        session.execute(sql=f"INSERT INTO {X}.main.t VALUES (7)")
        assert session.scalar(sql=f"SELECT count(*) FROM {Y}.main.t") >= 1
        session.execute(sql=f"DELETE FROM {X}.main.t WHERE id = 7")
        assert {X, Y} <= databases(session=session)


def test_the_password_admin_is_unaffected(server):
    admin = server.admin()
    admin.execute(sql=f"INSERT INTO {X}.main.t VALUES (6)")
    admin.execute(sql=f"INSERT INTO {Y}.main.t VALUES (6)")
    admin.execute(sql="CREATE TEMP TABLE password_admin_scratch AS SELECT 1 AS id")
    admin.execute(sql="CREATE OR REPLACE MACRO password_admin_m(a) AS a + 1")
    admin.execute(sql=f"DELETE FROM {X}.main.t WHERE id = 6")
    admin.execute(sql=f"DELETE FROM {Y}.main.t WHERE id = 6")


# ---- The server's own catalogs, sessions and KILL (adversarial review, finding 2) ----

def test_a_restricted_admin_cannot_read_the_servers_own_catalogs(server):
    # Unlike 1.x, a restricted administrator does not read the instrumentation/log
    # catalogs: a read exposes every session's SQL text, with literals from catalogs its
    # rules deny. They are hidden from it, as from a non-administrator (it reads as
    # missing). The unrestricted administrator still reads them (the twin below).
    session = admin_token(server=server, name="admin_instr", rules=[{"catalog": "*", "access": "none"}])
    assert "_hardcharger_instr" not in databases(session=session)
    session.error(sql="SELECT * FROM _hardcharger_instr.sessions LIMIT 1")  # a missing-object error
    session.error(sql="SELECT sql_text FROM _hardcharger_instr.sql_statements")
    session.error(sql="DELETE FROM _hardcharger_instr.sessions WHERE 1=0")
    writer = admin_token(server=server, name="admin_instr_w", rules=[{"catalog": "_hardcharger_instr", "access": "write"}])
    writer.error(sql="SELECT * FROM _hardcharger_instr.sessions LIMIT 1")
    # The twin: the unrestricted administrator reads it and the password admin writes nothing.
    assert "_hardcharger_instr" in databases(session=server.admin())
    server.admin().query(sql="SELECT * FROM _hardcharger_instr.sessions LIMIT 1")
    denied(session=server.admin(), sql="DELETE FROM _hardcharger_instr.sessions WHERE 1=0", why="read-only")


def test_a_restricted_admin_sees_only_its_own_session_and_cannot_kill(server):
    # hardcharger_sessions() and KILL SESSION are for the unrestricted administrator.
    other = server.as_role(role="user", subject="bystander")
    other_id = other.scalar(sql="SELECT HARDCHARGER_CURRENT_SESSION()")
    session = admin_token(server=server, name="admin_sessions", rules=x_read_rest_none())
    own = session.scalar(sql="SELECT HARDCHARGER_CURRENT_SESSION()")
    ids = set(session.query(sql="SELECT session_id FROM hardcharger_sessions()").column(0).to_pylist())
    assert ids == {own}, ids
    denied(session=session, sql=f"KILL SESSION '{other_id}'",
           why="KILL SESSION needs an administrator without catalog_access rules")
    # The twin: the unrestricted administrator sees both and could kill.
    full = set(server.admin().query(sql="SELECT session_id FROM hardcharger_sessions()").column(0).to_pylist())
    assert {own, other_id} <= full, full


# ---- Embedded queries can't slip past the rules (adversarial review, finding 1) ----

EMBEDDED = {
    "set_variable": "SET VARIABLE v = (SELECT count(*) FROM {c}.main.t)",
    "prepare": "PREPARE p AS SELECT * FROM {c}.main.t",
    "create_table_as": "CREATE TABLE {x}.main.ctas AS SELECT * FROM {c}.main.t",
    "insert_select": "INSERT INTO {x}.main.t SELECT * FROM {c}.main.t",
    "cte": "WITH q AS (SELECT * FROM {c}.main.t) SELECT count(*) FROM q",
    "scalar_subquery": "SELECT (SELECT count(*) FROM {c}.main.t) AS n",
    "exists": "SELECT 1 WHERE EXISTS (SELECT 1 FROM {c}.main.t)",
    "copy_select": "COPY (SELECT * FROM {c}.main.t) TO 'out.csv'",
    "create_view": "CREATE VIEW {x}.main.leak_v AS SELECT * FROM {c}.main.t",
    "delete_using": "DELETE FROM {x}.main.t WHERE id IN (SELECT id FROM {c}.main.t)",
    "update_from": "UPDATE {x}.main.t SET id = (SELECT count(*) FROM {c}.main.t)",
}


@pytest.mark.parametrize("kind", list(EMBEDDED))
def test_an_embedded_query_cannot_read_a_denied_catalog(server, kind):
    session = admin_token(server=server, name=f"admin_embed_{kind}", rules=x_read_rest_none())
    # COPY ... TO is refused for the write to the filesystem (a restricted-admin deviation)
    # before its SELECT is reached; either refusal keeps the denied catalog unread.
    why = RULES_ADMIN_REFUSAL if kind == "copy_select" else read_denied(catalog=Y)
    denied(session=session, sql=EMBEDDED[kind].format(c=Y, x=X), why=why)
    if kind == "set_variable":
        # getvariable() never carries the secret out, because the SET was refused.
        assert session.scalar(sql="SELECT getvariable('v')") is None


def test_set_variable_without_a_denied_read_still_works(server):
    # The positive twin: SET VARIABLE over an allowed catalog (or a constant) is fine, and
    # getvariable() returns it.
    session = admin_token(server=server, name="admin_setvar_ok", rules=x_read_rest_none())
    session.execute(sql=f"SET VARIABLE n = (SELECT count(*) FROM {X}.main.t)")
    assert session.scalar(sql="SELECT getvariable('n')") >= 1
    session.execute(sql="SET VARIABLE k = 7")
    assert session.scalar(sql="SELECT getvariable('k')") == 7


# ---- What a restricted administrator can still do (1.x), and what it can't ----

RULES_ADMIN_REFUSAL = "which an administrator token with catalog_access rules does not hold"


def test_a_restricted_admin_keeps_the_administration_that_reaches_no_data(server):
    session = admin_token(server=server, name="admin_commands", rules=x_read_rest_none())
    session.execute(sql="SET GLOBAL hardcharger.query_timeout = 0")
    session.execute(sql="SET GLOBAL threads = 4")
    session.execute(sql=f"CHECKPOINT {X}")


def test_a_restricted_admin_holds_no_privilege_that_reaches_data(server, tmp_path):
    # Deviation from 1.x, where the administrator kept these whatever its rules: each one
    # reaches data by a path the rules don't name.
    session = admin_token(server=server, name="admin_no_data_paths", rules=x_read_rest_none())
    for sql, privilege in [
        ("ATTACH ':memory:' AS ca_attached_by_restricted", "MANAGE CATALOGS"),
        (f"DETACH {Y}", "MANAGE CATALOGS"),
        (f"SELECT count(*) FROM read_blob('{DEFAULT}.db')", "READ FILESYSTEM"),
        (f"SELECT * FROM read_csv('{tmp_path / 'any.csv'}')", "READ FILESYSTEM"),
        (f"COPY {X}.main.t TO '{tmp_path / 'x.csv'}'", "WRITE FILESYSTEM"),
        ("LOAD json", "MANAGE EXTENSIONS"),
        ("CREATE SECRET s (TYPE s3, KEY_ID 'k', SECRET 'v')", "MANAGE SECRETS"),
        (f"EXPORT DATABASE {X} TO '{tmp_path / 'export'}'", "EXPORT DATABASE"),
    ]:
        detail = denied(session=session, sql=sql, why=RULES_ADMIN_REFUSAL)
        assert privilege in detail, (sql, detail)
    assert Y in databases(session=server.admin())  # the DETACH did not happen


def test_a_restricted_admin_cannot_reattach_a_denied_file_under_an_allowed_name(launcher, tmp_path):
    # What the refusals above prevent: with ATTACH and file rights, rules by name
    # are no limit (DETACH secret; ATTACH its file AS prod_copy; read it).
    running = launcher.start(args=["--database", "probe.db"])
    running.admin().execute(sql=f"ATTACH '{tmp_path / 'secret.db'}' AS secret")
    running.admin().execute(sql="CREATE TABLE secret.main.t AS SELECT 42 AS v")
    session = admin_token(server=running, name="admin_reattach",
                          rules=[{"catalog": "prod_*", "access": "read"}, {"catalog": "*", "access": "none"}])
    denied(session=session, sql="DETACH secret", why=RULES_ADMIN_REFUSAL)
    denied(session=session, sql=f"ATTACH '{tmp_path / 'secret.db'}' AS prod_copy (READ_ONLY)", why=RULES_ADMIN_REFUSAL)
    denied(session=session, sql=f"SELECT count(*) FROM read_blob('{tmp_path / 'secret.db'}')", why=RULES_ADMIN_REFUSAL)
    # The twin: the unrestricted administrator can.
    admin = running.admin()
    admin.execute(sql="DETACH secret")
    admin.execute(sql=f"ATTACH '{tmp_path / 'secret.db'}' AS prod_copy (READ_ONLY)")
    assert admin.scalar(sql="SELECT v FROM prod_copy.main.t") == 42


def test_deliberate_deviations_from_one_x(server):
    session = admin_token(server=server, name="admin_deviations", rules=x_read_rest_none())
    # A macro's body runs unchecked where it is used: no macros or types.
    denied(session=session, sql="CREATE TEMP MACRO m(a) AS a + 1", why="creating macros needs an administrator")
    # Table functions that read catalogs around the plan check stay out of reach.
    denied(session=session, sql=f"SELECT * FROM pragma_storage_info('{Y}.main.t')", why="pragma_storage_info")
    # A sequence in a catalog the rules deny.
    denied(session=session, sql=f"SELECT nextval('{Y}.main.seq')", why=read_denied(catalog=Y))


def test_a_restricted_admin_cannot_reach_around_its_rules(server, tmp_path):
    admin = server.admin()
    admin.execute(sql=f"CREATE OR REPLACE VIEW {X}.main.v_over_y AS SELECT * FROM {Y}.main.t")
    session = admin_token(server=server, name="admin_around", rules=x_read_rest_none())
    # A view it may read, over a catalog it may not: the view's body is checked too.
    denied(session=session, sql=f"SELECT * FROM {X}.main.v_over_y", why=read_denied(catalog=Y))
    # The plan check can't be turned off.
    denied(session=session, sql="SET enable_optimizer = false", why="changing enable_optimizer is not allowed")


@pytest.mark.parametrize("role", ["admin", "user"])
@pytest.mark.parametrize("claim", ["not-a-list", [], {"catalog": "*", "access": "write"}, None])
def test_a_malformed_claim_grants_nothing(server, claim, role):
    # Present but not a list of rules (or an empty list): fail closed, whatever the role.
    session = server.as_role(role=role, subject="malformed", catalog_access=claim)
    denied(session=session, sql=f"SELECT * FROM {X}.main.t", why=read_denied(catalog=X))


# ---- Under access control (RBAC) ----

@pytest.fixture(scope="module")
def rbac_server(module_launcher, store):
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database())
    admin = server.admin()
    for sql in [f"ATTACH ':memory:' AS {X}", f"ATTACH ':memory:' AS {Y}", f"CREATE TABLE {X}.main.t (id INTEGER)",
                f"CREATE TABLE {Y}.main.t (id INTEGER)", f"INSERT INTO {Y}.main.t VALUES (2)", "CREATE USER IF NOT EXISTS rita"]:
        admin.run(sql=sql)
    return server


def rbac_error(client, sql: str) -> str:
    from rbac import WarpError
    try:
        client.run(sql=sql)
    except WarpError as raised:
        return raised.detail
    pytest.fail(reason=f"{sql} was allowed")


def test_rbac_rules_narrow_an_admin_token(rbac_server):
    session = rbac_server.client(token=mint(user="admin", role="admin", catalog_access=x_read_rest_none()))
    session.run(sql=f"SELECT * FROM {X}.main.t")
    assert write_denied(catalog=X) in rbac_error(client=session, sql=f"INSERT INTO {X}.main.t VALUES (1)")
    assert read_denied(catalog=Y) in rbac_error(client=session, sql=f"SELECT * FROM {Y}.main.t")
    # Finding 1 under RBAC: an embedded query is checked too.
    assert read_denied(catalog=Y) in rbac_error(client=session,
                                                sql=f"SET VARIABLE v = (SELECT count(*) FROM {Y}.main.t)")
    # It can't step outside its rules by managing access control either.
    why = "an administrator token with catalog_access rules cannot run access-control statements"
    assert why in rbac_error(client=session, sql="CREATE USER escape_hatch")
    assert why in rbac_error(client=session, sql=f"GRANT SELECT ON {Y}.main.t TO rita")


def test_rbac_admin_without_rules_is_unrestricted(rbac_server):
    for session in (rbac_server.admin(), rbac_server.client(token=mint(user="admin", role="admin"))):
        session.run(sql=f"INSERT INTO {X}.main.t VALUES (1)")
        session.run(sql=f"SELECT * FROM {Y}.main.t")
        session.run(sql=f"DELETE FROM {X}.main.t")


def test_rbac_rules_never_widen_a_store_user(rbac_server):
    # Grants decide a store user's rights: rules that would allow everything grant nothing.
    rita = rbac_server.user(name="rita", catalog_access=[{"catalog": "*", "access": "write"}])
    detail = rbac_error(client=rita, sql=f"SELECT * FROM {Y}.main.t")
    assert "SELECT" in detail and "rita" in detail, detail
