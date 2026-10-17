"""Tenants: many customers on one shared server. A tenant's users and roles are named
tenant/name and see only the catalogs assigned to their tenant; everything else looks
exactly like something that does not exist."""

import pytest

from rbac import WarpError, expect_allowed, mint, start_rbac_server


@pytest.fixture(scope="module")
def server(module_launcher, store, tmp_path_factory):
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               args=["--database", str(tmp_path_factory.mktemp("db") / "rbac_tenants.duckdb")])
    admin = server.admin()
    for sql in [
        "CREATE TABLE shared_default AS SELECT 'default-secret' AS s",
        "ATTACH ':memory:' AS acme_db", "ATTACH ':memory:' AS beta_db",
        "CREATE TABLE acme_db.main.orders AS SELECT 1 AS id, 'acme-order' AS note",
        "CREATE TABLE beta_db.main.secrets AS SELECT 1 AS id, 'beta-secret' AS hush",
        "CREATE VIEW beta_db.main.secrets_view AS SELECT hush FROM beta_db.main.secrets",
        "CREATE TABLE beta_db.main.secrets_keyed (id INT PRIMARY KEY)",
        "CREATE INDEX secrets_idx ON beta_db.main.secrets (hush)",
        "CREATE TENANT acme", "CREATE TENANT beta",
        "ALTER CATALOG acme_db SET TENANT acme", "ALTER CATALOG beta_db SET TENANT beta",
        "ALTER TENANT acme SET DEFAULT CATALOG acme_db",
        "CREATE USER acme/boss", "GRANT acme/tenant_admin TO acme/boss",
        "CREATE USER beta/bea", "GRANT beta/tenant_admin TO beta/bea",
    ]:
        expect_allowed(client=admin, sql=sql)
    boss = server.user(name="acme/boss")
    for sql in ["CREATE USER acme/joe", "CREATE ROLE acme/readers",
                "GRANT SELECT ON acme_db.main.orders TO acme/readers", "GRANT acme/readers TO acme/joe",
                "CREATE ROLE acme/builders", "GRANT CREATE VIEW ON SCHEMA acme_db.main TO acme/builders",
                "GRANT acme/builders TO acme/joe"]:
        expect_allowed(client=boss, sql=sql)
    server.admin_client = admin
    return server


@pytest.fixture
def admin(server):
    return server.admin_client


@pytest.fixture
def joe(server):
    return server.user(name="acme/joe")


@pytest.fixture
def boss(server):
    return server.user(name="acme/boss")


@pytest.fixture
def bea(server):
    return server.user(name="beta/bea")


def outcome(client, sql: str):
    """('ok', '') or (code, detail): what the user is told."""
    try:
        client.run(sql=sql)
        return ("ok", "")
    except WarpError as error:
        return (error.code, error.detail)


def column(table, name: str | None = None) -> set:
    return set(table.column(name or table.column_names[0]).to_pylist())


def test_a_tenants_users_work_in_their_own_catalogs(joe, boss):
    # The default catalog is the tenant's, so unqualified names work.
    assert column(table=expect_allowed(client=joe, sql="SELECT note FROM orders")) == {"acme-order"}
    expect_allowed(client=joe, sql="SELECT note FROM acme_db.main.orders")
    # The tenant admin may create in the tenant's catalog and share it.
    expect_allowed(client=boss, sql="CREATE TABLE acme_db.main.made_by_boss AS SELECT 7 AS x")
    expect_allowed(client=boss, sql="GRANT SELECT ON acme_db.main.made_by_boss TO acme/readers")
    expect_allowed(client=joe, sql="SELECT x FROM made_by_boss")


