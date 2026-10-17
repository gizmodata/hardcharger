"""SET ROLE: a session acts with one of its user's roles (and what it inherits) until
RESET ROLE."""

import pytest

from rbac import WarpError, expect_allowed, start_rbac_server


@pytest.fixture(scope="module")
def server(module_launcher, store, tmp_path_factory):
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               args=["--database", str(tmp_path_factory.mktemp("db") / "rbac_set_role.duckdb")])
    admin = server.admin()
    for sql in [
        "CREATE TABLE t_a AS SELECT 'a' AS v", "CREATE TABLE t_b AS SELECT 'b' AS v",
        "CREATE TABLE t_pub AS SELECT 'pub' AS v", "CREATE TABLE t_rows AS SELECT * FROM (VALUES (1), (2)) t(id)",
        "CREATE ROLE ra", "CREATE ROLE rb", "CREATE ROLE rc", "CREATE ROLE spare",
        "GRANT SELECT ON t_a TO ra", "GRANT SELECT ON t_b TO rb", "GRANT ra TO rc",
        "GRANT SELECT ON t_pub TO PUBLIC", "GRANT SELECT ON t_rows TO ra, rb",
        "CREATE ROW POLICY only_rb ON t_rows USING (is_role_active('rb'))",
        "CREATE USER sam", "GRANT ra, rb, rc TO sam",
        "CREATE TENANT acme", "CREATE USER acme/ann", "CREATE ROLE acme/r", "GRANT acme/r TO acme/ann",
    ]:
        expect_allowed(client=admin, sql=sql)
    server.admin_client = admin
    return server


@pytest.fixture
def admin(server):
    return server.admin_client


@pytest.fixture
def sam(server):
    return server.user(name="sam")


def error_of(client, sql: str) -> str:
    """'' when the statement ran, else why not."""
    try:
        client.run(sql=sql)
        return ""
    except WarpError as error:
        return error.detail


def reads(client, table: str) -> None:
    assert error_of(client=client, sql=f"SELECT * FROM {table}") == "", f"could not read {table}"


def refused(client, table: str) -> None:
    error = error_of(client=client, sql=f"SELECT * FROM {table}")
    assert "no SELECT privilege on table" in error, f"{table} -> {error or 'READ ALLOWED'}"


def test_set_role_narrows_and_reset_role_restores(server, sam):
    reads(client=sam, table="t_a")
    reads(client=sam, table="t_b")
    assert error_of(client=sam, sql="SET ROLE ra") == ""
    reads(client=sam, table="t_a")
    refused(client=sam, table="t_b")
    # The refusal says the session narrowed itself.
    assert "acts with role ra only (SET ROLE); RESET ROLE" in error_of(client=sam, sql="SELECT * FROM t_b")
    reads(client=sam, table="t_pub")  # PUBLIC still applies
    # SHOW GRANTS shows what the session acts with.
    objects = set(sam.run(sql="SHOW GRANTS").column("object").to_pylist())
    assert "rbac_set_role.main.t_a" in objects and "rbac_set_role.main.t_b" not in objects, objects
    # Another session of the same user is not narrowed.
    reads(client=server.user(name="sam"), table="t_b")
    for back in ["RESET ROLE", "SET ROLE ALL", "SET ROLE NONE"]:
        assert error_of(client=sam, sql="SET ROLE ra") == ""
        refused(client=sam, table="t_b")
        assert error_of(client=sam, sql=back) == "", back
        reads(client=sam, table="t_b")


def test_an_active_role_brings_what_it_inherits(sam):
    assert error_of(client=sam, sql="SET ROLE rc") == ""
    reads(client=sam, table="t_a")  # rc holds ra
    refused(client=sam, table="t_b")


def test_only_a_role_the_user_holds(admin, sam):
    assert "does not hold role spare" in error_of(client=sam, sql="SET ROLE spare")
    # A role that does not exist reads the same.
    assert "does not hold role no_such_role" in error_of(client=sam, sql="SET ROLE no_such_role")
    # A refused SET ROLE changes nothing.
    reads(client=sam, table="t_b")
    # The administrator has nothing to narrow to.
    assert "the administrator holds every right" in error_of(client=admin, sql="SET ROLE ra")


def test_revoking_the_active_role_leaves_nothing_until_another_is_set(admin, sam):
    for sql in ["CREATE ROLE temp_role", "GRANT SELECT ON t_a TO temp_role", "GRANT temp_role TO sam"]:
        expect_allowed(client=admin, sql=sql)
    assert error_of(client=sam, sql="SET ROLE temp_role") == ""
    reads(client=sam, table="t_a")
    expect_allowed(client=admin, sql="REVOKE temp_role FROM sam")
    assert "no longer holds role temp_role" in error_of(client=sam, sql="SELECT * FROM t_pub")
    assert error_of(client=sam, sql="RESET ROLE") == ""
    reads(client=sam, table="t_a")
    expect_allowed(client=admin, sql="DROP ROLE temp_role")


def test_prepared_statements_follow_the_active_role(sam):
    read_b = sam.prepare(sql="SELECT * FROM t_b")
    read_a = sam.prepare(sql="SELECT * FROM t_a")
    sam.execute_prepared(prepared_id=read_b)
    assert error_of(client=sam, sql="SET ROLE ra") == ""
    with pytest.raises(WarpError) as raised:
        sam.execute_prepared(prepared_id=read_b)
    assert "no SELECT privilege on table" in raised.value.detail, "a statement prepared before SET ROLE kept the wider rights"
    sam.execute_prepared(prepared_id=read_a)  # positive twin
    assert error_of(client=sam, sql="RESET ROLE") == ""
    sam.execute_prepared(prepared_id=read_b)


def test_policies_see_the_active_role(sam):
    assert sam.run(sql="SELECT count(*) AS n FROM t_rows").column("n").to_pylist() == [2]
    assert error_of(client=sam, sql="SET ROLE ra") == ""
    assert sam.run(sql="SELECT count(*) AS n FROM t_rows").column("n").to_pylist() == [0], "is_role_active('rb') was true"
    assert error_of(client=sam, sql="SET ROLE rb") == ""
    assert sam.run(sql="SELECT count(*) AS n FROM t_rows").column("n").to_pylist() == [2]


def test_a_tenants_user_names_its_roles_without_the_tenant(server):
    ann = server.user(name="acme/ann")
    assert error_of(client=ann, sql="SET ROLE r") == ""
    assert error_of(client=ann, sql="SET ROLE acme/r") == ""
    assert "does not hold role ra" in error_of(client=ann, sql="SET ROLE ra")  # a server-wide role it does not hold
