"""A store user's sign-in token with catalog_access rules uses no system privilege.

Under access control a store user's catalog_access rules grant nothing (the user's grants
decide what it reads), and a session that signed in with them uses none of the system
privileges its roles hold, as a scoped token's session uses none: not READ FILESYSTEM, not
MANAGE CATALOGS, not a policy exemption, everywhere or on a location, nor the admin option to
grant one. Each refusal is asserted word for word next to its twin, the same user signed in
without catalog_access rules.
"""

import functools
import http.server
import threading

import pytest

from rbac import ChargewireError, expect_allowed, expect_blocked, start_rbac_server

RULES = [{"catalog": "*", "access": "write"}]


def refusal(what: str, privilege: str, user: str = "tok") -> str:
    return (f"Hardcharger blocked this statement: {what} needs the {privilege} system privilege, which this session "
            "does not use: it signed in with a token that carries catalog_access rules, and such a session (like a "
            f"scoped token's) uses no system privilege, whatever user {user}'s roles hold. Sign in without "
            "catalog_access rules to use it")


def gate_refusal(what: str, privilege: str, user: str = "tok") -> str:
    return refusal(what=what, privilege=privilege, user=user).replace("this statement: ", "", 1)


@pytest.fixture(scope="module")
def web(tmp_path_factory):
    root = tmp_path_factory.mktemp("web")
    (root / "a.csv").write_text("x\n1\n")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(scope="module")
def server(module_launcher, store, tmp_path_factory):
    work = tmp_path_factory.mktemp("tok")
    server = start_rbac_server(
        launcher=module_launcher, store_uri=store.new_database(),
        args=["--database", str(work / "tok.duckdb")],
        env={"HARDCHARGER_INIT_SQL_COMMANDS": f"SET secret_directory = '{work / 'secrets'}'; INSTALL httpfs; "
                                              "LOAD httpfs; SET http_retries = 0;"})
    admin = server.admin()
    (work / "in").mkdir()
    for sql in [
        "CREATE TABLE t AS SELECT 1 AS id",
        f"COPY (SELECT 42 AS n) TO '{work}/in/a.parquet' (FORMAT parquet)",
        # Object storage that answers nothing: a statement allowed through fails in httpfs.
        "CREATE SECRET nowhere (TYPE s3, KEY_ID 'k', SECRET 's', ENDPOINT '127.0.0.1:1', USE_SSL false, "
        "URL_STYLE 'path', REGION 'us-east-1')",
        "CREATE TABLE emp (id INT, ssn VARCHAR)", "INSERT INTO emp VALUES (1, '111'), (2, '222')",
        "CREATE ROW POLICY none_of_it ON emp USING (false)",
        "CREATE MASKING POLICY hide AS (v VARCHAR) RETURNS VARCHAR -> 'XXX'",
        "ALTER TABLE emp MODIFY COLUMN ssn SET MASKING POLICY hide",
        "CREATE ROLE tools", "CREATE ROLE located", "CREATE ROLE helpers", "CREATE USER tok", "CREATE USER loc",
        "GRANT tools TO tok", "GRANT located TO loc", "GRANT SELECT, INSERT ON t TO tools", "GRANT SELECT, INSERT ON emp TO tools",
        "GRANT CREATE TABLE ON SCHEMA main TO tools",
        "GRANT MANAGE CATALOGS, MANAGE EXTENSIONS, MANAGE SECRETS, READ FILESYSTEM, WRITE FILESYSTEM, "
        "EXPORT DATABASE, IMPORT DATABASE, CHECKPOINT, READ OBJECT STORAGE, WRITE OBJECT STORAGE, READ HTTP, "
        "WRITE HTTP TO tools",
        "GRANT MANAGE CONFIG TO tools WITH ADMIN OPTION",
        "GRANT EXEMPT ROW POLICIES, EXEMPT MASKING POLICIES TO ROLE tools",
        f"GRANT READ FILESYSTEM ON LOCATION '{work}/in/' TO located", "GRANT SELECT ON t TO located",
    ]:
        expect_allowed(client=admin, sql=sql)
    server.admin_client = admin
    server.dir = work
    return server


def sessions(server, user: str = "tok"):
    """The same user with catalog_access rules, and without (the twin)."""
    return server.user(name=user, catalog_access=RULES), server.user(name=user)


def reaches_duckdb(client, sql: str) -> None:
    """Hardcharger let it through: it runs, or fails in DuckDB (an unreachable endpoint)."""
    try:
        client.run(sql=sql)
    except ChargewireError as error:
        assert "Hardcharger blocked" not in error.detail and error.code != "permission_denied", f"{sql}: {error}"


