"""The DuckDB file store (rbac_store_uri: duckdb:/path/rbac.duckdb, or a path ending in
.duckdb), for development and a server that will only ever run one replica.

- The file is created 0600, and an existing one others can read or write is refused.
- One server per file: a second one on the same file fails at startup and says why.
- The file holds password hashes and grants, so no SQL reaches it, an admin's included:
  not by name, nor by any other spelling (computed, glob, .., relative, symlink, hard
  link, file://, case, file_search_path, ~), nor its WAL, nor through any reader,
  ATTACH, COPY or EXPORT. Each attack is run against a decoy (the same names in
  another directory) first, which must work: only the guard tells them apart.
- The instrumentation and log files, and the database, are guarded the same way (their
  catalogs are the only way in, and access control governs those).

Every other access-control test runs on this store too (CI's DuckDB pass:
HARDCHARGER_TEST_RBAC_STORE=duckdb, marker rbac_store).
"""

import dataclasses
import os
import pathlib
import shutil
import stat
import sys

import pytest

from rbac import WarpError, expect_allowed, rbac_env, start_rbac_server

PRIVATE = "one of the server's own files"
CASE_INSENSITIVE = sys.platform in ("darwin", "win32")


def store_dir(tmp_path: pathlib.Path, name: str = "store") -> pathlib.Path:
    directory = tmp_path / name
    directory.mkdir(mode=0o700)
    return directory.resolve()


def expect_private(client, sql: str, what: str) -> None:
    """`sql` must be refused by the private-file guard, naming `what`."""
    with pytest.raises(WarpError) as raised:
        client.run(sql=sql)
    detail = raised.value.detail
    assert "Hardcharger blocked access to" in detail and PRIVATE in detail, f"{sql}: {raised.value}"
    assert what in detail, f"{sql}: expected '{what}' in: {detail}"


# ---- The file ----

@pytest.mark.parametrize("spelling", ["duckdb:", ""])
def test_the_store_file_is_created_private_and_survives_a_restart(launcher, tmp_path, spelling):
    file = store_dir(tmp_path) / "rbac.duckdb"
    uri = f"{spelling}{file}"
    server = start_rbac_server(launcher=launcher, store_uri=uri)
    assert "DuckDB file" in server.log() and "one server only" in server.log(), server.log()
    admin = server.admin()
    for sql in ["CREATE TABLE orders (id INT)", "CREATE USER joe WITH PASSWORD 'pw'", "CREATE ROLE r",
                "GRANT SELECT ON orders TO r", "GRANT r TO joe"]:
        expect_allowed(client=admin, sql=sql)
    if sys.platform != "win32":
        for path in (file, file.with_name("rbac.duckdb.wal")):
            if path.exists():
                assert stat.S_IMODE(path.stat().st_mode) == 0o600, (path, oct(path.stat().st_mode))
    server.running.stop()
    restarted = start_rbac_server(launcher=launcher, store_uri=uri)
    assert restarted.password_token(user="joe", password="pw")
    grants = restarted.admin().run(sql="SHOW GRANTS FOR ROLE r").to_pylist()
    assert [row["privilege"] for row in grants] == ["SELECT"] and "orders" in str(grants), grants


