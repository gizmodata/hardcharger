"""Port of GizmoSQL 1.x tests/integration/test_system_managed_catalog_visibility.cpp (2 cases).

An admin sees the instrumentation catalog everywhere (catalogs metadata,
duckdb_databases(), tables metadata, hardcharger_settings()). A non-admin sees it nowhere
and can't read it ("Only administrators").

For non-admins: the catalogs, db-schemas and tables metadata requests (Warp SQL §10;
1.x: GetCatalogs, GetDbSchemas, GetTables), the instrumentation rows of
hardcharger_settings(), refusing any statement that names the catalog, and SQL's own catalog
functions (duckdb_databases(), duckdb_schemas(), duckdb_tables(), information_schema,
SHOW DATABASES, SHOW ALL TABLES), whose rows access control's plan rewrite filters.
"""

import pytest

CATALOG = "_hardcharger_instr"


@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start(args=["--enable-instrumentation"])


@pytest.fixture(scope="module")
def user(server):
    # 1.x minted this token without an instance_id claim; it is accepted all the same.
    return server.as_role(role="user", subject="visibility-user")


def test_admin_sees_instrumentation_catalog(server):
    admin = server.admin()
    assert CATALOG in admin.metadata(kind="catalogs").column("catalog_name").to_pylist()
    assert CATALOG in admin.query(sql="SELECT database_name FROM duckdb_databases()").column(0).to_pylist()
    tables = admin.metadata(kind="tables", catalog=CATALOG).column("table_name").to_pylist()
    assert {"instances", "sessions", "sql_statements", "sql_executions"} <= set(tables), tables
    assert admin.scalar(sql="SELECT value FROM hardcharger_settings() WHERE name = 'hardcharger.instrumentation_catalog'") == CATALOG
    assert admin.scalar(sql="SELECT value FROM hardcharger_settings() WHERE name = 'hardcharger.enable_instrumentation'") == "true"


def test_non_admin_metadata_never_shows_it(user):
    catalogs = user.metadata(kind="catalogs").column("catalog_name").to_pylist()
    assert catalogs and CATALOG not in catalogs, catalogs
    assert CATALOG not in user.metadata(kind="db-schemas").column("catalog_name").to_pylist()
    assert CATALOG not in user.metadata(kind="tables").column("catalog_name").to_pylist()
    hidden = user.query(sql="SELECT name FROM hardcharger_settings() WHERE name LIKE 'hardcharger.%instrumentation%' "
                            "OR name LIKE 'hardcharger.log_%'")
    assert hidden.num_rows == 0, hidden.to_pylist()
    assert "Only administrators" in user.error(sql=f"SELECT * FROM {CATALOG}.main.sessions LIMIT 1")["detail"]


def test_non_admin_sql_catalog_functions_never_show_it(user):
    for sql in ["SELECT database_name FROM duckdb_databases()", "SELECT DISTINCT database_name FROM duckdb_schemas()",
                "SELECT DISTINCT database_name FROM duckdb_tables()",
                "SELECT DISTINCT catalog_name FROM information_schema.schemata",
                "SELECT DISTINCT table_catalog FROM information_schema.tables", "SHOW DATABASES",
                "SELECT DISTINCT database FROM (SHOW ALL TABLES)"]:
        assert CATALOG not in user.query(sql=sql).column(0).to_pylist(), sql
