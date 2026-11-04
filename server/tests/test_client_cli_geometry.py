"""The CLI prints GEOMETRY as WKT and BLOB as DuckDB's \\xHH text, in every output mode (issue #22).

2.5.0 wrote a GEOMETRY column's WKB bytes (and a BLOB's bytes) straight to stdout: control
characters in the table, raw bytes in the CSV, and JSON that was neither valid JSON nor valid
UTF-8. Every geometry type and dimension of the web UI's fixture (ui/test/unit/fixtures/
make-geometry.mjs) goes through a real server with the spatial extension; the expected text
is DuckDB's own ST_AsText of each, so the CLI, the UI and DuckDB print the same WKT. Every
mode's output must be valid UTF-8 with no control characters, and the JSON modes valid JSON.
Needs the network once to INSTALL spatial; without it these fail, they don't skip.
"""

import csv
import io
import json
import re
import subprocess

import pytest

from test_client_cli import binary, client_env, conn_args, strip_ansi

CASES = [
    "POINT (1 2)",
    "POINT (-80.8431 35.2271)",
    "POINT (1e20 -1e-7)",
    "POINT (1e15 1e16)",
    "POINT (123456789012345678 0.0001)",
    "POINT (0.00001 -0.5)",
    "POINT (-0 123456789.123456789)",
    "POINT (0.1 0.30000000000000004)",
    "POINT (1.5e300 -2.5e-300)",
    "POINT (179.99999999999997 -89.123456789012345)",
    "POINT EMPTY",
    "POINT Z (1 2 3)",
    "POINT M (1 2 3)",
    "POINT ZM (1 2 3 4)",
    "POINT Z EMPTY",
    "LINESTRING (0 0, 1 1, 2 0.5)",
    "LINESTRING Z (0 0 1, 1 1 2)",
    "LINESTRING EMPTY",
    "POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0), (2 2, 2 4, 4 4, 4 2, 2 2))",
    "POLYGON ZM ((0 0 1 2, 1 0 1 2, 1 1 1 2, 0 0 1 2))",
    "POLYGON EMPTY",
    "MULTIPOINT ((1 2), (3 4))",
    "MULTIPOINT Z ((1 2 3), (4 5 6))",
    "MULTIPOINT EMPTY",
    "MULTILINESTRING ((0 0, 1 1), (2 2, 3 3, 4 4))",
    "MULTILINESTRING (EMPTY, (0 0, 1 1))",
    "MULTILINESTRING EMPTY",
    "MULTIPOLYGON (((0 0, 1 0, 1 1, 0 0)), ((5 5, 6 5, 6 6, 5 5), (5.2 5.1, 5.8 5.1, 5.8 5.7, 5.2 5.1)))",
    "MULTIPOLYGON (EMPTY, ((0 0, 1 0, 1 1, 0 0)))",
    "MULTIPOLYGON EMPTY",
    "GEOMETRYCOLLECTION (POINT (1 2), LINESTRING (0 0, 1 1), POLYGON ((0 0, 1 0, 1 1, 0 0)))",
    "GEOMETRYCOLLECTION (POINT EMPTY, GEOMETRYCOLLECTION (MULTIPOINT ((1 1)), LINESTRING EMPTY))",
    "GEOMETRYCOLLECTION M (POINT M (1 2 3))",
    "GEOMETRYCOLLECTION EMPTY",
]
# A BLOB with a byte that isn't UTF-8 (0xF5), a NUL, and the characters DuckDB escapes.
BLOB_SQL = r"'\xF5\x00ab\x5Cc\x27d\x22e'::BLOB"  # DuckDB's blob literal takes \xHH escapes only
BLOB_TEXT = r"\xF5\x00ab\x5Cc\x27d\x22e"
COLUMNS = ("i, g, g::GEOMETRY('EPSG:4326') AS g4326, [g, NULL] AS list, {'g': g} AS st, "
           f"NULL::GEOMETRY AS n, {BLOB_SQL} AS b")
MODES = ["table", "box", "csv", "tabs", "json", "jsonlines", "markdown", "line", "list", "html", "latex",
         "insert", "quote", "ascii"]


@pytest.fixture(scope="module")
def server(module_launcher):
    values = ", ".join(f"({i}, '{case}')" for i, case in enumerate(CASES))
    init = ("INSTALL spatial; LOAD spatial; "
            f"CREATE TABLE geo_cases AS SELECT i, input, ST_GeomFromText(input) AS g FROM (VALUES {values}) t(i, input);")
    return module_launcher.start(args=["--init-sql-commands", init], startup_seconds=60)


@pytest.fixture(scope="module")
def expected(server) -> list[str]:
    """DuckDB's own text for each case, in order."""
    rows = server.admin().rows(sql="SELECT ST_AsText(g) AS text FROM geo_cases ORDER BY i")
    assert len(rows) == len(CASES)
    return [row["text"] for row in rows]


