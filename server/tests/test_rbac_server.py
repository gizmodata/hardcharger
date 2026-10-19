"""Access control at the server: who connects as what, sign-in, sessions and tokens
ending when a user is disabled, the store requirements, and two servers
(replicas) on one store."""

import time

import httpx
import pytest

from rbac import (ADMIN, WarpError, expect_allowed, mint, rbac_env, redact,
                  start_rbac_server)


@pytest.fixture(scope="module")
def server(module_launcher, store):
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database())
    server.admin_client = server.admin()
    return server


@pytest.fixture
def admin(server):
    return server.admin_client


def error(client, sql: str) -> str:
    try:
        client.run(sql=sql)
        return ""
    except WarpError as raised:
        return raised.detail


def test_the_administrator_manages_access_control(admin):
    assert admin.run(sql="CREATE ROLE analyst").to_pylist() == [{"result": "Role analyst created"}]
    for sql in ["CREATE TABLE orders (id INT, amount INT)", "CREATE USER joe", "GRANT analyst TO joe"]:
        expect_allowed(client=admin, sql=sql)
    granted = admin.run(sql="GRANT SELECT (id) ON orders TO analyst").to_pylist()[0]["result"]
    assert "Granted SELECT (id) on" in granted, granted
    grants = admin.run(sql="SHOW GRANTS FOR joe")
    assert grants.column_names == ["privilege", "object_type", "object", "columns", "granted_to", "grantee_type",
                                   "grant_option", "granted_by"]
    assert any(row["privilege"] == "SELECT" and row["columns"] == "id" for row in grants.to_pylist())


def test_store_users_connect_with_tokens_and_cannot_grant(server, admin):
    expect_allowed(client=admin, sql="CREATE USER IF NOT EXISTS mia")
    expect_allowed(client=admin, sql="CREATE TABLE IF NOT EXISTS orders (id INT, amount INT)")
    mia = server.user(name="mia")
    # A store user may look at their own grants...
    expect_allowed(client=mia, sql="SHOW GRANTS")
    # ...but not change anything, and the refusal says why.
    for sql, why in [("CREATE ROLE sneaky", "only administrators manage users and roles"),
                     ("CREATE USER helper", "only administrators manage users and roles"),
                     ("ALTER USER mia DISABLE", "only administrators manage users and roles"),
                     ("GRANT SELECT ON orders TO PUBLIC", "may not grant SELECT"),
                     ("GRANT PUBLIC TO mia", "cannot be granted")]:
        assert why in error(client=mia, sql=sql), sql
    assert admin.run(sql="SHOW GRANTS FOR mia").num_rows == 0  # nothing was granted by those attempts


def test_unknown_users_are_refused(server):
    for role in ["user", "readonly"]:  # an old-style token role does not help: only admin is special
        with pytest.raises(WarpError) as raised:
            server.client(token=mint(user="nobody_here", role=role))
        assert raised.value.code == "invalid_token", str(raised.value)
        assert "is not defined on this server" in raised.value.detail, str(raised.value)


def test_disabling_a_user_ends_their_live_session_and_tokens(server, admin):
    expect_allowed(client=admin, sql="CREATE USER IF NOT EXISTS leo WITH PASSWORD 'leo-pw'")
    expect_allowed(client=admin, sql="ALTER USER leo ENABLE")
    leo = server.user(name="leo", password="leo-pw")
    finished = leo.submit(sql="SELECT 42 AS answer")
    assert leo.wait(statement_id=finished)["state"] == "succeeded"
    expect_allowed(client=admin, sql="ALTER USER leo DISABLE")
    with pytest.raises(WarpError) as ended:
        leo.run(sql="SELECT 1")
    assert ended.value.status == 401 and "is disabled" in ended.value.detail, str(ended.value)
    # Results the session kept are gone with its token.
    kept = leo.http.get(url=f"{leo.url}/statements/{finished}/result", headers=leo.headers())
    assert kept.status_code == 401, kept.text
    # A brand-new session is refused too.
    with pytest.raises(WarpError) as refused:
        server.client(token=mint(user="leo"))
    assert "is disabled" in refused.value.detail
    expect_allowed(client=admin, sql="ALTER USER leo ENABLE")


