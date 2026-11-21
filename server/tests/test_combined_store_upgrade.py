"""Open populated stores from released binaries with RBAC and instrumentation enabled together."""
import gzip
import hashlib
import json
import pathlib
import time

import psycopg
import pytest

from rbac import ChargewireError, rbac_env, start_rbac_server
from test_rbac_system_admin_upgrade import wait_for
from test_rbac_token_provisioning import ISSUER, exchange, idp, idp_env

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "combined_upgrade"
PASSWORD = "reader-upgrade-fixture"
VERSION = "SELECT value FROM {} WHERE key = 'schema_version'"


def restore(files, kind, tmp_path, request):
    args = ["--enable-instrumentation"]
    if kind == "postgres":
        uri = request.getfixturevalue("pg_store").new_database()
        with psycopg.connect(conninfo=uri) as connection:
            connection.execute(query=gzip.decompress((files / "postgres.sql.gz").read_bytes()).decode())
        args.extend(["--init-sql-commands", f"ATTACH '{uri}' AS history (TYPE postgres)",
                     "--instrumentation-catalog", "history", "--instrumentation-schema", "instr"])
        return uri, args, "history.instr"

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    rbac, history = private / "rbac.db", tmp_path / "history.db"
    for file, fixture in [(rbac, "rbac"), (history, "history")]:
        file.write_bytes(gzip.decompress((files / f"{fixture}.duckdb.gz").read_bytes()))
        file.chmod(mode=0o600)
    return f"duckdb:{rbac}", [*args, "--instrumentation-db-path", str(history)], "_hardcharger_instr.main"


def inspect_rbac(launcher, uri, statements):
    """Run SQL directly against a stopped disposable RBAC store, on either backend."""
    if uri.startswith("duckdb:"):
        plain = launcher.start()
        try:
            admin = plain.admin()
            admin.execute(sql=f"ATTACH '{uri.removeprefix('duckdb:')}' AS inspected")
            admin.execute(sql="USE inspected.hardcharger_rbac")
            results = []
            for sql in statements:
                if sql.startswith("SELECT "):
                    results.append(admin.rows(sql=sql))
                else:
                    admin.execute(sql=sql)
                    results.append([])
            return results
        finally:
            plain.stop()
    from psycopg.rows import dict_row
    with psycopg.connect(conninfo=uri, row_factory=dict_row) as connection:
        connection.execute(query="SET search_path TO hardcharger_rbac")
        results = []
        for sql in statements:
            cursor = connection.execute(query=sql)
            results.append(cursor.fetchall() if cursor.description else [])
        return results


