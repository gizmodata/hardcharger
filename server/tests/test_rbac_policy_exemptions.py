"""Policy exemptions: GRANT EXEMPT ROW POLICIES, EXEMPT MASKING POLICIES TO ROLE replicator.

A role holding EXEMPT ROW POLICIES reads every table without row-policy filters; one
holding EXEMPT MASKING POLICIES reads every column unmasked; one grant covers every table,
those made later too. A principal exempt from every row policy and mask on a table writes
it as freely as it reads it (INSERT, UPDATE, MERGE, INSERT ... ON CONFLICT, DELETE ...
RETURNING); everyone else is refused as before. Only an unrestricted administrator (the
administrator, or a session acting as System Admin) grants and revokes them, never with an
admin option; scoped tokens and tokens with catalog_access rules never pick one up; tenant
boundaries stay; a trigger body uses its owner's exemptions; a revoke counts at the next
statement, prepared ones and other replicas included. Each test gets its own server and
store, so none leaves anything behind for another.
"""

import urllib.parse

import psycopg
import pyarrow as pa
import pytest

from conftest import TIMEOUT_SCALE
from rbac import ChargewireError, expect_allowed, expect_blocked, expect_error, mint, start_rbac_server
from test_rbac_system_admin_upgrade import Forwarder, wait_for

EXEMPTIONS = "EXEMPT ROW POLICIES, EXEMPT MASKING POLICIES"
ALL_ROWS = [1, 2, 3]
REAL_SSN = ["111", "222", "333"]


def refused_write(verb: str, table: str = "emp") -> str:
    """Today's refusal of a write into a table with row or masking policies, word for word."""
    return (f"Hardcharger blocked this statement: {verb} on memory.main.{table}, which has row or masking "
            "policies, is not available under access control yet")


REFUSED_DELETE_RETURNING = ("Hardcharger blocked this statement: DELETE ... RETURNING on memory.main.emp, which has "
                            "masked columns")
ONLY = "only the administrator, or a user acting as System Admin (SET ROLE SYSTEM ADMIN)"


def rows(client, sql: str, parameters=None) -> list[dict]:
    return expect_allowed(client=client, sql=sql, parameters=parameters).to_pylist()


def column(client, sql: str) -> list:
    return sorted(next(iter(r.values())) for r in rows(client=client, sql=sql))


class World:
    """One server: emp has a row policy for staff and a masked ssn; rows_only has only a row
    policy, masks_only only a mask. staff (ann) and replicator (rep) may read and write all
    three; replicator is in no policy's TO list, so without an exemption it sees no rows."""

    def __init__(self, server):
        self.server = server
        self.admin = server.admin()

    def user(self, name: str, **claims):
        return self.server.user(name=name, **claims)


SETUP = [
    "CREATE TABLE emp (id INT PRIMARY KEY, region VARCHAR, ssn VARCHAR)",
    "INSERT INTO emp VALUES (1, 'EU', '111'), (2, 'US', '222'), (3, 'APAC', '333')",
    "CREATE TABLE rows_only (id INT PRIMARY KEY, region VARCHAR)",
    "INSERT INTO rows_only VALUES (1, 'EU'), (2, 'US'), (3, 'APAC')",
    "CREATE TABLE masks_only (id INT PRIMARY KEY, ssn VARCHAR)",
    "INSERT INTO masks_only VALUES (1, '111'), (2, '222'), (3, '333')",
    "CREATE TABLE plain (id INT)",
    "CREATE ROLE staff", "CREATE ROLE replicator", "CREATE ROLE row_exempt", "CREATE ROLE mask_exempt",
    "CREATE ROLE readers",
    "CREATE USER ann", "CREATE USER rep", "CREATE USER rob", "CREATE USER max", "CREATE USER rea",
    "GRANT staff TO ann", "GRANT replicator TO rep", "GRANT row_exempt TO rob", "GRANT mask_exempt TO max",
    "GRANT readers TO rea",
    "CREATE ROW POLICY eu ON emp TO staff USING (region = 'EU')",
    "CREATE ROW POLICY eu ON rows_only TO staff USING (region = 'EU')",
    "CREATE MASKING POLICY hide AS (v VARCHAR) RETURNS VARCHAR -> 'XXX'",
    "ALTER TABLE emp MODIFY COLUMN ssn SET MASKING POLICY hide",
    "ALTER TABLE masks_only MODIFY COLUMN ssn SET MASKING POLICY hide",
    "GRANT EXEMPT ROW POLICIES, EXEMPT MASKING POLICIES TO ROLE replicator",
    "GRANT EXEMPT ROW POLICIES TO ROLE row_exempt",
    "GRANT EXEMPT MASKING POLICIES TO ROLE mask_exempt",
    # readers holds both exemptions and SELECT only: an exemption is no grant.
    "GRANT EXEMPT ROW POLICIES, EXEMPT MASKING POLICIES TO ROLE readers",
    "GRANT SELECT ON emp TO readers",
] + [f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}"
     for table in ("emp", "rows_only", "masks_only", "plain")
     for role in ("staff", "replicator", "row_exempt", "mask_exempt")]


