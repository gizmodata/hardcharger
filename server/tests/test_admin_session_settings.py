"""Admin reads and changes a target's settings without executing inside its session."""
import pytest
import time


def rows(admin, target):
    return {row["name"]: row for row in admin.rows(sql=f"SELECT * FROM hardcharger_session_settings('{target.id}')")}


def test_session_override_reset_and_global_inheritance(launcher):
    server = launcher.start(args=["--query-timeout-seconds", "21"])
    admin, target, other = server.admin(), server.admin(), server.admin()
    assert rows(admin, target)["hardcharger.query_timeout"]["value"] == "21"
    assert admin.scalar(sql=f"SELECT hardcharger_set_session_setting('{target.id}', 'hardcharger.query_timeout', '42')")
    assert target.scalar(sql="SELECT session_value FROM hardcharger_settings() WHERE name = 'hardcharger.query_timeout'") == "42"
    assert rows(admin, other)["hardcharger.query_timeout"]["session_value"] is None
    admin.execute(sql="SET GLOBAL hardcharger.query_timeout = 99")
    assert rows(admin, target)["hardcharger.query_timeout"]["value"] == "42"
    assert rows(admin, other)["hardcharger.query_timeout"]["value"] == "99"
    assert admin.scalar(sql=f"SELECT hardcharger_reset_session_setting('{target.id}', 'hardcharger.query_timeout')")
    assert rows(admin, target)["hardcharger.query_timeout"]["value"] == "99"
    assert rows(admin, target)["hardcharger.query_timeout"]["session_value"] is None
    admin.execute(sql="RESET GLOBAL hardcharger.query_timeout")
    assert rows(admin, target)["hardcharger.query_timeout"]["value"] == "21"


@pytest.mark.parametrize("restricted", [False, True])
def test_only_unrestricted_admin_can_read_or_mutate_another_session(launcher, restricted):
    server = launcher.start()
    admin, target = server.admin(), server.admin()
    user = (server.as_role(role="admin", catalog_access=[{"catalog": "*", "access": "read"}]) if restricted
            else server.as_role(role="user"))
    for sql in [f"SELECT * FROM hardcharger_session_settings('{target.id}')",
                f"SELECT hardcharger_set_session_setting('{target.id}', 'query_timeout', '7')",
                f"SELECT hardcharger_reset_session_setting('{target.id}', 'query_timeout')"]:
        assert user.error(sql=sql)["code"] == "permission_denied"
    assert rows(admin, target)["hardcharger.query_timeout"]["session_value"] is None


def test_validation_and_prepare_have_no_side_effects(launcher):
    server = launcher.start()
    admin, target = server.admin(), server.admin()
    change = f"SELECT hardcharger_set_session_setting('{target.id}', 'capture_query_profile', 'detailed')"
    admin.prepare(sql=change)
    admin.query(sql="EXPLAIN " + change)
    assert rows(admin, target)["hardcharger.capture_query_profile"]["session_value"] is None
    for setting, value in [("capture_query_profile", "bogus"), ("query_timeout", "bad"),
                           ("max_concurrent_statements", "3"), ("enable_instrumentation", "true")]:
        assert admin.error(sql=f"SELECT hardcharger_set_session_setting('{target.id}', '{setting}', '{value}')")
    assert rows(admin, target)["hardcharger.query_timeout"]["session_value"] is None
    assert rows(admin, target)["hardcharger.capture_query_profile"]["session_value"] is None


def test_setting_a_live_session_preserves_its_transaction(launcher):
    server = launcher.start()
    admin, target = server.admin(), server.admin()
    target.execute(sql="CREATE TABLE admin_setting_tx (n INTEGER)")
    target.execute(sql="BEGIN")
    target.execute(sql="INSERT INTO admin_setting_tx VALUES (42)")
    admin.scalar(sql=f"SELECT hardcharger_set_session_setting('{target.id}', 'query_timeout', '12')")
    assert rows(admin, target)["hardcharger.query_timeout"]["value"] == "12"
    assert admin.scalar(sql="SELECT count(*) FROM admin_setting_tx") == 0
    target.execute(sql="ROLLBACK")
    assert target.scalar(sql="SELECT count(*) FROM admin_setting_tx") == 0
    admin.execute(sql=f"KILL SESSION '{target.id}'")
    assert "not found" in admin.error(sql=f"SELECT * FROM hardcharger_session_settings('{target.id}')")["detail"]


def test_settings_can_be_read_and_changed_while_a_target_is_executing_or_queued(launcher):
    server = launcher.start(args=["--max-concurrent-statements", "1"])
    admin, holder, queued = server.admin(), server.admin(), server.admin()
    holder.execute(sql="SET hardcharger.bypass_queue = false")
    queued.execute(sql="SET hardcharger.bypass_queue = false")
    ids = []
    try:
        ids.append((holder, holder.submit(sql="SELECT count(*) FROM range(10000000000) a, range(1000) b", headers={"Prefer": "respond-async"}).json()["statement_id"]))
        ids.append((queued, queued.submit(sql="SELECT 42", headers={"Prefer": "respond-async"}).json()["statement_id"]))
        started = time.monotonic()
        for target in (holder, queued):
            admin.scalar(sql=f"SELECT hardcharger_set_session_setting('{target.id}', 'query_timeout', '23')")
            assert rows(admin, target)["hardcharger.query_timeout"]["value"] == "23"
        assert time.monotonic() - started < 5, "configuration must not wait behind target SQL work"
    finally:
        for target, statement in reversed(ids):
            admin.scalar(sql=f"SELECT hardcharger_cancel('{target.id}')")
            target.wait_done(statement_id=statement)
