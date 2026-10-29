"""Every feature that was Enterprise in GizmoSQL 1.x works on a Hardcharger server
started with no license at all, turned on by its own options only: access control
(RBAC, from a store), instrumentation, catalog logging, query profiles, instance /
session / query tags, catalog_access rules, metrics, KILL SESSION, the statement queue,
identity-provider sign-in (JWKS) and the browser sign-in server (OAuth). There is no
license option: a license flag left in a deployment is an unknown option.
"""

import subprocess
import time

import httpx
import pytest

from conftest import BINARY, free_port
from mock_idp import MockIdP
from rbac import expect_allowed, start_rbac_server

AUDIENCE = "hardcharger-no-license"


@pytest.fixture(scope="module")
def idp():
    provider = MockIdP(audience=AUDIENCE, client_id="no-license-client", client_secret="no-license-secret")
    yield provider
    provider.stop()


@pytest.fixture(scope="module")
def server(module_launcher, idp):
    metrics_port = free_port()
    running = module_launcher.start(args=[
        "--enable-instrumentation", "--enable-catalog-logging", "--capture-query-profile", "standard",
        "--instance-tag", '{"team": "no-license"}',
        "--enable-metrics", "--metrics-port", str(metrics_port),
        "--max-concurrent-statements", "4",
        "--token-allowed-issuer", idp.issuer, "--token-allowed-audience", AUDIENCE, "--token-default-role", "user",
        "--oauth-client-id", "no-license-client", "--oauth-client-secret", "no-license-secret"])
    running.metrics_port = metrics_port
    return running


def test_nothing_about_a_license_is_logged(server):
    assert "license" not in server.log().lower() and "edition" not in server.log().lower(), server.log()


def test_instrumentation_catalog_logging_profiles_and_tags(server):
    admin = server.admin()
    admin.execute(sql="SET hardcharger.session_tag = '{\"who\": \"tester\"}'")
    admin.execute(sql="SET hardcharger.query_tag = '{\"q\": 1}'")
    assert admin.scalar(sql="SELECT 'no-license-probe'") == "no-license-probe"
    assert admin.scalar(sql="SELECT HARDCHARGER_INSTRUMENTATION_ENABLED()") is True
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        rows = admin.rows(sql="SELECT s.session_tag->>'who' AS who, st.query_tag->>'q' AS q, "
                              "e.query_profile IS NOT NULL AS profiled FROM _hardcharger_instr.sql_statements st "
                              "JOIN _hardcharger_instr.sql_executions e USING (statement_id) "
                              "JOIN _hardcharger_instr.sessions s USING (session_id) "
                              "WHERE st.sql_text = 'SELECT ''no-license-probe'''")
        # The profile lands after the statement's own row: wait for it too.
        if rows and rows[0]["profiled"]:
            break
        time.sleep(0.2)
    assert rows == [{"who": "tester", "q": "1", "profiled": True}], rows
    assert admin.scalar(sql="SELECT instance_tag->>'team' FROM _hardcharger_instr.instances "
                            "WHERE instance_id = HARDCHARGER_CURRENT_INSTANCE()::UUID") == "no-license"
    assert admin.scalar(sql="SELECT count(*) > 0 FROM _hardcharger_logs.main.logs") is True


def test_metrics(server):
    assert server.admin().scalar(sql="SELECT count(*) > 0 FROM hardcharger_metrics()") is True
    text = httpx.get(url=f"http://127.0.0.1:{server.metrics_port}/metrics").text
    assert "hardcharger_build_info" in text and "gizmosql_" not in text, text[:500]


def test_statement_queue(server):
    admin = server.admin()
    assert admin.scalar(sql="SELECT value FROM hardcharger_settings() "
                            "WHERE name = 'hardcharger.max_concurrent_statements'") == "4"
    user = server.as_role(role="user", subject="queued-user")
    assert user.scalar(sql="SELECT 41 + 1") == 42


def test_kill_session(server):
    victim = server.as_role(role="user", subject="victim")
    victim_id = victim.scalar(sql="SELECT HARDCHARGER_CURRENT_SESSION()")
    server.admin().execute(sql=f"KILL SESSION '{victim_id}'")
    problem = victim.error(sql="SELECT 1")
    assert problem["code"] == "session_ended", problem


def test_catalog_access_rules(server):
    admin = server.admin()
    admin.execute(sql="ATTACH ':memory:' AS open_cat")
    admin.execute(sql="ATTACH ':memory:' AS closed_cat")
    admin.execute(sql="CREATE TABLE closed_cat.main.t AS SELECT 1 AS v")
    user = server.as_role(role="user", subject="ruled", catalog_access=[{"catalog": "open_cat", "access": "write"},
                                                                        {"catalog": "*", "access": "none"}])
    user.execute(sql="CREATE TABLE open_cat.main.mine AS SELECT 1 AS v")
    problem = user.error(sql="SELECT * FROM closed_cat.main.t")
    assert problem["code"] == "permission_denied" and "closed_cat" in problem["detail"], problem


def test_identity_provider_sign_in(server, idp):
    response = httpx.post(url=f"{server.base_url}/chargewire/v1/auth/token",
                          json={"grant_type": "token_exchange", "subject_token": idp.id_token(email="a@gizmodata.com")})
    assert response.status_code == 200, response.text


def test_browser_sign_in(server):
    response = httpx.post(url=f"{server.base_url}/chargewire/v1/auth/oauth/flows", json={})
    assert response.status_code == 201 and "flow_id" in response.json(), response.text


def test_role_based_access_control(launcher, store):
    rbac = start_rbac_server(launcher=launcher, store_uri=store.new_database())
    admin = rbac.admin()
    expect_allowed(client=admin, sql="CREATE ROLE readers")
    expect_allowed(client=admin, sql="CREATE USER nora WITH PASSWORD 'nora-Pass-1'")
    expect_allowed(client=admin, sql="GRANT readers TO nora")
    assert "license" not in rbac.running.log().lower()


@pytest.mark.parametrize("flag", ["--license-key", "--license-key-file"])
def test_a_license_flag_is_an_unknown_option(flag):
    out = subprocess.run(args=[str(BINARY), flag, "x"], capture_output=True, text=True,
                         env={"PATH": "/usr/bin", "HARDCHARGER_ADMIN_PASSWORD": "x"})
    assert out.returncode == 2 and f"unknown option {flag}" in out.stderr, out.stderr
