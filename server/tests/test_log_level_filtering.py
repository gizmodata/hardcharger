"""Port of GizmoSQL 1.x tests/integration/test_log_level_filtering.cpp (14 cases).

The contract (a message appears only when its own level reaches the component's
threshold, always at its own level, never promoted: 1.x issue #136; the logger's
threshold applies too unless a session asked for more detail) is a C++ unit test of
Hardcharger's LogAt() (server/unit/log_level_test.cc), run here. All 14 apply.
"""

import pytest

from conftest import run_unit_tests

CASES = ["LogLevelFilteringTest.DynamicAt_InfoMessageSuppressedWhenThresholdIsError",
         "LogLevelFilteringTest.DynamicAt_InfoMessageShownWhenThresholdIsInfo",
         "LogLevelFilteringTest.DynamicAt_InfoMessageShownWhenThresholdIsDebug",
         "LogLevelFilteringTest.DynamicAt_ErrorMessageShownWhenThresholdIsError",
         "LogLevelFilteringTest.DynamicAt_InfoSuppressedByOverallLoggerThreshold",
         "SessionLogLevelFilteringTest.SessionDynamicAt_DebugMessageSuppressedWhenThresholdIsInfo",
         "SessionLogLevelFilteringTest.SessionDynamicAt_InfoMessageShownWhenThresholdIsInfo",
         "SessionLogLevelFilteringTest.SessionDynamicAt_InfoMessageNotPromotedWhenThresholdIsDebug",
         "SessionLogLevelFilteringTest.SessionDynamicAt_WarningSuppressedWhenThresholdIsError",
         "SessionLogLevelFilteringTest.SessionDynamicAt_ErrorShownWhenThresholdIsWarning",
         "SessionLogLevelFilteringTest.SessionDynamic_ShownAtNaturalSeverity",
         "SessionLogLevelFilteringTest.SessionDynamic_IgnoresLoggerThreshold",
         "LogLevelFilteringTest.BothThresholdsMustBeSatisfied",
         "LogLevelFilteringTest.WarningShownWhenBothThresholdsMet"]


@pytest.fixture(scope="module")
def results():
    return run_unit_tests(gtest_filter="*LogLevelFilteringTest.*")


def test_every_unit_case_ran(results):
    assert len(results) == len(CASES), sorted(results)


@pytest.mark.parametrize("case", CASES)
def test_unit_case(results, case):
    assert results.get(case) is True, results
