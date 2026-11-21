"""DuckLake's compaction under row and masking policies. An administrator's token with
catalog_access rules on a store server may compact a catalog its rules let it write
(test_tenant_isolation.py), and a row policy applies to it as to any restricted principal: the
compaction would read the table through the policy and replace its files with the rows the
policy shows. On the first 2.13.4 build it did, and DuckLake's commit then failed with an
internal error that invalidated the whole database. Refused, as any write into a table with
policies is; the table keeps every row."""

import pytest

from rbac import expect_allowed, expect_blocked, start_rbac_server

pytestmark = pytest.mark.rbac_store


@pytest.fixture(scope="module")
def world(module_launcher, store, tmp_path_factory):
    work = tmp_path_factory.mktemp("lake")
    (work / "data").mkdir()
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               args=["--database", str(work / "home.duckdb")],
                               env={"HARDCHARGER_INIT_SQL_COMMANDS": "INSTALL ducklake; LOAD ducklake;"})
    admin = server.admin()
    expect_allowed(client=admin, sql=f"ATTACH 'ducklake:{work}/p.ducklake' AS lake_p (DATA_PATH '{work}/data/')")
    yield {"server": server, "admin": admin}
    expect_allowed(client=admin, sql="DETACH lake_p")


def files_of(admin, table: str) -> int:
    return admin.run(sql=f"SELECT count(*) AS n FROM ducklake_list_files('lake_p', '{table}')").column("n").to_pylist()[0]


POLICIES = {
    "row": (["CREATE ROW POLICY us_only_{t} ON lake_p.main.{t} USING (region = 'US')"],
            ["DROP ROW POLICY us_only_{t} ON lake_p.main.{t}"]),
    "masking": (["CREATE MASKING POLICY hide_{t} AS (val VARCHAR) RETURNS VARCHAR -> 'x'",
                 "ALTER TABLE lake_p.main.{t} MODIFY COLUMN region SET MASKING POLICY hide_{t}"],
                ["ALTER TABLE lake_p.main.{t} MODIFY COLUMN region UNSET MASKING POLICY"]),
}


@pytest.mark.parametrize("policy", sorted(POLICIES))
def test_an_administrator_token_with_rules_keeps_a_governed_tables_rows(world, policy):
    admin = world["admin"]
    table = f"governed_{policy}"
    ruled = world["server"].user(name=f"ruled_admin_{table}", role="admin",
                                 catalog_access=[{"catalog": "lake_p", "access": "write"},
                                                 {"catalog": "*", "access": "none"}])
    expect_allowed(client=admin, sql=f"CREATE OR REPLACE TABLE lake_p.main.{table} (i INT, region VARCHAR)")
    for k in range(4):
        expect_allowed(client=admin, sql=f"INSERT INTO lake_p.main.{table} SELECT range, CASE WHEN range % 2 = 0 "
                                         f"THEN 'EU' ELSE 'US' END FROM range({k * 1000}, {k * 1000 + 1000})")
    create, drop = POLICIES[policy]
    for sql in create:
        expect_allowed(client=admin, sql=sql.format(t=table))
    expect_blocked(client=ruled, sql="CALL ducklake_merge_adjacent_files('lake_p')",
                   why=f"DuckLake's rewrite of the files of lake_p.main.{table}, which has row or masking policies, "
                       f"is not available under access control yet")
    assert files_of(admin=admin, table=table) == 4
    rows = admin.run(sql=f"SELECT count(*) AS n, count(*) FILTER (WHERE region = 'EU') AS eu "
                         f"FROM lake_p.main.{table}").to_pylist()
    assert rows == [{"n": 4000, "eu": 2000}], rows
    # The positive twin: without the policy, the same token compacts it, every row kept.
    for sql in drop:
        expect_allowed(client=admin, sql=sql.format(t=table))
    expect_allowed(client=ruled, sql="CALL ducklake_merge_adjacent_files('lake_p')")
    assert files_of(admin=admin, table=table) == 1
    rows = admin.run(sql=f"SELECT count(*) AS n, count(*) FILTER (WHERE region = 'EU') AS eu "
                         f"FROM lake_p.main.{table}").to_pylist()
    assert rows == [{"n": 4000, "eu": 2000}], rows
