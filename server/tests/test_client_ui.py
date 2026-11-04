"""`hardcharger ui`: the web UI on its own (client/ui_command.cc), with no server behind it.
It serves the embedded UI and the relay (src/web_relay.cc); the UI's contract says there is
no local server, and a relay session starts pending (registering a remote), then signs in
through a sign-in to that remote. Every refusal asserts its text and exit code.

The command needs a client build that embeds the UI; against one without it the command
says so and these fail, never skip (deselect them with -m "not web_ui").
"""

import os
import re
import socket
import subprocess
import sys
import time

import httpx
import pytest

from conftest import CLIENT_BINARY, TIMEOUT_SCALE, clean_environment, free_port
from test_web_ui_remotes import Relay, problem

pytestmark = pytest.mark.web_ui

SEED = "CREATE TABLE t AS SELECT range AS id FROM range(5)"


def client_binary() -> str:
    if not CLIENT_BINARY.exists():
        pytest.fail(reason=f"{CLIENT_BINARY} is missing: build the client first")
    return str(CLIENT_BINARY)


class StandaloneUi:
    def __init__(self, process: subprocess.Popen, url: str):
        self.process = process
        self.url = url
        self.base_url = url.removesuffix("/ui/")

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()

    # The relay helpers (test_web_ui_remotes.Relay) read these from a server.
    def token(self) -> None:
        return None


def start_ui(tmp_path, *args: str, timeout: float = 30) -> StandaloneUi:
    """Starts `hardcharger ui --no-open --quiet ...` and reads the URL it prints."""
    port = free_port()
    log = (tmp_path / f"ui-{port}.log").open("w")
    process = subprocess.Popen(args=[client_binary(), "ui", "--no-open", "--quiet", "--listen", f"127.0.0.1:{port}", *args],
                               env=clean_environment(), stdout=subprocess.PIPE, stderr=log, text=True, cwd=tmp_path)
    deadline = time.monotonic() + timeout * TIMEOUT_SCALE
    line = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(reason=f"hardcharger ui exited {process.returncode}: {(tmp_path / f'ui-{port}.log').read_text()}")
        line = process.stdout.readline().strip()
        if line:
            break
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+/ui/", line), line
    return StandaloneUi(process=process, url=line)


@pytest.fixture
def ui(tmp_path):
    running = start_ui(tmp_path)
    yield running
    running.stop()


@pytest.fixture(scope="module")
def remote(module_launcher):
    return module_launcher.start(args=["--init-sql-commands", SEED])


def test_it_serves_the_ui_and_says_there_is_no_local_server(ui):
    page = httpx.get(url=ui.url, timeout=10)
    assert page.status_code == 200 and '<div id="root"></div>' in page.text
    doc = httpx.get(url=f"{ui.base_url}/ui/v1/app", timeout=10).json()
    assert doc["local_server"] is False and doc["remote_servers"] is True
    # Discovery answers (the UI reads it), with nothing to sign in to.
    discovery = httpx.get(url=f"{ui.base_url}/chargewire", timeout=10).json()
    assert discovery["server"]["name"] == "hardcharger-ui" and discovery["auth"]["grant_types"] == []
    # And its SQL endpoints run nothing.
    token = httpx.post(url=f"{ui.base_url}/chargewire/v1/auth/token", timeout=10,
                       json={"grant_type": "password", "username": "x", "password": "y"})
    assert token.status_code in (400, 401, 501)


def test_a_relay_session_starts_pending_and_signs_in_through_a_remote(ui, remote):
    relay = Relay(ui)
    # Nothing to sign in to here, so registering a remote starts the (pending) session.
    problem(relay.remotes(), 401, "relay_session_required")
    added = relay.add(url=remote.base_url, name="first").json()
    assert added["signed_in"] is False and added["relay_session"].startswith("p-")
    assert relay.relay_session == added["relay_session"]
    listed = relay.remotes().json()
    assert listed["signed_in"] is False and [r["name"] for r in listed["remotes"]] == ["first"]
    # Pending: the remote's discovery and sign-in, nothing else.
    assert relay.http.get(url=added["relay_path"], headers=relay.headers()).json()["protocol"] == "chargewire"
    problem(relay.client(added["id"], "whatever").open_session(), 401, "relay_session_required")
    problem(relay.http.post(url=f"{added['relay_path']}/v1/auth/token", headers=relay.headers(),
                            json={"grant_type": "password", "username": remote.username, "password": "wrong"}),
            401, "invalid_credentials")
    assert relay.remotes().json()["signed_in"] is False
    # A sign-in that succeeds makes the session a signed-in one.
    token = relay.sign_in(added["id"], remote.username, remote.password)
    assert relay.remotes().json()["signed_in"] is True
    client = relay.client(added["id"], token)
    session = client.new_session()
    assert "memory" in session.metadata("catalogs").column("catalog_name").to_pylist()
    assert session.query("SELECT count(*) AS n FROM t").to_pylist() == [{"n": 5}]
    # A second remote in the same (now signed-in) session.
    second = relay.add(url=remote.base_url, name="second").json()
    assert second["signed_in"] is True and second["relay_session"] == relay.relay_session


