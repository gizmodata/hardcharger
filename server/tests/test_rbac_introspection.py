"""Access-control introspection: hardcharger_grants(), hardcharger_role_members(),
hardcharger_roles(), hardcharger_users(), hardcharger_policies(),
hardcharger_effective_privileges() and the SQL-standard views in
_hardcharger_system.hardcharger_information_schema.

Who sees what:
- the administrator (no catalog_access rules): everything;
- a store user: only grants to itself and the roles it holds (directly, inherited, or through
  its token's groups), the memberships that give it those roles, those roles, its own user
  row, and policies on tables it can see; nothing about other users' grants, whatever it
  filters or counts;
- an administrator's token with catalog_access rules: grants, ownerships and policies on
  objects inside the catalogs its rules reach, the roles those name, no users or memberships;
- a server without an access-control store: every function and view is empty.

Every negative case has a positive twin that sees the row the other may not.
"""

import pytest

from rbac import WarpError, expect_allowed, expect_blocked, mint, start_rbac_server

SCHEMA = "_hardcharger_system.hardcharger_information_schema"
ISSUER = "https://idp.example"
GRANTS = ("grantee", "grantee_type", "privilege", "object_type", "catalog_name", "schema_name", "object_name",
          "column_name", "grantor", "with_grant_option", "tenant")


@pytest.fixture(scope="module")
def server(module_launcher, store):
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database())
    admin = server.admin()
    for sql in [
        "CREATE TABLE sales (id INT, amount INT, region VARCHAR)", "INSERT INTO sales VALUES (1, 10, 'EU')",
        "CREATE SCHEMA hr", "CREATE TABLE hr.salaries (emp VARCHAR, pay INT)",
        "CREATE VIEW hr.v_secret AS SELECT * FROM hr.salaries", "CREATE INDEX salaries_emp ON hr.salaries (emp)",
        "CREATE SCHEMA ops", "CREATE TABLE ops.logs (line VARCHAR)", "CREATE TABLE pub (v INT)",
        "ATTACH ':memory:' AS other", "CREATE TABLE other.main.t (id INT)",
        "CREATE ROLE analyst", "CREATE ROLE reader", "CREATE ROLE ops_role", "CREATE ROLE colrole",
        "CREATE ROLE hr_role", "CREATE ROLE other_reader", "CREATE ROLE grouped",
        "GRANT SELECT ON sales TO analyst", "GRANT analyst TO reader",
        "GRANT INSERT ON SCHEMA ops TO ops_role WITH GRANT OPTION", "GRANT CREATE TABLE ON SCHEMA ops TO ops_role",
        "GRANT SELECT (id, amount) ON sales TO colrole", "GRANT SELECT ON pub TO PUBLIC",
        "GRANT SELECT ON hr.salaries TO hr_role", "GRANT SELECT ON hr.v_secret TO hr_role",
        "GRANT READ FILESYSTEM TO hr_role", "GRANT SELECT ON other.main.t TO other_reader",
        "GRANT SELECT ON ops.logs TO grouped",
        f"CREATE GROUP MAPPING 'team-ops' ISSUER '{ISSUER}' TO ROLE grouped",
        "CREATE USER alice", "GRANT reader, ops_role, colrole TO alice",
        "CREATE USER bob WITH PASSWORD 'bob-Secret-1'", "GRANT hr_role, other_reader TO bob",
        "ALTER USER bob SET ATTRIBUTE region = 'US'",
        "CREATE USER carol",
        "CREATE ROW POLICY eu_only ON sales TO analyst USING (region = 'EU')",
        "CREATE ROW POLICY own_pay ON hr.salaries TO hr_role USING (emp = HARDCHARGER_USER())",
        "CREATE ROW POLICY other_rows ON other.main.t USING (id > 0)",
        "CREATE MASKING POLICY hide_region AS (val VARCHAR) RETURNS VARCHAR -> '***'",
        "ALTER TABLE sales MODIFY COLUMN region SET MASKING POLICY hide_region EXEMPT ROLE hr_role",
        "CREATE TENANT acme", "ATTACH ':memory:' AS acme_db", "ALTER CATALOG acme_db SET TENANT acme",
        "CREATE TABLE acme_db.main.t (id INT)", "CREATE USER acme/ann", "CREATE ROLE acme/r",
        "GRANT SELECT ON acme_db.main.t TO acme/r", "GRANT acme/r TO acme/ann",
        # acme's tenant admin, and a tenant-mate of ann's with a role of its own.
        "CREATE USER acme/boss", "GRANT acme/tenant_admin TO acme/boss",
        "CREATE TABLE acme_db.main.t2 (id INT)", "CREATE USER acme/joe", "CREATE ROLE acme/r2",
        "GRANT SELECT ON acme_db.main.t2 TO acme/r2", "GRANT acme/r2 TO acme/joe",
        "CREATE ROW POLICY acme_rows ON acme_db.main.t2 TO acme/r2 USING (id > 0)",
        # Another tenant, with its own admin.
        "CREATE TENANT beta", "ATTACH ':memory:' AS beta_db", "ALTER CATALOG beta_db SET TENANT beta",
        "CREATE TABLE beta_db.main.t (id INT)", "CREATE USER beta/bea", "GRANT beta/tenant_admin TO beta/bea",
        "CREATE USER beta/bo", "CREATE ROLE beta/r", "GRANT SELECT ON beta_db.main.t TO beta/r", "GRANT beta/r TO beta/bo",
        "CREATE ROW POLICY beta_rows ON beta_db.main.t USING (id > 0)",
    ]:
        expect_allowed(client=admin, sql=sql)
    # alice owns a table she creates.
    expect_allowed(client=server.user(name="alice"), sql="CREATE TABLE ops.alice_t (x INT)")
    server.admin_client = admin
    return server