def test_store_users_sign_in_with_their_password(server, admin):
    expect_allowed(client=admin, sql="CREATE USER IF NOT EXISTS pat WITH PASSWORD 'correct horse 1'")
    expect_allowed(client=admin, sql="CREATE USER IF NOT EXISTS nopass")
    pat = server.user(name="pat", password="correct horse 1")
    expect_allowed(client=pat, sql="SELECT 1")
    # The session is pat's, not the administrator's.
    assert "only administrators manage users and roles" in error(client=pat, sql="CREATE ROLE pats_role")
    # Every failure looks the same: wrong password, no password, unknown name.
    for user, password in [("pat", "wrong"), ("pat", "correct horse"), ("nopass", "anything"),
                           ("nobody_at_all", "x")]:
        with pytest.raises(WarpError) as raised:
            server.password_token(user=user, password=password)
        assert (raised.value.code, raised.value.detail) == ("invalid_credentials",
                                                            "the user name or password is not correct"), user
    expect_allowed(client=admin, sql="ALTER USER pat DISABLE")
    with pytest.raises(WarpError):
        server.password_token(user="pat", password="correct horse 1")
    expect_allowed(client=admin, sql="ALTER USER pat ENABLE")
    # (DISABLE ended pat's session: sign in again.)
    pat = server.user(name="pat", password="correct horse 1")
    # A password change ends the sessions and tokens issued against the old one.
    expect_allowed(client=pat, sql="SELECT 1")
    expect_allowed(client=admin, sql="ALTER USER pat WITH PASSWORD 'battery staple 2'")
    with pytest.raises(WarpError) as stale:
        pat.run(sql="SELECT 1")
    assert stale.value.status == 401 and "changed since this access token was issued" in stale.value.detail
    with pytest.raises(WarpError):
        server.password_token(user="pat", password="correct horse 1")
    assert server.password_token(user="pat", password="battery staple 2")
    # The administrator's own sign-in is unchanged, and its name is not available.
    expect_allowed(client=server.admin(), sql="CREATE ROLE IF NOT EXISTS via_password")
    for sql in [f"CREATE USER {ADMIN} WITH PASSWORD 'x'", f"CREATE USER {ADMIN.upper()}"]:
        assert "is the server administrator's name" in error(client=admin, sql=sql), sql


def test_nothing_rides_along_with_an_access_control_statement(admin):
    expect_allowed(client=admin, sql="CREATE TABLE IF NOT EXISTS keepme (x INT)")
    for sql in ["CREATE ROLE r1; DROP TABLE keepme", "GRANT SELECT ON keepme TO PUBLIC; DROP TABLE keepme",
                "/* x */ CREATE ROLE r2 -- y\n; DROP TABLE keepme"]:
        assert "one at a time" in error(client=admin, sql=sql), sql
    expect_allowed(client=admin, sql="SELECT * FROM keepme")  # still there
    assert "keepme" not in str(admin.run(sql="SHOW GRANTS FOR ROLE PUBLIC").to_pylist())  # and no grant slipped in


def test_passwords_never_reach_the_log(server, admin):
    for sql in ["CREATE USER logged1 PASSWORD 'hunter2-a'", "alter user logged1 with password 'hunter2-b'",
                "/* /* nested */ */ CREATE USER logged2 PASSWORD 'hunter2-c'",
                "-- comment\nCREATE\n USER logged3 PASSWORD 'hunter2-d'",
                "CREATE USER logged1 PASSWORD 'hunter2-e'"]:  # also when refused
        error(client=admin, sql=sql)
    log = server.log()
    assert "hunter2" not in log
    assert "[REDACTED: user DDL]" in log


def test_access_control_statements_need_the_store(launcher):
    plain = launcher.start()
    token = plain.token()
    session = plain.session(token=token)
    body = plain.run(token=token, session_id=session, sql="CREATE ROLE analyst").json()
    assert body["code"] == "sql_error" and "access control is not enabled" in body["detail"], body
    # SHOW GRANTS is not ours there: DuckDB treats it as DESCRIBE of a table.
    assert plain.run(token=token, session_id=session, sql="CREATE TABLE grants (g INT)").status_code in (200, 201)
    assert plain.run(token=token, session_id=session, sql="SHOW GRANTS").status_code in (200, 201)
    # Without access control, a scoped token means nothing, so none is accepted.
    response = httpx.post(url=f"{plain.base_url}/warp/v1/sessions", json={},
                          headers={"Authorization": f"Bearer {mint(user='x', scope_id='s')}"})
    assert response.status_code == 401


def test_an_unreachable_store_stops_the_server_without_leaking_its_password(launcher):
    uri = "postgresql://hardcharger:very-secret-pw@127.0.0.1:1/nowhere"
    running = launcher.start(env=rbac_env(store_uri=uri), expect_up=False)
    assert running.process.wait(timeout=60) == 1
    log = running.log()
    assert "could not connect to the PostgreSQL store" in log, log
    assert "very-secret-pw" not in log, log
    assert redact(uri) != uri