def make_world(launcher, store_uri: str, env: dict | None = None) -> World:
    world = World(server=start_rbac_server(launcher=launcher, store_uri=store_uri, env=env))
    for sql in SETUP:
        expect_allowed(client=world.admin, sql=sql)
    return world


@pytest.fixture
def world(launcher, store):
    return make_world(launcher=launcher, store_uri=store.new_database())


def emp(client) -> list[dict]:
    return rows(client=client, sql="SELECT id, ssn FROM emp ORDER BY id")


def assert_held_to_policies(client) -> None:
    """The negative twin, with today's messages: filtered, masked, refused on writes."""
    assert column(client=client, sql="SELECT id FROM emp") == [1]
    assert column(client=client, sql="SELECT ssn FROM emp") == ["XXX"]
    assert column(client=client, sql="SELECT ssn FROM masks_only") == ["XXX"] * 3
    expect_blocked(client=client, sql="INSERT INTO emp VALUES (9, 'EU', '999')", why=refused_write(verb="INSERT"))
    expect_blocked(client=client, sql="UPDATE emp SET region = region", why=refused_write(verb="UPDATE"))
    expect_blocked(client=client, sql="DELETE FROM emp WHERE id = 1 RETURNING *", why=REFUSED_DELETE_RETURNING)


# ---- What each exemption lifts ----


def test_each_exemption_lifts_every_policy_of_its_kind(world):
    ann, rep, rob, max_ = (world.user(name=n) for n in ("ann", "rep", "rob", "max"))
    # The negative twins first: staff is filtered and masked; without an exemption a role no
    # permissive policy covers sees no rows at all (rep's own role is in no TO list).
    assert emp(client=ann) == [{"id": 1, "ssn": "XXX"}]
    # Both exemptions: every row, every value.
    assert emp(client=rep) == [{"id": i, "ssn": s} for i, s in zip(ALL_ROWS, REAL_SSN)]
    assert column(client=rep, sql="SELECT id FROM rows_only") == ALL_ROWS
    assert column(client=rep, sql="SELECT ssn FROM masks_only") == REAL_SSN
    # Rows only: every row, still masked; masks only: unmasked, still filtered (no rows).
    assert emp(client=rob) == [{"id": i, "ssn": "XXX"} for i in ALL_ROWS]
    assert column(client=rob, sql="SELECT ssn FROM masks_only") == ["XXX"] * 3
    assert emp(client=max_) == []
    assert column(client=max_, sql="SELECT ssn FROM masks_only") == REAL_SSN
    assert column(client=max_, sql="SELECT id FROM rows_only") == []
    # Masks are lifted wherever the column is used: filters, joins, grouping.
    assert column(client=rep, sql="SELECT count(*) FROM emp WHERE ssn = '222'") == [1]
    assert column(client=ann, sql="SELECT count(*) FROM emp WHERE ssn = '111'") == [0]
    # One grant covers tables made later.
    for sql in ["CREATE TABLE later (id INT, secret VARCHAR)", "INSERT INTO later VALUES (1, 'a'), (2, 'b')",
                "GRANT SELECT ON later TO replicator", "GRANT SELECT ON later TO staff",
                "CREATE ROW POLICY none_of_it ON later USING (false)",
                "ALTER TABLE later MODIFY COLUMN secret SET MASKING POLICY hide"]:
        expect_allowed(client=world.admin, sql=sql)
    assert column(client=rep, sql="SELECT secret FROM later") == ["a", "b"]
    assert column(client=ann, sql="SELECT secret FROM later") == []


def test_an_exempt_role_writes_as_freely_as_it_reads(world):
    rep = world.user(name="rep")
    expect_allowed(client=rep, sql="INSERT INTO emp VALUES (4, 'US', '444')")
    expect_allowed(client=rep, sql="UPDATE emp SET region = 'NA' WHERE id = 2")
    expect_allowed(client=rep, sql="INSERT INTO emp VALUES (4, 'US', '445') ON CONFLICT (id) DO UPDATE SET ssn = '446'")
    expect_allowed(client=rep, sql="INSERT OR REPLACE INTO emp VALUES (5, 'EU', '555')")
    expect_allowed(client=rep, sql="MERGE INTO emp USING (SELECT 6 AS id, 'US' AS region, '666' AS ssn) s ON emp.id = s.id "
                                   "WHEN MATCHED THEN UPDATE SET ssn = s.ssn WHEN NOT MATCHED THEN INSERT VALUES "
                                   "(s.id, s.region, s.ssn)")
    returned = rows(client=rep, sql="DELETE FROM emp WHERE id = 6 RETURNING id, ssn")
    assert returned == [{"id": 6, "ssn": "666"}]  # the real value: it reads it unmasked too
    returned = rows(client=rep, sql="UPDATE emp SET region = 'US' WHERE id = 2 RETURNING id, ssn")
    assert returned == [{"id": 2, "ssn": "222"}]
    # A bulk ingest is an INSERT too.
    assert rep.ingest(table="emp", rows=pa.table({"id": pa.array([7], type=pa.int32()), "region": ["EU"],
                                                  "ssn": ["777"]}), mode="append") == 1
    assert rows(client=world.admin, sql="SELECT id, region, ssn FROM emp ORDER BY id") == [
        {"id": 1, "region": "EU", "ssn": "111"}, {"id": 2, "region": "US", "ssn": "222"},
        {"id": 3, "region": "APAC", "ssn": "333"}, {"id": 4, "region": "US", "ssn": "446"},
        {"id": 5, "region": "EU", "ssn": "555"}, {"id": 7, "region": "EU", "ssn": "777"}]
    # DELETE without RETURNING reaches every row it reads, now every row.
    expect_allowed(client=rep, sql="DELETE FROM emp WHERE id >= 4")
    assert column(client=world.admin, sql="SELECT id FROM emp") == ALL_ROWS
    # The negative twin: staff keeps today's refusals, word for word.
    ann = world.user(name="ann")
    assert_held_to_policies(client=ann)
    expect_blocked(client=ann, sql="INSERT INTO emp VALUES (4, 'EU', '4') ON CONFLICT (id) DO NOTHING",
                   why=refused_write(verb="MERGE (or INSERT ... ON CONFLICT)"))
    expect_blocked(client=ann, sql="MERGE INTO emp USING (SELECT 1 AS id) s ON emp.id = s.id WHEN MATCHED THEN "
                                   "UPDATE SET region = 'EU'", why=refused_write(verb="MERGE (or INSERT ... ON CONFLICT)"))
    with pytest.raises(ChargewireError) as ingest:
        ann.ingest(table="emp", rows=pa.table({"id": pa.array([8], type=pa.int32()), "region": ["EU"],
                                               "ssn": ["888"]}), mode="append")
    assert refused_write(verb="INSERT") in ingest.value.detail, ingest.value
    assert column(client=world.admin, sql="SELECT id FROM emp") == ALL_ROWS


