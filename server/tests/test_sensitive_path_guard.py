"""Port of GizmoSQL 1.x tests/integration/test_sensitive_path_guard.cpp (10 cases: 5 unit, 5 server).

block_sensitive_paths (default on, HARDCHARGER_BLOCK_SENSITIVE_PATHS) keeps credential and
system files out of SQL's reach for every user, admins included; sensitive_paths
(HARDCHARGER_SENSITIVE_PATHS) adds the operator's own (GizmoData Cloud passes its secret
mounts). The 5 policy cases are C++ unit tests (server/unit/sensitive_paths_test.cc),
run here; the 5 server cases run below. All 10 apply.

As in 1.x, the guard sits in DuckDB's file system (ServerFileGuard,
server/src/authz/file_guard.cc), so a path is checked as DuckDB resolved it, however the
statement computed it. A glob that reaches a protected path from above lists its other
matches only (1.x's "ProtectedEntriesHiddenFromGlob"); one whose fixed part is protected
is refused. test_sensitive_path_fs_layer.py covers every way SQL reaches a file.
"""

import sys

import pytest

from conftest import Launcher, home_env, protected_system_file, run_unit_tests

# The 5 1.x cases, plus each platform's own: Windows has no /proc (its device namespace,
# credential stores and path spellings instead); elsewhere every file: URL form.
UNIT_CASES = ["DefaultCategories", "OrdinaryPathsAreAllowed", "OtherSpellingsResolveToTheSamePath",
              "SecretDirectoryAddedAfterStartup",
              # '{' is no wildcard: DuckDB's glob has no brace expansion.
              "BracesAreOrdinaryCharacters", "PatternsAreCheckedByTheirFixedPart", "HardLinksToProtectedFilesAreRefused",
              *(["WindowsStoresAndSpellings"] if sys.platform == "win32"
                else ["OnlyOwnCgroupFileIsReadableInProc", "EveryFileUrlForm"])]


@pytest.fixture(scope="module")
def unit_results():
    return run_unit_tests(gtest_filter="SensitivePathPolicyTest.*")


@pytest.mark.parametrize("case", UNIT_CASES)
def test_unit_case(unit_results, case):
    assert unit_results[f"SensitivePathPolicyTest.{case}"] is True


@pytest.fixture(scope="module")
def guard_dir(tmp_path_factory):
    directory = tmp_path_factory.mktemp("spg").resolve()
    (directory / "protected").mkdir()
    (directory / "protected" / "token.txt").write_text("do-not-read")
    (directory / "public.csv").write_text("a\n1\n")
    return directory


@pytest.fixture(scope="module")
def admin(module_launcher, guard_dir):
    server = module_launcher.start(env={"HARDCHARGER_SENSITIVE_PATHS": str(guard_dir / "protected")})
    return server.admin()


SYSTEM_ACCOUNTS_FILE = protected_system_file(posix="/etc/passwd")


def blocked(problem: dict) -> None:
    assert problem["code"] == "permission_denied", problem
    assert "blocked access to" in problem["detail"] and "Permission denied" in problem["detail"], problem


def test_admin_is_refused_protected_paths(admin, guard_dir):
    token = guard_dir / "protected" / "token.txt"
    for sql in [f"SELECT content FROM read_text('{token}')", f"SELECT * FROM read_csv('{token}')",
                f"SELECT content FROM read_text('{SYSTEM_ACCOUNTS_FILE}')",
                f"SELECT * FROM read_text('{guard_dir}/public.csv/../protected/token.txt')",
                f"SELECT * FROM read_text('file://{token}')", f"SELECT * FROM '{token}'",
                # file:/x (one slash) is DuckDB's file URL too (and on Windows file:/C:/x).
                f"SELECT * FROM read_text('file:/{token.as_posix().lstrip('/')}')",
                f"SELECT content FROM read_text('file:/{SYSTEM_ACCOUNTS_FILE.lstrip('/')}')"]:
        blocked(problem=admin.error(sql=sql))