@pytest.mark.parametrize("version", ["2.10.0", "2.13.4"])
@pytest.mark.parametrize("kind", ["duckdb", "postgres"])
@pytest.mark.parametrize("auto_provision", ["false", "true"])
def test_upgrade_with_rbac_and_instrumentation(launcher, request, tmp_path, version, kind, auto_provision, idp):
    files = FIXTURES / version
    old_session = json.loads((files / "sessions.json").read_text())[kind]
    uri, args, history = restore(files, kind, tmp_path, request)
    user_id = "SELECT user_id FROM users WHERE name = 'reader'"
    original_id, old_format = inspect_rbac(launcher, uri, [user_id, VERSION.format("meta")])
    assert old_format == [{"value": "1"}] and len(original_id) == 1
    env = idp_env(idp=idp, HARDCHARGER_TOKEN_AUTO_PROVISION_USERS=auto_provision)
    token = idp["token"](email="reader", sub="reader-id", role="user")
    sessions = []
    for startup in range(2):
        server = start_rbac_server(launcher=launcher, store_uri=uri, args=args, env=env)
        admin = server.admin()
        reader = server.user(name="reader", password=PASSWORD)
        assert admin.run(sql=VERSION.format(history + ".store_metadata")).to_pylist() == [{"value": "1.1"}]
        historical = admin.run(sql=f"SELECT hardcharger_version, username, auth_method, execution_status "
                                   f"FROM {history}.session_activity WHERE session_id = '{old_session}' "
                                   "AND sql_text = 'SELECT 17 AS released_probe'").to_pylist()
        assert historical == [{"hardcharger_version": version, "username": "reader",
                               "auth_method": "password", "execution_status": "success"}]
        admin.run(sql="CREATE TABLE ledger AS SELECT 18 AS amount")
        assert reader.run(sql="SELECT amount FROM ledger").to_pylist() == [{"amount": 18}]
        account = "SELECT has_password, has_sso_link, idp_issuer, idp_subject FROM hardcharger_users() WHERE name = 'reader'"
        methods = admin.run(sql=account).to_pylist()
        assert methods == [{"has_password": True, "has_sso_link": bool(startup),
                            "idp_issuer": ISSUER if startup else None, "idp_subject": "reader-id" if startup else None}]
        sso = exchange(server=server, subject_token=token)
        assert sso.run(sql="SELECT amount FROM ledger").to_pylist() == [{"amount": 18}]
        assert admin.run(sql=account).to_pylist() == [{"has_password": True, "has_sso_link": True,
                                                       "idp_issuer": ISSUER, "idp_subject": "reader-id"}]
        with pytest.raises(ChargewireError, match="identity does not match"):
            exchange(server=server, subject_token=idp["token"](email="reader", sub="other-reader", role="user"))
        if startup == 0:
            admin.run(sql="CREATE USER created_after_upgrade")
        else:
            assert admin.run(sql="SELECT name FROM hardcharger_users() WHERE name = 'created_after_upgrade'").num_rows == 1
        sessions.extend([(reader.session_id, "password"), (sso.session_id, "token")])
        for sid, method in sessions:
            count = (f"SELECT count(*) FROM {history}.execution_details WHERE session_id = '{sid}' "
                     f"AND sql_text = 'SELECT amount FROM ledger' AND auth_method = '{method}' AND status = 'success'")
            wait_for(condition=lambda: admin.run(sql=count).column(0)[0].as_py() == 1,
                     timeout=20, what="query history from this session")
        sso.close()
        reader.close()
        admin.close()
        server.running.stop()
        assert inspect_rbac(launcher, uri, [user_id, VERSION.format("meta")]) == [original_id, [{"value": "1.1"}]]


@pytest.mark.parametrize("kind", ["duckdb", "postgres"])
def test_preview_password_metadata_survives_upgrade(launcher, request, tmp_path, kind):
    uri, args, _ = restore(FIXTURES / "2.13.4", kind, tmp_path, request)
    [user] = inspect_rbac(launcher, uri, ["SELECT password_hash FROM users WHERE name = 'reader'"])[0]
    fingerprint = hashlib.sha256(user["password_hash"].encode()).hexdigest()[:32]
    now = time.time_ns() // 1_000_000
    inspect_rbac(launcher, uri, [
        # A partially applied migration, and a store without a version row.
        "DELETE FROM meta WHERE key = 'schema_version'",
        "ALTER TABLE users ADD COLUMN idp_subject VARCHAR",
        "CREATE TABLE password_changes (user_id VARCHAR PRIMARY KEY, changed_at_ms BIGINT NOT NULL)",
        "CREATE TABLE password_resets (user_id VARCHAR PRIMARY KEY, password_fingerprint VARCHAR NOT NULL, "
        "set_by VARCHAR NOT NULL, set_at_ms BIGINT NOT NULL)",
        f"INSERT INTO password_changes (user_id, changed_at_ms) SELECT user_id, {now - 1} FROM users WHERE name = 'reader'",
        f"INSERT INTO password_resets (user_id, password_fingerprint, set_by, set_at_ms) SELECT user_id, "
        f"'{fingerprint}', 'administrator', {now} FROM users WHERE name = 'reader'"])
    for _ in range(2):
        server = start_rbac_server(launcher=launcher, store_uri=uri, args=args)
        assert server.admin().run(sql="SELECT password_must_change FROM hardcharger_users() "
                                      "WHERE name = 'reader'").column(0)[0].as_py() is True
        server.running.stop()
        metadata, obsolete = inspect_rbac(launcher, uri, [
            "SELECT password_changed_at_ms, password_reset_fingerprint, password_reset_by, password_reset_at_ms "
            "FROM users WHERE name = 'reader'",
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'hardcharger_rbac' "
            "AND table_name IN ('password_changes', 'password_resets')"])
        assert metadata == [{"password_changed_at_ms": now - 1, "password_reset_fingerprint": fingerprint,
                             "password_reset_by": "administrator", "password_reset_at_ms": now}]
        assert obsolete == []