def test_another_tenants_catalog_looks_exactly_like_a_missing_one(joe):
    # The reference: a catalog that does not exist at all.
    missing = outcome(client=joe, sql="SELECT * FROM no_such_db.main.t")
    assert missing[0] == "permission_denied" and "Hardcharger blocked" in missing[1], missing
    for sql in ["SELECT * FROM beta_db.main.secrets", "SELECT * FROM beta_db.secrets",
                "SELECT hush FROM beta_db.main.secrets", "SELECT no_such_column FROM beta_db.main.secrets",
                "SELECT * FROM beta_db.main.no_such_table", "SELECT * FROM rbac_tenants.main.shared_default",
                "SELECT * FROM shared_default", "SELECT * FROM secrets", "USE beta_db",
                "CREATE TABLE beta_db.main.planted AS SELECT 1", "DROP TABLE beta_db.main.secrets",
                "INSERT INTO beta_db.main.secrets VALUES (2, 'x')",
                # DuckDB binds a view's query when it is created: whether that works would
                # say whether the table exists.
                "CREATE VIEW acme_db.main.oracle AS SELECT * FROM beta_db.main.secrets",
                "CREATE VIEW acme_db.main.oracle AS SELECT * FROM beta_db.secrets",
                "DESCRIBE SELECT * FROM beta_db.main.secrets", "SHOW beta_db.main.secrets"]:
        assert outcome(client=joe, sql=sql) == missing, f"{sql} told joe more than a missing catalog would"
    # Nothing in an error ever names the other tenant's objects.
    for sql in ["SELECT * FROM secretz", "SELECT * FROM beta_db.main.secretz", "SELECT * FROM orderz"]:
        detail = outcome(client=joe, sql=sql)[1]
        assert "beta_db" not in detail and "secrets" not in detail, f"{sql}: {detail}"


def test_metadata_shows_only_the_tenants_catalogs(joe):
    for sql in ["SELECT database_name FROM duckdb_databases()", "SHOW DATABASES", "SELECT * FROM duckdb_tables()",
                "SELECT * FROM information_schema.tables", "SELECT * FROM information_schema.schemata",
                "SELECT * FROM duckdb_columns()", "SHOW ALL TABLES"]:
        text = str(expect_allowed(client=joe, sql=sql).to_pylist())
        assert "beta_db" not in text, f"{sql} listed another tenant's catalog"
        assert "secrets" not in text, f"{sql} listed another tenant's table"
        assert "shared_default" not in text, f"{sql} listed the shared default catalog"
    databases = expect_allowed(client=joe, sql="SELECT database_name FROM duckdb_databases() WHERE NOT internal")
    assert column(table=databases) == {"acme_db"}
    # The Warp SQL metadata calls too.
    catalogs = joe.metadata(kind="catalogs")
    assert "beta_db" not in str(catalogs.to_pylist()) and "acme_db" in str(catalogs.to_pylist())
    tables = joe.metadata(kind="tables", include_schema="true")
    assert "secrets" not in str(tables.column("table_name").to_pylist())
    assert "orders" in tables.column("table_name").to_pylist(), "its own tables are listed"
    assert joe.metadata(kind="tables", catalog="beta_db").num_rows == 0
    assert joe.metadata(kind="db-schemas", catalog="beta_db").num_rows == 0
    assert joe.metadata(kind="primary-keys", catalog="beta_db", table="secrets_keyed").num_rows == 0


