"""Scoped tokens (CREATE SCOPED TOKEN): a token a user mints for a program, allowed only
some verbs on some catalogs until it expires, and never more than the user."""

import time
from datetime import UTC, datetime

import pytest

from conftest import TIMEOUT_SCALE
from rbac import WarpError, expect_allowed, start_rbac_server


@pytest.fixture(scope="module")
def server(module_launcher, store):
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database())
    admin = server.admin()
    for sql in ["ATTACH ':memory:' AS sales_db", "ATTACH ':memory:' AS hr_db",
                "CREATE TABLE sales_db.main.orders AS SELECT 1 AS id, 10 AS amount",
                "CREATE TABLE hr_db.main.staff AS SELECT 1 AS id, 'x' AS name",
                "CREATE ROLE worker", "GRANT SELECT, INSERT, UPDATE, DELETE ON CATALOG sales_db TO worker",
                "GRANT SELECT ON CATALOG hr_db TO worker", "GRANT CREATE TABLE ON SCHEMA sales_db.main TO worker",
                "CREATE USER wes", "GRANT worker TO wes", "CREATE USER ula"]:
        expect_allowed(client=admin, sql=sql)
    server.admin_client = admin
    return server


@pytest.fixture
def admin(server):
    return server.admin_client


@pytest.fixture
def wes(server):
    return server.user(name="wes")


def error(client, sql: str) -> str:
    try:
        client.run(sql=sql)
        return ""
    except WarpError as raised:
        return raised.detail


def mint_scoped(client, sql: str) -> str:
    rows = expect_allowed(client=client, sql=sql).to_pylist()
    assert rows and rows[0]["token"], rows
    return rows[0]["token"]


def agent(server, token: str):
    """A session that signs in with the scoped token itself, as its bearer token."""
    return server.client(token=token)


def test_a_token_does_only_what_it_allows(server, wes):
    bot = agent(server=server, token=mint_scoped(client=wes,
                                                 sql="CREATE SCOPED TOKEN reports ALLOW SELECT ON CATALOG sales_db"))
    expect_allowed(client=bot, sql="SELECT * FROM sales_db.main.orders")
    # wes may write, read hr_db and create tables; the token may not.
    for sql, why in [("INSERT INTO sales_db.main.orders VALUES (2, 20)", "allows only SELECT"),
                     ("DELETE FROM sales_db.main.orders", "allows only SELECT"),
                     ("SELECT * FROM hr_db.main.staff", "only on catalog sales_db"),
                     ("CREATE TABLE sales_db.main.planted AS SELECT 1", "scoped token reports")]:
        detail = error(client=bot, sql=sql)
        assert "Hardcharger blocked" in detail and why in detail, f"{sql} -> {detail}"
    # Positive twins: wes himself.
    expect_allowed(client=wes, sql="SELECT * FROM hr_db.main.staff")
    expect_allowed(client=wes, sql="INSERT INTO sales_db.main.orders VALUES (3, 30)")
    expect_allowed(client=wes, sql="DELETE FROM sales_db.main.orders WHERE id = 3")


def test_a_scoped_token_also_signs_in_as_the_password_of_user_token(server, wes):
    token = mint_scoped(client=wes, sql="CREATE SCOPED TOKEN via_password ALLOW SELECT ON CATALOG sales_db")
    # As GizmoSQL 1.x clients send it: user "token", the scoped token as the password.
    bot = server.client(token=server.password_token(user="token", password=token))
    expect_allowed(client=bot, sql="SELECT * FROM sales_db.main.orders")
    assert "only on catalog sales_db" in error(client=bot, sql="SELECT * FROM hr_db.main.staff")


def test_a_readonly_session_cannot_mint_its_way_out_of_readonly(server):
    # A scoped token signs in as a user, so one minted in a readonly session would write
    # with the user's grants (wes may INSERT); a readonly session changes nothing in the
    # store, and still reads it.
    readonly = server.user(name="wes", role="readonly")
    for sql in ["CREATE SCOPED TOKEN escape ALLOW SELECT, INSERT ON CATALOG sales_db", "CREATE ROLE planted"]:
        with pytest.raises(WarpError) as refused:
            readonly.run(sql=sql)
        assert refused.value.code == "permission_denied", refused.value
        assert "has a readonly session and cannot run" in refused.value.detail, refused.value
    assert readonly.run(sql="SHOW GRANTS FOR wes").num_rows >= 1
    expect_allowed(client=readonly, sql="SHOW SCOPED TOKENS")
    # The positive twin: the same user's own session mints one.
    mint_scoped(client=server.user(name="wes"), sql="CREATE SCOPED TOKEN not_readonly ALLOW SELECT")


