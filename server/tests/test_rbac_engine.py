"""Users, roles and grants as the store keeps them: names, PUBLIC, schema, catalog and
column grants, owners, disabled users, password sign-in, SHOW GRANTS. Each test gets its
own server and store."""

import pytest

from rbac import WarpError, expect_allowed, start_rbac_server

SCHEMA_SETUP = [
    "CREATE TABLE orders (id INT, amount INT, customer VARCHAR)",
    'CREATE TABLE users (id INT, email VARCHAR, "Signup TS" TIMESTAMP)',
    "CREATE VIEW daily AS SELECT 1 AS day, 2 AS total",
    "CREATE SCHEMA finance", "CREATE SCHEMA finance.reports",
    "CREATE TABLE finance.reports.q3 (x INT)",
]


class World:
    def __init__(self, server):
        self.server = server
        self.admin = server.admin()
        self.sessions = {}

    def session(self, user: str):
        if user not in self.sessions:
            self.sessions[user] = self.server.user(name=user)
        return self.sessions[user]

    def error(self, sql: str, user: str = "admin") -> str:
        client = self.admin if user == "admin" else self.session(user=user)
        try:
            client.run(sql=sql)
            return ""
        except WarpError as error:
            return error.detail

    def rows(self, sql: str, user: str = "admin") -> list[dict]:
        client = self.admin if user == "admin" else self.session(user=user)
        return expect_allowed(client=client, sql=sql).to_pylist()

    def can(self, user: str, privilege: str, table: str = "orders", columns: list[str] | None = None) -> bool:
        sql = {"SELECT": f"SELECT {', '.join(columns or ['*'])} FROM {table}",
               "INSERT": f"INSERT INTO {table} DEFAULT VALUES",
               "UPDATE": f"UPDATE {table} SET {(columns or ['id'])[0]} = NULL WHERE false",
               "DELETE": f"DELETE FROM {table}"}[privilege]
        detail = self.error(sql=sql, user=user)
        assert detail == "" or "Hardcharger blocked" in detail, f"{sql}: {detail}"
        return detail == ""


@pytest.fixture
def world(launcher, store):
    world = World(server=start_rbac_server(launcher=launcher, store_uri=store.new_database()))
    for sql in SCHEMA_SETUP:
        expect_allowed(client=world.admin, sql=sql)
    return world


def test_users_and_roles_share_one_namespace(world):
    expect_allowed(client=world.admin, sql="CREATE USER joe WITH PASSWORD 'pw1'")
    expect_allowed(client=world.admin, sql="CREATE ROLE analyst")
    assert "a user named JOE already exists" in world.error(sql="CREATE ROLE JOE")
    assert "a role named Analyst already exists" in world.error(sql="CREATE USER Analyst")
    assert "reserved" in world.error(sql="CREATE ROLE public")
    assert "built-in" in world.error(sql="DROP ROLE PUBLIC")
    # IF NOT EXISTS is a no-op, not an error.
    assert world.rows(sql="CREATE USER IF NOT EXISTS joe") == [{"result": "User joe already exists"}]
    assert world.error(sql="DROP USER nobody") == "Hardcharger access control: user nobody does not exist"
    assert world.error(sql="DROP USER IF EXISTS nobody") == ""
    # The administrator's own name is not for a store user.
    assert "administrator" in world.error(sql="CREATE USER admin")


def test_privileges_flow_through_roles(world):
    for sql in ["CREATE USER joe", "CREATE ROLE reader", "CREATE ROLE analyst", "GRANT SELECT ON orders TO reader"]:
        expect_allowed(client=world.admin, sql=sql)
    assert not world.can(user="joe", privilege="SELECT")  # default deny
    expect_allowed(client=world.admin, sql="GRANT reader TO analyst")  # analyst inherits reader
    expect_allowed(client=world.admin, sql="GRANT analyst TO joe")
    assert world.can(user="joe", privilege="SELECT")
    assert not world.can(user="joe", privilege="INSERT")
    assert not world.can(user="joe", privilege="SELECT", table="users")
    expect_allowed(client=world.admin, sql="REVOKE reader FROM analyst")
    assert not world.can(user="joe", privilege="SELECT")


def test_role_cycles_and_self_grants_are_refused(world):
    for sql in ["CREATE ROLE a", "CREATE ROLE b", "CREATE ROLE c", "GRANT a TO b", "GRANT b TO c"]:
        expect_allowed(client=world.admin, sql=sql)
    assert "cycle" in world.error(sql="GRANT c TO a")
    assert "itself" in world.error(sql="GRANT a TO a")
    assert "PUBLIC" in world.error(sql="GRANT PUBLIC TO a")


def test_privileges_go_to_roles_not_users(world):
    expect_allowed(client=world.admin, sql="CREATE USER joe")
    assert "granted to roles, not users" in world.error(sql="GRANT SELECT ON orders TO joe")
    assert "does not exist" in world.error(sql="GRANT SELECT ON orders TO nobody")
    assert "does not exist" in world.error(sql="GRANT SELECT ON missing_table TO PUBLIC")


