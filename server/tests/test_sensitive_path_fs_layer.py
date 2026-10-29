"""The sensitive-path guard in DuckDB's file system (0.4.0).

Hardcharger 0.3.0 looked for path-like literals in the SQL text. That missed every path
the text doesn't spell (query('...'), a macro's or view's argument, a prepared
statement's parameter, a variable, file_search_path), and it refused harmless literals
('*', '.*', '{:,}', '(\\d{2})', JSON) whenever the working directory held a protected
path, as the Docker image's (its home, with ~/.ssh in it) does. The guard now sits in
DuckDB's file system (ServerFileGuard, server/src/authz/file_guard.cc), as GizmoSQL's
SensitivePathGuardFileSystem does: every open, stat, listing, glob, copy, move and remove
is checked by the path DuckDB resolved, for every principal, the administrator included.

Everything here runs against a FAKE home under the test's temporary directory, holding
dummy credential files, and the servers' home and working directory are that fake home;
the real user's home is never named. System files (/etc/shadow, /proc) are only ever
asserted refused, and no guard-off twin reads them. Each refusal asserts the guard's own
message and category, and has a twin on a server started with block_sensitive_paths
false that reads the dummy file, so the test is known to bite.
"""

import os
import pathlib
import sys

import pytest

from conftest import Launcher, home_env, protected_system_file
from rbac import ChargewireError, expect_allowed, start_rbac_server

GUARD = "Hardcharger blocked access to"
CREDENTIALS = "user credential store"
SERVER_CREDENTIALS = "Hardcharger server credentials"
DUCKDB_SECRETS = "DuckDB persistent secrets"

# The dummy credential files (home-relative path -> its text): one per protected entry of
# the home directory (SensitivePaths, server/src/guards.cc).
DUMMY_FILES = {
    ".ssh/id_rsa": "DUMMY-ssh-key",
    ".aws/credentials": "DUMMY-aws-credentials",
    ".azure/accessTokens.json": "DUMMY-azure-token",
    ".config/gcloud/application_default_credentials.json": "DUMMY-gcloud-credentials",
    ".kube/config": "DUMMY-kube-config",
    ".docker/config.json": "DUMMY-docker-config",
    ".netrc": "DUMMY-netrc",
    ".pgpass": "DUMMY-pgpass",
    ".git-credentials": "DUMMY-git-credentials",
    ".gnupg/secring.gpg": "DUMMY-gnupg",
}
KEY = ".ssh/id_rsa"
KEY_TEXT = DUMMY_FILES[KEY]
KEY_CSV = ".ssh/keys.csv"
PUBLIC_TEXT = "PUBLIC-data"
# Autoinstalling an extension would download into the fake home; nothing here needs one.
NO_AUTOLOAD = "SET GLOBAL autoload_known_extensions = false; SET GLOBAL autoinstall_known_extensions = false;"


@pytest.fixture(scope="module")
def fake_home(tmp_path_factory, store) -> pathlib.Path:
    home = tmp_path_factory.mktemp("fake-home").resolve()
    for relative, text in DUMMY_FILES.items():
        (home / relative).parent.mkdir(parents=True, exist_ok=True)
        (home / relative).write_text(text)
    (home / KEY_CSV).write_text(f"key\n{KEY_TEXT}\n")
    (home / "data").mkdir()
    (home / "data" / "public.txt").write_text(PUBLIC_TEXT)
    # A home-directory canary: the servers' ~ must be this directory, or nothing runs.
    (home / "CANARY.txt").write_text(f"canary {home}")
    # Another name for ~/.ssh (on Windows a junction, which needs no privilege) and a
    # second hard link of ~/.aws/credentials.
    if sys.platform == "win32":
        import _winapi
        _winapi.CreateJunction(str(home / ".ssh"), str(home / "data" / "keys"))
    else:
        (home / "data" / "keys").symlink_to(home / ".ssh", target_is_directory=True)
    os.link(home / ".aws" / "credentials", home / "data" / "credentials-link")
    # The operator's init SQL file: one of the server's credentials.
    (home / "init.sql").write_text(NO_AUTOLOAD)
    if store.kind == "postgres":
        # This isolated HOME has no extension cache. Install the signed PostgreSQL
        # extension before opening the RBAC store: a repository download must not
        # consume the store's 10-second connection deadline on a busy CI runner.
        # The guarded servers still use this fake HOME and the default store timeout.
        bootstrap = Launcher(tmp_path=home)
        try:
            running = bootstrap.start(env=home_env(home=home))
            running.admin().execute(sql="INSTALL postgres")
        finally:
            bootstrap.stop_all()
    return home


