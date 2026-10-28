"""The storage privileges: READ / WRITE FILESYSTEM (the server's own files), READ / WRITE
OBJECT STORAGE (s3://, gs://, az://, ...) and READ / WRITE HTTP (http(s)://, hf://), granted
everywhere or ON LOCATION '<prefix>'. Each kind needs its own privilege; a location covers
itself and what is below it, by every spelling (symlinks resolved, scheme aliases folded), and
paths that could name one object two ways are refused. Object storage is the tests' S3 gateway
(docker-compose.yml); the web is a local HTTP server."""

import functools
import http.server
import os
import secrets
import threading

import psycopg
import pytest

from rbac import expect_allowed, expect_blocked, start_rbac_server
from services import S3_PORT, require

BUCKET = "ducklake"  # the gateway's bucket (docker-compose.yml)


@pytest.fixture(scope="module")
def web(tmp_path_factory):
    """A local HTTP server: /public/a.csv and /private/b.csv."""
    root = tmp_path_factory.mktemp("web")
    for name, body in [("public/a.csv", "x\n1\n"), ("private/b.csv", "secret\nweb\n")]:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(body)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(scope="module")
def world(module_launcher, store, tmp_path_factory, web):
    require(port=S3_PORT, what="the S3 gateway")
    work = tmp_path_factory.mktemp("storage")
    prefix = f"s3://{BUCKET}/storage-{secrets.token_hex(nbytes=4)}"
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               args=["--database", str(work / "storage.duckdb")],
                               env={"HARDCHARGER_INIT_SQL_COMMANDS": "INSTALL httpfs; LOAD httpfs;"})
    admin = server.admin()
    (work / "shared").mkdir()
    (work / "shared" / "a.csv").write_text("x\n1\n")
    (work / "secret").mkdir()
    (work / "secret" / "s.csv").write_text("secret\nlocal\n")
    os.symlink(work / "secret", work / "shared" / "link")
    for sql in [
        f"CREATE SECRET s3_test (TYPE s3, KEY_ID 'minioadmin', SECRET 'minioadmin', ENDPOINT '127.0.0.1:{S3_PORT}', "
        "USE_SSL false, URL_STYLE 'path', REGION 'us-east-1')",
        f"COPY (SELECT 1 AS x) TO '{prefix}/in/a.csv'",
        f"COPY (SELECT 2 AS x) TO '{prefix}/in/b.csv'",
        f"COPY (SELECT 'secret' AS s) TO '{prefix}/internal/s.csv'",
        f"COPY (SELECT 3 AS x) TO '{prefix}/in2/c.csv'",
        "CREATE TABLE t AS SELECT 7 AS n",
    ]:
        expect_allowed(client=admin, sql=sql)
    return {"server": server, "admin": admin, "work": work, "prefix": prefix, "web": web}


def user_with(world, name: str, grants: list[str]):
    """A fresh user whose role holds `grants` (system privilege phrases, ON LOCATION included)."""
    admin = world["admin"]
    for sql in [f"CREATE ROLE {name}_role", f"CREATE USER {name}", f"GRANT {name}_role TO {name}",
                f"GRANT SELECT ON t TO {name}_role"] + [f"GRANT {g} TO {name}_role" for g in grants]:
        expect_allowed(client=admin, sql=sql)
    return world["server"].user(name=name)


def test_each_kind_of_storage_needs_its_own_privilege(world):
    work, prefix, web = world["work"], world["prefix"], world["web"]
    local, remote, url = f"{work}/shared/a.csv", f"{prefix}/in/a.csv", f"{web}/public/a.csv"
    files = user_with(world=world, name="kind_fs", grants=["READ FILESYSTEM"])
    expect_allowed(client=files, sql=f"SELECT * FROM read_csv('{local}')")
    expect_blocked(client=files, sql=f"SELECT * FROM read_csv('{remote}')", why="needs the READ OBJECT STORAGE")
    expect_blocked(client=files, sql=f"SELECT * FROM read_csv('{url}')", why="needs the READ HTTP")
    bucket = user_with(world=world, name="kind_os", grants=["READ OBJECT STORAGE"])
    assert bucket.run(sql=f"SELECT x FROM read_csv('{remote}')").column("x").to_pylist() == [1]
    expect_blocked(client=bucket, sql=f"SELECT * FROM read_csv('{local}')", why="needs the READ FILESYSTEM")
    expect_blocked(client=bucket, sql=f"SELECT * FROM '{url}'", why="needs the READ HTTP")
    webby = user_with(world=world, name="kind_http", grants=["READ HTTP"])
    assert webby.run(sql=f"SELECT x FROM read_csv('{url}')").column("x").to_pylist() == [1]
    expect_blocked(client=webby, sql=f"SELECT * FROM read_csv('{remote}')", why="needs the READ OBJECT STORAGE")
    # Reading is not writing, and each kind's WRITE is its own too.
    expect_blocked(client=bucket, sql=f"COPY t TO '{prefix}/in/out.csv'", why="needs the WRITE OBJECT STORAGE")
    writer = user_with(world=world, name="kind_osw", grants=["WRITE OBJECT STORAGE"])
    expect_allowed(client=writer, sql=f"COPY t TO '{prefix}/out/w.csv'")
    expect_blocked(client=writer, sql=f"COPY t TO '{work}/shared/w.csv'", why="needs the WRITE FILESYSTEM")