def test_public_reaches_every_user(world):
    expect_allowed(client=world.admin, sql="CREATE USER joe")
    expect_allowed(client=world.admin, sql="GRANT SELECT ON orders TO PUBLIC")
    assert world.can(user="joe", privilege="SELECT")


def test_schema_and_catalog_grants_cover_everything_inside(world):
    for sql in ["CREATE USER joe", "CREATE ROLE r", "GRANT r TO joe", "GRANT SELECT ON SCHEMA memory.finance TO r"]:
        expect_allowed(client=world.admin, sql=sql)
    # A schema grant covers nested schemas.
    assert world.can(user="joe", privilege="SELECT", table="finance.reports.q3")
    assert not world.can(user="joe", privilege="SELECT")
    expect_allowed(client=world.admin, sql="GRANT INSERT ON CATALOG memory TO r")
    assert world.can(user="joe", privilege="INSERT")
    assert world.can(user="joe", privilege="INSERT", table="finance.reports.q3")
    assert not world.can(user="joe", privilege="DELETE")
    expect_allowed(client=world.admin, sql="GRANT ALL ON memory.main.users TO r")
    assert world.can(user="joe", privilege="DELETE", table="users")
    assert world.can(user="joe", privilege="UPDATE", table="users")
    expect_allowed(client=world.admin, sql="REVOKE ALL ON memory.main.users FROM r")
    assert not world.can(user="joe", privilege="DELETE", table="users")
    # Grants on a schema cover tables created later.
    expect_allowed(client=world.admin, sql="CREATE TABLE finance.later AS SELECT 1 AS x")
    assert world.can(user="joe", privilege="SELECT", table="finance.later")


def test_column_grants(world):
    for sql in ["CREATE USER joe", "CREATE ROLE r", "GRANT r TO joe", "GRANT SELECT (email) ON users TO r"]:
        expect_allowed(client=world.admin, sql=sql)
    assert world.can(user="joe", privilege="SELECT", table="users", columns=["email"])
    assert world.can(user="joe", privilege="SELECT", table="users", columns=["EMAIL"])  # case-insensitive
    assert not world.can(user="joe", privilege="SELECT", table="users", columns=["email", "id"])
    # Column grants add up.
    expect_allowed(client=world.admin, sql="GRANT SELECT (id) ON users TO r")
    assert world.can(user="joe", privilege="SELECT", table="users", columns=["email", "id"])
    # A column that does not exist is refused at GRANT time.
    assert "column password does not exist" in world.error(sql="GRANT SELECT (password) ON users TO r")
    # Revoking one column from a whole-table grant keeps the others.
    expect_allowed(client=world.admin, sql="GRANT SELECT ON orders TO r")
    expect_allowed(client=world.admin, sql="REVOKE SELECT (customer) ON orders FROM r")
    assert world.can(user="joe", privilege="SELECT", columns=["id", "amount"])
    assert not world.can(user="joe", privilege="SELECT", columns=["customer"])


def test_owners_hold_every_privilege_on_their_object(world):
    for sql in ["CREATE USER joe", "CREATE ROLE eng", "CREATE USER ann", "GRANT eng TO ann",
                "ALTER TABLE orders OWNER TO joe"]:
        expect_allowed(client=world.admin, sql=sql)
    assert world.can(user="joe", privilege="DELETE")
    assert not world.can(user="ann", privilege="SELECT")
    # Ownership by a role reaches its members; the old owner loses it.
    expect_allowed(client=world.admin, sql="ALTER TABLE orders OWNER TO ROLE eng")
    assert world.can(user="ann", privilege="SELECT")
    assert not world.can(user="joe", privilege="SELECT")
    assert "owns" in world.error(sql="DROP ROLE eng")
    assert "is a table" in world.error(sql="ALTER VIEW orders OWNER TO joe")


def test_disabled_and_dropped_users_lose_everything(world):
    for sql in ["CREATE USER joe WITH PASSWORD 'pw'", "GRANT SELECT ON orders TO PUBLIC"]:
        expect_allowed(client=world.admin, sql=sql)
    joe = world.server.user(name="joe", password="pw")
    expect_allowed(client=joe, sql="SELECT * FROM orders")
    expect_allowed(client=world.admin, sql="ALTER USER joe DISABLE")
    # The live session ends, and the token and the password stop working.
    with pytest.raises(WarpError) as ended:
        joe.run(sql="SELECT 1")
    # The session's own token is refused first, saying why (Warp SQL §4.2).
    assert ended.value.code == "invalid_token" and ended.value.reason == "principal_dropped", ended.value
    assert ended.value.detail == "Hardcharger access control: user joe is disabled", ended.value
    with pytest.raises(WarpError) as refused:
        world.server.password_token(user="joe", password="pw")
    assert refused.value.code == "invalid_credentials"
    expect_allowed(client=world.admin, sql="ALTER USER joe ENABLE")
    assert world.server.password_token(user="joe", password="pw")
    expect_allowed(client=world.admin, sql="DROP USER joe")
    with pytest.raises(WarpError) as dropped:
        world.server.password_token(user="joe", password="pw")
    assert dropped.value.code == "invalid_credentials"


