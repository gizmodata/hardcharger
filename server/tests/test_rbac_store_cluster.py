"""Which cluster an access-control store belongs to, the schema version it refuses, and
connection strings kept out of startup errors.

A store records the first cluster_id that opens it: grants name catalogs, so a store
shared by mistake would apply one cluster's grants to another cluster's catalogs of the
same names. Replicas (and restarts) of that cluster open it; another cluster is refused
unless rbac_store_shared. A store whose schema a later server migrated is refused too.
"""

import secrets

from conftest import free_port
from rbac import rbac_env, start_rbac_server


def users(admin) -> set[str]:
    return set(admin.run(sql="SELECT name FROM hardcharger_users()").column("name").to_pylist())


def test_a_store_belongs_to_one_cluster(launcher, store):
    uri = store.new_database()
    first, second = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    server = start_rbac_server(launcher=launcher, store_uri=uri, args=["--cluster-id", first])
    server.admin().run(sql="CREATE USER ann WITH PASSWORD 'ann-pw-12345'")
    server.running.stop()
    # A replica (or a restart) of the same cluster opens it.
    replica = start_rbac_server(launcher=launcher, store_uri=uri, args=["--cluster-id", first.upper()])
    replica.running.stop()
    # Another cluster is refused, saying whose store it is.
    other = launcher.start(env=rbac_env(store_uri=uri), args=["--cluster-id", second], expect_up=False)
    assert other.process.wait(timeout=60) == 1
    assert f"the access control store belongs to cluster {first}" in other.log(), other.log()
    assert "rbac_store_shared" in other.log()
    # Unless sharing is asked for.
    shared = start_rbac_server(launcher=launcher, store_uri=uri, args=["--cluster-id", second], env={"HARDCHARGER_RBAC_STORE_SHARED": "true"})
    assert "ann" in users(admin=shared.admin())
    assert "shares the access control store" in shared.log()
    shared.running.stop()
    # A server with no cluster_id can't tell: it opens the store, with a warning.
    anonymous = start_rbac_server(launcher=launcher, store_uri=uri)
    assert "this server has no cluster_id" in anonymous.log()


def test_a_store_schema_newer_than_the_server_is_refused(launcher, pg_store):
    import psycopg
    uri = pg_store.new_database()
    server = start_rbac_server(launcher=launcher, store_uri=uri)
    server.running.stop()
    with psycopg.connect(conninfo=uri, autocommit=True) as connection:
        connection.execute(query="UPDATE hardcharger_rbac.meta SET value = '2' WHERE key = 'schema_version'")
    newer = launcher.start(env=rbac_env(store_uri=uri), expect_up=False)
    assert newer.process.wait(timeout=60) == 1
    assert "the access control store's schema is version 2, newer than this server's (1)" in newer.log(), newer.log()
    with psycopg.connect(conninfo=uri, autocommit=True) as connection:
        connection.execute(query="UPDATE hardcharger_rbac.meta SET value = '1' WHERE key = 'schema_version'")
    start_rbac_server(launcher=launcher, store_uri=uri)  # the twin: the known version opens


def test_init_sql_attach_errors_are_redacted(launcher):
    secret = "init-secret-" + secrets.token_hex(nbytes=6)
    closed = free_port()  # nothing listens there
    for attach in (f"ATTACH 'postgresql://someone:{secret}@127.0.0.1:{closed}/db' AS x (TYPE postgres);",
                   f"ATTACH 'host=127.0.0.1 port={closed} dbname=db user=someone password={secret}' AS x (TYPE postgres);"):
        running = launcher.start(args=["--init-sql-commands", attach], expect_up=False)
        assert running.process.wait(timeout=60) == 1
        log = running.log()
        assert "init SQL failed" in log, log
        assert "Connection refused" in log or "connection to server" in log, log  # the reason survives
        assert secret not in log, log