def run_mode(server, mode: str, sql: str, *flags: str) -> bytes:
    """The CLI's stdout, as bytes, for `sql` in output mode `mode` (set by .mode on stdin: every
    mode has a .mode name; only some have a flag)."""
    script = f".mode {mode}\n{sql};\n"
    result = subprocess.run(args=[binary(), *conn_args(server), "--quiet", *flags], input=script.encode(),
                            env=client_env(server=server), capture_output=True, timeout=120)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout


def text_of(stdout: bytes, mode: str = "") -> str:
    """Decoded strictly: every mode's output is valid UTF-8, with no control characters but
    newlines, tabs and carriage returns (the box and table modes may colour with ANSI escapes;
    the ascii mode separates columns and rows with 0x1F and 0x1E by design, as SQLite's does)."""
    text = strip_ansi(stdout.decode(encoding="utf-8"))
    allowed = "\n\r\t" + ("\x1e\x1f" if mode == "ascii" else "")
    control = [c for c in text if ord(c) < 32 and c not in allowed]
    assert not control, f"control characters in the output: {control[:5]!r}"
    # LaTeX writes a backslash as \textbackslash{} and braces as \{ \}: read them back.
    if mode == "latex":
        text = text.replace("\\textbackslash{}", "\\").replace("\\{", "{").replace("\\}", "}")
    return text


def test_json_mode_prints_wkt_and_escaped_blobs_as_valid_json(server, expected):
    rows = json.loads(text_of(run_mode(server, "json", f"SELECT {COLUMNS} FROM geo_cases ORDER BY i")))
    assert len(rows) == len(CASES)
    for row, text in zip(rows, expected):
        assert row["g"] == text, row
        assert row["g4326"] == text, row
        assert row["list"] == f"[{text}, NULL]", row
        assert row["st"] == f"{{'g': {text}}}", row
        assert row["n"] is None, row
        assert row["b"] == BLOB_TEXT, row


def test_jsonlines_mode_prints_one_valid_document_per_row(server, expected):
    lines = text_of(run_mode(server, "jsonlines", f"SELECT {COLUMNS} FROM geo_cases ORDER BY i")).splitlines()
    rows = [json.loads(line) for line in lines if line.strip()]
    assert [row["g"] for row in rows] == expected
    assert all(row["b"] == BLOB_TEXT for row in rows)


def test_csv_mode_prints_wkt_and_escaped_blobs(server, expected):
    out = text_of(run_mode(server, "csv", f"SELECT {COLUMNS} FROM geo_cases ORDER BY i"))
    rows = list(csv.DictReader(io.StringIO(out)))
    assert [row["g"] for row in rows] == expected
    assert [row["g4326"] for row in rows] == expected
    assert all(row["b"] == BLOB_TEXT for row in rows)
    assert all(row["list"] == f"[{text}, NULL]" for row, text in zip(rows, expected))


@pytest.mark.parametrize("mode", MODES)
def test_every_mode_prints_wkt_and_blob_text_as_valid_utf8(server, expected, mode):
    """One column at a time, so no mode wraps or truncates a wide row: each case's WKT, the
    CRS-tagged twin's, and the BLOB's text appear verbatim, and nothing else of the bytes does."""
    out = text_of(run_mode(server, mode, f"SELECT g FROM geo_cases ORDER BY i"), mode=mode)
    for text in expected:
        assert text in out, f"{mode}: {text} missing from\n{out}"
    out = text_of(run_mode(server, mode, f"SELECT g4326 FROM (SELECT {COLUMNS} FROM geo_cases) ORDER BY i"), mode=mode)
    for text in expected:
        assert text in out, f"{mode}: {text} missing from\n{out}"
    out = text_of(run_mode(server, mode, f"SELECT {BLOB_SQL} AS b"), mode=mode)
    blob = json.dumps(BLOB_TEXT) if mode in ("json", "jsonlines") else BLOB_TEXT  # JSON escapes the backslashes
    assert blob in out, f"{mode}:\n{out}"


def test_table_mode_names_the_types(server):
    out = text_of(run_mode(server, "table", f"SELECT {COLUMNS} FROM geo_cases WHERE i = 0"))
    assert "GEOMETRY('EPSG:4326')" in out, out
    assert re.search(r"\bGEOMETRY\b", out), out
    assert "blob" in out, out
    assert "POINT (1 2)" in out and BLOB_TEXT in out, out


def test_describe_names_geometry_columns(server):
    out = text_of(run_mode(server, "table", ".describe geo_cases"))
    assert re.search(r"\bg\b.*GEOMETRY", out), out