def test_metadata_cannot_be_reached_another_way(admin, joe):
    # Joe's own views over the metadata functions list only what joe could list directly.
    for sql in ["CREATE OR REPLACE VIEW acme_db.main.all_views AS SELECT * FROM duckdb_views()",
                "CREATE OR REPLACE VIEW acme_db.main.all_tables AS SELECT * FROM duckdb_tables()",
                "CREATE OR REPLACE VIEW acme_db.main.all_info AS SELECT * FROM information_schema.tables"]:
        expect_allowed(client=joe, sql=sql)
    for sql in ["SELECT * FROM acme_db.main.all_views", "SELECT * FROM acme_db.main.all_tables",
                "SELECT * FROM acme_db.main.all_info", "WITH x AS (SELECT * FROM duckdb_tables()) SELECT * FROM x",
                "SELECT * FROM duckdb_tables ()", 'SELECT * FROM "duckdb_views"()', "SELECT * FROM duckdb_indexes()",
                "SELECT * FROM duckdb_constraints()", "SELECT * FROM duckdb_schemas()",
                "SELECT * FROM pragma_database_size()",
                "SELECT database_name, count(*) FROM duckdb_columns() GROUP BY ALL", "FROM duckdb_views() SELECT sql",
                "SHOW TABLES", "SHOW ALL TABLES", "SELECT * FROM information_schema.columns",
                "SELECT * FROM information_schema.views", "SELECT * FROM information_schema.table_constraints",
                "SELECT * FROM pg_catalog.pg_class", "SELECT * FROM pg_catalog.pg_namespace"]:
        try:
            text = str(joe.run(sql=sql).to_pylist())
        except WarpError as error:
            # Refusing is fine; it must not say why in a way that names beta's objects.
            assert "beta" not in error.detail, f"{sql}: {error}"
            continue
        for leak in ["beta_db", "secrets", "hush"]:
            assert leak not in text, f"{sql} listed another tenant's {leak}"
    # Positive twins: joe's own objects are there.
    assert "orders" in column(table=expect_allowed(client=joe, sql="SELECT table_name FROM acme_db.main.all_tables"))
    # The administrator still sees everything.
    assert "secrets_view" in column(table=expect_allowed(client=admin, sql="SELECT view_name FROM duckdb_views()"))

    # Naming the other tenant's objects inside a metadata call or a setting.
    missing = outcome(client=joe, sql="SELECT * FROM no_such_db.main.t")
    for sql in ["SELECT * FROM pragma_table_info('beta_db.main.secrets')", "PRAGMA table_info('beta_db.main.secrets')",
                "SELECT * FROM pragma_show('beta_db.main.secrets')", "DESCRIBE beta_db.main.secrets",
                "SUMMARIZE beta_db.main.secrets", "SET search_path = 'beta_db.main'", "SET schema = 'beta_db.main'",
                "SET search_path = 'acme_db.main,\"beta_db\".main'", "USE beta_db", "USE beta_db.main"]:
        assert outcome(client=joe, sql=sql) == missing, f"{sql} told joe more than a missing catalog would"
    # Positive twins.
    for sql in ["SELECT * FROM pragma_table_info('acme_db.main.orders')", "DESCRIBE acme_db.main.orders",
                "SET search_path = 'acme_db.main'", "USE acme_db"]:
        expect_allowed(client=joe, sql=sql)


@pytest.mark.parametrize(("who", "sql", "why"), [
    # a tenant admin manages only its own tenant's principals
    ("boss", "CREATE USER beta/sneak", "only administrators manage users and roles"),
    ("boss", "CREATE USER plain_name", "only administrators manage users and roles"),
    ("boss", "CREATE ROLE beta/r", "only administrators manage users and roles"),
    ("boss", "DROP USER beta/bea", "only administrators manage users and roles"),
    ("boss", "ALTER USER beta/bea DISABLE", "only administrators manage users and roles"),
    # its roles hold privileges only on its catalogs
    ("boss", "GRANT SELECT ON beta_db.main.secrets TO acme/readers", "only on that tenant's catalogs"),
    ("admin", "GRANT SELECT ON beta_db.main.secrets TO acme/readers", "only on that tenant's catalogs"),
    ("admin", "GRANT SELECT ON SCHEMA rbac_tenants.main TO acme/readers", "only on that tenant's catalogs"),
    # roles never cross
    ("boss", "GRANT beta/tenant_admin TO acme/joe", "may not grant or revoke role"),
    ("admin", "GRANT beta/tenant_admin TO acme/joe", "roles never cross tenants"),
    ("admin", "GRANT acme/readers TO beta/bea", "roles never cross tenants"),
    # no system privileges for a tenant
    ("admin", "GRANT READ FILESYSTEM TO acme/readers", "cannot hold them"),
    # tenants and catalogs are the administrator's
    ("boss", "ALTER CATALOG beta_db SET TENANT acme", "only administrators manage tenants"),
    ("boss", "CREATE TENANT gamma", "only administrators manage tenants"),
    ("admin", "ALTER CATALOG beta_db SET TENANT acme", "UNSET TENANT first"),
    ("admin", "CREATE USER nosuch/x", "tenant nosuch does not exist"),
    ("admin", "DROP TENANT acme", "still has users"),
])
def test_nothing_crosses_a_tenant(server, admin, boss, who, sql, why):
    client = admin if who == "admin" else boss
    code, detail = outcome(client=client, sql=sql)
    assert code != "ok", f"{sql} was allowed"
    assert why in detail, f"{sql} -> {detail}"


