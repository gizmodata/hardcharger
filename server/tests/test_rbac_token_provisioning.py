"""token_auto_provision_users: under access control, a verified sign-in token from the
identity provider (token_auto_provision_issuer, default token_allowed_issuer) creates its
user on the first sign-in, with no password and no grants. Off (the default), such a user
must already be in the store."""

import concurrent.futures
import datetime
import json

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from rbac import ChargewireError, expect_allowed, expect_blocked, start_rbac_server

ISSUER = "https://idp.example.test"
OTHER_ISSUER = "https://other-idp.example.test"
AUDIENCE = "hardcharger-test"
NOT_DEFINED = "is not defined on this server. An administrator creates it with CREATE USER"
READONLY = "has a readonly session and cannot run statements that modify state"


@pytest.fixture(scope="module")
def idp(tmp_path_factory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path_factory.mktemp("idp") / "idp.pem"
    pem.write_bytes(key.public_key().public_bytes(encoding=serialization.Encoding.PEM,
                                                  format=serialization.PublicFormat.SubjectPublicKeyInfo))

    def sign_in_token(issuer: str = ISSUER, **claims) -> str:
        now = datetime.datetime.now(tz=datetime.UTC)
        if "sub" not in claims and "email" in claims:
            claims["sub"] = claims["email"]
        return jwt.encode(payload={"iss": issuer, "aud": AUDIENCE, "iat": now,
                                   "exp": now + datetime.timedelta(minutes=10), **claims},
                          key=key, algorithm="RS256")

    return {"pem": str(pem), "token": sign_in_token}


def idp_env(idp, issuer: str | None = ISSUER, **extra: str) -> dict[str, str]:
    env = {"HARDCHARGER_TOKEN_SIGNATURE_VERIFY_CERT_PATH": idp["pem"],
           "HARDCHARGER_TOKEN_ALLOWED_AUDIENCE": AUDIENCE, "HARDCHARGER_LOG_FORMAT": "json", **extra}
    if issuer is not None:
        env["HARDCHARGER_TOKEN_ALLOWED_ISSUER"] = issuer
    return env


def exchange(server, subject_token: str):
    """Signs in with the identity provider's token; a client of the session's user."""
    response = httpx.post(url=f"{server.base_url}/chargewire/v1/auth/token",
                          json={"grant_type": "token_exchange", "subject_token": subject_token}, timeout=120)
    if response.status_code != 200:
        body = response.json()
        raise ChargewireError(status=response.status_code, code=body["code"], detail=body["detail"])
    return server.client(token=response.json()["access_token"])


def users(admin) -> dict[str, dict]:
    table = admin.run(sql="SELECT name, enabled, has_password, attributes FROM hardcharger_users()")
    return {row["name"]: row for row in table.to_pylist()}


@pytest.fixture(scope="module")
def provisioning(module_launcher, store, idp):
    """A server that provisions the users of ISSUER's tokens, and its administrator."""
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               env=idp_env(idp=idp, HARDCHARGER_TOKEN_AUTO_PROVISION_USERS="true"))
    admin = server.admin()
    expect_allowed(client=admin, sql="CREATE TABLE sales AS SELECT 1 AS id, 100 AS amount")
    return server, admin


def test_off_a_token_user_the_store_lacks_is_refused(launcher, store, idp):
    server = start_rbac_server(launcher=launcher, store_uri=store.new_database(), env=idp_env(idp=idp))
    with pytest.raises(ChargewireError) as refused:
        exchange(server=server, subject_token=idp["token"](sub="newcomer@example.com", role="user"))
    assert refused.value.code == "invalid_credentials", refused.value
    assert f"user newcomer@example.com {NOT_DEFINED}" in refused.value.detail, refused.value
    assert "newcomer@example.com" not in users(admin=server.admin())


