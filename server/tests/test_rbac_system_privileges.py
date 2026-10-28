"""System privileges (GRANT READ FILESYSTEM TO role, ...): each opens one family of
statements that access control otherwise keeps for the administrator."""

import pytest

from rbac import expect_allowed, expect_blocked, start_rbac_server


@pytest.fixture(scope="module")
def server(module_launcher, store, tmp_path_factory):
    work = tmp_path_factory.mktemp("system")
    # The tests' secrets stay out of the host's DuckDB profile.
    secrets_dir = work / "secrets"
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               args=["--database", str(work / "rbac_sys.duckdb")],
                               env={"HARDCHARGER_INIT_SQL_COMMANDS":
                                    f"SET secret_directory = '{secrets_dir}'; INSTALL httpfs; LOAD httpfs; "
                                    "SET http_proxy_password = 'proxy-pw-123';"})
    admin = server.admin()
    for sql in [
        "CREATE TABLE t AS SELECT 1 AS id",
        "CREATE TABLE hidden AS SELECT 'secret' AS s",
        f"COPY (SELECT 42 AS n) TO '{work}/in.parquet' (FORMAT parquet)",
        "CREATE ROLE fs_reader", "CREATE ROLE fs_writer", "CREATE ROLE catalogs", "CREATE ROLE config",
        "CREATE ROLE checkpointer", "CREATE ROLE secrets", "CREATE ROLE extensions", "CREATE ROLE readers",
        "CREATE ROLE ops",
        "GRANT READ FILESYSTEM TO fs_reader", "GRANT CREATE VIEW ON SCHEMA main TO fs_reader",
        "GRANT WRITE FILESYSTEM TO fs_writer", "GRANT SELECT ON t TO fs_writer",
        "GRANT MANAGE CATALOGS TO catalogs", "GRANT MANAGE CONFIG TO config",
        "GRANT CHECKPOINT TO checkpointer", "GRANT MANAGE SECRETS TO secrets",
        "GRANT MANAGE EXTENSIONS TO extensions", "GRANT SELECT ON main.t TO ops",
        "CREATE USER fay", "CREATE USER wes", "CREATE USER cam", "CREATE USER con", "CREATE USER kit",
        "CREATE USER sam", "CREATE USER eli", "CREATE USER nob", "CREATE USER pat",
        "GRANT fs_reader TO fay", "GRANT fs_writer TO wes", "GRANT catalogs TO cam", "GRANT config TO con",
        "GRANT checkpointer TO kit", "GRANT secrets TO sam", "GRANT extensions TO eli", "GRANT readers TO nob",
        "GRANT ops TO pat",
    ]:
        expect_allowed(client=admin, sql=sql)
    server.admin_client = admin
    server.dir = work
    return server


@pytest.fixture
def admin(server):
    return server.admin_client


def test_each_statement_family_needs_its_privilege(server, admin):
    pat = server.user(name="pat")
    work = server.dir

    def check(privilege: str, sql: str) -> None:
        # Refused without the privilege, naming it; allowed once pat's role holds it.
        expect_blocked(client=pat, sql=sql, why=f"needs the {privilege}")
        expect_allowed(client=admin, sql=f"GRANT {privilege} TO ops")
        try:
            expect_allowed(client=pat, sql=sql)
        finally:
            expect_allowed(client=admin, sql=f"REVOKE {privilege} FROM ops")

    check("READ FILESYSTEM", f"SELECT * FROM read_parquet('{work}/in.parquet')")
    check("READ FILESYSTEM", f"SELECT * FROM '{work}/in.parquet'")
    check("WRITE FILESYSTEM", f"COPY (SELECT * FROM t) TO '{work}/out.csv'")
    check("MANAGE CATALOGS", "ATTACH ':memory:' AS pat_scratch")
    check("MANAGE CATALOGS", "DETACH pat_scratch")
    check("CHECKPOINT", "CHECKPOINT")
    check("MANAGE SECRETS", "CREATE TEMPORARY SECRET pat_s (TYPE s3, KEY_ID 'k', SECRET 'v')")
    check("MANAGE SECRETS", "SELECT * FROM duckdb_secrets()")
    check("MANAGE SECRETS", "DROP SECRET pat_s")
    check("MANAGE EXTENSIONS", "LOAD parquet")
    check("MANAGE CONFIG", "SET GLOBAL threads = 2")


