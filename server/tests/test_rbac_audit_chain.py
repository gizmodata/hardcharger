"""The access-control audit records' hash chain: a server's own log verifies, and every
kind of edit to it is reported by `hardcharger-server --verify-audit-log`."""

import subprocess
import threading

import pytest

from conftest import BINARY
from rbac import ChargewireError, expect_allowed, start_rbac_server

KEY = "audit-chain-test-key"


def verify(log_text: str, tmp_path, key: str | None = KEY, partial: bool = False) -> tuple[int, str]:
    path = tmp_path / "checked.log"
    path.write_text(log_text)
    args = [str(BINARY), "--verify-audit-log", str(path)] + (["--verify-audit-log-partial"] if partial else [])
    env = {"PATH": "/usr/bin"} | ({"HARDCHARGER_AUDIT_CHAIN_KEY": key} if key else {})
    out = subprocess.run(args=args, capture_output=True, text=True, env=env)
    return out.returncode, out.stdout


def write_some_records(server) -> None:
    """Every kind of access-control record: allowed, denied, refused and run statements."""
    admin = server.admin()
    for sql in ["CREATE TABLE IF NOT EXISTS ledger AS SELECT 1 AS id",
                "CREATE TABLE IF NOT EXISTS vault AS SELECT 1 AS id",
                "CREATE ROLE IF NOT EXISTS clerks", "GRANT SELECT ON ledger TO clerks",
                "CREATE USER IF NOT EXISTS cleo", "GRANT clerks TO cleo"]:
        expect_allowed(client=admin, sql=sql)
    cleo = server.user(name="cleo")
    expect_allowed(client=cleo, sql="SELECT * FROM ledger")  # allowed
    for sql in ["SELECT * FROM vault", "GRANT SELECT ON vault TO clerks"]:  # denied, refused
        with pytest.raises(ChargewireError):
            cleo.run(sql=sql)
    expect_allowed(client=admin, sql="GRANT SELECT ON vault TO clerks")  # an access-control statement


@pytest.fixture
def server(launcher, store):
    return start_rbac_server(launcher=launcher, store_uri=store.new_database(),
                             env={"HARDCHARGER_LOG_FORMAT": "json", "HARDCHARGER_AUDIT_CHAIN_KEY": KEY,
                                  "HARDCHARGER_PRINT_QUERIES": "false"})


def chained_lines(log: str) -> list[str]:
    return [line for line in log.splitlines() if '"audit_hash"' in line]


def test_the_servers_own_log_verifies(server, tmp_path):
    write_some_records(server=server)
    log = server.log()
    code, report = verify(log_text=log, tmp_path=tmp_path)
    assert code == 0, report
    assert "intact" in report and "NOT intact" not in report
    lines = chained_lines(log=log)
    assert len(lines) >= 6
    # Each kind of record is in the chain.
    for what in ['"status":"allowed"', '"status":"denied"', '"status":"rejected"', '"status":"success"',
                 "access-control audit chain started"]:
        assert any(what in line for line in lines), f"{what} is not in the chain"
    # Lines in another order are still the same chain.
    assert verify(log_text="\n".join(reversed(log.splitlines())), tmp_path=tmp_path)[0] == 0


def test_every_kind_of_edit_is_reported(server, tmp_path):
    write_some_records(server=server)
    lines = server.log().splitlines()
    chained = [i for i, line in enumerate(lines) if '"audit_hash"' in line]
    denied = next(i for i in chained if '"status":"denied"' in lines[i])

    def edited(index: int, before: str, after: str) -> str:
        copy = list(lines)
        assert before in copy[index], (before, copy[index])
        copy[index] = copy[index].replace(before, after, 1)
        return "\n".join(copy)

    def problems(text: str, **options) -> str:
        code, report = verify(log_text=text, tmp_path=tmp_path, **options)
        assert code == 1, report
        return report

    # A denial rewritten as allowed; the user's name changed.
    assert "was changed after it was written" in problems(edited(denied, '"status":"denied"', '"status":"allowed"'))
    assert "was changed after it was written" in problems(edited(denied, '"user":"cleo"', '"user":"carl"'))
    # A field removed from the record; a field left out of what the digest covers.
    assert "the field user is missing" in problems(edited(denied, ',"user":"cleo"', ""))
    assert "was changed after it was written" in problems(edited(denied, ",user,", ","))
    # A record removed.
    removed = problems("\n".join(line for i, line in enumerate(lines) if i != chained[2]))
    assert "is missing" in removed and "does not follow the record before it" in removed
    # A record repeated.
    assert "twice" in problems("\n".join(lines + [lines[chained[2]]]))
    # The first records cut off: reported, unless the log is said to be partial.
    cut = "\n".join(lines[chained[2]:])
    assert "its first records are missing" in problems(cut)
    assert verify(log_text=cut, tmp_path=tmp_path, partial=True)[0] == 0
    # The wrong key, or none.
    assert "was changed after it was written" in problems("\n".join(lines), key="another-key")
    assert "sealed with a key" in problems("\n".join(lines), key=None)
    # Positive twin: the unedited log.
    assert verify(log_text="\n".join(lines), tmp_path=tmp_path)[0] == 0


def test_a_chain_without_the_key_is_not_accepted_for_one(launcher, store, tmp_path):
    # Someone who rewrites the log without the key can only rebuild an unkeyed chain; a
    # check with the key refuses it.
    unkeyed = start_rbac_server(launcher=launcher, store_uri=store.new_database(),
                                env={"HARDCHARGER_LOG_FORMAT": "json", "HARDCHARGER_PRINT_QUERIES": "false"})
    write_some_records(server=unkeyed)
    assert verify(log_text=unkeyed.log(), tmp_path=tmp_path, key=None)[0] == 0
    code, report = verify(log_text=unkeyed.log(), tmp_path=tmp_path)
    assert code == 1 and "not sealed with the key" in report, report


def test_records_below_the_log_level_do_not_break_the_chain(launcher, store, tmp_path):
    quiet = start_rbac_server(launcher=launcher, store_uri=store.new_database(),
                              env={"HARDCHARGER_LOG_FORMAT": "json", "HARDCHARGER_AUDIT_CHAIN_KEY": KEY,
                                   "HARDCHARGER_LOG_LEVEL": "warning"})
    write_some_records(server=quiet)  # the INFO records are not written
    code, report = verify(log_text=quiet.log(), tmp_path=tmp_path, partial=True)
    assert code == 0, report


def test_concurrent_writers_keep_one_chain(server, tmp_path):
    write_some_records(server=server)
    clients = [server.user(name="cleo") for _ in range(8)]

    def work(client) -> None:
        for _ in range(25):
            client.run(sql="SELECT * FROM ledger")

    threads = [threading.Thread(target=work, args=(client,)) for client in clients]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    code, report = verify(log_text=server.log(), tmp_path=tmp_path)
    assert code == 0, report
    assert len(chained_lines(log=server.log())) >= 8 * 25


def test_verify_audit_log_reports_a_log_without_records(tmp_path):
    code, report = verify(log_text='{"message": "nothing to see"}\n', tmp_path=tmp_path)
    assert code == 1 and "no access-control audit records found" in report
