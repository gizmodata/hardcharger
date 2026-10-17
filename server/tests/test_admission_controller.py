"""Port of GizmoSQL 1.x tests/integration/test_admission_controller.cpp (14 cases).

The statement queue's admission controller is a C++ unit test
(server/unit/admission_test.cc, the same 14 cases against Hardcharger's controller), run
here. All 14 apply.
"""

import pytest

from conftest import run_unit_tests

CASES = ["DisabledLimitIsUnlimited", "EnforceFalseBypasses", "TracksAndCapsActive", "BlocksUntilSlotFrees",
         "ResizeUpWakesWaiters", "DisableWhileWaitingReleasesWaiters", "MaxQueuedRejects", "MaxQueueWaitTimesOut",
         "ConcurrencyNeverExceedsLimit", "AdmitsInFifoOrder", "TimedOutWaiterIsRemovedFifoContinues",
         "AbortedWaiterIsCancelledViaWakeWaiters", "AbortBeforeQueuingReturnsCancelled",
         "PolledAbortDoesNotNeedWakeOrSlotRelease"]


@pytest.fixture(scope="module")
def results():
    return run_unit_tests(gtest_filter="AdmissionControllerTest.*")


def test_every_unit_case_ran(results):
    assert len(results) == len(CASES), sorted(results)


@pytest.mark.parametrize("case", CASES)
def test_unit_case(results, case):
    assert results.get(f"AdmissionControllerTest.{case}") is True, results
