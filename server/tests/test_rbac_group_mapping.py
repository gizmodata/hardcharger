"""Identity-provider group mappings: CREATE GROUP MAPPING 'group' [ISSUER 'iss'] TO ROLE r
gives the role to users whose sign-in token lists the group."""

import datetime
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from rbac import RbacServer, WarpError, expect_allowed, start_rbac_server

ISSUER = "https://idp.example.test"


@pytest.fixture(scope="module")
def server(module_launcher, store):
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database())
    admin = server.admin()
    for sql in ["CREATE TABLE sales AS SELECT 1 AS id, 100 AS amount",
                "CREATE TABLE payroll AS SELECT 1 AS id, 5000 AS salary",
                "CREATE ROLE analyst", "CREATE ROLE hr", "GRANT SELECT ON sales TO analyst",
                "GRANT SELECT ON payroll TO hr", "CREATE USER gina", "CREATE USER hank",
                f"CREATE GROUP MAPPING 'okta-analysts' ISSUER '{ISSUER}' TO ROLE analyst",
                "CREATE GROUP MAPPING 'hr-team' TO ROLE hr",
                "CREATE TENANT acme", "CREATE USER acme/amy", "CREATE ROLE acme/r",
                "CREATE GROUP MAPPING 'acme-staff' TO ROLE acme/r"]:
        expect_allowed(client=admin, sql=sql)
    server.admin_client = admin
    return server


@pytest.fixture
def admin(server):
    return server.admin_client


def member(server, user: str, groups: list[str] | None = None, issuer: str = ISSUER):
    """A session whose token carries the identity provider's groups."""
    claims = {"idp": {"iss": issuer, "groups": groups}} if groups else {}
    return server.user(name=user, **claims)


def error(client, sql: str) -> str:
    try:
        client.run(sql=sql)
        return ""
    except WarpError as raised:
        return raised.detail


def refused(detail: str, table: str) -> None:
    assert "no SELECT privilege on table" in detail, f"{table} -> {detail or 'READ ALLOWED'}"


def test_a_group_in_the_token_gives_its_role(server):
    gina = member(server=server, user="gina", groups=["okta-analysts", "unrelated"])
    assert error(client=gina, sql="SELECT * FROM sales") == ""
    refused(detail=error(client=gina, sql="SELECT * FROM payroll"), table="payroll")
    # The same user without the group: nothing.
    refused(detail=error(client=member(server=server, user="gina"), sql="SELECT * FROM sales"), table="sales")
    # The group from another issuer does not count for a mapping that names one...
    elsewhere = member(server=server, user="gina", groups=["okta-analysts"], issuer="https://other.example")
    refused(detail=error(client=elsewhere, sql="SELECT * FROM sales"), table="sales")
    # ...but does for a mapping that names none.
    hr = member(server=server, user="hank", groups=["hr-team"], issuer="https://other.example")
    assert error(client=hr, sql="SELECT * FROM payroll") == ""
    # Group names are exact.
    upper = member(server=server, user="gina", groups=["OKTA-ANALYSTS"])
    refused(detail=error(client=upper, sql="SELECT * FROM sales"), table="sales")
    # A mapped role can be the active one.
    assert error(client=gina, sql="SET ROLE analyst") == ""
    assert error(client=gina, sql="SELECT * FROM sales") == ""


def test_a_group_never_creates_a_user(server):
    with pytest.raises(WarpError) as raised:
        member(server=server, user="not_in_store", groups=["okta-analysts"])
    assert raised.value.code == "invalid_token" and "is not defined on this server" in raised.value.detail


def test_mapped_roles_stay_in_their_tenant(server):
    # acme/r is acme's: a server-wide user with the group does not get it.
    outsider = member(server=server, user="gina", groups=["acme-staff"])
    assert "acme/r" not in str(expect_allowed(client=outsider, sql="SHOW GRANTS").to_pylist())
    # acme's user does; and a server-wide role mapped to a group it lists does not.
    amy = member(server=server, user="acme/amy", groups=["acme-staff", "okta-analysts"])
    assert error(client=amy, sql="SET ROLE r") == ""
    assert "does not hold role analyst" in error(client=amy, sql="SET ROLE analyst")


def test_only_the_administrator_manages_mappings(server, admin):
    gina = member(server=server, user="gina", groups=["okta-analysts"])
    for sql in ["CREATE GROUP MAPPING 'x' TO ROLE analyst", "DROP GROUP MAPPING 'okta-analysts'",
                "SHOW GROUP MAPPINGS"]:
        assert "only administrators manage group mappings" in error(client=gina, sql=sql), sql
    text = str(expect_allowed(client=admin, sql="SHOW GROUP MAPPINGS").to_pylist())
    assert "okta-analysts" in text and "(any)" in text, text
    # Mistakes are refused with a reason.
    for sql, why in [(f"CREATE GROUP MAPPING 'okta-analysts' ISSUER '{ISSUER}' TO ROLE analyst", "already mapped"),
                     ("CREATE GROUP MAPPING 'g' TO ROLE no_such_role", "does not exist"),
                     ("CREATE GROUP MAPPING 'g' TO ROLE PUBLIC", "everyone holds PUBLIC already"),
                     ("CREATE GROUP MAPPING 'g'", "expected TO ROLE"),
                     ("CREATE GROUP MAPPING '' TO ROLE analyst", "cannot be empty"),
                     ("DROP GROUP MAPPING 'never-mapped'", "has no such mapping")]:
        assert why in error(client=admin, sql=sql), sql