def test_one_privilege_does_not_cover_another(server):
    wes = server.user(name="wes")
    expect_blocked(client=wes, sql=f"COPY (SELECT * FROM read_parquet('{server.dir}/in.parquet')) "
                                   f"TO '{server.dir}/o2.csv'", why="needs the READ FILESYSTEM")


def test_read_filesystem(server):
    fay, nob = server.user(name="fay"), server.user(name="nob")
    file = f"{server.dir}/in.parquet"
    assert expect_allowed(client=fay, sql=f"SELECT n FROM read_parquet('{file}')").num_rows == 1
    expect_allowed(client=fay, sql=f"SELECT * FROM '{file}'")
    expect_blocked(client=nob, sql=f"SELECT n FROM read_parquet('{file}')", why="read_parquet()")
    expect_blocked(client=nob, sql=f"SELECT * FROM '{file}'", why="reading a path in FROM")
    # Reading files is not writing them, nor reading tables without a grant.
    expect_blocked(client=fay, sql=f"COPY (SELECT 1) TO '{server.dir}/fay.csv'", why="COPY ... TO")
    expect_blocked(client=fay, sql=f"SELECT n, s FROM read_parquet('{file}'), hidden",
                   why="no SELECT privilege on table")
    # Every gated part of a statement must be covered: the file read is, the ATTACH
    # beside it is not.
    expect_blocked(client=fay, sql=f"SELECT * FROM read_parquet('{file}'); ATTACH ':memory:' AS sneaky",
                   why="ATTACH")
    # query() stays the administrator's: a covered file read next to it, in the same
    # statement or the next one, must not carry it through.
    expect_blocked(client=fay, sql=f"SELECT * FROM read_parquet('{file}'), query('SELECT 42 AS x')",
                   why="query()")
    expect_blocked(client=fay, sql=f"SELECT * FROM read_parquet('{file}'); SELECT * FROM query('SELECT 42')",
                   why="query()")


def test_a_file_view_reads_with_its_owners_filesystem_privilege(server, admin):
    fay, nob = server.user(name="fay"), server.user(name="nob")
    expect_allowed(client=fay, sql=f"CREATE VIEW fay_file AS SELECT n FROM read_parquet('{server.dir}/in.parquet')")
    expect_allowed(client=admin, sql="GRANT SELECT ON fay_file TO readers")
    expect_allowed(client=nob, sql="SELECT * FROM fay_file")  # nob has no READ FILESYSTEM himself
    expect_allowed(client=admin, sql="REVOKE READ FILESYSTEM FROM fs_reader")
    expect_blocked(client=nob, sql="SELECT * FROM fay_file", why="its owner lacks a privilege")
    expect_allowed(client=admin, sql="GRANT READ FILESYSTEM TO fs_reader")


def test_write_filesystem(server):
    wes, nob = server.user(name="wes"), server.user(name="nob")
    expect_allowed(client=wes, sql=f"COPY (SELECT * FROM t) TO '{server.dir}/wes.csv'")
    assert (server.dir / "wes.csv").exists()
    expect_blocked(client=wes, sql=f"COPY (SELECT * FROM hidden) TO '{server.dir}/wes2.csv'",
                   why="no SELECT privilege on table")
    expect_blocked(client=nob, sql=f"COPY (SELECT 1) TO '{server.dir}/nob.csv'", why="COPY ... TO")
    # Writing is not reading: a COPY that reads a file needs READ FILESYSTEM too.
    expect_blocked(client=wes, sql=f"COPY (SELECT * FROM read_parquet('{server.dir}/in.parquet')) "
                                   f"TO '{server.dir}/wes3.csv'", why="read_parquet()")


def test_manage_catalogs(server):
    cam, nob = server.user(name="cam"), server.user(name="nob")
    expect_allowed(client=cam, sql="ATTACH ':memory:' AS cam_scratch")
    expect_allowed(client=cam, sql="DETACH cam_scratch")
    expect_blocked(client=nob, sql="ATTACH ':memory:' AS nob_scratch", why="ATTACH")


