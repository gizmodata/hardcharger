"""SYSTEM ADMIN in the access-control audit chain: who holds it, every session that starts
and stops acting as one, every refusal, and everything done while acting as one, each
record naming the user (its name and its id in the store) and that it acted as System
Admin; never a password, a hash, a token or a secret."""

import json
import pathlib
import secrets

import pytest

from rbac import ChargewireError, expect_allowed, expect_blocked, start_rbac_server
from test_rbac_audit_chain import KEY, verify

ELEVATE = "SET ROLE SYSTEM ADMIN"
FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "rbac_store_2_13_0"
# Fields every record has for its own bookkeeping (the chain's, the log's), not compared.
BOOKKEEPING = ("time", "level", "kind", "instance_id", "instance_tag", "cluster_id")


@pytest.fixture
def server(launcher, store):
    return start_rbac_server(launcher=launcher, store_uri=store.new_database(),
                             env={"HARDCHARGER_LOG_FORMAT": "json", "HARDCHARGER_AUDIT_CHAIN_KEY": KEY})


def audit_records(server) -> list[dict]:
    """The chain's records, in order, without the chain's and the log's own fields."""
    records = [r for r in server.running.log_records() if "audit_hash" in r]
    records.sort(key=lambda r: int(r["audit_seq"]))
    return [{k: v for k, v in r.items() if not k.startswith("audit_") and k not in BOOKKEEPING} for r in records]


def about(records: list[dict], **fields) -> list[dict]:
    return [r for r in records if all(r.get(k) == v for k, v in fields.items())]