def test_a_write_needs_an_exemption_from_everything_that_applies(world):
    rob, max_ = world.user(name="rob"), world.user(name="max")
    # rob is exempt from row policies only: rows_only is his to write, emp (masked) is not.
    expect_allowed(client=rob, sql="INSERT INTO rows_only VALUES (4, 'US')")
    expect_allowed(client=rob, sql="UPDATE rows_only SET region = 'NA' WHERE id = 4")
    expect_blocked(client=rob, sql="INSERT INTO emp VALUES (4, 'US', '4')", why=refused_write(verb="INSERT"))
    expect_blocked(client=rob, sql="DELETE FROM emp WHERE id = 3 RETURNING *", why=REFUSED_DELETE_RETURNING)
    # max is exempt from masks only: masks_only is his, rows_only and emp (row policy) are not.
    expect_allowed(client=max_, sql="INSERT INTO masks_only VALUES (4, '444')")
    assert rows(client=max_, sql="DELETE FROM masks_only WHERE id = 4 RETURNING ssn") == [{"ssn": "444"}]
    expect_blocked(client=max_, sql="INSERT INTO rows_only VALUES (5, 'US')", why=refused_write(verb="INSERT",
                                                                                                 table="rows_only"))
    expect_blocked(client=max_, sql="UPDATE emp SET ssn = 'x'", why=refused_write(verb="UPDATE"))
    # The twin on masks_only: a role without the masking exemption is still refused there.
    expect_blocked(client=rob, sql="INSERT INTO masks_only VALUES (5, '5')",
                   why=refused_write(verb="INSERT", table="masks_only"))


def test_an_exemption_is_no_grant(world):
    rea = world.user(name="rea")
    assert column(client=rea, sql="SELECT ssn FROM emp") == REAL_SSN  # SELECT, granted, unfiltered and unmasked
    for sql, verb in (("INSERT INTO emp VALUES (9, 'EU', '9')", "INSERT"), ("UPDATE emp SET ssn = 'x'", "UPDATE"),
                      ("DELETE FROM emp", "DELETE")):
        expect_blocked(client=rea, sql=sql, why=f"user rea has no {verb} privilege on table memory.main.emp")
    expect_blocked(client=rea, sql="SELECT * FROM masks_only", why="user rea has no SELECT privilege")


# ---- Who grants them ----


