"""Port of GizmoSQL 1.x tests/integration/test_client_config.cpp (5 cases).

C++ unit tests of the client's environment variables
(server/client/tests/client_config_test.cc), with Hardcharger's names: GIZMOSQL_USER is
HARDCHARGER_USER and the server's GIZMOSQL_USERNAME is HARDCHARGER_ADMIN_USERNAME. All 5
apply. Added: no GIZMOSQL_* variable is read, and HARDCHARGER_PASSWORD (the user's own
credential) never falls back to the server administrator's HARDCHARGER_ADMIN_PASSWORD.
Also added: connection URIs (ClientConnectionUriTest), every form the Hardcharger ADBC
driver accepts and refuses, and that the environment never changes what a URI names; and
that TLS is on by default (ClientTlsDefaultTest), off only with --tls=false,
HARDCHARGER_TLS=false or a URI's ?tls=false.
"""

import pathlib

import pytest

from conftest import gtest_cases, run_client_tests

SOURCE = pathlib.Path(__file__).resolve().parent.parent / "client" / "tests" / "client_config_test.cc"
CASES = gtest_cases(source=SOURCE)
PORTED = ["UsernameFromHardchargerUser", "UsernameFallsBackToHardchargerAdminUsername",
          "HardchargerUserWinsOverHardchargerAdminUsername", "ExplicitUsernameNotOverriddenByEnv",
          "NoEnvLeavesUsernameEmpty"]


@pytest.fixture(scope="module")
def results():
    return run_client_tests(gtest_filter="ClientConfigEnvTest.*:ClientConnectionUriTest.*:ClientTlsDefaultTest.*")


def test_every_1x_case_is_ported():
    assert [f"ClientConfigEnvTest.{case}" for case in PORTED] == CASES[:5]
    assert len([case for case in CASES if case.startswith("ClientConfigEnvTest.")]) == 7  # +2
    assert len([case for case in CASES if case.startswith("ClientConnectionUriTest.")]) == 7
    assert len([case for case in CASES if case.startswith("ClientTlsDefaultTest.")]) == 1


@pytest.mark.parametrize("case", CASES)
def test_case(results, case):
    assert results.get(case) is True, results
