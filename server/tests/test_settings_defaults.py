"""hardcharger_settings()'s default_value column against the server itself.

The defaults come from the configuration's own defaults, so on a server started with no
options, every setting's live value is its reported default. The few without a fixed
default (the version, this process's id) are named, and checked for what they are.
"""

import re

from conftest import BINARY

# Settings whose value on a fresh server is not a fixed default.
NO_FIXED_DEFAULT = {
    "hardcharger.version",      # the build's version
    "hardcharger.instance_id",  # a new UUID for every process
}


def settings(server) -> dict[str, dict]:
    rows = server.admin().rows(sql="SELECT name, value, global_value, default_value, scope FROM hardcharger_settings()")
    assert rows, "hardcharger_settings() returned no rows"
    return {row["name"]: row for row in rows}


def test_every_reported_default_is_the_live_value_on_a_fresh_server(launcher):
    by_name = settings(server=launcher.start())
    checked = []
    for name, row in by_name.items():
        if name in NO_FIXED_DEFAULT:
            continue
        assert row["value"] == row["default_value"], row
        if row["global_value"] is not None:
            assert row["global_value"] == row["default_value"], row
        checked.append(name)
    # Every setting but the named few was compared, and the named ones exist.
    assert len(checked) == len(by_name) - len(NO_FIXED_DEFAULT), (checked, sorted(by_name))
    assert "hardcharger.max_queued_statements" in checked
    assert re.fullmatch(r"[0-9a-f-]{36}", by_name["hardcharger.instance_id"]["value"]), by_name["hardcharger.instance_id"]


def test_max_queued_statements_defaults_to_eight_times_concurrency(launcher):
    # The documented default (-1: 8 x max_concurrent_statements), as the option has it.
    row = settings(server=launcher.start())["hardcharger.max_queued_statements"]
    assert row["default_value"] == "-1", row
    assert row["value"] == "-1", row
    usage = __import__("subprocess").run(args=[str(BINARY), "--help"], capture_output=True, text=True,
                                         check=True).stdout
    assert "-1: 8 x concurrency" in usage, usage


def test_a_changed_setting_differs_from_its_default(launcher):
    # The positive twin: the comparison above sees a value that isn't the default.
    server = launcher.start(args=["--max-queued-statements", "5", "--max-queue-wait", "60"])
    by_name = settings(server=server)
    assert by_name["hardcharger.max_queued_statements"]["value"] == "5", by_name["hardcharger.max_queued_statements"]
    assert by_name["hardcharger.max_queued_statements"]["default_value"] == "-1"
    assert by_name["hardcharger.max_queue_wait"]["value"] == "60", by_name["hardcharger.max_queue_wait"]
    assert by_name["hardcharger.max_queue_wait"]["default_value"] == "300"
    # RESET goes back to the startup value, not to the built-in default.
    admin = server.admin()
    admin.execute(sql="SET GLOBAL hardcharger.max_queued_statements = 9")
    assert settings(server=server)["hardcharger.max_queued_statements"]["value"] == "9"
    admin.execute(sql="RESET GLOBAL hardcharger.max_queued_statements")
    assert settings(server=server)["hardcharger.max_queued_statements"]["value"] == "5"
    admin.execute(sql="SET GLOBAL hardcharger.max_queued_statements = -1")
    assert settings(server=server)["hardcharger.max_queued_statements"]["value"] == "-1"
