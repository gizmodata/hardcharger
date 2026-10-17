"""Port of GizmoSQL 1.x tests/integration/test_cross_instance_tokens.cpp (7 cases).

Access tokens are JWTs signed with the shared secret_key and carry the issuing
instance's id. By default (strict) a token another instance issued is refused; with
allow_cross_instance_tokens (HARDCHARGER_ALLOW_CROSS_INSTANCE_TOKENS) it is accepted, as a
multi-replica cluster needs. The tokens are minted the way 1.x's tests minted them
(iss=hardcharger, sub, role, auth_method, instance_id, session_id).

Strengthened: 1.x accepted any of three phrases for the strict refusal and asserted no
reason for the wrong-secret case; here both assert 401 invalid_token, and the strict one
also its "another server instance" detail. The two ConfigIs* cases read the setting
through the server rather than an in-process getter.
"""

import uuid

import pytest

from conftest import problem
from warp_client import mint_server_token

SECRET = "cross-instance-test-secret-0123456789abcdef"


@pytest.fixture(scope="module")
def strict(module_launcher):
    return module_launcher.start(env={"HARDCHARGER_SECRET_KEY": SECRET})


@pytest.fixture(scope="module")
def relaxed(module_launcher):
    return module_launcher.start(env={"HARDCHARGER_SECRET_KEY": SECRET, "HARDCHARGER_ALLOW_CROSS_INSTANCE_TOKENS": "true"})


def foreign_token(secret: str = SECRET) -> str:
    return mint_server_token(secret_key=secret, subject="admin", role="admin",
                             instance_id="different-instance-12345", session_id=str(uuid.uuid4()))


def open_session(server, token: str):
    return server.client(token=token).open_session()


def test_config_is_strict(strict):
    # The server's own token names its instance; a token naming another is refused.
    assert open_session(server=strict, token=strict.token()).status_code == 201
    assert open_session(server=strict, token=foreign_token()).status_code == 401


def test_strict_valid_token_from_same_instance_works(strict):
    assert strict.client().new_session().scalar(sql="SELECT 1") == 1


def test_token_from_different_instance_rejected(strict):
    body = problem(response=open_session(server=strict, token=foreign_token()), status=401, code="invalid_token")
    assert "another server instance" in body["detail"], body


def test_config_is_relaxed(relaxed):
    assert open_session(server=relaxed, token=foreign_token()).status_code == 201


def test_relaxed_valid_token_from_same_instance_works(relaxed):
    assert relaxed.client().new_session().scalar(sql="SELECT 1") == 1


def test_token_from_different_instance_accepted(relaxed):
    session = relaxed.client(token=foreign_token()).new_session()
    assert session.query(sql="SELECT 42 AS answer").num_rows == 1


def test_invalid_secret_key_still_rejected(relaxed):
    body = problem(response=open_session(server=relaxed, token=foreign_token(secret="the-wrong-secret-0123456789abcdef")),
                   status=401, code="invalid_token")
    assert body["detail"] == "a valid access token is required", body
