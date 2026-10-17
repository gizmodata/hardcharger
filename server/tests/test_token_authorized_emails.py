"""Port of GizmoSQL 1.x tests/integration/test_token_authorized_emails.cpp (7 cases).

token_authorized_emails (--token-authorized-emails, HARDCHARGER_TOKEN_AUTHORIZED_EMAILS):
which users a trusted issuer's sign-in token may name, as a comma list of
case-insensitive globs. A token naming anyone else is refused with 401
invalid_credentials and "User '<email>' is not authorized. Contact your administrator."
(1.x's text). Password sign-in is unaffected. 1.x compiled this into Enterprise builds
only and skipped the cases elsewhere; Hardcharger has one build, so all 7 run.
"""

import datetime

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from conftest import problem

ISSUER = "test-email-issuer"
AUDIENCE = "test-email-audience"


@pytest.fixture(scope="module")
def signing_key(tmp_path_factory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path_factory.mktemp("emails") / "verify.pem"
    pem.write_bytes(key.public_key().public_bytes(encoding=serialization.Encoding.PEM,
                                                  format=serialization.PublicFormat.SubjectPublicKeyInfo))
    return key, str(pem)


def start(module_launcher, signing_key, emails: str):
    return module_launcher.start(env={"HARDCHARGER_TOKEN_SIGNATURE_VERIFY_CERT_PATH": signing_key[1], "HARDCHARGER_TOKEN_ALLOWED_ISSUER": ISSUER,
                                      "HARDCHARGER_TOKEN_ALLOWED_AUDIENCE": AUDIENCE, "HARDCHARGER_TOKEN_AUTHORIZED_EMAILS": emails})


@pytest.fixture(scope="module")
def domain_server(module_launcher, signing_key):
    return start(module_launcher=module_launcher, signing_key=signing_key, emails="*@allowed.com")


@pytest.fixture(scope="module")
def multi_server(module_launcher, signing_key):
    return start(module_launcher=module_launcher, signing_key=signing_key, emails="admin@external.com,*@allowed.com")


def exchange(server, signing_key, email: str) -> httpx.Response:
    now = datetime.datetime.now(tz=datetime.UTC)
    token = jwt.encode(payload={"iss": ISSUER, "aud": AUDIENCE, "sub": email, "email": email, "role": "admin",
                                "jti": f"test-token-{email}", "iat": now, "exp": now + datetime.timedelta(hours=1)},
                       key=signing_key[0], algorithm="RS256")
    return httpx.post(url=f"{server.base_url}/warp/v1/auth/token",
                      json={"grant_type": "token_exchange", "subject_token": token})


def accepted(server, signing_key, email: str) -> None:
    response = exchange(server=server, signing_key=signing_key, email=email)
    assert response.status_code == 200, response.text
    session = server.client(token=response.json()["access_token"]).new_session()
    assert session.scalar(sql="SELECT HARDCHARGER_USER()") == email


def rejected(server, signing_key, email: str) -> None:
    body = problem(response=exchange(server=server, signing_key=signing_key, email=email), status=401,
                   code="invalid_credentials")
    assert body["detail"] == f"User '{email}' is not authorized. Contact your administrator.", body


def test_matching_domain_accepted(domain_server, signing_key):
    accepted(server=domain_server, signing_key=signing_key, email="user@allowed.com")


def test_non_matching_domain_rejected(domain_server, signing_key):
    rejected(server=domain_server, signing_key=signing_key, email="user@other.com")


def test_case_insensitive_match(domain_server, signing_key):
    accepted(server=domain_server, signing_key=signing_key, email="User@ALLOWED.COM")


def test_exact_email_match_accepted(multi_server, signing_key):
    accepted(server=multi_server, signing_key=signing_key, email="admin@external.com")


def test_wildcard_pattern_accepted(multi_server, signing_key):
    accepted(server=multi_server, signing_key=signing_key, email="someone@allowed.com")


def test_non_matching_email_rejected(multi_server, signing_key):
    rejected(server=multi_server, signing_key=signing_key, email="other@external.com")


def test_basic_auth_still_works(multi_server):
    assert multi_server.admin().scalar(sql="SELECT 1") == 1
