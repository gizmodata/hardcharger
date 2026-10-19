"""Browser sign-in (Warp SQL §4.3, capability oauth_browser) against a mock OpenID Connect
provider (mock_idp.py). Port of GizmoSQL 1.x tests/integration/test_oauth_server.cpp (9
cases: 8, and a non-Enterprise stub), moved from 1.x's side port and routes to the
protocol's flows on the main port: POST /warp/v1/auth/oauth/flows, the callback
GET /warp/v1/auth/oauth/callback, and polls with the poll secret. The provider's ID token
never reaches the client: the flow hands out Hardcharger's own access token, once. 1.x's
cases map to: a successful flow; the flow document; an invalid code (the provider refuses
it); an unauthorized e-mail; polling an unknown flow; a pending flow; a duplicate callback
(1.x: a duplicate session hash); a missing state (1.x: a missing session parameter). Not
applicable: the stub, which only skipped on builds without Enterprise code (Hardcharger has
one build, with no license: see test_needs_no_license).

Strengthened: PKCE and nonce; the poll secret required (and never in a URL); the
provider's error text escaped; return_to redirects with the flow id in the fragment; the
oauth_instance_id state prefix and an absolute return redirect on oauth_base_url; the code
exchange sends the client credentials, redirect URI and PKCE verifier; the signed-in user is
the token's email.
"""

import base64
import urllib.parse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from conftest import free_port
from mock_idp import MockIdP
from warp_client import Client

AUDIENCE = "hardcharger-oauth-test"
CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"
FLOWS = "/warp/v1/auth/oauth/flows"


