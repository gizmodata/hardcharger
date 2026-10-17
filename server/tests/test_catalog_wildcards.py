"""Port of GizmoSQL 1.x tests/integration/test_catalog_wildcards.cpp (12 cases).

The wildcard matcher (only * and ?, case-sensitive for catalogs) is a C++ unit test
(server/unit/patterns_test.cc, every 1.x case verbatim against MatchesPattern), run
here; plus two Hardcharger cases (case-insensitive e-mails, a pathological pattern).
"""

import pytest

from conftest import run_unit_tests

CASES = ["ExactMatch", "ExactMatchIsCaseSensitive", "EmptyStrings", "StarMatchesEverything", "PrefixGlob",
         "SuffixGlob", "InfixGlob", "MultipleStars", "QuestionMark", "MixedQuestionAndStar", "BacktrackingRequired",
         "NonWildcardSpecialCharsAreLiteral", "EmailMatchingIgnoresCase", "PathologicalPatternsAreLinear"]


@pytest.fixture(scope="module")
def results():
    return run_unit_tests(gtest_filter="CatalogWildcards.*")


def test_every_unit_case_ran(results):
    assert len(results) == len(CASES), sorted(results)


@pytest.mark.parametrize("case", sorted(CASES))
def test_unit_case(results, case):
    assert results.get(f"CatalogWildcards.{case}") is True, results