def test_on_the_first_sign_in_creates_the_user_with_no_grants(provisioning, idp):
    server, admin = provisioning
    assert "pat@example.com" not in users(admin=admin)
    pat = exchange(server=server, subject_token=idp["token"](sub="ignored-sub", email="pat@example.com", role="user"))
    created = users(admin=admin)["pat@example.com"]
    assert created["enabled"] is True and created["has_password"] is False, created
    assert "provisioned_role" in str(created["attributes"]) and "user" in str(created["attributes"]), created
    # No grants: reading a table is refused with the permission message, until a GRANT.
    expect_blocked(client=pat, sql="SELECT * FROM sales", why="no SELECT privilege on table")
    for sql in ["CREATE ROLE sales_reader", "GRANT SELECT ON sales TO sales_reader",
                'GRANT sales_reader TO USER "pat@example.com"']:
        expect_allowed(client=admin, sql=sql)
    assert pat.run(sql="SELECT amount FROM sales").to_pylist() == [{"amount": 100}]
    # The next sign-in finds the user (nothing created twice), with its grant.
    again = exchange(server=server, subject_token=idp["token"](sub="ignored-sub", email="pat@example.com", role="user"))
    assert again.run(sql="SELECT count(*) AS n FROM sales").to_pylist() == [{"n": 1}]
    assert list(users(admin=admin)).count("pat@example.com") == 1
    # No password was set, so there is no password sign-in.
    response = httpx.post(url=f"{server.base_url}/chargewire/v1/auth/token",
                          json={"grant_type": "password", "username": "pat@example.com", "password": ""}, timeout=60)
    assert response.status_code in (400, 401), response.text


def test_password_and_sso_share_one_user_across_restart(launcher, store, idp):
    uri = store.new_database()
    env = idp_env(idp=idp, HARDCHARGER_TOKEN_AUTO_PROVISION_USERS="true")
    server = start_rbac_server(launcher=launcher, store_uri=uri, env=env)
    admin = server.admin()
    name = "dual@example.test"
    token = idp["token"](email=name, sub="provider-user-007", role="user")
    sso = exchange(server=server, subject_token=token)
    identity = "SELECT idp_issuer, idp_subject FROM hardcharger_users() WHERE has_password AND has_sso_link"
    assert admin.run(sql=identity).num_rows == 0
    assert sso.run(sql="SELECT idp_subject FROM hardcharger_users() WHERE is_current").to_pylist() == [{"idp_subject": None}]
    admin.run(sql=f"""ALTER USER "{name}" WITH PASSWORD 'local-test-password'""")
    server.running.stop()

    server = start_rbac_server(launcher=launcher, store_uri=uri, env=env)
    admin = server.admin()
    assert admin.run(sql=identity).to_pylist() == [{"idp_issuer": ISSUER, "idp_subject": "provider-user-007"}]
    method = "SELECT auth_method FROM hardcharger_sessions() WHERE is_current"
    assert server.user(name=name, password="local-test-password").run(sql=method).to_pylist() == [{"auth_method": "password"}]
    assert exchange(server=server, subject_token=token).run(sql=method).to_pylist() == [{"auth_method": "token"}]

    admin.run(sql="""CREATE USER "local@example.test" WITH PASSWORD 'original-password'""")
    admin.run(sql=f"""ALTER USER "local@example.test" SET IDENTITY PROVIDER '{ISSUER}' SUBJECT 'local-subject'""")
    local = server.user(name="local@example.test", password="original-password")
    assert local.run(sql=method).to_pylist() == [{"auth_method": "password"}]
    linked_token = idp["token"](email="local@example.test", sub="local-subject", role="user")
    assert exchange(server=server, subject_token=linked_token).run(sql=method).to_pylist() == [{"auth_method": "token"}]
    with pytest.raises(ChargewireError, match="identity does not match"):
        exchange(server=server, subject_token=idp["token"](email=name, sub="different", role="user"))
    assert admin.run(sql=identity).num_rows == 2


def test_the_user_name_is_the_subject_without_an_email(provisioning, idp):
    server, admin = provisioning
    exchange(server=server, subject_token=idp["token"](sub="svc-loader", role="user"))
    assert "svc-loader" in users(admin=admin)