def test_only_an_unrestricted_administrator_grants_and_revokes_them(world):
    admin = world.admin
    for sql in ["CREATE USER alice", "CREATE ROLE helpers", "GRANT helpers TO alice", "GRANT SYSTEM ADMIN TO alice",
                "GRANT MANAGE CONFIG TO ROLE helpers WITH ADMIN OPTION"]:
        expect_allowed(client=admin, sql=sql)
    alice, rep = world.user(name="alice"), world.user(name="rep")
    for exemption in ("EXEMPT ROW POLICIES", "EXEMPT MASKING POLICIES"):
        # A store user, a holder of the exemption itself, or of another privilege's admin option: refused.
        for client in (alice, rep):
            expect_blocked(client=client, sql=f"GRANT {exemption} TO ROLE helpers",
                           why=f"Hardcharger blocked GRANT: {ONLY} grants and revokes {exemption}")
            expect_blocked(client=client, sql=f"REVOKE {exemption} FROM ROLE replicator",
                           why=f"Hardcharger blocked REVOKE: {ONLY} grants and revokes {exemption}")
        # Never with an admin option, whoever asks.
        expect_blocked(client=admin, sql=f"GRANT {exemption} TO ROLE helpers WITH ADMIN OPTION",
                       why=f"Hardcharger blocked GRANT: {exemption} has no admin option")
    # No grant to a user, a tenant's role, or on a location.
    expect_error(call=lambda: admin.run(sql="GRANT EXEMPT ROW POLICIES TO alice"), code="sql_error",
                 why="privileges are granted to roles, not users")
    for sql in ["CREATE TENANT acme", "CREATE ROLE acme/loaders"]:
        expect_allowed(client=admin, sql=sql)
    expect_error(call=lambda: admin.run(sql="GRANT EXEMPT ROW POLICIES TO ROLE acme/loaders"), code="sql_error",
                 why="system privileges reach the whole server; a tenant's role (acme/loaders) cannot hold them")
    with pytest.raises(ChargewireError) as located:
        admin.run(sql="GRANT EXEMPT ROW POLICIES ON LOCATION 's3://bucket/' TO ROLE helpers")
    assert "EXEMPT ROW POLICIES applies to the whole server: no ON clause" in located.value.detail, located.value
    # An administrator's token with catalog_access rules runs no access-control statement.
    restricted = world.server.client(token=mint(user="admin", role="admin",
                                                catalog_access=[{"catalog": "*", "access": "write"}]))
    expect_blocked(client=restricted, sql="GRANT EXEMPT ROW POLICIES TO ROLE helpers",
                   why="an administrator token with catalog_access rules cannot run access-control statements")
    granted = "SELECT count(*) AS n FROM hardcharger_grants() WHERE grantee = 'helpers' AND privilege LIKE 'EXEMPT%'"
    assert rows(client=admin, sql=granted) == [{"n": 0}]  # nothing was granted to helpers
    # The positive twins: a session acting as System Admin, and the administrator.
    expect_allowed(client=alice, sql="SET ROLE SYSTEM ADMIN")
    expect_allowed(client=alice, sql="GRANT EXEMPT ROW POLICIES TO ROLE helpers")
    expect_allowed(client=alice, sql="GRANT SELECT ON emp TO helpers")
    expect_allowed(client=alice, sql="RESET ROLE")
    assert column(client=alice, sql="SELECT id FROM emp") == ALL_ROWS
    assert rows(client=admin, sql="SELECT grantor FROM hardcharger_grants() WHERE grantee = 'helpers' AND "
                                  "privilege = 'EXEMPT ROW POLICIES'") == [{"grantor": "administrator"}]
    expect_allowed(client=admin, sql="REVOKE EXEMPT ROW POLICIES FROM ROLE helpers")
    assert column(client=alice, sql="SELECT id FROM emp") == []


def test_a_role_carries_one_only_through_memberships_the_administrator_granted(world):
    admin = world.admin
    # del may hand out replicator (WITH ADMIN OPTION), as she may any role she administers.
    for sql in ["CREATE USER del", "CREATE USER bob", "CREATE USER cy", "CREATE ROLE team",
                "GRANT replicator TO del WITH ADMIN OPTION", "GRANT team TO cy"]:
        expect_allowed(client=admin, sql=sql)
    dele = world.user(name="del")
    expect_allowed(client=dele, sql="GRANT replicator TO bob")
    expect_allowed(client=dele, sql="GRANT replicator TO ROLE team")
    assert column(client=dele, sql="SELECT ssn FROM emp") == REAL_SSN  # hers came from the administrator
    for name in ("bob", "cy"):
        session = world.user(name=name)
        # The role's grants reach them; its exemptions don't: no rows (no policy names them), masked, refused.
        assert column(client=session, sql="SELECT id FROM emp") == []
        assert column(client=session, sql="SELECT ssn FROM masks_only") == ["XXX"] * 3
        expect_blocked(client=session, sql="INSERT INTO rows_only VALUES (9, 'EU')",
                       why=refused_write(verb="INSERT", table="rows_only"))
        assert rows(client=session, sql="SELECT count(*) AS n FROM hardcharger_effective_privileges() "
                                        "WHERE privilege LIKE 'EXEMPT%'") == [{"n": 0}]
    # The twin: the administrator's own grant of the same role.
    expect_allowed(client=admin, sql="GRANT replicator TO bob")
    assert column(client=world.user(name="bob"), sql="SELECT ssn FROM emp") == REAL_SSN
    # Never to PUBLIC, which everyone holds.
    expect_blocked(client=admin, sql="GRANT EXEMPT ROW POLICIES TO ROLE PUBLIC",
                   why="Hardcharger blocked GRANT: EXEMPT ROW POLICIES is never granted to PUBLIC")