def test_passwords_are_stored_hashed(launcher, tmp_path):
    file = store_dir(tmp_path) / "rbac.duckdb"
    server = start_rbac_server(launcher=launcher, store_uri=f"duckdb:{file}")
    expect_allowed(client=server.admin(), sql="CREATE USER joe WITH PASSWORD 'correct horse'")
    server.running.stop()
    # Read a copy with a server of its own (the store's file is no one's to open).
    copy = tmp_path / "copy.duckdb"
    shutil.copy(src=file, dst=copy)
    if file.with_name("rbac.duckdb.wal").exists():
        shutil.copy(src=file.with_name("rbac.duckdb.wal"), dst=tmp_path / "copy.duckdb.wal")
    reader = launcher.start(args=["--database", str(copy)])
    stored = reader.admin().scalar(sql="SELECT password_hash FROM hardcharger_rbac.users WHERE name = 'joe'")
    assert "correct horse" not in stored
    assert stored.startswith("pbkdf2-sha256$"), stored


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o606])
def test_a_store_file_others_can_read_or_write_is_refused(launcher, tmp_path, mode):
    file = store_dir(tmp_path) / "rbac.duckdb"
    first = start_rbac_server(launcher=launcher, store_uri=f"duckdb:{file}")
    first.running.stop()
    if sys.platform == "win32":
        # No POSIX modes there (the directory's ACL decides): the file is taken as it is.
        start_rbac_server(launcher=launcher, store_uri=f"duckdb:{file}")
        return
    file.chmod(mode)
    running = launcher.start(env=rbac_env(store_uri=f"duckdb:{file}"), expect_up=False)
    assert running.process.wait(timeout=60) != 0
    log = running.log()
    assert f"has mode {mode:04o}" in log and "chmod 600" in log, log
    # The twin: restricted again, it starts.
    file.chmod(0o600)
    start_rbac_server(launcher=launcher, store_uri=f"duckdb:{file}")


def test_one_server_per_store_file(launcher, tmp_path):
    file = store_dir(tmp_path) / "rbac.duckdb"
    first = start_rbac_server(launcher=launcher, store_uri=f"duckdb:{file}")
    expect_allowed(client=first.admin(), sql="CREATE ROLE only_here")
    second = launcher.start(env=rbac_env(store_uri=f"duckdb:{file}"), expect_up=False)
    assert second.process.wait(timeout=60) != 0
    log = second.log()
    assert "is in use by another process" in log, log
    assert "A DuckDB store serves a single server" in log and "use a PostgreSQL store" in log, log
    # The first server is unharmed, and once it stops, another can take the file.
    expect_allowed(client=first.admin(), sql="CREATE ROLE still_here")
    first.running.stop()
    third = start_rbac_server(launcher=launcher, store_uri=f"duckdb:{file}")
    for role in ("only_here", "still_here"):
        with pytest.raises(WarpError, match="already exists"):
            third.admin().run(sql=f"CREATE ROLE {role}")


@pytest.mark.parametrize("option", ["--database", "--instrumentation-db-path", "--log-catalog-db-path"])
def test_the_store_needs_a_file_of_its_own(launcher, tmp_path, option):
    file = store_dir(tmp_path) / "rbac.duckdb"
    running = launcher.start(args=[option, str(file)], env=rbac_env(store_uri=f"duckdb:{file}"), expect_up=False)
    assert running.process.wait(timeout=30) != 0
    assert "the access-control store needs a DuckDB file of its own" in running.log(), running.log()


def test_a_store_in_a_missing_directory_is_refused(launcher, tmp_path):
    running = launcher.start(env=rbac_env(store_uri=f"duckdb:{tmp_path / 'nowhere' / 'rbac.duckdb'}"),
                             expect_up=False)
    assert running.process.wait(timeout=30) != 0
    assert "does not exist" in running.log(), running.log()


# ---- No SQL reaches it ----

@dataclasses.dataclass
class Target:
    """The ways to name a store file (or its decoy): `dir` holds rbac.duckdb and its WAL."""
    dir: pathlib.Path
    cwd: pathlib.Path      # the server's working directory
    links: pathlib.Path    # a directory of the test's own, for links to the file

    @property
    def file(self) -> str:
        return str(self.dir / "rbac.duckdb")

    def spellings(self) -> dict[str, str]:
        file = self.dir / "rbac.duckdb"
        out = {
            "literal": f"'{file}'",
            "computed": f"'{self.dir}/rb' || 'ac.duckdb'",
            "dotdot": f"'{self.dir}/../{self.dir.name}/./rbac.duckdb'",
            "relative": f"'{os.path.relpath(file, self.cwd)}'",
            "file url": f"'file://{file}'" if sys.platform != "win32" else f"'file:///{file.as_posix()}'",
            "symlink": f"'{self.links / 'link.bin'}'",
            "hard link": f"'{self.links / 'hard.bin'}'",
        }
        if CASE_INSENSITIVE:
            out["case"] = f"'{self.dir}/RBAC.DuckDB'"
        if sys.platform == "win32":
            out["8.3 name"] = f"'{short_name(file)}'"
        return out


