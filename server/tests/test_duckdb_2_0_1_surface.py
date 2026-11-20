"""What DuckDB 2.0.1 added that the server's checks must know about.

- CREATE EXTENSION REPOSITORY <host>: a repository named by its host, which DuckDB 2.0.1 resolves
  from https://<host>/.well-known/duckdb-extension-repo.json and allows by default
  (allow_extension_repositories = 'hosts'), also spelled as a string; INSTALL x FROM <host>.
  Each is a LOAD statement: the admin guard, access control (MANAGE EXTENSIONS), readonly
  principals and read-only connections refuse every form, as they refused the prefix form.
  Hardcharger keeps DuckDB 2.0.0's rule (scripts/patch-duckdb-extension-repositories.cmake): no
  repository is added, by host or by prefix, the administrator's included, until an administrator
  sets allow_extension_repositories to 'allowed' (or 'hosts_only'); 'forbidden' refuses it for good.
- parquet_page_index(path): reads a Parquet file's page index (each page's min and max), like
  parquet_metadata(): a local file read, refused for a non-admin by the admin guard and needing
  READ FILESYSTEM under access control.
- duckdb_live_query_metrics() (every connection's counters), add_grammar_extension() and
  remove_grammar_extension() (the session's SQL grammar, which SET active_grammar_extensions
  changes and access control refuses): the administrator's, which no EXECUTE grant opens.
"""

import pytest

from rbac import ChargewireError, expect_allowed, expect_blocked, start_rbac_server

ADMIN_ONLY = "which requires the 'admin' role"

# Hosts under .invalid never resolve, so nothing here reaches the network.
REPOSITORY_STATEMENTS = [
    "CREATE EXTENSION REPOSITORY hc.example.invalid",
    "CREATE OR REPLACE EXTENSION REPOSITORY hc.example.invalid",
    "CREATE EXTENSION REPOSITORY IF NOT EXISTS 'hc-repo.example.invalid'",
    "CREATE EXTENSION REPOSITORY hc_repo WITH PREFIX 'https://hc.example.invalid/repo'",
    "DROP EXTENSION REPOSITORY IF EXISTS hc.example.invalid",
    "INSTALL hc_no_such_extension FROM hc.example.invalid",
    "FORCE INSTALL hc_no_such_extension FROM 'https://hc.example.invalid/repo'",
]
GRAMMAR_CALLS = [
    "SELECT * FROM add_grammar_extension('hc_no_such_grammar')",
    "SELECT * FROM remove_grammar_extension('hc_no_such_grammar')",
]


def parquet_file(session, directory) -> str:
    path = f"{directory}/pages.parquet"
    sql = f"COPY (SELECT range AS n FROM range(10)) TO '{path}' (FORMAT parquet)"
    session.run(sql=sql) if hasattr(session, "run") else session.execute(sql=sql)
    return path


# ---- Token roles ----

@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start()


def test_a_non_admin_cannot_add_install_from_or_drop_an_extension_repository(server):
    analyst = server.as_role(role="analyst", subject="repo_analyst")
    for sql in REPOSITORY_STATEMENTS:
        problem = analyst.error(sql=sql)
        assert problem["code"] == "permission_denied" and ADMIN_ONLY in problem["detail"], (sql, problem)
    # Twin: the admin's statement is DuckDB's to answer (nothing exists to drop).
    server.admin().execute(sql="DROP EXTENSION REPOSITORY IF EXISTS hc.example.invalid")


def test_a_non_admin_cannot_read_a_local_parquet_files_page_index(server, tmp_path):
    path = parquet_file(session=server.admin(), directory=tmp_path)
    analyst = server.as_role(role="analyst", subject="pages_analyst")
    problem = analyst.error(sql=f"SELECT * FROM parquet_page_index('{path}')")
    assert problem["code"] == "permission_denied", problem
    assert "parquet_page_index() (local filesystem)" in problem["detail"] and ADMIN_ONLY in problem["detail"], problem
    # Twin: the admin reads it (a file this small has no page index: no rows, no error).
    assert server.admin().rows(sql=f"SELECT * FROM parquet_page_index('{path}')") == []


def test_a_readonly_principal_cannot_touch_extension_repositories_or_the_grammar(server):
    reader = server.as_role(role="readonly", subject="repo_reader")
    for sql in REPOSITORY_STATEMENTS + GRAMMAR_CALLS:
        problem = reader.error(sql=sql)
        assert problem["code"] == "permission_denied", (sql, problem)