def test_a_readonly_token_provisions_a_readonly_user(provisioning, idp):
    server, admin = provisioning
    rita = exchange(server=server, subject_token=idp["token"](email="rita@example.com", role="readonly"))
    for sql in ["CREATE ROLE sales_writer", "GRANT SELECT, INSERT, DELETE ON sales TO sales_writer",
                'GRANT sales_writer TO USER "rita@example.com"']:
        expect_allowed(client=admin, sql=sql)
    assert rita.run(sql="SELECT count(*) AS n FROM sales").to_pylist() == [{"n": 1}]
    with pytest.raises(ChargewireError) as refused:
        rita.run(sql="INSERT INTO sales VALUES (2, 200)")
    assert READONLY in refused.value.detail, refused.value
    # Provisioned readonly, it stays readonly when a later token says "user".
    later = exchange(server=server, subject_token=idp["token"](email="rita@example.com", role="user"))
    with pytest.raises(ChargewireError) as still:
        later.run(sql="INSERT INTO sales VALUES (2, 200)")
    assert READONLY in still.value.detail, still.value
    # Nor can it leave readonly through a scoped token (one signs in as a user): a readonly
    # session changes nothing in the access-control store, and only reads it.
    with pytest.raises(ChargewireError) as minted:
        later.run(sql="CREATE SCOPED TOKEN escape ALLOW SELECT, INSERT")
    assert minted.value.code == "permission_denied", minted.value
    assert "has a readonly session and cannot run CREATE SCOPED TOKEN" in minted.value.detail, minted.value
    assert later.run(sql='SHOW GRANTS FOR "rita@example.com"').num_rows >= 1
    # Until an administrator lifts it.
    expect_allowed(client=admin, sql='ALTER USER "rita@example.com" UNSET ATTRIBUTE provisioned_role')
    writer = exchange(server=server, subject_token=idp["token"](email="rita@example.com", role="user"))
    expect_allowed(client=writer, sql="INSERT INTO sales VALUES (2, 200)")
    expect_allowed(client=writer, sql="DELETE FROM sales WHERE id = 2")


def test_an_admin_token_is_unaffected(provisioning, idp):
    server, admin = provisioning
    boss = exchange(server=server, subject_token=idp["token"](email="boss@example.com", role="admin"))
    assert boss.run(sql="SELECT count(*) AS n FROM sales").to_pylist() == [{"n": 1}]
    assert "boss@example.com" not in users(admin=admin)


def test_a_disabled_user_is_not_re_enabled(provisioning, idp):
    server, admin = provisioning
    for sql in ['CREATE USER "dora@example.com"', 'ALTER USER "dora@example.com" DISABLE']:
        expect_allowed(client=admin, sql=sql)
    with pytest.raises(ChargewireError) as refused:
        exchange(server=server, subject_token=idp["token"](email="dora@example.com", role="user"))
    assert refused.value.code == "invalid_credentials" and "is disabled" in refused.value.detail, refused.value
    dora = users(admin=admin)["dora@example.com"]
    assert dora["enabled"] is False and "provisioned_role" not in str(dora["attributes"]), dora
    # Refused as any sign-in of a disabled user is: no provisioning was tried.
    assert not [r for r in server.running.log_records()
                if "provision" in r.get("message", "") and r.get("user") == "dora@example.com"]


def test_a_name_that_is_a_role_is_refused(provisioning, idp):
    server, admin = provisioning
    expect_allowed(client=admin, sql="CREATE ROLE analysts")
    with pytest.raises(ChargewireError) as refused:
        exchange(server=server, subject_token=idp["token"](sub="analysts", role="user"))
    assert refused.value.code == "invalid_credentials" and "a role named analysts already exists" in refused.value.detail
    assert "analysts" not in users(admin=admin)
    records = [r for r in server.running.log_records()
               if r.get("message") == "refused to provision a user from a sign-in token" and r.get("user") == "analysts"]
    assert len(records) == 1 and records[0]["status"] == "rejected" and records[0]["issuer"] == ISSUER, records
    assert records[0].get("audit_hash"), records