def short_name(path: pathlib.Path) -> str:
    """Windows' 8.3 spelling of `path` (the long one where the volume makes none)."""
    import ctypes
    buffer = ctypes.create_unicode_buffer(1024)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, 1024)
    return buffer.value if 0 < length < 1024 else str(path)


def make_links(target: Target) -> None:
    shutil.rmtree(target.links, ignore_errors=True)
    target.links.mkdir()
    (target.links / "link.bin").symlink_to(target.dir / "rbac.duckdb")
    os.link(src=target.dir / "rbac.duckdb", dst=target.links / "hard.bin")


def reset_decoy(server) -> None:
    """The decoy as the fixture made it: a write twin changed it."""
    decoy = server.targets["decoy"]
    shutil.rmtree(decoy.dir)
    decoy.dir.mkdir(mode=0o700)
    for sql in [f"ATTACH '{decoy.file}' AS decoy", "CREATE TABLE decoy.t AS SELECT 'decoy' AS s", "DETACH decoy"]:
        expect_allowed(client=server.principals["admin"], sql=sql)
    make_links(decoy)


# Reads of the file in each spelling; {p} is the spelling.
READS = {
    "read_blob": "SELECT md5(content) AS v FROM read_blob({p})",
    "read_duckdb": "SELECT count(*) AS v FROM read_duckdb({p}, table_name = 't')",
    "read_csv": "SELECT count(*) AS v FROM read_csv({p}, header = false, columns = {{'c': 'VARCHAR'}}, "
                "ignore_errors = true)",
    "read_json": "SELECT count(*) AS v FROM read_json_objects({p}, format = 'newline_delimited', "
                 "ignore_errors = true)",
    "ATTACH": "ATTACH {p} AS stolen (READ_ONLY)",
}


@pytest.fixture(scope="module")
def guarded(module_launcher, tmp_path_factory):
    """A server on a DuckDB store; a decoy with the same names in another directory; an
    admin, and fay, a user with every file-system privilege."""
    root = tmp_path_factory.mktemp("guard").resolve()
    store = store_dir(root, "store")
    decoy = store_dir(root, "decoy")
    server = start_rbac_server(launcher=module_launcher, store_uri=f"duckdb:{store / 'rbac.duckdb'}")
    admin = server.admin()
    for sql in ["CREATE ROLE fs", "GRANT READ FILESYSTEM TO fs", "GRANT WRITE FILESYSTEM TO fs",
                "GRANT MANAGE CATALOGS TO fs", "GRANT MANAGE CONFIG TO fs", "GRANT CREATE TABLE ON SCHEMA main TO fs",
                "CREATE USER fay", "GRANT fs TO fay", "CREATE USER victim WITH PASSWORD 'hunter2'"]:
        expect_allowed(client=admin, sql=sql)
    # The decoy: a real DuckDB file named rbac.duckdb, written by the server itself.
    for sql in [f"ATTACH '{decoy / 'rbac.duckdb'}' AS decoy", "CREATE TABLE decoy.t AS SELECT 'decoy' AS s",
                "DETACH decoy"]:
        expect_allowed(client=admin, sql=sql)
    targets = {"store": Target(dir=store, cwd=module_launcher.tmp_path, links=root / "store-links"),
               "decoy": Target(dir=decoy, cwd=module_launcher.tmp_path, links=root / "decoy-links")}
    for target in targets.values():
        make_links(target)
    assert (store / "rbac.duckdb.wal").exists() or (store / "rbac.duckdb").stat().st_size > 0
    server.principals = {"admin": admin, "fay": server.user(name="fay")}
    server.targets = targets
    return server


