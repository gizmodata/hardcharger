"""The protocol connection property, independent of token roles and server file mode."""

import json

import pyarrow as pa
import pytest

from rbac import rbac_env


def assert_read_only(problem):
    assert problem["code"] == "read_only", problem
    assert problem["sqlstate"] == "25006", problem


@pytest.fixture(scope="module")
def property_server(module_launcher):
    return module_launcher.start(args=["--enable-instrumentation"])


def test_reader_and_writer_share_database(property_server):
    writer = property_server.admin()
    writer.execute(sql="CREATE TABLE property_rows (n INTEGER)")
    writer.execute(sql="INSERT INTO property_rows VALUES (42)")
    reader = property_server.client().new_session(options={"read_only": True})
    assert reader.options()["read_only"] is True
    assert reader.scalar(sql="SELECT n FROM property_rows") == 42
    assert_read_only(reader.error(sql="INSERT INTO property_rows VALUES (43)"))
    response = reader.ingest(table=pa.table({"n": [44]}), name="property_rows", mode="append")
    assert_read_only(response.json())
    writer.execute(sql="INSERT INTO property_rows VALUES (45)")
    assert reader.scalar(sql="SELECT count(*) FROM property_rows") == 2
    reader.close()
    writer.close()


@pytest.mark.parametrize("function", [
    "hardcharger_settings", "hardcharger_sessions", "hardcharger_active_connections",
    "hardcharger_server_info", "hardcharger_monitor_info", "hardcharger_live_queries",
    "hardcharger_server_settings", "hardcharger_session_settings", "hardcharger_grants",
    "hardcharger_roles", "hardcharger_users", "hardcharger_role_members",
    "hardcharger_policies", "hardcharger_effective_privileges",
])
def test_introspection_remains_available(property_server, function):
    reader = property_server.client().new_session(options={"read_only": True})
    reader.query(sql=f"SELECT * FROM {function}()")
    reader.close()


def test_local_settings_and_batch_preflight(property_server):
    reader = property_server.client().new_session(options={"read_only": True})
    reader.execute(sql="SET hardcharger.query_timeout = 10")
    reader.execute(sql="""SET hardcharger.session_tag = '{"app":"reader"}'""")
    assert_read_only(reader.error(sql="SET GLOBAL hardcharger.query_timeout = 20"))
    assert_read_only(reader.error(sql="SET hardcharger.capture_query_profile = 'detailed'"))
    assert_read_only(reader.error(sql="""SET hardcharger.session_tag = '{"app":"changed"}'; """
                                      "SET GLOBAL hardcharger.query_timeout = 20"))
    tag = reader.scalar(sql="SELECT value FROM hardcharger_settings() "
                            "WHERE name = 'hardcharger.session_tag'")
    assert json.loads(s=tag) == {"app": "reader"}
    reader.execute(sql="RESET hardcharger.query_timeout")
    reader.close()


@pytest.mark.parametrize("sql", [
    "SELECT hardcharger_cancel('{target}')",
    "SELECT hardcharger_set_session_setting('{target}', 'query_timeout', '10')",
    "SELECT hardcharger_reset_session_setting('{target}', 'query_timeout')",
])
def test_administration_refused_after_transition(property_server, sql):
    target = property_server.admin()
    reader = property_server.admin()
    statement = sql.format(target=target.id)
    prepared = reader.prepare(sql=statement)
    response = reader.set_options(options={"read_only": True})
    assert response.status_code == 200, response.text
    assert_read_only(reader.error(sql=statement))
    response = reader.execute_prepared(prepared_id=prepared["prepared_id"])
    assert_read_only(response.json())
    assert_read_only(reader.error(sql=f"KILL SESSION '{target.id}'"))
    assert target.scalar(sql="SELECT 1") == 1
    assert reader.scalar(sql="SELECT 2") == 2
    reader.close()
    target.close()


def test_rbac_commands_follow_connection_mode(launcher, store):
    server = launcher.start(env=rbac_env(store_uri=store.new_database()))
    reader = server.admin()
    prepared = reader.prepare(sql="CREATE ROLE property_prepared_role")
    response = reader.set_options(options={"read_only": True})
    assert response.status_code == 200, response.text
    assert_read_only(reader.error(sql="CREATE ROLE property_direct_role"))
    response = reader.execute_prepared(prepared_id=prepared["prepared_id"])
    assert_read_only(response.json())
    reader.query(sql="SHOW GRANTS")
    reader.query(sql="SELECT * FROM hardcharger_roles()")
    writer = server.admin()
    writer.execute(sql="CREATE ROLE property_writer_role")
    writer.execute(sql="DROP ROLE property_writer_role")


def test_server_owned_profiling_remains_available(launcher):
    server = launcher.start(args=["--enable-instrumentation", "--capture-query-profile", "standard"])
    reader = server.client().new_session(options={"read_only": True})
    assert reader.scalar(sql="SELECT 42") == 42
    # Server-owned profiling must still turn off before credential-bearing reads,
    # then resume for ordinary queries on the same read-only connection.
    rows = reader.rows(sql="SELECT current_setting('enable_profiling') AS mode, "
                           "struct_pack(token := 'readonly-profile-fixture') AS credentials")
    assert rows[0]["mode"] is None
    prepared = reader.prepare(sql="SELECT struct_pack(token := 'readonly-prepared-profile-fixture')")
    result = reader.execute_prepared(prepared_id=prepared["prepared_id"])
    assert result.status_code == 200, result.text
    assert_read_only(reader.error(sql="CREATE TABLE forbidden_profile_write (n INTEGER)"))
    assert reader.scalar(sql="SELECT 43") == 43
    assert "readonly-profile-fixture" not in server.log()
    assert "readonly-prepared-profile-fixture" not in server.log()
