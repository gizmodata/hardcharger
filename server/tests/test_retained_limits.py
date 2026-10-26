"""Retained results reserve both their owner's quota and the server-wide budget."""

import pytest


def retained(session, length=5000):
    return session.execute(sql=f"SELECT repeat('x', {length}) AS payload")


def release(session, statement):
    response = session.http.delete(url=f"{session.url}/statements/{statement['statement_id']}",
                                   headers=session.headers())
    assert response.status_code == 204, response.text


def test_global_budget_is_shared_across_principals_and_delete_returns_it(launcher):
    server = launcher.start(args=["--retained-bytes-per-principal", "10000", "--retained-bytes-total", "8000"])
    alice = server.as_role(role="admin", subject="alice")
    bob = server.as_role(role="admin", subject="bob")
    first = retained(session=alice)
    assert bob.error(sql="SELECT repeat('y', 5000)")["code"] == "result_quota_exceeded"
    assert bob.scalar(sql="SELECT 42") == 42
    release(session=alice, statement=first)
    assert retained(session=bob)["state"] == "succeeded"


def test_principal_budget_is_shared_across_sessions_and_close_returns_it(launcher):
    server = launcher.start(args=["--retained-bytes-per-principal", "8000", "--retained-bytes-total", "20000"])
    alice = server.as_role(role="admin", subject="alice")
    alice_again = server.as_role(role="admin", subject="alice")
    bob = server.as_role(role="admin", subject="bob")
    retained(session=alice)
    assert alice_again.error(sql="SELECT repeat('y', 5000)")["code"] == "result_quota_exceeded"
    assert retained(session=bob)["state"] == "succeeded"
    assert alice.close().status_code == 204
    assert retained(session=alice_again)["state"] == "succeeded"


@pytest.mark.parametrize("expression", ["{'child': repeat('x', 10000)}", "[repeat('x', 10000)]"])
def test_nested_buffers_count_and_failed_reservations_do_not_leak(launcher, expression):
    session = launcher.start(args=["--retained-bytes-total", "8000"]).admin()
    assert session.error(sql=f"SELECT {expression}")["code"] == "result_quota_exceeded"
    assert retained(session=session)["state"] == "succeeded"


@pytest.mark.parametrize("source", ["flag", "env", "yaml"])
def test_retained_limits_are_configurable_and_visible_in_settings(launcher, tmp_path, source):
    args, env = [], {}
    if source == "flag":
        args = ["--retained-bytes-per-principal", "8192", "--retained-bytes-total", "16384"]
    elif source == "env":
        env = {"HARDCHARGER_RETAINED_BYTES_PER_PRINCIPAL": "8192", "HARDCHARGER_RETAINED_BYTES_TOTAL": "16384"}
    else:
        config = tmp_path / "retained.yaml"
        config.write_text("retained_bytes_per_principal: 8192\nretained_bytes_total: 16384\n")
        args = ["--config", str(config)]
    session = launcher.start(args=args, env=env).admin()
    values = session.rows(sql="SELECT name, value, scope, settable FROM hardcharger_server_settings() "
                              "WHERE name IN ('hardcharger.retained_bytes_per_principal', 'hardcharger.retained_bytes_total') "
                              "ORDER BY name")
    assert values == [
        {"name": "hardcharger.retained_bytes_per_principal", "value": "8192", "scope": "STARTUP", "settable": False},
        {"name": "hardcharger.retained_bytes_total", "value": "16384", "scope": "STARTUP", "settable": False},
    ]


@pytest.mark.parametrize("option", ["retained-bytes-per-principal", "retained-bytes-total"])
def test_zero_refuses_retention_but_still_allows_streaming(launcher, option):
    session = launcher.start(args=[f"--{option}", "0"]).admin()
    assert session.error(sql="SELECT 1")["code"] == "result_quota_exceeded"
    assert session.scalar(sql="SELECT 42") == 42


@pytest.mark.parametrize("option", ["retained-bytes-per-principal", "retained-bytes-total"])
def test_negative_retained_limits_are_rejected(launcher, option):
    server = launcher.start(args=[f"--{option}", "-1"], expect_up=False)
    assert server.process.wait(timeout=10) != 0
    assert "retained byte limits cannot be negative" in server.log()
