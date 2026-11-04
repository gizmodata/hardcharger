"""hardcharger-server --verify-credentials: a one-shot decision on a credential with the
server's own sign-in code and configuration, writing nothing. A control plane runs it with a
stopped server's configuration and secrets to learn whether a credential would sign in before
it starts the server (GizmoData Cloud's auto-resume).

stdin: one JSON object as a client sends it to /chargewire/v1/auth/token. stdout: one JSON line.
Exit 0 verified, 1 refused, 2 bad input, 3 undecided (the store unreachable or not initialized,
a configuration problem). The store is opened read-only: never claimed, created, upgraded or
written (a user a sign-in token would provision included), and the credential never appears in
the output or the log.
"""

import datetime
import hashlib
import json
import pathlib
import re
import subprocess

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from conftest import BINARY, TIMEOUT_SCALE, clean_environment
from rbac import SECRET_KEY, STORE_KIND, expect_allowed, start_rbac_server

pytestmark = pytest.mark.rbac_store

ADMIN_PASSWORD = "admin-s3cret:p@ss"
CLUSTER_A = "11111111-1111-4111-8111-111111111111"
CLUSTER_B = "22222222-2222-4222-8222-222222222222"
CLUSTER_PROBE = "33333333-3333-4333-8333-333333333333"
ISSUER = "https://idp.example.test"
AUDIENCE = "hardcharger-test"


def verify(credential: dict, env: dict[str, str] | None = None, secrets: tuple[str, ...] = ()) -> tuple[int, dict, str]:
    """Runs the check; returns (exit code, the one JSON answer, stderr). Every secret the test
    handed over must stay out of both streams."""
    run = subprocess.run(args=[str(BINARY), "--verify-credentials"],
                         env={**clean_environment(), "HARDCHARGER_ADMIN_USERNAME": "admin",
                              "HARDCHARGER_ADMIN_PASSWORD": ADMIN_PASSWORD, **(env or {})},
                         input=json.dumps(credential), capture_output=True, text=True, timeout=120 * TIMEOUT_SCALE)
    lines = run.stdout.splitlines()
    assert len(lines) == 1, f"stdout must be exactly one line: {run.stdout!r}\n{run.stderr}"
    answer = json.loads(lines[0])
    for secret in (ADMIN_PASSWORD, *secrets):
        assert secret not in run.stdout and secret not in run.stderr, f"a credential leaked: {run.stdout}\n{run.stderr}"
    return run.returncode, answer, run.stderr


def password(username: str, password: str) -> dict:
    return {"grant_type": "password", "username": username, "password": password}


def exchange(token: str) -> dict:
    return {"grant_type": "token_exchange", "subject_token": token}


def store_env(uri: str, **extra: str) -> dict[str, str]:
    return {"HARDCHARGER_RBAC_STORE_URI": uri, "HARDCHARGER_SECRET_KEY": SECRET_KEY,
            "HARDCHARGER_ALLOW_CROSS_INSTANCE_TOKENS": "true", "HARDCHARGER_CLUSTER_ID": CLUSTER_A, **extra}


def fingerprint(store_uri: str):
    """Everything a write would change, read without the server: PostgreSQL's meta rows
    (schema_version, data_version, cluster_id) and user names; a DuckDB store's file bytes
    (the Python duckdb package reads no DuckDB 2.0 file, and a byte-identical file is the
    stronger proof anyway) and whether a WAL appeared."""
    if store_uri.startswith("duckdb:"):
        file = pathlib.Path(store_uri.removeprefix("duckdb:"))
        return hashlib.sha256(file.read_bytes()).hexdigest(), file.with_name(file.name + ".wal").exists()
    with psycopg.connect(conninfo=store_uri) as connection:
        rows = dict(connection.execute(query="SELECT key, value FROM hardcharger_rbac.meta").fetchall())
        names = sorted(row[0] for row in connection.execute(query="SELECT name FROM hardcharger_rbac.users").fetchall())
        return rows, names


def claimed_by(store_uri: str) -> str | None:
    """The cluster the store belongs to, learned from the check itself: a check under a
    cluster id the store does not belong to is undecided and names the owner."""
    code, answer, _ = verify(credential=password(username="admin", password=ADMIN_PASSWORD),
                             env=store_env(store_uri, HARDCHARGER_CLUSTER_ID=CLUSTER_PROBE))
    if code == 0:
        return None
    assert code == 3 and answer["reason"] == "store_belongs_to_another_cluster", answer
    found = re.search(pattern=r"belongs to cluster ([0-9a-f-]{36})", string=answer["message"])
    assert found, answer
    return found.group(1)