def test_scoped_and_catalog_restricted_tokens_never_pick_one_up(world):
    rep = world.user(name="rep")
    scoped_token = rows(client=world.admin, sql="CREATE SCOPED TOKEN loader FOR USER rep ALLOW SELECT, INSERT, UPDATE, "
                                                "DELETE")[0]["token"]
    minted_by_rep = rows(client=rep, sql="CREATE SCOPED TOKEN mine ALLOW SELECT, INSERT")[0]["token"]
    sessions = [world.server.client(token=scoped_token), world.server.client(token=minted_by_rep),
                world.user(name="rep", catalog_access=[{"catalog": "*", "access": "write"}])]
    for session in sessions:
        # rep's own role is in no policy's TO list: without the exemption, no rows at all.
        assert column(client=session, sql="SELECT id FROM emp") == []
        assert column(client=session, sql="SELECT ssn FROM masks_only") == ["XXX"] * 3
        expect_blocked(client=session, sql="INSERT INTO emp VALUES (9, 'EU', '9')", why=refused_write(verb="INSERT"))
        assert rows(client=session, sql="SELECT count(*) AS n FROM hardcharger_effective_privileges() "
                                        "WHERE privilege LIKE 'EXEMPT%'") == [{"n": 0}]
    # The twin: rep's own sign-in.
    assert column(client=rep, sql="SELECT id FROM emp") == ALL_ROWS


def test_tenant_boundaries_still_apply(world):
    admin = world.admin
    for sql in ["CREATE TENANT acme", "ATTACH ':memory:' AS acme_db",
                "CREATE TABLE acme_db.main.orders (id INT, region VARCHAR)",
                "INSERT INTO acme_db.main.orders VALUES (1, 'EU'), (2, 'US')", "ALTER CATALOG acme_db SET TENANT acme",
                "CREATE USER acme/tina", "CREATE ROLE acme/clerks", "GRANT acme/clerks TO acme/tina",
                "GRANT SELECT, INSERT ON acme_db.main.orders TO acme/clerks",
                "CREATE ROW POLICY eu_orders ON acme_db.main.orders TO acme/clerks USING (region = 'EU')"]:
        expect_allowed(client=admin, sql=sql)
    rep = world.user(name="rep")
    # An exemption reaches no catalog the principal cannot see.
    expect_blocked(client=rep, sql="SELECT * FROM acme_db.main.orders", why="user rep has no SELECT privilege")
    # A tenant's role can't be granted one; the tenant's user stays filtered and refused.
    expect_error(call=lambda: admin.run(sql="GRANT EXEMPT ROW POLICIES TO ROLE acme/clerks"), code="sql_error",
                 why="system privileges reach the whole server; a tenant's role (acme/clerks) cannot hold them")
    tina = world.user(name="acme/tina")
    assert column(client=tina, sql="SELECT id FROM acme_db.main.orders") == [1]
    expect_blocked(client=tina, sql="INSERT INTO acme_db.main.orders VALUES (3, 'EU')",
                   why=refused_write(verb="INSERT", table="orders").replace("memory.main.", "acme_db.main."))


# ---- Definer's rights ----


def test_a_trigger_body_uses_its_owners_exemptions_never_the_writers(world):
    admin = world.admin
    for sql in ["CREATE SCHEMA app", "CREATE USER mia", "CREATE ROLE builders",
                "GRANT CREATE TABLE, CREATE TRIGGER, DROP TRIGGER, SELECT, INSERT ON SCHEMA app TO builders",
                "GRANT builders, staff TO mia", "GRANT INSERT ON SCHEMA app TO replicator",
                "GRANT INSERT ON SCHEMA app TO staff"]:
        expect_allowed(client=admin, sql=sql)
    mia = world.user(name="mia")
    for sql in ["CREATE TABLE app.feed (id INT)", "CREATE TABLE app.seen (n BIGINT, ssns VARCHAR)",
                # Her trigger reads emp (what it counts and the ssn it sees are the owner's) and writes it.
                "CREATE TRIGGER copy_feed AFTER INSERT ON app.feed FOR EACH STATEMENT "
                "INSERT INTO app.seen SELECT count(*), string_agg(ssn, ',' ORDER BY ssn) FROM emp",
                "CREATE TABLE app.push (id INT)",
                "CREATE TRIGGER push_emp AFTER INSERT ON app.push FOR EACH STATEMENT "
                "INSERT INTO emp SELECT 100 + id, 'US', 'x' FROM app.push"]:
        expect_allowed(client=mia, sql=sql)
    rep, ann = world.user(name="rep"), world.user(name="ann")

    def seen_after(writer) -> dict:
        expect_allowed(client=writer, sql="INSERT INTO app.feed VALUES (1)")
        return rows(client=admin, sql="SELECT n, ssns FROM app.seen ORDER BY rowid DESC LIMIT 1")[0]

    # mia is staff, not exempt: her body sees her rows (EU, masked), whoever writes, the
    # exempt replicator included; and her body may not write emp.
    for writer in (rep, ann):
        assert seen_after(writer=writer) == {"n": 1, "ssns": "XXX"}
        expect_blocked(client=writer, sql="INSERT INTO app.push VALUES (1)", why=refused_write(verb="INSERT"))
    # Exempt mia (through a role of hers): now her body reads and writes everything, for any writer.
    for sql in ["CREATE ROLE mia_loader", "GRANT EXEMPT ROW POLICIES, EXEMPT MASKING POLICIES TO ROLE mia_loader",
                "GRANT mia_loader TO mia"]:
        expect_allowed(client=admin, sql=sql)
    for writer in (ann, rep):
        assert seen_after(writer=writer) == {"n": 3, "ssns": "111,222,333"}
    expect_allowed(client=ann, sql="INSERT INTO app.push VALUES (1)")
    assert column(client=admin, sql="SELECT id FROM emp WHERE id > 100") == [101]
    # ann (the writer) gained nothing from it: her own statement is still held to her policies.
    assert_held_to_policies(client=ann)


