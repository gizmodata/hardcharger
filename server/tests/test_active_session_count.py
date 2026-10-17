"""Port of GizmoSQL 1.x tests/integration/test_active_session_count.cpp (2 cases).

1.x read an in-process counter. Warp SQL sessions start at POST /sessions, so here the
count is read where operators see it: hardcharger_sessions() (an admin lists every open
session). Before anyone opens a session the count is 0 (read through the first one
opened, which then counts itself); after a second session it is 2; closing one drops it.
"""


def test_session_count_starts_at_zero(launcher):
    server = launcher.start()
    first = server.admin()
    rows = first.rows(sql="SELECT session_id, is_current FROM hardcharger_sessions()")
    assert len(rows) == 1 and rows[0]["is_current"] is True, rows  # nothing but the reader


def test_session_count_after_query(launcher):
    server = launcher.start()
    first = server.admin()
    second = server.admin()
    assert second.scalar(sql="SELECT 1") == 1
    assert first.scalar(sql="SELECT count(*) FROM hardcharger_sessions()") == 2
    second.close()
    assert first.scalar(sql="SELECT count(*) FROM hardcharger_sessions()") == 1
