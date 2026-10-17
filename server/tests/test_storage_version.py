"""Port of GizmoSQL 1.x tests/integration/test_storage_version.cpp (3 cases).

storage_version (--storage-version, HARDCHARGER_STORAGE_VERSION) stamps new database files:
duckdb_databases().tags['storage_version'] is DuckDB 2.0's own version when unset and
v1.0.0+ when pinned to v1.0.0. All three apply; on DuckDB 2.0 the "latest" and "unset"
cases are the same assertion from two configurations.
"""

SQL = "SELECT tags['storage_version'] FROM duckdb_databases() WHERE database_name = 'storage_version_test'"


def tag(launcher, tmp_path, args=None, env=None) -> str:
    server = launcher.start(args=["--database", str(tmp_path / "storage_version_test.duckdb"), *(args or [])],
                            env=env)
    value = server.admin().scalar(sql=SQL)
    assert value, "no storage_version tag"
    return value


def test_tag_is_duckdb_2_when_storage_version_unset(launcher, tmp_path):
    assert tag(launcher=launcher, tmp_path=tmp_path) == "v2.0.0+"


def test_tag_reflects_latest_storage_version(launcher, tmp_path):
    assert tag(launcher=launcher, tmp_path=tmp_path, args=["--storage-version", "latest"]) != "v1.0.0+"


def test_tag_reflects_pinned_older_storage_version(launcher, tmp_path):
    assert tag(launcher=launcher, tmp_path=tmp_path, env={"HARDCHARGER_STORAGE_VERSION": "v1.0.0"}) == "v1.0.0+"