def test_views_and_macros_lend_no_exemption(world):
    admin = world.admin
    for sql in ["CREATE SCHEMA shared", "CREATE ROLE makers",
                "GRANT CREATE VIEW, CREATE MACRO, SELECT ON SCHEMA shared TO makers",
                "GRANT makers TO rep", "GRANT makers TO ann", "GRANT SELECT ON SCHEMA shared TO staff"]:
        expect_allowed(client=admin, sql=sql)
    rep, ann = world.user(name="rep"), world.user(name="ann")
    # rep (exempt) owns a view and a macro over emp; ann (not exempt) reads through them.
    expect_allowed(client=rep, sql="CREATE VIEW shared.all_emp AS SELECT id, ssn FROM memory.main.emp")
    expect_allowed(client=rep, sql="CREATE MACRO shared.emp_rows() AS TABLE SELECT id, ssn FROM memory.main.emp")
    # A view reads with its owner's grants, but policies follow the reader: ann's.
    assert rows(client=ann, sql="SELECT id, ssn FROM shared.all_emp") == [{"id": 1, "ssn": "XXX"}]
    assert rows(client=ann, sql="SELECT id, ssn FROM shared.emp_rows()") == [{"id": 1, "ssn": "XXX"}]
    # The positive twin: the exempt reader, through the same view and macro, and through
    # ann's (non-exempt) view: it is the reader's exemption that counts.
    expect_allowed(client=ann, sql="CREATE VIEW shared.ann_emp AS SELECT id, ssn FROM memory.main.emp")
    expected = [{"id": i, "ssn": s} for i, s in zip(ALL_ROWS, REAL_SSN)]
    for sql in ("SELECT id, ssn FROM shared.all_emp ORDER BY id", "SELECT id, ssn FROM shared.emp_rows() ORDER BY id",
                "SELECT id, ssn FROM shared.ann_emp ORDER BY id"):
        assert rows(client=rep, sql=sql) == expected


# ---- Revokes ----


def test_a_revoke_counts_at_the_next_statement_prepared_ones_included(world):
    admin = world.admin
    rep = world.user(name="rep")
    read = rep.prepare(sql="SELECT id, ssn FROM emp ORDER BY id")
    write = rep.prepare(sql="UPDATE emp SET region = region WHERE id = 3")
    with_parameter = rep.prepare(sql="SELECT count(*) AS n FROM emp WHERE id >= ?")
    assert rep.execute_prepared(prepared_id=read).to_pylist() == [{"id": i, "ssn": s} for i, s in zip(ALL_ROWS, REAL_SSN)]
    rep.execute_prepared(prepared_id=write)
    assert rep.execute_prepared(prepared_id=with_parameter, parameters=[0]).to_pylist() == [{"n": 3}]

    expect_allowed(client=admin, sql="REVOKE EXEMPT MASKING POLICIES FROM ROLE replicator")
    assert rep.execute_prepared(prepared_id=read).column("ssn").to_pylist() == ["XXX"] * 3
    with pytest.raises(ChargewireError) as refused:
        rep.execute_prepared(prepared_id=write)
    assert refused_write(verb="UPDATE") in refused.value.detail, refused.value
    expect_allowed(client=admin, sql="REVOKE EXEMPT ROW POLICIES FROM ROLE replicator")
    assert rep.execute_prepared(prepared_id=read).to_pylist() == []
    assert rep.execute_prepared(prepared_id=with_parameter, parameters=[0]).to_pylist() == [{"n": 0}]
    assert column(client=rep, sql="SELECT id FROM emp") == []
    # Granted again: the same prepared statements read everything again.
    expect_allowed(client=admin, sql=f"GRANT {EXEMPTIONS} TO ROLE replicator")
    assert rep.execute_prepared(prepared_id=read).column("ssn").to_pylist() == REAL_SSN
    # Taking the role away, and SET ROLE to a role without it, count the same way.
    expect_allowed(client=admin, sql="GRANT staff TO rep")
    expect_allowed(client=rep, sql="SET ROLE staff")
    assert rep.execute_prepared(prepared_id=read).to_pylist() == [{"id": 1, "ssn": "XXX"}]
    expect_allowed(client=rep, sql="RESET ROLE")
    assert rep.execute_prepared(prepared_id=read).column("ssn").to_pylist() == REAL_SSN
    expect_allowed(client=admin, sql="REVOKE replicator FROM rep")
    assert rep.execute_prepared(prepared_id=read).to_pylist() == [{"id": 1, "ssn": "XXX"}]