def attack(guarded, who: str, sql_for) -> None:
    """`sql_for(target)` works on the decoy and is refused on the store."""
    client = guarded.principals[who]
    decoy_sql, store_sql = sql_for(guarded.targets["decoy"]), sql_for(guarded.targets["store"])
    try:
        client.run(sql=decoy_sql)
    except WarpError as error:
        pytest.fail(reason=f"the decoy twin failed, so the attack proves nothing: {decoy_sql} -> {error}")
    finally:
        if decoy_sql.startswith("ATTACH"):
            client.run(sql="DETACH stolen")
    expect_private(client=client, sql=store_sql, what="the access-control store")


@pytest.mark.parametrize("who", ["admin", "fay"])
@pytest.mark.parametrize("reader", list(READS))
@pytest.mark.parametrize("spelling", ["literal", "computed", "dotdot", "relative", "file url", "symlink",
                                      "hard link", *(["case"] if CASE_INSENSITIVE else []),
                                      *(["8.3 name"] if sys.platform == "win32" else [])])
def test_no_reader_opens_the_store_in_any_spelling(guarded, who, reader, spelling):
    attack(guarded=guarded, who=who, sql_for=lambda t: READS[reader].format(p=t.spellings()[spelling]))


@pytest.mark.parametrize("who", ["admin", "fay"])
def test_a_glob_never_reads_the_store(guarded, who):
    # glob() may list the directory (names are no secret); reading what it matches is refused.
    attack(guarded=guarded, who=who, sql_for=lambda t: f"SELECT md5(content) AS v FROM read_blob('{t.dir}/*')")
    attack(guarded=guarded, who=who, sql_for=lambda t: f"SELECT md5(content) AS v FROM read_blob('{t.dir}/rbac.*')")


@pytest.mark.parametrize("who", ["admin", "fay"])
@pytest.mark.parametrize("suffix", [".wal"])
def test_the_wal_is_out_of_reach(guarded, who, suffix):
    decoy_wal = guarded.targets["decoy"].dir / f"rbac.duckdb{suffix}"
    decoy_wal.write_text("decoy wal")
    try:
        attack(guarded=guarded, who=who, sql_for=lambda t: f"SELECT md5(content) AS v FROM read_blob('{t.file}{suffix}')")
        attack(guarded=guarded, who=who,
               sql_for=lambda t: f"SELECT md5(content) AS v FROM read_blob('{t.dir}/rbac.duck' || 'db{suffix}')")
    finally:
        decoy_wal.unlink()


WRITES = {
    # A forged WAL would be replayed into the store at its next start.
    "wal": "COPY (SELECT 'forged' AS s) TO '{f}.wal' (FORMAT csv)",
    "checkpoint wal": "COPY (SELECT 'forged' AS s) TO '{f}.wal.checkpoint' (FORMAT csv)",
    # Written as tmp_rbac.duckdb, then renamed onto the target.
    "tmp file": "COPY (SELECT 'forged' AS s) TO '{f}' (FORMAT csv, USE_TMP_FILE true)",
    "overwrite": "COPY (SELECT 'forged' AS s) TO '{f}' (FORMAT csv, USE_TMP_FILE false)",
    "through a symlink": "COPY (SELECT 'forged' AS s) TO '{link}' (FORMAT csv, USE_TMP_FILE false)",
    "export": "EXPORT DATABASE '{f}.tmp'",
}


@pytest.mark.parametrize("who", ["admin", "fay"])
@pytest.mark.parametrize("write", list(WRITES))
def test_copy_and_export_never_write_the_store(guarded, who, write):
    if write == "export" and who == "fay":
        who = "admin"  # EXPORT DATABASE is the administrator's alone
    try:
        attack(guarded=guarded, who=who,
               sql_for=lambda t: WRITES[write].format(f=t.file, link=t.links / "link.bin"))
    finally:
        reset_decoy(server=guarded)
    # The store still works, every grant intact.
    assert guarded.password_token(user="victim", password="hunter2")


