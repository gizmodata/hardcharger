"""Mutual TLS that carries identity (Warp SQL §4.4), as Hardcharger maps it: the
client_certificate grant signs in mtls_admin_identities as the administrator and, under
access control, the store's users; tokens are bound to the certificate (a cnf claim);
sign-ins are audited; the command-line client's --auth-type mtls; the options' checks.
The protocol's black-box side is the conformance suite's test_mtls.py."""

import base64
import datetime
import hashlib
import ipaddress
import json
import socket
import ssl
import subprocess
import time

import httpx
import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from conftest import BINARY, CLIENT_BINARY, TIMEOUT_SCALE, clean_environment, problem
from rbac import Client, WarpError, expect_allowed, expect_blocked, expect_error, mint, rbac_env

NOW = datetime.datetime.now(tz=datetime.UTC)
AUDIT_KEY = "mtls-identity-audit-key"


class Pki:
    """A client CA, a server certificate, and client certificates for e-mail identities."""

    def __init__(self, directory):
        self.directory = directory
        self.ca_key = ec.generate_private_key(curve=ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(oid=NameOID.COMMON_NAME, value="hardcharger-test-ca")])
        self.ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(self.ca_key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(NOW - datetime.timedelta(minutes=5))
                   .not_valid_after(NOW + datetime.timedelta(days=1))
                   .add_extension(extval=x509.BasicConstraints(ca=True, path_length=None), critical=True)
                   .add_extension(extval=x509.KeyUsage(digital_signature=True, content_commitment=False,
                                                       key_encipherment=False, data_encipherment=False,
                                                       key_agreement=False, key_cert_sign=True, crl_sign=True,
                                                       encipher_only=False, decipher_only=False), critical=True)
                   .add_extension(extval=x509.SubjectKeyIdentifier.from_public_key(self.ca_key.public_key()),
                                  critical=False)
                   .sign(private_key=self.ca_key, algorithm=hashes.SHA256()))
        self.ca_path = directory / "ca.pem"
        self.ca_path.write_bytes(self.ca.public_bytes(encoding=serialization.Encoding.PEM))
        self.server = self.issue(name="server", sans=[x509.DNSName("localhost"),
                                                      x509.IPAddress(ipaddress.ip_address("127.0.0.1"))],
                                 usage=ExtendedKeyUsageOID.SERVER_AUTH)

    def issue(self, name: str, sans: list | None = None, usage=ExtendedKeyUsageOID.CLIENT_AUTH):
        """(cert path, key path, certificate) for `name`; an e-mail SAN `name` by default."""
        key = ec.generate_private_key(curve=ec.SECP256R1())
        sans = sans if sans is not None else [x509.RFC822Name(name)]
        cert = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(oid=NameOID.COMMON_NAME, value=name)]))
                .issuer_name(self.ca.subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(NOW - datetime.timedelta(minutes=5))
                .not_valid_after(NOW + datetime.timedelta(days=1))
                .add_extension(extval=x509.ExtendedKeyUsage([usage]), critical=False)
                .add_extension(extval=x509.SubjectAlternativeName(sans), critical=False)
                .add_extension(extval=x509.AuthorityKeyIdentifier.from_issuer_public_key(self.ca_key.public_key()),
                               critical=False)
                .sign(private_key=self.ca_key, algorithm=hashes.SHA256()))
        safe = name.replace("@", "_at_")
        cert_path, key_path = self.directory / f"{safe}.pem", self.directory / f"{safe}.key"
        cert_path.write_bytes(cert.public_bytes(encoding=serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(encoding=serialization.Encoding.PEM,
                                               format=serialization.PrivateFormat.PKCS8,
                                               encryption_algorithm=serialization.NoEncryption()))
        return cert_path, key_path, cert

    def context(self, identity) -> ssl.SSLContext:
        context = ssl.create_default_context(cafile=str(self.ca_path))
        if identity is not None:
            context.load_cert_chain(certfile=str(identity[0]), keyfile=str(identity[1]))
        return context


@pytest.fixture
def pki(tmp_path):
    return Pki(directory=tmp_path)


class TlsServer:
    def __init__(self, running, pki: Pki):
        self.running = running
        self.pki = pki
        self.base_url = f"https://127.0.0.1:{running.port}"

    def http(self, identity) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, verify=self.pki.context(identity=identity), timeout=60)

    def certificate_grant(self, identity) -> httpx.Response:
        with self.http(identity=identity) as http:
            return http.post(url="/warp/v1/auth/token", json={"grant_type": "client_certificate"})

    def certificate_token(self, identity) -> str:
        response = self.certificate_grant(identity=identity)
        assert response.status_code == 200, response.text
        return response.json()["access_token"]

    def password_token(self, identity, user: str, password: str) -> str:
        with self.http(identity=identity) as http:
            response = http.post(url="/warp/v1/auth/token",
                                 json={"grant_type": "password", "username": user, "password": password})
        assert response.status_code == 200, response.text
        return response.json()["access_token"]

    def client(self, identity, token: str) -> Client:
        return Client(base_url=self.base_url, token=token, verify=self.pki.context(identity=identity))

    def admin(self, identity) -> Client:
        token = self.password_token(identity=identity, user=self.running.username, password=self.running.password)
        return self.client(identity=identity, token=token)


def start(launcher, pki: Pki, probe, args: list[str] | None = None, env: dict | None = None) -> TlsServer:
    """A Hardcharger with TLS and the test CA for client certificates, `probe` the
    identity used to see it come up."""
    running = launcher.start(
        args=["--tls-cert", str(pki.server[0]), "--tls-key", str(pki.server[1]), "--mtls-ca-cert", str(pki.ca_path),
              *(args or [])],
        env={"HARDCHARGER_LOG_FORMAT": "json", "HARDCHARGER_PRINT_QUERIES": "false", **(env or {})},
        expect_up=False)
    server = TlsServer(running=running, pki=pki)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if running.process.poll() is not None:
            pytest.fail(reason=f"server exited: {running.log()}")
        try:
            with server.http(identity=probe) as http:
                if http.get(url="/warp", timeout=2).status_code == 200:
                    return server
        except httpx.HTTPError:
            time.sleep(0.2)
    pytest.fail(reason=f"server did not come up: {running.log()}")


def audit_records(server: TlsServer) -> list[dict]:
    return [r for r in server.running.log_records() if r.get("kind") == "authentication"]


# ---- Without access control: mtls_admin_identities ----

