"""Delegation: owners and WITH GRANT OPTION / WITH ADMIN OPTION holders pass rights on;
revoking takes back everything granted through them. Each test gets its own server
and store."""

import pytest

from rbac import ChargewireError, expect_allowed, start_rbac_server


class World:
    def __init__(self, server):
        self.server = server
        self.admin = server.admin()
        self.sessions = {}

    def as_user(self, user: str):
        if user not in self.sessions:
            self.sessions[user] = self.server.user(name=user)
        return self.sessions[user]

    def error(self, user: str, sql: str) -> str:
        """'' when the statement ran as `user`, else why not."""
        client = self.admin if user == "admin" else self.as_user(user=user)
        try:
            client.run(sql=sql)
            return ""
        except ChargewireError as error:
            return error.detail

    def can(self, user: str, privilege: str, columns: list[str] | None = None, table: str = "orders") -> bool:
        sql = {"SELECT": f"SELECT {', '.join(columns or ['*'])} FROM {table}",
               "INSERT": f"INSERT INTO {table} DEFAULT VALUES",
               "DELETE": f"DELETE FROM {table}"}[privilege]
        detail = self.error(user=user, sql=sql)
        assert detail == "" or "Hardcharger blocked" in detail, f"{sql} failed, but not by access control: {detail}"
        return detail == ""


@pytest.fixture
def world(launcher, store):
    server = start_rbac_server(launcher=launcher, store_uri=store.new_database())
    world = World(server=server)
    for sql in ["CREATE TABLE orders (id INT, amount INT, card VARCHAR)",
                "CREATE TABLE other (id INT, amount INT, card VARCHAR)",
                "CREATE USER ann", "CREATE USER bob", "CREATE USER cat", "CREATE ROLE leads", "CREATE ROLE team",
                "CREATE ROLE others", "GRANT leads TO ann", "GRANT team TO bob", "GRANT others TO cat"]:
        expect_allowed(client=world.admin, sql=sql)
    return world


def own(world, user: str, table: str = "orders") -> None:
    expect_allowed(client=world.admin, sql=f"ALTER TABLE {table} OWNER TO {user}")


def test_an_owner_grants_on_what_it_owns_only(world):
    own(world=world, user="ann")
    assert world.error(user="ann", sql="GRANT SELECT ON orders TO team") == ""
    assert world.can(user="bob", privilege="SELECT")
    assert "may not grant SELECT on memory.main.other" in world.error(user="ann", sql="GRANT SELECT ON other TO team")
    assert not world.can(user="bob", privilege="SELECT", table="other")
    # Not a schema or catalog it does not own, either.
    assert "may not grant" in world.error(user="ann", sql="GRANT SELECT ON SCHEMA main TO team")
    assert "may not grant" in world.error(user="ann", sql="GRANT SELECT ON CATALOG memory TO team")


def test_grant_option_passes_on_exactly_what_it_covers(world):
    expect_allowed(client=world.admin, sql="GRANT SELECT (id, amount) ON orders TO leads WITH GRANT OPTION")
    expect_allowed(client=world.admin, sql="GRANT INSERT ON orders TO leads")  # no grant option
    assert world.error(user="ann", sql="GRANT SELECT (id) ON orders TO team") == ""
    assert world.can(user="bob", privilege="SELECT", columns=["id"])
    assert not world.can(user="bob", privilege="SELECT", columns=["amount"])
    # Not a column outside the grant, not the whole table, not another privilege.
    assert "may not grant" in world.error(user="ann", sql="GRANT SELECT (card) ON orders TO team")
    assert "may not grant" in world.error(user="ann", sql="GRANT SELECT ON orders TO team")
    assert "may not grant INSERT" in world.error(user="ann", sql="GRANT INSERT ON orders TO team")
    assert not world.can(user="bob", privilege="SELECT", columns=["card"])
    assert not world.can(user="bob", privilege="INSERT")
    # Someone without the option cannot pass on what they were given.
    assert "may not grant" in world.error(user="bob", sql="GRANT SELECT (id) ON orders TO others")
    assert not world.can(user="cat", privilege="SELECT", columns=["id"])


