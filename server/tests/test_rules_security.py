"""Regression tests for holes a security review found in rules-only access control
(catalog_access rules without an RBAC store), and on plain servers.

A token principal that is not the administrator holds no system privilege, as a
GizmoSQL 1.x non-administrator: no files or URLs (also through query()), no secrets, no
extensions, no server-wide or credential settings, no table function outside the
allow-list, no catalog it may not see (also through DESCRIBE or sequences). Every
statement's plan is checked, so nested SQL is caught where it runs, not by scanning the
text. Each refusal has a twin that shows the administrator (or the user, in an allowed
catalog) can.

Setup as in the review: licensed catalog_permissions + instrumentation; the
administrator attaches secretdb (table t holding 42 and 'top-secret-value', sequence
seq), creates a temporary S3 secret and sets s3_secret_access_key globally; mallory
(role user) has catalog_access [{maindb: read}, {pubdb: write}, {*: none}].
"""

import pytest


RULES = [{"catalog": "maindb", "access": "read"}, {"catalog": "pubdb", "access": "write"},
         {"catalog": "*", "access": "none"}]
SECRET = "top-secret-value"


@pytest.fixture(scope="module")
def secret_file(tmp_path_factory):
    path = tmp_path_factory.mktemp("files") / "secret.csv"
    path.write_text("word\nfile-secret-word\n")
    return path


def setup(admin, directory):
    for sql in [f"ATTACH '{directory}/secret.db' AS secretdb",
                f"CREATE TABLE secretdb.main.t AS SELECT 42 AS n, '{SECRET}' AS s",
                "CREATE SEQUENCE secretdb.main.seq",
                "ATTACH ':memory:' AS maindb", "CREATE TABLE maindb.main.facts AS SELECT 1 AS id",
                "CREATE SEQUENCE maindb.main.seq",
                "ATTACH ':memory:' AS pubdb", "CREATE TABLE pubdb.main.notes (id INTEGER)",
                "CREATE SEQUENCE pubdb.main.seq",
                "CREATE TEMPORARY SECRET s3_admin (TYPE s3, KEY_ID 'admin-key', SECRET 'admin-secret')",
                "SET GLOBAL s3_secret_access_key = 'global-shh'"]:
        admin.execute(sql=sql)


@pytest.fixture(scope="module")
def server(module_launcher, tmp_path_factory):
    running = module_launcher.start(features=["catalog_permissions", "instrumentation"],
                                    args=["--enable-instrumentation"])
    setup(admin=running.admin(), directory=tmp_path_factory.mktemp("dbs"))
    return running


@pytest.fixture
def admin(server):
    return server.admin()


@pytest.fixture
def mallory(server):
    return server.as_role(role="user", subject="mallory", catalog_access=RULES)


def refused(session, sql: str, *why: str) -> str:
    problem = session.error(sql=sql)
    assert problem["code"] == "permission_denied", f"{sql}: {problem}"
    for words in why:
        assert words in problem["detail"], f"{sql}: {problem['detail']}"
    return problem["detail"]


# ---- 1. nested SQL in query() ----

def test_query_cannot_read_files(server, admin, mallory, secret_file):
    sql = f"SELECT * FROM query('SELECT * FROM read_text(''{secret_file}'')')"
    refused(mallory, sql, "Hardcharger blocked", "query()")
    assert "file-secret-word" in admin.scalar(sql=f"SELECT content FROM ({sql})")
    sql = f"SELECT * FROM query('SELECT * FROM read_csv(''{secret_file}'')')"
    refused(mallory, sql, "Hardcharger blocked", "query()")
    assert admin.scalar(sql=sql) == "file-secret-word"


def test_query_cannot_list_secrets(admin, mallory):
    sql = "SELECT * FROM query('SELECT name, secret_string FROM duckdb_secrets()')"
    refused(mallory, sql, "Hardcharger blocked", "query()")
    assert "s3_admin" in admin.query(sql=sql).column(0).to_pylist()


def test_direct_file_reads_are_refused(admin, mallory, secret_file):
    refused(mallory, f"SELECT * FROM read_csv('{secret_file}')", "Hardcharger blocked")
    assert admin.scalar(sql=f"SELECT * FROM read_csv('{secret_file}')") == "file-secret-word"


