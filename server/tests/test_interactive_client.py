"""Port of GizmoSQL 1.x tests/integration/test_interactive_client.cpp (115 cases).

The 1.x file tests the client's classes directly, so its port stays C++
(server/client/tests/interactive_client_test.cc, the same 115 cases against the
hardcharger client's classes), run here against a real hardcharger-server started with
the 1.x fixture's tables. All 115 apply; FlightConnection is ChargewireConnection. Two are
re-expressed (see the .cc file's header): TlsSkipVerifyWithMtlsCertIsRejected (Chargewire
sends the client certificate with verification off, so the combination is allowed) and
DotShow (no edition line: Hardcharger has no editions).
"""

import pathlib

import pytest

from conftest import gtest_cases, run_client_tests

SOURCE = pathlib.Path(__file__).resolve().parent.parent / "client" / "tests" / "interactive_client_test.cc"
CASES = gtest_cases(source=SOURCE)
INIT_SQL = ("CREATE TABLE test_data (id INTEGER, name VARCHAR, value DOUBLE);"
            "INSERT INTO test_data VALUES (1, 'Alice', 10.5);"
            "INSERT INTO test_data VALUES (2, 'Bob', 20.3);"
            "INSERT INTO test_data VALUES (3, 'Charlie', 30.1);"
            "CREATE TABLE empty_table (x INTEGER);")


@pytest.fixture(scope="module")
def results(module_launcher):
    server = module_launcher.start(args=["--init-sql-commands", INIT_SQL], username="clientuser")
    suites = sorted({case.split(".")[0] for case in CASES})
    return run_client_tests(gtest_filter=":".join(f"{suite}.*" for suite in suites),
                            env={"HARDCHARGER_TEST_CLIENT_PORT": str(server.port),
                                 "HARDCHARGER_TEST_CLIENT_USERNAME": server.username,
                                 "HARDCHARGER_TEST_CLIENT_PASSWORD": server.password})


def test_every_case_is_ported():
    # 1.x's 115, plus the two shutdown cases with SIGINT ignored (a script's background job)
    # and two of dollar-quoted strings (SqlProcessorDollar*).
    assert len(CASES) == 119, CASES


def test_every_case_ran(results):
    assert sorted(results) == sorted(CASES)


@pytest.mark.parametrize("case", CASES)
def test_case(results, case):
    assert results.get(case) is True, f"{case} failed"