def test_a_read_only_connection_cannot_touch_extension_repositories_or_the_grammar(server):
    # The administrator's own connection, read-only: nothing but the read-only guard stands in
    # the way. A grammar call reaching DuckDB would fail on the unknown name instead.
    reader = server.client().new_session(options={"read_only": True})
    try:
        for sql in REPOSITORY_STATEMENTS + GRAMMAR_CALLS:
            problem = reader.error(sql=sql)
            assert problem["code"] == "read_only", (sql, problem)
        # Twin: a read is a read.
        assert reader.scalar(sql="SELECT count(*) FROM duckdb_tables()") >= 0
    finally:
        reader.close()


NOT_YET = "this server adds none until an administrator allows it"


def public_key_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    return key.public_bytes(encoding=serialization.Encoding.PEM,
                            format=serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def test_no_repository_is_added_until_an_administrator_allows_it(server):
    admin = server.admin()
    assert admin.scalar(sql="SELECT current_setting('allow_extension_repositories')") == "hosts"
    for sql in ["CREATE EXTENSION REPOSITORY hc.example.invalid",
                "CREATE EXTENSION REPOSITORY 'hc-repo.example.invalid'",
                "CREATE OR REPLACE EXTENSION REPOSITORY hc.example.invalid"]:
        problem = admin.error(sql=sql)
        assert NOT_YET in problem["detail"] and "allow_extension_repositories = 'allowed'" in problem["detail"], \
            (sql, problem)
    # The prefix form, as before.
    problem = admin.error(sql="CREATE EXTENSION REPOSITORY hc_repo WITH PREFIX 'https://hc.example.invalid/repo'")
    assert NOT_YET in problem["detail"], problem
    # What was there before is unchanged: DROP, and INSTALL from a repository by name or URL.
    admin.execute(sql="DROP EXTENSION REPOSITORY IF EXISTS hc.example.invalid")
    assert admin.rows(sql="SELECT repository_name FROM duckdb_extension_repositories() WHERE repository_name = 'core'")


def test_an_administrator_allows_new_repositories(module_launcher, tmp_path):
    # Repositories are kept under ~/.duckdb/extension_repositories (the setting that moves them is
    # fixed before the instance opens): a home of its own keeps these out of the real one.
    home = {"HOME": str(tmp_path)}
    admin = module_launcher.start(env=home).admin()
    key = public_key_pem().replace("'", "''")
    create = f"CREATE EXTENSION REPOSITORY hc_repo WITH PREFIX 'https://hc.example.invalid/repo' USING PUBLIC KEY '{key}'"
    assert NOT_YET in admin.error(sql=create)["detail"]
    assert NOT_YET in admin.error(sql="CREATE EXTENSION REPOSITORY hc.example.invalid")["detail"]
    admin.execute(sql="SET GLOBAL allow_extension_repositories = 'allowed'")
    admin.execute(sql=create)
    listed = "SELECT prefix FROM duckdb_extension_repositories() WHERE repository_name = 'hc_repo'"
    assert admin.scalar(sql=listed) == "https://hc.example.invalid/repo"
    # The host form now gets past the opt-in, to the host itself (which does not resolve).
    problem = admin.error(sql="CREATE EXTENSION REPOSITORY hc.example.invalid")
    assert NOT_YET not in problem["detail"] and "allow_extension_repositories" not in problem["detail"], problem
    # It cannot be loosened back to the default while the server runs.
    assert "can only be tightened" in admin.error(sql="SET GLOBAL allow_extension_repositories = 'hosts'")["detail"]
    # A repository added once stays after a restart, under the default again, and drops as before.
    again = module_launcher.start(env=home).admin()
    assert again.scalar(sql="SELECT current_setting('allow_extension_repositories')") == "hosts"
    assert again.scalar(sql=listed) == "https://hc.example.invalid/repo"
    again.execute(sql="DROP EXTENSION REPOSITORY hc_repo")
    assert again.rows(sql=listed) == []


def test_startup_sql_can_forbid_extension_repositories(module_launcher):
    locked = module_launcher.start(
        env={"HARDCHARGER_INIT_SQL_COMMANDS": "SET GLOBAL allow_extension_repositories = 'forbidden'"})
    admin = locked.admin()
    assert admin.scalar(sql="SELECT current_setting('allow_extension_repositories')") == "forbidden"
    problem = admin.error(sql="CREATE EXTENSION REPOSITORY hc.example.invalid")
    assert "allow_extension_repositories" in problem["detail"], problem


# ---- Access control ----

@pytest.fixture(scope="module")
def rbac(module_launcher, store, tmp_path_factory):
    work = tmp_path_factory.mktemp("surface_201")
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               args=["--database", str(work / "surface.duckdb")])
    admin = server.admin()
    for sql in ["CREATE ROLE surface_ops", "CREATE USER sue", "GRANT surface_ops TO sue"]:
        expect_allowed(client=admin, sql=sql)
    server.admin_client = admin
    server.dir = work
    return server