def test_ordinary_files_still_readable(admin, guard_dir):
    assert admin.query(sql=f"SELECT * FROM read_csv('{guard_dir / 'public.csv'}')").num_rows == 1


def test_protected_entries_hidden_from_glob(admin, guard_dir):
    rows = admin.query(sql=f"SELECT file FROM glob('{guard_dir}/*/*')").column("file").to_pylist()
    assert not [f for f in rows if f.endswith("token.txt")], "a protected file was listed"
    # A pattern that can't reach a protected path lists normally.
    assert admin.query(sql=f"SELECT file FROM glob('{guard_dir}/*.csv')").num_rows == 1


def test_secret_directory_cannot_be_changed(admin):
    problem = admin.error(sql="SET secret_directory = '/tmp/elsewhere'")
    assert "blocked changing secret_directory" in problem["detail"], problem


def test_guard_off_allows_access(launcher, guard_dir):
    server = launcher.start(env={"HARDCHARGER_SENSITIVE_PATHS": str(guard_dir / "protected"),
                                 "HARDCHARGER_BLOCK_SENSITIVE_PATHS": "false"})
    token = guard_dir / "protected" / "token.txt"
    assert server.admin().scalar(sql=f"SELECT content FROM read_text('{token}')") == "do-not-read"


# ---- Braces (Hardcharger 0.3.0 refused every literal with a '{') ----
#
# The guard took '{' for a glob wildcard reaching anywhere below the pattern's directory,
# and a literal with no directory part is relative to the working directory. So a server
# started in its home directory (the Docker image's working directory is its home, with
# ~/.ssh in it) refused every JSON or regex literal with a brace, for "user credential
# store". DuckDB's glob has no brace expansion: '{' is an ordinary character in a path.

@pytest.fixture(scope="module")
def home_dir(tmp_path_factory):
    """A server directory that is also its home, holding a protected ~/.ssh, as in the image."""
    directory = tmp_path_factory.mktemp("home").resolve()
    (directory / ".ssh").mkdir()
    (directory / ".ssh" / "key").write_text("do-not-read")
    (directory / "data").mkdir()
    for name in ["a.txt", "b.txt"]:
        (directory / "data" / name).write_text(name)
    return directory


@pytest.fixture(scope="module")
def home_launcher(home_dir):
    launch = Launcher(tmp_path=home_dir)  # the servers' working directory
    yield launch
    launch.stop_all()


@pytest.fixture(scope="module")
def home_admin(home_launcher, home_dir):
    return home_launcher.start(env=home_env(home=home_dir)).admin()


BRACE_QUERIES = [
    ("""SELECT '{"a": 1}' AS v""", '{"a": 1}'),
    ("""SELECT ('{"a": {"b": [1, 2]}}'::JSON ->> '$.a.b[1]') AS v""", "2"),
    ("""SELECT json_extract_string('{"a": {"b": "x"}}', '$.a.b') AS v""", "x"),
    ("SELECT regexp_matches('123', '^[0-9]{3}$') AS v", True),
    ("SELECT regexp_full_match('ab-12', '[a-z]{1,2}-\\d{2}') AS v", True),
    ("SELECT regexp_extract('aaaa', 'a{2,3}') AS v", "aaa"),
    ("SELECT regexp_replace('2026-10-19', '^[0-9]{4}', 'YYYY') AS v", "YYYY-10-19"),
    ("SELECT '{}' AS v", "{}"),
    ("SELECT format('{}-{}', 1, 2) AS v", "1-2"),
    ("SELECT replace('{x}', '{', '(') AS v", "(x}"),
    ("SELECT string_split('a{b{c', '{')[2] AS v", "b"),
    ("SELECT strpos('x{y', '{') AS v", 2),
    ("SELECT '{a,b}' LIKE '{%}' AS v", True),
]


@pytest.mark.parametrize("sql,expected", BRACE_QUERIES, ids=[q for q, _ in BRACE_QUERIES])
def test_brace_literals_are_not_paths(home_admin, sql, expected):
    assert home_admin.scalar(sql=sql) == expected