def test_an_admin_identity_signs_in_as_the_administrator(launcher, pki):
    ops = pki.issue(name="ops@example.com")
    server = start(launcher=launcher, pki=pki, probe=ops, args=["--mtls-certificate-sign-in", "--mtls-admin-identities", "OPS@example.com"])
    token = server.certificate_token(identity=ops)
    claims = jwt.decode(jwt=token, options={"verify_signature": False})
    # The configured spelling, the administrator's role, the method; bound to the certificate.
    assert claims["sub"] == "OPS@example.com" and claims["role"] == "admin"
    assert claims["auth_method"] == "client_certificate"
    der = ops[2].public_bytes(encoding=serialization.Encoding.DER)
    thumbprint = base64.urlsafe_b64encode(hashlib.sha256(der).digest()).rstrip(b"=").decode()
    assert claims["cnf"] == {"x5t#S256": thumbprint}
    session = server.client(identity=ops, token=token)
    rows = session.run(sql="SELECT username, role, auth_method FROM hardcharger_sessions() WHERE is_current").to_pylist()
    assert rows == [{"username": "OPS@example.com", "role": "admin", "auth_method": "client_certificate"}]


def test_any_other_identity_is_refused_without_access_control(launcher, pki):
    ops, stranger = pki.issue(name="ops@example.com"), pki.issue(name="stranger@example.com")
    server = start(launcher=launcher, pki=pki, probe=ops, args=["--mtls-certificate-sign-in", "--mtls-admin-identities", "ops@example.com"])
    body = problem(response=server.certificate_grant(identity=stranger), status=401,
                   code="unknown_certificate_identity")
    assert "stranger@example.com" in body["detail"]
    # Not offered at all without an identity that could use it.
    plain = start(launcher=launcher, pki=pki, probe=ops)
    with plain.http(identity=ops) as http:
        assert "client_certificate" not in http.get(url="/warp").json()["auth"]["grant_types"]
        problem(response=http.post(url="/warp/v1/auth/token", json={"grant_type": "client_certificate"}),
                status=400, code="unsupported_grant_type")
    with server.http(identity=ops) as http:
        assert "client_certificate" in http.get(url="/warp").json()["auth"]["grant_types"]


def test_an_admin_identity_is_a_name_not_a_pattern(launcher, pki):
    ops, other = pki.issue(name="ops@example.com"), pki.issue(name="eve@example.com")
    server = start(launcher=launcher, pki=pki, probe=ops, args=["--mtls-certificate-sign-in", "--mtls-admin-identities", "*@example.com"])
    problem(response=server.certificate_grant(identity=other), status=401, code="unknown_certificate_identity")
    problem(response=server.certificate_grant(identity=ops), status=401, code="unknown_certificate_identity")


def test_a_bound_token_minted_with_a_broken_cnf_is_refused(launcher, pki):
    # A token with the server's own signature (the tests know its key) whose cnf can't
    # be read binds to nothing the server can check: refused, never treated as unbound.
    ops = pki.issue(name="ops@example.com")
    server = start(launcher=launcher, pki=pki, probe=ops, args=["--mtls-certificate-sign-in", "--mtls-admin-identities", "ops@example.com"])
    good = jwt.decode(jwt=server.certificate_token(identity=ops), options={"verify_signature": False})
    with server.http(identity=ops) as http:
        for cnf in [{"x5t#S256": ""}, {"x5t": good["cnf"]["x5t#S256"]}, "x", good["cnf"]]:
            token = jwt.encode(payload={**good, "cnf": cnf}, key=server.running.secret_key, algorithm="HS256")
            response = http.post(url="/warp/v1/sessions", headers={"Authorization": f"Bearer {token}"}, json={})
            if cnf == good["cnf"]:
                assert response.status_code == 201, response.text  # the twin: intact, it works
            else:
                problem(response=response, status=401, code="invalid_token")


# ---- Under access control: the store's users ----

@pytest.fixture
def rbac(launcher, store, pki):
    ops = pki.issue(name="ops@example.com")
    store_uri = store.new_database()
    server = start(launcher=launcher, pki=pki, probe=ops,
                   args=["--mtls-certificate-sign-in", "--mtls-admin-identities", "ops@example.com"],
                   env={**rbac_env(store_uri=store_uri), "HARDCHARGER_AUDIT_CHAIN_KEY": AUDIT_KEY})
    server.ops = ops
    server.store_uri = store_uri
    return server


def test_a_store_user_signs_in_with_a_certificate_and_grants_decide(rbac, pki):
    carol = pki.issue(name="carol@example.com")
    admin = rbac.admin(identity=rbac.ops)
    for sql in ['CREATE USER "carol@example.com"',  # no password: certificate only
                "CREATE TABLE ledger AS SELECT 1 AS id", "CREATE TABLE vault AS SELECT 2 AS id",
                "CREATE ROLE readers", "GRANT SELECT ON ledger TO readers", 'GRANT readers TO "carol@example.com"']:
        expect_allowed(client=admin, sql=sql)
    token = rbac.certificate_token(identity=carol)
    assert jwt.decode(jwt=token, options={"verify_signature": False})["role"] == "user"
    session = rbac.client(identity=carol, token=token)
    assert session.run(sql="SELECT id FROM ledger").to_pylist() == [{"id": 1}]
    expect_blocked(client=session, sql="SELECT id FROM vault")
    # The name is compared as user names are (ASCII case ignored).
    shouty = pki.issue(name="CAROL@example.com")
    rbac.client(identity=shouty, token=rbac.certificate_token(identity=shouty)).run(sql="SELECT id FROM ledger")
    # A user without a password cannot sign in with one.
    with rbac.http(identity=carol) as http:
        response = http.post(url="/warp/v1/auth/token",
                             json={"grant_type": "password", "username": "carol@example.com", "password": ""})
    problem(response=response, status=401, code="invalid_credentials")


def test_an_identity_the_store_does_not_have_is_refused(rbac, pki):
    dave = pki.issue(name="dave@example.com")
    body = problem(response=rbac.certificate_grant(identity=dave), status=401, code="unknown_certificate_identity")
    assert "CREATE USER" in body["detail"]
    expect_allowed(client=rbac.admin(identity=rbac.ops), sql='CREATE USER "dave@example.com" PASSWORD \'pw\'')
    # The twin: once the user exists (a password doesn't matter), the same certificate signs in.
    rbac.certificate_token(identity=dave)


def test_disabling_or_dropping_the_user_ends_certificate_sign_in(rbac, pki):
    erin = pki.issue(name="erin@example.com")
    admin = rbac.admin(identity=rbac.ops)
    expect_allowed(client=admin, sql='CREATE USER "erin@example.com"')
    token = rbac.certificate_token(identity=erin)
    session = rbac.client(identity=erin, token=token)
    expect_allowed(client=admin, sql='ALTER USER "erin@example.com" DISABLE')
    problem(response=rbac.certificate_grant(identity=erin), status=401, code="unknown_certificate_identity")
    # The token already issued stops working too (checked against the store every request).
    deadline = time.monotonic() + 15
    while True:
        try:
            session.run(sql="SELECT 1")
        except WarpError as error:
            assert error.code in ("invalid_token", "session_ended"), str(error)
            break
        assert time.monotonic() < deadline, "a disabled user's bound token kept working"
        time.sleep(0.2)
    expect_allowed(client=admin, sql='ALTER USER "erin@example.com" ENABLE')
    rbac.certificate_token(identity=erin)
    expect_allowed(client=admin, sql='DROP USER "erin@example.com"')
    problem(response=rbac.certificate_grant(identity=erin), status=401, code="unknown_certificate_identity")