def test_every_extension_repository_form_needs_manage_extensions(rbac):
    sue, admin = rbac.user(name="sue"), rbac.admin_client
    for sql in REPOSITORY_STATEMENTS:
        expect_blocked(client=sue, sql=sql, why="MANAGE EXTENSIONS")
    # Twin: with the privilege the statement is DuckDB's to answer.
    expect_allowed(client=admin, sql="GRANT MANAGE EXTENSIONS TO surface_ops")
    try:
        expect_allowed(client=sue, sql="DROP EXTENSION REPOSITORY IF EXISTS hc.example.invalid")
        # MANAGE EXTENSIONS does not add a repository by itself: the administrator's opt-in comes first.
        with pytest.raises(ChargewireError) as raised:
            sue.run(sql="CREATE EXTENSION REPOSITORY hc.example.invalid")
        assert NOT_YET in raised.value.detail, raised.value.detail
    finally:
        expect_allowed(client=admin, sql="REVOKE MANAGE EXTENSIONS FROM surface_ops")


def test_parquet_page_index_needs_read_filesystem(rbac):
    sue, admin = rbac.user(name="sue"), rbac.admin_client
    path = parquet_file(session=admin, directory=rbac.dir)
    sql = f"SELECT * FROM parquet_page_index('{path}')"
    expect_blocked(client=sue, sql=sql, why="READ FILESYSTEM")
    with pytest.raises(ChargewireError) as raised:
        admin.run(sql="GRANT EXECUTE ON TABLE FUNCTION parquet_page_index TO surface_ops")
    assert "reads files: the storage privileges decide" in raised.value.detail, raised.value.detail
    expect_allowed(client=admin, sql="GRANT READ FILESYSTEM TO surface_ops")
    try:
        expect_allowed(client=sue, sql=sql)
    finally:
        expect_allowed(client=admin, sql="REVOKE READ FILESYSTEM FROM surface_ops")


@pytest.mark.parametrize("function", ["duckdb_live_query_metrics", "add_grammar_extension", "remove_grammar_extension"])
def test_instance_wide_and_grammar_functions_stay_the_administrators(rbac, function):
    sue, admin = rbac.user(name="sue"), rbac.admin_client
    sql = f"SELECT * FROM {function}()" if function == "duckdb_live_query_metrics" else \
        f"SELECT * FROM {function}('hc_no_such_grammar')"
    expect_blocked(client=sue, sql=sql, why=f"the table function {function}")
    with pytest.raises(ChargewireError) as raised:
        admin.run(sql=f"GRANT EXECUTE ON TABLE FUNCTION {function} TO surface_ops")
    assert "stays the administrator's" in raised.value.detail, raised.value.detail
    # Twin: a table function no list names takes a grant.
    expect_allowed(client=admin, sql="GRANT EXECUTE ON TABLE FUNCTION pg_timezone_names TO surface_ops")
    expect_allowed(client=admin, sql="REVOKE EXECUTE ON TABLE FUNCTION pg_timezone_names FROM surface_ops")


def test_the_administrator_may_read_live_query_metrics(rbac):
    assert expect_allowed(client=rbac.admin_client, sql="SELECT * FROM duckdb_live_query_metrics()").num_rows > 0


# ---- DROP and DROP ... CASCADE of what other objects depend on, in any schema ----
#
# lou may drop types and sequences in lib. Tables in app depend on some of them: moods and rows
# (the administrator's, which lou cannot see) and tones and counts (lou may SELECT them). A
# refusal names only what lou sees, as the metadata views list it.

CAT = "surface"


@pytest.fixture(scope="module")
def cascade(rbac):
    admin = rbac.admin_client
    for sql in [
        "CREATE SCHEMA lib", "CREATE SCHEMA app",
        "CREATE TYPE lib.mood AS ENUM ('ok')", "CREATE SEQUENCE lib.ids",
        "CREATE TYPE lib.tone AS ENUM ('low')", "CREATE SEQUENCE lib.nums",
        "CREATE TABLE app.moods (m lib.mood)", "CREATE TABLE app.rows (id BIGINT DEFAULT nextval('lib.ids'))",
        "CREATE TABLE app.tones (t lib.tone)", "CREATE TABLE app.counts (id BIGINT DEFAULT nextval('lib.nums'))",
        "CREATE ROLE lib_keepers", "CREATE USER lou", "GRANT lib_keepers TO lou",
        "GRANT DROP TYPE, DROP SEQUENCE, DROP SCHEMA ON SCHEMA lib TO lib_keepers",
        "GRANT SELECT ON app.tones TO lib_keepers", "GRANT SELECT ON app.counts TO lib_keepers",
    ]:
        expect_allowed(client=admin, sql=sql)
    return rbac


