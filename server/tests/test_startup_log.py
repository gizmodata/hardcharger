"""What the server says when it starts: the banner (text logs only, and only when asked
for), addresses a person can open (never the bind address 0.0.0.0), and the log_emoji
setting's markers (auto: a UTF-8 terminal only; never in JSON).

Every server here binds 127.0.0.1; how a wildcard bind (0.0.0.0, ::) turns into
localhost and the host name (or the certificate's name) is the unit test's
(unit/startup_banner_test.cc, UsableUrls.*).
"""

import json
import pathlib
import re
import secrets
import subprocess
import time

import pexpect
import pytest

from conftest import BINARY, clean_environment, free_port

pytestmark = pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "banner-art.txt"
ART = FIXTURE.read_text(encoding="ascii").splitlines()
SUCCESS, WARNING, ERROR, FATAL = "✅", "⚠️", "❌", "\U0001f4a5"
EMOJI = re.compile("[✀-➿☀-⛿\U0001f300-\U0001faff]")


def version() -> str:
    out = subprocess.run(args=[str(BINARY), "--version"], capture_output=True, text=True, timeout=30)
    return out.stdout.split()[1]


def banner_lines(log: str) -> list[str]:
    """The banner's lines in a text log: from the art's first line to the line before the
    first log line after it."""
    lines = log.splitlines()
    if ART[0] not in lines:
        return []
    start = lines.index(ART[0])
    end = start
    while end < len(lines) and not re.match(r"^\d{4}-\d\d-\d\dT", lines[end]):
        end += 1
    return lines[start:end]


def startup_log(server, marker="Hardcharger is ready"):
    # Health can answer before the startup thread has finished writing its log.
    deadline = time.monotonic() + 10
    log = server.log()
    while marker not in log and time.monotonic() < deadline:
        time.sleep(0.01)
        log = server.log()
    return log


def test_the_banner_opens_a_text_log(launcher):
    server = launcher.start()
    log = startup_log(server)
    lines = banner_lines(log)
    assert lines, log
    # The art, byte for byte (pyfiglet's "slant"), then the versions, then the URL and the docs.
    assert lines[:6] == ART
    # (One line when it fits in 80 columns; a long DuckDB version moves it to a second.)
    info = " ".join(" ".join(lines[6:]).split())
    # The Warp SQL version is the specification's (third_party/warp-sql/spec/openapi.yaml).
    assert re.search(rf"Hardcharger {re.escape(version())} \| Warp SQL \d+\.\d+\.\d+ \|? ?DuckDB v\S+ \| by GizmoData", info), \
        lines
    assert any(line.startswith(f"  Warp SQL  http://127.0.0.1:{server.port}/warp") for line in lines), lines
    assert "docs.hardcharger.gizmodata.com" in info
    assert len(lines) <= 10, lines
    for line in lines:
        assert len(line) <= 80, line
        assert line.isascii(), line
    # Before the "ready" line, once.
    assert log.count(ART[1]) == 1
    assert log.index(ART[0]) < log.index("Hardcharger is ready")


@pytest.mark.web_ui
def test_with_the_web_ui_the_banner_names_its_url(launcher):
    server = launcher.start(args=["--web-ui"])
    log = startup_log(server, "the web UI is on")
    lines = banner_lines(log)
    assert any(line.startswith(f"  UI   http://127.0.0.1:{server.port}/ui/") for line in lines), lines
    assert not any("/warp" in line for line in lines), lines
    assert re.search(rf" info +the web UI is on url=http://127\.0\.0\.1:{server.port}/ui/ version=", server.log())


@pytest.mark.parametrize("off", [["--no-banner"], ["--banner=false"], ["--banner", "false"],
                                 {"HARDCHARGER_NO_BANNER": "true"}, {"HARDCHARGER_BANNER": "false"}, "yaml"])
def test_the_banner_can_be_turned_off(launcher, tmp_path, off):
    if off == "yaml":
        config = tmp_path / "hardcharger.yaml"
        config.write_text("banner: false\n")
        server = launcher.start(args=["--config", str(config)])
    elif isinstance(off, dict):
        server = launcher.start(env=off)
    else:
        server = launcher.start(args=off)
    log = startup_log(server)
    assert "Hardcharger is ready" in log
    assert ART[1] not in log and "| by GizmoData" not in log, log


