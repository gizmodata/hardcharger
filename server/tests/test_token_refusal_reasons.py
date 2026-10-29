"""Why a token stopped working (Chargewire §4.2) and why a session ended (§5.6).

Changing a user's password, disabling or dropping the user, and revoking a scoped token
end the user's sessions and the tokens they were opened with. The session's own token is
refused before the server looks at the session, so its holder learns why from the
invalid_token refusal's `reason` (credentials_changed, principal_dropped, token_revoked,
token_expired). A token that signs in again, used on the old session, gets session_ended
with the same reason; the user's own token on a scoped token's session is another
principal's (Chargewire §9.2) and gets session_not_found. A token the server can't verify as one it signed (another key, a
changed signature) gets no reason, whatever user it names.
"""

import datetime
import time

import httpx
import jwt
import pytest

from conftest import TIMEOUT_SCALE
from rbac import SECRET_KEY, Client, ChargewireError, expect_allowed, mint, start_rbac_server


@pytest.fixture(scope="module")
def server(module_launcher, store):
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database())
    server.admin_client = server.admin()
    return server


def create_user(server, name: str, password: str = "pw-1") -> None:
    expect_allowed(client=server.admin_client, sql=f"CREATE USER {name} WITH PASSWORD '{password}'")


def refusal(response: httpx.Response) -> dict:
    assert response.status_code == 401, response.text
    assert response.headers.get("www-authenticate") == "Bearer", response.headers
    return response.json()


def until_refused(client: Client) -> httpx.Response:
    """The session's next request once access control has seen the change (it polls the
    store's version about once a second)."""
    deadline = time.monotonic() + 15 * TIMEOUT_SCALE
    while True:
        response = client.http.get(url=client.url, headers=client.headers())
        if response.status_code != 200 or time.monotonic() > deadline:
            return response
        time.sleep(0.2)


def assert_token_refused(response: httpx.Response, reason: str, detail: str) -> None:
    problem = refusal(response=response)
    assert problem["code"] == "invalid_token" and problem.get("reason") == reason, problem
    assert detail in problem["detail"], problem


def assert_session_ended(response: httpx.Response, reason: str) -> None:
    problem = refusal(response=response)
    assert problem["code"] == "session_ended" and problem["reason"] == reason, problem


def assert_session_not_found(response: httpx.Response) -> None:
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "session_not_found", response.text


def on(client: Client, token: str, path: str = "") -> httpx.Response:
    """A request on `client`'s session with another token."""
    return client.http.get(url=client.url + path, headers={"Authorization": f"Bearer {token}"})


def test_a_password_change_says_credentials_changed(server):
    create_user(server=server, name="pat")
    create_user(server=server, name="sam")
    pat = server.user(name="pat", password="pw-1")
    sam = server.user(name="sam", password="pw-1")
    expect_allowed(client=server.admin_client, sql="ALTER USER pat WITH PASSWORD 'pw-2'")
    assert_token_refused(response=until_refused(client=pat), reason="credentials_changed",
                         detail="the password of user pat changed since this access token was issued")
    # Every request with that token says so, a new session included.
    assert_token_refused(response=pat.http.post(url="/chargewire/v1/sessions", headers=pat.headers(), json={}),
                         reason="credentials_changed", detail="the password of user pat changed")
    # Signed in again, the old session is gone, for the same reason.
    assert_session_ended(response=on(client=pat, token=server.password_token(user="pat", password="pw-2")),
                         reason="credentials_changed")
    # The twin: a user whose password didn't change.
    assert sam.http.get(url=sam.url, headers=sam.headers()).status_code == 200


def test_disabling_a_user_says_principal_dropped(server):
    create_user(server=server, name="dee")
    dee = server.user(name="dee", password="pw-1")
    expect_allowed(client=server.admin_client, sql="ALTER USER dee DISABLE")
    assert_token_refused(response=until_refused(client=dee), reason="principal_dropped", detail="user dee is disabled")
    # Enabled again, a new sign-in finds the old session ended because the user was disabled.
    expect_allowed(client=server.admin_client, sql="ALTER USER dee ENABLE")
    assert_session_ended(response=on(client=dee, token=server.password_token(user="dee", password="pw-1")),
                         reason="principal_dropped")


def test_dropping_a_user_says_principal_dropped(server):
    create_user(server=server, name="dot")
    dot = server.user(name="dot", password="pw-1")
    expect_allowed(client=server.admin_client, sql="DROP USER dot")
    assert_token_refused(response=until_refused(client=dot), reason="principal_dropped",
                         detail="user dot is not defined on this server")


def scoped(server, owner: Client, sql: str) -> Client:
    rows = expect_allowed(client=owner, sql=sql).to_pylist()
    return server.client(token=rows[0]["token"])


