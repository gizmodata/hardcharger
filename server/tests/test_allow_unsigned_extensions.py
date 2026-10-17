"""Port of GizmoSQL 1.x tests/integration/test_allow_unsigned_extensions.cpp (2 cases).

allow_unsigned_extensions is a DuckDB setting fixed when the instance opens, so it must
reach the instance's configuration. Both cases apply.
"""


def test_disabled_by_default(launcher):
    assert launcher.start().admin().scalar(sql="SELECT current_setting('allow_unsigned_extensions')") is False


def test_enabled_via_config(launcher):
    server = launcher.start(env={"HARDCHARGER_ALLOW_UNSIGNED_EXTENSIONS": "true"})
    assert server.admin().scalar(sql="SELECT current_setting('allow_unsigned_extensions')") is True