def schema_exists(store_uri: str) -> bool:
    if store_uri.startswith("duckdb:"):
        return pathlib.Path(store_uri.removeprefix("duckdb:")).exists()
    with psycopg.connect(conninfo=store_uri) as connection:
        return connection.execute(
            query="SELECT 1 FROM information_schema.schemata WHERE schema_name = 'hardcharger_rbac'").fetchone() is not None


# ---- The administrator, without a store ----

def test_administrator_password():
    code, answer, _ = verify(credential=password(username="admin", password=ADMIN_PASSWORD))
    assert code == 0 and answer == {"verified": True, "user": "admin", "role": "admin", "method": "password",
                                    "would_provision": False}, answer
    code, answer, _ = verify(credential=password(username="admin", password="wrong"), secrets=("wrong",))
    assert code == 1 and answer == {"verified": False, "reason": "invalid_credentials",
                                    "message": "the user name or password is not correct"}, answer
    # A store user's name without a store: the same refusal as the server gives.
    code, answer, _ = verify(credential=password(username="pat", password=ADMIN_PASSWORD))
    assert code == 1 and answer["reason"] == "invalid_credentials", answer


def test_bad_input_is_exit_2():
    def run(stdin: str) -> tuple[int, dict]:
        run = subprocess.run(args=[str(BINARY), "--verify-credentials"],
                             env={**clean_environment(), "HARDCHARGER_ADMIN_USERNAME": "admin",
                                  "HARDCHARGER_ADMIN_PASSWORD": ADMIN_PASSWORD},
                             input=stdin, capture_output=True, text=True, timeout=60 * TIMEOUT_SCALE)
        assert len(run.stdout.splitlines()) == 1, run.stdout
        return run.returncode, json.loads(run.stdout)

    for stdin, why in [("", "not JSON"), ("not json", "not JSON"), ("[1]", "one JSON object"),
                       ('{"username": "admin"}', "grant_type is required"),
                       ('{"grant_type": "refresh_token"}', "unknown grant_type"),
                       ('{"grant_type": "password", "username": "admin"}', "needs username and password"),
                       ('{"grant_type": "token_exchange"}', "needs subject_token")]:
        code, answer = run(stdin=stdin)
        assert code == 2 and answer["verified"] is False and answer["reason"] == "bad_input", (stdin, answer)
        assert why in answer["message"], (stdin, answer)
    # A certificate is the TLS connection's to verify: not refused, not decided.
    code, answer = run(stdin='{"grant_type": "client_certificate"}')
    assert code == 3 and answer["reason"] == "unsupported_grant", answer


def test_the_answer_is_stdout_and_the_log_is_stderr(tmp_path):
    # log_file in the server's configuration is ignored: a Job reads stderr.
    log_file = tmp_path / "server.log"
    code, answer, err = verify(credential=password(username="admin", password=ADMIN_PASSWORD),
                               env={"HARDCHARGER_LOG_FILE": str(log_file), "HARDCHARGER_LOG_FORMAT": "json"})
    assert code == 0 and answer["verified"] is True, answer
    assert not log_file.exists()
    records = [json.loads(line) for line in err.splitlines() if line.startswith("{")]
    assert any(record.get("action") == "verify-credentials" for record in records), err


# ---- Store users ----

@pytest.fixture
def stopped_cluster(launcher, store):
    """A store a server under cluster_id CLUSTER_A created users in, then stopped: the
    situation the check is for."""
    uri = store.new_database()
    server = start_rbac_server(launcher=launcher, store_uri=uri, env={"HARDCHARGER_CLUSTER_ID": CLUSTER_A})
    admin = server.admin()
    for sql in ["CREATE USER pat WITH PASSWORD 'pat-pw:1'", "CREATE USER sam WITH PASSWORD 'sam-pw:1'",
                "ALTER USER sam DISABLE", "CREATE USER nopw"]:
        expect_allowed(client=admin, sql=sql)
    server.running.stop()
    return uri


