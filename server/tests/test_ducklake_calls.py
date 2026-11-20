"""DuckLake functions for catalog_access tokens (GizmoSQL 1.42.3's classification, checked
against DuckDB 2.0's DuckLake) and the plan check's refusal of a call the gate did not check: the
C++ unit tests of server/unit/ducklake_calls_test.cc and ducklake_plan_check_test.cc, run here so
every job runs them. The server-side cases (DuckLake on PostgreSQL and S3) are in
test_tenant_isolation.py."""

import pytest

from conftest import run_unit_tests

CASES = ["InspectionNeedsReadAccess", "MaintenanceNeedsWriteAccess", "PathFunctionsAreTheAdministrators",
         "AnUnknownFunctionFailsClosed", "CatalogMethods"]


@pytest.fixture(scope="module")
def results():
    return run_unit_tests(gtest_filter="DuckLakeCalls.*")


def test_every_unit_case_ran(results):
    assert len(results) == len(CASES), sorted(results)


@pytest.mark.parametrize("case", sorted(CASES))
def test_unit_case(results, case):
    assert results.get(f"DuckLakeCalls.{case}") is True, results


# The plan check's refusal of a DuckLake call the gate did not check (unit/ducklake_plan_check_test.cc).
PLAN_CHECK_CASES = ["ACallTheGateDidNotCheckIsRefused", "OnlyTheCallTheGateCheckedRuns", "ACallTheGateCheckedRuns"]


@pytest.fixture(scope="module")
def plan_check_results():
    return run_unit_tests(gtest_filter="DuckLakePlanCheck.*")


def test_every_plan_check_case_ran(plan_check_results):
    assert len(plan_check_results) == len(PLAN_CHECK_CASES), sorted(plan_check_results)


@pytest.mark.parametrize("case", sorted(PLAN_CHECK_CASES))
def test_plan_check_case(plan_check_results, case):
    assert plan_check_results.get(f"DuckLakePlanCheck.{case}") is True, plan_check_results
