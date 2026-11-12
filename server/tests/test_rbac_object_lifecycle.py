"""DDL keeps grants, ownership and policy bindings attached to object lifetimes."""

import pytest

from rbac import ChargewireError, expect_allowed, expect_blocked, start_rbac_server


@pytest.fixture(scope="module")
def server(module_launcher, store):
    return start_rbac_server(launcher=module_launcher, store_uri=store.new_database())


def setup(server, name):
    admin = server.admin()
    for sql in [f"CREATE SCHEMA {name}", f"CREATE ROLE {name}_r", f"CREATE USER {name}_u",
                f"GRANT ALL PRIVILEGES ON SCHEMA {name} TO {name}_r", f"GRANT {name}_r TO {name}_u"]:
        expect_allowed(client=admin, sql=sql)
    return admin, server.user(name=f"{name}_u")


def grants(admin, schema):
    return admin.run(sql=f"SELECT * FROM hardcharger_grants() WHERE schema_name = '{schema}'").to_pylist()


@pytest.mark.parametrize("actor", ["admin", "owner"])
def test_cascade_drop_removes_all_access_before_name_reuse(server, actor):
    name = f"cascade_{actor}"
    admin, owner = setup(server=server, name=name)
    owner.run(sql=f"CREATE TABLE {name}.t AS SELECT 1 AS x")
    owner.run(sql=f"CREATE VIEW {name}.v AS SELECT * FROM {name}.t")
    admin.run(sql=f"GRANT SELECT ON {name}.t TO {name}_r")
    (admin if actor == "admin" else owner).run(sql=f"DROP SCHEMA {name} CASCADE")
    assert grants(admin=admin, schema=name) == []
    admin.run(sql=f"CREATE SCHEMA {name}")
    admin.run(sql=f"CREATE TABLE {name}.t AS SELECT 'new data' AS x")
    for sql in [f"SELECT * FROM {name}.t", f"INSERT INTO {name}.t VALUES ('x')", f"DROP TABLE {name}.t"]:
        expect_blocked(client=owner, sql=sql, why="Hardcharger blocked")
    admin.run(sql=f"DROP USER {name}_u")


@pytest.mark.parametrize("actor", ["admin", "owner"])
@pytest.mark.parametrize("kind", ["TABLE", "VIEW"])
def test_rename_preserves_owner_and_object_grants(server, actor, kind):
    name = f"rename_{actor}_{kind.lower()}"
    admin, owner = setup(server=server, name=name)
    admin.run(sql=f"CREATE ROLE {name}_reader")
    admin.run(sql=f"CREATE USER {name}_reader_u")
    admin.run(sql=f"GRANT {name}_reader TO {name}_reader_u")
    owner.run(sql=f"CREATE {kind} {name}.old AS SELECT 1 AS x")
    admin.run(sql=f"GRANT SELECT ON {name}.old TO {name}_reader")
    (admin if actor == "admin" else owner).run(sql=f"ALTER {kind} {name}.old RENAME TO new_name")
    records = grants(admin=admin, schema=name)
    assert not any(r['object_name'] == 'old' for r in records)
    assert any(r['object_name'] == 'new_name' and r['privilege'] == 'OWNERSHIP' and
               r['grantee'] == f'{name}_u' for r in records)
    reader = server.user(name=f"{name}_reader_u")
    assert reader.run(sql=f"SELECT * FROM {name}.new_name").to_pylist() == [{"x": 1}]
    # The old name is a different object; the old grant cannot reappear there.
    admin.run(sql=f"CREATE {kind} {name}.old AS SELECT 2 AS x")
    expect_blocked(client=reader, sql=f"SELECT * FROM {name}.old", why="Hardcharger blocked")
    admin.run(sql=f"DROP {kind} {name}.new_name")
    admin.run(sql=f"DROP USER {name}_u")


def test_failed_ddl_and_if_not_exists_leave_access_intact(server):
    admin, owner = setup(server=server, name="failed_ddl")
    owner.run(sql="CREATE TABLE failed_ddl.t AS SELECT 1 AS x")
    owner.run(sql="CREATE TABLE failed_ddl.other AS SELECT 2 AS x")
    before = grants(admin=admin, schema="failed_ddl")
    with pytest.raises(ChargewireError):
        admin.run(sql="DROP SCHEMA failed_ddl")
    with pytest.raises(ChargewireError):
        owner.run(sql="ALTER TABLE failed_ddl.t RENAME TO other")
    admin.run(sql="CREATE TABLE IF NOT EXISTS failed_ddl.t (x INT)")
    admin.run(sql="CREATE SCHEMA IF NOT EXISTS failed_ddl")
    assert grants(admin=admin, schema="failed_ddl") == before