def test_an_object_storage_location_covers_what_is_below_it(world):
    prefix = world["prefix"]
    reader = user_with(world=world, name="loc_os", grants=[f"READ OBJECT STORAGE ON LOCATION '{prefix}/in/'"])
    assert reader.run(sql=f"SELECT x FROM read_csv('{prefix}/in/a.csv')").column("x").to_pylist() == [1]
    assert sorted(reader.run(sql=f"SELECT x FROM read_csv('{prefix}/in/*.csv')").column("x").to_pylist()) == [1, 2]
    # s3a:// is s3://, a bucket's name is compared in lower case.
    expect_allowed(client=reader, sql=f"SELECT * FROM read_csv('{prefix.replace('s3://', 's3a://')}/in/a.csv')")
    for sql, why in [
        (f"SELECT * FROM read_csv('{prefix}/internal/s.csv')", "on a location that covers it"),
        (f"SELECT * FROM read_csv('{prefix}/in2/c.csv')", "on a location that covers it"),  # a prefix of a name
        (f"SELECT * FROM read_csv('{prefix}/*/s.csv')", "on a location that covers it"),  # a glob above it
        (f"SELECT * FROM glob('{prefix}/**')", "on a location that covers it"),
        (f"SELECT * FROM read_csv('{prefix}/in/../internal/s.csv')", "'..' segment"),
        (f"SELECT * FROM read_csv('{prefix}/in//a.csv')", "empty path segment"),
        (f"SELECT * FROM read_csv('{prefix}/in/a.csv?s3_endpoint=127.0.0.1:1')", "'?' or '#'"),
        (f"SELECT * FROM read_csv('{prefix}/in/%2e%2e/internal/s.csv')", "percent-escape"),
        ("SELECT * FROM read_csv('ftp://example.invalid/in/a.csv')", "the scheme ftp://"),
    ]:
        expect_blocked(client=reader, sql=sql, why=why)


def test_a_write_location_is_where_copy_may_write(world):
    prefix = world["prefix"]
    writer = user_with(world=world, name="loc_osw", grants=[f"WRITE OBJECT STORAGE ON LOCATION '{prefix}/exports'"])
    expect_allowed(client=writer, sql=f"COPY t TO '{prefix}/exports/t.csv'")
    expect_allowed(client=writer, sql=f"COPY (SELECT n, 1 AS v FROM t) TO '{prefix}/exports/by_n' (FORMAT parquet, PARTITION_BY (n))")
    expect_blocked(client=writer, sql=f"COPY t TO '{prefix}/in/t.csv'", why="on a location that covers it")
    # A file name pattern would place partition files outside the directory checked.
    expect_blocked(client=writer, sql=f"COPY (SELECT n, 1 AS v FROM t) TO '{prefix}/exports/p' "
                                      "(FORMAT parquet, PARTITION_BY (n), FILENAME_PATTERN '../../../in/t_{i}')",
                   why="FILENAME_PATTERN")
    assert world["admin"].run(sql=f"SELECT n FROM read_csv('{prefix}/exports/t.csv')").column("n").to_pylist() == [7]


def test_a_web_location_is_a_host_and_path_prefix(world):
    web = world["web"]
    reader = user_with(world=world, name="loc_http", grants=[f"READ HTTP ON LOCATION '{web}/public/'"])
    expect_allowed(client=reader, sql=f"SELECT * FROM read_csv('{web}/public/a.csv')")
    expect_allowed(client=reader, sql=f"SELECT * FROM read_csv('{web}/public/a.csv?v=2')")  # a query is the server's
    for sql, why in [
        (f"SELECT * FROM read_csv('{web}/private/b.csv')", "on a location that covers it"),
        (f"SELECT * FROM read_csv('{web.replace('127.0.0.1', '127.0.0.1@localhost')}/public/a.csv')",
         "user information"),
        (f"SELECT * FROM read_csv('{web}/public/%2e%2e/private/b.csv')", "escaped"),
        (f"SELECT * FROM read_csv('{web}/public/../private/b.csv')", "'..' segment"),
        (f"SELECT * FROM read_csv('{web.replace('http://', 'https://')}/public/a.csv')", "on a location that covers it"),
    ]:
        expect_blocked(client=reader, sql=sql, why=why)


