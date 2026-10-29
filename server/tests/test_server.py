"""Hardcharger behaviour beyond the Chargewire conformance suite: configuration, tokens,
sign-in, roles, init SQL and operations."""

import datetime
import subprocess
import time

import httpx
import jwt
import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from conftest import BINARY


def problem(response: httpx.Response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code, body
    return body


# ---- Command line and configuration ----

def test_version_and_duckdb_version():
    out = subprocess.run(args=[str(BINARY), "--version"], capture_output=True, text=True, check=True)
    assert out.stdout.startswith("Hardcharger ")
    out = subprocess.run(args=[str(BINARY), "--print-duckdb-version"], capture_output=True, text=True,
                         check=True)
    assert out.stdout.strip().startswith("v2.")


def test_missing_password_stops_the_server():
    out = subprocess.run(args=[str(BINARY)], capture_output=True, text=True, env={"PATH": "/usr/bin"})
    assert out.returncode == 2
    assert "an administrator password is required: set HARDCHARGER_ADMIN_PASSWORD" in out.stderr


def test_unknown_flag_is_an_error():
    out = subprocess.run(args=[str(BINARY), "--no-such-option", "1"], capture_output=True, text=True,
                         env={"PATH": "/usr/bin", "HARDCHARGER_ADMIN_PASSWORD": "x"})
    assert out.returncode == 2
    assert "unknown option --no-such-option" in out.stderr


def test_yaml_config_and_precedence(launcher, tmp_path):
    config = tmp_path / "hardcharger.yaml"
    config.write_text(yaml.safe_dump({"init_sql_commands": "CREATE TABLE from_yaml AS SELECT 1 AS v",
                                      "print_queries": False, "log_level": "debug"}))
    # The environment beats the file; the flag beats the environment.
    server = launcher.start(args=["--config", str(config), "--log-level", "info"],
                            env={"HARDCHARGER_LOG_LEVEL": "error"})
    token = server.token()
    session = server.session(token=token)
    response = server.run(token=token, session_id=session, sql="SELECT v FROM from_yaml")
    assert response.status_code in (200, 201), response.text
    assert "level=debug" not in server.log() and " debug " not in server.log()


def test_unknown_yaml_key_is_an_error(tmp_path):
    config = tmp_path / "bad.yaml"
    config.write_text("no_such_option: 1\n")
    out = subprocess.run(args=[str(BINARY), "--config", str(config)], capture_output=True, text=True,
                         env={"PATH": "/usr/bin", "HARDCHARGER_ADMIN_PASSWORD": "x"})
    assert out.returncode == 2
    assert "unknown option 'no_such_option'" in out.stderr


def test_admin_credentials_by_flag_environment_and_yaml(launcher, tmp_path):
    # --admin-username / --admin-password, HARDCHARGER_ADMIN_*, YAML admin_*.
    by_flag = launcher.start(args=["--admin-username", "chief", "--admin-password", "flag-Pass-1"],
                             username="chief", password="flag-Pass-1", env={"HARDCHARGER_ADMIN_PASSWORD": ""})
    assert by_flag.client(token=by_flag.token(username="chief", password="flag-Pass-1")).new_session().scalar(
        sql="SELECT HARDCHARGER_USER()") == "chief"
    by_env = launcher.start(username="boss", password="env-Pass-1")  # the launcher sets HARDCHARGER_ADMIN_*
    assert by_env.token(username="boss", password="env-Pass-1")
    config = tmp_path / "admin.yaml"
    config.write_text(yaml.safe_dump({"admin_username": "yamladmin", "admin_password": "yaml-Pass-1"}))
    by_yaml = launcher.start(args=["--config", str(config)], username="yamladmin", password="yaml-Pass-1",
                             env={"HARDCHARGER_ADMIN_USERNAME": "", "HARDCHARGER_ADMIN_PASSWORD": ""})
    assert by_yaml.token(username="yamladmin", password="yaml-Pass-1")


@pytest.mark.parametrize("flag", ["--username", "--password", "--hostname", "--database-filename", "--readonly",
                                  "-D", "-O", "--max-metadata-size", "--license-key-file", "--license-key",
                                  "--otel-enabled", "--reject-unknown-sessions", "--backend", "--tls"])
def test_old_and_gizmosql_flags_are_unknown_options(flag):
    # Hardcharger takes only its own names: a GizmoSQL 1.x flag, a license flag or a
    # 0.1.x name is an unknown option like any typo.
    out = subprocess.run(args=[str(BINARY), flag, "x"], capture_output=True, text=True,
                         env={"PATH": "/usr/bin", "HARDCHARGER_ADMIN_PASSWORD": "x"})
    assert out.returncode == 2, out.stderr
    assert f"unknown option {flag}" in out.stderr, out.stderr


def test_old_and_gizmosql_environment_names_are_not_read(launcher):
    # Neither GizmoSQL 1.x's names nor Hardcharger 0.1.x's admin names are read, and
    # nothing is logged about them.
    server = launcher.start(env={"GIZMOSQL_INIT_SQL_COMMANDS": "CREATE TABLE from_gizmosql AS SELECT 1 AS v",
                                 "INIT_SQL_COMMANDS": "CREATE TABLE from_bare AS SELECT 1 AS v",
                                 "GIZMOSQL_LICENSE_KEY": "x", "HARDCHARGER_LICENSE_KEY": "x",
                                 "HARDCHARGER_USERNAME": "old-admin", "HARDCHARGER_PASSWORD": "old-Pass-1"})
    session = server.admin()
    for table in ("from_gizmosql", "from_bare"):
        assert "does not exist" in session.error(sql=f"SELECT * FROM {table}")["detail"]
    assert "GIZMOSQL" not in server.log() and "license" not in server.log().lower(), server.log()
    response = httpx.post(url=f"{server.base_url}/chargewire/v1/auth/token",
                          json={"grant_type": "password", "username": "old-admin", "password": "old-Pass-1"})
    assert response.status_code == 401, response.text
    # HARDCHARGER_PASSWORD alone is no administrator password.
    out = subprocess.run(args=[str(BINARY)], capture_output=True, text=True,
                         env={"PATH": "/usr/bin", "HARDCHARGER_PASSWORD": "x", "GIZMOSQL_PASSWORD": "x"})
    assert out.returncode == 2 and "administrator password is required" in out.stderr, out.stderr


# ---- Init SQL ----

def test_init_sql_commands_then_file(launcher, tmp_path):
    sql_file = tmp_path / "init.sql"
    sql_file.write_text("INSERT INTO ordered VALUES (2); INSERT INTO ordered VALUES (3);")
    server = launcher.start(env={"HARDCHARGER_INIT_SQL_COMMANDS": "CREATE TABLE ordered (v INTEGER); "
                                                                   "INSERT INTO ordered VALUES (1);",
                                 "HARDCHARGER_INIT_SQL_COMMANDS_FILE": str(sql_file)})
    token = server.token()
    session = server.session(token=token)
    doc = server.run(token=token, session_id=session, sql="SELECT count(*) AS n FROM ordered").json()
    assert doc["state"] == "succeeded"


def test_failing_init_sql_stops_the_server(launcher):
    server = launcher.start(env={"HARDCHARGER_INIT_SQL_COMMANDS": "SELECT * FROM no_such_table"},
                            expect_up=False)
    assert server.process.wait(timeout=20) == 1
    assert "init SQL failed" in server.log()


# ---- Tokens ----

def test_tokens_survive_a_restart_with_a_shared_secret(launcher):
    env = {"HARDCHARGER_SECRET_KEY": "shared-secret-for-tests", "HARDCHARGER_ALLOW_CROSS_INSTANCE_TOKENS": "true"}
    first = launcher.start(env=env, password="pw:1")
    token = first.token()
    second = launcher.start(env=env, password="pw:1")
    assert second.session(token=token)


def test_other_instances_tokens_are_refused_by_default(launcher):
    env = {"HARDCHARGER_SECRET_KEY": "shared-secret-for-tests"}
    first = launcher.start(env=env)
    second = launcher.start(env=env, password=first.password)
    response = httpx.post(url=f"{second.base_url}/chargewire/v1/sessions",
                          headers={"Authorization": f"Bearer {first.token()}"}, json={})
    body = problem(response=response, status=401, code="invalid_token")
    assert "another server instance" in body["detail"]


def test_tokens_signed_with_another_key_are_refused(launcher):
    server = launcher.start(env={"HARDCHARGER_SECRET_KEY": "right-key"})
    now = datetime.datetime.now(tz=datetime.UTC)
    forged = jwt.encode(payload={"iss": "hardcharger", "sub": "admin", "role": "admin",
                                 "iat": now, "exp": now + datetime.timedelta(hours=1)},
                        key="wrong-key", algorithm="HS256")
    response = httpx.post(url=f"{server.base_url}/chargewire/v1/sessions",
                          headers={"Authorization": f"Bearer {forged}"}, json={})
    problem(response=response, status=401, code="invalid_token")


def test_tokens_of_another_issuer_are_refused(launcher):
    # Only tokens this server signs (iss=hardcharger) are its own; GizmoSQL 1.x's
    # server tokens (iss=gizmosql) over the same secret are refused.
    server = launcher.start(env={"HARDCHARGER_SECRET_KEY": "the-cluster-secret"})
    now = datetime.datetime.now(tz=datetime.UTC)
    for issuer, accepted in (("gizmosql", False), ("hardcharger", True)):
        token = jwt.encode(payload={"iss": issuer, "sub": "reporting", "role": "user", "jti": f"{issuer}-1",
                                    "iat": now, "exp": now + datetime.timedelta(hours=1)},
                           key="the-cluster-secret", algorithm="HS256")
        response = httpx.post(url=f"{server.base_url}/chargewire/v1/sessions", headers={"Authorization": f"Bearer {token}"},
                              json={})
        assert (response.status_code == 201) == accepted, (issuer, response.text)


def test_expired_tokens_are_refused(launcher):
    server = launcher.start(env={"HARDCHARGER_SECRET_KEY": "k"})
    past = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(hours=2)
    token = jwt.encode(payload={"iss": "hardcharger", "sub": "admin", "role": "admin", "iat": past,
                                "exp": past + datetime.timedelta(hours=1)}, key="k", algorithm="HS256")
    response = httpx.post(url=f"{server.base_url}/chargewire/v1/sessions",
                          headers={"Authorization": f"Bearer {token}"}, json={})
    problem(response=response, status=401, code="invalid_token")


# ---- Sign-in tokens from a trusted issuer (how GizmoData Cloud signs users in) ----

@pytest.fixture
def issuer(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path / "verify.pem"
    pem.write_bytes(key.public_key().public_bytes(encoding=serialization.Encoding.PEM,
                                                  format=serialization.PublicFormat.SubjectPublicKeyInfo))

    def mint(**claims) -> str:
        now = datetime.datetime.now(tz=datetime.UTC)
        payload = {"iss": "nexus", "aud": "cluster-1", "iat": now,
                   "exp": now + datetime.timedelta(hours=1), **claims}
        return jwt.encode(payload=payload, key=key, algorithm="RS256")

    return {"pem": str(pem), "mint": mint}


def start_with_issuer(launcher, issuer):
    return launcher.start(env={"HARDCHARGER_TOKEN_SIGNATURE_VERIFY_CERT_PATH": issuer["pem"],
                               "HARDCHARGER_TOKEN_ALLOWED_ISSUER": "nexus", "HARDCHARGER_TOKEN_ALLOWED_AUDIENCE": "cluster-1"})


def exchange(server, subject_token: str) -> httpx.Response:
    return httpx.post(url=f"{server.base_url}/chargewire/v1/auth/token",
                      json={"grant_type": "token_exchange", "subject_token": subject_token})


def test_token_exchange_signs_users_in(launcher, issuer):
    server = start_with_issuer(launcher=launcher, issuer=issuer)
    response = exchange(server=server, subject_token=issuer["mint"](sub="ada@example.com", role="user"))
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    session = server.session(token=token)
    doc = server.run(token=token, session_id=session, sql="SELECT 1 AS v").json()
    assert doc["state"] == "succeeded"
    assert "token_exchange" in httpx.get(url=f"{server.base_url}/chargewire").json()["auth"]["grant_types"]


def test_sign_in_token_as_password(launcher, issuer):
    server = start_with_issuer(launcher=launcher, issuer=issuer)
    assert server.token(username="token", password=issuer["mint"](sub="bob@example.com", role="admin"))


@pytest.mark.parametrize("claims", [
    {"sub": "x@example.com", "role": "user", "aud": "other-cluster"},
    {"sub": "x@example.com", "role": "user", "iss": "someone-else"},
    {"sub": "x@example.com"},  # no role, no default role
    {"sub": "x@example.com", "role": "superuser"},
])
def test_untrusted_sign_in_tokens_are_refused(launcher, issuer, claims):
    server = start_with_issuer(launcher=launcher, issuer=issuer)
    problem(response=exchange(server=server, subject_token=issuer["mint"](**claims)), status=401,
            code="invalid_credentials")


def test_catalog_access_needs_no_license(launcher, issuer):
    server = start_with_issuer(launcher=launcher, issuer=issuer)
    token = issuer["mint"](sub="x@example.com", role="user",
                           catalog_access=[{"catalog": "*", "access": "read"}])
    assert exchange(server=server, subject_token=token).status_code == 200


# ---- Roles ----

def test_readonly_role_cannot_modify(launcher, issuer):
    server = start_with_issuer(launcher=launcher, issuer=issuer)
    admin = server.token()
    admin_session = server.session(token=admin)
    server.run(token=admin, session_id=admin_session, sql="CREATE TABLE shared AS SELECT 1 AS v")
    reader = exchange(server=server, subject_token=issuer["mint"](sub="r@example.com", role="readonly")).json()["access_token"]
    session = server.session(token=reader)
    assert server.run(token=reader, session_id=session, sql="SELECT v FROM shared").json()["state"] == "succeeded"
    for sql in ["INSERT INTO shared VALUES (2)", "CREATE TABLE mine (v INT)", "DROP TABLE shared",
                "SELECT 1; DELETE FROM shared"]:
        body = problem(response=server.run(token=reader, session_id=session, sql=sql), status=403,
                       code="permission_denied")
        assert "readonly" in body["detail"]
        assert body["sqlstate"] == "42501"


# ---- Server info ----

def test_server_info(launcher):
    server = launcher.start()
    token = server.token()
    session = server.session(token=token)
    info = httpx.get(url=f"{server.base_url}/chargewire/v1/sessions/{session}/metadata/server-info",
                     headers={"Authorization": f"Bearer {token}"}).json()
    assert info["server_name"] == "Hardcharger"
    assert "edition" not in info  # no license, no editions


# ---- Operations ----

def test_health_port_serves_only_health(launcher):
    from conftest import free_port
    health_port = free_port()
    server = launcher.start(env={"HARDCHARGER_HEALTH_PORT": str(health_port)})
    launcher.wait_healthy(running=server)
    assert httpx.get(url=f"http://127.0.0.1:{health_port}/health").json() == {"status": "serving"}
    assert httpx.get(url=f"http://127.0.0.1:{health_port}/chargewire/v1/health").status_code == 200
    assert httpx.get(url=f"http://127.0.0.1:{health_port}/chargewire").status_code == 404
    assert server.process.poll() is None


def test_default_query_timeout(launcher):
    server = launcher.start(env={"HARDCHARGER_QUERY_TIMEOUT_SECONDS": "1"})
    token = server.token()
    session = server.session(token=token)
    response = httpx.post(url=f"{server.base_url}/chargewire/v1/sessions/{session}/statements",
                          headers={"Authorization": f"Bearer {token}", "Prefer": "respond-async"},
                          json={"sql": "SELECT count(*) FROM range(100000000000)"})
    statement_id = response.json()["statement_id"]
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        doc = httpx.get(url=f"{server.base_url}/chargewire/v1/sessions/{session}/statements/{statement_id}",
                        headers={"Authorization": f"Bearer {token}", "Prefer": "wait=5"}, timeout=30).json()
        if doc["state"] in ("succeeded", "failed", "cancelled"):
            break
    assert doc["state"] == "cancelled"
    assert doc["cancel_reason"] == "timeout"


def test_queries_are_logged_unless_turned_off(launcher):
    server = launcher.start()
    token = server.token()
    session = server.session(token=token)
    server.run(token=token, session_id=session, sql="SELECT 'logged-marker' AS v")
    assert "logged-marker" in server.log()

    quiet = launcher.start(env={"HARDCHARGER_PRINT_QUERIES": "false"})
    token = quiet.token()
    session = quiet.session(token=token)
    quiet.run(token=token, session_id=session, sql="SELECT 'quiet-marker' AS v")
    assert "quiet-marker" not in quiet.log()


def test_read_only_database(launcher, tmp_path):
    database = tmp_path / "data.duckdb"
    writer = launcher.start(env={"HARDCHARGER_DATABASE": str(database),
                                 "HARDCHARGER_INIT_SQL_COMMANDS": "CREATE TABLE t AS SELECT 1 AS v"})
    writer.stop()
    server = launcher.start(env={"HARDCHARGER_DATABASE": str(database), "HARDCHARGER_READ_ONLY": "true"})
    token = server.token()
    session = server.session(token=token)
    assert server.run(token=token, session_id=session, sql="SELECT v FROM t").json()["state"] == "succeeded"
    response = server.run(token=token, session_id=session, sql="INSERT INTO t VALUES (2)")
    assert response.status_code >= 400
    assert "read-only" in response.text.lower() or "read only" in response.text.lower()


def test_every_setting_names_a_real_option(launcher):
    # hardcharger_settings() says where a startup setting is set: each flag and variable
    # it names must be one the server takes.
    usage = subprocess.run(args=[str(BINARY), "--help"], capture_output=True, text=True, check=True).stdout
    rows = launcher.start().admin().rows(sql="SELECT name, cli_flag, env_var FROM hardcharger_settings()")
    assert rows and all(row["name"].startswith("hardcharger.") for row in rows), rows
    for row in rows:
        for column in ("cli_flag", "env_var"):
            if row[column] is not None:
                assert f"{row[column]}\n" in usage or f"[{row[column]}]" in usage, (row, column)
