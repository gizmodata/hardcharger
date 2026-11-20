"""DuckLake functions for catalog_access tokens (GizmoSQL 1.42.3's classification, checked
against DuckDB 2.0's DuckLake): the C++ unit tests of server/unit/ducklake_calls_test.cc, run
here so every job runs them. The server-side cases (DuckLake on PostgreSQL and S3) are in
test_tenant_isolation.py."""

import pytest

from conftest import run_unit_tests

CASES = ["InspectionNeedsReadAccess", "MaintenanceNeedsWriteAccess", "PathFunctionsAreTheAdministrators",
         "AnUnknownFunctionFailsClosed", "CatalogMethods", "TokenCount", "LooksLikeACall"]


@pytest.fixture(scope="module")
def results():
    return run_unit_tests(gtest_filter="DuckLakeCalls.*")


def test_every_unit_case_ran(results):
    assert len(results) == len(CASES), sorted(results)


@pytest.mark.parametrize("case", sorted(CASES))
def test_unit_case(results, case):
    assert results.get(f"DuckLakeCalls.{case}") is True, results