@pytest.fixture(scope="module")
def home_launcher(fake_home):
    launch = Launcher(tmp_path=fake_home)  # the servers' working directory
    yield launch
    launch.stop_all()


def start_server(home_launcher, fake_home, store, guarded: bool):
    env = {**home_env(home=fake_home), "HARDCHARGER_INIT_SQL_COMMANDS_FILE": str(fake_home / "init.sql")}
    if not guarded:
        env["HARDCHARGER_BLOCK_SENSITIVE_PATHS"] = "false"
    name = "guarded" if guarded else "open"
    server = start_rbac_server(launcher=home_launcher, store_uri=store.new_database(), env=env,
                               args=["--database", str(fake_home / f"{name}.duckdb")])
    admin = server.admin()
    # The servers' home must be the fake one before anything names ~ or a relative path.
    canary = admin.run(sql="SELECT content FROM read_text('~/CANARY.txt')").column("content").to_pylist()
    if canary != [f"canary {fake_home}"]:
        pytest.fail(reason="the server's home is not the fake home: refusing to run")
    for sql in ["CREATE ROLE fs", "GRANT READ FILESYSTEM TO fs", "GRANT READ OBJECT STORAGE TO fs", "GRANT READ HTTP TO fs", "GRANT WRITE FILESYSTEM TO fs",
                "GRANT MANAGE CATALOGS TO fs", "GRANT CREATE TABLE ON SCHEMA main TO fs",
                "GRANT CREATE VIEW ON SCHEMA main TO fs", "CREATE USER fay", "GRANT fs TO fay",
                # A table macro of the administrator's (fay's calls are checked as the
                # plan it expands to).
                "CREATE MACRO main.text_of(p) AS TABLE SELECT content FROM read_text(p)"]:
        expect_allowed(client=admin, sql=sql)
    server.principals = {"admin": admin, "fay": server.user(name="fay")}
    return server


@pytest.fixture(scope="module")
def guarded(home_launcher, fake_home, store):
    return start_server(home_launcher=home_launcher, fake_home=fake_home, store=store, guarded=True)


@pytest.fixture(scope="module")
def open_server(home_launcher, fake_home, store):
    return start_server(home_launcher=home_launcher, fake_home=fake_home, store=store, guarded=False)


def refused(client, sql: str | list[str], category: str = CREDENTIALS) -> None:
    """The last statement of `sql` is refused by the sensitive-path guard, for `category`
    (the statements before it set up the session)."""
    statements = [sql] if isinstance(sql, str) else sql
    for setup in statements[:-1]:
        client.run(sql=setup)
    try:
        client.run(sql=statements[-1])
    except ChargewireError as error:
        assert error.code == "permission_denied", f"{statements[-1]}: {error}"
        assert GUARD in error.detail and f"({category})" in error.detail, f"{statements[-1]}: {error}"
        return
    pytest.fail(reason=f"NOT REFUSED: {statements[-1]}")  # (the result is never printed)


def reads(client, sql: str | list[str], text: str) -> None:
    """The last statement of `sql` succeeds, and its rows hold `text`."""
    statements = [sql] if isinstance(sql, str) else sql
    for setup in statements[:-1]:
        client.run(sql=setup)
    try:
        table = client.run(sql=statements[-1])
    except ChargewireError as error:
        pytest.fail(reason=f"{statements[-1]} failed: {error}")
    if text not in str(table.to_pylist()):
        pytest.fail(reason=f"{statements[-1]}: no row holds {text!r}")


def not_guarded(client, sql: str) -> None:
    """`sql` is not refused by the sensitive-path guard (it may fail for another reason)."""
    try:
        client.run(sql=sql)
    except ChargewireError as error:
        assert GUARD not in error.detail, f"{sql}: refused by the guard with it off: {error}"


# ---- Every way SQL reaches a file: refused with the guard on, read with it off ----