def test_a_local_location_does_not_reach_through_a_symlink(world):
    work = world["work"]
    reader = user_with(world=world, name="loc_fs", grants=[f"READ FILESYSTEM ON LOCATION '{work}/shared'"])
    expect_allowed(client=reader, sql=f"SELECT * FROM read_csv('{work}/shared/a.csv')")
    expect_allowed(client=reader, sql=f"SELECT * FROM read_csv('file://{work}/shared/a.csv')")
    for sql in [f"SELECT * FROM read_csv('{work}/shared/link/s.csv')",  # a symlink out of it
                f"SELECT * FROM read_csv('{work}/shared/../secret/s.csv')",
                f"SELECT * FROM read_csv('{work}/secret/s.csv')"]:
        expect_blocked(client=reader, sql=sql, why="on a location that covers it")


def test_a_computed_path_needs_every_kind_everywhere(world):
    prefix = world["prefix"]
    scoped = user_with(world=world, name="computed", grants=[f"READ OBJECT STORAGE ON LOCATION '{prefix}/in/'"])
    computed = f"SELECT x FROM read_csv('{prefix}/in/' || 'a.csv')"
    expect_blocked(client=scoped, sql=computed, why="an expression or a parameter")
    expect_blocked(client=scoped, sql=f"SELECT * FROM read_csv(['{prefix}/in/a.csv', lower('{prefix}/in/b.csv')])",
                   why="an expression or a parameter")
    expect_allowed(client=scoped, sql=f"SELECT * FROM read_csv(['{prefix}/in/a.csv', '{prefix}/in/b.csv'])")
    everywhere = user_with(world=world, name="computed_all",
                           grants=["READ FILESYSTEM", "READ OBJECT STORAGE", "READ HTTP"])
    assert everywhere.run(sql=computed).column("x").to_pylist() == [1]
    # The bound path is still checked: DuckLake's storage stays out of reach (test_rbac_ducklake_storage.py).


def test_a_prepared_path_is_checked_as_bound(world):
    prefix = world["prefix"]
    everywhere = user_with(world=world, name="prepared",
                           grants=["READ FILESYSTEM", f"READ OBJECT STORAGE ON LOCATION '{prefix}/in/'", "READ HTTP"])
    # Not everywhere for object storage: a parameter's path is refused before it binds.
    expect_blocked(client=everywhere, sql="SELECT * FROM read_csv($1)", why="an expression or a parameter")


def test_grants_on_locations_are_validated_and_listed(world):
    admin, prefix, work, web = world["admin"], world["prefix"], world["work"], world["web"]
    expect_allowed(client=admin, sql="CREATE ROLE locs")
    for sql, why in [
        (f"GRANT READ HTTP ON LOCATION '{prefix}/in/' TO locs", "covers the web"),
        (f"GRANT READ FILESYSTEM ON LOCATION '{web}/public/' TO locs", "covers the server's file system"),
        ("GRANT READ FILESYSTEM ON LOCATION 'relative/dir' TO locs", "not an absolute path"),
        ("GRANT READ FILESYSTEM ON LOCATION '~/data' TO locs", "not an absolute path"),
        (f"GRANT READ OBJECT STORAGE ON LOCATION '{prefix}/in/*' TO locs", "pattern"),
        (f"GRANT READ HTTP ON LOCATION '{web}/public/?x=1' TO locs", "query"),
        ("GRANT MANAGE CONFIG ON LOCATION '/tmp' TO locs", "no ON clause"),
    ]:
        with pytest.raises(Exception) as raised:
            admin.run(sql=sql)
        assert why in str(raised.value), f"{sql}: {raised.value}"
    expect_allowed(client=admin, sql=f"GRANT READ OBJECT STORAGE, WRITE OBJECT STORAGE ON LOCATION '{prefix}/in/' TO locs")
    expect_allowed(client=admin, sql=f"GRANT READ FILESYSTEM ON LOCATION '{work}/shared/' TO locs")
    rows = admin.run(sql="SHOW GRANTS FOR locs").to_pylist()
    located = {(r["privilege"], r["object_type"], r["object"]) for r in rows}
    assert ("READ OBJECT STORAGE", "LOCATION", f"{prefix}/in") in located, rows
    assert ("WRITE OBJECT STORAGE", "LOCATION", f"{prefix}/in") in located, rows
    grants = admin.run(sql="SELECT privilege, object_type, object_name FROM hardcharger_grants() "
                           "WHERE grantee = 'locs'").to_pylist()
    assert {"privilege": "READ OBJECT STORAGE", "object_type": "LOCATION", "object_name": f"{prefix}/in"} in grants, grants
    expect_allowed(client=admin, sql=f"REVOKE WRITE OBJECT STORAGE ON LOCATION '{prefix}/in' FROM locs")
    remaining = {r["privilege"] for r in admin.run(sql="SHOW GRANTS FOR locs").to_pylist()}
    assert "WRITE OBJECT STORAGE" not in remaining and "READ OBJECT STORAGE" in remaining, remaining