def test_store_user_password(stopped_cluster):
    uri = stopped_cluster
    before = fingerprint(store_uri=uri)
    code, answer, _ = verify(credential=password(username="pat", password="pat-pw:1"), env=store_env(uri),
                             secrets=("pat-pw:1",))
    assert code == 0 and answer == {"verified": True, "user": "pat", "role": "user", "method": "password",
                                    "would_provision": False}, answer
    # Names compare ignoring ASCII case, as at sign-in.
    code, answer, _ = verify(credential=password(username="PAT", password="pat-pw:1"), env=store_env(uri),
                             secrets=("pat-pw:1",))
    assert code == 0 and answer["user"] == "pat", answer
    for name, pw in [("pat", "wrong"), ("sam", "sam-pw:1"), ("nopw", "anything"), ("nobody", "nobody-pw:9")]:
        code, answer, _ = verify(credential=password(username=name, password=pw), env=store_env(uri), secrets=(pw,))
        assert code == 1 and answer == {"verified": False, "reason": "invalid_credentials",
                                        "message": "the user name or password is not correct"}, (name, answer)
    # The administrator signs in with a store too.
    code, answer, _ = verify(credential=password(username="admin", password=ADMIN_PASSWORD), env=store_env(uri))
    assert code == 0 and answer["role"] == "admin", answer
    assert fingerprint(store_uri=uri) == before, "the check wrote to the store"


def test_another_clusters_store_is_undecided(stopped_cluster):
    uri = stopped_cluster
    code, answer, _ = verify(credential=password(username="pat", password="pat-pw:1"),
                             env=store_env(uri, HARDCHARGER_CLUSTER_ID=CLUSTER_B), secrets=("pat-pw:1",))
    assert code == 3 and answer["reason"] == "store_belongs_to_another_cluster", answer
    assert CLUSTER_A in answer["message"], answer
    # Shared stores are for sharing: decided.
    code, answer, _ = verify(credential=password(username="pat", password="pat-pw:1"),
                             env=store_env(uri, HARDCHARGER_CLUSTER_ID=CLUSTER_B, HARDCHARGER_RBAC_STORE_SHARED="true"),
                             secrets=("pat-pw:1",))
    assert code == 0, answer
    assert claimed_by(store_uri=uri) == CLUSTER_A


def test_an_unclaimed_store_stays_unclaimed(launcher, store):
    uri = store.new_database()
    server = start_rbac_server(launcher=launcher, store_uri=uri)  # no cluster_id: the store is nobody's
    expect_allowed(client=server.admin(), sql="CREATE USER pat WITH PASSWORD 'pat-pw:1'")
    server.running.stop()
    assert claimed_by(store_uri=uri) is None
    before = fingerprint(store_uri=uri)
    code, answer, _ = verify(credential=password(username="pat", password="pat-pw:1"),
                             env=store_env(uri, HARDCHARGER_CLUSTER_ID=CLUSTER_B), secrets=("pat-pw:1",))
    assert code == 0, answer
    assert claimed_by(store_uri=uri) is None, "a read-only check claimed the store"
    assert fingerprint(store_uri=uri) == before


def test_an_uninitialized_store_is_undecided_and_left_alone(store):
    uri = store.new_database()  # no server has opened it
    code, answer, _ = verify(credential=password(username="pat", password="x"), env=store_env(uri))
    assert code == 3 and answer["reason"] == "store_uninitialized", answer
    assert "has not been initialized" in answer["message"], answer
    assert not schema_exists(store_uri=uri), "the check created the schema"


def test_an_unreachable_store_is_undecided():
    if STORE_KIND == "duckdb":
        uri, reason = "duckdb:/nonexistent-directory/rbac.duckdb", "configuration"
    else:
        uri, reason = "postgresql://hardcharger:hardcharger@127.0.0.1:1/hardcharger", "store_unavailable"
    code, answer, _ = verify(credential=password(username="pat", password="x"),
                             env=store_env(uri, HARDCHARGER_RBAC_STORE_TIMEOUT_SECONDS="3"))
    assert code == 3 and answer["reason"] == reason, answer
    assert "hardcharger:hardcharger" not in json.dumps(answer), "the store's credentials leaked"


# ---- Sign-in tokens (an identity provider's, or GizmoData Cloud's) ----