@pytest.fixture
def admin(server):
    return server.admin_client


@pytest.fixture
def alice(server):
    return server.user(name="alice")


@pytest.fixture
def bob(server):
    return server.user(name="bob", password="bob-Secret-1")


def rows(client, sql: str) -> list[dict]:
    return expect_allowed(client=client, sql=sql).to_pylist()


def grantees(client, sql: str = "SELECT DISTINCT grantee FROM hardcharger_grants()") -> set:
    return {r["grantee"] for r in rows(client=client, sql=sql)}


def restricted_admin(server, rules):
    return server.client(token=mint(user="admin", role="admin", catalog_access=rules))


# ---- Columns ----

def test_each_function_has_its_columns(alice):
    expected = {
        "hardcharger_grants()": GRANTS,
        "hardcharger_role_members()": ("role", "member", "member_type", "admin_option", "grantor", "issuer", "tenant"),
        "hardcharger_roles()": ("name", "builtin", "tenant", "is_active"),
        "hardcharger_users()": ("name", "enabled", "has_password", "tenant", "attributes", "is_current"),
        "hardcharger_policies()": ("name", "kind", "catalog_name", "schema_name", "table_name", "column_name",
                                   "permissive", "roles", "exempt_roles", "expression", "applies_to_current_user"),
        "hardcharger_effective_privileges()": ("user_name", "privilege", "object_type", "catalog_name", "schema_name",
                                               "object_name", "column_name", "via", "with_grant_option"),
        f"{SCHEMA}.table_privileges": ("grantor", "grantee", "table_catalog", "table_schema", "table_name",
                                       "privilege_type", "is_grantable", "with_hierarchy"),
        f"{SCHEMA}.column_privileges": ("grantor", "grantee", "table_catalog", "table_schema", "table_name",
                                        "column_name", "privilege_type", "is_grantable"),
        f"{SCHEMA}.role_table_grants": ("grantor", "grantee", "table_catalog", "table_schema", "table_name",
                                        "privilege_type", "is_grantable", "with_hierarchy"),
        f"{SCHEMA}.applicable_roles": ("grantee", "role_name", "is_grantable"),
        f"{SCHEMA}.enabled_roles": ("role_name",),
    }
    for source, columns in expected.items():
        assert tuple(expect_allowed(client=alice, sql=f"SELECT * FROM {source} LIMIT 0").column_names) == columns, source


# ---- The administrator sees everything (the positive twin of every user rule) ----

