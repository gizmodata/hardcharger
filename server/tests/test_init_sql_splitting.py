"""Port of GizmoSQL 1.x tests/integration/test_init_sql_splitting.cpp (7 cases).

The 6 splitter cases are C++ unit tests of Hardcharger's SplitStatements() (DuckDB's own
tokenizer; server/unit/sql_text_test.cc), run here. The server case starts a server whose
init SQL has semicolons in strings, quoted identifiers, comments and a $$ body.
"""

import pytest

from conftest import run_unit_tests

SPLITTER_CASES = ["SplitsOnTopLevelSemicolons", "SemicolonInStringLiteralDoesNotSplit",
                  "SemicolonInQuotedIdentifierDoesNotSplit", "SemicolonInCommentsDoesNotSplit",
                  "SemicolonInDollarQuotedStringDoesNotSplit", "UnterminatedQuoteRunsToTheEnd"]


@pytest.fixture(scope="module")
def unit_results():
    return run_unit_tests(gtest_filter="SqlText.*")


@pytest.mark.parametrize("case", SPLITTER_CASES)
def test_splitter(unit_results, case):
    assert unit_results.get(f"SqlText.{case}") is True, unit_results


def test_init_sql_with_semicolons_inside_runs_intact(launcher):
    init_sql = ("-- create the objects; the cluster needs them\n"
                "CREATE TABLE \"semi;colon\" (note VARCHAR);\n"
                "/* seed; one row */ INSERT INTO \"semi;colon\" VALUES ('a;b');\n"
                "CREATE MACRO semi_text() AS $$x;y$$;\n"
                "-- done;\n")
    server = launcher.start(env={"HARDCHARGER_INIT_SQL_COMMANDS": init_sql})
    rows = server.admin().rows(sql='SELECT note, semi_text() AS m FROM "semi;colon"')
    assert rows == [{"note": "a;b", "m": "x;y"}]