def test_admin_option_on_a_location_delegates_below_it_only(world):
    admin, server, prefix = world["admin"], world["server"], world["prefix"]
    for sql in ["CREATE ROLE delegator", "CREATE USER dee", "GRANT delegator TO dee", "CREATE ROLE delegate",
                f"GRANT READ OBJECT STORAGE ON LOCATION '{prefix}/in' TO delegator WITH ADMIN OPTION"]:
        expect_allowed(client=admin, sql=sql)
    dee = server.user(name="dee")
    expect_allowed(client=dee, sql=f"GRANT READ OBJECT STORAGE ON LOCATION '{prefix}/in/sub' TO delegate")
    for sql in [f"GRANT READ OBJECT STORAGE ON LOCATION '{prefix}' TO delegate",
                "GRANT READ OBJECT STORAGE TO delegate",
                f"GRANT WRITE OBJECT STORAGE ON LOCATION '{prefix}/in/sub' TO delegate"]:
        with pytest.raises(Exception) as raised:
            dee.run(sql=sql)
        assert "may not grant" in str(raised.value), f"{sql}: {raised.value}"
    # Revoking the grantor's privilege takes the grants made through it (CASCADE).
    expect_allowed(client=admin, sql=f"REVOKE READ OBJECT STORAGE ON LOCATION '{prefix}/in' FROM delegator")
    assert admin.run(sql="SHOW GRANTS FOR delegate").num_rows == 0


def test_filesystem_grants_from_before_the_split_keep_their_reach_once(launcher, pg_store, tmp_path):
    """Before 2.2, READ / WRITE FILESYSTEM also covered object storage and URLs; a store from
    then gives their holders the new privileges everywhere, once."""
    uri = pg_store.new_database()
    server = start_rbac_server(launcher=launcher, store_uri=uri)
    for sql in ["CREATE ROLE legacy_r", "CREATE ROLE legacy_w", "GRANT READ FILESYSTEM TO legacy_r WITH ADMIN OPTION",
                "GRANT WRITE FILESYSTEM TO legacy_w"]:
        expect_allowed(client=server.admin(), sql=sql)
    server.running.stop()
    # As 2.1 left it: no new privileges, no record of the split.
    with psycopg.connect(conninfo=uri, autocommit=True) as connection:
        connection.execute(query="DELETE FROM hardcharger_rbac.storage_privileges")
        connection.execute(query="DELETE FROM hardcharger_rbac.meta WHERE key = 'storage_privileges_split'")
    restarted = start_rbac_server(launcher=launcher, store_uri=uri)
    rows = {(r["grantee"], r["privilege"], r["with_grant_option"])
            for r in restarted.admin().run(sql="SELECT grantee, privilege, with_grant_option FROM hardcharger_grants() "
                                               "WHERE grantee LIKE 'legacy_%'").to_pylist()}
    assert rows == {("legacy_r", "READ FILESYSTEM", True), ("legacy_r", "READ OBJECT STORAGE", True),
                    ("legacy_r", "READ HTTP", True), ("legacy_w", "WRITE FILESYSTEM", False),
                    ("legacy_w", "WRITE OBJECT STORAGE", False), ("legacy_w", "WRITE HTTP", False)}, rows
    # Once: a revoke after the split stays revoked across restarts.
    expect_allowed(client=restarted.admin(), sql="REVOKE READ HTTP FROM legacy_r")
    restarted.running.stop()
    again = start_rbac_server(launcher=launcher, store_uri=uri)
    privileges = {r["privilege"] for r in again.admin().run(sql="SELECT privilege FROM hardcharger_grants() "
                                                                 "WHERE grantee = 'legacy_r'").to_pylist()}
    assert privileges == {"READ FILESYSTEM", "READ OBJECT STORAGE"}, privileges