def test_nested_drop_does_not_match_a_quoted_or_prefix_sibling(server):
    admin, owner = setup(server=server, name="nested_lifetime")
    for sql in ["CREATE SCHEMA nested_lifetime.child", 'CREATE SCHEMA "nested_lifetime.child"',
                "CREATE SCHEMA nested_lifetime_extra", "CREATE TABLE nested_lifetime.child.t (x INT)",
                'GRANT SELECT ON SCHEMA "nested_lifetime.child" TO nested_lifetime_r',
                "GRANT SELECT ON SCHEMA nested_lifetime_extra TO nested_lifetime_r"]:
        admin.run(sql=sql)
    admin.run(sql="DROP SCHEMA nested_lifetime CASCADE")
    assert grants(admin=admin, schema="nested_lifetime") == []
    assert grants(admin=admin, schema='"nested_lifetime.child"')
    assert grants(admin=admin, schema="nested_lifetime_extra")


def test_prepared_rename_and_drop(server):
    admin, owner = setup(server=server, name="prepared_lifetime")
    owner.run(sql="CREATE TABLE prepared_lifetime.old (x INT)")
    rename = owner.prepare(sql="ALTER TABLE prepared_lifetime.old RENAME TO new_name")
    owner.execute_prepared(prepared_id=rename)
    assert any(r['object_name'] == 'new_name' and r['privilege'] == 'OWNERSHIP'
               for r in grants(admin=admin, schema="prepared_lifetime"))
    drop = admin.prepare(sql="DROP SCHEMA prepared_lifetime CASCADE")
    admin.execute_prepared(prepared_id=drop)
    assert grants(admin=admin, schema="prepared_lifetime") == []
    admin.run(sql="DROP USER prepared_lifetime_u")


def test_catalog_spelling_hints_are_not_exposed_to_store_users(server):
    admin, owner = setup(server=server, name="hint_visible")
    admin.run(sql="CREATE SCHEMA hint_hidden")
    admin.run(sql="CREATE TABLE hint_hidden.confidential_measurements (x INT)")
    owner.run(sql="SET schema = 'hint_visible'")
    for sql in ["SELECT * FROM hint_hidden.confidential_measurement",
                "SELECT * FROM confidential_measurement"]:
        for prepare in [False, True]:
            with pytest.raises(ChargewireError) as raised:
                if prepare:
                    owner.prepare(sql=sql)
                else:
                    owner.run(sql=sql)
            assert 'confidential_measurements' not in raised.value.detail
            assert 'Did you mean' not in raised.value.detail
            assert 'does not exist' in raised.value.detail
    # Admin diagnostics remain useful.
    with pytest.raises(ChargewireError) as raised:
        admin.run(sql="SELECT * FROM hint_hidden.confidential_measurement")
    assert 'confidential_measurements' in raised.value.detail


def test_drop_and_rename_rollback_preserve_existing_access(server):
    admin, owner = setup(server=server, name="rollback_lifetime")
    owner.run(sql="CREATE TABLE rollback_lifetime.t AS SELECT 1 AS x")
    admin.run(sql="GRANT SELECT ON rollback_lifetime.t TO rollback_lifetime_r")
    before = grants(admin=admin, schema="rollback_lifetime")
    admin.run(sql="BEGIN")
    admin.run(sql="DROP SCHEMA rollback_lifetime CASCADE")
    admin.run(sql="ROLLBACK")
    assert grants(admin=admin, schema="rollback_lifetime") == before
    owner.run(sql="BEGIN")
    owner.run(sql="ALTER TABLE rollback_lifetime.t RENAME TO moved")
    owner.run(sql="SELECT * FROM rollback_lifetime.moved")
    owner.run(sql="ROLLBACK")
    assert grants(admin=admin, schema="rollback_lifetime") == before
    admin.run(sql="BEGIN")
    admin.run(sql="DROP SCHEMA rollback_lifetime CASCADE")
    admin.run(sql="COMMIT")
    assert grants(admin=admin, schema="rollback_lifetime") == []