def test_an_admin_identity_is_the_administrator_under_access_control(rbac, pki):
    ops = rbac.client(identity=rbac.ops, token=rbac.certificate_token(identity=rbac.ops))
    expect_allowed(client=ops, sql='CREATE USER "frank@example.com"')
    expect_allowed(client=ops, sql="CREATE TABLE ops_only AS SELECT 1 AS id")


def test_an_admin_identity_is_no_store_user_name(rbac, pki, launcher, store):
    # Handles belong to a principal by name: the administrator and a store user of the
    # same name would share sessions. No store user may take an admin identity's name...
    admin = rbac.admin(identity=rbac.ops)
    expect_error(call=lambda: admin.run(sql='CREATE USER "OPS@example.com"'), code="sql_error",
                 why="signs in as the server administrator with a client certificate")
    expect_allowed(client=admin, sql='CREATE USER "nina@example.com"')
    # ... and one that already has it (made before the identity was configured, or on a
    # replica configured otherwise) makes the certificate sign-in refuse.
    nina = pki.issue(name="nina@example.com")
    rbac.certificate_token(identity=nina)  # a store user, as such
    other = start(launcher=launcher, pki=pki, probe=nina, args=["--mtls-certificate-sign-in", "--mtls-admin-identities", "nina@example.com"],
                  env={**rbac_env(store_uri=rbac.store_uri)})
    body = problem(response=other.certificate_grant(identity=nina), status=401, code="unknown_certificate_identity")
    assert "both one of mtls_admin_identities and a user" in body["detail"], body


def thumbprint_of(identity) -> str:
    der = identity[2].public_bytes(encoding=serialization.Encoding.DER)
    return base64.urlsafe_b64encode(hashlib.sha256(der).digest()).rstrip(b"=").decode()


def test_a_certificate_session_refuses_the_same_users_password_token(rbac, pki):
    carol = pki.issue(name="carol@example.com")
    expect_allowed(client=rbac.admin(identity=rbac.ops), sql="CREATE USER \"carol@example.com\" PASSWORD 'pw:1'")
    cert_session = rbac.client(identity=carol, token=rbac.certificate_token(identity=carol))
    password_token = rbac.password_token(identity=carol, user="carol@example.com", password="pw:1")
    with rbac.http(identity=carol) as http:
        response = http.post(url=f"{cert_session.url}/statements", headers={"Authorization": f"Bearer {password_token}"},
                             json={"sql": "SELECT 1"})
    problem(response=response, status=401, code="certificate_mismatch")
    expect_allowed(client=cert_session, sql="SELECT 1")  # its own token still works
    # A session of the password token is unbound, as before.
    expect_allowed(client=rbac.client(identity=carol, token=password_token), sql="SELECT 1")


def test_scoped_tokens_inherit_the_minters_certificate_binding(rbac, pki):
    carol = pki.issue(name="carol@example.com")
    carol_again = pki.issue(name="carol-again", sans=[x509.RFC822Name("carol@example.com")])  # hers, another key
    admin = rbac.admin(identity=rbac.ops)
    expect_allowed(client=admin, sql="CREATE USER \"carol@example.com\" PASSWORD 'pw:1'")
    cert_session = rbac.client(identity=carol, token=rbac.certificate_token(identity=carol))
    scoped = cert_session.run(sql="CREATE SCOPED TOKEN job ALLOW SELECT").to_pylist()[0]["token"]
    claims = jwt.decode(jwt=scoped, options={"verify_signature": False})
    assert claims["cnf"] == {"x5t#S256": thumbprint_of(identity=carol)}
    # Over the minter's certificate it works, as a bearer token and as user "token"'s password.
    expect_allowed(client=rbac.client(identity=carol, token=scoped), sql="SELECT 1")
    exchanged = rbac.password_token(identity=carol, user="token", password=scoped)
    assert jwt.decode(jwt=exchanged, options={"verify_signature": False})["cnf"] == claims["cnf"]
    expect_allowed(client=rbac.client(identity=carol, token=exchanged), sql="SELECT 1")
    # Over another certificate (even one of hers) both are refused.
    with rbac.http(identity=carol_again) as http:
        for token in (scoped, exchanged):
            response = http.post(url="/warp/v1/sessions", headers={"Authorization": f"Bearer {token}"}, json={})
            problem(response=response, status=401, code="certificate_mismatch")
    # The twin: a password-signed minter's scoped token is unbound, and works over any certificate.
    password_session = rbac.client(identity=carol, token=rbac.password_token(identity=carol, user="carol@example.com",
                                                                            password="pw:1"))
    unbound = password_session.run(sql="CREATE SCOPED TOKEN job2 ALLOW SELECT").to_pylist()[0]["token"]
    assert "cnf" not in jwt.decode(jwt=unbound, options={"verify_signature": False})
    expect_allowed(client=rbac.client(identity=carol_again, token=unbound), sql="SELECT 1")


def test_certificate_sign_ins_are_audited(rbac, pki, tmp_path):
    carol, stranger = pki.issue(name="carol@example.com"), pki.issue(name="stranger@example.com")
    expect_allowed(client=rbac.admin(identity=rbac.ops), sql='CREATE USER "carol@example.com"')
    rbac.certificate_token(identity=carol)
    problem(response=rbac.certificate_grant(identity=stranger), status=401, code="unknown_certificate_identity")
    records = audit_records(server=rbac)
    allowed = [r for r in records if r.get("status") == "allowed" and r.get("certificate_identity") == "carol@example.com"]
    denied = [r for r in records if r.get("status") == "denied" and "audit_hash" in r]
    assert len(allowed) == 1 and len(denied) == 1, records
    der = carol[2].public_bytes(encoding=serialization.Encoding.DER)
    assert allowed[0]["method"] == "client_certificate"
    assert allowed[0]["user"] == "carol@example.com" and allowed[0]["role"] == "user"
    assert allowed[0]["certificate_thumbprint"] == (
        base64.urlsafe_b64encode(hashlib.sha256(der).digest()).rstrip(b"=").decode())
    assert "CN=carol@example.com" in allowed[0]["certificate_subject"]
    assert denied[0]["code"] == "unknown_certificate_identity"
    assert denied[0]["user"] == "stranger@example.com"
    assert denied[0]["certificate_thumbprint"] == thumbprint_of(identity=stranger)
    # Both are links of the access-control audit chain, which verifies.
    assert all("audit_hash" in r for r in allowed + denied)
    log = tmp_path / "audit.log"
    log.write_text(rbac.running.log())
    out = subprocess.run(args=[str(BINARY), "--verify-audit-log", str(log)], capture_output=True, text=True,
                         env={"PATH": "/usr/bin", "HARDCHARGER_AUDIT_CHAIN_KEY": AUDIT_KEY})
    assert out.returncode == 0, out.stdout + out.stderr