def test_manage_config(server, admin):
    con, nob = server.user(name="con"), server.user(name="nob")
    value = expect_allowed(client=admin, sql="SELECT current_setting('threads') AS t").to_pylist()[0]["t"]
    expect_allowed(client=con, sql=f"SET GLOBAL threads = {value}")
    expect_allowed(client=con, sql=f"SET threads = {value}")  # global-only, so the same thing
    expect_blocked(client=nob, sql=f"SET GLOBAL threads = {value}", why="SET GLOBAL threads")
    # Not the switches the access check itself depends on.
    expect_blocked(client=con, sql="SET GLOBAL disabled_optimizers = 'filter_pushdown'", why="disabled_optimizers")


def test_checkpoint(server):
    kit, nob = server.user(name="kit"), server.user(name="nob")
    expect_allowed(client=kit, sql="CHECKPOINT")
    expect_blocked(client=nob, sql="CHECKPOINT", why="CHECKPOINT")


def test_manage_secrets(server):
    sam, nob = server.user(name="sam"), server.user(name="nob")
    expect_allowed(client=sam, sql="CREATE TEMPORARY SECRET sam_secret (TYPE s3, KEY_ID 'k', SECRET 'v')")
    listed = expect_allowed(client=sam, sql="SELECT name FROM duckdb_secrets() WHERE name = 'sam_secret'")
    assert listed.num_rows == 1
    expect_blocked(client=nob, sql="SELECT name FROM duckdb_secrets()", why="duckdb_secrets()")
    expect_blocked(client=nob, sql="DROP SECRET sam_secret", why="DROP SECRET")
    expect_allowed(client=sam, sql="DROP SECRET sam_secret")


def test_manage_extensions(server):
    eli, nob = server.user(name="eli"), server.user(name="nob")
    expect_allowed(client=eli, sql="LOAD parquet")
    expect_blocked(client=nob, sql="LOAD parquet", why="LOAD extension")


def test_show_grants_lists_them(admin):
    rows = expect_allowed(client=admin, sql="SHOW GRANTS FOR fay").to_pylist()
    assert any(row["privilege"] == "READ FILESYSTEM" and row["object_type"] == "SYSTEM" for row in rows), rows


def test_a_refusal_names_the_privilege_that_would_allow_it(server):
    nob = server.user(name="nob")
    expect_blocked(client=nob, sql=f"SELECT * FROM read_parquet('{server.dir}/in.parquet')",
                   why="needs the READ FILESYSTEM system privilege")
    expect_blocked(client=nob, sql="ATTACH ':memory:' AS x", why="needs the MANAGE CATALOGS system privilege")
    # What no privilege covers stays the administrator's, and says so.
    expect_blocked(client=nob, sql="SELECT * FROM query('SELECT 1')", why="available to the administrator only")
    expect_blocked(client=nob, sql="SELECT * FROM create_external_resource('x', 'y')",
                   why="available to the administrator only")


def test_settings_that_hold_credentials_need_manage_config(server):
    nob, con = server.user(name="nob"), server.user(name="con")
    for sql in ["SELECT current_setting('http_proxy_password') AS v",
                # DuckDB folds the argument when it binds the call: so does the check.
                "SELECT current_setting('http_proxy_' || 'password') AS v",
                "SELECT current_setting(lower('HTTP_PROXY_PASSWORD')) AS v",
                "SHOW http_proxy_password"]:
        error = expect_blocked(client=nob, sql=sql, why="needs the MANAGE CONFIG")
        assert "proxy-pw-123" not in error.detail
    # duckdb_settings() leaves them out.
    listed = expect_allowed(client=nob, sql="SELECT name, value FROM duckdb_settings() WHERE name LIKE 'http_proxy%'")
    assert "proxy-pw-123" not in str(listed.to_pylist())
    assert "http_proxy_password" not in listed.column("name").to_pylist()
    assert expect_allowed(client=nob, sql="SELECT count(*) AS n FROM duckdb_settings()").column("n").to_pylist()[0] > 100
    # Positive twins: other settings, and a holder of MANAGE CONFIG.
    expect_allowed(client=nob, sql="SELECT current_setting('threads') AS v")
    assert expect_allowed(client=con, sql="SELECT current_setting('http_proxy_password') AS v").to_pylist() == [
        {"v": "proxy-pw-123"}]
    assert "proxy-pw-123" in str(expect_allowed(
        client=con, sql="SELECT value FROM duckdb_settings() WHERE name = 'http_proxy_password'").to_pylist())