def test_dropping_a_mapping_takes_the_role_away_at_once(server, admin):
    for sql in ["CREATE ROLE temp_reader", "GRANT SELECT ON sales TO temp_reader",
                "CREATE GROUP MAPPING 'temp-group' TO ROLE temp_reader"]:
        expect_allowed(client=admin, sql=sql)
    hank = member(server=server, user="hank", groups=["temp-group"])
    assert error(client=hank, sql="SELECT * FROM sales") == ""
    # A statement running on the mapped role is stopped.
    runner = member(server=server, user="hank", groups=["temp-group"])
    running = runner.submit(sql="SELECT sum(s.amount * r.range) FROM sales s, range(1000000000000) r")
    try:
        time.sleep(1)
        assert runner.wait(statement_id=running, timeout=0.5)["state"] == "running"
        expect_allowed(client=admin, sql="DROP GROUP MAPPING 'temp-group' TO ROLE temp_reader")
        doc = runner.wait(statement_id=running, timeout=30)
        assert doc["state"] in ("failed", "cancelled"), "a statement kept running after its group mapping was dropped"
    finally:
        runner.http.delete(url=f"{runner.url}/statements/{running}", headers=runner.headers())
    refused(detail=error(client=hank, sql="SELECT * FROM sales"), table="sales")
    expect_allowed(client=admin, sql="DROP ROLE temp_reader")


# ---- End to end: a sign-in token from an identity provider (RS256) ----

@pytest.fixture
def idp(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path / "idp.pem"
    pem.write_bytes(key.public_key().public_bytes(encoding=serialization.Encoding.PEM,
                                                  format=serialization.PublicFormat.SubjectPublicKeyInfo))

    def sign_in_token(**claims) -> str:
        now = datetime.datetime.now(tz=datetime.UTC)
        return jwt.encode(payload={"iss": ISSUER, "aud": "hardcharger-test", "iat": now,
                                   "exp": now + datetime.timedelta(minutes=10), **claims},
                          key=key, algorithm="RS256")

    return {"pem": str(pem), "token": sign_in_token}


def start_with_idp(launcher, store, idp, **env) -> RbacServer:
    return start_rbac_server(launcher=launcher, store_uri=store.new_database(),
                             env={"HARDCHARGER_TOKEN_SIGNATURE_VERIFY_CERT_PATH": idp["pem"],
                                  "HARDCHARGER_TOKEN_ALLOWED_ISSUER": ISSUER,
                                  "HARDCHARGER_TOKEN_ALLOWED_AUDIENCE": "hardcharger-test", **env})


def exchange(server, subject_token: str):
    response = httpx.post(url=f"{server.base_url}/warp/v1/auth/token",
                          json={"grant_type": "token_exchange", "subject_token": subject_token})
    if response.status_code != 200:
        raise WarpError(status=response.status_code, code=response.json()["code"], detail=response.json()["detail"])
    return server.client(token=response.json()["access_token"])


def test_the_sign_in_tokens_groups_reach_the_session(launcher, store, idp):
    server = start_with_idp(launcher=launcher, store=store, idp=idp)
    admin = server.admin()
    for sql in ["CREATE TABLE sales AS SELECT 1 AS id", "CREATE ROLE analyst", "GRANT SELECT ON sales TO analyst",
                "CREATE USER gina", f"CREATE GROUP MAPPING 'okta-analysts' ISSUER '{ISSUER}' TO ROLE analyst"]:
        expect_allowed(client=admin, sql=sql)
    with_group = exchange(server=server, subject_token=idp["token"](sub="gina", role="user", groups=["okta-analysts"]))
    assert error(client=with_group, sql="SELECT * FROM sales") == "", "the group's role did not apply"
    # A single group as a plain string, and the 1.x way in: user "token".
    one = server.client(token=server.password_token(
        user="token", password=idp["token"](sub="gina", role="user", groups="okta-analysts")))
    assert error(client=one, sql="SELECT * FROM sales") == ""
    without = exchange(server=server, subject_token=idp["token"](sub="gina", role="user", groups=["other"]))
    refused(detail=error(client=without, sql="SELECT * FROM sales"), table="sales")
    # Under access control a sign-in token signs in only the store's users.
    with pytest.raises(WarpError) as stranger:
        exchange(server=server, subject_token=idp["token"](sub="stranger", role="user", groups=["okta-analysts"]))
    assert stranger.value.code == "invalid_credentials" and "is not defined on this server" in stranger.value.detail


def test_the_groups_claim_can_be_named(launcher, store, idp):
    server = start_with_idp(launcher=launcher, store=store, idp=idp, HARDCHARGER_TOKEN_GROUPS_CLAIM="roles")
    admin = server.admin()
    for sql in ["CREATE TABLE sales AS SELECT 1 AS id", "CREATE ROLE analyst", "GRANT SELECT ON sales TO analyst",
                "CREATE USER gina", "CREATE GROUP MAPPING 'from-roles' TO ROLE analyst"]:
        expect_allowed(client=admin, sql=sql)
    renamed = exchange(server=server, subject_token=idp["token"](sub="gina", role="user", roles=["from-roles"]))
    assert error(client=renamed, sql="SELECT * FROM sales") == ""
    ignored = exchange(server=server, subject_token=idp["token"](sub="gina", role="user", groups=["from-roles"]))
    refused(detail=error(client=ignored, sql="SELECT * FROM sales"), table="sales")