def test_rules_only_access_control_signs_in_no_one_but_the_admin_identities(launcher, pki):
    # Without a store, sign-in tokens' catalog_access rules restrict sessions; a
    # certificate carries no rules, so only the administrator's identities may use it.
    ops, carol = pki.issue(name="ops@example.com"), pki.issue(name="carol@example.com")
    server = start(launcher=launcher, pki=pki, probe=ops, args=["--mtls-certificate-sign-in", "--mtls-admin-identities", "ops@example.com"])
    problem(response=server.certificate_grant(identity=carol), status=401, code="unknown_certificate_identity")
    server.certificate_token(identity=ops)


# ---- Options ----

@pytest.mark.parametrize("args, message", [
    (["--mtls-admin-identities", "ops@example.com"], "mtls_admin_identities needs mtls_ca_cert"),
    (["--mtls-require-identity-match"], "mtls_require_identity_match needs mtls_ca_cert"),
    (["--mtls-crl-file", "x.pem"], "mtls_crl_file needs mtls_ca_cert"),
    (["--mtls-require-client-cert=false"], "mtls_require_client_cert needs mtls_ca_cert"),
    (["--mtls-identity-source", "subject"], "mtls_identity_source must be san_email, san_uri or subject_cn"),
])
def test_mtls_options_are_checked(launcher, pki, args, message):
    tls = ["--tls-cert", str(pki.server[0]), "--tls-key", str(pki.server[1])]
    running = launcher.start(args=[*tls, *args], expect_up=False)
    assert running.process.wait(timeout=20) != 0
    assert message in running.log(), running.log()


def test_mtls_ca_cert_without_tls_is_refused(launcher, pki):
    # It used to be ignored: a server that looked like it required client certificates
    # took plain HTTP from anyone.
    running = launcher.start(args=["--mtls-ca-cert", str(pki.ca_path)], expect_up=False)
    assert running.process.wait(timeout=20) != 0
    assert "mtls_ca_cert needs tls_cert and tls_key" in running.log()


def test_a_crl_file_that_cannot_be_read_stops_startup(launcher, pki, tmp_path):
    ops = pki.issue(name="ops@example.com")
    broken = tmp_path / "broken.crl"
    broken.write_text("-----BEGIN X509 CRL-----\nnot a crl\n-----END X509 CRL-----\n")
    tls = ["--tls-cert", str(pki.server[0]), "--tls-key", str(pki.server[1]), "--mtls-ca-cert", str(pki.ca_path)]
    running = launcher.start(args=[*tls, "--mtls-crl-file", str(broken)], expect_up=False)
    assert running.process.wait(timeout=20) != 0
    assert "CRL" in running.log(), running.log()
    # The twin: a real (empty) CRL starts, and its load is logged.
    crl = (x509.CertificateRevocationListBuilder().issuer_name(pki.ca.subject)
           .last_update(NOW - datetime.timedelta(minutes=5)).next_update(NOW + datetime.timedelta(days=1))
           .sign(private_key=pki.ca_key, algorithm=hashes.SHA256()))
    good = tmp_path / "good.crl"
    good.write_bytes(crl.public_bytes(encoding=serialization.Encoding.PEM))
    server = start(launcher=launcher, pki=pki, probe=ops, args=["--mtls-crl-file", str(good)])
    assert any("loaded the client CRL file" in r.get("message", r.get("msg", "")) for r in server.running.log_records()), \
        server.running.log()


# ---- The command-line client ----

def run_client(server: TlsServer, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args=[str(CLIENT_BINARY), "--host", "127.0.0.1", "--port", str(server.running.port),
                                "--tls", "--tls-roots", str(server.pki.ca_path), *args],
                          capture_output=True, text=True, timeout=60, env=clean_environment())


def test_the_client_signs_in_with_its_certificate(launcher, pki):
    ops, stranger = pki.issue(name="ops@example.com"), pki.issue(name="stranger@example.com")
    server = start(launcher=launcher, pki=pki, probe=ops, args=["--mtls-certificate-sign-in", "--mtls-admin-identities", "ops@example.com"])
    ok = run_client(server, "--mtls-cert", str(ops[0]), "--mtls-key", str(ops[1]), "--auth-type", "mtls",
                    "--csv", "--no-header", "-c", "SELECT auth_method FROM hardcharger_sessions() WHERE is_current")
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout.strip() == "client_certificate"
    refused = run_client(server, "--mtls-cert", str(stranger[0]), "--mtls-key", str(stranger[1]),
                         "--auth-type", "mtls", "-c", "SELECT 1")
    assert refused.returncode != 0
    assert "Certificate sign-in" in refused.stderr and "stranger@example.com" in refused.stderr, refused.stderr
    missing = run_client(server, "--auth-type", "mtls", "-c", "SELECT 1")
    assert missing.returncode != 0
    assert "--auth-type mtls needs --mtls-cert and --mtls-key" in missing.stderr, missing.stderr


# ---- Review fixes ----

def test_certificate_sign_in_is_off_unless_enabled(launcher, store, pki):
    # A store user and a client CA are not enough: with mtls_ca_cert alone the certificate
    # admits the connection and signs no one in.
    ops, alice = pki.issue(name="ops@example.com"), pki.issue(name="alice@example.com")
    store_uri = store.new_database()
    server = start(launcher=launcher, pki=pki, probe=ops, env=rbac_env(store_uri=store_uri))
    admin = server.admin(identity=ops)
    expect_allowed(client=admin, sql="CREATE USER \"alice@example.com\" PASSWORD 'S3cret:pw'")
    with server.http(identity=alice) as http:
        assert "client_certificate" not in http.get(url="/warp").json()["auth"]["grant_types"]
    problem(response=server.certificate_grant(identity=alice), status=400, code="unsupported_grant_type")
    # The twin: the same store, with certificate sign-in turned on.
    enabled = start(launcher=launcher, pki=pki, probe=ops, args=["--mtls-certificate-sign-in"],
                    env=rbac_env(store_uri=store_uri))
    enabled.certificate_token(identity=alice)
    # Admin identities without it are a configuration error, not a silent no-op.
    tls = ["--tls-cert", str(pki.server[0]), "--tls-key", str(pki.server[1]), "--mtls-ca-cert", str(pki.ca_path)]
    running = launcher.start(args=[*tls, "--mtls-admin-identities", "ops@example.com"], expect_up=False)
    assert running.process.wait(timeout=20) != 0
    assert "mtls_admin_identities needs mtls_certificate_sign_in" in running.log(), running.log()