@pytest.mark.parametrize("who", ["admin", "fay"])
def test_copy_from_the_store_is_refused(guarded, who):
    client = guarded.principals[who]
    client.run(sql=f"CREATE OR REPLACE TABLE {who}_lines (line VARCHAR)")
    attack(guarded=guarded, who=who,
           sql_for=lambda t: f"COPY {who}_lines FROM '{t.file}' (FORMAT csv, HEADER false, DELIMITER '\\x01', "
                             "IGNORE_ERRORS true)")


@pytest.mark.parametrize("who", ["admin", "fay"])
def test_file_search_path_and_home_directory_do_not_reach_the_store(guarded, who):
    client = guarded.principals[who]
    try:
        for setting in ("file_search_path", "home_directory"):
            def sql_for(t, setting=setting):
                client.run(sql=f"SET {setting} = '{t.dir}'")
                name = "rbac.duckdb" if setting == "file_search_path" else "~/rbac.duckdb"
                return f"SELECT md5(content) AS v FROM read_blob('{name}')"
            decoy, store = guarded.targets["decoy"], guarded.targets["store"]
            client.run(sql=sql_for(decoy))  # the twin
            expect_private(client=client, sql=sql_for(store), what="the access-control store")
    finally:
        client.run(sql="RESET file_search_path")
        client.run(sql="RESET home_directory")


def test_the_guard_holds_with_block_sensitive_paths_off(module_launcher, tmp_path_factory):
    directory = store_dir(tmp_path_factory.mktemp("off"))
    server = start_rbac_server(launcher=module_launcher, store_uri=f"duckdb:{directory / 'rbac.duckdb'}",
                               env={"HARDCHARGER_BLOCK_SENSITIVE_PATHS": "false"})
    admin = server.admin()
    for spelling in (f"'{directory}/rbac.duckdb'", f"'{directory}/rbac' || '.duckdb'"):
        expect_private(client=admin, sql=f"SELECT md5(content) FROM read_blob({spelling})", what="the access-control store")


def test_the_store_is_no_catalog_and_no_setting_names_it(guarded):
    admin = guarded.principals["admin"]
    catalogs = {row["database_name"] for row in admin.run(sql="SELECT database_name FROM duckdb_databases()").to_pylist()}
    assert "rbac_store" not in catalogs, catalogs
    files = {row["path"] for row in admin.run(sql="SELECT path FROM duckdb_databases() WHERE path IS NOT NULL").to_pylist()}
    assert not any("rbac.duckdb" in path for path in files), files


# ---- The server's attached files ----

@pytest.fixture(scope="module")
def attached(module_launcher, tmp_path_factory, store):
    """A server with a database file, instrumentation and catalog logging, on either store."""
    directory = tmp_path_factory.mktemp("attached").resolve()
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               args=["--database", str(directory / "srv.duckdb"), "--enable-instrumentation",
                                     "--enable-catalog-logging"])
    admin = server.admin()
    for sql in ["CREATE TABLE secret AS SELECT 'top secret' AS s", "CHECKPOINT", "CREATE ROLE fs",
                "GRANT READ FILESYSTEM TO fs", "GRANT WRITE FILESYSTEM TO fs", "GRANT MANAGE CATALOGS TO fs",
                "CREATE USER fay", "GRANT fs TO fay"]:
        expect_allowed(client=admin, sql=sql)
    # The decoys: DuckDB files with the same names, in a directory of their own (made by the
    # server: Windows won't let a test copy the files the server has open).
    decoy = directory / "decoy"
    decoy.mkdir()
    for name in ("srv.duckdb", "hardcharger_instrumentation.db", "hardcharger_logs.db"):
        for sql in [f"ATTACH '{decoy / name}' AS decoy", "CREATE TABLE decoy.t AS SELECT 'decoy' AS s",
                    "DETACH decoy"]:
            expect_allowed(client=admin, sql=sql)
    server.dir = directory
    server.principals = {"admin": admin, "fay": server.user(name="fay")}
    return server