def test_the_administrator_sees_every_grant_membership_role_user_and_policy(admin):
    assert {"analyst", "ops_role", "colrole", "PUBLIC", "hr_role", "other_reader", "grouped", "acme/r",
            "alice"} <= grantees(client=admin)
    assert {(r["role"], r["member"], r["member_type"]) for r in rows(client=admin, sql="SELECT * FROM hardcharger_role_members()")} >= {
        ("analyst", "reader", "ROLE"), ("reader", "alice", "USER"), ("hr_role", "bob", "USER"),
        ("other_reader", "bob", "USER"), ("acme/r", "acme/ann", "USER"), ("grouped", "team-ops", "GROUP")}
    assert {"PUBLIC", "analyst", "hr_role", "acme/r", "acme/PUBLIC"} <= {
        r["name"] for r in rows(client=admin, sql="SELECT name FROM hardcharger_roles()")}
    assert {"alice", "bob", "carol", "acme/ann"} <= {r["name"] for r in rows(client=admin, sql="SELECT name FROM hardcharger_users()")}
    assert {"eu_only", "own_pay", "other_rows", "hide_region"} <= {
        r["name"] for r in rows(client=admin, sql="SELECT name FROM hardcharger_policies()")}
    system = rows(client=admin, sql="SELECT * FROM hardcharger_grants() WHERE object_type = 'SYSTEM'")
    assert [(r["grantee"], r["privilege"]) for r in system] == [("hr_role", "READ FILESYSTEM")], system


def test_grant_rows_say_what_was_granted(admin):
    by_key = {(r["grantee"], r["privilege"], r["object_name"], r["column_name"]): r
              for r in rows(client=admin, sql="SELECT * FROM hardcharger_grants()")}
    insert = by_key[("ops_role", "INSERT", None, None)]
    assert (insert["object_type"], insert["catalog_name"], insert["schema_name"], insert["with_grant_option"],
            insert["grantor"], insert["grantee_type"]) == ("SCHEMA", "memory", "ops", True, "administrator", "ROLE")
    # A column-level grant: one row per column.
    assert {k[3] for k in by_key if k[:3] == ("colrole", "SELECT", "sales")} == {"id", "amount"}
    view = by_key[("hr_role", "SELECT", "v_secret", None)]
    assert view["object_type"] == "VIEW", view
    owned = by_key[("alice", "OWNERSHIP", "alice_t", None)]
    assert (owned["grantee_type"], owned["object_type"], owned["with_grant_option"]) == ("USER", "TABLE", True)
    assert by_key[("acme/r", "SELECT", "t", None)]["tenant"] == "acme"
    assert by_key[("analyst", "SELECT", "sales", None)]["tenant"] is None


# ---- A store user sees only its own ----

def test_a_user_sees_grants_to_the_roles_it_holds_inherited_ones_included(alice, bob):
    assert grantees(client=alice) == {"analyst", "ops_role", "colrole", "PUBLIC", "alice"}
    # reader holds analyst: alice sees analyst's grant through it.
    assert rows(client=alice, sql="SELECT privilege, object_name FROM hardcharger_grants() WHERE grantee = 'analyst'") == [
        {"privilege": "SELECT", "object_name": "sales"}]
    # Twin: bob's grants are bob's.
    assert grantees(client=bob) == {"hr_role", "other_reader", "PUBLIC"}


def test_a_user_sees_only_the_memberships_that_give_it_its_roles(alice, bob):
    assert {(r["role"], r["member"]) for r in rows(client=alice, sql="SELECT * FROM hardcharger_role_members()")} == {
        ("reader", "alice"), ("ops_role", "alice"), ("colrole", "alice"), ("analyst", "reader")}
    assert {(r["role"], r["member"]) for r in rows(client=bob, sql="SELECT * FROM hardcharger_role_members()")} == {
        ("hr_role", "bob"), ("other_reader", "bob")}


def test_a_user_sees_only_the_roles_it_holds_and_its_own_user_row(alice, bob):
    assert {r["name"] for r in rows(client=alice, sql="SELECT name FROM hardcharger_roles()")} == {
        "PUBLIC", "reader", "analyst", "ops_role", "colrole"}
    me = rows(client=bob, sql="SELECT * FROM hardcharger_users()")
    assert me == [{"name": "bob", "enabled": True, "has_password": True, "tenant": None,
                   "attributes": [("region", "US")], "is_current": True}], me
    assert [r["name"] for r in rows(client=alice, sql="SELECT name FROM hardcharger_users()")] == ["alice"]


def test_a_users_token_groups_show_their_mappings(server):
    in_group = server.user(name="carol", idp={"iss": ISSUER, "groups": ["team-ops"]})
    assert "grouped" in grantees(client=in_group)
    members = rows(client=in_group, sql="SELECT * FROM hardcharger_role_members()")
    assert [(r["role"], r["member"], r["member_type"], r["issuer"]) for r in members] == [
        ("grouped", "team-ops", "GROUP", ISSUER)], members
    # Twin: the same user without the group holds nothing from it, and sees no mapping.
    plain = server.user(name="carol")
    assert "grouped" not in grantees(client=plain)
    assert rows(client=plain, sql="SELECT * FROM hardcharger_role_members()") == []