def test_each_provisioning_is_audited(provisioning, idp):
    server, _ = provisioning
    exchange(server=server, subject_token=idp["token"](email="audited@example.com", role="user"))
    exchange(server=server, subject_token=idp["token"](email="audited@example.com", role="user"))
    records = [r for r in server.running.log_records()
               if r.get("message") == "provisioned a user on its first sign-in" and r.get("user") == "audited@example.com"]
    assert len(records) == 1, records  # the first sign-in only
    record = records[0]
    assert record["issuer"] == ISSUER and record["kind"] == "access_control" and record["status"] == "success", record
    assert record["statement"] == "CREATE USER" and record["role"] == "user", record
    assert record.get("audit_hash") and record.get("audit_chain"), f"not in the audit chain: {json.dumps(record)}"


def test_only_the_provisioning_issuers_tokens_create_users(launcher, store, idp):
    # Tokens from any issuer verify (no token_allowed_issuer); only ISSUER's provision.
    server = start_rbac_server(launcher=launcher, store_uri=store.new_database(),
                               env=idp_env(idp=idp, issuer=None, HARDCHARGER_TOKEN_AUTO_PROVISION_USERS="true",
                                           HARDCHARGER_TOKEN_AUTO_PROVISION_ISSUER=ISSUER))
    with pytest.raises(ChargewireError) as refused:
        exchange(server=server, subject_token=idp["token"](issuer=OTHER_ISSUER, email="eve@example.com", role="user"))
    assert refused.value.code == "invalid_credentials" and NOT_DEFINED in refused.value.detail, refused.value
    assert "eve@example.com" not in users(admin=server.admin())
    exchange(server=server, subject_token=idp["token"](email="eve@example.com", role="user"))
    assert "eve@example.com" in users(admin=server.admin())


def test_first_match_on_two_replicas_records_one_identity(launcher, pg_store, idp):
    uri = pg_store.new_database()
    env = idp_env(idp=idp, issuer=None)
    first = start_rbac_server(launcher=launcher, store_uri=uri, env=env)
    admin = first.admin()
    admin.run(sql="CREATE USER reader")
    admin.run(sql="CREATE USER other_reader")
    second = start_rbac_server(launcher=launcher, store_uri=uri, env=env)
    for claims in ({"issuer": ""}, {"sub": ""}):
        with pytest.raises(ChargewireError, match="issuer and subject"):
            exchange(server=first, subject_token=idp["token"](email="reader", role="user", **claims))
    account = "SELECT idp_issuer, idp_subject FROM hardcharger_users() WHERE name = 'reader'"
    assert first.admin().run(sql=account).to_pylist() == [{"idp_issuer": None, "idp_subject": None}]

    def sign_in(replica, issuer):
        try:
            exchange(server=replica, subject_token=idp["token"](issuer=issuer, email="reader", email_verified=True,
                                                                sub="123", role="user"))
        except ChargewireError as error:
            assert error.code == "invalid_credentials" and "identity does not match" in error.detail
            return None
        return issuer

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(sign_in, first, ISSUER), pool.submit(sign_in, second, OTHER_ISSUER)]
        winners = [issuer for job in jobs if (issuer := job.result()) is not None]
    assert len(winners) == 1
    for replica in (first, second):
        assert sign_in(replica, winners[0]) == winners[0]
        assert replica.admin().run(sql=account).to_pylist() == [{"idp_issuer": winners[0], "idp_subject": "123"}]
    with pytest.raises(ChargewireError, match="already linked to another user"):
        exchange(server=first, subject_token=idp["token"](issuer=winners[0], email="other_reader", email_verified=True,
                                                          sub="123", role="user"))
    assert admin.run(sql="SELECT has_sso_link FROM hardcharger_users() WHERE name = 'other_reader'").column(0)[0].as_py() is False
    links = [r for replica in (first, second) for r in replica.running.log_records()
             if r.get("message") == "linked an existing user on its first SSO sign-in"]
    assert len(links) == 1 and links[0]["issuer"] == winners[0] and links[0].get("audit_hash")