# ---- 2. table functions outside the allow-list ----

def test_destroy_external_resource_is_refused(admin, mallory):
    sql = "SELECT * FROM destroy_external_resource('query', 'SELECT 1')"
    refused(mallory, sql, "Hardcharger blocked", "destroy_external_resource")
    admin.query(sql=sql)


# ---- 3. storage and metadata functions of a catalog the user may not see ----

@pytest.mark.parametrize("sql", ["SELECT * FROM pragma_storage_info('secretdb.main.t')",
                                 "SELECT * FROM pragma_metadata_info('secretdb')"])
def test_storage_functions_are_refused(admin, mallory, sql):
    refused(mallory, sql, "Hardcharger blocked")
    admin.query(sql=sql)


# ---- 4. sequences ----

def test_sequences_need_their_catalogs_access(mallory, admin):
    refused(mallory, "SELECT nextval('secretdb.main.seq')", "access to catalog 'secretdb'")
    refused(mallory, "SELECT currval('secretdb.main.seq')", "access to catalog 'secretdb'")
    refused(mallory, "SELECT nextval('maindb.main.seq')", "You do not have write access to catalog 'maindb'")
    assert mallory.scalar(sql="SELECT nextval('pubdb.main.seq')") == 1
    assert mallory.scalar(sql="SELECT currval('pubdb.main.seq')") == 1
    assert admin.scalar(sql="SELECT nextval('secretdb.main.seq')") == 1


# ---- 5. credential settings ----

def test_credential_settings_stay_hidden(admin, mallory):
    refused(mallory, "SELECT current_setting('s3_secret_access_key')", "Hardcharger blocked", "MANAGE CONFIG")
    rows = mallory.rows(sql="SELECT name, value FROM duckdb_settings() WHERE name = 's3_secret_access_key'")
    assert all("global-shh" not in str(row["value"]) for row in rows), rows
    assert admin.scalar(sql="SELECT current_setting('s3_secret_access_key')") == "global-shh"
    assert "global-shh" in str(admin.rows(sql="SELECT value FROM duckdb_settings() "
                                              "WHERE name = 's3_secret_access_key'"))


def test_settings_writes_are_refused(mallory):
    refused(mallory, "SET GLOBAL s3_secret_access_key = 'mine'", "Permission denied")


# ---- 6. DESCRIBE ----

def test_describe_of_a_hidden_catalog_is_refused(admin, mallory):
    detail = refused(mallory, "DESCRIBE secretdb.main.t", "read access to catalog 'secretdb'")
    assert SECRET not in detail and "column_name" not in detail
    assert mallory.query(sql="DESCRIBE pubdb.main.notes").num_rows == 1
    assert admin.query(sql="DESCRIBE secretdb.main.t").num_rows == 2


# ---- what the user may do ----

def test_the_user_keeps_its_rights(mallory):
    assert mallory.scalar(sql="SELECT count(*) FROM maindb.main.facts") == 1
    mallory.execute(sql="INSERT INTO pubdb.main.notes VALUES (7)")
    assert mallory.scalar(sql="SELECT sum(id) FROM pubdb.main.notes") >= 7
    refused(mallory, "INSERT INTO maindb.main.facts VALUES (2)", "You do not have write access to catalog 'maindb'")
    refused(mallory, "SELECT * FROM secretdb.main.t", "read access to catalog 'secretdb'")
    assert mallory.query(sql="PIVOT (SELECT * FROM (VALUES ('a', 1), ('b', 2)) v(k, x)) ON k USING sum(x)").num_rows == 1
    assert {"maindb", "pubdb"} <= set(mallory.metadata(kind="catalogs").column(0).to_pylist())
    assert mallory.scalar(sql="SELECT GIZMOSQL_USER()") == "mallory"
    assert mallory.scalar(sql="SELECT count(*) FROM range(3)") == 3


def test_admin_with_rules_keeps_its_privileges(server, secret_file):
    boss = server.as_role(role="admin", subject="boss", catalog_access=[{"catalog": "*", "access": "write"}])
    assert boss.scalar(sql=f"SELECT * FROM read_csv('{secret_file}')") == "file-secret-word"
    assert boss.scalar(sql="SELECT current_setting('s3_secret_access_key')") == "global-shh"