def test_what_a_system_admin_does_is_recorded_under_its_own_name(server, tmp_path):
    # Every credential of this test, to look for in the log afterwards.
    passwords = {name: f"{name}-{secrets.token_urlsafe(nbytes=9)}" for name in
                 ("alice", "carl_first", "carl_second", "s3_key", "s3_secret", "attach")}
    admin = server.admin()
    for sql in ["CREATE TABLE ledger AS SELECT 1 AS id", "CREATE ROLE clerks",
                f"CREATE USER alice WITH PASSWORD '{passwords['alice']}'", "CREATE USER bob",
                "GRANT SYSTEM ADMIN TO alice"]:
        expect_allowed(client=admin, sql=sql)
    bob = server.user(name="bob")
    expect_blocked(client=bob, sql=ELEVATE, why="user bob does not hold SYSTEM ADMIN")
    expect_blocked(client=bob, sql="GRANT SYSTEM ADMIN TO bob", why="grants and revokes SYSTEM ADMIN")

    alice = server.user(name="alice", password=passwords["alice"])
    expect_allowed(client=alice, sql=ELEVATE)
    for sql in [f"CREATE USER carl WITH PASSWORD '{passwords['carl_first']}'",
                f"ALTER USER carl WITH PASSWORD '{passwords['carl_second']}'",
                "ALTER USER carl SET ATTRIBUTE region = 'eu-secret-region'",
                "GRANT clerks TO carl, bob WITH ADMIN OPTION", "GRANT SELECT (id), INSERT ON ledger TO clerks",
                "GRANT READ OBJECT STORAGE ON LOCATION 's3://bucket/in/' TO clerks",
                "GRANT SYSTEM ADMIN TO bob", "REVOKE SYSTEM ADMIN FROM bob",
                "CREATE ROW POLICY mine ON ledger TO clerks USING (id > 0)"]:
        expect_allowed(client=alice, sql=sql)
    minted = expect_allowed(client=alice, sql="CREATE SCOPED TOKEN nightly FOR USER carl ALLOW SELECT") \
        .column("token").to_pylist()[0]
    for sql in ["SELECT * FROM ledger",
                f"CREATE SECRET s (TYPE s3, KEY_ID '{passwords['s3_key']}', SECRET '{passwords['s3_secret']}')",
                "ATTACH ':memory:' AS scratch", "SET GLOBAL hardcharger.query_timeout = 0",
                f"KILL SESSION '{bob.session_id}'"]:
        expect_allowed(client=alice, sql=sql)
    with pytest.raises(ChargewireError):  # a statement that fails as System Admin
        alice.run(sql=f"ATTACH 'postgresql://someone:{passwords['attach']}@127.0.0.1:1/db' AS x (TYPE postgres)")
    with pytest.raises(ChargewireError):
        alice.run(sql="DROP USER nobody_at_all")
    expect_allowed(client=alice, sql="RESET ROLE")
    expect_blocked(client=alice, sql="SELECT * FROM ledger", why="user alice has no SELECT privilege on table")

    records = audit_records(server=server)
    # The ids the store gave them, from the records of their creation.
    ids = {name: about(records, statement="CREATE USER", target=f"user {name}")[0]["target_ids"]
           for name in ("alice", "bob", "carl")}
    clerks = about(records, statement="CREATE ROLE", target="role clerks")[0]["target_ids"]
    assert len({*ids.values(), clerks}) == 4 and all(len(i) == 36 for i in ids.values()), ids
    admin_session = {"user": "admin", "session_id": admin.session_id}
    as_bob = {"user": "bob", "user_id": ids["bob"], "session_id": bob.session_id}
    as_alice = {"user": "alice", "user_id": ids["alice"], "session_id": alice.session_id}
    as_admin = as_alice | {"acting_as": "SYSTEM ADMIN"}

    # The grant, by the administrator.
    assert about(records, statement="GRANT SYSTEM ADMIN", user="admin") == [
        admin_session | {"message": "access-control statement", "statement": "GRANT SYSTEM ADMIN", "status": "success",
                         "target": "user alice", "target_ids": ids["alice"], "privilege": "SYSTEM ADMIN"}]
    # bob's refused attempts: to act as one, and to grant it to himself.
    assert about(records, user="bob", status="rejected") == [
        as_bob | {"message": "System Admin elevation refused", "status": "rejected", "statement": ELEVATE,
                  "error": "Hardcharger access control: Hardcharger blocked SET ROLE SYSTEM ADMIN: user bob does not "
                           "hold SYSTEM ADMIN", "privilege": "SYSTEM ADMIN"},
        as_bob | {"message": "access-control statement refused", "status": "rejected", "statement": "GRANT SYSTEM ADMIN",
                  "error": "Hardcharger access control: Hardcharger blocked GRANT SYSTEM ADMIN: only the administrator, "
                           "or a user acting as System Admin (SET ROLE SYSTEM ADMIN) grants and revokes SYSTEM ADMIN",
                  "target": "user bob", "target_ids": ids["bob"], "privilege": "SYSTEM ADMIN"}]

    # alice's session, from asking to RESET ROLE: every record, in order.
    mine = about(records, session_id=alice.session_id)
    done = {"message": "access-control statement", "status": "success"}
    ran = {"message": "statement run as System Admin", "status": "success"}
    expected = [
        as_admin | {"message": "System Admin elevation", "status": "success", "statement": ELEVATE,
                    "held_through": "a grant to the user"},
        as_admin | done | {"statement": "CREATE USER", "target": "user carl", "target_ids": ids["carl"]},
        as_admin | done | {"statement": "ALTER USER", "target": "user carl", "target_ids": ids["carl"],
                           "change": "password"},
        as_admin | done | {"statement": "ALTER USER", "target": "user carl", "target_ids": ids["carl"],
                           "change": "attribute region"},
        as_admin | done | {"statement": "GRANT", "target": "user carl, user bob",
                           "target_ids": f"{ids['carl']}, {ids['bob']}", "membership": "role clerks",
                           "membership_ids": clerks, "option": "WITH ADMIN OPTION"},
        as_admin | done | {"statement": "GRANT", "target": "role clerks", "target_ids": clerks,
                           "privilege": "SELECT (id), INSERT", "object": "TABLE ledger"},
        as_admin | done | {"statement": "GRANT", "target": "role clerks", "target_ids": clerks,
                           "privilege": "READ OBJECT STORAGE", "object": "LOCATION 's3://bucket/in/'"},
        as_admin | done | {"statement": "GRANT SYSTEM ADMIN", "target": "user bob", "target_ids": ids["bob"],
                           "privilege": "SYSTEM ADMIN"},
        as_admin | done | {"statement": "REVOKE SYSTEM ADMIN", "target": "user bob", "target_ids": ids["bob"],
                           "privilege": "SYSTEM ADMIN"},
        as_admin | done | {"statement": "CREATE ROW POLICY", "target": "role clerks", "target_ids": clerks,
                           "policy": "mine", "object": "TABLE ledger"},
        as_admin | done | {"statement": "CREATE SCOPED TOKEN", "target": "user carl", "target_ids": ids["carl"],
                           "token": "nightly", "privilege": "SELECT"},
        as_admin | ran | {"sql": "SELECT * FROM ledger"},
        as_admin | ran | {"sql": "[REDACTED: ...]", "administrative": "CREATE SECRET"},
        as_admin | ran | {"sql": "ATTACH ':memory:' AS scratch", "administrative": "ATTACH"},
        as_admin | {"message": "administrative command run as System Admin", "status": "success",
                    "statement": "SET GLOBAL hardcharger.query_timeout"},
        as_admin | {"message": "administrative command run as System Admin", "status": "success",
                    "statement": "KILL SESSION", "target_session": bob.session_id},
        as_admin | {"message": "statement run as System Admin", "status": "failed", "administrative": "ATTACH",
                    "sql": "ATTACH 'postgresql://someone:***@127.0.0.1:1/db' AS x (TYPE postgres)",
                    "error": "[REDACTED: error of a credential statement]"},
        as_admin | {"message": "access-control statement refused", "status": "rejected", "statement": "DROP USER",
                    "error": "Hardcharger access control: user nobody_at_all does not exist",
                    "target": "user nobody_at_all", "target_ids": "?"},
        as_alice | {"message": "System Admin elevation ended", "status": "ended", "reason": "RESET ROLE"},
        as_alice | done | {"statement": "SET ROLE"},
        as_alice | {"message": "access control refused a statement", "status": "denied", "active_role": "",
                    "sql": "SELECT * FROM ledger",
                    "error": "Permission Error: Hardcharger blocked this statement: user alice has no SELECT privilege "
                             "on table memory.main.ledger. Access control on this server lets user alice do only "
                             "what their roles are granted (see SHOW GRANTS)"},
    ]
    assert len(mine) == len(expected), [r["message"] + " " + r.get("statement", r.get("sql", "")) for r in mine]
    for got, want in zip(mine, expected):
        if want.get("sql") == "[REDACTED: ...]":  # a credential statement is redacted whole, whatever the wording
            assert got["sql"].startswith("[REDACTED: ") and got["sql"].endswith("]"), got
            want = want | {"sql": got["sql"]}
        assert got == want

    # The chain verifies, and nothing in the whole log (the audit records and every other
    # line) is a credential: no password, password hash, token or secret.
    log = server.log()
    code, report = verify(log_text=log, tmp_path=tmp_path)
    assert code == 0, report
    credentials = {**passwords, "the scoped token": minted, "alice's access token": alice.token,
                   "the administrator's access token": admin.token, "bob's access token": bob.token,
                   "the administrator's password": server.running.password,
                   "the server's secret key": server.running.secret_key, "the audit key": KEY,
                   "an attribute's value": "eu-secret-region", "a password hash": "pbkdf2-sha256$"}
    for what, value in credentials.items():
        assert value not in log, f"{what} is in the log"
    for part in minted.split("."):  # no piece of the signed token either
        assert part not in log, "a part of the scoped token is in the log"


