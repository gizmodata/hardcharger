"""Server against the real hardcharger-server (HARDCHARGER_PY_TEST_SERVER_BINARY).

Sign-in is checked over plain Warp SQL HTTP, so these need no client; the ``client``
tests go through Server.connect() and adbc-driver-hardcharger.
"""

from __future__ import annotations

import datetime
import ipaddress
import json
import subprocess
import urllib.error
import urllib.request

import pytest

from hardcharger import Server, ServerError

pytestmark = pytest.mark.integration


def sign_in(server: Server, password: str, context=None) -> tuple[int, dict]:
    scheme = "https" if server.tls else "http"
    request = urllib.request.Request(
        url=f"{scheme}://127.0.0.1:{server.port}/warp/v1/auth/token",
        method="POST",
        data=json.dumps(
            obj={"grant_type": "password", "username": server.username, "password": password}
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10, context=context) as response:
            return response.status, json.load(fp=response)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(s=error.read() or b"null")


def test_server_starts_and_admin_signs_in(real_binary):
    with Server(binary=real_binary) as server:
        assert server.is_running()
        status, body = sign_in(server=server, password=server.password)
        assert status == 200, body
        assert body["access_token"]
        with urllib.request.urlopen(f"http://127.0.0.1:{server.health_port}/health") as health:
            assert health.status == 200
    assert not server.is_running()
    assert server.log_path is None  # the temporary log went with it


def test_wrong_password_is_refused(real_binary):
    with Server(binary=real_binary) as server:
        status, body = sign_in(server=server, password=server.password + "x")
    assert status == 401, body
    assert body["code"] == "invalid_credentials", body


def test_credentials_are_not_on_the_command_line(real_binary):
    with Server(binary=real_binary, password="not-in-ps-0123456789") as server:
        args = subprocess.run(
            args=["ps", "-o", "args=", "-p", str(server.pid)], capture_output=True, text=True
        ).stdout
        assert "hardcharger-server" in args
        assert "not-in-ps-0123456789" not in args
        assert sign_in(server=server, password="not-in-ps-0123456789")[0] == 200


def test_a_custom_username_and_database_file(real_binary, tmp_path):
    database = tmp_path / "my.duckdb"
    with Server(
        binary=real_binary, username="joe", password="joe-password", database=database
    ) as s:
        assert sign_in(server=s, password="joe-password")[0] == 200
    assert database.exists()


def test_a_bad_option_fails_with_the_server_message(real_binary):
    server = Server(binary=real_binary, extra_args=["--no-such-option"])
    with pytest.raises(expected_exception=ServerError) as error:
        server.start()
    assert "exited while starting" in str(error.value)
    assert "no-such-option" in str(error.value)


def test_a_log_file_is_kept(real_binary, tmp_path):
    log = tmp_path / "server.log"
    with Server(binary=real_binary, log_file=log):
        pass
    assert log.stat().st_size > 0


@pytest.fixture
def certificates(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(curve=ec.SECP256R1())
    name = x509.Name(attributes=[x509.NameAttribute(oid=NameOID.COMMON_NAME, value="127.0.0.1")])
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name=name)
        .issuer_name(name=name)
        .public_key(key=key.public_key())
        .serial_number(number=x509.random_serial_number())
        .not_valid_before(time=now - datetime.timedelta(minutes=5))
        .not_valid_after(time=now + datetime.timedelta(days=1))
        .add_extension(
            extval=x509.SubjectAlternativeName(
                general_names=[x509.IPAddress(value=ipaddress.ip_address(address="127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(extval=x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(private_key=key, algorithm=hashes.SHA256())
    )
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_bytes(data=cert.public_bytes(encoding=serialization.Encoding.PEM))
    key_file.write_bytes(
        data=key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_file, key_file


def test_tls_server(real_binary, certificates):
    import ssl

    cert_file, key_file = certificates
    with Server(binary=real_binary, tls_cert=cert_file, tls_key=key_file) as server:
        assert server.uri == f"hardcharger://127.0.0.1:{server.port}"
        trusted = ssl.create_default_context(cafile=str(cert_file))
        assert sign_in(server=server, password=server.password, context=trusted)[0] == 200
        # Plain HTTP to the TLS port doesn't get a Warp SQL answer.
        with pytest.raises(expected_exception=(urllib.error.URLError, ConnectionError, OSError)):
            urllib.request.urlopen(f"http://127.0.0.1:{server.port}/warp", timeout=5)


# ---- through the client ---------------------------------------------------------------


@pytest.mark.client
def test_connect_returns_a_connection(real_binary):
    with Server(binary=real_binary) as server, server.connect() as conn, conn.cursor() as cur:
        cur.execute(operation="SELECT 42 AS answer, HARDCHARGER_USER() AS who")
        assert cur.fetch_arrow_table().to_pylist() == [{"answer": 42, "who": "hardcharger"}]


@pytest.mark.client
def test_connect_over_tls(real_binary, certificates):
    cert_file, key_file = certificates
    with Server(binary=real_binary, tls_cert=cert_file, tls_key=key_file) as server:
        with server.connect() as conn, conn.cursor() as cur:
            cur.execute(operation="SELECT 1 AS one")
            assert cur.fetchone() == (1,)


@pytest.mark.client
def test_connect_with_a_wrong_password_is_refused(real_binary):
    import adbc_driver_manager

    with Server(binary=real_binary) as server:
        from adbc_driver_hardcharger import dbapi

        with pytest.raises(expected_exception=adbc_driver_manager.ProgrammingError) as error:
            dbapi.connect(uri=server.uri, username=server.username, password="wrong")
    assert "UNAUTHENTICATED" in str(error.value)
    assert "invalid_credentials, HTTP 401" in str(error.value)