def test_certificate_refusals_are_security_events_and_audited(rbac, pki, tmp_path):
    carol = pki.issue(name="carol@example.com")
    carol_again = pki.issue(name="carol-again", sans=[x509.RFC822Name("carol@example.com")])
    stranger = pki.issue(name="stranger@example.com")
    expect_allowed(client=rbac.admin(identity=rbac.ops), sql='CREATE USER "carol@example.com"')
    token = rbac.certificate_token(identity=carol)
    jti = jwt.decode(jwt=token, options={"verify_signature": False})["jti"]
    with rbac.http(identity=carol_again) as http:  # a stolen token, replayed over another certificate
        for _ in range(2):
            problem(response=http.post(url="/warp/v1/sessions", headers={"Authorization": f"Bearer {token}"}, json={}),
                    status=401, code="certificate_mismatch")
    problem(response=rbac.certificate_grant(identity=stranger), status=401, code="unknown_certificate_identity")
    time.sleep(0.5)
    records = rbac.running.log_records()
    warnings = [r for r in records if r.get("message") == "security: refused certificate_mismatch"]
    assert len(warnings) == 2, records
    for record in warnings:
        assert record["level"] == "warning"
        assert record["user"] == "carol@example.com" and record["token_id"] == jti
        assert record["certificate_thumbprint"] == thumbprint_of(identity=carol_again)
        assert record["peer"].startswith("127.0.0.1:")
    audited = [r for r in records if r.get("message") == "client certificate check refused a request"]
    assert {r["code"] for r in audited} == {"certificate_mismatch", "unknown_certificate_identity"}, audited
    assert len(audited) == 3 and all("audit_hash" in r for r in audited)
    assert [r for r in audited if r["code"] == "unknown_certificate_identity"][0]["user"] == "stranger@example.com"
    # The refused uses were never logged as a validated token; the twin, a real use, is.
    validated = [r for r in records if r.get("message") == "Bearer Token was validated successfully"
                 and r.get("user") == "carol@example.com"]
    assert validated == [], validated
    rbac.client(identity=carol, token=token)
    time.sleep(0.5)
    assert any(r.get("message") == "Bearer Token was validated successfully" and r.get("user") == "carol@example.com"
               for r in rbac.running.log_records())
    log = tmp_path / "audit.log"
    log.write_text(rbac.running.log())
    out = subprocess.run(args=[str(BINARY), "--verify-audit-log", str(log)], capture_output=True, text=True,
                         env={"PATH": "/usr/bin", "HARDCHARGER_AUDIT_CHAIN_KEY": AUDIT_KEY})
    assert out.returncode == 0, out.stdout + out.stderr


def test_a_bound_token_cannot_mint_in_an_unbound_session(rbac, pki):
    # The probe's route to an unbound scoped token: a password session, a bound token on it.
    carol = pki.issue(name="carol@example.com")
    expect_allowed(client=rbac.admin(identity=rbac.ops), sql="CREATE USER \"carol@example.com\" PASSWORD 'pw:1'")
    password_session = rbac.client(identity=carol, token=rbac.password_token(identity=carol, user="carol@example.com",
                                                                            password="pw:1"))
    bound = rbac.certificate_token(identity=carol)
    with rbac.http(identity=carol) as http:
        response = http.post(url=f"{password_session.url}/statements", headers={"Authorization": f"Bearer {bound}"},
                             json={"sql": "CREATE SCOPED TOKEN viabound ALLOW SELECT"})
    problem(response=response, status=401, code="certificate_mismatch")
    # Minted under the bound token's own session, it carries the binding (the twin).
    scoped = rbac.client(identity=carol, token=bound).run(sql="CREATE SCOPED TOKEN own ALLOW SELECT").to_pylist()[0]
    assert jwt.decode(jwt=scoped["token"], options={"verify_signature": False})["cnf"] == {
        "x5t#S256": thumbprint_of(identity=carol)}


def test_a_bound_admin_session_cannot_mint_for_another_user(rbac, pki):
    admin = rbac.admin(identity=rbac.ops)  # a password sign-in (over a certificate connection)
    expect_allowed(client=admin, sql='CREATE USER "ula@example.com"')
    bound_admin = rbac.client(identity=rbac.ops, token=rbac.certificate_token(identity=rbac.ops))
    expect_blocked(client=bound_admin, sql='CREATE SCOPED TOKEN ops FOR USER "ula@example.com" ALLOW SELECT',
                   why="its scoped tokens are bound to that certificate")
    # The twin: from the password sign-in it works, and the token is unbound.
    rows = expect_allowed(client=admin, sql='CREATE SCOPED TOKEN ops FOR USER "ula@example.com" ALLOW SELECT').to_pylist()
    assert "cnf" not in jwt.decode(jwt=rows[0]["token"], options={"verify_signature": False})


# ---- Second review ----

SPIFFE_ID = "spiffe://example.org/ns/etl/sa/loader"


def spiffe_rbac(launcher, store, pki):
    ops = pki.issue(name="ops@example.com", sans=[x509.RFC822Name("ops@example.com"),
                                                  x509.UniformResourceIdentifier("spiffe://example.org/ops")])
    server = start(launcher=launcher, pki=pki, probe=ops,
                   args=["--mtls-certificate-sign-in", "--mtls-identity-source", "san_uri"],
                   env=rbac_env(store_uri=store.new_database()))
    server.ops = ops
    return server


def test_a_spiffe_id_is_an_rbac_user(launcher, store, pki):
    server = spiffe_rbac(launcher=launcher, store=store, pki=pki)
    loader = pki.issue(name="loader", sans=[x509.UniformResourceIdentifier(SPIFFE_ID)])
    problem(response=server.certificate_grant(identity=loader), status=401, code="unknown_certificate_identity")
    admin = server.admin(identity=server.ops)
    for sql in [f'CREATE USER "{SPIFFE_ID}"', "CREATE TABLE staged AS SELECT 1 AS id", "CREATE TABLE vault AS SELECT 2 AS id",
                "CREATE ROLE loaders", "GRANT SELECT ON staged TO loaders", f'GRANT loaders TO "{SPIFFE_ID}"']:
        expect_allowed(client=admin, sql=sql)
    token = server.certificate_token(identity=loader)
    assert jwt.decode(jwt=token, options={"verify_signature": False})["sub"] == SPIFFE_ID
    session = server.client(identity=loader, token=token)
    assert session.run(sql="SELECT id FROM staged").to_pylist() == [{"id": 1}]
    expect_blocked(client=session, sql="SELECT id FROM vault")