def app_tables(admin) -> set[str]:
    rows = expect_allowed(client=admin, sql="SELECT table_name FROM duckdb_tables() WHERE schema_name = 'app'")
    return set(rows.column("table_name").to_pylist())


def refusal(client, sql: str) -> ChargewireError:
    with pytest.raises(ChargewireError) as raised:
        client.run(sql=sql)
    return raised.value


ALL_APP = {"moods", "rows", "tones", "counts"}
NOT_LOU = "which user lou may not drop; ask an administrator. Access control on this server lets user lou do only " \
          "what their roles are granted (see SHOW GRANTS)"


@pytest.mark.parametrize("kind, name", [("type", "mood"), ("sequence", "ids")])
def test_a_drop_blocked_by_objects_the_user_cannot_see_does_not_name_them(cascade, kind, name):
    lou, admin = cascade.user(name="lou"), cascade.admin_client
    plain = refusal(client=lou, sql=f"DROP {kind.upper()} lib.{name}")
    assert plain.detail == (f"Dependency Error: Cannot drop {kind} {CAT}.lib.{name}: other objects depend on it. "
                            "Drop them first, or use DROP ... CASCADE"), plain.detail
    with_cascade = refusal(client=lou, sql=f"DROP {kind.upper()} lib.{name} CASCADE")
    assert with_cascade.code == "permission_denied", with_cascade
    assert with_cascade.detail == (f"Permission Error: Hardcharger blocked this statement: DROP ... CASCADE of {kind} "
                                   f"{CAT}.lib.{name} would also drop objects that depend on it, {NOT_LOU}"), \
        with_cascade.detail
    for detail in (plain.detail, with_cascade.detail):
        assert "app" not in detail and "moods" not in detail and "rows" not in detail, detail
    assert app_tables(admin) == ALL_APP


@pytest.mark.parametrize("kind, name, table", [("type", "tone", "tones"), ("sequence", "nums", "counts")])
def test_a_dependent_the_user_sees_is_named(cascade, kind, name, table):
    lou, admin = cascade.user(name="lou"), cascade.admin_client
    plain = refusal(client=lou, sql=f"DROP {kind.upper()} lib.{name}")
    assert plain.detail == (f"Dependency Error: Cannot drop {kind} {CAT}.lib.{name}: other objects depend on it "
                            f"(table {CAT}.app.{table}). Drop them first, or use DROP ... CASCADE"), plain.detail
    with_cascade = refusal(client=lou, sql=f"DROP {kind.upper()} lib.{name} CASCADE")
    assert with_cascade.detail == (f"Permission Error: Hardcharger blocked this statement: user lou may not drop table "
                                   f"{CAT}.app.{table} (only its owner, or a role granted DROP TABLE). Access control "
                                   "on this server lets user lou do only what their roles are granted (see SHOW GRANTS)"), \
        with_cascade.detail
    assert app_tables(admin) == ALL_APP


def test_drop_schema_names_only_what_the_user_sees(cascade):
    lou, admin = cascade.user(name="lou"), cascade.admin_client
    admin.run(sql="CREATE TABLE lib.hidden_table (i INT)")
    try:
        plain = refusal(client=lou, sql="DROP SCHEMA lib")
        assert plain.detail.startswith(f"Dependency Error: Cannot drop schema {CAT}.lib: other objects depend on it ("), \
            plain.detail
        assert "hidden_table" not in plain.detail and "app" not in plain.detail, plain.detail
        assert f"type {CAT}.lib.mood" in plain.detail and f"sequence {CAT}.lib.ids" in plain.detail, plain.detail
        # With CASCADE the first dependent lou sees and may not drop is named (by name: counts,
        # then tones); moods and rows, which lou cannot see, never are.
        with_cascade = refusal(client=lou, sql="DROP SCHEMA lib CASCADE")
        assert with_cascade.detail == ("Permission Error: Hardcharger blocked this statement: user lou may not drop "
                                       f"table {CAT}.app.counts (only its owner, or a role granted DROP TABLE). Access "
                                       "control on this server lets user lou do only what their roles are granted "
                                       "(see SHOW GRANTS)"), with_cascade.detail
        assert app_tables(admin) == ALL_APP
    finally:
        admin.run(sql="DROP TABLE lib.hidden_table")


def test_the_administrator_gets_duckdbs_own_answer(cascade):
    # Twin: the administrator sees everything, and DuckDB's own message names it all.
    detail = refusal(client=cascade.admin_client, sql="DROP TYPE lib.mood").detail
    assert 'table "moods" depends on type "mood"' in detail, detail


