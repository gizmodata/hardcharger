"""Grants on attached catalogs: DuckLake and PostgreSQL tables are checked like DuckDB's
own."""

import pytest

from rbac import expect_allowed, expect_blocked, start_rbac_server


@pytest.fixture
def server(launcher, store):
    return start_rbac_server(launcher=launcher, store_uri=store.new_database())


def test_grants_apply_to_ducklake_tables(server, tmp_path):
    admin = server.admin()
    (tmp_path / "data").mkdir()
    for sql in ["INSTALL ducklake", "LOAD ducklake",
                f"ATTACH 'ducklake:{tmp_path}/meta.ducklake' AS lake (DATA_PATH '{tmp_path}/data/')",
                "CREATE TABLE lake.main.events AS SELECT 1 AS id, 'payload' AS body",
                "CREATE TABLE lake.main.hidden AS SELECT 1 AS id",
                "CREATE ROLE lake_reader", "CREATE USER lake_user", "GRANT lake_reader TO lake_user",
                "GRANT SELECT (id) ON lake.main.events TO lake_reader"]:
        expect_allowed(client=admin, sql=sql)
    user = server.user(name="lake_user")
    expect_allowed(client=user, sql="SELECT id FROM lake.main.events")
    expect_allowed(client=user, sql="SELECT count(*) FROM lake.events")
    expect_blocked(client=user, sql="SELECT body FROM lake.main.events", why="no SELECT on column(s) body")
    expect_blocked(client=user, sql="SELECT * FROM lake.main.hidden", why="no SELECT privilege on table lake.main.hidden")
    expect_blocked(client=user, sql="INSERT INTO lake.main.events VALUES (2, 'x')", why="no INSERT privilege")
    # Time travel reads the same table: same check.
    expect_blocked(client=user, sql="SELECT body FROM lake.main.events AT (VERSION => 1)", why="body")
    # A DuckLake view the administrator made reads with the administrator's rights
    # (DuckLake stores the view's SQL and parses it back: the same definition).
    expect_allowed(client=admin, sql="CREATE VIEW lake.main.hidden_v AS SELECT id FROM lake.main.hidden")
    expect_blocked(client=user, sql="SELECT * FROM lake.main.hidden_v", why="no SELECT privilege on view")
    expect_allowed(client=admin, sql="GRANT SELECT ON lake.main.hidden_v TO lake_reader")
    expect_allowed(client=user, sql="SELECT * FROM lake.main.hidden_v")
    expect_blocked(client=user, sql="SELECT * FROM lake.main.hidden", why="no SELECT privilege on table lake.main.hidden")
    # Prepared statements on DuckLake (DuckDB re-plans them for every run) are checked too.
    prepared = user.prepare(sql="SELECT id FROM lake.main.events")
    assert user.execute_prepared(prepared_id=prepared).num_rows == 1
    expect_allowed(client=admin, sql="REVOKE SELECT (id) ON lake.main.events FROM lake_reader")
    with pytest.raises(Exception) as raised:
        user.execute_prepared(prepared_id=prepared)
    assert "no SELECT" in str(raised.value)
    # Row and masking policies apply to DuckLake tables as well.
    for sql in ["CREATE TABLE lake.main.governed AS SELECT * FROM (VALUES (1, 'EU', 'a@x'), (2, 'US', 'b@x')) "
                "t(id, region, email)",
                "GRANT SELECT ON lake.main.governed TO lake_reader",
                "CREATE ROW POLICY eu_only ON lake.main.governed USING (region = 'EU')",
                "CREATE MASKING POLICY lake_mask AS (val VARCHAR) RETURNS VARCHAR -> 'masked'",
                "ALTER TABLE lake.main.governed MODIFY COLUMN email SET MASKING POLICY lake_mask"]:
        expect_allowed(client=admin, sql=sql)
    assert user.run(sql="SELECT id FROM lake.main.governed").column("id").to_pylist() == [1]
    assert user.run(sql="SELECT email FROM lake.main.governed").column("email").to_pylist() == ["masked"]
    assert user.run(sql="SELECT count(*) AS n FROM lake.main.governed WHERE email = 'a@x'").column("n").to_pylist() == [0]


def test_grants_apply_to_postgres_tables(server, pg_store):
    admin = server.admin()
    target = pg_store.new_database()
    for sql in ["LOAD postgres", f"ATTACH '{target}' AS pgc (TYPE postgres)",
                "CREATE TABLE pgc.public.rbac_pg_t AS SELECT 1 AS id, 'pg-secret' AS s",
                "CREATE ROLE lake_reader", "CREATE USER lake_user", "GRANT lake_reader TO lake_user",
                "GRANT SELECT (id) ON pgc.public.rbac_pg_t TO lake_reader"]:
        expect_allowed(client=admin, sql=sql)
    user = server.user(name="lake_user")
    expect_allowed(client=user, sql="SELECT id FROM pgc.public.rbac_pg_t")
    expect_blocked(client=user, sql="SELECT s FROM pgc.public.rbac_pg_t", why="no SELECT on column(s) s")
    # The pass-through functions run SQL in PostgreSQL directly: refused.
    expect_blocked(client=user, sql="SELECT * FROM postgres_query('pgc', 'SELECT s FROM public.rbac_pg_t')",
                   why="the table function postgres_query")
    expect_blocked(client=user, sql="CALL postgres_execute('pgc', 'DROP TABLE public.rbac_pg_t')",
                   why="the table function postgres_execute")
    expect_allowed(client=admin, sql="SELECT * FROM pgc.public.rbac_pg_t")  # still there
    expect_allowed(client=admin, sql="DETACH pgc")