def test_a_user_sees_policies_only_on_tables_it_can_see(alice, bob, admin):
    def policies(client):
        return {r["name"]: r for r in rows(client=client, sql="SELECT * FROM hardcharger_policies()")}
    mine = policies(client=alice)
    assert set(mine) == {"eu_only", "hide_region"}, mine
    assert mine["eu_only"]["applies_to_current_user"] is True and mine["eu_only"]["roles"] == ["analyst"]
    assert mine["hide_region"]["applies_to_current_user"] is True
    assert mine["hide_region"]["expression"] == "(val VARCHAR) RETURNS VARCHAR -> '***'"
    # Role lists show a user only roles it holds: the exemption is hr_role's.
    assert mine["hide_region"]["exempt_roles"] == []
    assert policies(client=admin)["hide_region"]["exempt_roles"] == ["hr_role"]
    # bob reads hr.salaries and other.main.t, not sales.
    assert set(policies(client=bob)) == {"own_pay", "other_rows"}
    assert policies(client=bob)["own_pay"]["applies_to_current_user"] is True


def test_a_user_cannot_infer_another_users_grants_by_filtering_or_counting(alice, admin):
    for sql in [
        "SELECT count(*) AS n FROM hardcharger_grants() WHERE grantee = '{role}'",
        "SELECT count(*) AS n FROM hardcharger_grants() WHERE object_name = 'salaries'",
        "SELECT count(*) AS n FROM hardcharger_role_members() WHERE member = '{user}' OR role = '{role}'",
        "SELECT count(*) AS n FROM hardcharger_roles() WHERE name = '{role}'",
        "SELECT count(*) AS n FROM hardcharger_users() WHERE name = '{user}'",
        "SELECT count(*) AS n FROM hardcharger_policies() WHERE table_name = 'salaries'",
        f"SELECT count(*) AS n FROM {SCHEMA}.table_privileges WHERE grantee = '{{role}}'",
        f"SELECT count(*) AS n FROM {SCHEMA}.column_privileges WHERE table_name = 'salaries'",
        f"SELECT count(*) AS n FROM {SCHEMA}.applicable_roles WHERE grantee = '{{user}}'",
        f"SELECT count(*) AS n FROM {SCHEMA}.enabled_roles WHERE role_name = '{{role}}'",
    ]:
        real = sql.format(role="hr_role", user="bob")
        made_up = sql.format(role="no_such_role", user="no_such_user")
        assert rows(client=alice, sql=real) == [{"n": 0}] == rows(client=alice, sql=made_up), real
        # Twin: the administrator counts the real one.
        assert rows(client=admin, sql=real)[0]["n"] > 0, real
    # The same refusal for another user who exists and one who doesn't.
    # (DuckDB quotes the statement after the message.)
    exists = expect_blocked(client=alice, sql="SELECT * FROM hardcharger_effective_privileges('bob')")
    missing = expect_blocked(client=alice, sql="SELECT * FROM hardcharger_effective_privileges('no_such_user')")
    assert exists.detail.splitlines()[0] == missing.detail.splitlines()[0], (exists, missing)
    # A bound parameter changes nothing.
    statement = alice.prepare(sql="SELECT count(*) AS n FROM hardcharger_grants() WHERE grantee = ?")
    assert alice.execute_prepared(prepared_id=statement, parameters=["hr_role"]).to_pylist() == [{"n": 0}]
    # Every row alice sees is to her roles or herself: what she counts is what she holds.
    total = rows(client=alice, sql="SELECT count(*) AS n FROM hardcharger_grants()")[0]["n"]
    expected = rows(client=admin, sql="SELECT count(*) AS n FROM hardcharger_grants() WHERE grantee IN "
                                      "('analyst', 'ops_role', 'colrole', 'PUBLIC', 'alice')")[0]["n"]
    assert total == expected, (total, expected)


# ---- Effective privileges ----