@pytest.mark.parametrize(("who", "why"), [
    ("scoped", "this session signed in with scoped token reports, and a scoped token never acts as System Admin"),
    ("rules", "this session's sign-in token carries catalog_access rules, and a token limited to some catalogs never "
              "acts as System Admin"),
    ("read_only", "this is a read-only session (its connection asked for read_only), and a read-only session never "
                  "acts as System Admin"),
    ("readonly", "this session signed in readonly (its sign-in's role), and a readonly session never acts as System "
                 "Admin"),
    ("tenant", "user acme/tom belongs to a tenant, and a tenant's user never acts as System Admin"),
])
def test_a_refused_elevation_is_recorded_with_what_prevented_it(server, tmp_path, who, why):
    admin = server.admin()
    for sql in ["CREATE USER alice", "GRANT SYSTEM ADMIN TO alice", "CREATE TENANT acme", "CREATE USER acme/tom"]:
        expect_allowed(client=admin, sql=sql)
    if who == "scoped":
        token = expect_allowed(client=server.user(name="alice"), sql="CREATE SCOPED TOKEN reports ALLOW SELECT") \
            .column("token").to_pylist()[0]
        session = server.client(token=token)
    elif who == "rules":
        session = server.user(name="alice", catalog_access=[{"catalog": "*", "access": "write"}])
    elif who == "read_only":
        session = server.client(token=server.user(name="alice").token, options={"read_only": True})
    elif who == "readonly":
        session = server.user(name="alice", role="readonly")
    else:
        session = server.user(name="acme/tom")
    expect_blocked(client=session, sql=ELEVATE, why="Hardcharger blocked SET ROLE SYSTEM ADMIN: " + why)
    refusals = about(audit_records(server=server), message="System Admin elevation refused")
    assert len(refusals) == 1, refusals
    record = refusals[0]
    assert record["error"].endswith("Hardcharger blocked SET ROLE SYSTEM ADMIN: " + why), record
    assert (record["user"], record["session_id"], record["status"], record["statement"]) == \
        ("acme/tom" if who == "tenant" else "alice", session.session_id, "rejected", ELEVATE), record
    assert "acting_as" not in record and len(record["user_id"]) == 36, record
    assert about(audit_records(server=server), message="System Admin elevation") == []
    assert verify(log_text=server.log(), tmp_path=tmp_path)[0] == 0


ENDS = {
    "RESET ROLE": "RESET ROLE", "SET ROLE ALL": "RESET ROLE", "SET ROLE clerks": "SET ROLE clerks",
    "revoked": "SYSTEM ADMIN no longer held", "closed": "session closed", "disabled": "session ended: user disabled",
    "dropped": "session ended: user dropped", "read_only": "the session became read-only",
}