def test_a_token_never_does_more_than_its_user(server, admin, wes):
    # A token that allows writes on a catalog wes may only read stays read-only there.
    bot = agent(server=server, token=mint_scoped(client=wes,
                                                 sql="CREATE SCOPED TOKEN writer ALLOW SELECT, INSERT ON CATALOG hr_db"))
    expect_allowed(client=bot, sql="SELECT * FROM hr_db.main.staff")
    assert "has no INSERT privilege" in error(client=bot, sql="INSERT INTO hr_db.main.staff VALUES (2, 'y')")
    # A grant taken from the user is taken from the token.
    expect_allowed(client=admin, sql="REVOKE SELECT ON CATALOG hr_db FROM worker")
    assert "has no SELECT privilege" in error(client=bot, sql="SELECT * FROM hr_db.main.staff")
    expect_allowed(client=admin, sql="GRANT SELECT ON CATALOG hr_db TO worker")


def test_a_token_cannot_change_access_control(server, wes):
    bot = agent(server=server, token=mint_scoped(client=wes, sql="CREATE SCOPED TOKEN quiet ALLOW SELECT"))
    for sql in ["GRANT worker TO ula", "CREATE ROLE sneaky", "ALTER USER wes DISABLE"]:
        assert "a scoped token cannot change access control" in error(client=bot, sql=sql), sql
    for sql in ["SET GLOBAL threads = 2", "ATTACH ':memory:' AS planted"]:
        assert "Hardcharger blocked" in error(client=bot, sql=sql), sql
    # It can read its own grants.
    expect_allowed(client=bot, sql="SHOW GRANTS")


def test_a_child_token_is_narrower_and_dies_with_its_parent(server, wes):
    parent = agent(server=server, token=mint_scoped(
        client=wes, sql="CREATE SCOPED TOKEN parent ALLOW SELECT, INSERT ON CATALOG sales_db, hr_db"))
    # Wider than the parent: refused.
    for sql, why in [("CREATE SCOPED TOKEN wider ALLOW SELECT, DELETE", "cannot allow DELETE"),
                     ("CREATE SCOPED TOKEN elsewhere ALLOW SELECT ON CATALOG other_db", "cannot reach catalog other_db"),
                     ("CREATE SCOPED TOKEN longer ALLOW SELECT VALID FOR '30 days'", "cannot outlast"),
                     ("CREATE SCOPED TOKEN other ALLOW SELECT FOR USER ula", "only for the user")]:
        assert why in error(client=parent, sql=sql), sql
    child_token = mint_scoped(client=parent, sql="CREATE SCOPED TOKEN child ALLOW SELECT ON CATALOG sales_db")
    child = agent(server=server, token=child_token)
    expect_allowed(client=child, sql="SELECT * FROM sales_db.main.orders")
    assert "only on catalog sales_db" in error(client=child, sql="SELECT * FROM hr_db.main.staff")
    # Revoking the parent ends the child's sessions too, and new ones.
    expect_allowed(client=wes, sql="DROP SCOPED TOKEN parent")
    for client in (parent, child):
        assert error(client=client, sql="SELECT 1") != "", "a revoked token still works"
    with pytest.raises(WarpError) as raised:
        agent(server=server, token=child_token)
    # Warp SQL §4.2: the refusal says why (its parent was revoked).
    assert raised.value.code == "invalid_token" and raised.value.reason == "token_revoked", raised.value
    assert raised.value.detail == "this scoped token was revoked", raised.value


