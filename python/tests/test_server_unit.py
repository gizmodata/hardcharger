"""Server without the real binary: argument checks, a server that dies at startup, and
the missing-client error."""

from __future__ import annotations

import sys
import textwrap

import pytest

from hardcharger import Server, ServerError


def test_uri_is_plain_http_without_tls():
    server = Server(port=40001, binary="/nonexistent")
    assert server.uri == "hardcharger://127.0.0.1:40001?tls=false"


def test_uri_is_tls_with_a_certificate():
    server = Server(port=40001, tls_cert="cert.pem", tls_key="key.pem", binary="/nonexistent")
    assert server.uri == "hardcharger://127.0.0.1:40001"


def test_tls_cert_needs_a_key():
    with pytest.raises(expected_exception=ValueError) as error:
        Server(tls_cert="cert.pem")
    assert "tls_cert and tls_key go together" in str(error.value)


def test_generated_passwords_are_url_and_shell_safe():
    for _ in range(50):
        password = Server(binary="/nonexistent").password
        assert len(password) >= 32
        assert not set(password) & set(":@/;?#%&=+ \t'\"\\$`")


def test_ports_are_free_and_distinct():
    server = Server(binary="/nonexistent")
    assert server.port != server.health_port


def test_missing_binary_is_refused(tmp_path):
    with pytest.raises(expected_exception=ServerError) as error:
        Server(binary=tmp_path / "missing").start()
    assert "doesn't exist" in str(error.value)


def test_a_server_that_exits_at_startup_reports_its_output(tmp_path):
    binary = tmp_path / "hardcharger-server"
    binary.write_text("#!/bin/sh\necho 'error: the port is taken (simulated)' >&2\nexit 7\n")
    binary.chmod(mode=0o755)
    server = Server(binary=binary)
    with pytest.raises(expected_exception=ServerError) as error:
        server.start()
    assert "exited while starting (exit code 7)" in str(error.value)
    assert "error: the port is taken (simulated)" in str(error.value)
    assert not server.is_running()


def test_a_server_that_answers_404_has_started(tmp_path):
    # A server released before the protocol's rename (a pinned older version) answers 404 at
    # the root the readiness probe asks for; any HTTP answer means it is serving.
    binary = tmp_path / "hardcharger-server"
    binary.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            """
            import http.server
            import sys

            port = int(sys.argv[sys.argv.index("--port") + 1])

            class NotFound(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_error(code=404)

                def log_message(self, *args):
                    pass

            server = http.server.HTTPServer(
                server_address=("127.0.0.1", port), RequestHandlerClass=NotFound
            )
            server.serve_forever()
            """
        )
    )
    binary.chmod(mode=0o755)
    server = Server(binary=binary, host="127.0.0.1", startup_timeout=10.0)
    server.start()
    try:
        assert server.is_running()
    finally:
        server.stop()


def test_connect_without_the_client_says_how_to_install_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "adbc_driver_hardcharger", None)
    with pytest.raises(expected_exception=ImportError) as error:
        Server(binary="/nonexistent").connect()
    assert 'pip install "hardcharger[adbc]"' in str(error.value)


def test_a_long_startup_failure_keeps_the_first_and_last_lines(tmp_path):
    binary = tmp_path / "hardcharger-server"
    binary.write_text(
        "#!/bin/sh\necho 'error: unknown option --bogus'\n"
        'i=0; while [ $i -lt 200 ]; do echo "usage line $i"; i=$((i+1)); done\nexit 2\n'
    )
    binary.chmod(mode=0o755)
    with pytest.raises(expected_exception=ServerError) as error:
        Server(binary=binary).start()
    message = str(error.value)
    assert "error: unknown option --bogus" in message
    assert "usage line 199" in message
    assert "(161 lines)" in message
    assert "usage line 100" not in message