@pytest.mark.parametrize("table,label,bad", [("hardcharger_rbac.meta", "access control", "1.10"),
                                             ("instr.store_metadata", "instrumentation", "1.10"),
                                             ("instr.store_metadata", "instrumentation", "1.1x")])
def test_independent_store_format_checks(launcher, request, tmp_path, table, label, bad):
    uri, args, _ = restore(FIXTURES / "2.13.4", "postgres", tmp_path, request)
    with psycopg.connect(conninfo=uri) as connection:
        connection.execute(query="CREATE TABLE instr.store_metadata (key VARCHAR PRIMARY KEY, value VARCHAR)")
        connection.execute(query="INSERT INTO instr.store_metadata (key, value) VALUES ('schema_version', NULL)")
        connection.execute(query=f"UPDATE {table} SET value = %s WHERE key = 'schema_version'", params=[bad])
    running = launcher.start(env=rbac_env(store_uri=uri), args=args, expect_up=False)
    assert running.process.wait(timeout=30) != 0
    assert label in running.log() and bad in running.log()
    with psycopg.connect(conninfo=uri) as connection:
        assert connection.execute(query=VERSION.format(table)).fetchone() == (bad,)
        # The released RBAC table disallows SQL NULL. Its equivalent is the absent key.
        if label == "access control":
            connection.execute(query=f"DELETE FROM {table} WHERE key = 'schema_version'")
        else:
            connection.execute(query=f"UPDATE {table} SET value = NULL WHERE key = 'schema_version'")
    start_rbac_server(launcher=launcher, store_uri=uri, args=args)
    with psycopg.connect(conninfo=uri) as connection:
        for migrated in ("hardcharger_rbac.meta", "instr.store_metadata"):
            assert connection.execute(query=VERSION.format(migrated)).fetchone() == ("1.1",)


@pytest.mark.parametrize("component", ["rbac", "instrumentation"])
def test_failed_migration_keeps_format_retryable(launcher, request, tmp_path, component):
    uri, args, _ = restore(FIXTURES / "2.13.4", "postgres", tmp_path, request)
    with psycopg.connect(conninfo=uri) as conn:
        if component == "rbac":
            # The preview migration needs the timestamp before it can retire this table.
            conn.execute(query="CREATE TABLE hardcharger_rbac.password_changes (user_id VARCHAR PRIMARY KEY)")
        else:
            conn.execute(query="ALTER VIEW instr.execution_details RENAME TO saved_execution_details")
            conn.execute(query="CREATE TABLE instr.execution_details (id INT)")
    broken = launcher.start(env=rbac_env(store_uri=uri), args=args, expect_up=False)
    assert broken.process.wait(timeout=45) != 0
    with psycopg.connect(conninfo=uri) as conn:
        if component == "rbac":
            assert conn.execute(query=VERSION.format("hardcharger_rbac.meta")).fetchone() == ("1",)
            conn.execute(query="ALTER TABLE hardcharger_rbac.password_changes ADD COLUMN changed_at_ms BIGINT")
        else:
            assert conn.execute(query="SELECT to_regclass('instr.store_metadata')").fetchone() == (None,)
            conn.execute(query="DROP TABLE instr.execution_details")
            conn.execute(query="ALTER VIEW instr.saved_execution_details RENAME TO execution_details")
    start_rbac_server(launcher=launcher, store_uri=uri, args=args)
    with psycopg.connect(conninfo=uri) as conn:
        for table in ("hardcharger_rbac.meta", "instr.store_metadata"):
            assert conn.execute(query=VERSION.format(table)).fetchone() == ("1.1",)