ATTACHED = {"srv.duckdb": "the server's database", "hardcharger_instrumentation.db": "the instrumentation database",
            "hardcharger_logs.db": "the log database"}


@pytest.mark.parametrize("who", ["admin", "fay"])
@pytest.mark.parametrize("name", list(ATTACHED))
def test_the_attached_files_are_read_through_their_catalogs_only(attached, who, name):
    client = attached.principals[who]
    directory, decoy = attached.dir, attached.dir / "decoy"
    stem, dot, extension = name.rpartition(".")
    for template in ["SELECT md5(content) AS v FROM read_blob('{d}/{n}')",
                     "SELECT md5(content) AS v FROM read_blob('{d}/" + stem + "' || '." + extension + "')",
                     "SELECT md5(content) AS v FROM read_blob('{d}/*')",
                     "COPY (SELECT 'forged' AS s) TO '{d}/{n}.wal' (FORMAT csv)"]:
        twin = template.format(d=decoy, n=name)
        try:
            client.run(sql=twin)
        except WarpError as error:
            pytest.fail(reason=f"the decoy twin failed: {twin} -> {error}")
        (decoy / f"{name}.wal").unlink(missing_ok=True)
        sql = template.format(d=directory, n=name)
        with pytest.raises(WarpError) as raised:
            client.run(sql=sql)
        assert "Hardcharger blocked access to" in raised.value.detail and PRIVATE in raised.value.detail, \
            f"{sql}: {raised.value}"
    # Through a hard link, ATTACH would open the file a second time (DuckDB's own check
    # compares canonical paths, which a symlink can't fool, but a hard link does): refused.
    for base in (directory, decoy):
        links = base / f"links-{who}"
        links.mkdir(exist_ok=True)
        if not (links / name).exists():
            os.link(src=base / name, dst=links / name)
    expect_allowed(client=client, sql=f"ATTACH '{decoy}/links-{who}/{name}' AS twice (READ_ONLY)")  # the twin
    expect_allowed(client=client, sql="DETACH twice")
    expect_private(client=client, sql=f"ATTACH '{directory}/links-{who}/{name}' AS twice (READ_ONLY)",
                   what=ATTACHED[name])


def test_the_attached_files_keep_working(attached):
    admin = attached.principals["admin"]
    expect_allowed(client=admin, sql="INSERT INTO secret VALUES ('more')")
    expect_allowed(client=admin, sql="CHECKPOINT")
    assert admin.run(sql="SELECT count(*) AS n FROM secret").column("n").to_pylist() == [2]
    assert admin.run(sql="SELECT count(*) AS n FROM _hardcharger_instr.main.sessions").column("n").to_pylist()[0] >= 1
    # fay reads none of it: no grant on the table, and the server's own catalogs are hidden.
    fay = attached.principals["fay"]
    with pytest.raises(WarpError):
        fay.run(sql="SELECT * FROM secret")
    with pytest.raises(WarpError):
        fay.run(sql="SELECT * FROM _hardcharger_instr.main.sql_statements")


def test_a_server_without_access_control_guards_its_files_from_its_admin(launcher, tmp_path):
    directory = tmp_path.resolve()
    server = launcher.start(args=["--database", str(directory / "plain.duckdb"), "--enable-instrumentation"])
    admin = server.admin()
    for name in ("plain.duckdb", "hardcharger_instrumentation.db"):
        error = admin.error(sql=f"SELECT md5(content) FROM read_blob('{directory}/' || '{name}')")
        assert "Hardcharger blocked access to" in error["detail"] and PRIVATE in error["detail"], error
        error = admin.error(sql=f"COPY (SELECT 1) TO '{directory}/{name}.wal'")
        assert PRIVATE in error["detail"], error