def test_the_listen_rules(tmp_path, remote):
    env = clean_environment()

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(args=[client_binary(), "ui", "--no-open", *args], env=env, capture_output=True,
                              text=True, timeout=60, cwd=tmp_path)

    # Plain HTTP on an address other computers can reach is refused without --allow-plaintext.
    refused = run("--listen", "0.0.0.0:0")
    assert refused.returncode == 1 and "refusing to serve the UI in plain HTTP on 0.0.0.0" in refused.stderr
    assert "--allow-plaintext" in refused.stderr and "--tls-cert" in refused.stderr
    for bad, text in [("127.0.0.1", "no port"), ("127.0.0.1:x", "is not a port"), ("[::1", "[address]:port")]:
        result = run("--listen", bad)
        assert result.returncode == 1 and text in result.stderr, (bad, result.stderr)
    only_cert = run("--tls-cert", "x.pem")
    assert only_cert.returncode == 1 and "--tls-cert and --tls-key go together" in only_cert.stderr
    missing = run("--tls-cert", "x.pem", "--tls-key", "y.pem")
    assert missing.returncode == 1 and "cannot read x.pem" in missing.stderr
    extra = run("extra")
    assert extra.returncode == 1 and "takes no positional argument" in extra.stderr
    unknown = run("--bogus")
    assert unknown.returncode == 1 and "hardcharger ui --help" in unknown.stderr
    help_text = run("--help")
    assert help_text.returncode == 0 and "Usage: hardcharger ui [OPTIONS]" in help_text.stdout
    assert "--allow-plaintext" in help_text.stdout and "--tls-cert" in help_text.stdout
    # The positive twin: --allow-plaintext lets it serve on every address.
    port = free_port()
    allowed = subprocess.Popen(args=[client_binary(), "ui", "--no-open", "--quiet", "--allow-plaintext",
                                     "--listen", f"0.0.0.0:{port}"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, cwd=tmp_path)
    try:
        line = allowed.stdout.readline().strip()
        assert line == f"http://localhost:{port}/ui/", (line, allowed.stderr.read() if allowed.poll() is not None else "")
        assert httpx.get(url=f"http://127.0.0.1:{port}/ui/v1/app", timeout=10).json()["local_server"] is False
    finally:
        allowed.terminate()
        allowed.wait(timeout=20)


def test_a_port_in_use_is_an_error(tmp_path):
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    port = holder.getsockname()[1]
    try:
        result = subprocess.run(args=[client_binary(), "ui", "--no-open", "--listen", f"127.0.0.1:{port}"],
                                env=clean_environment(), capture_output=True, text=True, timeout=60, cwd=tmp_path)
    finally:
        holder.close()
    assert result.returncode == 1 and f"cannot listen on 127.0.0.1:{port}" in result.stderr


def test_the_sql_client_is_unchanged_beside_the_subcommand(remote):
    """The subcommand is `ui` alone: the client's flags, URI and help work as before."""
    env = {**clean_environment(), "HARDCHARGER_PASSWORD": remote.password}
    args = ["--host", "127.0.0.1", "--port", str(remote.port), "--tls=false", "--username", remote.username]
    rows = subprocess.run(args=[client_binary(), *args, "--quiet", "--csv", "-c", "SELECT count(*) AS n FROM t"],
                          env=env, capture_output=True, text=True, timeout=60)
    assert rows.returncode == 0 and rows.stdout.strip().splitlines() == ["n", "5"], rows.stderr
    # The subcommand's word after other arguments is the positional URI, as any word is.
    as_uri = subprocess.run(args=[client_binary(), "ui", "--help"], env=env, capture_output=True, text=True, timeout=60)
    assert as_uri.returncode == 0 and "Usage: hardcharger ui" in as_uri.stdout
    help_text = subprocess.run(args=[client_binary(), "--help"], env=env, capture_output=True, text=True, timeout=60)
    assert help_text.returncode == 0 and "hardcharger ui [OPTIONS]" in help_text.stdout
    version = subprocess.run(args=[client_binary(), "--version"], env=env, capture_output=True, text=True, timeout=60)
    assert version.returncode == 0 and version.stdout.startswith("Hardcharger Client ")


def test_a_build_without_the_ui_says_so(tmp_path):
    """A client built without -DHARDCHARGER_WEB_UI refuses `ui` with a clear message and exit
    code 2. This build embeds the UI, so here the message is checked where it lives: the
    binary carries it, and the branch is the first thing the command checks (ui_command.cc)."""
    with open(client_binary(), "rb") as f:
        binary = f.read()
    assert b"this hardcharger build has no web UI embedded" in binary
    assert b"-DHARDCHARGER_WEB_UI=<hardcharger-ui tarball>" in binary