def test_a_revoke_stops_a_statement_that_is_running(world):
    runner = world.user(name="rep")
    running = runner.submit(sql="SELECT sum(e.id * r.range) FROM emp e, range(1000000000000) r")
    try:
        assert runner.wait(statement_id=running, timeout=2 * TIMEOUT_SCALE)["state"] == "running"
        expect_allowed(client=world.admin, sql="REVOKE EXEMPT ROW POLICIES FROM ROLE replicator")
        doc = runner.wait(statement_id=running, timeout=30 * TIMEOUT_SCALE)
        assert doc["state"] in ("failed", "cancelled"), "a statement kept reading every row after the revoke"
    finally:
        runner.http.delete(url=f"{runner.url}/statements/{running}", headers=runner.headers())
    assert column(client=runner, sql="SELECT id FROM emp") == []
    # The twin: a grant takes nothing away, and a statement running meanwhile goes on.
    running = runner.submit(sql="SELECT sum(e.id * r.range) FROM emp e, range(1000000000000) r")
    try:
        expect_allowed(client=world.admin, sql="GRANT EXEMPT ROW POLICIES TO ROLE replicator")
        assert runner.wait(statement_id=running, timeout=3 * TIMEOUT_SCALE)["state"] == "running"
    finally:
        runner.http.delete(url=f"{runner.url}/statements/{running}", headers=runner.headers())


def test_a_revoke_on_another_replica_counts_at_once(launcher, pg_store):
    uri = pg_store.new_database()
    first, second = (start_rbac_server(launcher=launcher, store_uri=uri) for _ in range(2))
    admin, local_admin = first.admin(), second.admin()
    # Each replica has its own memory database: its tables first, then the shared grants.
    for sql in SETUP[:6]:
        expect_allowed(client=local_admin, sql=sql)
    for sql in SETUP:
        expect_allowed(client=admin, sql=sql)
    wait_for(condition=lambda: "rep" in {r["name"] for r in rows(client=local_admin,
                                                                   sql="SELECT name FROM hardcharger_users()")},
             timeout=15, what="the users to reach the second replica")
    rep = second.user(name="rep")
    read = rep.prepare(sql="SELECT id, ssn FROM emp ORDER BY id")
    assert column(client=rep, sql="SELECT ssn FROM emp") == REAL_SSN
    expect_allowed(client=admin, sql="REVOKE EXEMPT MASKING POLICIES FROM ROLE replicator")
    # No wait for the second replica to poll: a session that holds an exemption asks the
    # store before every statement.
    assert column(client=rep, sql="SELECT ssn FROM emp") == ["XXX"] * 3
    expect_allowed(client=admin, sql="REVOKE EXEMPT ROW POLICIES FROM ROLE replicator")
    assert rep.execute_prepared(prepared_id=read).to_pylist() == []
    expect_blocked(client=rep, sql="INSERT INTO rows_only VALUES (9, 'EU')", why=refused_write(verb="INSERT",
                                                                                                table="rows_only"))


def test_a_replica_cut_off_from_the_store_applies_every_policy(launcher, pg_store):
    uri = pg_store.new_database()
    parts = urllib.parse.urlsplit(uri)
    forwarder = Forwarder(host=parts.hostname, port=parts.port)
    try:
        through = urllib.parse.urlunsplit(parts._replace(
            netloc=parts.netloc.replace(f":{parts.port}", f":{forwarder.port}")))
        world = make_world(launcher=launcher, store_uri=through, env={"HARDCHARGER_RBAC_STORE_TIMEOUT_SECONDS": "2"})
        rep, ann = world.user(name="rep"), world.user(name="ann")
        assert column(client=rep, sql="SELECT ssn FROM emp") == REAL_SSN
        forwarder.cut()
        # What this replica last heard is not enough to lift a policy on: filtered, masked, refused.
        assert column(client=rep, sql="SELECT id FROM emp") == []
        assert column(client=rep, sql="SELECT ssn FROM masks_only") == ["XXX"] * 3
        expect_blocked(client=rep, sql="INSERT INTO rows_only VALUES (9, 'EU')",
                       why=refused_write(verb="INSERT", table="rows_only"))
        # A user without an exemption is served from the last grants as before (the twin).
        assert column(client=ann, sql="SELECT id FROM emp") == [1]
        forwarder.restore()
        wait_for(condition=lambda: column(client=rep, sql="SELECT ssn FROM masks_only") == REAL_SSN, timeout=30,
                 what="the exemption to apply again once the store answers")
    finally:
        forwarder.close()


# ---- Seen, and recorded ----


