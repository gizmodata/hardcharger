"""Port of GizmoSQL 1.x tests/integration/test_server_cli.cpp (2 cases).

`--print-duckdb-version` prints exactly the linked DuckDB version (GizmoData Cloud's pod
wrapper calls it, so the flag keeps its name) and `--version` prints the server banner,
here "Hardcharger <version>" rather than 1.x's "GizmoSQL Server CLI".
"""

import subprocess

import duckdb_version_probe
from conftest import BINARY


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args=[str(BINARY), *args], capture_output=True, text=True, timeout=30,
                          env={"PATH": "/usr/bin:/bin"})


def test_print_duckdb_version_matches_linked_library():
    out = run("--print-duckdb-version")
    assert out.returncode == 0, out.stderr
    printed = out.stdout.strip()
    assert printed == duckdb_version_probe.linked_version(), printed
    assert any(c.isdigit() for c in printed), printed


def test_version_flag_prints_server_version():
    out = run("--version")
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("Hardcharger "), out.stdout
    version = out.stdout.split()[1]
    assert version.count(".") == 2 and version.split(".")[0].isdigit(), out.stdout
