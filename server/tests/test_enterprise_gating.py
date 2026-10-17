"""Port of GizmoSQL 1.x tests/integration/test_enterprise_gating.cpp (7 cases).

Without a license (Core edition): KILL SESSION, SET gizmosql.session_tag and SET
gizmosql.query_tag fail with a license error; the statement queue's options stop the
server at startup; ordinary SQL works and GIZMOSQL_EDITION() is Core. All 7 apply.
Strengthened: the license errors must name the feature, and the startup failures must
exit 1 with the license message rather than any non-zero code.
"""

import pytest


@pytest.fixture(scope="module")
def session(module_launcher):
    return module_launcher.start().admin()


def license_error(problem: dict, feature: str) -> None:
    assert problem["code"] == "permission_denied", problem
    assert "enterprise" in problem["detail"].lower() and f"'{feature}'" in problem["detail"], problem


def test_kill_session_rejected_without_license(session):
    license_error(problem=session.error(sql="KILL SESSION '00000000-0000-0000-0000-000000000000'"), feature="kill_session")


def test_basic_queries_work_without_license(session):
    assert session.query(sql="SELECT 1 AS one").num_rows == 1


def test_edition_returns_core_without_license(session):
    assert session.scalar(sql="SELECT GIZMOSQL_EDITION()") == "Core"
    assert session.scalar(sql="SELECT HARDCHARGER_EDITION()") == "Core"


def test_set_session_tag_rejected_without_license(session):
    license_error(problem=session.error(sql="""SET gizmosql.session_tag = '{"test":"gating"}'"""), feature="instrumentation")


def test_set_query_tag_rejected_without_license(session):
    license_error(problem=session.error(sql="""SET gizmosql.query_tag = '{"test":"gating"}'"""), feature="instrumentation")


@pytest.mark.parametrize("args", [["--max-concurrent-statements", "8"],
                                  ["--max-queue-wait", "60"]],
                         ids=["max_concurrent_statements", "queue_tuning_knob"])
def test_statement_queue_options_stop_an_unlicensed_server(launcher, args):
    server = launcher.start(args=args, expect_up=False)
    assert server.process.wait(timeout=20) == 1, server.log()
    assert "statement_queue" in server.log() and "enterprise" in server.log().lower(), server.log()