def test_effective_privileges_follow_inheritance_ownership_and_set_role(server, admin):
    alice = server.user(name="alice")

    def effective(client, sql="SELECT * FROM hardcharger_effective_privileges()"):
        return {(r["privilege"], r["object_type"], r["schema_name"], r["object_name"], r["column_name"], r["via"],
                 r["with_grant_option"]) for r in rows(client=client, sql=sql)}
    mine = effective(client=alice)
    assert ("SELECT", "TABLE", "main", "sales", None, "analyst", False) in mine  # inherited through reader
    assert ("INSERT", "SCHEMA", "ops", None, None, "ops_role", True) in mine
    assert ("SELECT", "TABLE", "main", "sales", "id", "colrole", False) in mine
    assert ("SELECT", "TABLE", "main", "pub", None, "PUBLIC", False) in mine
    assert ("OWNERSHIP", "TABLE", "ops", "alice_t", None, "OWNER", True) in mine
    assert ("DELETE", "TABLE", "ops", "alice_t", None, "OWNER", True) in mine
    assert not any(row[3] == "salaries" for row in mine)
    # The administrator asks for her: the same rows.
    assert effective(client=admin, sql="SELECT * FROM hardcharger_effective_privileges(user_name := 'alice')") == mine
    with pytest.raises(WarpError) as raised:
        admin.run(sql="SELECT * FROM hardcharger_effective_privileges('no_such_user')")
    assert "user no_such_user does not exist" in raised.value.detail, raised.value
    # SET ROLE narrows what she holds now, and enabled_roles; hardcharger_grants still shows
    # every role she holds.
    expect_allowed(client=alice, sql="SET ROLE ops_role")
    narrowed = effective(client=alice)
    # (What she owns herself she owns whatever role she acts with.)
    assert {row[5] for row in narrowed} == {"ops_role", "PUBLIC", "OWNER"}, narrowed
    assert {r["role_name"] for r in rows(client=alice, sql=f"SELECT * FROM {SCHEMA}.enabled_roles")} == {
        "PUBLIC", "ops_role"}
    assert "analyst" in grantees(client=alice)
    expect_allowed(client=alice, sql="RESET ROLE")
    assert effective(client=alice) == mine


def test_effective_privileges_list_system_privileges(bob, alice):
    system = rows(client=bob, sql="SELECT privilege, via FROM hardcharger_effective_privileges() "
                                  "WHERE object_type = 'SYSTEM'")
    assert system == [{"privilege": "READ FILESYSTEM", "via": "hr_role"}], system
    assert rows(client=alice, sql="SELECT * FROM hardcharger_effective_privileges() WHERE object_type = 'SYSTEM'") == []


def test_the_administrator_holds_everything(admin, server):
    assert rows(client=admin, sql="SELECT privilege, object_type, via FROM hardcharger_effective_privileges()") == [
        {"privilege": "ALL", "object_type": "SERVER", "via": "SUPERUSER"}]


# ---- A restricted administrator ----

def test_a_restricted_administrator_sees_only_grants_inside_its_catalogs(server):
    session = restricted_admin(server=server, rules=[{"catalog": "other", "access": "read"},
                                                    {"catalog": "*", "access": "none"}])
    grants = rows(client=session, sql="SELECT grantee, catalog_name, object_name FROM hardcharger_grants()")
    assert grants == [{"grantee": "other_reader", "catalog_name": "other", "object_name": "t"}], grants
    assert rows(client=session, sql="SELECT * FROM hardcharger_role_members()") == []
    assert rows(client=session, sql="SELECT * FROM hardcharger_users()") == []
    assert [r["name"] for r in rows(client=session, sql="SELECT name FROM hardcharger_roles()")] == ["other_reader"]
    assert [r["name"] for r in rows(client=session, sql="SELECT name FROM hardcharger_policies()")] == ["other_rows"]
    assert {(r["privilege"], r["catalog_name"]) for r in rows(
        client=session, sql="SELECT * FROM hardcharger_effective_privileges() WHERE object_type = 'CATALOG'")} == {
        ("SELECT", "other"), ("NONE", "*")}
    expect_blocked(client=session, sql="SELECT * FROM hardcharger_effective_privileges('bob')",
                   why="needs an administrator without catalog_access rules")
    assert [r["table_name"] for r in rows(client=session, sql=f"SELECT * FROM {SCHEMA}.table_privileges")] == ["t"]
    # Twin: rules that reach memory show memory's grants (and still not acme_db's).
    wider = restricted_admin(server=server, rules=[{"catalog": "memory", "access": "write"},
                                                  {"catalog": "*", "access": "none"}])
    seen = rows(client=wider, sql="SELECT DISTINCT catalog_name FROM hardcharger_grants()")
    assert seen == [{"catalog_name": "memory"}], seen
    assert "hr_role" in grantees(client=wider)


# ---- Tenants ----

