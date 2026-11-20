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


# ---- DROP ... CASCADE drops what depends on the object, in any schema ----

@pytest.fixture(scope="module")
def cascade(rbac):
    admin = rbac.admin_client
    for sql in [
        "CREATE SCHEMA lib", "CREATE SCHEMA app",
        "CREATE TYPE lib.mood AS ENUM ('ok')", "CREATE SEQUENCE lib.ids",
        "CREATE TABLE app.moods (m lib.mood)", "CREATE TABLE app.rows (id BIGINT DEFAULT nextval('lib.ids'))",
        "CREATE ROLE lib_keepers", "CREATE USER lou", "GRANT lib_keepers TO lou",
        "GRANT DROP TYPE, DROP SEQUENCE, DROP SCHEMA ON SCHEMA lib TO lib_keepers",
    ]:
        expect_allowed(client=admin, sql=sql)
    return rbac


def app_tables(admin) -> set[str]:
    rows = expect_allowed(client=admin, sql="SELECT table_name FROM duckdb_tables() WHERE schema_name = 'app'")
    return set(rows.column("table_name").to_pylist())


@pytest.mark.parametrize("sql, table", [
    ("DROP TYPE lib.mood CASCADE", "app.moods"),
    ("DROP SEQUENCE lib.ids CASCADE", "app.rows"),
    ("DROP SCHEMA lib CASCADE", "app."),
])
def test_cascade_needs_the_right_to_drop_what_depends_on_it(cascade, sql, table):
    lou, admin = cascade.user(name="lou"), cascade.admin_client
    expect_blocked(client=lou, sql=sql, why=f"may not drop table")
    assert app_tables(admin) == {"moods", "rows"}
    # Without CASCADE DuckDB refuses the drop while anything depends on it.
    with pytest.raises(ChargewireError) as raised:
        lou.run(sql=sql.removesuffix(" CASCADE"))
    assert "depend" in raised.value.detail, raised.value.detail
    assert app_tables(admin) == {"moods", "rows"}


def test_cascade_with_the_right_to_drop_the_dependents(cascade):
    # Twin: with DROP TABLE on app, lou's cascades go through, and take the tables with them.
    lou, admin = cascade.user(name="lou"), cascade.admin_client
    expect_allowed(client=admin, sql="GRANT DROP TABLE ON SCHEMA app TO lib_keepers")
    expect_allowed(client=lou, sql="DROP TYPE lib.mood CASCADE")
    assert app_tables(admin) == {"rows"}
    expect_allowed(client=lou, sql="DROP SCHEMA lib CASCADE")
    assert app_tables(admin) == set()
