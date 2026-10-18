"""An external policy service (Open Policy Agent's data API), asked after Hardcharger's own
check: it can refuse what grants allow, never allow what they refuse, and no answer
means no."""

import http.server
import json
import threading
import time

import pytest

from conftest import TIMEOUT_SCALE, free_port
from rbac import WarpError, expect_allowed, start_rbac_server


class FakeOpa:
    """Plays OPA: POST /v1/data/hardcharger/allow with {"input": ...} -> {"result": bool}."""

    def __init__(self):
        self.mode = "policy"  # policy, error500, not_a_bool, slow
        self.statements = 0
        self.last_statement: dict = {}
        self.lock = threading.Lock()
        self.release = threading.Event()  # "slow" answers only once this is set
        # The latest requests (arrival, action, mode, seconds to answer): an
        # unexpected "no answer" then says whether the request arrived, and how it fared.
        self.requests: list[dict] = []
        opa = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet
                pass

            def do_POST(self):
                arrived = time.monotonic()
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                entry = {"at": arrived, "action": body.get("input", {}).get("action", "?"), "mode": opa.mode,
                         "took": None}
                with opa.lock:
                    opa.requests = [*opa.requests[-19:], entry]
                decision = opa.decide(path=self.path, input_=body.get("input", {}))
                entry["took"] = time.monotonic() - arrived
                if decision is None:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b"boom")
                    return
                payload = json.dumps(decision).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.port = free_port()
        # Bound and listening from here (before any server under test starts): a connect
        # made before serve_forever runs waits in the backlog, it is not refused.
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.port}/v1/data/hardcharger/allow"

    def decide(self, path: str, input_: dict):
        if path != "/v1/data/hardcharger/allow":
            return None
        with self.lock:
            if input_.get("action") == "statement":
                self.statements += 1
                self.last_statement = input_
        if self.mode == "error500":
            return None
        if self.mode == "not_a_bool":
            return {"result": "yes"}
        if self.mode == "slow":
            self.release.wait(timeout=120)  # far past any timeout, until the test lets go
            return {"result": True}
        allow = True
        user = input_.get("user", "")
        if input_.get("action") == "connect" and user == "mallory":
            allow = False
        for obj in input_.get("objects", []):
            if obj.get("name") == "payroll" and user != "hr_bot":
                allow = False
        return {"result": allow}

    def recent(self) -> str:
        now = time.monotonic()
        with self.lock:
            return "; ".join(f"{r['action']} in mode {r['mode']} {now - r['at']:.2f}s ago, " +
                             ("not answered yet" if r["took"] is None else f"answered in {r['took'] * 1000:.0f} ms")
                             for r in self.requests[-6:]) or "none arrived"

    def stop(self):
        self.server.shutdown()


@pytest.fixture(scope="module")
def opa():
    service = FakeOpa()
    yield service
    service.stop()


# The server's deadline for an answer. It was 800 ms, and a whole exchange with the
# healthy service sometimes took longer on a busy runner: the session's connect check (or
# its first statement's) got "no answer", and test_no_answer_means_no failed before its
# service had been told to misbehave (TSan once; Windows amd64 twice in one run). Only the
# "slow" case waits it out, so it can be generous; stretched on a sanitizer build.
OPA_TIMEOUT_S = 5 * TIMEOUT_SCALE


@pytest.fixture(scope="module")
def server(module_launcher, store, opa):
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               env={"HARDCHARGER_RBAC_OPA_URL": opa.url,
                                    "HARDCHARGER_RBAC_OPA_TIMEOUT_MS": str(int(OPA_TIMEOUT_S * 1000))})
    admin = server.admin()
    for sql in ["CREATE TABLE orders AS SELECT 1 AS id, 10 AS amount",
                "CREATE TABLE payroll AS SELECT 1 AS id, 5000 AS salary", "CREATE TABLE secrets AS SELECT 1 AS id",
                "CREATE ROLE reader", "GRANT SELECT ON orders TO reader", "GRANT SELECT ON payroll TO reader",
                "CREATE USER ann", "CREATE USER hr_bot", "CREATE USER mallory", "GRANT reader TO ann",
                "GRANT reader TO hr_bot", "GRANT reader TO mallory"]:
        expect_allowed(client=admin, sql=sql)
    server.admin_client = admin
    return server


@pytest.fixture(autouse=True)
def policy_mode(opa):
    opa.mode = "policy"
    opa.release.clear()
    yield
    opa.mode = "policy"
    opa.release.set()  # a "slow" answer still pending returns now


def error(client, sql: str) -> str:
    try:
        client.run(sql=sql)
        return ""
    except WarpError as raised:
        return raised.detail


def test_the_service_sees_what_an_allowed_statement_touches(server, opa):
    ann = server.user(name="ann")
    assert error(client=ann, sql="SELECT id FROM orders") == ""
    seen = opa.last_statement
    assert seen["user"] == "ann" and "reader" in seen["roles"], seen
    assert len(seen["objects"]) == 1, seen
    assert {key: seen["objects"][0][key] for key in ("name", "schema", "type", "privilege", "columns")} == \
        {"name": "orders", "schema": "main", "type": "table", "privilege": "SELECT", "columns": ["id"]}


def test_the_service_can_refuse_what_grants_allow(server):
    assert "the access policy service denied it" in error(client=server.user(name="ann"), sql="SELECT * FROM payroll")
    # Positive twin: the service allows hr_bot.
    assert error(client=server.user(name="hr_bot"), sql="SELECT * FROM payroll") == ""


def test_the_service_never_allows_what_grants_refuse(server, opa):
    ann = server.user(name="ann")
    before = opa.statements
    assert "no SELECT privilege on table" in error(client=ann, sql="SELECT * FROM secrets")
    assert opa.statements == before, "a statement the grants refused was sent to the service"


@pytest.mark.parametrize("mode", ["error500", "not_a_bool", "slow"])
def test_no_answer_means_no(server, opa, mode):
    started = time.monotonic()
    try:
        ann = server.user(name="ann")
    except WarpError as raised:
        raise AssertionError(f"a session was refused while the service was healthy, after "
                             f"{time.monotonic() - started:.2f}s: {raised}; the service's latest requests: "
                             f"{opa.recent()}") from raised
    assert error(client=ann, sql="SELECT * FROM orders") == ""
    opa.mode = mode
    started = time.monotonic()
    assert "could not get an answer from the access policy service" in error(client=ann, sql="SELECT * FROM orders")
    assert time.monotonic() - started < OPA_TIMEOUT_S + 1.7 * TIMEOUT_SCALE, "the timeout was not kept"
    opa.release.set()
    # The administrator is not held up by the service (a way in when it is down).
    assert error(client=server.admin_client, sql="SELECT * FROM orders") == ""
    opa.mode = "policy"
    assert error(client=ann, sql="SELECT * FROM orders") == ""


def test_the_service_can_refuse_a_session(server):
    with pytest.raises(WarpError) as raised:
        server.user(name="mallory")
    assert raised.value.code == "permission_denied"
    assert "the access policy service refused the session" in raised.value.detail
    assert error(client=server.user(name="ann"), sql="SELECT 1") == ""


def test_a_policy_service_needs_access_control(launcher, opa):
    running = launcher.start(env={"HARDCHARGER_RBAC_OPA_URL": opa.url}, expect_up=False)
    assert running.process.wait(timeout=20) == 2
    assert "rbac_opa_url needs rbac_store_uri" in running.log()