def test_cascade_with_the_right_to_drop_the_dependents(cascade):
    # Twin: with DROP TABLE on app (and still no SELECT on moods or rows), lou's cascades go
    # through and take the tables with them.
    lou, admin = cascade.user(name="lou"), cascade.admin_client
    expect_allowed(client=admin, sql="GRANT DROP TABLE ON SCHEMA app TO lib_keepers")
    expect_allowed(client=lou, sql="DROP TYPE lib.mood CASCADE")
    assert app_tables(admin) == ALL_APP - {"moods"}
    expect_allowed(client=lou, sql="DROP SCHEMA lib CASCADE")
    assert app_tables(admin) == set()


# ---- What a DROP or CREATE OR REPLACE takes through ownership, triggers and foreign keys ----
#
# lou may drop tables, sequences and macros in lib (and create tables, macros and triggers
# there), and sees only lib.lt, lib.parent, lib.parent2, lib.child_seen and what it creates.
# The administrator's objects in app (and lib.child_hidden, lib.t) are hidden from lou.

LATE = "late"


@pytest.fixture(scope="module")
def late(module_launcher, store, tmp_path_factory):
    work = tmp_path_factory.mktemp("late")
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               args=["--database", str(work / "late.duckdb")])
    admin = server.admin()
    for sql in [
        "CREATE SCHEMA lib", "CREATE SCHEMA app",
        # A sequence of app's, OWNED BY lib.lt: dropping lib.lt drops it, and with CASCADE app.payroll.
        "CREATE SEQUENCE app.admin_seq", "CREATE TABLE app.payroll (id BIGINT DEFAULT nextval('app.admin_seq'))",
        "CREATE TABLE lib.lt (i INT)", "ALTER SEQUENCE app.admin_seq OWNED BY lib.lt",
        # A sequence of lib's OWNED BY a hidden table in app.
        "CREATE SEQUENCE lib.s_owned", "CREATE TABLE app.hidden_owner (i INT)",
        "ALTER SEQUENCE lib.s_owned OWNED BY app.hidden_owner",
        # Foreign keys: DuckDB never cascades to a referencing table.
        "CREATE TABLE lib.parent (id INT PRIMARY KEY)", "CREATE TABLE lib.child_hidden (pid INT REFERENCES lib.parent(id))",
        "CREATE TABLE lib.parent2 (id INT PRIMARY KEY)", "CREATE TABLE lib.child_seen (pid INT REFERENCES lib.parent2(id))",
        # CREATE OR REPLACE drops the old entry first.
        "CREATE MACRO lib.m_def() AS 7", "CREATE TABLE app.payroll_def (i INT DEFAULT lib.m_def())",
        "CREATE MACRO lib.m_seen() AS 7", "CREATE TABLE app.seen_def (i INT DEFAULT lib.m_seen())",
        "CREATE TABLE lib.t (i INT)", "CREATE TABLE app.src (i INT)", "CREATE TABLE app.secret_audit (n BIGINT)",
        "CREATE TRIGGER payroll_trg AFTER INSERT ON app.src FOR EACH STATEMENT "
        "INSERT INTO app.secret_audit SELECT count(*) FROM lib.t",
        "CREATE ROLE keepers", "CREATE USER lou", "GRANT keepers TO lou",
        "GRANT CREATE TABLE, DROP TABLE, DROP SCHEMA, DROP SEQUENCE, CREATE MACRO, DROP MACRO, CREATE TRIGGER, "
        "DROP TRIGGER ON SCHEMA lib TO keepers",
        "GRANT SELECT ON lib.lt TO keepers", "GRANT SELECT ON lib.parent TO keepers",
        "GRANT SELECT ON lib.parent2 TO keepers", "GRANT SELECT ON lib.child_seen TO keepers",
        "GRANT SELECT ON app.seen_def TO keepers",
        "CREATE ROLE configurers", "CREATE USER cora", "GRANT configurers TO cora",
        "GRANT MANAGE CONFIG TO configurers",
    ]:
        expect_allowed(client=admin, sql=sql)
    server.admin_client = admin
    return server


def tables_of(admin, schema: str) -> set[str]:
    rows = expect_allowed(client=admin, sql=f"SELECT table_name FROM duckdb_tables() WHERE schema_name = '{schema}'")
    return set(rows.column("table_name").to_pylist())


def sequences_of(admin, schema: str) -> set[str]:
    rows = expect_allowed(client=admin,
                          sql=f"SELECT sequence_name FROM duckdb_sequences() WHERE schema_name = '{schema}'")
    return set(rows.column("sequence_name").to_pylist())


BLOCKED_PREFIX = "Permission Error: Hardcharger blocked this statement: "
LOU_SUFFIX = ". Access control on this server lets user lou do only what their roles are granted (see SHOW GRANTS)"