def test_first_sign_ins_on_two_replicas_at_once_all_succeed(launcher, pg_store, idp):
    uri = pg_store.new_database()
    env = idp_env(idp=idp, HARDCHARGER_TOKEN_AUTO_PROVISION_USERS="true")
    replicas = [start_rbac_server(launcher=launcher, store_uri=uri, env=env) for _ in range(2)]
    names = [f"crowd{i}@example.com" for i in range(6)]
    # Each user signs in for the first time on both replicas at once, three times over.
    jobs = [(replica, name) for name in names for replica in replicas for _ in range(3)]

    def sign_in(job):
        replica, name = job
        client = exchange(server=replica, subject_token=idp["token"](email=name, role="user"))
        return client.run(sql="SELECT 1 AS one").to_pylist()

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        results = list(pool.map(sign_in, jobs))
    assert results == [[{"one": 1}]] * len(jobs)
    listed = users(admin=replicas[0].admin())
    assert set(names) <= set(listed)
    created = [r for replica in replicas for r in replica.running.log_records()
               if r.get("message") == "provisioned a user on its first sign-in"]
    assert sorted(r["user"] for r in created) == sorted(names), "each user is created exactly once"


NO_MATCH = "the sign-in identity does not match an enabled user"
UNLINKED = {"idp_issuer": None, "idp_subject": None}


def pair(admin, name: str) -> dict:
    """The user's recorded SSO identity (issuer and subject, NULL when unlinked)."""
    table = admin.run(sql=f"SELECT idp_issuer, idp_subject FROM hardcharger_users() WHERE name = '{name}'")
    return table.to_pylist()[0]


def signs_in(server, token: str) -> bool:
    """The token signs in, and its session runs a query."""
    return exchange(server=server, subject_token=token).run(sql="SELECT 1 AS one").to_pylist() == [{"one": 1}]


def no_match(server, token: str) -> None:
    with pytest.raises(ChargewireError) as refused:
        exchange(server=server, subject_token=token)
    assert refused.value.code == "invalid_credentials" and NO_MATCH in refused.value.detail, refused.value


@pytest.fixture
def linking(launcher, store, idp):
    """A server that trusts ISSUER and OTHER_ISSUER, and its administrator."""
    server = start_rbac_server(launcher=launcher, store_uri=store.new_database(), env=idp_env(idp=idp, issuer=None))
    return server, server.admin()


def test_only_a_verified_email_records_the_first_match(linking, idp):
    server, admin = linking
    admin.run(sql="""CREATE USER "owner@example.com" WITH PASSWORD 'owner-password-1'""")
    admin.run(sql='CREATE USER "strings@example.com"')
    # Another account of the provider, whose e-mail claim is the owner's name but unverified:
    # it signs in as 2.13 let it, and records nothing, so the owner can still link.
    for verified in ({}, {"email_verified": False}, {"email_verified": "false"}, {"email_verified": "TRUE"},
                     {"email_verified": 1}):
        token = idp["token"](email="owner@example.com", sub="not-the-owner", role="user", **verified)
        assert signs_in(server=server, token=token), verified
        assert pair(admin=admin, name="owner@example.com") == UNLINKED, verified
    assert not [r for r in server.running.log_records() if r.get("message") == "linked an existing user on its first SSO sign-in"]
    # The owner's verified sign-in records its pair; from then on the other account is refused.
    owner = idp["token"](email="owner@example.com", email_verified=True, sub="owner-001", role="user")
    assert signs_in(server=server, token=owner)
    assert pair(admin=admin, name="owner@example.com") == {"idp_issuer": ISSUER, "idp_subject": "owner-001"}
    no_match(server=server, token=idp["token"](email="owner@example.com", sub="not-the-owner", role="user"))
    no_match(server=server, token=idp["token"](email="owner@example.com", email_verified=True, sub="not-the-owner",
                                               role="user"))
    assert signs_in(server=server, token=owner)  # the twin: the recorded pair
    assert server.user(name="owner@example.com", password="owner-password-1").run(sql="SELECT 1").num_rows == 1
    # Providers that send claims as strings: "true" verifies.
    assert signs_in(server=server, token=idp["token"](email="strings@example.com", email_verified="true",
                                                      sub="strings-001", role="user"))
    assert pair(admin=admin, name="strings@example.com") == {"idp_issuer": ISSUER, "idp_subject": "strings-001"}
    links = [r["user"] for r in server.running.log_records()
             if r.get("message") == "linked an existing user on its first SSO sign-in"]
    assert links == ["owner@example.com", "strings@example.com"], links