def test_a_revoked_scoped_token_says_token_revoked(server):
    create_user(server=server, name="ray")
    ray = server.user(name="ray", password="pw-1")
    bot = scoped(server=server, owner=ray, sql="CREATE SCOPED TOKEN job ALLOW SELECT")
    other = scoped(server=server, owner=ray, sql="CREATE SCOPED TOKEN keep ALLOW SELECT")
    expect_allowed(client=ray, sql="DROP SCOPED TOKEN job")
    assert_token_refused(response=until_refused(client=bot), reason="token_revoked",
                         detail="this scoped token was revoked")
    # The user's own token on the scoped session: another principal's session (Chargewire
    # §9.2: a scoped token is another authority), so it is told nothing about it.
    assert_session_not_found(response=on(client=bot, token=ray.token))
    # The twins: the user's other scoped token and the user's own session still work.
    assert other.http.get(url=other.url, headers=other.headers()).status_code == 200
    assert ray.http.get(url=ray.url, headers=ray.headers()).status_code == 200


def test_an_expired_scoped_token_says_token_expired(server):
    create_user(server=server, name="eve")
    eve = server.user(name="eve", password="pw-1")
    valid = int(2 * TIMEOUT_SCALE)
    bot = scoped(server=server, owner=eve, sql=f"CREATE SCOPED TOKEN brief ALLOW SELECT VALID FOR '{valid} seconds'")
    assert bot.http.get(url=bot.url, headers=bot.headers()).status_code == 200
    time.sleep(2 * valid)
    assert_token_refused(response=bot.http.get(url=bot.url, headers=bot.headers()), reason="token_expired",
                         detail="the access token has expired")
    assert_session_not_found(response=on(client=bot, token=eve.token))  # another authority's


def test_an_expired_token_says_token_expired(server):
    create_user(server=server, name="old")
    token = mint(user="old")
    client = server.client(token=token)
    now = datetime.datetime.now(tz=datetime.UTC)
    expired = jwt.encode(payload={"iss": "hardcharger", "sub": "old", "role": "user",
                                  "iat": now - datetime.timedelta(hours=2), "exp": now - datetime.timedelta(hours=1)},
                         key=SECRET_KEY, algorithm="HS256")
    assert_token_refused(response=on(client=client, token=expired), reason="token_expired",
                         detail="the access token has expired")


def test_a_token_the_server_did_not_sign_gets_no_reason(server):
    """Only the holder of a token the server signed learns why it stopped working: a token
    signed with another key, or with its signature changed, says nothing about the user
    it names (dropped, disabled, expired)."""
    create_user(server=server, name="gil")
    gil = server.user(name="gil", password="pw-1")
    expect_allowed(client=server.admin_client, sql="DROP USER gil")
    now = datetime.datetime.now(tz=datetime.UTC)
    for claims in [{"sub": "gil", "exp": now + datetime.timedelta(hours=1)},     # dropped
                   {"sub": "nobody", "exp": now + datetime.timedelta(hours=1)},  # never existed
                   {"sub": "gil", "exp": now - datetime.timedelta(hours=1)}]:    # expired
        payload = {"iss": "hardcharger", "role": "user", "iat": now - datetime.timedelta(hours=2), **claims}
        other_key = jwt.encode(payload=payload, key="another-servers-secret-key-0123456789", algorithm="HS256")
        ours = jwt.encode(payload=payload, key=SECRET_KEY, algorithm="HS256")
        at = len(ours) - 10
        changed = ours[:at] + ("B" if ours[at] == "A" else "A") + ours[at + 1:]
        for token in (other_key, changed):
            problem = refusal(response=on(client=gil, token=token))
            assert problem["code"] == "invalid_token" and "reason" not in problem, (claims, problem)
            assert problem["detail"] == "a valid access token is required", problem
    # The twin: the same claims signed with this server's key do say why.
    signed = jwt.encode(payload={"iss": "hardcharger", "sub": "gil", "role": "user", "iat": now,
                                 "exp": now + datetime.timedelta(hours=1)}, key=SECRET_KEY, algorithm="HS256")
    assert_token_refused(response=on(client=gil, token=signed), reason="principal_dropped",
                         detail="user gil is not defined on this server")


def test_a_refused_token_has_no_reason_without_access_control(module_launcher):
    """A server without access control refuses only tokens it can't verify, or expired ones."""
    plain = module_launcher.start()
    with pytest.raises(ChargewireError) as raised:
        Client(base_url=plain.base_url, token="not-a-token")
    assert raised.value.code == "invalid_token" and raised.value.reason is None, raised.value