def quiet_refusal(statement: str) -> str:
    return (f"{BLOCKED_PREFIX}{statement} would also drop objects that depend on it, which user lou may not drop; "
            f"ask an administrator{LOU_SUFFIX}")


def dependency_refusal(verb: str, what: str, seen: str = "", cascade: bool = False) -> str:
    # The suggestion to use CASCADE only where the statement did not.
    tail = ", or use DROP ... CASCADE" if verb == "drop" and not cascade else ""
    return f"Dependency Error: Cannot {verb} {what}: other objects depend on it{seen}. Drop them first{tail}"


def test_a_cascade_through_an_owned_sequence_needs_the_right_to_drop_what_it_takes(late):
    lou, admin = late.user(name="lou"), late.admin_client
    # L1: lib.lt owns app.admin_seq, which app.payroll's default calls.
    assert refusal(client=lou, sql="DROP TABLE lib.lt CASCADE").detail == \
        quiet_refusal(f"DROP ... CASCADE of table {LATE}.lib.lt")
    # The schema's cascade also reaches app.seen_def (through lib.m_seen), which lou sees: named
    # first; app.payroll, behind the owned sequence, never is.
    assert refusal(client=lou, sql="DROP SCHEMA lib CASCADE").detail == \
        (f"{BLOCKED_PREFIX}user lou may not drop table {LATE}.app.seen_def (only its owner, or a role granted DROP "
         f"TABLE){LOU_SUFFIX}")
    # L2: without CASCADE the owned sequence goes with the table, and app.payroll blocks it.
    assert refusal(client=lou, sql="DROP TABLE lib.lt").detail == dependency_refusal("drop", f"table {LATE}.lib.lt")
    # L2: a sequence owned by a hidden table.
    assert refusal(client=lou, sql="DROP SEQUENCE lib.s_owned").detail == \
        dependency_refusal("drop", f"sequence {LATE}.lib.s_owned")
    assert tables_of(admin, "app") >= {"payroll", "hidden_owner"} and "admin_seq" in sequences_of(admin, "app")


def test_foreign_keys_block_a_drop_with_or_without_cascade_and_name_only_what_is_seen(late):
    lou = late.user(name="lou")
    for sql in ["DROP TABLE lib.parent", "DROP TABLE lib.parent CASCADE"]:
        assert refusal(client=lou, sql=sql).detail == \
            dependency_refusal("drop", f"table {LATE}.lib.parent", cascade=sql.endswith("CASCADE")), sql
    # Twin: the referencing table lou sees is named.
    assert refusal(client=lou, sql="DROP TABLE lib.parent2 CASCADE").detail == \
        dependency_refusal("drop", f"table {LATE}.lib.parent2", f" (table {LATE}.lib.child_seen)", cascade=True)


def test_create_or_replace_names_only_the_dependents_the_user_sees(late):
    lou = late.user(name="lou")
    assert refusal(client=lou, sql="CREATE OR REPLACE MACRO lib.m_def() AS 9").detail == \
        dependency_refusal("replace", f"macro {LATE}.lib.m_def")
    assert refusal(client=lou, sql="CREATE OR REPLACE TABLE lib.t (i INT, j INT)").detail == \
        dependency_refusal("replace", f"table {LATE}.lib.t")
    assert refusal(client=lou, sql="CREATE OR REPLACE TABLE lib.lt (i INT)").detail == \
        dependency_refusal("replace", f"table {LATE}.lib.lt")
    # Twin: a dependent lou sees is named.
    assert refusal(client=lou, sql="CREATE OR REPLACE MACRO lib.m_seen() AS 9").detail == \
        dependency_refusal("replace", f"macro {LATE}.lib.m_seen", f" (table {LATE}.app.seen_def)")


def test_an_owner_drops_its_own_table_with_its_own_trigger(late):
    lou, admin = late.user(name="lou"), late.admin_client
    for sql in ["CREATE TABLE lib.mine (i INT)", "CREATE TABLE lib.mine_log (i INT)",
                "CREATE TRIGGER mine_trg AFTER INSERT ON lib.mine FOR EACH STATEMENT INSERT INTO lib.mine_log VALUES (1)"]:
        expect_allowed(client=lou, sql=sql)
    # L3: the trigger goes with its table, and the table its body writes goes with its trigger.
    expect_allowed(client=lou, sql="DROP TABLE lib.mine_log CASCADE")
    expect_allowed(client=lou, sql="DROP TABLE lib.mine CASCADE")
    assert not tables_of(admin, "lib") & {"mine", "mine_log"}