def read_vectors(home: pathlib.Path, who: str) -> dict[str, str | list[str]]:
    key = (home / KEY).as_posix()
    split = len(home.as_posix()) + 3  # inside '.ssh'
    vectors = {
        "read_text": f"SELECT content FROM read_text('{key}')",
        "read_csv": f"SELECT * FROM read_csv('{key}', header = false, columns = {{'line': 'VARCHAR'}})",
        "read_blob": f"SELECT content::VARCHAR AS c FROM read_blob('{key}')",
        "file URL": f"SELECT content FROM read_text('{(home / KEY).as_uri()}')",
        "~": f"SELECT content FROM read_text('~/{KEY}')",
        "relative to the working directory": f"SELECT content FROM read_text('{KEY}')",
        "..": f"SELECT content FROM read_text('{home.as_posix()}/data/../{KEY}')",
        "symlinked directory": f"SELECT content FROM read_text('{home.as_posix()}/data/keys/id_rsa')",
        "concatenated": f"SELECT content FROM read_text('{key[:split]}' || '{key[split:]}')",
        "table macro": f"SELECT * FROM main.text_of('{key}')",
        "COPY FROM": [f"CREATE OR REPLACE TABLE main.copied_{who} (line VARCHAR)",
                      f"COPY main.copied_{who} FROM '{key}' (FORMAT csv, HEADER false)",
                      f"SELECT * FROM main.copied_{who}"],
    }
    if who == "admin":
        # Under access control these are the administrator's only (query(), query_table()
        # and SET VARIABLE are refused to everyone else, before any path is looked at).
        vectors.update({
            "query()": f"SELECT * FROM query('SELECT content FROM read_text(''{key}'')')",
            # (A file query_table() can read: it picks the reader by the extension.)
            "query_table()": f"SELECT * FROM query_table(['{(home / KEY_CSV).as_posix()}'])",
            "computed at run time": [f"SET VARIABLE p = '{key[:split]}' || '{key[split:]}'",
                                     "SELECT content FROM read_text(getvariable('p'))"],
        })
    return vectors


# (Only the names: any absolute path will do, and "/h" is none on Windows.)
VECTORS = [(who, vector) for who in ("admin", "fay") for vector in read_vectors(home=pathlib.Path.cwd(), who=who)]


@pytest.mark.parametrize("who,vector", VECTORS, ids=[f"{who}-{vector}" for who, vector in VECTORS])
def test_reading_a_credential_file_is_refused(guarded, fake_home, who, vector):
    statements = read_vectors(home=fake_home, who=who)[vector]
    if vector == "COPY FROM":
        statements = statements[:-1]  # the COPY itself is refused
    refused(client=guarded.principals[who], sql=statements)


@pytest.mark.parametrize("who,vector", VECTORS, ids=[f"{who}-{vector}" for who, vector in VECTORS])
def test_reading_a_credential_file_with_the_guard_off(open_server, fake_home, who, vector):
    reads(client=open_server.principals[who], sql=read_vectors(home=fake_home, who=who)[vector], text=KEY_TEXT)


@pytest.mark.parametrize("who", ["admin", "fay"])
def test_a_view_is_checked_when_it_is_read(guarded, open_server, fake_home, who):
    # A view bound to an ordinary file, which then becomes a second name of the key (a
    # hard link, which every platform makes without privilege).
    for server in (guarded, open_server):
        name = f"view-{who}-{'guarded' if server is guarded else 'open'}.txt"
        target = fake_home / "data" / name
        target.write_text(PUBLIC_TEXT)
        client = server.principals[who]
        view = f"main.text_view_{who}"
        expect_allowed(client=client, sql=f"CREATE OR REPLACE VIEW {view} AS "
                                          f"SELECT content FROM read_text('{target.as_posix()}')")
        reads(client=client, sql=f"SELECT * FROM {view}", text=PUBLIC_TEXT)
        target.unlink()
        os.link(fake_home / KEY, target)
        if server is guarded:
            refused(client=client, sql=f"SELECT * FROM {view}")
        else:
            reads(client=client, sql=f"SELECT * FROM {view}", text=KEY_TEXT)


@pytest.mark.parametrize("who", ["admin", "fay"])
def test_a_prepared_statements_parameter_is_checked(guarded, open_server, fake_home, who):
    key = (fake_home / KEY).as_posix()
    for server in (guarded, open_server):
        client = server.principals[who]
        prepared = client.prepare(sql="SELECT content FROM read_text(?)")
        if server is open_server:
            table = client.execute_prepared(prepared_id=prepared, parameters=[key])
            if KEY_TEXT not in str(table.to_pylist()):
                pytest.fail(reason="the guard-off twin did not read the dummy key")
            continue
        try:
            client.execute_prepared(prepared_id=prepared, parameters=[key])
        except ChargewireError as error:
            assert GUARD in error.detail and f"({CREDENTIALS})" in error.detail, str(error)
        else:
            pytest.fail(reason="a prepared statement read the key")


@pytest.mark.parametrize("who", ["admin", "fay"])
@pytest.mark.parametrize("relative", list(DUMMY_FILES))
def test_every_credential_store_is_refused(guarded, open_server, fake_home, who, relative):
    sql = f"SELECT content FROM read_text('{(fake_home / relative).as_posix()}')"
    refused(client=guarded.principals[who], sql=sql)
    reads(client=open_server.principals[who], sql=sql, text=DUMMY_FILES[relative])