def test_revoke_cascades_through_what_was_granted_with_it(world):
    expect_allowed(client=world.admin, sql="GRANT SELECT ON orders TO leads WITH GRANT OPTION")
    assert world.error(user="ann", sql="GRANT SELECT ON orders TO team WITH GRANT OPTION") == ""
    assert world.error(user="bob", sql="GRANT SELECT ON orders TO others") == ""
    assert world.can(user="cat", privilege="SELECT")
    # RESTRICT refuses while others depend on it, and changes nothing.
    assert "other grants depend on this one" in world.error(user="admin",
                                                            sql="REVOKE SELECT ON orders FROM leads RESTRICT")
    assert world.can(user="cat", privilege="SELECT")
    expect_allowed(client=world.admin, sql="REVOKE SELECT ON orders FROM leads")
    assert not world.can(user="ann", privilege="SELECT")
    assert not world.can(user="bob", privilege="SELECT"), "a grant made through a revoked option survived"
    assert not world.can(user="cat", privilege="SELECT"), "a second-hand grant survived"


def test_revoking_only_the_grant_option_keeps_the_privilege(world):
    expect_allowed(client=world.admin, sql="GRANT SELECT ON orders TO leads WITH GRANT OPTION")
    assert world.error(user="ann", sql="GRANT SELECT ON orders TO team") == ""
    expect_allowed(client=world.admin, sql="REVOKE GRANT OPTION FOR SELECT ON orders FROM leads")
    assert world.can(user="ann", privilege="SELECT")
    assert not world.can(user="bob", privilege="SELECT")
    assert "may not grant" in world.error(user="ann", sql="GRANT SELECT ON orders TO team")


def test_grants_made_through_each_other_do_not_keep_each_other_alive(world):
    # dan owns the table and gives leads the option. ann (leads) passes it to team, and
    # bob (team) passes it back to leads: two grants that each justify the other. Once
    # dan's grant goes, neither has a root and both must fall.
    expect_allowed(client=world.admin, sql="CREATE USER dan")
    own(world=world, user="dan")
    assert world.error(user="dan", sql="GRANT SELECT ON orders TO leads WITH GRANT OPTION") == ""
    assert world.error(user="ann", sql="GRANT SELECT ON orders TO team WITH GRANT OPTION") == ""
    assert world.error(user="bob", sql="GRANT SELECT ON orders TO leads WITH GRANT OPTION") == ""
    assert world.error(user="dan", sql="REVOKE SELECT ON orders FROM leads") == ""  # only dan's own record
    assert not world.can(user="ann", privilege="SELECT"), "a cycle of grants kept itself alive"
    assert not world.can(user="bob", privilege="SELECT"), "a cycle of grants kept itself alive"


def test_only_the_grantor_or_an_administrator_revokes(world):
    expect_allowed(client=world.admin, sql="GRANT SELECT ON orders TO leads WITH GRANT OPTION")
    expect_allowed(client=world.admin, sql="GRANT SELECT ON orders TO team")
    # ann cannot take away the administrator's grant to team...
    assert "from another grantor" in world.error(user="ann", sql="REVOKE SELECT ON orders FROM team")
    assert world.can(user="bob", privilege="SELECT")
    # ...but can revoke her own, which leaves the administrator's in place.
    assert world.error(user="ann", sql="GRANT SELECT ON orders TO others") == ""
    assert world.error(user="ann", sql="REVOKE SELECT ON orders FROM others") == ""
    assert not world.can(user="cat", privilege="SELECT")


