"""Port of GizmoSQL 1.x tests/integration/test_authentication.cpp (30 cases).

Sign-in (Warp SQL §4.1: POST /warp/v1/auth/token, grant_type password) and bearer tokens
(§4.2). 1.x asserted a reason in only 3 of its 30 cases; here every rejection asserts
the exact problem code (401 invalid_credentials for sign-in, 401 invalid_token for a
bad bearer token, with WWW-Authenticate: Bearer), so a case can't pass for the wrong
reason. All 30 cases apply.
"""

import datetime
import json
import socket

import httpx
import jwt
import pytest

from conftest import problem

USERNAME = "validuser"
PASSWORD = "validpassword123"


@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start(username=USERNAME, password=PASSWORD)


def sign_in(server, username: str, password: str) -> httpx.Response:
    return httpx.post(url=f"{server.base_url}/warp/v1/auth/token",
                      json={"grant_type": "password", "username": username, "password": password})


def refused(server, username: str, password: str) -> None:
    body = problem(response=sign_in(server=server, username=username, password=password), status=401,
                   code="invalid_credentials")
    # One answer for every wrong credential: it never says which part was wrong.
    assert body["detail"] == "the user name or password is not correct", body


def open_session(server, authorization: str | None) -> httpx.Response:
    headers = {} if authorization is None else {"Authorization": authorization}
    return httpx.post(url=f"{server.base_url}/warp/v1/sessions", headers=headers, json={})


def token_refused(server, authorization: str | None) -> None:
    response = open_session(server=server, authorization=authorization)
    problem(response=response, status=401, code="invalid_token")
    assert response.headers.get("www-authenticate", "").startswith("Bearer"), response.headers


# ---- Valid credentials ----

def test_valid_credentials_authenticate(server):
    response = sign_in(server=server, username=USERNAME, password=PASSWORD)
    assert response.status_code == 200, response.text
    assert response.json()["token_type"] == "Bearer"


def test_valid_credentials_can_execute_query(server):
    session = server.client(token=server.token()).new_session()
    assert session.scalar(sql="SELECT 1") == 1


# ---- Wrong passwords and user names ----

@pytest.mark.parametrize("username,password", [
    (USERNAME, "wrongpassword"),                 # WrongPasswordRejected
    (USERNAME, "validpassword12"),               # SimilarPasswordRejected
    (USERNAME, "VALIDPASSWORD123"),              # CaseSensitivePassword
    ("wronguser", PASSWORD),                     # WrongUsernameRejected
    ("VALIDUSER", PASSWORD),                     # CaseSensitiveUsername
    ("validuse", PASSWORD),                      # SimilarUsernameRejected
    ("", PASSWORD),                              # BlankUsernameRejected
    (USERNAME, ""),                              # BlankPasswordRejected
    ("", ""),                                    # BothBlankRejected
    ("wronguser", "wrongpassword"),              # BothWrongRejected
    ("' OR '1'='1", PASSWORD),                   # SQLInjectionInUsernameRejected
    (USERNAME, "' OR '1'='1"),                   # SQLInjectionInPasswordRejected
    ("valid\x00user", PASSWORD),                 # NullByteInUsernameRejected
    ("   ", PASSWORD),                           # WhitespaceOnlyUsernameRejected
    (USERNAME, "   "),                           # WhitespaceOnlyPasswordRejected
    ("üsér", PASSWORD),                          # UnicodeUsernameRejected
    ("user\U0001F600", "pass\U0001F600"),        # EmojiInCredentialsRejected
    ("u" * 10240, PASSWORD),                     # VeryLongUsernameRejected
    (USERNAME, "p" * 10240),                     # VeryLongPasswordRejected
], ids=["wrong_password", "similar_password", "case_sensitive_password", "wrong_username",
        "case_sensitive_username", "similar_username", "blank_username", "blank_password", "both_blank",
        "both_wrong", "sql_injection_username", "sql_injection_password", "nul_in_username",
        "whitespace_username", "whitespace_password", "unicode_username", "emoji", "long_username",
        "long_password"])
def test_wrong_credentials_are_refused(server, username, password):
    refused(server=server, username=username, password=password)


# ---- Bearer tokens ----

def test_unauthenticated_query_rejected(server):
    token_refused(server=server, authorization=None)


def test_forged_bearer_token_rejected(server):
    token_refused(server=server, authorization="Bearer forged.token.value")


def test_random_jwt_rejected(server):
    now = datetime.datetime.now(tz=datetime.UTC)
    unsigned = jwt.encode(payload={"iss": "hardcharger", "sub": USERNAME, "role": "admin", "iat": now,
                                   "exp": now + datetime.timedelta(hours=1)}, key="not-the-server-key-at-all-0123456789",
                          algorithm="HS256")
    token_refused(server=server, authorization=f"Bearer {unsigned}")


def test_empty_bearer_token_rejected(server):
    # httpx won't send a header value with a trailing space, so this one goes raw.
    with socket.create_connection(address=("127.0.0.1", server.port), timeout=10) as sock:
        sock.sendall(b"POST /warp/v1/sessions HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer \r\n"
                     b"Content-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
        reply = b""
        while chunk := sock.recv(65536):
            reply += chunk
    head, _, body = reply.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 401"), reply
    assert b"www-authenticate: bearer" in head.lower(), head
    assert json.loads(body)["code"] == "invalid_token", body


def test_garbage_authorization_header_rejected(server):
    token_refused(server=server, authorization="garbage-not-a-scheme")


def test_modified_token_rejected(server):
    token_refused(server=server, authorization=f"Bearer {server.token()}MODIFIED")


def test_truncated_token_rejected(server):
    token = server.token()
    token_refused(server=server, authorization=f"Bearer {token[:len(token) // 2]}")


# ---- Behaviour over several requests ----

def test_multiple_failed_attempts_still_reject_then_allow(server):
    for _ in range(5):
        refused(server=server, username=USERNAME, password="wrongpassword")
    # No lockout: the right password still works.
    assert sign_in(server=server, username=USERNAME, password=PASSWORD).status_code == 200


def test_token_remains_valid_for_multiple_queries(server):
    session = server.client(token=server.token()).new_session()
    for i in range(5):
        assert session.scalar(sql=f"SELECT {i}") == i