def test_a_view_over_another_tenants_catalog_does_not_let_a_tenant_read_it(admin, joe, bea):
    # A view in acme's catalog over beta's, even the administrator's.
    expect_allowed(client=admin, sql="CREATE OR REPLACE VIEW acme_db.main.peek AS SELECT * FROM beta_db.main.secrets")
    expect_allowed(client=admin, sql="GRANT SELECT ON acme_db.main.peek TO acme/readers")
    assert outcome(client=joe, sql="SELECT * FROM acme_db.main.peek") == \
        outcome(client=joe, sql="SELECT * FROM no_such_db.main.t")
    expect_allowed(client=admin, sql="DROP VIEW acme_db.main.peek")
    # bea, beta's admin, sees none of acme either.
    assert outcome(client=bea, sql="SELECT * FROM acme_db.main.orders") == \
        outcome(client=bea, sql="SELECT * FROM no_such_db.main.t")


def test_show_tenants(admin, joe):
    every = expect_allowed(client=admin, sql="SHOW TENANTS")
    assert column(table=every) == {"acme", "beta"}
    assert "acme_db" in str(every.to_pylist())
    own = expect_allowed(client=joe, sql="SHOW TENANTS")
    assert column(table=own) == {"acme"} and "beta" not in str(own.to_pylist())
    # DROP TENANT names every user still in it.
    assert "acme/boss, acme/joe" in outcome(client=admin, sql="DROP TENANT acme")[1]


def test_changing_a_tenants_catalogs_ends_its_sessions(server, admin):
    expect_allowed(client=admin, sql="ATTACH ':memory:' AS acme_extra")
    expect_allowed(client=admin, sql="CREATE TABLE acme_extra.main.extra AS SELECT 1 AS x")
    joe = server.user(name="acme/joe")
    expect_allowed(client=joe, sql="SELECT 1")
    expect_allowed(client=admin, sql="ALTER CATALOG acme_extra SET TENANT acme")
    code, detail = outcome(client=joe, sql="SELECT 1")
    assert code == "session_ended" and "tenant's catalogs changed" in detail, (code, detail)
    fresh = server.user(name="acme/joe")
    databases = expect_allowed(client=fresh, sql="SELECT database_name FROM duckdb_databases() WHERE NOT internal")
    assert column(table=databases) == {"acme_db", "acme_extra"}
    expect_allowed(client=admin, sql="ALTER CATALOG acme_extra UNSET TENANT")


def test_session_options_say_no_more_than_a_missing_catalog(server):
    def refusal(catalog: str):
        with pytest.raises(WarpError) as raised:
            server.client(token=mint(user="acme/joe"), options={"catalog": catalog})
        return raised.value.code, raised.value.detail

    missing = refusal(catalog="no_such_db")
    assert missing[0] == "invalid_option_value" and "Hardcharger blocked" in missing[1], missing
    assert refusal(catalog="beta_db") == missing
    assert refusal(catalog="rbac_tenants") == missing
    # Positive twin.
    server.client(token=mint(user="acme/joe"), options={"catalog": "acme_db"})


def test_a_users_bare_set_of_a_server_wide_setting_never_reaches_other_sessions(joe, admin, bea):
    # A bare SET (DuckDB's AUTOMATIC scope) of a setting that only exists server-wide
    # would change it for everyone; for a store user it must stay theirs or be refused.
    before = admin.run(sql="SELECT current_setting('http_proxy') AS v").column("v").to_pylist()
    outcome(client=joe, sql="SET http_proxy = 'evil.example:8080'")
    outcome(client=joe, sql="SET checkpoint_threshold = '1GB'")
    for other in (admin, bea):
        assert other.run(sql="SELECT current_setting('http_proxy') AS v").column("v").to_pylist() == before
    assert admin.run(sql="SELECT current_setting('checkpoint_threshold') AS v").column("v").to_pylist() != ["1.0 GiB"]


def test_a_tenants_user_may_create_temporary_tables_and_views(joe):
    expect_allowed(client=joe, sql="CREATE TEMP TABLE joe_scratch AS SELECT 1 AS x")
    expect_allowed(client=joe, sql="CREATE TEMP VIEW joe_scratch_view AS SELECT x FROM joe_scratch")
    assert column(table=expect_allowed(client=joe, sql="SELECT x FROM joe_scratch_view")) == {1}


def test_a_tenants_user_sees_every_data_type(joe):
    # type-info names no catalog, so the tenant filter must not apply to it.
    names = set(joe.metadata(kind="type-info").column("type_name").to_pylist())
    assert {"integer", "varchar", "boolean"} <= names