# ---- plain servers: no store, no license ----

@pytest.fixture(scope="module")
def plain(module_launcher, tmp_path_factory):
    running = module_launcher.start()
    admin = running.admin()
    admin.execute(sql="CREATE TABLE facts AS SELECT 1 AS id")
    admin.execute(sql="CREATE TEMPORARY SECRET s3_admin (TYPE s3, KEY_ID 'admin-key', SECRET 'admin-secret')")
    return running


def test_plain_server_users_cannot_nest_file_reads(plain, secret_file):
    user = plain.as_role(role="user", subject="plain-user")
    for sql in [f"SELECT * FROM query('SELECT * FROM read_text(''{secret_file}'')')",
                "SELECT * FROM query('SELECT name, secret_string FROM duckdb_secrets()')",
                "SELECT * FROM destroy_external_resource('query', 'SELECT 1')"]:
        refused(user, sql, "Hardcharger blocked")
    assert user.scalar(sql="SELECT count(*) FROM facts") == 1
    user.execute(sql="CREATE TABLE mine AS SELECT 2 AS id")
    assert plain.admin().scalar(sql=f"SELECT count(*) FROM query('SELECT * FROM read_text(''{secret_file}'')')") == 1


# ---- nested SQL, table-function wrappers and macros (second review) ----
# An ordinary temp file, never /etc/passwd: the sensitive-path guard would stop that one
# and hide a real hole.

def nested_cases(path) -> list[str]:
    return [
        "SELECT * FROM query('SELECT name FROM duckdb_secrets()')",
        f"SELECT * FROM query('SELECT * FROM read_csv(''{path}'')')",
        f"SELECT * FROM query('SELECT * FROM ''{path}''')",
        f"SELECT * FROM query_table('{path}')",
        f"SELECT * FROM json_execute_serialized_sql(json_serialize_sql('SELECT * FROM read_csv(''{path}'')'))",
        "SELECT * FROM query('SELECT 1' || '')",  # non-literal SQL
    ]


def macro_cases(path) -> list[str]:
    return [
        f"CREATE TEMP MACRO m_leak() AS TABLE SELECT * FROM read_csv('{path}')",
        "CREATE MACRO q_leak(x) AS TABLE SELECT * FROM query_table(x)",
        f"CREATE TEMP MACRO m_leak2() AS TABLE SELECT * FROM query_table('{path}')",
    ]


def test_rules_user_cannot_reach_files_through_nested_sql(server, admin, mallory, secret_file):
    for sql in nested_cases(path=secret_file):
        refused(mallory, sql, "Hardcharger blocked")
        # Admin twin: the same statement runs and sees the file word.
        text = repr(admin.query(sql=sql).to_pylist())
        assert "file-secret-word" in text or "s3_admin" in text or sql.endswith("|| '')"), sql


def test_rules_user_cannot_define_table_macros(server, admin, mallory, secret_file):
    for sql in macro_cases(path=secret_file):
        refused(mallory, sql, "Hardcharger blocked")
    # The administrator may; a non-admin still cannot call an admin's file-reading macro.
    admin.execute(sql=f"CREATE MACRO shared_leak() AS TABLE SELECT * FROM read_csv('{secret_file}')")
    refused(mallory, "SELECT * FROM shared_leak()", "Hardcharger blocked")
    assert "file-secret-word" in repr(admin.query(sql="SELECT * FROM shared_leak()").to_pylist())
    admin.execute(sql="DROP MACRO TABLE shared_leak")


def test_plain_server_non_admin_cannot_reach_files_through_nested_sql(plain, secret_file):
    user = plain.as_role(role="user", subject="plain-nested")
    for sql in nested_cases(path=secret_file) + macro_cases(path=secret_file):
        refused(user, sql, "Hardcharger blocked")
    # The admin of the same plain server can.
    assert "file-secret-word" in repr(
        plain.admin().query(sql=f"SELECT * FROM query('SELECT * FROM read_csv(''{secret_file}'')')").to_pylist())
