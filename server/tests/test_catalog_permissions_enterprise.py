"""Port of GizmoSQL 1.x tests/integration/test_catalog_permissions_enterprise.cpp (8 cases).

Sign-in tokens from a trusted issuer (RS256) may carry catalog_access rules
([{"catalog": <name or glob>, "access": "read" | "write" | "none"}]). 1.x refused such a
token without an Enterprise license; Hardcharger has no license, so every token signs
in and its rules are enforced (the 1.x "requires enterprise" cases check that it signs
in).

Enforcement is access control's plan check, per catalog (test_catalog_access.py has
the 42 cases of catalog_access.cpp); a refusal says "Access denied: You do not have
write access to catalog ...".
"""

import datetime

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "test-idp"
AUDIENCE = "hardcharger-test"


@pytest.fixture(scope="module")
def key(tmp_path_factory):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path_factory.mktemp("catalog_permissions") / "verify.pem"
    pem.write_bytes(private.public_key().public_bytes(encoding=serialization.Encoding.PEM,
                                                      format=serialization.PublicFormat.SubjectPublicKeyInfo))
    return private, str(pem)


@pytest.fixture(scope="module")
def server(module_launcher, key):
    return module_launcher.start(env={"HARDCHARGER_TOKEN_SIGNATURE_VERIFY_CERT_PATH": key[1],
                                      "HARDCHARGER_TOKEN_ALLOWED_ISSUER": ISSUER,
                                      "HARDCHARGER_TOKEN_ALLOWED_AUDIENCE": AUDIENCE})


def exchange(server, key, user: str, role: str, catalog_access=None) -> httpx.Response:
    now = datetime.datetime.now(tz=datetime.UTC)
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": user, "role": role, "iat": now,
              "exp": now + datetime.timedelta(hours=1)}
    if catalog_access is not None:
        claims["catalog_access"] = catalog_access
    return httpx.post(url=f"{server.base_url}/chargewire/v1/auth/token",
                      json={"grant_type": "token_exchange", "subject_token": jwt.encode(payload=claims, key=key[0],
                                                                                        algorithm="RS256")})


def gated(server, response: httpx.Response) -> bool:
    """A token with catalog_access rules signs in (no license needed)."""
    assert response.status_code == 200, response.text
    return True


def session_for(server, response: httpx.Response):
    return server.client(token=response.json()["access_token"]).new_session()


def test_bootstrap_token_without_catalog_access_succeeds(server, key):
    response = exchange(server=server, key=key, user="test_user", role="user")
    assert response.status_code == 200, response.text


def test_bootstrap_token_with_catalog_access_signs_in(server, key):
    gated(server=server, response=exchange(server=server, key=key, user="test_user", role="user",
                                           catalog_access=[{"catalog": "*", "access": "read"}]))


def test_bootstrap_token_with_write_catalog_access_signs_in(server, key):
    gated(server=server, response=exchange(server=server, key=key, user="test_user", role="admin",
                                           catalog_access=[{"catalog": "main", "access": "write"}]))


def test_bootstrap_token_with_multiple_catalog_rules_signs_in(server, key):
    rules = [{"catalog": "production", "access": "read"}, {"catalog": "staging", "access": "write"},
             {"catalog": "*", "access": "none"}]
    gated(server=server, response=exchange(server=server, key=key, user="data_analyst", role="user",
                                           catalog_access=rules))


def test_catalog_access_read_only_can_execute_select(server, key):
    response = exchange(server=server, key=key, user="readonly_user", role="user",
                        catalog_access=[{"catalog": "*", "access": "read"}])
    if gated(server=server, response=response):
        assert session_for(server=server, response=response).scalar(sql="SELECT 1 AS test_value") == 1


def test_catalog_access_write_can_execute_ddl(server, key):
    response = exchange(server=server, key=key, user="write_user", role="user",
                        catalog_access=[{"catalog": "*", "access": "write"}])
    if gated(server=server, response=response):
        session = session_for(server=server, response=response)
        session.execute(sql="CREATE TABLE IF NOT EXISTS test_catalog_write (id INTEGER, name VARCHAR)")
        session.execute(sql="INSERT INTO test_catalog_write VALUES (1, 'test')")
        assert session.scalar(sql="SELECT count(*) FROM test_catalog_write") >= 1


def test_catalog_access_read_only_blocks_write(server, key):
    response = exchange(server=server, key=key, user="readonly_user", role="user",
                        catalog_access=[{"catalog": "*", "access": "read"}])
    if gated(server=server, response=response):
        problem = session_for(server=server, response=response).error(
            sql="CREATE TABLE should_fail_catalog_read (id INTEGER)")
        assert problem["code"] == "permission_denied", problem


def test_catalog_access_rejection_has_helpful_message(server, key):
    response = exchange(server=server, key=key, user="readonly_user", role="user",
                        catalog_access=[{"catalog": "*", "access": "read"}])
    if gated(server=server, response=response):
        server.admin().execute(sql="CREATE TABLE IF NOT EXISTS helpful_x (id INTEGER)")
        detail = session_for(server=server, response=response).error(sql="INSERT INTO helpful_x VALUES (1)")["detail"]
        assert "Access denied" in detail and "write access" in detail, detail
