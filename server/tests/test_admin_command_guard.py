"""Port of GizmoSQL 1.x tests/integration/test_admin_command_guard.cpp (42 cases).

36 cases unit-test the classifier: they are C++ unit tests against a real DuckDB
connection (server/unit/guards_test.cc, the 1.x bodies verbatim), run here. 1.x
classified from DuckDB's parse tree; Hardcharger classifies from DuckDB's tokens and
statement types through the v2 C API, failing closed where a path is not a literal
remote URL. The 6 server cases run below, with non-admin principals from tokens in the
server's own format (1.x: role "analyst"). All 42 apply.
"""

import pytest

from conftest import run_unit_tests

UNIT_CASES = [
    "AdminCommandGuard.AttachIsGated", "AdminCommandGuard.DetachIsGated",
    "AdminCommandGuard.ExplainWrappedStatementsAreGated", "AdminCommandGuard.AttachDetachEvasionStillCaught",
    "UnredactedSecretsSet.TrueIsMatched", "UnredactedSecretsSet.TrueEquivalentsAreMatched",
    "UnredactedSecretsSet.ExplainWrappedSetIsMatched", "UnredactedSecretsSet.FalseAndOtherSqlAreNotMatched",
    "AdminCommandGuard.SetGlobalIsGated", "AdminCommandGuard.BareSetOfDangerousGlobalSettingIsGated",
    "AdminCommandGuard.HarmlessSessionSetIsAllowed", "AdminCommandGuard.InstallLoadIsGated",
    "AdminCommandGuard.CheckpointIsGated", "AdminCommandGuard.HarmlessPragmaIsAllowed",
    "AdminCommandGuard.CopyLocalIsGated", "AdminCommandGuard.CopyRemoteIsAllowed",
    "AdminCommandGuard.CopyRemoteButNestedLocalReadIsGated", "AdminCommandGuard.ExportImportDatabaseAlwaysGated",
    "AdminCommandGuard.ReadFunctionsLocalAreGated", "AdminCommandGuard.ReadFunctionsRemoteAreAllowed",
    "AdminCommandGuard.ReadFunctionsNonLiteralPathIsGated", "AdminCommandGuard.ReadFunctionsNestedAreGated",
    "AdminCommandGuard.PrepareIndirectionIsGated", "AdminCommandGuard.CreateDropSecretIsGated",
    "AdminCommandGuard.DuckDBSecretsIsAlwaysGated", "AdminCommandGuard.LocalReplacementScanIsGated",
    "AdminCommandGuard.RemoteReplacementScanIsAllowed", "AdminCommandGuard.QuackFunctionsAreGated",
    "AdminCommandGuard.CopyInsideCteIsGated", "AdminCommandGuard.ConnectAndExternalResourceAreGated",
    "AdminCommandGuard.NewSingleFileReadersAreGated", "AdminCommandGuard.DmlSourcesAreWalked",
    "AdminCommandGuard.OrdinaryQueriesAreAllowed", "AdminCommandGuard.UnparseableSqlIsAllowedThroughToDuckDB",
    "AdminCommandGuard.CheckWrapperReturnsErrorForGated", "AdminCommandGuard.CheckWrapperOkForAllowed",
]


@pytest.fixture(scope="module")
def unit_results():
    return run_unit_tests(gtest_filter="AdminCommandGuard.*:UnredactedSecretsSet.*")


def test_every_unit_case_ran(unit_results):
    assert sorted(unit_results) == sorted(UNIT_CASES)


@pytest.mark.parametrize("case", UNIT_CASES)
def test_unit_case(unit_results, case):
    assert unit_results[case] is True


@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start(env={"HARDCHARGER_ALLOW_CROSS_INSTANCE_TOKENS": "true"})


@pytest.fixture(scope="module")
def analyst(server):
    return server.as_role(role="analyst", subject="analyst_user")


@pytest.mark.parametrize("sql,category", [
    ("ATTACH 'side.db' AS side", "ATTACH"),
    ("DETACH some_catalog", "DETACH"),
    ("SET GLOBAL memory_limit = '512MB'", "SET GLOBAL memory_limit"),
    ("INSTALL httpfs", "INSTALL extension"),
    ("SELECT * FROM read_csv('/etc/passwd')", "read_csv() (local filesystem)"),
    ("SELECT * FROM duckdb_secrets()", "duckdb_secrets()"),
    ("CHECKPOINT", "CHECKPOINT"),
])
def test_non_admin_token_rejected_for_gated_commands(analyst, sql, category):
    problem = analyst.error(sql=sql)
    assert problem["code"] == "permission_denied" and problem["status"] == 403, problem
    assert "'admin' role" in problem["detail"] and category in problem["detail"], problem


def test_non_admin_token_allowed_for_ordinary_queries(analyst):
    assert analyst.scalar(sql="SELECT 1 AS x") == 1
    assert analyst.query(sql="SELECT * FROM range(10)").num_rows == 10
    analyst.execute(sql="SET search_path = 'main'")


def test_admin_basic_auth_unaffected(server):
    admin = server.admin()
    admin.execute(sql="SET GLOBAL memory_limit = '512MB'")
    admin.execute(sql="CHECKPOINT")


def test_admin_set_unredacted_secrets_true_is_rejected(server):
    admin = server.admin()
    problem = admin.error(sql="SET allow_unredacted_secrets = true")
    assert problem["code"] == "permission_denied" and "disabled on this server" in problem["detail"], problem
    admin.execute(sql="SET allow_unredacted_secrets = false")


def test_sql_with_nul_byte_is_rejected(server):
    problem = server.admin().error(sql="SELECT 1\x00; SET allow_unredacted_secrets = true")
    assert "NUL byte" in problem["detail"], problem


def test_set_true_reaches_duckdb_when_the_block_is_off(launcher):
    admin = launcher.start(env={"HARDCHARGER_BLOCK_UNREDACTED_SECRETS": "false"}).admin()
    problem = admin.error(sql="SET allow_unredacted_secrets = true")
    assert "disabled on this server" not in problem["detail"], problem
    assert "allow_unredacted_secrets" in problem["detail"], problem  # DuckDB's own refusal
