"""Port of GizmoSQL 1.x tests/integration/test_memory_limit.cpp (2 cases).

memory_limit (--memory-limit, GIZMOSQL_MEMORY_LIMIT) reaches DuckDB at startup, and a
value that is not a size or percentage stops the server with "Invalid memory_limit".
Both cases apply; the second also covers the 1.x environment name.
"""


def test_current_setting_reflects_startup_limit(launcher):
    server = launcher.start(args=["--memory-limit", "512MiB"])
    setting = server.admin().scalar(sql="SELECT current_setting('memory_limit')")
    assert "512" in setting.lower() and "mib" in setting.lower(), setting


def test_rejects_disallowed_characters(launcher):
    server = launcher.start(env={"GIZMOSQL_MEMORY_LIMIT": "512MB'; DROP TABLE t; --"}, expect_up=False)
    assert server.process.wait(timeout=20) == 2
    assert "Invalid memory_limit" in server.log(), server.log()
