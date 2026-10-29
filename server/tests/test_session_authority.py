"""A session and its handles belong to the principal's name AND its authority (Chargewire §9.2).

A session's DuckDB connection is bound, at creation, to the principal that opened it. Were
handles bound to the user name alone, any credential of that name (a readonly token, a
scoped token, a token with catalog_access limits, another identity provider's groups)
could run statements through a session a stronger credential of the same user opened, with
that credential's privileges, simply by naming its id (the ADBC driver attaches to a session
by id and token). Every such request must get 404 session_not_found, as another user's
would, on every route: the session, its statements, its retained results, prepared
statements, ingests, metadata, cancel and delete.

The positive twins: a new token with the same authority (signing in again with the password,
a fresh server-signed token with the same claims, the administrator's second sign-in, a second
exchange of the same scoped token, a sign-in after a password change) reaches the session.
"""

import secrets

import httpx
import pytest

from conftest import mint_server_token, run_unit_tests
from rbac import (
    Client,
    ChargewireError,
    expect_allowed,
    expect_blocked,
    mint,
    start_rbac_server,
)

READONLY_REFUSED = "has a readonly session and cannot run statements that modify state"


# ---- Helpers ----

class Attached(Client):
    """A client that uses an existing session by its id (as the ADBC driver's attach does)."""

    def __init__(self, base_url: str, token: str, session_id: str):
        self.http = httpx.Client(base_url=base_url, timeout=120)
        self.token = token
        self.session_id = session_id
        self.url = f"/chargewire/v1/sessions/{session_id}"


def owner_session(base_url: str, token: str) -> tuple[Client, str]:
    """A session of `token`, with a statement whose result is retained."""
    owner = Client(base_url=base_url, token=token)
    statement_id = owner.submit(sql="SELECT 42 AS answer")
    assert owner.wait(statement_id=statement_id)["state"] == "succeeded"
    return owner, statement_id


def every_route(base_url: str, token: str, session_id: str, statement_id: str) -> dict[str, httpx.Response]:
    """What `token` gets on each route of session `session_id` (DELETE session last)."""
    s = f"/chargewire/v1/sessions/{session_id}"
    q = f"{s}/statements/{statement_id}"
    auth = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=base_url, timeout=120) as http:
        return {
            "GET session": http.get(url=s, headers=auth),
            "PATCH session": http.patch(url=s, headers=auth, json={"options": {}}),
            "submit": http.post(url=f"{s}/statements", headers=auth, json={"sql": "SELECT 1"}),
            "GET statement": http.get(url=q, headers=auth),
            "GET result": http.get(url=f"{q}/result", headers={**auth, "Accept": "application/vnd.apache.arrow.stream"}),
            "cancel statement": http.post(url=f"{q}/cancel", headers=auth, json={}),
            "prepare": http.post(url=f"{s}/prepared", headers=auth, json={"sql": "SELECT 1"}),
            "metadata": http.get(url=f"{s}/metadata/catalogs", headers=auth),
            "open ingest": http.post(url=f"{s}/ingests", headers=auth, json={"table": "t_ingest", "mode": "create"}),
            "commit": http.post(url=f"{s}/commit", headers=auth, json={}),
            "cancel session": http.post(url=f"{s}/cancel", headers=auth, json={}),
            "DELETE statement": http.delete(url=q, headers=auth),
            "DELETE session": http.delete(url=s, headers=auth),
        }


def assert_shut_out(base_url: str, owner: Client, statement_id: str, other_token: str) -> None:
    """`other_token` gets session_not_found on every route of the owner's session, and the
    owner's session, statement and retained result are untouched afterwards."""
    answers = every_route(base_url=base_url, token=other_token, session_id=owner.session_id,
                          statement_id=statement_id)
    wrong = {route: f"{r.status_code} {r.text[:200]}" for route, r in answers.items()
             if r.status_code != 404 or r.json().get("code") != "session_not_found"}
    assert not wrong, f"another authority reached the session: {wrong}"
    # The same answer as for a session that does not exist (spec §9.2: ids can't be probed).
    missing = httpx.get(url=f"{base_url}/chargewire/v1/sessions/s_{'a' * 32}",
                        headers={"Authorization": f"Bearer {other_token}"})
    assert missing.status_code == 404 and missing.json()["detail"] == answers["GET session"].json()["detail"]
    # The owner's retained result is still there: none of the requests above touched it.
    assert owner.wait(statement_id=statement_id)["state"] == "succeeded"
    result = owner.http.get(url=f"{owner.url}/statements/{statement_id}/result", headers=owner.headers())
    assert result.status_code == 200, result.text
    assert owner._outcome(response=result).to_pylist() == [{"answer": 42}]


