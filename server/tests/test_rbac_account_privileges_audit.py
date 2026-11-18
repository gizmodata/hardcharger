"""The account privileges in the access-control audit chain: every account change and every
refusal is a record naming who (user, id, session, active role), what (statement, target and its
store id, the privilege it needs, the membership it changes), on whose authority, which
statement of the session it was, and the account's state before and after, sanitized: never a
password, a hash, a token or an attribute's value, in the audit records or anywhere in the log."""

import secrets

import pytest

from rbac import expect_allowed, expect_blocked, start_rbac_server
from test_rbac_audit_chain import KEY, verify
from test_rbac_system_admin_audit import about, audit_records

ALL_FIVE = "CREATE USER, ALTER USER, DROP USER, CREATE ROLE, DROP ROLE"


@pytest.fixture
def server(launcher, store):
    return start_rbac_server(launcher=launcher, store_uri=store.new_database(),
                             env={"HARDCHARGER_LOG_FORMAT": "json", "HARDCHARGER_AUDIT_CHAIN_KEY": KEY})


def test_account_changes_and_refusals_are_recorded_without_secrets(server, tmp_path):
    secret = {name: f"{name}-{secrets.token_urlsafe(nbytes=12)}" for name in
              ("amy", "eve_first", "eve_second", "bob_attempt", "carl_reset", "short_lived")}
    admin = server.admin()
    for sql in ["CREATE TABLE ledger AS SELECT 1 AS id", "CREATE ROLE helpdesk", f"GRANT {ALL_FIVE} TO helpdesk",
                "CREATE ROLE analyst", "GRANT SELECT ON ledger TO analyst", "CREATE ROLE clerks",
                f"CREATE USER amy WITH PASSWORD '{secret['amy']}'", "GRANT helpdesk TO amy", "GRANT clerks TO amy",
                "CREATE USER bob", "GRANT analyst TO bob", "ALTER USER bob SET ATTRIBUTE region = 'eu-secret-value'",
                "CREATE USER carl"]:
        expect_allowed(client=admin, sql=sql)
    amy = server.user(name="amy", password=secret["amy"])
    expect_allowed(client=amy, sql="SET ROLE helpdesk")
    for sql in [f"CREATE USER eve WITH PASSWORD '{secret['eve_first']}'",
                f"ALTER USER eve WITH PASSWORD '{secret['eve_second']}'", "ALTER USER eve DISABLE",
                f"ALTER USER carl WITH PASSWORD '{secret['carl_reset']}'"]:
        expect_allowed(client=amy, sql=sql)
    expect_blocked(client=amy, sql=f"ALTER USER bob WITH PASSWORD '{secret['bob_attempt']}'",
                   why="user bob holds role analyst")
    expect_blocked(client=amy, sql="ALTER USER eve SET ATTRIBUTE region = 'eu-secret-value'",
                   why="only an administrator sets them")
    expect_blocked(client=amy, sql="GRANT CREATE USER TO clerks", why="only an administrator grants CREATE USER")
    expect_allowed(client=amy, sql="RESET ROLE")
    for sql in ["DROP USER eve", "CREATE ROLE night_shift", "DROP ROLE night_shift"]:
        expect_allowed(client=amy, sql=sql)
    expect_blocked(client=amy, sql="DROP ROLE analyst", why="holds privileges of its own")

    records = audit_records(server=server)
    ids = {name: about(records, statement="CREATE USER", target=f"user {name}")[0]["target_ids"]
           for name in ("amy", "bob", "carl", "eve")}
    analyst = about(records, statement="CREATE ROLE", target="role analyst")[0]["target_ids"]
    helpdesk = about(records, statement="CREATE ROLE", target="role helpdesk")[0]["target_ids"]
    night = about(records, statement="CREATE ROLE", target="role night_shift")[0]["target_ids"]
    # The administrator's grant of the privileges, with the role's holding before and after.
    granted = about(records, user="admin", statement="GRANT", target="role helpdesk")
    assert len(granted) == 1 and granted[0]["privilege"] == ALL_FIVE, granted
    assert granted[0]["authorized_by"] == "administrator"
    assert granted[0]["before"] == "; ".join(f"role helpdesk: {p}: no" for p in ALL_FIVE.split(", "))
    assert granted[0]["after"] == "; ".join(f"role helpdesk: {p}: yes" for p in ALL_FIVE.split(", "))

    # amy's session: every record, in order, each with its own statement id.
    mine = about(records, session_id=amy.session_id)
    statement_ids = [r.pop("statement_id") for r in mine]
    assert all(i.startswith("q_") for i in statement_ids) and len(set(statement_ids)) == len(statement_ids)
    # The statement id is the one the session's own statement log lines carry.
    lines = [r for r in server.running.log_records() if "audit_hash" not in r and r.get("session_id") == amy.session_id]
    assert set(statement_ids) <= {r.get("statement_id") for r in lines}
    who = {"user": "amy", "user_id": ids["amy"], "session_id": amy.session_id}
    done = who | {"message": "access-control statement", "status": "success"}
    refused = who | {"message": "access-control statement refused", "status": "rejected"}
    through = "privilege, through role helpdesk"
    as_desk = {"active_role": "helpdesk"}
    eve_was = "enabled; password login; roles: none"
    expected = [
        done | {"statement": "SET ROLE", "membership": "role helpdesk", "membership_ids": helpdesk},
        done | as_desk | {"statement": "CREATE USER", "target": "user eve", "target_ids": ids["eve"],
                          "privilege": "CREATE USER", "authorized_by": f"CREATE USER {through}", "before": "none",
                          "after": eve_was},
        done | as_desk | {"statement": "ALTER USER", "target": "user eve", "target_ids": ids["eve"],
                          "privilege": "ALTER USER", "change": "password", "authorized_by": f"ALTER USER {through}",
                          "before": eve_was, "after": "enabled; password login (new password); roles: none"},
        done | as_desk | {"statement": "ALTER USER", "target": "user eve", "target_ids": ids["eve"],
                          "privilege": "ALTER USER", "change": "disabled", "authorized_by": f"ALTER USER {through}",
                          "before": eve_was, "after": "disabled; password login; roles: none"},
        done | as_desk | {"statement": "ALTER USER", "target": "user carl", "target_ids": ids["carl"],
                          "privilege": "ALTER USER", "change": "password", "authorized_by": f"ALTER USER {through}",
                          "before": "enabled; no password login; roles: none",
                          "after": "enabled; password login; roles: none"},
        refused | as_desk | {"statement": "ALTER USER", "target": "user bob", "target_ids": ids["bob"],
                             "privilege": "ALTER USER", "change": "password",
                             "error": "Hardcharger access control: Hardcharger blocked ALTER USER: user bob holds role "
                                      "analyst, which user amy does not hold in this session; only an administrator "
                                      "changes it"},
        refused | as_desk | {"statement": "ALTER USER", "target": "user eve", "target_ids": ids["eve"],
                             "privilege": "ALTER USER", "change": "attribute region",
                             "error": "Hardcharger access control: Hardcharger blocked ALTER USER: session attributes "
                                      "decide what row and masking policies show, and only an administrator sets "
                                      "them (the ALTER USER privilege sets passwords and enables and disables users)"},
        refused | as_desk | {"statement": "GRANT", "target": "role clerks",
                             "target_ids": about(records, statement="CREATE ROLE", target="role clerks")[0]["target_ids"],
                             "privilege": "CREATE USER",
                             "error": "Hardcharger access control: Hardcharger blocked GRANT: only an administrator "
                                      "grants CREATE USER (an account privilege is never delegated)"},
        done | as_desk | {"statement": "SET ROLE"},
        done | {"statement": "DROP USER", "target": "user eve", "target_ids": ids["eve"], "privilege": "DROP USER",
                "authorized_by": f"DROP USER {through}", "before": "disabled; password login; roles: none",
                "after": "none"},
        done | {"statement": "CREATE ROLE", "target": "role night_shift", "target_ids": night,
                "privilege": "CREATE ROLE", "authorized_by": f"CREATE ROLE {through}", "before": "none",
                "after": "members: 0; inherits: none; privileges: none"},
        done | {"statement": "DROP ROLE", "target": "role night_shift", "target_ids": night,
                "privilege": "DROP ROLE", "authorized_by": f"DROP ROLE {through}",
                "before": "members: 0; inherits: none; privileges: none", "after": "none"},
        refused | {"statement": "DROP ROLE", "target": "role analyst", "target_ids": analyst, "privilege": "DROP ROLE",
                   "error": "Hardcharger access control: Hardcharger blocked DROP ROLE: role analyst holds privileges "
                            "of its own, and user amy does not hold the role in this session; only an administrator "
                            "drops it"},
    ]
    assert len(mine) == len(expected), [r.get("statement") for r in mine]
    for got, want in zip(mine, expected):
        assert got == want

    # The chain verifies, and nothing in the whole log is a credential or an attribute's value.
    log = server.log()
    code, report = verify(log_text=log, tmp_path=tmp_path)
    assert code == 0, report
    credentials = {**secret, "amy's access token": amy.token, "the administrator's access token": admin.token,
                   "the administrator's password": server.running.password,
                   "the server's secret key": server.running.secret_key, "the audit key": KEY,
                   "an attribute's value": "eu-secret-value", "a password hash": "pbkdf2-sha256$"}
    for what, value in credentials.items():
        assert value not in log, f"{what} is in the log"