def test_a_flag_beats_hardcharger_no_banner(launcher):
    server = launcher.start(args=["--banner"], env={"HARDCHARGER_NO_BANNER": "true"})
    assert banner_lines(startup_log(server))[:6] == ART


def test_hardcharger_banner_and_no_banner_together_are_refused():
    out = subprocess.run(args=[str(BINARY), "--port", str(free_port())],
                         env={**clean_environment(), "HARDCHARGER_ADMIN_PASSWORD": "pw",
                              "HARDCHARGER_BANNER": "true", "HARDCHARGER_NO_BANNER": "true"},
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 2
    assert out.stderr.startswith(
        "hardcharger-server: set HARDCHARGER_BANNER or HARDCHARGER_NO_BANNER, not both\n"), out.stderr


def test_never_in_a_json_log(launcher):
    server = launcher.start(args=["--log-format", "json", "--banner", "--log-emoji", "always"])
    log = startup_log(server)
    assert ART[1] not in log
    records = server.log_records()
    assert len(records) == len([line for line in log.splitlines() if line.strip()]), log  # every line is JSON
    ready = next(r for r in records if r["message"] == "Hardcharger is ready")
    assert ready["level"] == "info"
    assert ready["url"] == f"http://127.0.0.1:{server.port}/warp"
    assert ready["listen"] == f"127.0.0.1:{server.port}"
    assert not EMOJI.search(log), log


def test_the_ready_line_has_a_url_to_open_and_the_bind_address(launcher):
    server = launcher.start()
    line = next(line for line in startup_log(server).splitlines() if "Hardcharger is ready" in line)
    assert f" url=http://127.0.0.1:{server.port}/warp listen=127.0.0.1:{server.port} " in line, line


# ---- log_emoji ----


def ready_line(log: str) -> str:
    return next(line for line in log.splitlines() if "Hardcharger is ready" in line)


@pytest.mark.parametrize("mode", ["auto", "never"])
def test_no_emoji_off_a_terminal(launcher, mode):
    # The tests' servers log to a file: auto means none there, as under systemd, Docker or k8s.
    server = launcher.start(args=["--log-emoji", mode], env={"HARDCHARGER_SECRET_KEY": "", "LANG": "en_US.UTF-8"})
    log = server.log()
    assert not EMOJI.search(log), log
    assert re.search(r"^\S+Z info    Hardcharger is ready ", log, flags=re.M), log
    assert re.search(r"^\S+Z warning no secret_key", log, flags=re.M), log


def test_always_marks_levels_and_outcomes(launcher, tmp_path):
    server = launcher.start(args=["--log-emoji", "always"], env={"HARDCHARGER_SECRET_KEY": ""})
    log = server.log()
    assert re.search(rf"^\S+Z {SUCCESS} info    Hardcharger is ready ", log, flags=re.M), log
    assert re.search(rf"^\S+Z {WARNING} warning no secret_key", log, flags=re.M), log
    # An info line has no marker of its own: two spaces keep the columns.
    assert re.search(r"^\S+Z    info    health check passing", log, flags=re.M), log
    server.stop()
    assert re.search(rf"^\S+Z {SUCCESS} info    stopped$", server.log(), flags=re.M), server.log()
    # An error that stops the server is fatal.
    missing = tmp_path / "no-such-dir" / "x.duckdb"
    failed = launcher.start(args=["--log-emoji", "always", "--database", str(missing)], expect_up=False)
    assert failed.process.wait(timeout=60) == 1
    assert re.search(rf"^\S+Z {FATAL} error   ", failed.log(), flags=re.M), failed.log()


def test_a_fatal_error_is_redacted(launcher, tmp_path):
    # The last-resort error line (main.cc) goes through the credential redaction too: here a
    # database path that reads as a libpq key=value pair, quoted back by DuckDB's open error.
    secret = "fatal-secret-" + secrets.token_hex(nbytes=6)
    missing = tmp_path / "no-such-dir" / f"password={secret}" / "x.duckdb"
    failed = launcher.start(args=["--database", str(missing)], expect_up=False)
    assert failed.process.wait(timeout=60) == 1
    log = failed.log()
    assert "Cannot open file" in log, log  # the reason survives
    assert "password=***" in log, log
    assert secret not in log, log


def test_an_unknown_log_emoji_is_refused():
    out = subprocess.run(args=[str(BINARY), "--log-emoji", "sometimes"],
                         env={**clean_environment(), "HARDCHARGER_ADMIN_PASSWORD": "pw"},
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 2
    assert out.stderr.startswith("hardcharger-server: log_emoji must be auto, always or never, not 'sometimes'\n"), \
        out.stderr


def test_options_json_has_the_new_options():
    options = {o["name"]: o for o in json.loads(subprocess.run(args=[str(BINARY), "--print-options-json"],
                                                               capture_output=True, text=True, timeout=30)
                                                .stdout)["options"]}
    assert (options["log_emoji"]["default"], options["log_emoji"]["type"]) == ("auto", "string")
    assert (options["banner"]["default"], options["banner"]["type"]) == ("true", "boolean")
    help_text = subprocess.run(args=[str(BINARY), "--help"], capture_output=True, text=True, timeout=30).stdout
    assert "--log-emoji" in help_text and "--banner" in help_text and "--no-banner" in help_text


def spawn_at_a_terminal(args: list[str], env: dict[str, str]) -> tuple[pexpect.spawn, int]:
    port = free_port()
    child = pexpect.spawn(command=str(BINARY), args=["--host", "127.0.0.1", "--port", str(port), *args],
                          env={**clean_environment(), "HARDCHARGER_ADMIN_PASSWORD": "pw", **env},
                          encoding="utf-8", dimensions=(40, 200), timeout=60)
    return child, port


@pytest.mark.pty
@pytest.mark.parametrize("locale", [{"LANG": "en_US.UTF-8"}, {"LC_ALL": "C.UTF-8", "LANG": "C"}])
def test_auto_marks_lines_at_a_utf8_terminal(locale):
    child, port = spawn_at_a_terminal(args=[], env={"LC_ALL": "", "LC_CTYPE": "", **locale})
    try:
        child.expect(f"{SUCCESS} info    Hardcharger is ready version=")
        # The banner is there too, at a terminal.
        assert ART[1] in child.before
        child.expect(f" url=http://127.0.0.1:{port}/warp ")
    finally:
        child.kill(15)
        child.close()


@pytest.mark.pty
@pytest.mark.parametrize("args,env", [([], {"LANG": "C", "LC_ALL": "", "LC_CTYPE": ""}),
                                      (["--log-emoji", "never"], {"LANG": "en_US.UTF-8"}),
                                      (["--log-format", "json", "--log-emoji", "always"], {"LANG": "en_US.UTF-8"})])
def test_a_terminal_without_utf8_never_or_json_has_none(args, env):
    child, _ = spawn_at_a_terminal(args=args, env=env)
    try:
        child.expect("Hardcharger is ready")
        seen = child.before + child.after
        child.expect(r"\n")
        seen += child.before
        assert "Hardcharger is ready" in seen, seen
        assert not EMOJI.search(seen), seen
    finally:
        child.kill(15)
        child.close()


# ---- Fields without a value ----


def test_a_text_line_leaves_out_fields_without_a_value(launcher):
    server = launcher.start()
    log = server.log()
    passing = next(line for line in log.splitlines() if "health check passing" in line)
    assert passing.endswith("health check passing"), passing  # no "error="
    # No line of the text log has a field with nothing after its "=".
    empty = [line for line in log.splitlines() if re.search(r" [a-z_]+=(?: |$)", line)]
    assert empty == [], empty


def test_a_failing_health_check_names_its_error(launcher):
    # The twin: an error that happened is there, with its text.
    server = launcher.start(args=["--health-check-query", "SELECT no_such_column"])
    failing = None
    for _ in range(100):
        failing = next((line for line in server.log().splitlines() if "health check failing" in line), None)
        if failing:
            break
        time.sleep(0.1)
    assert failing and re.search(r" error=\S", failing), server.log()
    assert "no_such_column" in failing, failing


def test_a_json_line_of_a_passing_health_check_has_no_error(launcher):
    server = launcher.start(args=["--log-format", "json"])
    passing = next(r for r in server.log_records() if r["message"] == "health check passing")
    assert "error" not in passing, passing