def assert_reaches(base_url: str, owner: Client, other_token: str) -> None:
    """`other_token` (the same authority) runs in the owner's session and reads its results."""
    attached = Attached(base_url=base_url, token=other_token, session_id=owner.session_id)
    assert attached.http.get(url=attached.url, headers=attached.headers()).status_code == 200
    statement_id = attached.submit(sql="SELECT 7 AS seven")
    assert attached.wait(statement_id=statement_id)["state"] == "succeeded"
    # ... and the owner's token reads what it ran: one session.
    result = owner.http.get(url=f"{owner.url}/statements/{statement_id}/result", headers=owner.headers())
    assert owner._outcome(response=result).to_pylist() == [{"seven": 7}]


def test_the_authority_key_unit_tests_pass():
    # server/unit/authority_key_test.cc: what is in the key and what isn't.
    results = run_unit_tests(gtest_filter="AuthorityKey.*")
    assert len(results) == 3, results
    assert all(results.values()), results


# ---- Without access control: roles and catalog_access rules ----

@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start(args=["--database", "session_authority.db"])


def token(server, subject: str, role: str, **claims) -> str:
    return mint_server_token(secret_key=server.secret_key, subject=subject, role=role, **claims)


def test_a_readonly_token_cannot_use_its_users_full_session(server):
    full = token(server=server, subject="tess", role="user")
    readonly = token(server=server, subject="tess", role="readonly")
    owner, statement_id = owner_session(base_url=server.base_url, token=full)
    # What the readonly token would gain: a write it is refused in a session of its own...
    with pytest.raises(ChargewireError) as refused:
        Client(base_url=server.base_url, token=readonly).run(sql="CREATE TABLE ro_escalated AS SELECT 1 AS x")
    assert READONLY_REFUSED in refused.value.detail, refused.value
    # ... it can't get through the user's session either.
    attached = Attached(base_url=server.base_url, token=readonly, session_id=owner.session_id)
    with pytest.raises(ChargewireError) as through:
        attached.run(sql="CREATE TABLE ro_escalated AS SELECT 1 AS x")
    assert (through.value.status, through.value.code) == (404, "session_not_found"), through.value
    assert expect_allowed(client=owner, sql="SELECT count(*) AS n FROM duckdb_tables() "
                                            "WHERE table_name = 'ro_escalated'").to_pylist() == [{"n": 0}]
    assert_shut_out(base_url=server.base_url, owner=owner, statement_id=statement_id, other_token=readonly)


def test_a_full_token_cannot_use_its_users_readonly_session(server):
    readonly = token(server=server, subject="tess2", role="readonly")
    owner, statement_id = owner_session(base_url=server.base_url, token=readonly)
    assert_shut_out(base_url=server.base_url, owner=owner, statement_id=statement_id,
                    other_token=token(server=server, subject="tess2", role="user"))


def test_a_new_token_with_the_same_claims_reaches_the_session(server):
    owner, _ = owner_session(base_url=server.base_url, token=token(server=server, subject="tess3", role="user"))
    assert_reaches(base_url=server.base_url, owner=owner, other_token=token(server=server, subject="tess3", role="user"))


def test_the_administrators_new_sign_in_reaches_its_session(server):
    first = server.token()
    owner, _ = owner_session(base_url=server.base_url, token=first)
    second = server.token()
    assert second != first
    assert_reaches(base_url=server.base_url, owner=owner, other_token=second)


@pytest.mark.parametrize("owner_rules,other_rules", [
    (None, [{"catalog": "session_authority", "access": "read"}]),     # unrestricted's session, restricted token
    ([{"catalog": "session_authority", "access": "read"}], None),     # and the reverse
    ([{"catalog": "*", "access": "read"}], [{"catalog": "*", "access": "write"}]),  # other rules
    ([], None),                                                       # an empty list is not no rules
], ids=["restricted-token", "unrestricted-token", "other-rules", "empty-rules"])
def test_catalog_access_rules_are_part_of_the_authority(server, owner_rules, other_rules):
    name = "carol_" + secrets.token_hex(nbytes=3)

    def rules(value):
        return {} if value is None else {"catalog_access": value}
    owner, statement_id = owner_session(base_url=server.base_url,
                                        token=token(server=server, subject=name, role="user", **rules(owner_rules)))
    assert_shut_out(base_url=server.base_url, owner=owner, statement_id=statement_id,
                    other_token=token(server=server, subject=name, role="user", **rules(other_rules)))
    # The twin: the owner's own rules in a new token reach it.
    assert_reaches(base_url=server.base_url, owner=owner,
                   other_token=token(server=server, subject=name, role="user", **rules(owner_rules)))


