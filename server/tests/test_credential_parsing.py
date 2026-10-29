"""Port of GizmoSQL 1.x tests/integration/test_credential_parsing.cpp (5 cases; 4 apply).

A password containing ':' signs in, and nothing that is a prefix or an extension of it
does. Not applicable: UsernameWithColonIsRejectedAtStartup. 1.x split Basic-auth
"user:password" on ':', so a ':' in the user name had to be refused; Chargewire sends user
name and password as separate JSON fields and splits nothing.
"""

import httpx
import pytest

from conftest import problem


def sign_in(server, password: str) -> httpx.Response:
    return httpx.post(url=f"{server.base_url}/chargewire/v1/auth/token",
                      json={"grant_type": "password", "username": server.username, "password": password})


@pytest.fixture(scope="module")
def colon_server(module_launcher):
    return module_launcher.start(password="pa:ss:word")


@pytest.fixture(scope="module")
def plain_server(module_launcher):
    return module_launcher.start(password="secret")


def test_password_with_colons_authenticates(colon_server):
    assert colon_server.client(token=colon_server.token(password="pa:ss:word")).new_session().scalar(sql="SELECT 1") == 1


@pytest.mark.parametrize("wrong", ["pa", "pa:ss", "pa:ss:word:", "pa:ss:word:extra"])
def test_prefix_or_extended_password_is_rejected(colon_server, wrong):
    problem(response=sign_in(server=colon_server, password=wrong), status=401, code="invalid_credentials")


def test_correct_password_authenticates(plain_server):
    assert sign_in(server=plain_server, password="secret").status_code == 200


def test_password_with_appended_colon_suffix_is_rejected(plain_server):
    problem(response=sign_in(server=plain_server, password="secret:anything"), status=401,
            code="invalid_credentials")