@pytest.mark.parametrize("who", ["admin", "fay"])
def test_a_second_hard_link_is_refused(guarded, open_server, fake_home, who):
    # No spelling shows it: the guard compares the file's identity.
    sql = f"SELECT content FROM read_text('{(fake_home / 'data' / 'credentials-link').as_posix()}')"
    refused(client=guarded.principals[who], sql=sql)
    reads(client=open_server.principals[who], sql=sql, text=DUMMY_FILES[".aws/credentials"])


@pytest.mark.parametrize("who", ["admin", "fay"])
def test_the_servers_own_credentials_are_refused(guarded, open_server, fake_home, who):
    sql = f"SELECT content FROM read_text('{(fake_home / 'init.sql').as_posix()}')"
    refused(client=guarded.principals[who], sql=sql, category=SERVER_CREDENTIALS)
    reads(client=open_server.principals[who], sql=sql, text="autoload_known_extensions")


@pytest.mark.parametrize("who", ["admin", "fay"])
def test_duckdbs_secret_directory_is_refused(guarded, open_server, who):
    # DuckDB's secret manager reads it past the guarded file system; SQL may not.
    sql = "SELECT * FROM glob('~/.duckdb/stored_secrets/*')"
    refused(client=guarded.principals[who], sql=sql, category=DUCKDB_SECRETS)
    not_guarded(client=open_server.principals[who], sql=sql)


@pytest.mark.parametrize("who", ["admin", "fay"])
def test_system_files_are_refused(guarded, who):
    # Refusal only: no guard-off twin ever reads these.
    client = guarded.principals[who]
    refused(client=client, sql=f"SELECT content FROM read_text('{protected_system_file(posix='/etc/shadow')}')",
            category="system account files")
    # Kubernetes' secret mounts: Windows pods get them at C:\var\run\secrets.
    secrets = f"{os.environ.get('SystemDrive', 'C:')}/var/run" if sys.platform == "win32" else "/var/run"
    refused(client=client, sql=f"SELECT content FROM read_text('{secrets}/secrets/kubernetes.io/token')",
            category="container and Kubernetes secrets")
    # POSIX only: Windows has no /etc/sudoers or /proc, so the guard's Windows list doesn't
    # name them (its account database, the SAM, is the shadow case above).
    if sys.platform != "win32":
        refused(client=client, sql="SELECT content FROM read_text('/etc/sudoers')", category="system account files")
        refused(client=client, sql="SELECT content FROM read_text('/proc/self/environ')",
                category="process information (/proc)")


# ---- Attaching ----

def attach_vectors(home: pathlib.Path) -> dict[str, str]:
    key = (home / KEY).as_posix()
    return {
        "DuckDB storage": f"ATTACH '{key}' AS evil",
        "TYPE sqlite": f"ATTACH '{key}' AS evil (TYPE sqlite)",
        "sqlite: prefix": f"ATTACH 'sqlite:{key}' AS evil",
        "computed path": f"ATTACH '{key[:-3]}' || 'rsa' AS evil (TYPE sqlite)",
    }


@pytest.mark.parametrize("who", ["admin", "fay"])
@pytest.mark.parametrize("vector", list(attach_vectors(home=pathlib.Path.cwd())))
def test_attaching_a_credential_file_is_refused(guarded, open_server, fake_home, who, vector):
    sql = attach_vectors(home=fake_home)[vector]
    refused(client=guarded.principals[who], sql=sql)
    # With the guard off it fails for what the file is (no database; sqlite_scanner not
    # loaded), never the guard.
    not_guarded(client=open_server.principals[who], sql=sql)


# ---- Writing ----

@pytest.mark.parametrize("who", ["admin", "fay"])
def test_writing_into_a_credential_store_is_refused(guarded, open_server, fake_home, who):
    target = fake_home / ".ssh" / f"authorized_keys-{who}"
    sql = f"COPY (SELECT 'ssh-ed25519 AAAA attacker' AS k) TO '{target.as_posix()}' (FORMAT csv, HEADER false)"
    refused(client=guarded.principals[who], sql=sql)
    assert not target.exists(), "a refused COPY left the file behind"
    expect_allowed(client=open_server.principals[who], sql=sql)
    assert target.exists()
    target.unlink()


# ---- Listing: a protected path's entries are left out, not refused ----