def test_legacy_stale_owners_do_not_prevent_drop_user(launcher, pg_store):
    import psycopg

    server = start_rbac_server(launcher=launcher, store_uri=pg_store.new_database())
    admin = server.admin()
    admin.run(sql="CREATE USER legacy_owner")
    catalog = admin.run(sql="SELECT current_database() AS c").to_pylist()[0]['c']
    with psycopg.connect(conninfo=server.store_uri, autocommit=True) as connection:
        user_id = connection.execute(query="SELECT user_id FROM hardcharger_rbac.users WHERE name = 'legacy_owner'").fetchone()[0]
        for schema, table, kind in [('main', 'gone', 'table'), ('gone_schema', '', 'schema')]:
            import json
            connection.execute(query="INSERT INTO hardcharger_rbac.owners VALUES (%s,%s,%s,%s,%s,'user',%s,'')",
                               params=(f'{catalog}\x1f{schema}\x1e{table}', catalog, json.dumps(obj=[schema]), table, kind, user_id))
        connection.execute(query="UPDATE hardcharger_rbac.meta SET value = (value::bigint + 1)::varchar WHERE key = 'data_version'")
    admin.run(sql="DROP USER legacy_owner")
    assert not admin.run(sql="SELECT * FROM hardcharger_grants() WHERE grantee = 'legacy_owner'").to_pylist()


def test_transaction_create_then_drop_does_not_leave_owner(server):
    admin, owner = setup(server=server, name="transient_lifetime")
    owner.run(sql="BEGIN")
    owner.run(sql="CREATE TABLE transient_lifetime.t (x INT)")
    owner.run(sql="DROP TABLE transient_lifetime.t")
    owner.run(sql="COMMIT")
    assert not any(r['privilege'] == 'OWNERSHIP' and r['object_name'] == 't'
                   for r in grants(admin=admin, schema="transient_lifetime"))
    admin.run(sql="DROP USER transient_lifetime_u")


def test_rename_keeps_row_and_masking_policies(server):
    admin, owner = setup(server=server, name="policy_lifetime")
    owner.run(sql="CREATE TABLE policy_lifetime.t AS SELECT 1 AS x, 10 AS pay UNION ALL SELECT 2, 20")
    admin.run(sql="CREATE ROW POLICY one_row ON policy_lifetime.t TO policy_lifetime_r USING (x = 1)")
    admin.run(sql="CREATE MASKING POLICY lifetime_mask AS (val INT) RETURNS INT -> 0")
    admin.run(sql="ALTER TABLE policy_lifetime.t MODIFY COLUMN pay SET MASKING POLICY lifetime_mask")
    admin.run(sql="ALTER TABLE policy_lifetime.t RENAME TO moved")
    assert owner.run(sql="SELECT * FROM policy_lifetime.moved").to_pylist() == [{'x': 1, 'pay': 0}]
    admin.run(sql="DROP SCHEMA policy_lifetime CASCADE")
    admin.run(sql="DROP MASKING POLICY lifetime_mask")


def test_uncommitted_rename_keeps_source_and_destination_policies(server):
    admin, owner = setup(server=server, name="rename_policy_tx")
    owner.run(sql="CREATE TABLE rename_policy_tx.t AS SELECT 1 AS x UNION ALL SELECT 2")
    admin.run(sql="CREATE ROW POLICY tx_one ON rename_policy_tx.t TO rename_policy_tx_r USING (x = 1)")
    reader = server.user(name="rename_policy_tx_u")
    owner.run(sql="BEGIN")
    owner.run(sql="ALTER TABLE rename_policy_tx.t RENAME TO moved")
    assert owner.run(sql="SELECT * FROM rename_policy_tx.moved").to_pylist() == [{'x': 1}]
    assert reader.run(sql="SELECT * FROM rename_policy_tx.t").to_pylist() == [{'x': 1}]
    owner.run(sql="ROLLBACK")
    assert reader.run(sql="SELECT * FROM rename_policy_tx.t").to_pylist() == [{'x': 1}]
    assert not any(r['object_name'] == 'moved' for r in grants(admin=admin, schema="rename_policy_tx"))


def test_prepared_noop_and_failed_rename_do_not_replace_existing_access(server):
    admin, owner = setup(server=server, name="prepared_occupied")
    create = admin.prepare(sql="CREATE TABLE IF NOT EXISTS prepared_occupied.t (x INT)")
    owner.run(sql="CREATE TABLE prepared_occupied.t (x INT)")
    owner.run(sql="CREATE TABLE prepared_occupied.source (x INT)")
    rename = admin.prepare(sql="ALTER TABLE prepared_occupied.source RENAME TO occupied")
    owner.run(sql="CREATE TABLE prepared_occupied.occupied (x INT)")
    before = grants(admin=admin, schema="prepared_occupied")
    admin.execute_prepared(prepared_id=create)
    with pytest.raises(ChargewireError):
        admin.execute_prepared(prepared_id=rename)
    assert grants(admin=admin, schema="prepared_occupied") == before