def test_a_tenants_user_sees_its_tenants_grants_with_the_tenant(server):
    ann = server.user(name="acme/ann")
    grants = rows(client=ann, sql="SELECT grantee, catalog_name, tenant FROM hardcharger_grants()")
    assert grants == [{"grantee": "acme/r", "catalog_name": "acme_db", "tenant": "acme"}], grants
    assert rows(client=ann, sql="SELECT name, tenant FROM hardcharger_users()") == [{"name": "acme/ann", "tenant": "acme"}]
    assert [r["table_catalog"] for r in rows(client=ann, sql=f"SELECT * FROM {SCHEMA}.table_privileges")] == ["acme_db"]
    # The system catalog's views list nothing outside her tenant (twin: the administrator
    # sees memory's view there, in the system catalog test below).
    assert rows(client=ann, sql="SELECT DISTINCT \"TABLE_CAT\" FROM _hardcharger_system.main.hardcharger_view_definition "
                                "WHERE \"TABLE_CAT\" NOT IN ('system', 'temp', '_hardcharger_system')") == []
    assert rows(client=ann, sql="SELECT count(*) AS n FROM hardcharger_policies()") == [{"n": 0}]


# ---- The SQL-standard views match the table functions ----

TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")


def expected_table_privileges(client) -> set:
    objects = rows(client=client, sql="SELECT database_name AS c, schema_name AS s, table_name AS t FROM duckdb_tables() "
                                      "WHERE NOT internal UNION ALL SELECT database_name, schema_name, view_name "
                                      "FROM duckdb_views() WHERE NOT internal")
    out = set()
    for g in rows(client=client, sql="SELECT * FROM hardcharger_grants()"):
        if g["column_name"] is not None or g["object_type"] not in ("CATALOG", "SCHEMA", "TABLE", "VIEW"):
            continue
        privileges = TABLE_PRIVILEGES if g["privilege"] == "ALL" else (g["privilege"],)
        for o in objects:
            same_catalog = o["c"].lower() == g["catalog_name"].lower()
            covered = same_catalog and (
                g["object_type"] == "CATALOG"
                or (g["object_type"] == "SCHEMA" and o["s"].lower() == g["schema_name"].lower())
                or (o["s"].lower() == (g["schema_name"] or "").lower() and o["t"].lower() == (g["object_name"] or "").lower()))
            for p in privileges:
                if covered and p in TABLE_PRIVILEGES:
                    out.add((g["grantor"], g["grantee"], o["c"], o["s"], o["t"], p,
                             "YES" if g["with_grant_option"] else "NO"))
    return out


@pytest.mark.parametrize("who", ["admin", "alice", "bob"])
def test_the_standard_views_match_the_table_functions(server, who):
    client = server.admin_client if who == "admin" else (
        server.user(name="alice") if who == "alice" else server.user(name="bob", password="bob-Secret-1"))
    table_privileges = {(r["grantor"], r["grantee"], r["table_catalog"], r["table_schema"], r["table_name"],
                         r["privilege_type"], r["is_grantable"])
                        for r in rows(client=client, sql=f"SELECT * FROM {SCHEMA}.table_privileges")}
    assert table_privileges == expected_table_privileges(client=client)
    assert table_privileges, "no table privileges at all"
    # Column privileges: each table privilege on every column of the table, and each
    # column-level grant.
    columns = rows(client=client, sql="SELECT database_name AS c, schema_name AS s, table_name AS t, column_name AS col "
                                      "FROM duckdb_columns() WHERE NOT internal")
    expected_columns = {(grantor, grantee, c["c"], c["s"], c["t"], c["col"], p, grantable)
                        for (grantor, grantee, cat, sch, tab, p, grantable) in table_privileges
                        for c in columns
                        if (c["c"], c["s"], c["t"]) == (cat, sch, tab) and p in ("SELECT", "INSERT", "UPDATE")}
    for g in rows(client=client, sql="SELECT * FROM hardcharger_grants() WHERE column_name IS NOT NULL"):
        expected_columns |= {(g["grantor"], g["grantee"], c["c"], c["s"], c["t"], c["col"], g["privilege"],
                              "YES" if g["with_grant_option"] else "NO")
                             for c in columns if (c["c"].lower(), c["s"].lower(), c["t"].lower(), c["col"].lower()) ==
                             (g["catalog_name"].lower(), g["schema_name"].lower(), g["object_name"].lower(),
                              g["column_name"].lower())}
    column_privileges = {tuple(r.values()) for r in rows(client=client, sql=f"SELECT * FROM {SCHEMA}.column_privileges")}
    assert column_privileges == expected_columns
    enabled = {r["name"] for r in rows(client=client, sql="SELECT name FROM hardcharger_roles() WHERE is_active")}
    assert {r["role_name"] for r in rows(client=client, sql=f"SELECT * FROM {SCHEMA}.enabled_roles")} == enabled
    role_table_grants = {(r["grantor"], r["grantee"], r["table_catalog"], r["table_schema"], r["table_name"],
                          r["privilege_type"], r["is_grantable"])
                         for r in rows(client=client, sql=f"SELECT * FROM {SCHEMA}.role_table_grants")}
    assert role_table_grants == {row for row in table_privileges if row[1] in enabled}
    applicable = {tuple(r.values()) for r in rows(client=client, sql=f"SELECT * FROM {SCHEMA}.applicable_roles")}
    assert applicable == {(m["member"], m["role"], "YES" if m["admin_option"] else "NO")
                          for m in rows(client=client, sql="SELECT * FROM hardcharger_role_members() "
                                                           "WHERE member_type IN ('USER', 'ROLE')")}