def test_password_sign_in_failures_look_alike(world):
    expect_allowed(client=world.admin, sql="CREATE USER joe WITH PASSWORD 'correct horse'")
    expect_allowed(client=world.admin, sql="CREATE USER token_only")
    # Names are case-insensitive, and the token carries the store's spelling.
    joe = world.server.user(name="JOE", password="correct horse")
    assert joe.run(sql="SHOW GRANTS").num_rows == 0
    answers = set()
    for user, password in [("joe", "Correct horse"), ("nobody", "x"), ("token_only", ""), ("token_only", "x")]:
        with pytest.raises(WarpError) as refused:
            world.server.password_token(user=user, password=password)
        answers.add((refused.value.status, refused.value.code, refused.value.detail))
    assert answers == {(401, "invalid_credentials", "the user name or password is not correct")}, answers
    # A password change takes effect at once.
    expect_allowed(client=world.admin, sql="ALTER USER joe WITH PASSWORD 'battery staple'")
    with pytest.raises(WarpError):
        world.server.password_token(user="joe", password="correct horse")
    assert world.server.password_token(user="joe", password="battery staple")


def test_passwords_are_stored_hashed(world, store):
    import psycopg
    expect_allowed(client=world.admin, sql="CREATE USER joe WITH PASSWORD 'correct horse'")
    with psycopg.connect(conninfo=world.server.store_uri) as connection:
        stored = connection.execute(query="SELECT password_hash FROM hardcharger_rbac.users WHERE name = 'joe'").fetchone()[0]
    assert "correct horse" not in stored
    assert stored.startswith("pbkdf2-sha256$")


def test_only_administrators_change_access_control(world):
    for sql in ["CREATE USER joe", "CREATE ROLE analyst", "GRANT analyst TO joe"]:
        expect_allowed(client=world.admin, sql=sql)
    for sql, why in [("CREATE ROLE sneaky", "only administrators manage users and roles"),
                     ("DROP USER joe", "only administrators manage users and roles"),
                     ("ALTER USER joe WITH PASSWORD 'x'", "only administrators manage users and roles"),
                     ("GRANT analyst TO joe", "may not grant or revoke role analyst"),
                     ("REVOKE analyst FROM joe", "may not grant or revoke role analyst"),
                     ("GRANT SELECT ON orders TO analyst", "may not grant SELECT on memory.main.orders"),
                     ("ALTER TABLE orders OWNER TO joe", "may not change the owner")]:
        error = world.error(sql=sql, user="joe")
        assert why in error, f"{sql} -> {error}"
        assert "Hardcharger blocked" in error, error
    # ...and none of them changed anything.
    assert "sneaky" not in str(world.rows(sql="SHOW GRANTS FOR ROLE PUBLIC"))
    assert "does not exist" in world.error(sql="DROP ROLE sneaky")


def test_show_grants(world):
    for sql in ["CREATE USER joe", "CREATE USER ann", "CREATE ROLE analyst", "GRANT analyst TO joe",
                "GRANT SELECT (email) ON users TO analyst"]:
        expect_allowed(client=world.admin, sql=sql)
    mine = world.rows(sql="SHOW GRANTS", user="joe")
    assert any(row["privilege"] == "SELECT" and row["object"] == "memory.main.users" and row["columns"] == "email"
               for row in mine), mine
    # A user may look at their own grants and roles they hold, nobody else's.
    assert world.error(sql="SHOW GRANTS FOR ROLE analyst", user="joe") == ""
    assert "blocked SHOW GRANTS for another user" in world.error(sql="SHOW GRANTS FOR USER ann", user="joe")
    expect_allowed(client=world.admin, sql="CREATE ROLE secret_role")
    assert "which you do not hold" in world.error(sql="SHOW GRANTS FOR secret_role", user="joe")
    # The administrator sees anyone's.
    assert world.rows(sql="SHOW GRANTS FOR ann") == []


def test_names_are_case_insensitive_but_keep_their_spelling(world):
    expect_allowed(client=world.admin, sql='CREATE ROLE "Data Team"')
    expect_allowed(client=world.admin, sql='GRANT SELECT ON ORDERS TO "data team"')
    grants = world.rows(sql='SHOW GRANTS FOR ROLE "DATA TEAM"')
    assert [row["granted_to"] for row in grants] == ["Data Team"]
    # Granting the same thing twice does not duplicate it.
    expect_allowed(client=world.admin, sql='GRANT SELECT ON orders TO "Data Team"')
    assert len(world.rows(sql='SHOW GRANTS FOR ROLE "Data Team"')) == 1


def test_a_failed_statement_changes_nothing(world):
    expect_allowed(client=world.admin, sql="CREATE ROLE a")
    assert "already exists" in world.error(sql="CREATE ROLE a")
    assert "already exists" in world.error(sql="CREATE ROLE a")
    expect_allowed(client=world.admin, sql="DROP ROLE a")
    assert "does not exist" in world.error(sql="DROP ROLE a")