def test_a_restricted_administrator_token_cannot_use_the_administrators_session(server):
    owner, statement_id = owner_session(base_url=server.base_url, token=server.token())
    restricted = token(server=server, subject=server.username, role="admin",
                       catalog_access=[{"catalog": "session_authority", "access": "read"}])
    # What it would gain: a write the rules refuse in its own session.
    with pytest.raises(ChargewireError) as refused:
        Client(base_url=server.base_url, token=restricted).run(sql="CREATE TABLE admin_escalated AS SELECT 1 AS x")
    assert "Access denied" in refused.value.detail, refused.value
    assert_shut_out(base_url=server.base_url, owner=owner, statement_id=statement_id, other_token=restricted)


# ---- Under access control: store users, scoped tokens, identity providers ----

ISSUER = "https://idp.example.test"


@pytest.fixture(scope="module")
def rbac_server(module_launcher, store):
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database())
    admin = server.admin()
    for sql in ["ATTACH ':memory:' AS sales_db", "CREATE TABLE sales_db.main.orders AS SELECT 1 AS id",
                "CREATE ROLE worker", "GRANT SELECT, INSERT ON CATALOG sales_db TO worker",
                "GRANT CREATE TABLE ON SCHEMA sales_db.main TO worker",
                "CREATE USER wes PASSWORD 'wes-password-1'", "GRANT worker TO wes",
                "CREATE ROLE analyst", "GRANT SELECT ON CATALOG sales_db TO analyst", "CREATE USER gina",
                f"CREATE GROUP MAPPING 'okta-analysts' ISSUER '{ISSUER}' TO ROLE analyst"]:
        expect_allowed(client=admin, sql=sql)
    server.admin_client = admin
    return server


def test_a_store_users_readonly_token_cannot_use_its_password_session(rbac_server):
    owner, statement_id = owner_session(base_url=rbac_server.base_url,
                                        token=rbac_server.password_token(user="wes", password="wes-password-1"))
    readonly = mint(user="wes", role="readonly")
    attached = Attached(base_url=rbac_server.base_url, token=readonly, session_id=owner.session_id)
    with pytest.raises(ChargewireError) as through:
        attached.run(sql="INSERT INTO sales_db.main.orders VALUES (99)")
    assert (through.value.status, through.value.code) == (404, "session_not_found"), through.value
    assert expect_allowed(client=owner, sql="SELECT count(*) AS n FROM sales_db.main.orders "
                                            "WHERE id = 99").to_pylist() == [{"n": 0}]
    assert_shut_out(base_url=rbac_server.base_url, owner=owner, statement_id=statement_id, other_token=readonly)


def test_signing_in_again_with_the_password_reaches_the_session(rbac_server):
    owner, _ = owner_session(base_url=rbac_server.base_url,
                             token=rbac_server.password_token(user="wes", password="wes-password-1"))
    again = rbac_server.password_token(user="wes", password="wes-password-1")
    assert again != owner.token
    assert_reaches(base_url=rbac_server.base_url, owner=owner, other_token=again)
    # A server-signed token of the same authority (no password fingerprint) too.
    assert_reaches(base_url=rbac_server.base_url, owner=owner, other_token=mint(user="wes", role="user"))


def test_after_a_password_change_the_new_sign_in_is_still_the_sessions_owner(rbac_server):
    admin = rbac_server.admin_client
    expect_allowed(client=admin, sql="CREATE USER pat PASSWORD 'pat-old-password'")
    expect_allowed(client=admin, sql="GRANT worker TO pat")
    owner = Client(base_url=rbac_server.base_url, token=rbac_server.password_token(user="pat",
                                                                                 password="pat-old-password"))
    expect_allowed(client=admin, sql="ALTER USER pat PASSWORD 'pat-new-password'")
    # The old token stops working (CheckToken), and access control ends the session.
    stale = owner.http.get(url=owner.url, headers=owner.headers())
    assert stale.status_code == 401 and stale.json().get("reason") == "credentials_changed", stale.text
    # The password fingerprint is not part of the authority: a sign-in with the new password
    # is the session's owner, so it learns why the session ended (spec §5.6) rather than
    # getting session_not_found, and may forget it.
    new_token = rbac_server.password_token(user="pat", password="pat-new-password")
    ended = httpx.get(url=f"{rbac_server.base_url}{owner.url}", headers={"Authorization": f"Bearer {new_token}"})
    assert ended.status_code == 401 and ended.json()["code"] == "session_ended", ended.text
    assert "password changed" in ended.json()["detail"], ended.text
    # A readonly token of pat is told nothing about it, as another user would be.
    hidden = httpx.get(url=f"{rbac_server.base_url}{owner.url}",
                       headers={"Authorization": f"Bearer {mint(user='pat', role='readonly')}"})
    assert hidden.status_code == 404 and hidden.json()["code"] == "session_not_found", hidden.text
    deleted = httpx.delete(url=f"{rbac_server.base_url}{owner.url}", headers={"Authorization": f"Bearer {new_token}"})
    assert deleted.status_code == 204, deleted.text