# ---- The system catalog: readable, but only what the reader may see ----

def test_a_store_user_reads_the_system_catalogs_views_with_its_own_rights(server, admin):
    carol = server.user(name="carol")
    # Positive twin first: the administrator sees the hidden view and index.
    assert rows(client=admin, sql="SELECT count(*) AS n FROM _hardcharger_system.main.hardcharger_view_definition "
                                  "WHERE \"TABLE_NAME\" = 'v_secret'") == [{"n": 1}]
    assert rows(client=admin, sql="SELECT count(*) AS n FROM _hardcharger_system.main.hardcharger_index_info "
                                  "WHERE \"TABLE_NAME\" = 'salaries'") == [{"n": 1}]
    assert rows(client=carol, sql="SELECT count(*) AS n FROM _hardcharger_system.main.hardcharger_view_definition "
                                  "WHERE \"TABLE_NAME\" = 'v_secret'") == [{"n": 0}]
    assert rows(client=carol, sql="SELECT count(*) AS n FROM _hardcharger_system.main.hardcharger_index_info "
                                  "WHERE \"TABLE_NAME\" = 'salaries'") == [{"n": 0}]
    # bob, who may read hr.salaries and the view, sees them.
    bob = server.user(name="bob", password="bob-Secret-1")
    assert rows(client=bob, sql="SELECT count(*) AS n FROM _hardcharger_system.main.hardcharger_view_definition "
                                "WHERE \"TABLE_NAME\" = 'v_secret'") == [{"n": 1}]
    # carol, with no role of her own, holds only PUBLIC's grant.
    assert grantees(client=carol) == {"PUBLIC"}
    # Still read-only for everyone.
    expect_blocked(client=carol, sql=f"CREATE VIEW {SCHEMA}.evil AS SELECT 1", why="")


def test_a_prepared_statement_sees_grants_as_they_are_when_it_runs(server, admin):
    carol = server.user(name="carol")
    statement = carol.prepare(sql="SELECT count(*) AS n FROM hardcharger_grants() WHERE grantee = 'analyst'")
    assert carol.execute_prepared(prepared_id=statement).to_pylist() == [{"n": 0}]
    expect_allowed(client=admin, sql="GRANT analyst TO carol")
    try:
        assert carol.execute_prepared(prepared_id=statement).to_pylist() == [{"n": 1}]
    finally:
        expect_allowed(client=admin, sql="REVOKE analyst FROM carol")


def test_no_secret_reaches_any_row(admin, bob):
    for client in (admin, bob):
        for source in ["hardcharger_users()", "hardcharger_role_members()", "hardcharger_grants()"]:
            for row in rows(client=client, sql=f"SELECT * FROM {source}"):
                assert "pbkdf2" not in repr(row) and "bob-Secret-1" not in repr(row), row


# ---- Without access control ----

def test_a_server_without_access_control_returns_no_rows(module_launcher):
    running = module_launcher.start()
    session = running.admin()
    for source in ["hardcharger_grants()", "hardcharger_role_members()", "hardcharger_roles()", "hardcharger_users()",
                   "hardcharger_policies()", "hardcharger_effective_privileges()",
                   "hardcharger_effective_privileges('anyone')", f"{SCHEMA}.table_privileges",
                   f"{SCHEMA}.column_privileges", f"{SCHEMA}.role_table_grants", f"{SCHEMA}.applicable_roles",
                   f"{SCHEMA}.enabled_roles"]:
        assert session.query(sql=f"SELECT count(*) AS n FROM {source}").to_pylist() == [{"n": 0}], source