def test_admin_option_delegates_role_membership(world):
    assert "may not grant or revoke role team" in world.error(user="ann", sql="GRANT team TO cat")
    expect_allowed(client=world.admin, sql="GRANT team TO ann WITH ADMIN OPTION")
    expect_allowed(client=world.admin, sql="GRANT SELECT ON orders TO team")
    assert world.error(user="ann", sql="GRANT team TO cat") == ""
    assert world.can(user="cat", privilege="SELECT")
    # Not a role she cannot administer.
    assert "may not grant or revoke role others" in world.error(user="ann", sql="GRANT others TO bob")
    # Taking her admin option away takes away what she granted with it.
    expect_allowed(client=world.admin, sql="REVOKE ADMIN OPTION FOR team FROM ann")
    assert world.can(user="ann", privilege="SELECT")
    assert not world.can(user="cat", privilege="SELECT"), "a membership granted through a revoked admin option survived"


def test_an_owner_hands_an_object_only_to_itself_or_its_roles(world):
    own(world=world, user="ann")
    for sql in ["ALTER TABLE orders OWNER TO bob", "ALTER TABLE orders OWNER TO team",
                "ALTER TABLE orders OWNER TO admin"]:
        assert "may not change the owner" in world.error(user="ann", sql=sql), sql
    assert "may not change the owner" in world.error(user="bob", sql="ALTER TABLE orders OWNER TO team")
    assert world.error(user="ann", sql="ALTER TABLE orders OWNER TO leads") == ""
    assert world.can(user="ann", privilege="DELETE")  # through leads, the owner now


def test_an_owners_grants_survive_a_hand_over(world):
    own(world=world, user="ann")
    assert world.error(user="ann", sql="GRANT SELECT ON orders TO team") == ""
    expect_allowed(client=world.admin, sql="ALTER TABLE orders OWNER TO cat")
    assert world.can(user="bob", privilege="SELECT"), "the previous owner's grant fell with the ownership"
    assert not world.can(user="ann", privilege="SELECT")


def test_users_and_roles_stay_with_the_administrator(world):
    expect_allowed(client=world.admin, sql="GRANT team TO ann WITH ADMIN OPTION")
    for sql in ["CREATE USER x", "CREATE ROLE x", "DROP ROLE team", "ALTER USER bob DISABLE"]:
        assert "only administrators manage users and roles" in world.error(user="ann", sql=sql), sql


def test_system_privileges_and_their_admin_option(world):
    def reads_files(user: str) -> bool:
        detail = world.error(user=user, sql="SELECT * FROM read_csv('/nonexistent/x.csv')")
        return "READ FILESYSTEM" not in detail

    assert "may not grant READ FILESYSTEM" in world.error(user="ann", sql="GRANT READ FILESYSTEM TO team")
    expect_allowed(client=world.admin, sql="GRANT READ FILESYSTEM, CHECKPOINT TO leads WITH ADMIN OPTION")
    assert reads_files(user="ann") and not reads_files(user="bob")
    assert world.error(user="ann", sql="GRANT READ FILESYSTEM TO team") == ""
    assert reads_files(user="bob")
    # Only what she holds with the option.
    assert "may not grant MANAGE CATALOGS" in world.error(user="ann", sql="GRANT MANAGE CATALOGS TO team")
    assert "may not grant" in world.error(user="bob", sql="GRANT READ FILESYSTEM TO others")
    # Revoking the administrator's grant takes ann's with it.
    expect_allowed(client=world.admin, sql="REVOKE READ FILESYSTEM FROM leads")
    assert not reads_files(user="ann")
    assert not reads_files(user="bob"), "a delegated system privilege survived"
    assert world.error(user="ann", sql="CHECKPOINT") == ""


@pytest.mark.parametrize(("sql", "why"), [
    ("GRANT READ FILESYSTEM ON orders TO team", "expected LOCATION"),
    ("GRANT CHECKPOINT ON LOCATION '/tmp' TO team", "no ON clause"),
    ("GRANT MANAGE CATALOGS TO ann", "granted to roles, not users"),
    ("REVOKE GRANT OPTION FOR CHECKPOINT FROM team", "access control"),
])
def test_system_privilege_grammar(world, sql, why):
    error = world.error(user="admin", sql=sql)
    assert error, f"{sql} was accepted"
    assert why in error, f"{sql} -> {error}"
    assert world.error(user="ann", sql="SHOW GRANTS") == ""