def test_a_subject_that_is_the_user_name_records_the_first_match(linking, idp):
    server, admin = linking
    for name in ("svc_reader", "svc_case", "both@example.com"):
        admin.run(sql=f'CREATE USER "{name}"')
    # No e-mail claim: the user is named by sub, which is the user's name exactly.
    assert signs_in(server=server, token=idp["token"](sub="svc_reader", role="user"))
    assert pair(admin=admin, name="svc_reader") == {"idp_issuer": ISSUER, "idp_subject": "svc_reader"}
    assert signs_in(server=server, token=idp["token"](sub="svc_reader", role="user"))
    no_match(server=server, token=idp["token"](issuer=OTHER_ISSUER, sub="svc_reader", role="user"))
    # A sub that names the user only ignoring case signs in (user names compare that way) and
    # records nothing: a subject is the provider's exact identifier.
    assert signs_in(server=server, token=idp["token"](sub="SVC_CASE", role="user"))
    assert pair(admin=admin, name="svc_case") == UNLINKED
    # An unverified e-mail claim names the user, even when sub spells the same.
    assert signs_in(server=server, token=idp["token"](email="both@example.com", sub="both@example.com", role="user"))
    assert pair(admin=admin, name="both@example.com") == UNLINKED


def test_unset_identity_provider_clears_a_link_and_the_owner_links_again(linking, idp):
    server, admin = linking
    name = "victim@example.com"
    admin.run(sql=f"""CREATE USER "{name}" WITH PASSWORD 'victim-password-1'""")
    admin.run(sql="CREATE ROLE helpdesk")
    admin.run(sql="GRANT ALTER USER TO helpdesk")
    admin.run(sql="CREATE USER amy WITH PASSWORD 'amy-password-1'")
    admin.run(sql="GRANT helpdesk TO amy")
    # A wrong link (an identity-provider migration, a mistaken SET): the owner is refused.
    admin.run(sql=f"""ALTER USER "{name}" SET IDENTITY PROVIDER '{ISSUER}' SUBJECT 'wrong-subject'""")
    owner = idp["token"](email=name, email_verified=True, sub="victim-001", role="user")
    no_match(server=server, token=owner)
    # Only an unrestricted administrator unlinks, as only one links.
    amy = server.user(name="amy", password="amy-password-1")
    amy.run(sql="SET ROLE helpdesk")
    for sql, verb in ((f'ALTER USER "{name}" UNSET IDENTITY PROVIDER', "unlink"),
                      (f"""ALTER USER "{name}" SET IDENTITY PROVIDER '{ISSUER}' SUBJECT 'victim-001'""", "link")):
        with pytest.raises(ChargewireError) as refused:
            amy.run(sql=sql)
        assert f"only an unrestricted administrator can {verb} an SSO identity" in refused.value.detail, refused.value
    assert pair(admin=admin, name=name) == {"idp_issuer": ISSUER, "idp_subject": "wrong-subject"}
    assert admin.run(sql=f'ALTER USER "{name}" UNSET IDENTITY PROVIDER').to_pylist() == [
        {"result": f"SSO identity unlinked from user {name}"}]
    assert pair(admin=admin, name=name) == UNLINKED
    linked = f"SELECT has_sso_link FROM hardcharger_users() WHERE name = '{name}'"
    assert admin.run(sql=linked).column(0)[0].as_py() is False
    assert admin.run(sql=f'ALTER USER "{name}" UNSET IDENTITY PROVIDER').to_pylist() == [
        {"result": f"User {name} has no SSO identity"}]
    # The owner's next verified sign-in records its own pair.
    assert signs_in(server=server, token=owner)
    assert pair(admin=admin, name=name) == {"idp_issuer": ISSUER, "idp_subject": "victim-001"}
    assert server.user(name=name, password="victim-password-1").run(sql="SELECT 1").num_rows == 1
    changes = [r.get("change") for r in server.running.log_records()
               if r.get("statement") == "ALTER USER" and r.get("status") == "success" and r.get("audit_hash")]
    assert changes == ["SSO identity linked", "SSO identity unlinked", "SSO identity unlinked"], changes