# ---- A tenant admin sees its own tenant, never another ----

def test_a_tenant_admin_sees_everything_of_its_tenant(server):
    boss = server.user(name="acme/boss")
    assert grantees(client=boss) >= {"acme/r", "acme/r2"}
    assert {(r["role"], r["member"]) for r in rows(client=boss, sql="SELECT * FROM hardcharger_role_members()")} == {
        ("acme/r", "acme/ann"), ("acme/r2", "acme/joe"), ("acme/tenant_admin", "acme/boss")}
    assert {r["name"] for r in rows(client=boss, sql="SELECT name FROM hardcharger_users()")} == {
        "acme/ann", "acme/boss", "acme/joe"}
    assert {r["name"] for r in rows(client=boss, sql="SELECT name FROM hardcharger_roles()")} == {
        "acme/PUBLIC", "acme/tenant_admin", "acme/r", "acme/r2"}
    assert [r["name"] for r in rows(client=boss, sql="SELECT name FROM hardcharger_policies()")] == ["acme_rows"]
    joe = rows(client=boss, sql="SELECT privilege, object_name, via FROM hardcharger_effective_privileges('acme/joe') "
                                "WHERE object_name IS NOT NULL")
    assert joe == [{"privilege": "SELECT", "object_name": "t2", "via": "acme/r2"}], joe
    assert {r["table_name"] for r in rows(client=boss, sql=f"SELECT * FROM {SCHEMA}.table_privileges")} == {"t", "t2"}
    # Twin: ann, a plain user of the same tenant, sees none of joe's.
    ann = server.user(name="acme/ann")
    assert "acme/r2" not in grantees(client=ann)
    assert [r["name"] for r in rows(client=ann, sql="SELECT name FROM hardcharger_users()")] == ["acme/ann"]
    expect_blocked(client=ann, sql="SELECT * FROM hardcharger_effective_privileges('acme/joe')",
                   why="for another user")


def test_a_tenant_admin_sees_nothing_of_another_tenant_or_the_system_scope(server, admin):
    boss = server.user(name="acme/boss")
    everything = " ".join(str(rows(client=boss, sql=f"SELECT * FROM {source}")) for source in [
        "hardcharger_grants()", "hardcharger_role_members()", "hardcharger_roles()", "hardcharger_users()",
        "hardcharger_policies()", f"{SCHEMA}.table_privileges", f"{SCHEMA}.column_privileges",
        f"{SCHEMA}.applicable_roles"])
    for name in ["beta", "bea", "beta_db", "analyst", "hr_role", "alice", "'bob'", "sales", "salaries"]:
        assert name not in everything, f"acme's tenant admin saw {name}"
    # The same counts (zero) for another tenant's real names and made-up ones, and the
    # same refusal for another tenant's user, a system-scope user and a user that doesn't exist.
    for sql in ["SELECT count(*) AS n FROM hardcharger_grants() WHERE grantee = '{role}'",
                "SELECT count(*) AS n FROM hardcharger_role_members() WHERE member = '{user}' OR role = '{role}'",
                "SELECT count(*) AS n FROM hardcharger_roles() WHERE name = '{role}'",
                "SELECT count(*) AS n FROM hardcharger_users() WHERE name = '{user}'",
                "SELECT count(*) AS n FROM hardcharger_policies() WHERE catalog_name = '{catalog}'"]:
        real = sql.format(role="beta/r", user="beta/bo", catalog="beta_db")
        made_up = sql.format(role="beta/nope", user="beta/nobody", catalog="nope_db")
        assert rows(client=boss, sql=real) == [{"n": 0}] == rows(client=boss, sql=made_up), real
        assert rows(client=admin, sql=real)[0]["n"] > 0, real
    refusals = {expect_blocked(client=boss, sql=f"SELECT * FROM hardcharger_effective_privileges('{user}')",
                               why="for another user").detail.splitlines()[0]
                for user in ["beta/bo", "beta/bea", "bob", "beta/nobody"]}
    assert len(refusals) == 1, refusals
    # Twin: beta's admin sees beta's, and none of acme's.
    bea = server.user(name="beta/bea")
    assert "beta/r" in grantees(client=bea) and not {"acme/r", "acme/r2"} & grantees(client=bea)
    assert {r["name"] for r in rows(client=bea, sql="SELECT name FROM hardcharger_users()")} == {"beta/bea", "beta/bo"}
