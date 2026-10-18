"""The web UI's serving rules on a file list of their own (unit/web_ui_test.cc, which needs
no embedded UI): /ui/ and the app's routes get index.html, hashed assets are immutable,
missing files are 404 and never cached, and nothing outside /ui is the UI's."""

import pathlib

import pytest

from conftest import gtest_cases, run_unit_tests

CASES = gtest_cases(source=pathlib.Path(__file__).resolve().parent.parent / "unit" / "web_ui_test.cc")


@pytest.fixture(scope="module")
def results():
    return run_unit_tests(gtest_filter="WebUi.*")


def test_every_unit_case_ran(results):
    assert CASES and len(results) == len(CASES), sorted(results)


@pytest.mark.parametrize("case", CASES)
def test_unit_case(results, case):
    assert results[case], case
