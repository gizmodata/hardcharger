"""Port of GizmoSQL 1.x tests/integration/test_server_cli.cpp (2 cases).

`--print-duckdb-version` prints exactly the linked DuckDB version (GizmoData Cloud's pod
wrapper calls it, so the flag keeps its name) and `--version` prints the server banner,
here "Hardcharger <version>" rather than 1.x's "GizmoSQL Server CLI".
"""

import pathlib
import subprocess

import duckdb_version_probe
from conftest import BINARY

NOTICES = pathlib.Path(__file__).resolve().parents[2] / "THIRD_PARTY_NOTICES.md"


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


def test_print_options_json_lists_every_option_of_help():
    """The documentation's configuration reference is generated from this output."""
    import json
    import re

    out = run("--print-options-json")
    assert out.returncode == 0, out.stderr
    document = json.loads(out.stdout)
    options = document["options"]
    assert document["config_file"] == {"flag": "--config", "env": "HARDCHARGER_CONFIG"}
    assert document["precedence"] == ["flag", "env", "file", "default"]

    help_text = run("--help").stdout
    help_flags = re.findall(r"^  (--[a-z0-9-]+)\n      .*\[(HARDCHARGER_[A-Z0-9_]+)\]$", help_text, flags=re.M)
    assert [(o["flag"], o["env"]) for o in options] == help_flags

    for option in options:
        assert option["group"], option
        assert option["description"], option
        assert option["type"] in {"string", "integer", "boolean", "list"}, option
        assert option["yaml_key"] == option["name"], option
        assert option["flag"] == "--" + option["name"].replace("_", "-"), option
        assert option["env"] == "HARDCHARGER_" + option["name"].upper(), option
    by_name = {o["name"]: o for o in options}
    assert by_name["port"]["default"] == "31337"
    assert by_name["read_only"]["default"] == "false"
    assert by_name["token_authorized_emails"]["default"] == "*"
    assert by_name["admin_username"]["default"] == "hardcharger"
    assert by_name["admin_password"]["default"] == ""


def test_licenses_flag_prints_the_third_party_notices():
    """--licenses prints THIRD_PARTY_NOTICES.md as this build embedded it, then the embedded
    web UI's own notices (when the build embeds the UI, as every CI and release build does)."""
    out = subprocess.run(args=[str(BINARY), "--licenses"], capture_output=True, timeout=30,
                         env={"PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr
    printed = out.stdout.replace(b"\r\n", b"\n").decode("utf-8")
    notices = NOTICES.read_text(encoding="utf-8")
    assert printed.startswith(notices), printed[:300]
    assert printed.startswith("# Third-party software in Hardcharger\n")
    for component in ("### DuckDB\n", "### Apache Arrow C++\n", "### OpenSSL\n", "### Boost "):
        assert component in notices, component
    rest = printed[len(notices):]
    assert rest == "" or rest.startswith("\n\nHardcharger web UI "), rest[:300]
    if rest:
        assert "\nreact " in rest and "Permission is hereby granted" in rest, rest[:300]