def mint_scoped(client, sql: str) -> str:
    rows = expect_allowed(client=client, sql=sql).to_pylist()
    assert rows and rows[0]["token"], rows
    return rows[0]["token"]


def test_a_scoped_token_and_its_user_never_share_a_session(rbac_server):
    full = rbac_server.password_token(user="wes", password="wes-password-1")
    wes, wes_statement = owner_session(base_url=rbac_server.base_url, token=full)
    scoped = mint_scoped(client=wes, sql="CREATE SCOPED TOKEN reader ALLOW SELECT ON CATALOG sales_db")
    bot, bot_statement = owner_session(base_url=rbac_server.base_url, token=scoped)
    # What the scoped token would gain through its user's session: a write it may not do.
    expect_blocked(client=bot, sql="INSERT INTO sales_db.main.orders VALUES (98)", why="allows only SELECT")
    attached = Attached(base_url=rbac_server.base_url, token=scoped, session_id=wes.session_id)
    with pytest.raises(ChargewireError) as through:
        attached.run(sql="INSERT INTO sales_db.main.orders VALUES (98)")
    assert (through.value.status, through.value.code) == (404, "session_not_found"), through.value
    assert_shut_out(base_url=rbac_server.base_url, owner=wes, statement_id=wes_statement, other_token=scoped)
    # The reverse: the user's full token on the scoped token's session.
    assert_shut_out(base_url=rbac_server.base_url, owner=bot, statement_id=bot_statement, other_token=full)
    # Another scoped token of the same user, with the same allowance, is another authority.
    other = mint_scoped(client=wes, sql="CREATE SCOPED TOKEN reader2 ALLOW SELECT ON CATALOG sales_db")
    assert_shut_out(base_url=rbac_server.base_url, owner=bot, statement_id=bot_statement, other_token=other)
    # The twin: the same scoped token, exchanged as the password of user "token", reaches it.
    exchanged = rbac_server.password_token(user="token", password=scoped)
    assert_reaches(base_url=rbac_server.base_url, owner=bot, other_token=exchanged)


@pytest.mark.parametrize("other_idp", [
    {"iss": "https://other-idp.example.test", "groups": ["okta-analysts"]},  # another issuer
    {"iss": ISSUER, "groups": ["unrelated"]},                                # other groups
    None,                                                                    # no groups at all
], ids=["other-issuer", "other-groups", "no-groups"])
def test_the_identity_providers_issuer_and_groups_are_part_of_the_authority(rbac_server, other_idp):
    owner_claims = {"idp": {"iss": ISSUER, "groups": ["okta-analysts", "unrelated"]}}
    owner, statement_id = owner_session(base_url=rbac_server.base_url, token=mint(user="gina", **owner_claims))
    other = mint(user="gina", **({} if other_idp is None else {"idp": other_idp}))
    assert_shut_out(base_url=rbac_server.base_url, owner=owner, statement_id=statement_id, other_token=other)
    # The twin: the same groups, listed in another order, are the same authority.
    same = mint(user="gina", idp={"iss": ISSUER, "groups": ["unrelated", "okta-analysts"]})
    assert_reaches(base_url=rbac_server.base_url, owner=owner, other_token=same)


def test_a_user_created_again_under_the_name_is_another_principal(rbac_server):
    admin = rbac_server.admin_client
    expect_allowed(client=admin, sql="CREATE USER zed")
    expect_allowed(client=admin, sql="GRANT worker TO zed")
    owner, _ = owner_session(base_url=rbac_server.base_url, token=mint(user="zed"))
    expect_allowed(client=admin, sql="DROP USER zed")
    expect_allowed(client=admin, sql="CREATE USER zed")  # no grants
    answer = httpx.get(url=f"{rbac_server.base_url}{owner.url}",
                       headers={"Authorization": f"Bearer {mint(user='zed')}"})
    assert answer.status_code == 404 and answer.json()["code"] == "session_not_found", answer.text