def test_a_uri_name_never_belongs_to_a_tenant(launcher, store, pki):
    server = spiffe_rbac(launcher=launcher, store=store, pki=pki)
    admin = server.admin(identity=server.ops)
    for sql in ["CREATE TENANT acme", "CREATE USER acme/boss", "GRANT acme/tenant_admin TO acme/boss",
                "CREATE ROLE acme/readers", f'CREATE USER "{SPIFFE_ID}"']:
        expect_allowed(client=admin, sql=sql)
    boss = server.client(identity=server.ops, token=mint(user="acme/boss"))
    # A tenant admin manages its tenant's users; a URI-form user is system scope, so not theirs.
    for sql in ['CREATE USER "spiffe://example.org/ns/acme/sa/escape"', 'CREATE USER "SPIFFE://example.org/x"',
                'DROP USER "spiffe://example.org/ns/etl/sa/loader"']:
        expect_error(call=lambda sql=sql: boss.run(sql=sql), code="permission_denied",
                     why="only administrators manage users")
    # Nor can a tenant name hide one: "acme/spiffe://..." is no valid tenant user name.
    expect_error(call=lambda: admin.run(sql='CREATE USER "acme/spiffe://example.org/x"'), code="sql_error",
                 why="a tenant's users and roles are named tenant/name")
    # A tenant's role never reaches it, and a role can't be named like one.
    expect_error(call=lambda: admin.run(sql=f'GRANT acme/readers TO "{SPIFFE_ID}"'), code="sql_error",
                 why="roles never cross tenants")
    expect_error(call=lambda: admin.run(sql='CREATE ROLE "spiffe://example.org/role"'), code="sql_error",
                 why="a role name cannot be a URI")
    # The twin: the tenant admin still manages its own tenant's users.
    expect_allowed(client=boss, sql="CREATE USER acme/newhire")


def test_the_handshake_refusing_a_certificate_is_audited(launcher, pki, tmp_path):
    ops, dave = pki.issue(name="ops@example.com"), pki.issue(name="dave@example.com")
    crl = (x509.CertificateRevocationListBuilder().issuer_name(pki.ca.subject)
           .last_update(NOW - datetime.timedelta(minutes=5)).next_update(NOW + datetime.timedelta(days=1))
           .add_revoked_certificate(x509.RevokedCertificateBuilder().serial_number(dave[2].serial_number)
                                    .revocation_date(NOW - datetime.timedelta(minutes=1)).build())
           .sign(private_key=pki.ca_key, algorithm=hashes.SHA256()))
    path = tmp_path / "revoked.crl"
    path.write_bytes(crl.public_bytes(encoding=serialization.Encoding.PEM))
    server = start(launcher=launcher, pki=pki, probe=ops, args=["--mtls-crl-file", str(path)],
                   env={"HARDCHARGER_AUDIT_CHAIN_KEY": AUDIT_KEY})
    with pytest.raises((httpx.HTTPError, ssl.SSLError)):
        with server.http(identity=dave) as http:
            http.get(url="/warp")
    time.sleep(0.5)
    records = [r for r in server.running.log_records() if r.get("message") == "the TLS handshake refused a client certificate"]
    assert len(records) == 1, server.running.log()
    record = records[0]
    assert record["code"] == "client_certificate_refused" and "revoked" in record["error"]
    assert record["certificate_thumbprint"] == thumbprint_of(identity=dave)
    assert "CN=dave@example.com" in record["certificate_subject"] and record["peer"].startswith("127.0.0.1:")
    assert "audit_hash" in record
    # The twin: a certificate the handshake accepts writes no such record.
    with server.http(identity=ops) as http:
        assert http.get(url="/warp").status_code == 200
    time.sleep(0.3)
    assert len([r for r in server.running.log_records()
                if r.get("message") == "the TLS handshake refused a client certificate"]) == 1