@pytest.mark.parametrize("how", list(ENDS))
def test_each_elevation_has_one_start_and_one_end(server, tmp_path, how):
    admin = server.admin()
    for sql in ["CREATE ROLE clerks", "CREATE USER alice", "GRANT clerks TO alice", "GRANT SYSTEM ADMIN TO alice"]:
        expect_allowed(client=admin, sql=sql)
    alice = server.user(name="alice")
    expect_allowed(client=alice, sql=ELEVATE)
    expect_allowed(client=alice, sql=ELEVATE)  # saying it again starts nothing new
    if how in ("RESET ROLE", "SET ROLE ALL", "SET ROLE clerks"):
        expect_allowed(client=alice, sql=how)
        expect_allowed(client=alice, sql="RESET ROLE")  # and ending it again ends nothing more
    elif how == "revoked":
        expect_allowed(client=admin, sql="REVOKE SYSTEM ADMIN FROM alice")
        expect_blocked(client=alice, sql="SELECT 1", why="no longer holds SYSTEM ADMIN")
        expect_allowed(client=alice, sql="RESET ROLE")
    elif how == "closed":
        alice.close()
    elif how == "disabled":
        expect_allowed(client=admin, sql="ALTER USER alice DISABLE")
    elif how == "dropped":
        expect_allowed(client=admin, sql="DROP USER alice")
    else:
        response = alice.http.patch(url=alice.url, headers=alice.headers(), json={"options": {"read_only": True}})
        assert response.status_code == 200, response.text
    server.running.stop()  # every session is closed: nothing more is written
    records = audit_records(server=server)
    starts = about(records, message="System Admin elevation")
    ends = about(records, message="System Admin elevation ended")
    assert len(starts) == 1 and len(ends) == 1, (starts, ends)
    assert ends[0]["reason"] == ENDS[how], ends
    for record in (starts[0], ends[0]):
        assert (record["user"], record["session_id"]) == ("alice", alice.session_id), record
        assert record["user_id"] == starts[0]["user_id"] and len(record["user_id"]) == 36
    assert records.index(starts[0]) < records.index(ends[0])
    assert verify(log_text=server.log(), tmp_path=tmp_path)[0] == 0


def test_a_role_named_like_it_is_never_mistaken_for_it(launcher, tmp_path):
    """A store an earlier version made may hold a role called "SYSTEM ADMIN": a session
    that makes it active acts with that role, and no record says it acted as System Admin."""
    from test_rbac_system_admin_upgrade import duckdb_store_of_2_13_0
    server = start_rbac_server(launcher=launcher, store_uri=duckdb_store_of_2_13_0(directory=tmp_path / "store"),
                               env={"HARDCHARGER_LOG_FORMAT": "json", "HARDCHARGER_AUDIT_CHAIN_KEY": KEY})
    expect_allowed(client=server.admin(), sql="CREATE TABLE vault AS SELECT 42 AS secret")
    cleo = server.user(name="cleo")
    expect_allowed(client=cleo, sql='SET ROLE "SYSTEM ADMIN"')
    assert expect_allowed(client=cleo, sql="SELECT HARDCHARGER_IS_SYSTEM_ADMIN() AS a, HARDCHARGER_ACTIVE_ROLE() AS r") \
        .to_pylist() == [{"a": False, "r": "SYSTEM ADMIN"}]
    expect_blocked(client=cleo, sql="SELECT * FROM vault",
                   why="user cleo has no SELECT privilege on table memory.main.vault. This session acts with role "
                       "SYSTEM ADMIN only (SET ROLE)")
    expect_blocked(client=cleo, sql="CREATE USER made_by_cleo", why="only administrators manage users and roles")
    expect_blocked(client=cleo, sql=ELEVATE, why="user cleo does not hold SYSTEM ADMIN")
    records = audit_records(server=server)
    assert about(records, user="cleo") != []
    assert [r for r in records if "acting_as" in r] == [], "a role's name was taken for the capability"
    assert about(records, message="System Admin elevation") == []
    denied = about(records, user="cleo", status="denied")
    assert denied and all(r["active_role"] == "SYSTEM ADMIN" for r in denied), denied


def test_a_chain_an_earlier_version_wrote_still_verifies(tmp_path):
    # The records gained fields; each record says which fields its digest covers, so a log
    # written before them checks as it did.
    log = (FIXTURES / "audit-chain.jsonl").read_text()
    assert log.count('"audit_hash"') >= 6 and '"user_id"' not in log
    code, report = verify(log_text=log, tmp_path=tmp_path)
    assert code == 0, report
    edited = log.replace('"status":"denied"', '"status":"allowed"', 1)
    assert edited != log
    code, report = verify(log_text=edited, tmp_path=tmp_path)
    assert code == 1 and "was changed after it was written" in report, report
    assert json.loads(log.splitlines()[0])["message"] == "access-control audit chain started"