def test_they_show_in_show_grants_the_grants_listing_and_effective_privileges(world):
    admin, rep, ann = world.admin, world.user(name="rep"), world.user(name="ann")
    shown = rows(client=admin, sql="SHOW GRANTS FOR ROLE replicator")
    assert sorted((r["privilege"], r["object_type"], r["grant_option"], r["granted_by"]) for r in shown
                  if r["privilege"].startswith("EXEMPT")) == [
        ("EXEMPT MASKING POLICIES", "SYSTEM", "NO", "administrator"),
        ("EXEMPT ROW POLICIES", "SYSTEM", "NO", "administrator")]
    assert {r["privilege"] for r in rows(client=rep, sql="SHOW GRANTS") if r["privilege"].startswith("EXEMPT")} == {
        "EXEMPT MASKING POLICIES", "EXEMPT ROW POLICIES"}
    listed = rows(client=admin, sql="SELECT grantee, privilege, object_type, with_grant_option FROM hardcharger_grants() "
                                    "WHERE privilege LIKE 'EXEMPT%' ORDER BY grantee, privilege")
    assert [(r["grantee"], r["privilege"]) for r in listed] == [
        ("mask_exempt", "EXEMPT MASKING POLICIES"), ("readers", "EXEMPT MASKING POLICIES"),
        ("readers", "EXEMPT ROW POLICIES"), ("replicator", "EXEMPT MASKING POLICIES"),
        ("replicator", "EXEMPT ROW POLICIES"), ("row_exempt", "EXEMPT ROW POLICIES")]
    assert all(r["object_type"] == "SYSTEM" and r["with_grant_option"] is False for r in listed)
    mine = "SELECT privilege, via FROM hardcharger_effective_privileges() WHERE privilege LIKE 'EXEMPT%' ORDER BY 1"
    assert rows(client=rep, sql=mine) == [{"privilege": "EXEMPT MASKING POLICIES", "via": "replicator"},
                                          {"privilege": "EXEMPT ROW POLICIES", "via": "replicator"}]
    assert rows(client=ann, sql=mine) == []
    assert rows(client=admin, sql="SELECT privilege FROM hardcharger_effective_privileges('rob') "
                                  "WHERE privilege LIKE 'EXEMPT%'") == [{"privilege": "EXEMPT ROW POLICIES"}]
    # hardcharger_policies() says which policies apply to the session: none to rep.
    applies = "SELECT name, table_name, applies_to_current_user AS a FROM hardcharger_policies() ORDER BY 2, 1"
    assert {(r["name"], r["table_name"]): r["a"] for r in rows(client=rep, sql=applies)} == {
        ("eu", "emp"): False, ("hide", "emp"): False, ("hide", "masks_only"): False, ("eu", "rows_only"): False}
    assert {(r["name"], r["table_name"]): r["a"] for r in rows(client=ann, sql=applies)} == {
        ("eu", "emp"): True, ("hide", "emp"): True, ("hide", "masks_only"): True, ("eu", "rows_only"): True}


def test_the_audit_records_what_an_exemption_let_through(launcher, store):
    world = make_world(launcher=launcher, store_uri=store.new_database(), env={"HARDCHARGER_LOG_FORMAT": "json"})
    rep, ann = world.user(name="rep"), world.user(name="ann")
    expect_allowed(client=rep, sql="SELECT id, ssn FROM emp")
    expect_allowed(client=rep, sql="INSERT INTO masks_only VALUES (4, '444')")
    expect_allowed(client=rep, sql="SELECT count(*) FROM plain")
    expect_allowed(client=ann, sql="SELECT id, ssn FROM emp")

    def allowed(session) -> list[str]:
        return [r["objects"] for r in world.server.running.log_records()
                if r.get("message") == "access control allowed a statement" and r.get("session_id") == session]

    by_rep = allowed(session=rep.session_id)
    assert any("SELECT on table memory.main.emp" in o and "EXEMPT ROW POLICIES on table memory.main.emp" in o and
               "EXEMPT MASKING POLICIES on table memory.main.emp" in o for o in by_rep), by_rep
    assert any("INSERT on table memory.main.masks_only" in o and
               "EXEMPT MASKING POLICIES on table memory.main.masks_only" in o and "ROW POLICIES" not in o
               for o in by_rep), by_rep
    # Nothing to exempt from on a table without policies; nothing exempted for ann.
    assert any("memory.main.plain" in o and "EXEMPT" not in o for o in by_rep), by_rep
    by_ann = allowed(session=ann.session_id)
    assert by_ann and all("EXEMPT" not in o for o in by_ann), by_ann


# ---- The store ----


def test_older_servers_never_read_them(launcher, pg_store):
    """A server before 2.14 refuses a store whose system_privileges table names a privilege it
    doesn't know, and skips one in storage_privileges: the exemptions go there, so such a
    server (another replica, or after a rollback) still opens the store and simply applies
    every policy."""
    uri = pg_store.new_database()
    make_world(launcher=launcher, store_uri=uri)
    with psycopg.connect(conninfo=uri, autocommit=True) as connection:
        found = connection.execute(
            query="SELECT 'system_privileges', privilege FROM hardcharger_rbac.system_privileges "
                  "WHERE privilege LIKE 'EXEMPT%' UNION ALL SELECT 'storage_privileges', privilege || '@' || location "
                  "FROM hardcharger_rbac.storage_privileges WHERE privilege LIKE 'EXEMPT%' ORDER BY 1, 2").fetchall()
    assert found == [("storage_privileges", "EXEMPT MASKING POLICIES@")] * 3 + \
        [("storage_privileges", "EXEMPT ROW POLICIES@")] * 3, found


def test_a_restart_on_either_store_keeps_them(launcher, store, tmp_path):
    database = tmp_path / "data.duckdb"
    uri = store.new_database()
    server = start_rbac_server(launcher=launcher, store_uri=uri, args=["--database", str(database)])
    admin = server.admin()
    for sql in SETUP:
        expect_allowed(client=admin, sql=sql)
    server.running.stop()
    restarted = start_rbac_server(launcher=launcher, store_uri=uri, args=["--database", str(database)])
    assert column(client=restarted.user(name="rep"), sql="SELECT ssn FROM emp") == REAL_SSN
    assert column(client=restarted.user(name="ann"), sql="SELECT ssn FROM emp") == ["XXX"]
