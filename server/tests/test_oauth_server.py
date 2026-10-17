"""Port of GizmoSQL 1.x tests/integration/test_oauth_server.cpp (9 cases: 8, and a
non-Enterprise stub).

Browser sign-in (--oauth-client-id and friends, the
HARDCHARGER_OAUTH_* variables) against a mock OpenID Connect provider (mock_idp.py), on
1.x's routes: /oauth/initiate, /oauth/start, /oauth/callback, /oauth/token/<uuid>. The
ID token the flow yields signs in through token_exchange (and as the password of user
"token"), verified with the provider's JWKS. Not applicable: the stub, which only
skipped on builds without Enterprise code (Hardcharger has one build, with no license:
see test_needs_no_license).

Strengthened: a completed flow's token is handed out once; the provider's error text is
escaped in the page (1.x put it in the HTML as is); the oauth_instance_id state prefix;
the code exchange sends the client credentials and redirect URI; the signed-in user is
the token's email.
"""

import hashlib
import hmac
import uuid as uuid_module

import httpx
import jwt
import pytest

from conftest import free_port
from mock_idp import MockIdP
from warp_client import Client

AUDIENCE = "hardcharger-oauth-test"
CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"


@pytest.fixture(scope="module")
def idp():
    provider = MockIdP(audience=AUDIENCE, client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    yield provider
    provider.stop()


def oauth_args(idp, port: int, *extra: str) -> list[str]:
    return ["--token-allowed-issuer", idp.issuer, "--token-allowed-audience", AUDIENCE,
            "--token-default-role", "admin", "--token-authorized-emails", "*@gizmodata.com",
            "--oauth-client-id", CLIENT_ID, "--oauth-client-secret", CLIENT_SECRET,
            "--oauth-port", str(port), *extra]


@pytest.fixture(scope="module")
def server(module_launcher, idp):
    port = free_port()
    running = module_launcher.start(args=oauth_args(idp, port))
    running.oauth = httpx.Client(base_url=f"http://127.0.0.1:{port}", follow_redirects=False, timeout=30)
    return running


def state_of(auth_url: str) -> str:
    return httpx.URL(auth_url).params["state"]


def test_successful_oauth_flow(server, idp):
    initiate = server.oauth.get(url="/oauth/initiate")
    assert initiate.status_code == 200, initiate.text
    flow = initiate.json()
    auth_url = httpx.URL(flow["auth_url"])
    assert auth_url.path == "/authorize" and str(auth_url).startswith(idp.issuer)
    assert auth_url.params["client_id"] == CLIENT_ID and auth_url.params["response_type"] == "code"
    redirect_uri = auth_url.params["redirect_uri"]
    assert redirect_uri == f"http://localhost:{server.oauth.base_url.port}/oauth/callback"

    idp.valid_code = "test-auth-code-123"
    idp.email = "testuser@gizmodata.com"
    callback = server.oauth.get(url="/oauth/callback", params={"code": "test-auth-code-123",
                                                               "state": state_of(flow["auth_url"])})
    assert callback.status_code == 200 and "Authentication Successful" in callback.text, callback.text
    exchange = idp.token_requests[-1]
    assert exchange == {"grant_type": "authorization_code", "code": "test-auth-code-123",
                        "redirect_uri": redirect_uri, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}

    poll = server.oauth.get(url=f"/oauth/token/{flow['session_uuid']}")
    assert poll.status_code == 200 and poll.json()["status"] == "complete", poll.text
    id_token = poll.json()["token"]
    # The provider's own ID token, not one of the server's.
    claims = jwt.decode(jwt=id_token, options={"verify_signature": False})
    assert claims["iss"] == idp.issuer and claims["email"] == "testuser@gizmodata.com"
    # Handed out once.
    again = server.oauth.get(url=f"/oauth/token/{flow['session_uuid']}")
    assert again.status_code == 404 and again.json()["status"] == "not_found"

    # It signs in: token_exchange, and as the password of user "token" (1.x drivers).
    for body in ({"grant_type": "token_exchange", "subject_token": id_token},
                 {"grant_type": "password", "username": "token", "password": id_token}):
        response = httpx.post(url=f"{server.base_url}/warp/v1/auth/token", json=body)
        assert response.status_code == 200, response.text
        session = Client(base_url=server.base_url, token=response.json()["access_token"]).new_session()
        assert session.scalar(sql="SELECT 1 AS result") == 1
        assert session.scalar(sql="SELECT HARDCHARGER_USER()") == "testuser@gizmodata.com"


def test_initiate_endpoint_returns_valid_json(server):
    response = server.oauth.get(url="/oauth/initiate")
    assert response.status_code == 200
    flow = response.json()
    uuid = flow["session_uuid"]
    assert len(uuid) == 36 and [uuid[i] for i in (8, 13, 18, 23)] == ["-"] * 4
    params = httpx.URL(flow["auth_url"]).params
    assert {"response_type", "client_id", "redirect_uri", "state", "scope"} <= set(params.keys())
    assert params["scope"] == "openid profile email"
    # The state is HMAC-SHA256(secret_key, uuid), as clients that start their own flow compute it.
    assert params["state"] == hmac_hex(key=server.secret_key, data=uuid)
    poll = server.oauth.get(url=f"/oauth/token/{uuid}")
    assert poll.status_code == 200 and poll.json() == {"status": "pending"}


def hmac_hex(key: str, data: str) -> str:
    return hmac.new(key=key.encode(), msg=data.encode(), digestmod=hashlib.sha256).hexdigest()


def start_flow(server) -> tuple[str, str]:
    uuid = str(uuid_module.uuid4())
    session_hash = hmac_hex(key=server.secret_key, data=uuid)
    start = server.oauth.get(url="/oauth/start", params={"session": session_hash})
    assert start.status_code == 302, start.text
    assert state_of(start.headers["location"]) == session_hash
    return uuid, session_hash


def test_invalid_authorization_code(server, idp):
    uuid, session_hash = start_flow(server=server)
    callback = server.oauth.get(url="/oauth/callback", params={"code": "bad-code", "state": session_hash})
    assert "Authentication Failed" in callback.text and "invalid_grant" in callback.text, callback.text
    poll = server.oauth.get(url=f"/oauth/token/{uuid}").json()
    assert poll["status"] == "error" and "invalid_grant: the code is not valid" in poll["error"], poll


def test_unauthorized_email(server, idp):
    uuid, session_hash = start_flow(server=server)
    idp.email = "unauthorized@evil.com"
    idp.valid_code = "unauth-code-456"
    try:
        callback = server.oauth.get(url="/oauth/callback", params={"code": "unauth-code-456", "state": session_hash})
    finally:
        idp.email = "testuser@gizmodata.com"
    assert "Authentication Failed" in callback.text and "unauthorized@evil.com" in callback.text, callback.text
    poll = server.oauth.get(url=f"/oauth/token/{uuid}").json()
    assert poll["status"] == "error" and "is not authorized" in poll["error"], poll
    # And the provider's token for that user doesn't sign in either.
    token = idp.id_token(email="unauthorized@evil.com")
    response = httpx.post(url=f"{server.base_url}/warp/v1/auth/token",
                          json={"grant_type": "token_exchange", "subject_token": token})
    assert response.status_code == 401 and "is not authorized" in response.json()["detail"], response.text


def test_token_poll_not_found(server):
    response = server.oauth.get(url=f"/oauth/token/{uuid_module.uuid4()}")
    assert response.status_code == 404 and response.json() == {"status": "not_found"}


def test_pending_session_poll(server):
    uuid, _ = start_flow(server=server)
    response = server.oauth.get(url=f"/oauth/token/{uuid}")
    assert response.status_code == 200 and response.json() == {"status": "pending"}


def test_duplicate_session_rejected(server):
    _, session_hash = start_flow(server=server)
    again = server.oauth.get(url="/oauth/start", params={"session": session_hash})
    assert again.status_code == 409 and "Session already in progress" in again.text


def test_missing_session_parameter(server):
    response = server.oauth.get(url="/oauth/start")
    assert response.status_code == 400 and "Missing session parameter" in response.text


def test_provider_error_is_escaped(server):
    uuid, session_hash = start_flow(server=server)
    response = server.oauth.get(url="/oauth/callback", params={
        "error": "access_denied", "error_description": "<script>alert(1)</script>", "state": session_hash})
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert server.oauth.get(url=f"/oauth/token/{uuid}").json()["status"] == "error"


def test_instance_id_prefixes_the_state(launcher, idp):
    port = free_port()
    server = launcher.start(args=oauth_args(idp, port, "--oauth-instance-id", "replica-7",
                                            "--oauth-base-url", "https://sign-in.example.test"))
    oauth = httpx.Client(base_url=f"http://127.0.0.1:{port}", follow_redirects=False)
    flow = oauth.get(url="/oauth/initiate").json()
    params = httpx.URL(flow["auth_url"]).params
    assert params["state"] == "replica-7." + hmac_hex(key=server.secret_key, data=flow["session_uuid"])
    assert params["redirect_uri"] == "https://sign-in.example.test/oauth/callback"
    idp.valid_code = "prefixed-code"
    callback = oauth.get(url="/oauth/callback", params={"code": "prefixed-code", "state": params["state"]})
    assert "Authentication Successful" in callback.text, callback.text
    assert oauth.get(url=f"/oauth/token/{flow['session_uuid']}").json()["status"] == "complete"


def test_needs_no_license(launcher, idp):
    # 1.x refused the OAuth server without its license feature; Hardcharger has no license.
    port = free_port()
    server = launcher.start(args=oauth_args(idp, port))
    response = httpx.get(url=f"http://127.0.0.1:{port}/oauth/initiate")
    assert response.status_code == 200 and "session_uuid" in response.json(), server.log()


def exchange(server, token: str) -> httpx.Response:
    return httpx.post(url=f"{server.base_url}/warp/v1/auth/token",
                      json={"grant_type": "token_exchange", "subject_token": token})


def test_jwks_verifies_provider_tokens(server, idp):
    assert idp.jwks_requests > 0, "the server never read the provider's JWKS"
    assert exchange(server=server, token=idp.id_token(email="a@gizmodata.com")).status_code == 200
    # Signed by a key the provider doesn't publish, under its kid or another.
    from cryptography.hazmat.primitives.asymmetric import rsa
    stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for token in (idp.id_token(email="a@gizmodata.com", key=stranger),
                  idp.id_token(email="a@gizmodata.com", kid="unknown-kid", key=stranger)):
        response = exchange(server=server, token=token)
        assert response.status_code == 401 and response.json()["code"] == "invalid_credentials", response.text
    # No kid: JWKS can't say which key, so it is refused.
    no_kid = jwt.encode(payload=jwt.decode(jwt=idp.id_token(email="a@gizmodata.com"),
                                           options={"verify_signature": False}),
                        key=idp.key, algorithm="RS256")
    assert exchange(server=server, token=no_kid).status_code == 401
    # Another audience or issuer.
    for claims in ({"aud": "someone-else"}, {"iss": "http://127.0.0.1:1"}):
        assert exchange(server=server, token=idp.id_token(email="a@gizmodata.com", **claims)).status_code == 401
    # The discovery document advertises token_exchange.
    assert "token_exchange" in httpx.get(url=f"{server.base_url}/warp").json()["auth"]["grant_types"]


def test_jwks_with_ec_keys(launcher):
    provider = MockIdP(audience=AUDIENCE, algorithm="ES256")
    try:
        server = launcher.start(args=["--token-allowed-issuer", provider.issuer, "--token-allowed-audience", AUDIENCE,
                                      "--token-default-role", "user"])
        response = exchange(server=server, token=provider.id_token(email="ec@gizmodata.com"))
        assert response.status_code == 200, response.text
        session = Client(base_url=server.base_url, token=response.json()["access_token"]).new_session()
        assert session.scalar(sql="SELECT HARDCHARGER_ROLE()") == "user"
    finally:
        provider.stop()


def test_jwks_sign_in_needs_no_license(launcher, idp):
    # token_jwks_uri set explicitly. 1.x refused these tokens without its license feature;
    # Hardcharger accepts them.
    server = launcher.start(args=["--token-allowed-issuer", idp.issuer, "--token-allowed-audience", AUDIENCE,
                                  "--token-default-role", "admin", "--token-jwks-uri", f"{idp.issuer}/jwks"])
    response = exchange(server=server, token=idp.id_token(email="a@gizmodata.com"))
    assert response.status_code == 200, response.text
