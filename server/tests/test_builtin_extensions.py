"""Only DuckDB’s four standard built-ins work with an empty cache and no repository.

Explicitly disable automation to model an air-gapped deployment. Runtime extension
coverage is in test_loadable_extensions.py and test_extension_loading.py.
"""

import pytest

BUILT_IN = ["core_functions", "parquet", "json", "icu"]
OPTIONAL = ["httpfs", "ducklake", "postgres_scanner", "autocomplete", "tpch"]
NOWHERE = "http://127.0.0.1:9/no-extensions"


@pytest.fixture(scope="module")
def server(module_launcher, tmp_path_factory):
    empty = tmp_path_factory.mktemp("no-extensions")
    init = (f"SET GLOBAL custom_extension_repository = '{NOWHERE}'; "
            f"SET GLOBAL autoinstall_extension_repository = '{NOWHERE}'; "
            f"SET GLOBAL extension_directory = '{empty}'; "
            "SET GLOBAL autoinstall_known_extensions = false; SET GLOBAL autoload_known_extensions = false;")
    return module_launcher.start(env={"HARDCHARGER_INIT_SQL_COMMANDS": init})


def test_repositories_are_out_of_reach_and_nothing_autoinstalls(server):
    session = server.admin()
    settings = session.rows(sql="SELECT name, value FROM duckdb_settings() WHERE name IN "
                                "('autoinstall_known_extensions', 'autoload_known_extensions')")
    assert {r["name"]: r["value"] for r in settings} == {
        "autoinstall_known_extensions": "false", "autoload_known_extensions": "false"}
    detail = session.error(sql="INSTALL spatial")["detail"]
    assert "127.0.0.1:9" in detail, detail


def test_only_standard_extensions_are_linked_and_loaded(server):
    rows = server.admin().rows(sql="SELECT extension_name, loaded, install_mode FROM duckdb_extensions()")
    by_name = {r["extension_name"]: r for r in rows}
    assert {r["extension_name"] for r in rows if r["install_mode"] == "STATICALLY_LINKED"} == set(BUILT_IN)
    for name in BUILT_IN:
        assert by_name[name]["loaded"]
    for name in OPTIONAL:
        assert not by_name[name]["loaded"] and by_name[name]["install_mode"] != "STATICALLY_LINKED"


def test_load_of_standard_builtins_needs_no_repository(server):
    session = server.admin()
    for name in BUILT_IN:
        session.execute(sql=f"LOAD {name}")
    detail = session.error(sql="LOAD tpch")["detail"]
    assert "tpch" in detail and "INSTALL" in detail, detail


def test_parquet_json_and_icu(server, tmp_path):
    session = server.admin()
    path = tmp_path / "answer.parquet"
    session.execute(sql=f"COPY (SELECT 42 AS answer) TO '{path}' (FORMAT parquet)")
    assert session.scalar(sql=f"SELECT answer FROM read_parquet('{path}')") == 42
    assert session.scalar(sql="""SELECT json_extract_string('{"a": {"b": "json"}}', '$.a.b')""") == "json"
    assert session.scalar(sql="SELECT (TIMESTAMPTZ '2026-01-01 12:00:00+00' AT TIME ZONE 'Asia/Tokyo')::VARCHAR") \
        == "2026-01-01 21:00:00"