def test_brace_literals_in_views(home_admin):
    home_admin.execute(sql="CREATE TABLE codes AS SELECT * FROM (VALUES ('123'), ('12a')) t(code)")
    home_admin.execute(sql="""CREATE VIEW code_checks AS SELECT code, regexp_matches(code, '^[0-9]{3}$') AS ok, """
                           """'{"source": "codes"}'::JSON AS meta, '{}' AS empty FROM codes""")
    rows = home_admin.rows(sql="SELECT code, ok, meta->>'source' AS source, empty FROM code_checks ORDER BY code")
    assert rows == [{"code": "123", "ok": True, "source": "codes", "empty": "{}"},
                    {"code": "12a", "ok": False, "source": "codes", "empty": "{}"}]


def test_braced_paths_reaching_protected_files_are_refused(home_admin, home_dir):
    parent, _, name = SYSTEM_ACCOUNTS_FILE.rpartition("/")
    for sql in [f"SELECT content FROM read_text('{home_dir}/.ssh/{{key,other}}')",
                "SELECT content FROM read_text('.ssh/{key}')",
                f"SELECT content FROM read_text('{parent}/{{a,b}}/../{name}')"]:  # .. past a braced name
        blocked(problem=home_admin.error(sql=sql))
    # A wildcard before the protected part lists the other matches only.
    for sql in [f"SELECT file FROM glob('{home_dir}/.ss?/{{key}}')", f"SELECT file FROM glob('{home_dir}/*/{{k}}*')",
                "SELECT file FROM glob('**/{key}')", f"SELECT file FROM glob('{home_dir}/.ss?/*')"]:
        assert home_admin.query(sql=sql).num_rows == 0, sql


def test_duckdb_does_not_expand_braces(home_launcher, home_dir):
    """The premise: '{a,b}' in a path is no pattern for DuckDB (with the guard off, as it
    would see the path). If DuckDB ever expands braces, '{' must be a wildcard again."""
    server = home_launcher.start(env={**home_env(home=home_dir), "HARDCHARGER_BLOCK_SENSITIVE_PATHS": "false"})
    admin = server.admin()
    assert admin.query(sql=f"SELECT * FROM glob('{home_dir}/data/*.txt')").num_rows == 2
    assert admin.query(sql=f"SELECT * FROM glob('{home_dir}/data/{{a,b}}.txt')").num_rows == 0
    assert admin.query(sql=f"SELECT * FROM read_text('{home_dir}/data/{{a,b}}.txt')").num_rows == 0
    server.stop()


def test_brace_literals_in_masking_policies(home_launcher, home_dir, store):
    from rbac import expect_allowed, start_rbac_server
    server = start_rbac_server(launcher=home_launcher, store_uri=store.new_database(),
                               env=home_env(home=home_dir), args=["--database", str(home_dir / "masks.duckdb")])
    admin = server.admin()
    for sql in [
        "CREATE TABLE people (name VARCHAR, ssn VARCHAR, prefs VARCHAR)",
        """INSERT INTO people VALUES ('ann', '123-45-6789', '{"theme": "dark"}')""",
        "CREATE MASKING POLICY ssn_mask AS (val VARCHAR) RETURNS VARCHAR -> "
        "regexp_replace(val, '^[0-9]{3}-[0-9]{2}', '***-**')",
        """CREATE MASKING POLICY prefs_mask AS (val VARCHAR) RETURNS VARCHAR -> '{"masked": true}'""",
        "ALTER TABLE people MODIFY COLUMN ssn SET MASKING POLICY ssn_mask",
        "ALTER TABLE people MODIFY COLUMN prefs SET MASKING POLICY prefs_mask",
        "CREATE ROLE readers", "GRANT SELECT ON people TO readers", "CREATE USER ann", "GRANT readers TO ann",
    ]:
        expect_allowed(client=admin, sql=sql)
    rows = expect_allowed(client=server.user(name="ann"), sql="SELECT ssn, prefs FROM people").to_pylist()
    assert rows == [{"ssn": "***-**-6789", "prefs": '{"masked": true}'}]
