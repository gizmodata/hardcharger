"""hardcharger_settings()'s default_value column against the server itself.

The defaults come from the configuration's own defaults, so on a server started with no
options, every setting's live value is its reported default. The few without a fixed
default (the version, this process's id) are named, and checked for what they are.
"""

import re
import json
import subprocess
import yaml

from conftest import BINARY, free_port

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
    server = launcher.start()
    by_name = settings(server=server)
    fixture_overrides = {"hardcharger.host": "127.0.0.1", "hardcharger.port": str(server.port)}
    checked = []
    for name, row in by_name.items():
        if name in NO_FIXED_DEFAULT:
            continue
        assert row["value"] == fixture_overrides.get(name, row["default_value"]), row
        if row["global_value"] is not None:
            assert row["global_value"] == fixture_overrides.get(name, row["default_value"]), row
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


def test_operational_settings_report_configuration_and_visibility(launcher, tmp_path):
    config = {
        "max_result_partitions": 4, "result_batch_rows": 4096, "result_retention_seconds": 120,
        "default_catalog": "settings_catalog", "default_schema": "reports",
        "host": "127.0.0.1", "port": free_port(), "health_port": free_port(),
        "web_ui": False, "web_ui_remote_servers": False,
        "metrics_port": 0, "metrics_bind_address": "127.0.0.1",
        "block_sensitive_paths": False, "block_unredacted_secrets": False,
    }
    path = tmp_path / "operations.yaml"
    path.write_text(yaml.safe_dump({**config, "enable_metrics": False,
                                   "init_sql_commands": "ATTACH ':memory:' AS settings_catalog; CREATE SCHEMA settings_catalog.reports"}),
                    encoding="utf-8")
    server = launcher.start(args=["--config", str(path)], port=config["port"])
    admin = server.admin()
    rows = {r["name"]: r for r in admin.rows(sql="SELECT * FROM hardcharger_settings()")}
    options = {o["name"]: o for o in json.loads(subprocess.check_output(args=[str(BINARY), "--print-options-json"]))["options"]}
    for name, value in config.items():
        row = rows[f"hardcharger.{name}"]
        expected = json.dumps(value, separators=(",", ":")) if isinstance(value, (list, bool)) else str(value)
        assert row["value"] == expected and row["global_value"] == expected, row
        assert row["scope"] == "STARTUP" and row["settable"] is False and row["session_value"] is None, row
        assert row["cli_flag"] == options[name]["flag"] and row["env_var"] == options[name]["env"], row
        expected_default = options[name]["default"]
        assert row["default_value"] == expected_default, row
        for command in (f"SET GLOBAL hardcharger.{name} = 'changed'", f"RESET hardcharger.{name}"):
            assert "fixed at server startup" in admin.error(sql=command)["detail"]
    public = {"max_result_partitions", "result_batch_rows", "result_retention_seconds", "web_ui", "web_ui_remote_servers"}
    user = server.as_role(role="readonly")
    visible = {r["name"] for r in user.rows(sql="SELECT name FROM hardcharger_settings()")}
    assert visible & {f"hardcharger.{name}" for name in config} == {f"hardcharger.{name}" for name in public}
    # Credentials are not introduced into the inventory, even for administrators.
    assert not {"hardcharger.admin_password", "hardcharger.secret_key", "hardcharger.oauth_client_secret",
                "hardcharger.audit_chain_key", "hardcharger.rbac_store_uri"} & rows.keys()