def test_no_system_privilege_family_through_the_gate(server):
    limited, plain = sessions(server=server)
    work = server.dir
    cases = [
        ("MANAGE CATALOGS", "ATTACH", "ATTACH ':memory:' AS tok_scratch", "DETACH tok_scratch"),
        ("MANAGE EXTENSIONS", "LOAD extension", "LOAD parquet", None),
        ("MANAGE SECRETS", "CREATE SECRET", "CREATE TEMPORARY SECRET tok_s (TYPE s3, KEY_ID 'k', SECRET 'v')",
         "DROP SECRET tok_s"),
        ("MANAGE SECRETS", "duckdb_secrets()", "SELECT count(*) FROM duckdb_secrets()", None),
        ("MANAGE CONFIG", "SET GLOBAL threads", "SET GLOBAL threads = 4", None),
        ("CHECKPOINT", "CHECKPOINT", "CHECKPOINT", None),
        ("EXPORT DATABASE", "EXPORT DATABASE", f"EXPORT DATABASE '{work}/export_twin'", None),
        ("IMPORT DATABASE", "IMPORT DATABASE", f"IMPORT DATABASE '{work}/nothing_to_import'", None),
    ]
    for privilege, category, sql, cleanup in cases:
        expect_blocked(client=limited, sql=sql, why=gate_refusal(what=category, privilege=privilege))
        reaches_duckdb(client=plain, sql=sql)
        if cleanup:
            expect_allowed(client=plain, sql=cleanup)


def test_no_storage_privilege_by_path(server, web):
    limited, plain = sessions(server=server)
    work = server.dir
    cases = [
        ("READ FILESYSTEM", f"SELECT * FROM read_parquet('{work}/in/a.parquet')", "read_parquet()",
         f"{work}/in/a.parquet"),
        ("WRITE FILESYSTEM", f"COPY t TO '{work}/out.csv'", "COPY ... TO", f"{work}/out.csv"),
        ("READ OBJECT STORAGE", "SELECT * FROM read_csv('s3://bucket/a.csv')", "read_csv()", "s3://bucket/a.csv"),
        ("WRITE OBJECT STORAGE", "COPY t TO 's3://bucket/out.csv'", "COPY ... TO", "s3://bucket/out.csv"),
        ("READ HTTP", f"SELECT * FROM read_csv('{web}/a.csv')", "read_csv()", f"{web}/a.csv"),
        ("WRITE HTTP", f"COPY t TO '{web}/out.csv'", "COPY ... TO", f"{web}/out.csv"),
    ]
    for privilege, sql, what, path in cases:
        error = expect_blocked(client=limited, sql=sql, why=f"needs the {privilege} system privilege, which this "
                                                            "session does not use: it signed in with a token that "
                                                            "carries catalog_access rules")
        assert path in error.detail, error.detail
        reaches_duckdb(client=plain, sql=sql)
    assert expect_allowed(client=plain, sql=f"SELECT * FROM read_csv('{web}/a.csv')").to_pylist() == [{"x": 1}]


def test_no_location_grant_either(server):
    limited, plain = sessions(server=server, user="loc")
    sql = f"SELECT n FROM read_parquet('{server.dir}/in/a.parquet')"
    expect_blocked(client=limited, sql=sql, why="system privilege, which this session does not use")
    assert expect_allowed(client=plain, sql=sql).to_pylist() == [{"n": 42}]


def test_no_policy_exemption(server):
    limited, plain = sessions(server=server)
    assert expect_allowed(client=limited, sql="SELECT id, ssn FROM emp").to_pylist() == []
    expect_blocked(client=limited, sql="INSERT INTO emp VALUES (3, '333')",
                   why="Hardcharger blocked this statement: INSERT on tok.main.emp, which has row or masking "
                       "policies, is not available under access control yet")
    assert expect_allowed(client=plain, sql="SELECT id, ssn FROM emp ORDER BY id").to_pylist() == [
        {"id": 1, "ssn": "111"}, {"id": 2, "ssn": "222"}]
    expect_allowed(client=plain, sql="INSERT INTO emp VALUES (3, '333')")
    expect_allowed(client=server.admin_client, sql="DELETE FROM emp WHERE id = 3")


def test_no_admin_option_and_nothing_listed(server):
    limited, plain = sessions(server=server)
    expect_blocked(client=limited, sql="GRANT MANAGE CONFIG TO helpers",
                   why="Hardcharger blocked GRANT: user tok may not grant MANAGE CONFIG")
    expect_allowed(client=plain, sql="GRANT MANAGE CONFIG TO helpers")
    expect_allowed(client=plain, sql="REVOKE MANAGE CONFIG FROM helpers")
    listed = "SELECT count(*) AS n FROM hardcharger_effective_privileges() WHERE object_type IN ('SYSTEM', 'LOCATION')"
    assert expect_allowed(client=limited, sql=listed).to_pylist() == [{"n": 0}]
    assert expect_allowed(client=plain, sql=listed).to_pylist()[0]["n"] >= 15


def test_its_reads_of_tables_are_unchanged(server):
    # Its grants still decide what it reads and writes: only the system privileges are gone.
    limited, _ = sessions(server=server)
    expect_allowed(client=limited, sql="INSERT INTO t VALUES (2)")
    assert {r["id"] for r in expect_allowed(client=limited, sql="SELECT id FROM t").to_pylist()} >= {1, 2}