@pytest.fixture(scope="module")
def idp(tmp_path_factory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path_factory.mktemp("idp") / "idp.pem"
    pem.write_bytes(key.public_key().public_bytes(encoding=serialization.Encoding.PEM,
                                                  format=serialization.PublicFormat.SubjectPublicKeyInfo))

    def sign_in_token(minutes: int = 10, **claims) -> str:
        now = datetime.datetime.now(tz=datetime.UTC)
        return jwt.encode(payload={"iss": ISSUER, "aud": AUDIENCE, "iat": now,
                                   "exp": now + datetime.timedelta(minutes=minutes), **claims},
                          key=key, algorithm="RS256")

    return {"pem": str(pem), "token": sign_in_token}


def idp_env(idp, **extra: str) -> dict[str, str]:
    return {"HARDCHARGER_TOKEN_SIGNATURE_VERIFY_CERT_PATH": idp["pem"], "HARDCHARGER_TOKEN_ALLOWED_AUDIENCE": AUDIENCE,
            "HARDCHARGER_TOKEN_ALLOWED_ISSUER": ISSUER, **extra}


def test_sign_in_token_without_a_store(idp):
    token = idp["token"](sub="ana@example.test", role="admin")
    code, answer, _ = verify(credential=exchange(token), env=idp_env(idp), secrets=(token,))
    assert code == 0 and answer == {"verified": True, "user": "ana@example.test", "role": "admin", "method": "token",
                                    "would_provision": False}, answer
    expired = idp["token"](sub="ana@example.test", role="admin", minutes=-1)
    code, answer, _ = verify(credential=exchange(expired), env=idp_env(idp), secrets=(expired,))
    assert code == 1 and answer["reason"] == "invalid_credentials" and "not accepted" in answer["message"], answer
    forged = jwt.encode(payload={"iss": ISSUER, "aud": AUDIENCE, "sub": "ana@example.test", "role": "admin",
                                 "exp": datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(minutes=5)},
                        key="not-the-key-0123456789abcdef0123456789", algorithm="HS256")
    code, answer, _ = verify(credential=exchange(forged), env=idp_env(idp), secrets=(forged,))
    assert code == 1 and answer["reason"] == "invalid_credentials", answer
    # The same token as the password of user "token", as clients that only send a password do.
    code, answer, _ = verify(credential=password(username="token", password=token), env=idp_env(idp), secrets=(token,))
    assert code == 0 and answer["user"] == "ana@example.test" and answer["method"] == "token", answer
    # Who the issuer may sign in (token_authorized_emails).
    code, answer, _ = verify(credential=exchange(token),
                             env=idp_env(idp, HARDCHARGER_TOKEN_AUTHORIZED_EMAILS="*@other.test"), secrets=(token,))
    assert code == 1 and "not authorized" in answer["message"], answer


def test_sign_in_token_with_a_store(stopped_cluster, idp):
    uri = stopped_cluster
    env = {**store_env(uri), **idp_env(idp)}
    before = fingerprint(store_uri=uri)
    # A token signs in one of the store's users (the administrator role aside).
    code, answer, _ = verify(credential=exchange(idp["token"](sub="pat", role="user")), env=env)
    assert code == 0 and answer == {"verified": True, "user": "pat", "role": "user", "method": "token",
                                    "would_provision": False}, answer
    code, answer, _ = verify(credential=exchange(idp["token"](sub="pat", role="readonly")), env=env)
    assert code == 0 and answer["role"] == "readonly", answer
    code, answer, _ = verify(credential=exchange(idp["token"](sub="sam", role="user")), env=env)
    assert code == 1 and answer["reason"] == "invalid_credentials" and "disabled" in answer["message"], answer
    code, answer, _ = verify(credential=exchange(idp["token"](sub="zed", role="user")), env=env)
    assert code == 1 and "is not defined on this server" in answer["message"], answer
    code, answer, _ = verify(credential=exchange(idp["token"](sub="zed", role="admin")), env=env)
    assert code == 0 and answer["role"] == "admin", answer
    assert fingerprint(store_uri=uri) == before


def test_auto_provisioning_is_decided_without_creating_the_user(stopped_cluster, idp):
    uri = stopped_cluster
    env = {**store_env(uri), **idp_env(idp, HARDCHARGER_TOKEN_AUTO_PROVISION_USERS="true")}
    before = fingerprint(store_uri=uri)
    code, answer, err = verify(credential=exchange(idp["token"](sub="zed", role="user")), env=env)
    assert code == 0 and answer == {"verified": True, "user": "zed", "role": "user", "method": "token",
                                    "would_provision": True}, answer
    assert "would provision" in err and "provisioned a user" not in err, err
    assert fingerprint(store_uri=uri) == before, "the check created the user"
    # A user that exists is signed in as is, and a disabled one stays refused.
    code, answer, _ = verify(credential=exchange(idp["token"](sub="pat", role="user")), env=env)
    assert code == 0 and answer["would_provision"] is False, answer
    code, answer, _ = verify(credential=exchange(idp["token"](sub="sam", role="user")), env=env)
    assert code == 1, answer
    # A name no user may take (the administrator's) is refused, as the creation would be.
    code, answer, _ = verify(credential=exchange(idp["token"](sub="admin", role="user")), env=env)
    assert code == 1 and "administrator" in answer["message"], answer
    assert fingerprint(store_uri=uri) == before


# ---- Scoped tokens (CREATE SCOPED TOKEN), sent as the password of user "token" ----

@pytest.fixture
def scoped_tokens(launcher, store):
    """A stopped cluster whose user wes minted two scoped tokens and revoked one."""
    uri = store.new_database()
    server = start_rbac_server(launcher=launcher, store_uri=uri, env={"HARDCHARGER_CLUSTER_ID": CLUSTER_A})
    admin = server.admin()
    for sql in ["ATTACH ':memory:' AS sales_db", "CREATE TABLE sales_db.main.orders AS SELECT 1 AS id",
                "CREATE ROLE reader", "GRANT SELECT ON CATALOG sales_db TO reader",
                "CREATE USER wes WITH PASSWORD 'wes-pw:1'", "GRANT reader TO wes"]:
        expect_allowed(client=admin, sql=sql)
    wes = server.user(name="wes", password="wes-pw:1")
    live = expect_allowed(client=wes, sql="CREATE SCOPED TOKEN reports ALLOW SELECT ON CATALOG sales_db").to_pylist()[0]["token"]
    revoked = expect_allowed(client=wes, sql="CREATE SCOPED TOKEN gone ALLOW SELECT ON CATALOG sales_db").to_pylist()[0]["token"]
    expect_allowed(client=wes, sql="DROP SCOPED TOKEN gone")
    server.running.stop()
    return uri, live, revoked


def test_scoped_token(scoped_tokens):
    uri, live, revoked = scoped_tokens
    before = fingerprint(store_uri=uri)
    code, answer, _ = verify(credential=password(username="token", password=live), env=store_env(uri), secrets=(live,))
    assert code == 0 and answer == {"verified": True, "user": "wes", "role": "user", "method": "scoped_token",
                                    "would_provision": False}, answer
    # As a password, a revoked token gets the password refusal, as the server gives it (as a
    # bearer token it would say token_revoked).
    code, answer, _ = verify(credential=password(username="token", password=revoked), env=store_env(uri),
                             secrets=(revoked,))
    assert code == 1 and answer["reason"] == "invalid_credentials", answer
    # Signed with another key: not this server's token.
    code, answer, _ = verify(credential=password(username="token", password=live),
                             env=store_env(uri, HARDCHARGER_SECRET_KEY="another-secret-key-0123456789"), secrets=(live,))
    assert code == 1 and answer["reason"] == "invalid_credentials", answer
    assert fingerprint(store_uri=uri) == before


def test_scoped_token_bound_to_the_issuing_instance(scoped_tokens):
    """Without allow_cross_instance_tokens a token is the issuing process's only, so after a
    restart the server would refuse it: the check answers the same way."""
    uri, live, _ = scoped_tokens
    code, answer, _ = verify(credential=password(username="token", password=live),
                             env=store_env(uri, HARDCHARGER_ALLOW_CROSS_INSTANCE_TOKENS="false"), secrets=(live,))
    assert code == 1 and answer["reason"] == "invalid_credentials", answer
    # No secret_key at all: the server would sign with a key that died with it.
    env = store_env(uri)
    del env["HARDCHARGER_SECRET_KEY"]
    code, answer, err = verify(credential=password(username="token", password=live), env=env, secrets=(live,))
    assert code == 1 and answer["reason"] == "invalid_credentials", answer
    assert "no secret_key" in err, err