def test_table_and_sequence_with_same_name_keep_distinct_grants(server):
    admin, owner = setup(server=server, name="object_namespaces")
    owner.run(sql="CREATE TABLE object_namespaces.t AS SELECT 1 AS x")
    admin.run(sql="CREATE SEQUENCE object_namespaces.t")
    admin.run(sql="GRANT USAGE ON SEQUENCE object_namespaces.t TO object_namespaces_r")
    admin.run(sql="DROP SEQUENCE object_namespaces.t")
    assert any(r['object_name'] == 't' and r['privilege'] == 'OWNERSHIP'
               for r in grants(admin=admin, schema="object_namespaces"))
    assert owner.run(sql="SELECT * FROM object_namespaces.t").to_pylist() == [{'x': 1}]
    admin.run(sql="CREATE SEQUENCE object_namespaces.t")
    admin.run(sql="GRANT USAGE ON SEQUENCE object_namespaces.t TO object_namespaces_r")
    owner.run(sql="ALTER TABLE object_namespaces.t RENAME TO moved")
    assert any(r['object_name'] == 't' and r['privilege'] == 'USAGE'
               for r in grants(admin=admin, schema="object_namespaces"))
    assert not any(r['object_name'] == 'moved' and r['privilege'] == 'USAGE'
                   for r in grants(admin=admin, schema="object_namespaces"))


def test_rollback_of_drop_and_recreate_restores_original_grants(server):
    admin, owner = setup(server=server, name="recreate_rollback")
    owner.run(sql="CREATE TABLE recreate_rollback.t AS SELECT 1 AS x")
    admin.run(sql="GRANT SELECT ON recreate_rollback.t TO recreate_rollback_r")
    before = grants(admin=admin, schema="recreate_rollback")
    admin.run(sql="BEGIN")
    admin.run(sql="DROP SCHEMA recreate_rollback CASCADE")
    admin.run(sql="CREATE SCHEMA recreate_rollback")
    admin.run(sql="CREATE TABLE recreate_rollback.t AS SELECT 2 AS x")
    admin.run(sql="ROLLBACK")
    assert grants(admin=admin, schema="recreate_rollback") == before
    assert owner.run(sql="SELECT * FROM recreate_rollback.t").to_pylist() == [{'x': 1}]


def test_store_outage_cannot_reuse_a_dropped_schemas_grants(launcher, pg_store):
    import psycopg

    server = start_rbac_server(launcher=launcher, store_uri=pg_store.new_database(),
                               env={"HARDCHARGER_RBAC_STORE_TIMEOUT_SECONDS": "1"})
    admin, owner = setup(server=server, name="outage_lifetime")
    owner.run(sql="CREATE TABLE outage_lifetime.t (x INT)")
    with psycopg.connect(conninfo=server.store_uri) as blocker:
        blocker.execute(query="LOCK TABLE hardcharger_rbac.meta IN ACCESS EXCLUSIVE MODE")
        # The DDL succeeds but post-drop metadata cleanup cannot reach the store.
        admin.run(sql="DROP SCHEMA outage_lifetime CASCADE")
        # Reusing that name must fail before DuckDB creates the replacement schema.
        with pytest.raises(ChargewireError) as raised:
            admin.run(sql="CREATE SCHEMA outage_lifetime")
        assert 'could not be recorded' in raised.value.detail
        assert not admin.run(sql="SELECT schema_name FROM duckdb_schemas() WHERE schema_name = 'outage_lifetime'").to_pylist()
        blocker.rollback()
    # The bounded store worker must finish cancelling its blocked request before
    # it accepts another write. Once recovered, reuse cleans old grants first.
    import time
    deadline = time.monotonic() + 10
    while True:
        try:
            admin.run(sql="CREATE SCHEMA outage_lifetime")
            break
        except ChargewireError as error:
            if 'store unavailable' not in error.detail or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)
    admin.run(sql="CREATE TABLE outage_lifetime.t AS SELECT 42 AS x")
    expect_blocked(client=owner, sql="SELECT * FROM outage_lifetime.t", why="Hardcharger blocked")
