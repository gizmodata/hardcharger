"""One version string everywhere the server reports it: `--version`, HARDCHARGER_VERSION(),
hardcharger_settings(), the hardcharger_build_info metric, the instrumentation's instances
table, the client's --version, and (followed by the DuckDB version) the /chargewire discovery
document and server-info. It is the plain semantic version, as the CHANGELOG and the
release tags' numbers have it ("0.2.0", never "v0.2.0"), and the full one server/CMakeLists.txt
holds: a release candidate's suffix included ("2.0.0-rc.1").

Also the metrics' help text: Hardcharger has no editions, and no help line says otherwise.
"""

import pathlib
import re
import subprocess

import httpx

from conftest import BINARY, CLIENT_BINARY, free_port

SEMVER = re.compile(r"\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?")


def server_version() -> str:
    out = subprocess.run(args=[str(BINARY), "--version"], capture_output=True, text=True, check=True).stdout
    match = re.fullmatch(r"Hardcharger (\S+)\n", out)
    assert match, out
    assert SEMVER.fullmatch(match.group(1)), out
    return match.group(1)


def source_version() -> str:
    """`project(hardcharger VERSION X.Y.Z ...)` and `set(HARDCHARGER_VERSION_SUFFIX "...")`."""
    cmake = (pathlib.Path(__file__).resolve().parents[1] / "CMakeLists.txt").read_text(encoding="utf-8")
    numbers = re.search(pattern=r"^project\(hardcharger VERSION ([0-9.]+)", string=cmake, flags=re.MULTILINE)
    suffix = re.search(pattern=r'^set\(HARDCHARGER_VERSION_SUFFIX "([^"]*)"\)', string=cmake, flags=re.MULTILINE)
    assert numbers and suffix, "no version in server/CMakeLists.txt"
    return numbers.group(1) + suffix.group(1)


def test_every_reported_version_is_the_same_semantic_version(launcher):
    version = server_version()
    assert version == source_version()
    metrics_port = free_port()
    server = launcher.start(args=["--enable-metrics", "--metrics-port", str(metrics_port),
                                  "--metrics-bind-address", "127.0.0.1", "--enable-instrumentation"])
    admin = server.admin()
    assert admin.scalar(sql="SELECT HARDCHARGER_VERSION()") == version
    assert admin.scalar(sql="SELECT value FROM hardcharger_settings() WHERE name = 'hardcharger.version'") == version
    assert admin.scalar(sql="SELECT labels['version'] FROM hardcharger_metrics() "
                            "WHERE name = 'hardcharger_build_info'") == version
    scraped = httpx.get(url=f"http://127.0.0.1:{metrics_port}/metrics").text
    assert f'version="{version}"' in scraped, scraped[:800]
    assert admin.scalar(sql="SELECT hardcharger_version FROM _hardcharger_instr.instances "
                            "WHERE instance_id = HARDCHARGER_CURRENT_INSTANCE()::UUID") == version
    # Discovery and server-info carry the DuckDB version after it.
    discovered = httpx.get(url=f"{server.base_url}/chargewire").json()["server"]["version"]
    assert discovered.startswith(f"{version} (DuckDB "), discovered
    info = admin.metadata(kind="server-info")
    assert info["server_version"].startswith(f"{version} (DuckDB "), info
    client = subprocess.run(args=[str(CLIENT_BINARY), "--version"], capture_output=True, text=True, check=True)
    assert client.stdout == f"Hardcharger Client {version}\n", client.stdout


def test_no_metric_help_mentions_an_edition(launcher):
    metrics_port = free_port()
    launcher.start(args=["--enable-metrics", "--metrics-port", str(metrics_port), "--metrics-bind-address", "127.0.0.1"])
    scraped = httpx.get(url=f"http://127.0.0.1:{metrics_port}/metrics").text
    help_lines = [line for line in scraped.splitlines() if line.startswith("# HELP ")]
    # The positive twin: the help lines are there, build_info's among them.
    assert any(line.startswith("# HELP hardcharger_build_info ") for line in help_lines), help_lines
    for line in help_lines:
        assert not re.search(r"edition|licen[cs]e|enterprise", line, flags=re.IGNORECASE), line
