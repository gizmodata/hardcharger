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

from conftest import BINARY, CLIENT_BINARY, clean_environment, problem
from rbac import Client, WarpError, expect_allowed, expect_blocked, expect_error, rbac_env

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
    server = start(launcher=launcher, pki=pki, probe=ops, args=["--mtls-admin-identities", "OPS@example.com"])
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
    server = start(launcher=launcher, pki=pki, probe=ops, args=["--mtls-admin-identities", "ops@example.com"])
    body = problem(response=server.certificate_grant(identity=stranger), status=401,
                   code="unknown_certificate_identity")
    assert "stranger@example.com" in body["detail"]
    # Not offered at all without an identity that could use it.
    plain = start(launcher=launcher, pki=pki, probe=ops)
    with plain.http(identity=ops) as http:
        assert "client_certificate" not in http.get(url="/warp").json()["auth"]["grant_types"]
        problem(response=http.post(url="/warp/v1/auth/token", json={"grant_type": "client_certificate"}),
                status=400, code="bad_request")
    with server.http(identity=ops) as http:
        assert "client_certificate" in http.get(url="/warp").json()["auth"]["grant_types"]


def test_an_admin_identity_is_a_name_not_a_pattern(launcher, pki):
    ops, other = pki.issue(name="ops@example.com"), pki.issue(name="eve@example.com")
    server = start(launcher=launcher, pki=pki, probe=ops, args=["--mtls-admin-identities", "*@example.com"])
    problem(response=server.certificate_grant(identity=other), status=401, code="unknown_certificate_identity")
    problem(response=server.certificate_grant(identity=ops), status=401, code="unknown_certificate_identity")


def test_a_bound_token_minted_with_a_broken_cnf_is_refused(launcher, pki):
    # A token with the server's own signature (the tests know its key) whose cnf can't
    # be read binds to nothing the server can check: refused, never treated as unbound.
    ops = pki.issue(name="ops@example.com")
    server = start(launcher=launcher, pki=pki, probe=ops, args=["--mtls-admin-identities", "ops@example.com"])
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
                   args=["--mtls-admin-identities", "ops@example.com"],
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
    other = start(launcher=launcher, pki=pki, probe=nina, args=["--mtls-admin-identities", "nina@example.com"],
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
    denied = [r for r in records if r.get("status") == "denied"]
    assert len(allowed) == 1 and len(denied) == 1, records
    der = carol[2].public_bytes(encoding=serialization.Encoding.DER)
    assert allowed[0]["method"] == "client_certificate"
    assert allowed[0]["user"] == "carol@example.com" and allowed[0]["role"] == "user"
    assert allowed[0]["certificate_thumbprint"] == (
        base64.urlsafe_b64encode(hashlib.sha256(der).digest()).rstrip(b"=").decode())
    assert "CN=carol@example.com" in allowed[0]["certificate_subject"]
    assert denied[0]["method"] == "client_certificate"
    assert denied[0]["certificate_identity"] == "stranger@example.com"
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
    server = start(launcher=launcher, pki=pki, probe=ops, args=["--mtls-admin-identities", "ops@example.com"])
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
    server = start(launcher=launcher, pki=pki, probe=ops, args=["--mtls-admin-identities", "ops@example.com"])
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