def short_lived_agent(server, client, name: str):
    """A session on a scoped token that lives a few seconds, and the token's expiry.

    A loaded runner can take longer than that to sign in and run a statement (windows-arm64,
    2026-10-28: "the access token has expired" before the first statement), so a token that
    expires first is replaced by one that lives twice as long."""
    valid = max(2, int(2 * TIMEOUT_SCALE))
    while True:
        rows = expect_allowed(client=client,
                              sql=f"CREATE SCOPED TOKEN {name} ALLOW SELECT VALID FOR '{valid} seconds'").to_pylist()
        token, expires_at = rows[0]["token"], datetime.fromisoformat(rows[0]["expires_at"])
        try:
            bot = agent(server=server, token=token)
            bot.run(sql="SELECT 1")
            return token, bot, expires_at
        except WarpError as raised:
            if "expired" not in raised.detail or valid >= 64:
                raise
        expect_allowed(client=client, sql=f"DROP SCOPED TOKEN {name}")
        valid *= 2


def test_expiry_and_bookkeeping(server, admin, wes):
    token, bot, expires_at = short_lived_agent(server=server, client=wes, name="brief")
    # Until a second past the expiry the server recorded (whole seconds, on this machine's clock).
    time.sleep(max(0.0, (expires_at - datetime.now(tz=UTC)).total_seconds()) + 1)
    with pytest.raises(WarpError) as refused:
        agent(server=server, token=token)  # an expired token does not start a session
    assert "expired" in refused.value.detail, str(refused.value)
    # A session that was open stops at the expiry too (its token carries the expiry).
    assert "expired" in error(client=bot, sql="SELECT 1")
    # SHOW SCOPED TOKENS lists a user's tokens and never the token itself.
    shown = str(expect_allowed(client=wes, sql="SHOW SCOPED TOKENS").to_pylist())
    assert "brief" in shown and token[:20] not in shown
    # Others do not see wes's tokens; the administrator does.
    assert "brief" not in str(expect_allowed(client=server.user(name="ula"), sql="SHOW SCOPED TOKENS").to_pylist())
    assert "brief" in str(expect_allowed(client=admin, sql="SHOW SCOPED TOKENS").to_pylist())
    # Mistakes.
    for client, sql, why in [(wes, "CREATE SCOPED TOKEN brief ALLOW SELECT", "already has a scoped token named brief"),
                             (wes, "CREATE SCOPED TOKEN x ALLOW SELECT VALID FOR '1 year'", "at most 30 days"),
                             (wes, "CREATE SCOPED TOKEN x ALLOW SELECT VALID FOR 'soon'", "VALID FOR"),
                             (wes, "CREATE SCOPED TOKEN x ALLOW CREATE TABLE", "SELECT, INSERT, UPDATE or DELETE"),
                             (wes, "CREATE SCOPED TOKEN x ALLOW SELECT FOR USER ula", "only for yourself"),
                             (admin, "CREATE SCOPED TOKEN x ALLOW SELECT", "FOR USER"),
                             (wes, "DROP SCOPED TOKEN no_such", "no scoped token named no_such")]:
        assert why in error(client=client, sql=sql), sql
    # The administrator mints for a user, and can revoke it.
    ula_bot = agent(server=server, token=mint_scoped(client=admin, sql="CREATE SCOPED TOKEN ops FOR USER ula ALLOW SELECT"))
    expect_allowed(client=ula_bot, sql="SELECT 1")
    expect_allowed(client=admin, sql="DROP SCOPED TOKEN ops FOR USER ula")
    assert error(client=ula_bot, sql="SELECT 1") != ""


def test_a_disabled_users_tokens_stop_working(server, admin):
    expect_allowed(client=admin, sql="CREATE USER vic")
    expect_allowed(client=admin, sql="GRANT worker TO vic")
    token = mint_scoped(client=server.user(name="vic"), sql="CREATE SCOPED TOKEN job ALLOW SELECT")
    expect_allowed(client=admin, sql="ALTER USER vic DISABLE")
    with pytest.raises(WarpError):
        agent(server=server, token=token)
    # A token whose signature was tampered with is not a token.
    expect_allowed(client=admin, sql="ALTER USER vic ENABLE")
    agent(server=server, token=token)
    with pytest.raises(WarpError) as forged:
        agent(server=server, token=token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB"))
    assert forged.value.code == "invalid_token"
