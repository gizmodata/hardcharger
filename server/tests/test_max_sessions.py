"""Port of GizmoSQL 1.x tests/integration/test_max_sessions.cpp (4 cases).

max_sessions (--max-sessions, HARDCHARGER_MAX_SESSIONS) caps non-admin sessions; the next
one gets 429 too_many_sessions (Chargewire §5.1) with 1.x's "max-sessions ... Unavailable"
text. Admins are never capped, closing a session frees its place, and so does KILL
SESSION. The settings case checks hardcharger_settings()'s
cells for the startup settings and that SET can't change them.

Differences from 1.x, on purpose: session_idle_timeout defaults to 900 s (the Chargewire SDK's
default; 1.x: 0, never), health_check_interval_seconds to 5 s (1.x: 0, on demand), and
version is Hardcharger's ("0.x.y", as --version prints it).
"""

import pytest

from conftest import problem


@pytest.fixture
def server(launcher):
    return launcher.start(args=["--max-sessions", "2"])


def user_client(server, subject: str):
    from chargewire_client import mint_server_token
    return server.client(token=mint_server_token(secret_key=server.secret_key, subject=subject, role="user"))


def test_under_cap_succeeds(server):
    first = user_client(server=server, subject="u1").new_session()
    second = user_client(server=server, subject="u2").new_session()
    assert first.scalar(sql="SELECT 1") == 1 and second.scalar(sql="SELECT 2") == 2


def test_rejects_beyond_cap_with_unavailable(server):
    first = user_client(server=server, subject="u1").new_session()
    user_client(server=server, subject="u2").new_session()
    body = problem(response=user_client(server=server, subject="u3").open_session(), status=429,
                   code="too_many_sessions")
    assert "max-sessions" in body["detail"] and "Unavailable" in body["detail"], body
    assert first.scalar(sql="SELECT 2") == 2  # the open ones keep working
    first.close()
    assert user_client(server=server, subject="u3").new_session().scalar(sql="SELECT 3") == 3


def test_admin_bypasses_cap_and_kill_frees_slot(server):
    victim = user_client(server=server, subject="u1").new_session()
    user_client(server=server, subject="u2").new_session()
    admin = server.admin()  # past the cap
    assert admin.scalar(sql="SELECT 1") == 1
    problem(response=user_client(server=server, subject="u3").open_session(), status=429, code="too_many_sessions")
    victim_id = victim.scalar(sql="SELECT HARDCHARGER_CURRENT_SESSION()")
    admin.execute(sql=f"KILL SESSION '{victim_id}'")
    assert user_client(server=server, subject="u3").new_session().scalar(sql="SELECT 3") == 3


def test_startup_only_settings_are_visible_but_not_settable(server):
    admin = server.admin()

    def cell(name: str, column: str):
        return admin.scalar(sql=f"SELECT CAST({column} AS VARCHAR) FROM hardcharger_settings() WHERE name = '{name}'")

    assert cell("hardcharger.max_sessions", "value") == "2"
    assert cell("hardcharger.max_sessions", "scope") == "STARTUP"
    assert cell("hardcharger.max_sessions", "settable") == "false"
    assert cell("hardcharger.max_sessions", "cli_flag") == "--max-sessions"
    assert cell("hardcharger.max_sessions", "env_var") == "HARDCHARGER_MAX_SESSIONS"
    assert cell("hardcharger.session_idle_timeout", "value") == "900"
    assert cell("hardcharger.session_idle_timeout", "settable") == "false"
    assert cell("hardcharger.session_idle_timeout", "cli_flag") == "--session-idle-timeout"
    assert cell("hardcharger.query_timeout", "settable") == "true"
    assert cell("hardcharger.query_timeout", "cli_flag") == "--query-timeout-seconds"
    assert cell("hardcharger.version", "value") == admin.scalar(sql="SELECT HARDCHARGER_VERSION()")
    assert cell("hardcharger.read_only", "value") == "false"
    assert cell("hardcharger.read_only", "cli_flag") == "--read-only"
    assert cell("hardcharger.admin_bypass_queue_default", "value") == "true"
    assert cell("hardcharger.health_check_interval_seconds", "value") == "5"
    assert cell("hardcharger.session_log_level", "scope") == "STARTUP"
    assert cell("hardcharger.instance_id", "value") == admin.scalar(sql="SELECT HARDCHARGER_CURRENT_INSTANCE()")
    assert cell("hardcharger.cluster_id", "value") == ""
    assert cell("hardcharger.enable_instrumentation", "value") == "false"
    assert cell("hardcharger.instrumentation_catalog", "value") == ""

    user = user_client(server=server, subject="u1").new_session()
    assert user.scalar(sql="SELECT value FROM hardcharger_settings() WHERE name = 'hardcharger.read_only'") == "false"
    assert user.scalar(sql="SELECT value FROM hardcharger_settings() WHERE name = 'hardcharger.session_idle_timeout'") == "900"
    hidden = user.scalar(sql="SELECT count(*) FROM hardcharger_settings() WHERE name IN ("
                             "'hardcharger.enable_instrumentation', 'hardcharger.instrumentation_catalog', "
                             "'hardcharger.instrumentation_schema', 'hardcharger.enable_catalog_logging', "
                             "'hardcharger.log_catalog', 'hardcharger.log_schema')")
    assert hidden == 0, "admin-only settings rows leaked to a non-admin"

    for sql in ["SET GLOBAL hardcharger.max_sessions = 5", "SET hardcharger.session_idle_timeout = 30"]:
        detail = admin.error(sql=sql)["detail"]
        assert "fixed at server startup" in detail and "--" in detail, detail
