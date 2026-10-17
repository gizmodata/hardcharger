"""Port of GizmoSQL 1.x tests/integration/test_sensitive_path_guard.cpp (10 cases: 5 unit, 5 server).

block_sensitive_paths (default on, HARDCHARGER_BLOCK_SENSITIVE_PATHS) keeps credential and
system files out of SQL's reach for every user, admins included; sensitive_paths
(HARDCHARGER_SENSITIVE_PATHS) adds the operator's own (GizmoData Cloud passes its secret
mounts). The 5 policy cases are C++ unit tests (server/unit/sensitive_paths_test.cc),
run here; the 5 server cases run below. All 10 apply.

How it differs from 1.x: 1.x wrapped DuckDB's file system, which the v2 C API cannot do,
so Hardcharger checks every path the SQL text names (literals and quoted identifiers,
however spelled: .., symlinks, file://, ~, relative). So glob() over a directory that
holds a protected file is refused rather than listed without that file; the 1.x case
"ProtectedEntriesHiddenFromGlob" asserts the property that matters, that a protected
file is never listed, and passes either way. A path an admin computes at run time
('/etc/' || 'passwd') is not seen; non-admins can't use computed paths at all (the
admin command guard fails closed).
"""

import os
import sys

import pytest

from conftest import run_unit_tests

# The 5 1.x cases, plus each platform's own: Windows has no /proc (its device namespace,
# credential stores and path spellings instead); elsewhere every file: URL form.
UNIT_CASES = ["DefaultCategories", "OrdinaryPathsAreAllowed", "OtherSpellingsResolveToTheSamePath",
              "SecretDirectoryAddedAfterStartup",
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


# A system file every host has that the guard always protects.
SYSTEM_ACCOUNTS_FILE = (f"{os.environ.get('SystemRoot', 'C:/Windows')}/System32/config/SAM"
                        if sys.platform == "win32" else "/etc/passwd")


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
    pattern = f"{guard_dir}/*/*"
    response = admin.submit(sql=f"SELECT file FROM glob('{pattern}') WHERE file LIKE '%token.txt'", arrow=True)
    if response.status_code == 200 and response.headers["content-type"].startswith("application/vnd.apache.arrow"):
        from warp_client import read_table
        assert read_table(content=response.content).num_rows == 0, "a protected file was listed"
    else:
        blocked(problem=response.json())
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