@pytest.fixture(scope="module")
def idp():
    provider = MockIdP(audience=AUDIENCE, client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    yield provider
    provider.stop()


@pytest.fixture(autouse=True)
def reset_idp(idp):
    idp.email, idp.deny, idp.nonce_override = "testuser@gizmodata.com", False, None
    yield


def oauth_args(idp, *extra: str) -> list[str]:
    return ["--token-allowed-issuer", idp.issuer, "--token-allowed-audience", AUDIENCE,
            "--token-default-role", "admin", "--token-authorized-emails", "*@gizmodata.com",
            "--oauth-client-id", CLIENT_ID, *extra]


@pytest.fixture(scope="module")
def server(module_launcher, idp):
    port = free_port()
    running = module_launcher.start(args=oauth_args(idp, "--oauth-base-url", f"http://127.0.0.1:{port}",
                                                    "--oauth-provider-name", "Mock IdP"),
                                    env={"HARDCHARGER_OAUTH_CLIENT_SECRET": CLIENT_SECRET}, port=port)
    running.http = httpx.Client(base_url=running.base_url, follow_redirects=False, timeout=30)
    return running


def create(server, body: dict | None = None) -> dict:
    response = server.http.post(url=FLOWS, json=body if body is not None else {})
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def callback_url(flow: dict) -> str:
    """What the browser is sent to after signing in: the provider's redirect."""
    at_idp = httpx.get(url=flow["authorize_url"], follow_redirects=False)
    assert at_idp.status_code == 302, at_idp.text
    return at_idp.headers["location"]


def browse(server, flow: dict) -> httpx.Response:
    return server.http.get(url=callback_url(flow=flow))


def poll(server, flow: dict, secret: str | None = None) -> httpx.Response:
    return server.http.post(url=f"{FLOWS}/{flow['flow_id']}",
                            json={"poll_secret": flow["poll_secret"] if secret is None else secret})


def test_successful_oauth_flow(server, idp):
    flow = create(server=server)
    authorize = httpx.URL(flow["authorize_url"])
    assert authorize.path == "/authorize" and str(authorize).startswith(idp.issuer)
    params = authorize.params
    assert params["client_id"] == CLIENT_ID and params["response_type"] == "code"
    redirect_uri = params["redirect_uri"]
    assert redirect_uri == f"{server.base_url}/warp/v1/auth/oauth/callback"

    page = browse(server=server, flow=flow)
    assert page.status_code == 200 and "Signed in" in page.text, page.text
    assert "testuser@gizmodata.com" in page.text
    exchange = idp.token_requests[-1]
    assert {key: exchange[key] for key in ("grant_type", "redirect_uri", "client_id", "client_secret")} == {
        "grant_type": "authorization_code", "redirect_uri": redirect_uri, "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET}
    # PKCE: the verifier the server kept matches the challenge it sent.
    import hashlib
    challenge = base64.urlsafe_b64encode(hashlib.sha256(exchange["code_verifier"].encode()).digest()).rstrip(b"=")
    assert challenge.decode() == params["code_challenge"] and params["code_challenge_method"] == "S256"

    done = poll(server=server, flow=flow)
    assert done.status_code == 200 and done.json()["state"] == "complete", done.text
    token = done.json()["access_token"]
    # Hardcharger's own token, not the provider's ID token.
    claims = jwt.decode(jwt=token, options={"verify_signature": False})
    assert claims.get("iss") != idp.issuer and claims.get("auth_method") == "oauth_browser", claims
    session = Client(base_url=server.base_url, token=token).new_session()
    assert session.scalar(sql="SELECT 1 AS result") == 1
    assert session.scalar(sql="SELECT HARDCHARGER_USER()") == "testuser@gizmodata.com"
    # Handed out once.
    again = poll(server=server, flow=flow)
    assert again.status_code == 404 and again.json()["code"] == "flow_not_found"


def test_flow_document(server):
    flow = create(server=server)
    assert set(flow) == {"flow_id", "poll_secret", "authorize_url", "poll_interval_s", "expires_at"}
    params = httpx.URL(flow["authorize_url"]).params
    assert {"response_type", "client_id", "redirect_uri", "state", "scope", "code_challenge",
            "code_challenge_method", "nonce"} <= set(params.keys())
    assert params["scope"] == "openid profile email"
    # The state finds the flow at the callback; it is neither the flow id nor the secret,
    # and the secret appears nowhere in the URL.
    assert params["state"] not in (flow["flow_id"], flow["poll_secret"])
    assert flow["poll_secret"] not in flow["authorize_url"]
    assert poll(server=server, flow=flow).json() == {"state": "pending"}
    # Discovery says so, with the provider's name.
    doc = server.http.get(url="/warp").json()
    assert "oauth_browser" in doc["capabilities"] and doc["auth"]["oauth_browser"] == {"provider": "Mock IdP"}


def test_invalid_authorization_code(server, idp):
    flow = create(server=server)
    callback = urllib.parse.urlsplit(callback_url(flow=flow))
    state = dict(urllib.parse.parse_qsl(callback.query))["state"]
    page = server.http.get(url="/warp/v1/auth/oauth/callback", params={"code": "bad-code", "state": state})
    assert "Sign-in failed" in page.text and "invalid_grant" in page.text, page.text
    body = poll(server=server, flow=flow).json()
    assert body["state"] == "failed" and body["error"]["code"] == "identity_provider_error", body
    assert "invalid_grant: the code is not valid" in body["error"]["detail"], body


def test_unauthorized_email(server, idp):
    idp.email = "unauthorized@evil.com"
    flow = create(server=server)
    page = browse(server=server, flow=flow)
    assert "Sign-in failed" in page.text and "unauthorized@evil.com" in page.text, page.text
    body = poll(server=server, flow=flow).json()
    assert body["state"] == "failed" and body["error"]["code"] == "invalid_credentials", body
    assert "is not authorized" in body["error"]["detail"]
    # And the provider's token for that user doesn't sign in either.
    response = exchange(server=server, token=idp.id_token(email="unauthorized@evil.com"))
    assert response.status_code == 401 and "is not authorized" in response.json()["detail"], response.text


def test_token_poll_not_found(server):
    response = server.http.post(url=f"{FLOWS}/f_doesnotexist", json={"poll_secret": "x"})
    assert response.status_code == 404 and response.json()["code"] == "flow_not_found"


def test_pending_session_poll(server):
    flow = create(server=server)
    response = poll(server=server, flow=flow)
    assert response.status_code == 200 and response.json() == {"state": "pending"}


def test_the_poll_secret_is_required(server):
    flow = create(server=server)
    browse(server=server, flow=flow)
    for wrong in ("", "nope", flow["flow_id"], flow["poll_secret"] + "x"):
        response = poll(server=server, flow=flow, secret=wrong)
        assert response.status_code == 404 and response.json()["code"] == "flow_not_found", wrong
    missing = server.http.post(url=f"{FLOWS}/{flow['flow_id']}", json={})
    assert missing.status_code == 400 and missing.json()["code"] == "bad_request"
    in_url = server.http.get(url=f"{FLOWS}/{flow['flow_id']}", params={"poll_secret": flow["poll_secret"]})
    assert in_url.status_code == 405
    assert poll(server=server, flow=flow).json()["state"] == "complete"


def test_duplicate_callback_rejected(server):
    flow = create(server=server)
    url = callback_url(flow=flow)
    assert server.http.get(url=url).status_code == 200
    again = server.http.get(url=url)
    assert again.status_code == 409 and "already been completed" in again.text, again.text


def test_missing_state(server):
    response = server.http.get(url="/warp/v1/auth/oauth/callback", params={"code": "x"})
    assert response.status_code == 400 and "unknown or has expired" in response.text


def test_provider_error_is_escaped(server, idp):
    idp.deny = True
    idp.deny_description = "<script>alert(1)</script>"
    flow = create(server=server)
    response = browse(server=server, flow=flow)
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "default-src 'none'" in response.headers["content-security-policy"]
    body = poll(server=server, flow=flow).json()
    assert body["state"] == "failed" and body["error"]["code"] == "access_denied", body


def test_a_foreign_nonce_is_refused(server, idp):
    idp.nonce_override = "another-flows-nonce"
    flow = create(server=server)
    browse(server=server, flow=flow)
    body = poll(server=server, flow=flow).json()
    assert body["state"] == "failed" and body["error"]["code"] == "invalid_credentials", body
    assert "nonce" in body["error"]["detail"]


def test_return_to_comes_back_with_the_flow_id(server):
    flow = create(server=server, body={"return_to": "/ui/"})
    redirect = browse(server=server, flow=flow)
    # oauth_base_url is set, so the redirect is absolute on it.
    assert redirect.status_code == 303
    assert redirect.headers["location"] == f"{server.base_url}/ui/#warp_oauth_flow={flow['flow_id']}"
    assert poll(server=server, flow=flow).json()["state"] == "complete"


@pytest.mark.parametrize("return_to", ["https://evil.example/", "//evil.example", "/\\evil.example", "/ui/#x", ""])
def test_return_to_must_stay_on_this_server(server, return_to):
    response = server.http.post(url=FLOWS, json={"return_to": return_to})
    assert response.status_code == 400 and response.json()["code"] == "invalid_return_to", response.text


def test_default_redirect_uri_and_relative_return(launcher, idp):
    # Without oauth_base_url: http://localhost:<port>, and a relative return redirect.
    server = launcher.start(args=oauth_args(idp, "--oauth-client-secret", CLIENT_SECRET))
    http = httpx.Client(base_url=server.base_url, follow_redirects=False)
    flow = http.post(url=FLOWS, json={"return_to": "/ui/"}).json()
    params = httpx.URL(flow["authorize_url"]).params
    assert params["redirect_uri"] == f"http://localhost:{server.port}/warp/v1/auth/oauth/callback"
    callback = urllib.parse.urlsplit(callback_url(flow=flow))
    redirect = http.get(url=f"{callback.path}?{callback.query}")
    assert redirect.headers["location"] == f"/ui/#warp_oauth_flow={flow['flow_id']}"
    # No provider name configured: discovery names none.
    assert http.get(url="/warp").json()["auth"]["oauth_browser"] == {}


def test_instance_id_prefixes_the_state(launcher, idp):
    # A shared callback proxy on another origin routes the provider's redirects by the
    # state's prefix to the server's callback; the return redirect is absolute on the
    # server's public URL.
    server = launcher.start(args=oauth_args(idp, "--oauth-client-secret", CLIENT_SECRET,
                                            "--oauth-instance-id", "replica-7",
                                            "--oauth-redirect-uri", "https://sign-in.example.test/callback",
                                            "--oauth-base-url", "https://replica-7.example.test"))
    http = httpx.Client(base_url=server.base_url, follow_redirects=False)
    flow = http.post(url=FLOWS, json={"return_to": "/ui/"}).json()
    params = httpx.URL(flow["authorize_url"]).params
    assert params["state"].startswith("replica-7.")
    assert params["redirect_uri"] == "https://sign-in.example.test/callback"
    proxied = urllib.parse.urlsplit(callback_url(flow=flow))
    assert proxied.netloc == "sign-in.example.test"
    redirect = http.get(url=f"/warp/v1/auth/oauth/callback?{proxied.query}")
    assert redirect.headers["location"] == f"https://replica-7.example.test/ui/#warp_oauth_flow={flow['flow_id']}"
    assert http.post(url=f"{FLOWS}/{flow['flow_id']}", json={"poll_secret": flow["poll_secret"]}).json()["state"] == \
        "complete"


def test_pending_sign_ins_are_bounded(launcher, idp):
    server = launcher.start(args=oauth_args(idp, "--oauth-client-secret", CLIENT_SECRET, "--oauth-max-flows", "3"))
    http = httpx.Client(base_url=server.base_url)
    for _ in range(3):
        assert http.post(url=FLOWS, json={}).status_code == 201
    refused = http.post(url=FLOWS, json={})
    assert refused.status_code == 429 and refused.json()["code"] == "rate_limited" and refused.headers["retry-after"]


def test_without_oauth_client_id_there_is_no_browser_sign_in(launcher):
    server = launcher.start()
    http = httpx.Client(base_url=server.base_url)
    assert "oauth_browser" not in http.get(url="/warp").json()["capabilities"]
    response = http.post(url=FLOWS, json={})
    assert response.status_code == 501 and response.json()["code"] == "not_implemented"


def test_the_old_side_port_options_are_gone(launcher):
    for flag in ("--oauth-port", "--oauth-disable-tls"):
        server = launcher.start(args=[flag, "31339"], expect_up=False)
        assert server.process.wait(timeout=30) != 0
        assert flag.removeprefix("--") in server.log() or "unknown" in server.log().lower(), server.log()


def test_needs_no_license(launcher, idp):
    # 1.x refused the OAuth server without its license feature; Hardcharger has no license.
    server = launcher.start(args=oauth_args(idp, "--oauth-client-secret", CLIENT_SECRET))
    response = httpx.post(url=f"{server.base_url}{FLOWS}", json={})
    assert response.status_code == 201 and "flow_id" in response.json(), server.log()


def exchange(server, token: str) -> httpx.Response:
    return httpx.post(url=f"{server.base_url}/warp/v1/auth/token",
                      json={"grant_type": "token_exchange", "subject_token": token})


def test_jwks_verifies_provider_tokens(server, idp):
    # (Exchange first: run alone, or first of its module on a pytest-xdist worker, this
    # test may be the server's first use of the provider.)
    assert exchange(server=server, token=idp.id_token(email="a@gizmodata.com")).status_code == 200
    assert idp.jwks_requests > 0, "the server never read the provider's JWKS"
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


@pytest.mark.parametrize("coordinate", ["x", "y"])
def test_jwks_ec_key_with_a_leading_zero_coordinate(launcher, coordinate):
    # About 2 P-256 keys in 256 have a coordinate whose first byte is zero. RFC 7518
    # §6.2.1.2 says a JWK still carries it in full (32 bytes); the mock once wrote 31, and
    # the server refused every token of such a key (test_jwks_with_ec_keys then failed
    # now and then). The key is chosen to have the short coordinate, every time.
    provider = MockIdP(audience=AUDIENCE, algorithm="ES256")
    while getattr(provider.key.public_key().public_numbers(), coordinate).bit_length() > 248:
        provider.key = ec.generate_private_key(curve=ec.SECP256R1())
    try:
        server = launcher.start(args=["--token-allowed-issuer", provider.issuer, "--token-allowed-audience", AUDIENCE,
                                      "--token-default-role", "user"])
        response = exchange(server=server, token=provider.id_token(email="ec@gizmodata.com"))
        assert response.status_code == 200, response.text
        assert len(base64.urlsafe_b64decode(provider.jwk()[coordinate] + "==")) == 32
    finally:
        provider.stop()


def test_jwks_sign_in_needs_no_license(launcher, idp):
    # token_jwks_uri set explicitly. 1.x refused these tokens without its license feature;
    # Hardcharger accepts them.
    server = launcher.start(args=["--token-allowed-issuer", idp.issuer, "--token-allowed-audience", AUDIENCE,
                                  "--token-default-role", "admin", "--token-jwks-uri", f"{idp.issuer}/jwks"])
    response = exchange(server=server, token=idp.id_token(email="a@gizmodata.com"))
    assert response.status_code == 200, response.text