def test_an_administrators_trigger_on_a_hidden_table_is_not_named_or_dropped(late):
    lou, admin = late.user(name="lou"), late.admin_client
    expect_allowed(client=lou, sql="CREATE TABLE lib.lou_target (i INT)")
    for sql in ["CREATE TABLE app.adm_src (i INT)",
                "CREATE TRIGGER adm_trg AFTER INSERT ON app.adm_src FOR EACH STATEMENT INSERT INTO lib.lou_target VALUES (1)"]:
        expect_allowed(client=admin, sql=sql)
    assert refusal(client=lou, sql="DROP TABLE lib.lou_target").detail == \
        dependency_refusal("drop", f"table {LATE}.lib.lou_target")
    assert refusal(client=lou, sql="DROP TABLE lib.lou_target CASCADE").detail == \
        quiet_refusal(f"DROP ... CASCADE of table {LATE}.lib.lou_target")
    # Twin: once lou sees app.adm_src, the trigger is named, and its drop needs DROP TRIGGER on app.
    expect_allowed(client=admin, sql="GRANT SELECT ON app.adm_src TO keepers")
    try:
        assert refusal(client=lou, sql="DROP TABLE lib.lou_target").detail == \
            dependency_refusal("drop", f"table {LATE}.lib.lou_target", f" (trigger adm_trg on table {LATE}.app.adm_src)")
        detail = refusal(client=lou, sql="DROP TABLE lib.lou_target CASCADE").detail
        assert detail == (f"{BLOCKED_PREFIX}user lou may not drop trigger adm_trg on table {LATE}.app.adm_src (only its "
                          f"owner, or a role granted DROP TRIGGER on schema {LATE}.app or its catalog, or owning the "
                          f"schema){LOU_SUFFIX}"), detail
    finally:
        expect_allowed(client=admin, sql="REVOKE SELECT ON app.adm_src FROM keepers")


def test_cascades_go_through_with_the_rights_to_what_they_take(late):
    lou, admin = late.user(name="lou"), late.admin_client
    expect_allowed(client=admin, sql="GRANT DROP TABLE, DROP SEQUENCE ON SCHEMA app TO keepers")
    try:
        expect_allowed(client=lou, sql="DROP TABLE lib.lt CASCADE")
        assert "payroll" not in tables_of(admin, "app") and "admin_seq" not in sequences_of(admin, "app")
    finally:
        expect_allowed(client=admin, sql="REVOKE DROP TABLE, DROP SEQUENCE ON SCHEMA app FROM keepers")


def test_only_the_administrator_changes_allow_extension_repositories(late):
    cora, admin = late.user(name="cora"), late.admin_client
    why = ("allow_extension_repositories decides whether new extension repositories, whose signing keys the server "
           "then trusts, may be added; only an administrator without catalog_access rules may change it")
    for sql, verb in [("SET GLOBAL allow_extension_repositories = 'allowed'", "SET"),
                      ("SET allow_extension_repositories = 'allowed'", "SET"),
                      ("RESET GLOBAL allow_extension_repositories", "RESET")]:
        error = expect_blocked(client=cora, sql=sql, why=f"{verb} {why}")
        assert error.detail == f"Permission Error: {BLOCKED_PREFIX.split(': ', 1)[1]}{verb} {why}. Access control on " \
                               "this server lets user cora do only what their roles are granted (see SHOW GRANTS)", \
            error.detail
    # Twin: MANAGE CONFIG still sets the other global settings, and the administrator this one.
    threads = expect_allowed(client=admin, sql="SELECT current_setting('threads') AS t").column("t")[0].as_py()
    expect_allowed(client=cora, sql=f"SET GLOBAL threads = {threads}")
    expect_allowed(client=admin, sql="SET GLOBAL allow_extension_repositories = 'allowed'")
    assert expect_allowed(client=admin, sql="SELECT current_setting('allow_extension_repositories') AS v") \
        .column("v")[0].as_py() == "allowed"


# ---- An index taken without its table (review L6) ----
#
# A DROP MACRO ... CASCADE takes the indexes whose expressions call the macro, while their
# tables stay. Dropping an index is DROP INDEX's right: the administrator's (as DROP INDEX
# app.x is for lou). lou may create and drop macros and tables in lib, sees
# app.visible_accounts, and owns lib.own_t.

IDX = "idx"


