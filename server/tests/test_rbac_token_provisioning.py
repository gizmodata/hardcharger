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

from rbac import WarpError, expect_allowed, expect_blocked, start_rbac_server

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
    response = httpx.post(url=f"{server.base_url}/warp/v1/auth/token",
                          json={"grant_type": "token_exchange", "subject_token": subject_token}, timeout=120)
    if response.status_code != 200:
        body = response.json()
        raise WarpError(status=response.status_code, code=body["code"], detail=body["detail"])
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
    with pytest.raises(WarpError) as refused:
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
    again = exchange(server=server, subject_token=idp["token"](email="pat@example.com", role="user"))
    assert again.run(sql="SELECT count(*) AS n FROM sales").to_pylist() == [{"n": 1}]
    assert list(users(admin=admin)).count("pat@example.com") == 1
    # No password was set, so there is no password sign-in.
    response = httpx.post(url=f"{server.base_url}/warp/v1/auth/token",
                          json={"grant_type": "password", "username": "pat@example.com", "password": ""}, timeout=60)
    assert response.status_code in (400, 401), response.text


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
    with pytest.raises(WarpError) as refused:
        rita.run(sql="INSERT INTO sales VALUES (2, 200)")
    assert READONLY in refused.value.detail, refused.value
    # Provisioned readonly, it stays readonly when a later token says "user".
    later = exchange(server=server, subject_token=idp["token"](email="rita@example.com", role="user"))
    with pytest.raises(WarpError) as still:
        later.run(sql="INSERT INTO sales VALUES (2, 200)")
    assert READONLY in still.value.detail, still.value
    # Nor can it leave readonly through a scoped token (one signs in as a user): a readonly
    # session changes nothing in the access-control store, and only reads it.
    with pytest.raises(WarpError) as minted:
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
    with pytest.raises(WarpError) as refused:
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
    with pytest.raises(WarpError) as refused:
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
    with pytest.raises(WarpError) as refused:
        exchange(server=server, subject_token=idp["token"](issuer=OTHER_ISSUER, email="eve@example.com", role="user"))
    assert refused.value.code == "invalid_credentials" and NOT_DEFINED in refused.value.detail, refused.value
    assert "eve@example.com" not in users(admin=server.admin())
    exchange(server=server, subject_token=idp["token"](email="eve@example.com", role="user"))
    assert "eve@example.com" in users(admin=server.admin())


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
