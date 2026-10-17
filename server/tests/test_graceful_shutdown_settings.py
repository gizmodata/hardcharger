"""Port of GizmoSQL 1.x tests/integration/test_graceful_shutdown_settings.cpp (4 cases).

SET GLOBAL hardcharger.graceful_shutdown and hardcharger.shutdown_grace_period_seconds change
the next shutdown live and show in hardcharger_settings(). All 4 apply. Difference on
purpose: graceful_shutdown defaults to true in Hardcharger (1.x: false), because
GizmoData Cloud gives every pod a grace period to drain in.
"""

import pytest


@pytest.fixture(scope="module")
def admin(module_launcher):
    return module_launcher.start().admin()


def cell(session, name: str, column: str):
    return session.scalar(sql=f"SELECT CAST({column} AS VARCHAR) FROM hardcharger_settings() WHERE name = 'hardcharger.{name}'")


def test_graceful_shutdown_is_live_adjustable(admin):
    admin.execute(sql="SET GLOBAL hardcharger.graceful_shutdown = true")
    assert cell(admin, "graceful_shutdown", "value") == "true"
    admin.execute(sql="SET GLOBAL hardcharger.graceful_shutdown = false")
    assert cell(admin, "graceful_shutdown", "value") == "false"
    assert cell(admin, "graceful_shutdown", "global_value") == "false"
    assert cell(admin, "graceful_shutdown", "scope") == "GLOBAL"
    admin.execute(sql="RESET GLOBAL hardcharger.graceful_shutdown")
    assert cell(admin, "graceful_shutdown", "value") == "true"


def test_grace_period_is_live_adjustable(admin):
    admin.execute(sql="SET GLOBAL hardcharger.shutdown_grace_period_seconds = 600")
    assert cell(admin, "shutdown_grace_period_seconds", "value") == "600"
    admin.execute(sql="SET GLOBAL hardcharger.shutdown_grace_period_seconds = 0")
    assert cell(admin, "shutdown_grace_period_seconds", "value") == "0"
    assert cell(admin, "shutdown_grace_period_seconds", "default_value") == "300"


def test_grace_period_rejects_negative(admin):
    assert "shutdown_grace_period_seconds" in admin.error(sql="SET GLOBAL hardcharger.shutdown_grace_period_seconds = -5")["detail"]


def test_graceful_shutdown_rejects_invalid_value(admin):
    assert "graceful_shutdown" in admin.error(sql="SET GLOBAL hardcharger.graceful_shutdown = 'maybe'")["detail"]


def test_non_admins_cannot_change_them(module_launcher, admin):
    from warp_client import mint_server_token
    server = module_launcher.started[0]
    user = server.client(token=mint_server_token(secret_key=server.secret_key, subject="u", role="user")).new_session()
    problem = user.error(sql="SET GLOBAL hardcharger.shutdown_grace_period_seconds = 1")
    assert problem["code"] == "permission_denied" and "Only admin users" in problem["detail"], problem
