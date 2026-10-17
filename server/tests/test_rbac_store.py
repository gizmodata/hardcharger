"""The PostgreSQL grant store: replicas writing at once, a restart, and connection
errors that never echo the connection string."""

import subprocess
import threading
import urllib.parse

import pytest

from rbac import WarpError, expect_allowed, rbac_env, start_rbac_server


def test_concurrent_writers_on_two_replicas_serialize(launcher, store):
    uri = store.new_database()
    first, second = (start_rbac_server(launcher=launcher, store_uri=uri) for _ in range(2))
    failures: list[str] = []

    def writer(server, prefix: str) -> None:
        admin = server.admin()
        for i in range(15):
            try:
                admin.run(sql=f"CREATE ROLE {prefix}{i}")
            except WarpError as error:
                failures.append(f"{prefix}{i}: {error}")

    threads = [threading.Thread(target=writer, args=(first, "a_role_")),
               threading.Thread(target=writer, args=(second, "b_role_"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    # Every role exists once: each is refused a second time, on either replica.
    admin = first.admin()
    for prefix in ("a_role_", "b_role_"):
        for i in range(15):
            with pytest.raises(WarpError) as raised:
                admin.run(sql=f"CREATE ROLE {prefix}{i}")
            assert "already exists" in raised.value.detail


def test_the_same_name_raced_from_two_replicas_is_created_once(launcher, store):
    uri = store.new_database()
    first, second = (start_rbac_server(launcher=launcher, store_uri=uri) for _ in range(2))
    admins = [first.admin(), second.admin()]
    for round_ in range(10):
        name = f"raced_{round_}"
        outcomes: list[bool] = []

        def create(admin) -> None:
            try:
                admin.run(sql=f"CREATE ROLE {name}")
                outcomes.append(True)
            except WarpError as error:
                assert "already exists" in error.detail, str(error)
                outcomes.append(False)

        threads = [threading.Thread(target=create, args=(admin,)) for admin in admins]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(outcomes) == [False, True], (name, outcomes)


def test_data_survives_a_restart(launcher, store):
    uri = store.new_database()
    server = start_rbac_server(launcher=launcher, store_uri=uri)
    admin = server.admin()
    for sql in ["CREATE TABLE orders (id INT, amount INT)", "CREATE USER joe WITH PASSWORD 'pw'",
                'CREATE ROLE "Data Team"', 'GRANT SELECT (id) ON orders TO "Data Team"', 'GRANT "Data Team" TO joe']:
        expect_allowed(client=admin, sql=sql)
    server.running.stop()
    restarted = start_rbac_server(launcher=launcher, store_uri=uri)
    assert restarted.password_token(user="joe", password="pw")
    grants = restarted.admin().run(sql='SHOW GRANTS FOR ROLE "data team"').to_pylist()
    assert [(row["granted_to"], row["privilege"], row["columns"]) for row in grants] == [("Data Team", "SELECT", "id")]


def test_connection_errors_never_echo_the_connection_string(launcher, store):
    secret = "not-the-password-8c1f"
    parts = urllib.parse.urlsplit(store.new_database())
    wrong_password = urllib.parse.urlunsplit(parts._replace(
        netloc=f"{parts.username}:{secret}@{parts.hostname}:{parts.port}"))
    running = launcher.start(env=rbac_env(store_uri=wrong_password), expect_up=False)
    try:
        assert running.process.wait(timeout=60) == 1
    except subprocess.TimeoutExpired:
        pytest.fail(reason="the server started with a wrong store password: does the test PostgreSQL "
                           "check passwords (not trust authentication)?")
    log = running.log()
    assert secret not in log, log
    assert "could not connect to the PostgreSQL store" in log, log
    # The reason survives, so an operator can still tell what went wrong.
    assert "password authentication failed" in log, log
    # The same with a libpq key=value connection string.
    key_value = (f"host={parts.hostname} port={parts.port} user={parts.username} "
                 f"dbname={parts.path.lstrip('/')} password={secret}")
    running = launcher.start(env=rbac_env(store_uri=key_value), expect_up=False)
    assert running.process.wait(timeout=60) == 1
    assert secret not in running.log(), running.log()
    assert "could not connect to the PostgreSQL store" in running.log()


def test_the_store_lives_in_its_own_schema_and_no_session_can_reach_it(launcher, store):
    import psycopg
    uri = store.new_database()
    server = start_rbac_server(launcher=launcher, store_uri=uri)
    expect_allowed(client=server.admin(), sql="CREATE USER probe")
    with psycopg.connect(conninfo=uri) as connection:
        tables = {row[0] for row in connection.execute(
            query="SELECT table_name FROM information_schema.tables WHERE table_schema = 'hardcharger_rbac'")}
    assert {"users", "roles", "memberships", "object_grants", "owners", "meta"} <= tables, tables
    # The store is attached on a private DuckDB instance: the server's sessions, even
    # the administrator's, have no catalog for it.
    admin = server.admin()
    catalogs = {row["database_name"] for row in admin.run(sql="SELECT database_name FROM duckdb_databases()").to_pylist()}
    assert "rbac_store" not in catalogs, catalogs


def test_the_store_extension_installs_on_a_fresh_host(launcher, store, tmp_path):
    # A host that never ran DuckDB: the private store instance must find the published
    # postgres extension for this DuckDB's version string (a source build would ask
    # for a -dev version nobody publishes).
    home = tmp_path / "fresh-home"
    home.mkdir()
    server = start_rbac_server(launcher=launcher, store_uri=store.new_database(), env={"HOME": str(home)})
    expect_allowed(client=server.admin(), sql="CREATE ROLE installed")
    assert list(home.glob(".duckdb/extensions/v2.0.0-alpha*/*/postgres_scanner.duckdb_extension")), \
        list(home.rglob("*"))[:20]
