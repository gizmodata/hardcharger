"""Port of GizmoSQL 1.x tests/integration/test_auth_log_level.cpp (2 cases).

The first use of a bearer token logs "Bearer Token was validated successfully" at INFO;
later uses of the same token log it at DEBUG, so with auth_log_level INFO they don't
appear at all, even when the logger itself is at DEBUG (1.x issue #136: never promoted).
1.x swapped in a capturing logger; here the server writes JSON lines and the test reads
them. Added: with auth_log_level DEBUG the repeats do appear, at DEBUG.
"""

MESSAGE = "Bearer Token was validated successfully"


def bearer_lines(server) -> list[dict]:
    return [r for r in server.log_records() if r.get("message") == MESSAGE]


def use_token_three_times(server) -> None:
    client = server.client()
    session = client.new_session()
    session.scalar(sql="SELECT 1")
    session.scalar(sql="SELECT 2")


def test_repeat_bearer_token_logs_suppressed_at_info_threshold(launcher):
    server = launcher.start(env={"GIZMOSQL_LOG_LEVEL": "debug", "GIZMOSQL_LOG_FORMAT": "json",
                                 "GIZMOSQL_AUTH_LOG_LEVEL": "info"})
    use_token_three_times(server=server)
    lines = bearer_lines(server=server)
    assert [line["level"] for line in lines] == ["info"], lines


def test_first_seen_bearer_token_logged_at_info_threshold(launcher):
    server = launcher.start(env={"GIZMOSQL_LOG_FORMAT": "json"})
    use_token_three_times(server=server)
    lines = bearer_lines(server=server)
    assert len(lines) == 1 and lines[0]["level"] == "info" and lines[0]["first_use"] == "true", lines


def test_repeats_appear_at_debug_when_auth_log_level_is_debug(launcher):
    server = launcher.start(env={"GIZMOSQL_LOG_LEVEL": "debug", "GIZMOSQL_LOG_FORMAT": "json",
                                 "GIZMOSQL_AUTH_LOG_LEVEL": "debug"})
    use_token_three_times(server=server)
    levels = [line["level"] for line in bearer_lines(server=server)]
    assert levels[0] == "info" and len(levels) >= 3 and set(levels[1:]) == {"debug"}, levels
