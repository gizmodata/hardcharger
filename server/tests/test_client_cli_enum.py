"""The CLI prints an ENUM as its value, in every output mode and nested (found by the
access-control track while writing docs examples for 2.7.0).

An ENUM arrives as an Arrow dictionary column, and the CLI printed Arrow's own text for a
dictionary scalar, the dictionary and the index (`["low","high"][1]`), and Arrow's type
string in the headers. DuckDB's CLI prints the value (`high`), `[low, NULL]` for a list of
them, `{'m': low}` for a struct, and names the type ENUM('low', 'high').
"""

import csv
import io
import json

import pytest

from test_client_cli_geometry import MODES, run_mode, text_of

COLUMNS = "m, l, st, n"
EXPECTED = {"m": "high", "l": "[low, NULL]", "st": "{'m': low}"}


@pytest.fixture(scope="module")
def server(module_launcher):
    init = ("CREATE TYPE mood AS ENUM ('low', 'high'); "
            "CREATE TABLE moods AS SELECT 'high'::mood AS m, ['low'::mood, NULL] AS l, {'m': 'low'::mood} AS st, "
            "NULL::mood AS n;")
    return module_launcher.start(args=["--init-sql-commands", init])


def test_json_mode_prints_the_values(server):
    (row,) = json.loads(text_of(run_mode(server, "json", f"SELECT {COLUMNS} FROM moods")))
    assert row == {**EXPECTED, "n": None}, row
    lines = text_of(run_mode(server, "jsonlines", f"SELECT {COLUMNS} FROM moods")).splitlines()
    assert [json.loads(line) for line in lines if line.strip()] == [{**EXPECTED, "n": None}]


def test_csv_mode_prints_the_values(server):
    (row,) = csv.DictReader(io.StringIO(text_of(run_mode(server, "csv", f"SELECT {COLUMNS} FROM moods"))))
    assert {k: row[k] for k in EXPECTED} == EXPECTED, row


@pytest.mark.parametrize("mode", MODES)
def test_every_mode_prints_the_values_not_the_dictionary(server, mode):
    out = text_of(run_mode(server, mode, f"SELECT {COLUMNS} FROM moods"), mode=mode)
    for text in EXPECTED.values():
        if mode in ("insert", "quote"):
            text = text.replace("'", "''")  # values as SQL literals
        assert text in out, f"{mode}: {text} missing from\n{out}"
    assert "dictionary<" not in out and '["low"' not in out and "][1]" not in out, f"{mode}:\n{out}"


def test_headers_name_the_enum_type_as_duckdb_does(server):
    for mode in ("table", "box"):
        out = text_of(run_mode(server, mode, f"SELECT {COLUMNS} FROM moods"), mode=mode)
        for name in ("ENUM('low', 'high')", "ENUM('low', 'high')[]", "struct(m ENUM('low', 'high'))"):
            assert name in out, f"{mode}: {name} missing from\n{out}"
        assert "dictionary" not in out, out