@pytest.mark.parametrize("who", ["admin", "fay"])
def test_globs_from_above_leave_protected_matches_out(guarded, open_server, fake_home, who):
    home = fake_home.as_posix()
    pattern = f"SELECT file FROM glob('{home}/*/id_rsa') ORDER BY file"
    names = [pathlib.PurePosixPath(f).name for f in
             guarded.principals[who].run(sql=pattern).column("file").to_pylist()]
    assert names == [], "a protected file was listed"
    # With the guard off the same pattern finds the key (DuckDB's glob doesn't follow the
    # symlinked directory).
    found = open_server.principals[who].run(sql=pattern).column("file").to_pylist()
    assert [pathlib.Path(f) for f in found] == [fake_home / KEY]
    # Ordinary matches are still listed; a pattern whose fixed part is protected is refused.
    listed = guarded.principals[who].run(sql=f"SELECT file FROM glob('{home}/data/pub*')").column("file").to_pylist()
    assert [pathlib.Path(f) for f in listed] == [fake_home / "data" / "public.txt"]
    refused(client=guarded.principals[who], sql=f"SELECT file FROM glob('{home}/.ssh/*')")


@pytest.mark.parametrize("who", ["admin", "fay"])
def test_file_search_path_finds_no_protected_file(guarded, open_server, fake_home, who):
    statements = [f"SET file_search_path = '{(fake_home / '.ssh').as_posix()}'",
                  "SELECT content FROM read_text('id_rsa')"]
    client = guarded.principals[who]
    try:
        client.run(sql=statements[0])
        rows = client.run(sql=statements[1]).num_rows
    finally:
        client.run(sql="RESET file_search_path")
    assert rows == 0, "file_search_path reached a protected file"
    open_client = open_server.principals[who]
    try:
        reads(client=open_client, sql=statements, text=KEY_TEXT)
    finally:
        open_client.run(sql="RESET file_search_path")


# ---- Ordinary files and literals: no false positives ----

@pytest.mark.parametrize("who", ["admin", "fay"])
def test_ordinary_files_in_the_home_directory_stay_readable(guarded, fake_home, who):
    client = guarded.principals[who]
    for sql in [f"SELECT content FROM read_text('{(fake_home / 'data' / 'public.txt').as_posix()}')",
                "SELECT content FROM read_text('data/public.txt')",
                "SELECT content FROM read_text('~/data/public.txt')",
                "SELECT * FROM main.text_of('data/public.txt')",
                *(["SELECT * FROM query('SELECT content FROM read_text(''data/public.txt'')')"]
                  if who == "admin" else [])]:
        reads(client=client, sql=sql, text=PUBLIC_TEXT)


# Literals the 0.3.0 text scan refused in a server whose working directory holds ~/.ssh.
LITERALS = [
    ("SELECT '*' AS v", "*"),
    ("SELECT '.*' AS v", ".*"),
    ("SELECT regexp_matches('anything', '.*') AS v", True),
    ("SELECT format('{:,}', 1234567) AS v", "1,234,567"),
    ("SELECT regexp_extract('ab12', '(\\d{2})', 1) AS v", "12"),
    ("""SELECT '{"a": {"b": [1, 2]}}'::JSON ->> '$.a.b[1]' AS v""", "2"),
    ("SELECT '~/.ssh/id_rsa' AS v", "~/.ssh/id_rsa"),
    ("SELECT length('.ssh/id_rsa') AS v", 11),
    ("SELECT '/etc/shadow' LIKE '/etc/%' AS v", True),
    ("SELECT replace('{home}/.aws/credentials', '{home}', '~') AS v", "~/.aws/credentials"),
]


@pytest.mark.parametrize("who", ["admin", "fay"])
@pytest.mark.parametrize("sql,expected", LITERALS, ids=[sql for sql, _ in LITERALS])
def test_literals_are_not_paths(guarded, who, sql, expected):
    assert guarded.principals[who].run(sql=sql).column("v").to_pylist() == [expected]


def test_literals_in_views(guarded):
    admin = guarded.principals["admin"]
    columns = ", ".join(f"{sql.removeprefix('SELECT ').removesuffix(' AS v')} AS v{i}"
                        for i, (sql, _) in enumerate(LITERALS))
    expect_allowed(client=admin, sql=f"CREATE OR REPLACE VIEW main.literals AS SELECT {columns}")
    expect_allowed(client=admin, sql="GRANT SELECT ON main.literals TO fs")
    for who in ("admin", "fay"):
        row = guarded.principals[who].run(sql="SELECT * FROM main.literals").to_pylist()
        assert row == [{f"v{i}": expected for i, (_, expected) in enumerate(LITERALS)}]