def test_set_identity_provider_replaces_a_link(linking, idp):
    server, admin = linking
    for name in ("mover@example.com", "keeper@example.com"):
        admin.run(sql=f'CREATE USER "{name}"')
    old = idp["token"](email="mover@example.com", email_verified=True, sub="old-subject", role="user")
    assert signs_in(server=server, token=old)
    assert signs_in(server=server, token=idp["token"](email="keeper@example.com", email_verified=True,
                                                      sub="keeper-001", role="user"))
    # The provider changed: the administrator moves the user to its new pair.
    sql = f"""ALTER USER "mover@example.com" SET IDENTITY PROVIDER '{OTHER_ISSUER}' SUBJECT 'new-subject'"""
    assert admin.run(sql=sql).to_pylist() == [{"result": "SSO identity replaced for user mover@example.com"}]
    assert pair(admin=admin, name="mover@example.com") == {"idp_issuer": OTHER_ISSUER, "idp_subject": "new-subject"}
    no_match(server=server, token=old)
    assert signs_in(server=server, token=idp["token"](issuer=OTHER_ISSUER, email="mover@example.com",
                                                      sub="new-subject", role="user"))
    # The same pair again changes nothing.
    assert admin.run(sql=sql).to_pylist() == [{"result": "SSO identity linked to user mover@example.com"}]
    # A pair another user has is refused, and the link stays.
    with pytest.raises(ChargewireError) as refused:
        admin.run(sql=f"""ALTER USER "mover@example.com" SET IDENTITY PROVIDER '{ISSUER}' SUBJECT 'keeper-001'""")
    assert "the SSO identity is already linked to another user" in refused.value.detail, refused.value
    assert pair(admin=admin, name="mover@example.com") == {"idp_issuer": OTHER_ISSUER, "idp_subject": "new-subject"}
    assert pair(admin=admin, name="keeper@example.com") == {"idp_issuer": ISSUER, "idp_subject": "keeper-001"}
    # A sign-in with another user's pair is refused too, verified e-mail or not.
    admin.run(sql='CREATE USER "fresh@example.com"')
    no_match(server=server, token=idp["token"](email="fresh@example.com", sub="keeper-001", role="user"))
    with pytest.raises(ChargewireError, match="the SSO identity is already linked to another user"):
        exchange(server=server, subject_token=idp["token"](email="fresh@example.com", email_verified=True,
                                                           sub="keeper-001", role="user"))
    assert pair(admin=admin, name="fresh@example.com") == UNLINKED


@pytest.mark.parametrize(("env", "why"), [
    ({"HARDCHARGER_TOKEN_AUTO_PROVISION_USERS": "true", "HARDCHARGER_RBAC_STORE_URI": ""},
     "token_auto_provision_users needs rbac_store_uri"),
    ({"HARDCHARGER_TOKEN_AUTO_PROVISION_ISSUER": ISSUER}, "token_auto_provision_issuer needs token_auto_provision_users"),
    ({"HARDCHARGER_TOKEN_AUTO_PROVISION_USERS": "true", "HARDCHARGER_TOKEN_AUTO_PROVISION_ISSUER": OTHER_ISSUER},
     "is not token_allowed_issuer"),
])
def test_startup_refuses_a_setup_that_cannot_provision(launcher, store, idp, env, why):
    from rbac import rbac_env
    running = launcher.start(env={**rbac_env(store_uri=store.new_database()), **idp_env(idp=idp), **env},
                             expect_up=False)
    assert running.process.wait(timeout=60) != 0
    assert why in running.log(), running.log()


def test_startup_refuses_provisioning_without_a_way_to_verify_tokens(launcher, store):
    from rbac import rbac_env
    running = launcher.start(env={**rbac_env(store_uri=store.new_database()),
                                  "HARDCHARGER_TOKEN_AUTO_PROVISION_USERS": "true",
                                  "HARDCHARGER_TOKEN_ALLOWED_ISSUER": ISSUER},
                             expect_up=False)
    assert running.process.wait(timeout=60) != 0
    assert "token_auto_provision_users needs a way to verify sign-in tokens" in running.log(), running.log()