def test_a_flood_of_refusals_is_bounded_and_still_counted(launcher, pki):
    import concurrent.futures
    ops, bob = pki.issue(name="ops@example.com"), pki.issue(name="bob@example.com")
    server = start(launcher=launcher, pki=pki, probe=ops,
                   args=["--mtls-require-client-cert=false", "--mtls-certificate-sign-in",
                         "--mtls-admin-identities", "ops@example.com"],
                   env={"HARDCHARGER_AUDIT_CHAIN_KEY": AUDIT_KEY})
    before = len(server.running.log())
    total = 10_000

    def flood(count: int) -> int:
        refused = 0
        with server.http(identity=None) as http:  # no certificate, no credentials
            for _ in range(count):
                response = http.post(url="/warp/v1/auth/token", json={"grant_type": "client_certificate"})
                refused += response.status_code == 401
        return refused

    started_at = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        flooding = [pool.submit(flood, total // 8) for _ in range(8)]
        # Someone else's refusal in the same window is still recorded in full.
        problem(response=server.certificate_grant(identity=bob), status=401, code="unknown_certificate_identity")
        assert sum(f.result() for f in flooding) == total
    windows = int((time.monotonic() - started_at) // 60) + 1  # minutes the flood spanned (a sanitizer build is slow)
    flood_records = lambda: [r for r in server.running.log_records()
                             if r.get("kind") == "authentication" and r.get("code") == "client_certificate_required"
                             and "audit_hash" in r]
    counted = lambda records: sum(int(r.get("suppressed", 1)) for r in records)
    # The windows end, and summaries count what wasn't written one by one: every refusal is
    # accounted for, ten a minute in full.
    deadline = time.monotonic() + 90 * TIMEOUT_SCALE
    while counted(flood_records()) < total and time.monotonic() < deadline:
        time.sleep(1)
    records = flood_records()
    full = [r for r in records if "suppressed" not in r]
    summaries = [r for r in records if "suppressed" in r]
    assert counted(records) == total, (len(full), [r["suppressed"] for r in summaries])
    assert 10 <= len(full) <= 10 * windows and 1 <= len(summaries) <= windows, (len(full), len(summaries))
    assert len(server.running.log()) - before < 64 * 1024 * windows, "the flood grew the log without bound"
    bob_records = [r for r in server.running.log_records()
                   if r.get("code") == "unknown_certificate_identity" and "audit_hash" in r]
    assert len(bob_records) == 1 and bob_records[0]["user"] == "bob@example.com"

# ---- Third review (from the reviewer's probes) ----

HANDSHAKE_MESSAGE = "the TLS handshake refused a client certificate"


def handshake_records(server) -> list[dict]:
    return [r for r in server.running.log_records() if r.get("message") == HANDSHAKE_MESSAGE]


def self_signed(directory, key, index: int, subject_attributes: int = 1):
    """A self-signed client certificate (no CA the server trusts), reusing `key`: minting
    a new thumbprint costs the client nothing but a serial number."""
    attributes = [x509.NameAttribute(oid=NameOID.COMMON_NAME, value=f"flood-{index}")]
    attributes += [x509.NameAttribute(oid=NameOID.ORGANIZATIONAL_UNIT_NAME, value="u" * 60)
                   for _ in range(subject_attributes - 1)]
    name = x509.Name(attributes)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(NOW - datetime.timedelta(minutes=5)).not_valid_after(NOW + datetime.timedelta(days=1))
            .add_extension(extval=x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
            .sign(private_key=key, algorithm=hashes.SHA256()))
    cert_path, key_path = directory / f"flood-{index}.pem", directory / "flood.key"
    cert_path.write_bytes(cert.public_bytes(encoding=serialization.Encoding.PEM))
    if not key_path.exists():
        key_path.write_bytes(key.private_bytes(encoding=serialization.Encoding.PEM,
                                               format=serialization.PrivateFormat.PKCS8,
                                               encryption_algorithm=serialization.NoEncryption()))
    return cert_path, key_path


def try_handshake(port: int, ca_path, cert_path, key_path) -> None:
    context = ssl.create_default_context(cafile=str(ca_path))
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
            with context.wrap_socket(raw, server_hostname="127.0.0.1") as tls:
                tls.sendall(b"GET /warp HTTP/1.1\r\nHost: x\r\n\r\n")
                tls.recv(1)  # TLS 1.3: the server's refusal arrives after the client's Finished
    except (ssl.SSLError, OSError):
        pass


# ---- Finding: handshake refusals are rate-limited by a thumbprint the client chooses ----

def test_handshake_refusals_from_one_address_are_rate_limited(launcher, pki, tmp_path):
    """One address presenting 60 untrusted certificates (one key, new serials) must not get
    60 full audit records: the spec (§4.4) asks for refusal records rate-limited per source."""
    ops = pki.issue(name="ops@example.com")
    server = start(launcher=launcher, pki=pki, probe=ops, env={"HARDCHARGER_AUDIT_CHAIN_KEY": AUDIT_KEY})
    key = ec.generate_private_key(curve=ec.SECP256R1())
    attempts = 60
    for index in range(attempts):
        cert_path, key_path = self_signed(directory=tmp_path, key=key, index=index)
        try_handshake(port=server.running.port, ca_path=pki.ca_path, cert_path=cert_path, key_path=key_path)
    time.sleep(1)
    records = handshake_records(server)
    thumbprints = {r["certificate_thumbprint"] for r in records}
    print(f"{len(records)} full handshake-refusal records for {attempts} attempts, {len(thumbprints)} sources")
    assert len(records) <= 20, f"{len(records)} records in full for {attempts} refusals from one address"


def test_one_handshake_refusal_record_has_a_bounded_size(launcher, pki, tmp_path):
    """A refused, unverified certificate's subject goes into the log and the audit in full:
    its size is the attacker's choice (up to OpenSSL's 100 KiB certificate-list limit)."""
    ops = pki.issue(name="ops@example.com")
    server = start(launcher=launcher, pki=pki, probe=ops, env={"HARDCHARGER_AUDIT_CHAIN_KEY": AUDIT_KEY})
    key = ec.generate_private_key(curve=ec.SECP256R1())
    before = len(server.running.log())
    cert_path, key_path = self_signed(directory=tmp_path, key=key, index=0, subject_attributes=600)
    print("certificate PEM bytes:", cert_path.stat().st_size)
    try_handshake(port=server.running.port, ca_path=pki.ca_path, cert_path=cert_path, key_path=key_path)
    time.sleep(1)
    grown = len(server.running.log()) - before
    print("log bytes written for one refused handshake:", grown)
    assert grown < 8 * 1024, f"one refused handshake wrote {grown} bytes to the log"


# ---- Finding: a failed CRL reload is not retried ----

def test_a_crl_reload_that_failed_is_retried(launcher, pki, tmp_path):
    """Write a CRL revoking dave while the reload can't succeed (the server key unusable
    for a moment); once it can, the revocation must take effect without touching the CRL."""
    ops, dave = pki.issue(name="ops@example.com"), pki.issue(name="dave@example.com")

    def crl(revoked: list) -> bytes:
        builder = (x509.CertificateRevocationListBuilder().issuer_name(pki.ca.subject)
                   .last_update(NOW - datetime.timedelta(minutes=5)).next_update(NOW + datetime.timedelta(days=1)))
        for serial in revoked:
            builder = builder.add_revoked_certificate(
                x509.RevokedCertificateBuilder().serial_number(serial)
                .revocation_date(NOW - datetime.timedelta(minutes=1)).build())
        return builder.sign(private_key=pki.ca_key, algorithm=hashes.SHA256()).public_bytes(
            encoding=serialization.Encoding.PEM)

    path = tmp_path / "ca.crl"
    path.write_bytes(crl(revoked=[]))
    server = start(launcher=launcher, pki=pki, probe=ops, args=["--mtls-crl-file", str(path)])
    with server.http(identity=dave) as http:
        assert http.get(url="/warp").status_code == 200
    # The server's key unusable for a moment, as mid-rotation (a garbage file, so this
    # holds on Windows too, where a mode of 0 doesn't stop the server reading it).
    key_file = pki.server[1]
    real_key = key_file.read_bytes()
    key_file.write_bytes(b"-----BEGIN PRIVATE KEY-----\nnot a key\n-----END PRIVATE KEY-----\n")
    try:
        path.write_bytes(crl(revoked=[dave[2].serial_number]))
        time.sleep(7)  # one look at the file (every 5 s): the reload fails
        with server.http(identity=ops) as http:  # a new connection makes it look
            http.get(url="/warp")
        time.sleep(1)
    finally:
        key_file.write_bytes(real_key)
    failed = [r for r in server.running.log_records() if r.get("component") == "tls" and r.get("level") in ("error", "ERROR")]
    assert any("reloading the client CRL file" in r.get("message", "") and "tried again" in r.get("message", "")
               for r in failed), [r.get("message") for r in failed]
    time.sleep(7)
    with server.http(identity=ops) as http:
        http.get(url="/warp")
    time.sleep(1)
    refused = False
    try:
        with server.http(identity=dave) as http:
            http.get(url="/warp")
    except (httpx.HTTPError, ssl.SSLError):
        refused = True
    assert refused, "dave's revocation never took effect: the failed reload was not retried"


# ---- Finding (low): a bound scoped token is exchanged without its certificate ----

def test_a_bound_scoped_token_cannot_be_exchanged_over_another_certificate(rbac, pki):
    carol = pki.issue(name="carol@example.com")
    mallory = pki.issue(name="mallory@example.com")  # any certificate the CA issued
    expect_allowed(client=rbac.admin(identity=rbac.ops), sql='CREATE USER "carol@example.com"')
    session = rbac.client(identity=carol, token=rbac.certificate_token(identity=carol))
    scoped = session.run(sql="CREATE SCOPED TOKEN job ALLOW SELECT").to_pylist()[0]["token"]
    with rbac.http(identity=mallory) as http:
        response = http.post(url="/warp/v1/auth/token",
                             json={"grant_type": "password", "username": "token", "password": scoped})
    print("exchange over another certificate:", response.status_code, response.text[:200])
    if response.status_code == 200:
        claims = jwt.decode(jwt=response.json()["access_token"], options={"verify_signature": False})
        print("issued token cnf:", claims.get("cnf"), "expected:", thumbprint_of(identity=carol))
    problem(response=response, status=401, code="certificate_mismatch")


# ---- Probes the third review found already holding ----

def test_the_admin_username_as_a_san_is_not_the_administrator(launcher, pki):
    ops = pki.issue(name="ops@example.com")
    admin_san = pki.issue(name="admin")  # the bootstrap administrator's user name ("admin")
    server = start(launcher=launcher, pki=pki, probe=ops,
                   args=["--mtls-certificate-sign-in", "--mtls-admin-identities", "ops@example.com"])
    problem(response=server.certificate_grant(identity=admin_san), status=401, code="unknown_certificate_identity")
    cn_only = pki.issue(name="cn-only", sans=[x509.DNSName("ops.example.com")])
    problem(response=server.certificate_grant(identity=cn_only), status=401, code="unknown_certificate_identity")


def test_subject_email_attribute_is_not_a_san_email(launcher, pki, tmp_path):
    ops = pki.issue(name="ops@example.com")
    server = start(launcher=launcher, pki=pki, probe=ops,
                   args=["--mtls-certificate-sign-in", "--mtls-admin-identities", "ops@example.com"])
    key = ec.generate_private_key(curve=ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(oid=NameOID.COMMON_NAME, value="x"),
                      x509.NameAttribute(oid=NameOID.EMAIL_ADDRESS, value="ops@example.com")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(pki.ca.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(NOW - datetime.timedelta(minutes=5)).not_valid_after(NOW + datetime.timedelta(days=1))
            .add_extension(extval=x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
            .sign(private_key=pki.ca_key, algorithm=hashes.SHA256()))
    cert_path, key_path = tmp_path / "dn-email.pem", tmp_path / "dn-email.key"
    cert_path.write_bytes(cert.public_bytes(encoding=serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(encoding=serialization.Encoding.PEM,
                                           format=serialization.PrivateFormat.PKCS8,
                                           encryption_algorithm=serialization.NoEncryption()))
    problem(response=server.certificate_grant(identity=(cert_path, key_path, cert)), status=401,
            code="unknown_certificate_identity")


def test_an_email_san_with_an_embedded_nul_is_refused(launcher, pki):
    ops = pki.issue(name="ops@example.com")
    server = start(launcher=launcher, pki=pki, probe=ops,
                   args=["--mtls-certificate-sign-in", "--mtls-admin-identities", "ops@example.com"])
    nul = pki.issue(name="nul", sans=[x509.RFC822Name("ops@example.com\x00.evil.example")])
    body = problem(response=server.certificate_grant(identity=nul), status=401, code="unknown_certificate_identity")
    assert "\\x00" in body["detail"], body


def test_a_cert_sign_in_off_server_authenticates_no_one_with_a_certificate(launcher, pki):
    ops = pki.issue(name="ops@example.com")
    # A client certificate whose SAN is the admin's name, on a server with sign-in off.
    admin_san = pki.issue(name="admin")
    server = start(launcher=launcher, pki=pki, probe=ops)
    for who in (ops, admin_san):
        problem(response=server.certificate_grant(identity=who), status=400, code="unsupported_grant_type")
    # The handshake still requires a valid certificate.
    with pytest.raises((httpx.HTTPError, ssl.SSLError)):
        with server.http(identity=None) as http:
            http.get(url="/warp")


def test_an_identity_mismatch_refusal_record_has_a_bounded_size(launcher, pki):
    """With identity match on, the refusal's detail and user name carry the client's
    `username` in full (the JSON body may be 64 MiB)."""
    ops = pki.issue(name="ops@example.com")
    server = start(launcher=launcher, pki=pki, probe=ops,
                   args=["--mtls-require-identity-match"], env={"HARDCHARGER_AUDIT_CHAIN_KEY": AUDIT_KEY})
    before = len(server.running.log())
    with server.http(identity=ops) as http:
        response = http.post(url="/warp/v1/auth/token",
                             json={"grant_type": "password", "username": "x" * 1_000_000, "password": "y"})
    problem(response=response, status=401, code="certificate_identity_mismatch")
    time.sleep(0.5)
    grown = len(server.running.log()) - before
    print("log bytes for one identity-mismatch refusal:", grown)
    assert grown < 64 * 1024, f"one refusal wrote {grown} bytes"


def test_handshake_refusal_log_volume_from_one_address(launcher, pki, tmp_path):
    """Measures (and bounds) what 100 refused handshakes with large unverified subjects
    write: the per-source limit should hold it to ~10 records."""
    ops = pki.issue(name="ops@example.com")
    server = start(launcher=launcher, pki=pki, probe=ops, env={"HARDCHARGER_AUDIT_CHAIN_KEY": AUDIT_KEY})
    key = ec.generate_private_key(curve=ec.SECP256R1())
    certs = [self_signed(directory=tmp_path, key=key, index=i, subject_attributes=600) for i in range(100)]
    before = len(server.running.log())
    started = time.monotonic()
    for cert_path, key_path in certs:
        try_handshake(port=server.running.port, ca_path=pki.ca_path, cert_path=cert_path, key_path=key_path)
    elapsed = time.monotonic() - started
    time.sleep(1)
    grown = len(server.running.log()) - before
    print(f"100 refused handshakes in {elapsed:.2f}s wrote {grown} log bytes ({grown / elapsed / 1e6:.1f} MB/s)")
    assert grown < 2 * 1024 * 1024, f"{grown} bytes"


def test_a_refused_password_sign_in_logs_a_bounded_user_name(launcher, pki):
    # The name a refused password sign-in logs is the client's, unauthenticated.
    ops = pki.issue(name="ops@example.com")
    server = start(launcher=launcher, pki=pki, probe=ops)
    before = len(server.running.log())
    with server.http(identity=ops) as http:
        response = http.post(url="/warp/v1/auth/token",
                             json={"grant_type": "password", "username": "x" * 1_000_000, "password": "y"})
    problem(response=response, status=401, code="invalid_credentials")
    time.sleep(0.5)
    refused = [r for r in server.running.log_records() if r.get("message") == "sign-in refused"]
    assert refused and refused[-1]["user"].endswith("(1000000 bytes)"), refused[-1:]
    assert len(server.running.log()) - before < 8 * 1024


def test_a_spiffe_id_must_match_its_user_name_exactly(launcher, store, pki):
    # URI identities are case-sensitive (SPIFFE paths are); e-mail ones are not (twin below).
    server = spiffe_rbac(launcher=launcher, store=store, pki=pki)
    expect_allowed(client=server.admin(identity=server.ops), sql=f'CREATE USER "{SPIFFE_ID}"')
    shouty = pki.issue(name="loader-case", sans=[x509.UniformResourceIdentifier(SPIFFE_ID.replace("loader", "Loader"))])
    body = problem(response=server.certificate_grant(identity=shouty), status=401, code="unknown_certificate_identity")
    assert "only in case" in body["detail"] and "must match a user name exactly" in body["detail"], body
    loader = pki.issue(name="loader", sans=[x509.UniformResourceIdentifier(SPIFFE_ID)])
    server.certificate_token(identity=loader)


def test_an_email_identity_matches_its_user_ignoring_case(rbac, pki):
    expect_allowed(client=rbac.admin(identity=rbac.ops), sql='CREATE USER "gina@example.com"')
    rbac.certificate_token(identity=pki.issue(name="GINA@Example.com"))