def test_the_in_memory_store_is_for_tests_only(launcher):
    running = launcher.start(env=rbac_env(store_uri="memory"), expect_up=False)
    assert running.process.wait(timeout=20) == 2
    assert "must be a PostgreSQL connection string" in running.log()


def test_server_info_says_access_control_is_on(server, admin):
    info = admin.http.get(url=f"{admin.url}/metadata/server-info", headers=admin.headers()).json()
    assert info["hardcharger_access_control"] is True
    assert "edition" not in info


# ---- Two replicas on one store ----

@pytest.fixture
def cluster(launcher, pg_store):
    uri = pg_store.new_database()
    first = start_rbac_server(launcher=launcher, store_uri=uri)
    second = start_rbac_server(launcher=launcher, store_uri=uri)
    return first, second


def eventually(check, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.1)
    return False


def test_a_change_on_one_replica_reaches_the_other(cluster):
    here, elsewhere = cluster
    admin_here, admin_elsewhere = here.admin(), elsewhere.admin()
    # Each replica has its own (in-memory) database here; a real cluster shares one
    # catalog (DuckLake), so give both the same table.
    for admin in (admin_here, admin_elsewhere):
        expect_allowed(client=admin, sql="CREATE TABLE t AS SELECT 1 AS id")
    for sql in ["CREATE USER nina", "CREATE ROLE nina_role", "GRANT nina_role TO nina"]:
        expect_allowed(client=admin_elsewhere, sql=sql)
    # This replica learns of the new user by polling; then the user can connect here.
    assert eventually(check=lambda: _connects(server=here, user="nina")), "the new user never reached this replica"
    nina = here.user(name="nina")
    assert "no SELECT privilege" in error(client=nina, sql="SELECT * FROM t")
    expect_allowed(client=admin_elsewhere, sql="GRANT SELECT ON t TO nina_role")
    assert eventually(check=lambda: error(client=nina, sql="SELECT * FROM t") == ""), \
        "a grant made on another replica never applied here"
    expect_allowed(client=admin_elsewhere, sql="REVOKE SELECT ON t FROM nina_role")
    assert eventually(check=lambda: "no SELECT privilege" in error(client=nina, sql="SELECT * FROM t")), \
        "a revoke made on another replica never applied here"


def _connects(server, user: str) -> bool:
    try:
        server.user(name=user)
        return True
    except WarpError:
        return False


def test_disabling_a_user_on_another_replica_ends_sessions_here(cluster):
    here, elsewhere = cluster
    admin_elsewhere = elsewhere.admin()
    expect_allowed(client=admin_elsewhere, sql="CREATE USER otto")
    assert eventually(check=lambda: _connects(server=here, user="otto"))
    otto = here.user(name="otto")
    expect_allowed(client=otto, sql="SELECT 1")
    expect_allowed(client=admin_elsewhere, sql="ALTER USER otto DISABLE")
    assert eventually(check=lambda: error(client=otto, sql="SELECT 1") != ""), \
        "the session survived a DISABLE made on another replica"
    assert "is disabled" in error(client=otto, sql="SELECT 1")


def test_a_revoke_on_another_replica_stops_a_statement_running_here(cluster):
    here, elsewhere = cluster
    admin_here, admin_elsewhere = here.admin(), elsewhere.admin()
    for admin in (admin_here, admin_elsewhere):
        expect_allowed(client=admin, sql="CREATE TABLE ot AS SELECT 1 AS id")
    for sql in ["CREATE ROLE otto_role", "CREATE USER ottilie", "GRANT otto_role TO ottilie",
                "GRANT SELECT ON ot TO otto_role"]:
        expect_allowed(client=admin_elsewhere, sql=sql)
    assert eventually(check=lambda: _connects(server=here, user="ottilie"))
    runner = here.user(name="ottilie")
    assert eventually(check=lambda: error(client=runner, sql="SELECT * FROM ot") == "")
    running = runner.submit(sql="SELECT sum(t.id * r.range) FROM ot t, range(1000000000000) r")
    try:
        time.sleep(1)
        assert runner.wait(statement_id=running, timeout=0.5)["state"] == "running"
        expect_allowed(client=admin_elsewhere, sql="REVOKE SELECT ON ot FROM otto_role")
        doc = runner.wait(statement_id=running, timeout=30)
        assert doc["state"] in ("failed", "cancelled"), "a REVOKE on another replica left a statement running here"
    finally:
        runner.http.delete(url=f"{runner.url}/statements/{running}", headers=runner.headers())
    assert "no SELECT privilege" in error(client=runner, sql="SELECT * FROM ot")
