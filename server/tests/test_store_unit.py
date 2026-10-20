"""The access-control store's and the engine's C++ unit tests (server/unit/store_test.cc,
redaction_test.cc, extension_hint_test.cc): the bounds added to a PostgreSQL store's
connection string, credential redaction, the cluster a DuckDB store belongs to, and the
hint a LOAD gets when an extension needs another one that isn't installed."""

from conftest import run_unit_tests


def test_store_unit_tests_pass():
    results = run_unit_tests(gtest_filter="StoreConnection.*:StoreRedaction.*:StoreOwnership.*:ExtensionHint.*:"
                                          "Redaction.SharedHelperMasksEveryConnectionStringForm")
    assert len(results) == 11, results
    assert all(results.values()), results