@pytest.fixture(scope="module")
def idx(module_launcher, store, tmp_path_factory):
    work = tmp_path_factory.mktemp("idx")
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               args=["--database", str(work / "idx.duckdb")])
    admin = server.admin()
    for sql in [
        "CREATE SCHEMA lib", "CREATE SCHEMA app",
        "CREATE MACRO lib.norm(x) AS lower(x)", "CREATE MACRO lib.norm2(x) AS upper(x)",
        "CREATE MACRO lib.norm3(x) AS trim(x)",
        "CREATE TABLE app.payroll_accounts (email VARCHAR)",
        "CREATE UNIQUE INDEX payroll_email_uq ON app.payroll_accounts (lib.norm(email))",
        "INSERT INTO app.payroll_accounts VALUES ('a@x.org')",
        "CREATE TABLE app.visible_accounts (email VARCHAR)",
        "CREATE UNIQUE INDEX visible_email_uq ON app.visible_accounts (lib.norm2(email))",
        "INSERT INTO app.visible_accounts VALUES ('b@x.org')",
        "CREATE TABLE app.drop_me (email VARCHAR)", "CREATE INDEX drop_me_ix ON app.drop_me (lib.norm3(email))",
        "CREATE ROLE keepers", "CREATE USER lou", "GRANT keepers TO lou",
        "GRANT CREATE MACRO, DROP MACRO, CREATE TABLE, DROP TABLE ON SCHEMA lib TO keepers",
        "GRANT SELECT ON app.visible_accounts TO keepers",
    ]:
        expect_allowed(client=admin, sql=sql)
    server.admin_client = admin
    return server


def indexes(admin) -> set[str]:
    rows = expect_allowed(client=admin, sql="SELECT index_name FROM duckdb_indexes()")
    return set(rows.column("index_name").to_pylist())


def still_unique(admin, table: str, duplicate: str) -> None:
    with pytest.raises(ChargewireError) as raised:
        admin.run(sql=f"INSERT INTO {table} VALUES ('{duplicate}')")
    assert "Duplicate key" in raised.value.detail or "unique" in raised.value.detail.lower(), raised.value.detail


def test_drop_macro_cascade_does_not_take_an_index_on_a_hidden_table(idx):
    lou, admin = idx.user(name="lou"), idx.admin_client
    assert refusal(client=lou, sql="DROP MACRO lib.norm CASCADE").detail == \
        (f"{BLOCKED_PREFIX}DROP ... CASCADE of macro {IDX}.lib.norm would also drop objects that depend on it, which "
         f"user lou may not drop; ask an administrator{LOU_SUFFIX}")
    assert "payroll_email_uq" in indexes(admin)
    still_unique(admin=admin, table="app.payroll_accounts", duplicate="A@X.ORG")


def test_drop_macro_cascade_names_an_index_on_a_visible_table_and_keeps_it(idx):
    lou, admin = idx.user(name="lou"), idx.admin_client
    assert refusal(client=lou, sql="DROP MACRO lib.norm2 CASCADE").detail == \
        (f"{BLOCKED_PREFIX}DROP ... CASCADE of macro {IDX}.lib.norm2 would also drop index {IDX}.app.visible_email_uq, "
         f"which only an administrator may drop (as DROP INDEX){LOU_SUFFIX}")
    assert "visible_email_uq" in indexes(admin)
    still_unique(admin=admin, table="app.visible_accounts", duplicate="b@X.org")


def test_an_owner_drops_its_table_with_its_index_but_not_the_index_alone(idx):
    lou, admin = idx.user(name="lou"), idx.admin_client
    for sql in ["CREATE MACRO lib.norm4(x) AS lower(x)", "CREATE TABLE lib.own_t (email VARCHAR)",
                "CREATE INDEX own_ix ON lib.own_t (lib.norm4(email))"]:
        expect_allowed(client=lou, sql=sql)
    # The index alone, through its macro: DROP INDEX's right, the owner's as everyone's.
    assert refusal(client=lou, sql="DROP MACRO lib.norm4 CASCADE").detail == \
        (f"{BLOCKED_PREFIX}DROP ... CASCADE of macro {IDX}.lib.norm4 would also drop index {IDX}.lib.own_ix, which only "
         f"an administrator may drop (as DROP INDEX){LOU_SUFFIX}")
    assert "own_ix" in indexes(admin)
    # Twin: the index goes with its own table.
    expect_allowed(client=lou, sql="DROP TABLE lib.own_t")
    assert "own_ix" not in indexes(admin)


def test_a_table_dropped_with_the_right_takes_its_index(idx):
    lou, admin = idx.user(name="lou"), idx.admin_client
    expect_allowed(client=admin, sql="GRANT DROP TABLE ON SCHEMA app TO keepers")
    try:
        expect_allowed(client=lou, sql="DROP TABLE app.drop_me CASCADE")
        assert "drop_me_ix" not in indexes(admin)
    finally:
        expect_allowed(client=admin, sql="REVOKE DROP TABLE ON SCHEMA app FROM keepers")
    # Twin: the administrator's own DROP MACRO ... CASCADE takes the index, as DuckDB does.
    expect_allowed(client=admin, sql="DROP MACRO lib.norm3 CASCADE")
