"""The web UI's relay to other Hardcharger servers (src/web_relay.cc) on a server that serves
the UI: the contract at /ui/v1/app, registering remotes (plaintext, TLS verified against the
roots in SSL_CERT_FILE, TLS trusted by its certificate's fingerprint), and the remote's
Chargewire requests forwarded under /ui/v1/remotes/<id>/chargewire with the remote's token
in Chargewire-Relay-Authorization, streaming both ways. Every refusal asserts its code.

These need a build that embeds the UI (the relay lives under /ui); against one without it
they fail, never skip (deselect them with -m "not web_ui").
"""

import datetime
import ipaddress
import time

import httpx
import pyarrow as pa
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from chargewire_client import Client, Session
from conftest import TIMEOUT_SCALE, PERF_SCALE
from resuming_holder import Holder

pytestmark = pytest.mark.web_ui

NOW = datetime.datetime.now(tz=datetime.UTC)
SEED = "CREATE TABLE t AS SELECT range AS id, 'row ' || range AS label FROM range(5)"


def _write_pem(path, obj, private=False):
    if private:
        path.write_bytes(obj.private_bytes(encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8,
                                           encryption_algorithm=serialization.NoEncryption()))
    else:
        path.write_bytes(obj.public_bytes(encoding=serialization.Encoding.PEM))


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(oid=NameOID.COMMON_NAME, value=cn)])


def _builder(subject: str, issuer: str, public_key, ca: bool):
    builder = (x509.CertificateBuilder().subject_name(_name(subject)).issuer_name(_name(issuer)).public_key(public_key)
               .serial_number(x509.random_serial_number()).not_valid_before(NOW - datetime.timedelta(minutes=5))
               .not_valid_after(NOW + datetime.timedelta(days=1))
               .add_extension(extval=x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if not ca:
        builder = (builder.add_extension(extval=x509.SubjectAlternativeName(
            [x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")]), critical=False)
            .add_extension(extval=x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False))
    return builder


class Certificates:
    """A CA with a server certificate for 127.0.0.1 signed by it, and a self-signed one."""

    def __init__(self, directory):
        ca_key = ec.generate_private_key(curve=ec.SECP256R1())
        ca = _builder("relay-test-ca", "relay-test-ca", ca_key.public_key(), ca=True).sign(
            private_key=ca_key, algorithm=hashes.SHA256())
        self.ca_path = directory / "ca.pem"
        _write_pem(self.ca_path, ca)
        signed_key = ec.generate_private_key(curve=ec.SECP256R1())
        signed = _builder("relay-test-signed", "relay-test-ca", signed_key.public_key(), ca=False).sign(
            private_key=ca_key, algorithm=hashes.SHA256())
        self.signed = (directory / "signed.pem", directory / "signed.key")
        _write_pem(self.signed[0], signed)
        _write_pem(self.signed[1], signed_key, private=True)
        self_key = ec.generate_private_key(curve=ec.SECP256R1())
        selfsigned = _builder("relay-test-self", "relay-test-self", self_key.public_key(), ca=False).sign(
            private_key=self_key, algorithm=hashes.SHA256())
        self.selfsigned = (directory / "self.pem", directory / "self.key")
        _write_pem(self.selfsigned[0], selfsigned)
        _write_pem(self.selfsigned[1], self_key, private=True)
        self.selfsigned_fingerprint = ":".join(f"{b:02x}" for b in selfsigned.fingerprint(hashes.SHA256()))
        self.signed_fingerprint = ":".join(f"{b:02x}" for b in signed.fingerprint(hashes.SHA256()))


def _start_tls(launcher, cert, key, args=()):
    running = launcher.start(args=["--tls-cert", str(cert), "--tls-key", str(key), *args], expect_up=False)
    trust = httpx.Client(verify=False, timeout=5)
    deadline = time.monotonic() + 60 * TIMEOUT_SCALE
    while time.monotonic() < deadline:
        if running.process.poll() is not None:
            pytest.fail(reason=f"the TLS server exited: {running.log()}")
        try:
            if trust.get(url=f"https://127.0.0.1:{running.port}/chargewire").status_code == 200:
                running.base_url = f"https://127.0.0.1:{running.port}"
                return running
        except httpx.HTTPError:
            time.sleep(0.2)
    pytest.fail(reason=f"the TLS server did not come up: {running.log()}")


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    return Certificates(tmp_path_factory.mktemp("relay-pki"))


@pytest.fixture(scope="module")
def ui(module_launcher, certificates):
    """The server that serves the UI and relays; it trusts the test CA as a root."""
    return module_launcher.start(args=["--web-ui", "--log-format", "json"],
                                 env={"SSL_CERT_FILE": str(certificates.ca_path)})


@pytest.fixture(scope="module")
def plain(module_launcher):
    return module_launcher.start(args=["--init-sql-commands", SEED])


@pytest.fixture(scope="module")
def signed(module_launcher, certificates):
    return _start_tls(module_launcher, *certificates.signed, args=["--init-sql-commands", SEED])


@pytest.fixture(scope="module")
def selfsigned(module_launcher, certificates):
    return _start_tls(module_launcher, *certificates.selfsigned, args=["--init-sql-commands", SEED])


class Relay:
    """The relay as the UI uses it: this server's token, then remotes by id."""

    def __init__(self, ui, token: str | None = None, relay_session: str | None = None):
        self.ui = ui
        self.token = token
        self.relay_session = relay_session
        self.http = httpx.Client(base_url=ui.base_url, timeout=120)

    def headers(self, **extra) -> dict[str, str]:
        headers = dict(extra)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.relay_session:
            headers["Chargewire-Relay-Session"] = self.relay_session
        return headers

    def probe(self, **body) -> httpx.Response:
        return self.http.post(url="/ui/v1/remotes/probe", json=body, headers=self.headers())

    def add(self, **body) -> httpx.Response:
        response = self.http.post(url="/ui/v1/remotes", json=body, headers=self.headers())
        if response.status_code == 201 and not self.relay_session:
            self.relay_session = response.json()["relay_session"]
        return response

    def remotes(self) -> httpx.Response:
        return self.http.get(url="/ui/v1/remotes", headers=self.headers())

    def remove(self, remote_id: str) -> httpx.Response:
        return self.http.delete(url=f"/ui/v1/remotes/{remote_id}", headers=self.headers())

    def client(self, remote_id: str, remote_token: str | None) -> "RelayClient":
        return RelayClient(relay=self, remote_id=remote_id, remote_token=remote_token)

    def sign_in(self, remote_id: str, username: str, password: str) -> str:
        response = self.http.post(url=f"/ui/v1/remotes/{remote_id}/chargewire/v1/auth/token",
                                  json={"grant_type": "password", "username": username, "password": password},
                                  headers=self.headers())
        assert response.status_code == 200, response.text
        return response.json()["access_token"]


class RelayClient(Client):
    """chargewire_client.Client through the relay: its /chargewire/... paths resolve under the
    remote's relay path, this server's token stays in Authorization, the remote's goes in
    Chargewire-Relay-Authorization."""

    def __init__(self, relay: Relay, remote_id: str, remote_token: str | None):
        super().__init__(base_url=f"{relay.ui.base_url}/ui/v1/remotes/{remote_id}", token=remote_token or "")
        self.relay = relay

    def headers(self, **extra: str) -> dict[str, str]:
        headers = self.relay.headers(**extra)
        if self.token:
            headers["Chargewire-Relay-Authorization"] = f"Bearer {self.token}"
        return headers


def problem(response: httpx.Response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code, body
    return body


# ---- The contract ----


def test_the_contract_names_a_local_server_and_the_relay(ui, launcher):
    doc = httpx.get(url=f"{ui.base_url}/ui/v1/app", timeout=10).json()
    assert doc["local_server"] is True and doc["remote_servers"] is True
    assert doc["version"] == httpx.get(url=f"{ui.base_url}/chargewire", timeout=10).json()["server"]["version"].split(" ")[0]
    # Without the web UI there is no contract (and no relay) at all.
    without = launcher.start()
    assert httpx.get(url=f"{without.base_url}/ui/v1/app", timeout=10).status_code == 404
    assert httpx.post(url=f"{without.base_url}/ui/v1/remotes", json={"url": ui.base_url}, timeout=10).status_code == 404


def test_the_relay_needs_this_servers_sign_in(ui, plain):
    nobody = Relay(ui)
    problem(nobody.remotes(), 401, "relay_session_required")
    problem(nobody.add(url=plain.base_url), 401, "relay_session_required")
    problem(Relay(ui, token="not-a-token").remotes(), 401, "invalid_token")
    # The positive twin: signed in, the same calls work.
    signed_in = Relay(ui, token=ui.token())
    assert signed_in.remotes().status_code == 200
    assert signed_in.add(url=plain.base_url).status_code == 201


# ---- Plaintext remotes, and what the forwarding carries ----


def test_a_plaintext_remote_end_to_end(ui, plain):
    relay = Relay(ui, token=ui.token())
    probed = relay.probe(url=plain.base_url).json()
    assert probed["tls_mode"] == "plaintext" and probed["tls"] is None
    assert probed["server"]["name"] == "Hardcharger" and "password" in probed["auth"]["grant_types"]
    added = relay.add(url=plain.base_url, name="plain").json()
    assert added["name"] == "plain" and added["tls_mode"] == "plaintext" and added["signed_in"] is True
    assert added["relay_path"] == f"/ui/v1/remotes/{added['id']}/chargewire"
    assert [r["id"] for r in relay.remotes().json()["remotes"]] == [added["id"]]
    # The remote's discovery document, through the relay.
    discovery = relay.http.get(url=added["relay_path"], headers=relay.headers()).json()
    assert discovery["protocol"] == "chargewire"
    # Sign in to the remote through the relay, then a session, a query and its rows.
    token = relay.sign_in(added["id"], plain.username, plain.password)
    client = relay.client(added["id"], token)
    session = client.new_session()
    assert session.query("SELECT count(*) AS n FROM t").to_pylist() == [{"n": 5}]
    # The statement ran as the remote's user, on the remote (its session id is the remote's).
    assert session.query("SELECT HARDCHARGER_USER() AS u").to_pylist() == [{"u": plain.username}]
    # Metadata (the schema browser's listings) and bulk ingest (an upload) go through too.
    catalogs = client.new_session().metadata("catalogs")
    assert "memory" in catalogs.column("catalog_name").to_pylist()
    response = session.ingest(table=pa.table({"id": [10, 11], "label": ["ten", "eleven"]}), name="t", mode="append")
    assert response.status_code == 200, response.text
    assert session.query("SELECT count(*) AS n FROM t").to_pylist() == [{"n": 7}]
    # Removing the remote ends the forwarding (the remote's token is the remote's business).
    assert relay.remove(added["id"]).status_code == 204
    problem(relay.http.get(url=added["relay_path"], headers=relay.headers()), 404, "remote_not_found")
    assert relay.remotes().json()["remotes"] == []


def test_only_the_remotes_chargewire_paths_are_relayed(ui, plain):
    relay = Relay(ui, token=ui.token())
    added = relay.add(url=plain.base_url).json()
    for path in ["/ui/", "/health", "/chargewireX", "/metrics"]:
        problem(relay.http.get(url=f"/ui/v1/remotes/{added['id']}{path}", headers=relay.headers()), 404, "not_found")
    problem(relay.http.get(url="/ui/v1/remotes/r-nope/chargewire", headers=relay.headers()), 404, "remote_not_found")
    problem(relay.http.get(url="/ui/v1/nope", headers=relay.headers()), 404, "not_found")


def test_a_remote_belongs_to_the_sign_in_that_added_it(ui, plain):
    first = Relay(ui, token=ui.token())
    added = first.add(url=plain.base_url).json()
    # Another token (another sign-in, even of the same user) has its own relay session.
    second = Relay(ui, token=ui.token())
    assert second.remotes().json()["remotes"] == []
    problem(second.http.get(url=added["relay_path"], headers=second.headers()), 404, "remote_not_found")
    problem(second.remove(added["id"]), 404, "remote_not_found")
    # The named relay session works as the token does (the UI keeps it across a reload).
    named = Relay(ui, token=ui.token(), relay_session=added["relay_session"])
    assert [r["id"] for r in named.remotes().json()["remotes"]] == [added["id"]]
    # Adding the same server again replaces the earlier entry, so a reload doesn't pile them up.
    again = first.add(url=plain.base_url).json()
    assert [r["id"] for r in first.remotes().json()["remotes"]] == [again["id"]]


def test_the_remotes_errors_come_back_as_they_are(ui, plain):
    relay = Relay(ui, token=ui.token())
    added = relay.add(url=plain.base_url).json()
    bad = relay.http.post(url=f"{added['relay_path']}/v1/auth/token",
                          json={"grant_type": "password", "username": plain.username, "password": "wrong"},
                          headers=relay.headers())
    assert bad.status_code == 401 and bad.json()["code"] == "invalid_credentials"
    assert bad.headers["content-type"].startswith("application/problem+json")
    token = relay.sign_in(added["id"], plain.username, plain.password)
    client = relay.client(added["id"], token)
    session = client.new_session()
    # A failed statement: the same status and problem the remote answers directly.
    failed = session.submit(sql="SELECT * FROM no_such_table", headers={"Prefer": "wait=10"})
    direct = Client(base_url=plain.base_url, token=token).new_session().submit(
        sql="SELECT * FROM no_such_table", headers={"Prefer": "wait=10"})
    assert failed.status_code == direct.status_code
    assert failed.json()["code"] == direct.json()["code"] and "no_such_table" in failed.text
    # No remote token: the remote answers 401 (the relay forwards no Authorization of its own).
    problem(relay.client(added["id"], None).open_session(), 401, "invalid_token")


def test_a_resuming_remotes_answer_reaches_the_browser_as_it_is(ui, plain):
    """A remote behind an auto-resume holder (Chargewire §11.2): while it starts, the holder
    answers 503 server_resuming with a Retry-After, and the relay hands that to the browser
    unchanged, so the UI's client waits as the remote asks and shows it, then goes on."""
    holder = Holder(upstream_port=plain.port, retry_after="2")
    try:
        relay = Relay(ui, token=ui.token())
        added = relay.add(url=f"http://127.0.0.1:{holder.port}", name="resuming").json()
        holder.resuming = 2  # the remote stops, and its holder answers for it
        sign_in = {"grant_type": "password", "username": plain.username, "password": plain.password}
        for _ in range(2):
            held = relay.http.post(url=f"{added['relay_path']}/v1/auth/token", json=sign_in, headers=relay.headers())
            body = problem(held, 503, "server_resuming")
            assert held.headers["retry-after"] == "2", held.headers
            assert held.headers["content-type"].startswith("application/problem+json")
            assert body["detail"] == "the server is resuming; retry after 2 s", body
        # Started: the same sign-in now goes through, and so does the rest.
        token = relay.sign_in(added["id"], plain.username, plain.password)
        session = relay.client(added["id"], token).new_session()
        assert session.query("SELECT count(*) AS n FROM t").to_pylist()[0]["n"] >= 5
        assert len(holder.held) == 2
    finally:
        holder.stop()


def test_the_url_rules(ui, plain, signed):
    relay = Relay(ui, token=ui.token())
    problem(relay.add(url="ftp://x"), 400, "bad_request")
    problem(relay.add(url="not a url"), 400, "bad_request")
    problem(relay.add(url=f"http://user:pw@127.0.0.1:{plain.port}"), 400, "bad_request")
    problem(relay.add(url=plain.base_url, tls="verified"), 400, "bad_request")  # http has no certificate
    problem(relay.add(url=signed.base_url, tls="plaintext"), 400, "bad_request")  # https isn't plaintext
    problem(relay.add(url=signed.base_url, tls="banana"), 400, "bad_request")
    problem(relay.add(url=signed.base_url, tls="fingerprint"), 400, "bad_request")  # needs the fingerprint
    problem(relay.add(url=signed.base_url, tls="fingerprint", fingerprint="zz"), 400, "bad_request")
    problem(relay.add(), 400, "bad_request")
    problem(relay.add(url="http://127.0.0.1:1"), 502, "remote_unreachable")
    # A server that answers but isn't Chargewire.
    problem(relay.add(url=f"{ui.base_url}/ui"), 502, "remote_not_chargewire")
    # A base path other than /chargewire is kept (the UI's own origin mounts it there).
    assert relay.probe(url=f"{plain.base_url}/chargewire").json()["base_path"] == "/chargewire"


# ---- TLS: verified, and trusted by fingerprint ----


def test_a_tls_remote_verified_against_the_trusted_roots(ui, signed, certificates):
    relay = Relay(ui, token=ui.token())
    probed = relay.probe(url=signed.base_url).json()
    assert probed["tls_mode"] == "verified" and probed["tls"]["verified"] is True
    assert probed["tls"]["fingerprint"] == certificates.signed_fingerprint
    assert "127.0.0.1" in probed["tls"]["names"] and probed["tls"]["subject"] == "CN=relay-test-signed"
    added = relay.add(url=signed.base_url).json()
    assert added["tls_mode"] == "verified" and added["tls"]["verified"] is True
    token = relay.sign_in(added["id"], signed.username, signed.password)
    assert relay.client(added["id"], token).new_session().query("SELECT 42 AS v").to_pylist() == [{"v": 42}]


def test_a_self_signed_remote_is_refused_until_its_fingerprint_is_trusted(ui, selfsigned, certificates):
    relay = Relay(ui, token=ui.token())
    refused = problem(relay.add(url=selfsigned.base_url), 502, "remote_certificate_untrusted")
    assert "fingerprint" in refused["detail"]
    problem(relay.probe(url=selfsigned.base_url, tls="verified"), 502, "remote_certificate_untrusted")
    # Probing in fingerprint mode shows the certificate, so the person can decide.
    shown = relay.probe(url=selfsigned.base_url, tls="fingerprint").json()
    assert shown["tls"]["verified"] is False and shown["tls"]["fingerprint"] == certificates.selfsigned_fingerprint
    assert shown["tls"]["subject"] == "CN=relay-test-self" and shown["tls"]["not_after"].endswith("Z")
    # The wrong fingerprint (the signed server's) is refused; the right one, in any spelling, works.
    mismatch = problem(relay.add(url=selfsigned.base_url, tls="fingerprint", fingerprint=certificates.signed_fingerprint),
                       502, "remote_certificate_mismatch")
    assert certificates.selfsigned_fingerprint in mismatch["detail"]
    spelled = certificates.selfsigned_fingerprint.replace(":", "").upper()
    added = relay.add(url=selfsigned.base_url, tls="fingerprint", fingerprint=spelled).json()
    assert added["tls_mode"] == "fingerprint" and added["tls"]["verified"] is False
    assert added["tls"]["fingerprint"] == certificates.selfsigned_fingerprint
    token = relay.sign_in(added["id"], selfsigned.username, selfsigned.password)
    assert relay.client(added["id"], token).new_session().query("SELECT 7 AS v").to_pylist() == [{"v": 7}]


def test_the_relay_logs_remotes_without_their_tokens(ui, plain):
    relay = Relay(ui, token=ui.token())
    added = relay.add(url=plain.base_url).json()
    token = relay.sign_in(added["id"], plain.username, plain.password)
    records = [r for r in ui.log_records() if r.get("component") == "web_ui"]
    assert any(r["message"] == "web UI: remote server added" and r.get("remote") == plain.base_url for r in records)
    assert token not in ui.log() and relay.token not in ui.log()


# ---- Streaming: a big result and a big upload ----


@pytest.mark.serial
def test_a_big_result_streams_through_the_relay_at_about_the_direct_speed(ui, plain):
    relay = Relay(ui, token=ui.token())
    added = relay.add(url=plain.base_url).json()
    token = relay.sign_in(added["id"], plain.username, plain.password)
    sql = "SELECT range AS a, range * 2 AS b, 'value ' || range AS s FROM range(4000000)"

    def read(session: Session) -> tuple[int, float]:
        submitted = session.submit(sql=sql, headers={"Prefer": "wait=60"}).json()
        assert submitted["state"] == "succeeded", submitted
        started = time.monotonic()
        size = 0
        with session.http.stream("GET", url=f"{session.url}/statements/{submitted['statement_id']}/result",
                                 headers=session.headers(Accept="application/vnd.apache.arrow.stream")) as response:
            assert response.status_code == 200
            for chunk in response.iter_raw():
                size += len(chunk)
        return size, time.monotonic() - started

    direct = Client(base_url=plain.base_url, token=token).new_session()
    direct_size, direct_seconds = read(direct)
    relayed_size, relayed_seconds = read(relay.client(added["id"], token).new_session())
    assert relayed_size == direct_size > 100 * 1024 * 1024
    # Measured: 110 MB in 0.09 s through the relay against 0.02 s direct on a laptop. The
    # bound leaves room for a loaded runner; a relay that buffered the whole answer, or read
    # it in small pieces, misses it by far more.
    assert relayed_seconds <= max(4 * direct_seconds, 1.5 * PERF_SCALE), (direct_seconds, relayed_seconds)


def test_a_big_upload_streams_through_the_relay(ui, plain):
    relay = Relay(ui, token=ui.token())
    added = relay.add(url=plain.base_url).json()
    token = relay.sign_in(added["id"], plain.username, plain.password)
    session = relay.client(added["id"], token).new_session()
    rows = 2_000_000
    table = pa.table({"id": pa.array(range(rows), type=pa.int64()), "s": pa.array([f"v{i}" for i in range(rows)])})
    response = session.ingest(table=table, name="big", mode="create")
    assert response.status_code == 200, response.text
    assert response.json()["rows_ingested"] == rows
    assert session.query("SELECT count(*) AS n, max(id) AS m FROM big").to_pylist() == [{"n": rows, "m": rows - 1}]


# ---- The operator switch ----


def test_web_ui_remote_servers_false_turns_the_relay_off(launcher, plain):
    off = launcher.start(args=["--web-ui", "--web-ui-remote-servers=false"])
    doc = httpx.get(url=f"{off.base_url}/ui/v1/app", timeout=10).json()
    assert doc["local_server"] is True and doc["remote_servers"] is False
    relay = Relay(off, token=off.token())
    refused = problem(relay.add(url=plain.base_url), 404, "not_found")
    assert "web_ui_remote_servers" in refused["detail"]
    problem(relay.remotes(), 404, "not_found")
    problem(relay.probe(url=plain.base_url), 404, "not_found")
    # The UI itself is still served.
    assert httpx.get(url=f"{off.base_url}/ui/", timeout=10).status_code == 200


# ---- A scoped token, as the dialog's hint has it: user `token`, the token as the password ----


def test_a_scoped_token_signs_in_through_the_relay_as_user_token(ui, module_launcher, tmp_path_factory):
    store = tmp_path_factory.mktemp("relay-token-store") / "rbac.duckdb"
    remote = module_launcher.start(args=["--init-sql-commands", SEED, "--rbac-store-uri", f"duckdb:{store}"])
    admin = Client(base_url=remote.base_url, token=remote.token()).new_session()
    for sql in ["CREATE USER reader PASSWORD 'pw:reader-1'", "CREATE ROLE readers",
                "GRANT SELECT ON t TO readers", "GRANT readers TO reader"]:
        admin.execute(sql=sql)
    scoped = admin.query(sql="CREATE SCOPED TOKEN relay_job FOR USER reader ALLOW SELECT").to_pylist()[0]["token"]
    time.sleep(1.5 * TIMEOUT_SCALE)  # the store's grants reach the sessions within about a second
    relay = Relay(ui, token=ui.token())
    added = relay.add(url=remote.base_url, name="by token").json()
    token = relay.sign_in(added["id"], "token", scoped)
    session = relay.client(added["id"], token).new_session()
    assert session.query(sql="SELECT HARDCHARGER_USER() AS u").to_pylist() == [{"u": "reader"}]
    assert session.query(sql="SELECT count(*) AS n FROM t").to_pylist() == [{"n": 5}]
    # The token's scope holds through the relay: it allows SELECT only.
    refused = session.submit(sql="CREATE TABLE nope (i INTEGER)", headers={"Prefer": "wait=10"})
    assert refused.status_code in (201, 403) and "nope" not in session.query(
        sql="SELECT table_name FROM duckdb_tables()").column("table_name").to_pylist()
    # A wrong token is the remote's refusal, passed through unchanged.
    bad = relay.http.post(url=f"{added['relay_path']}/v1/auth/token", headers=relay.headers(),
                          json={"grant_type": "password", "username": "token", "password": scoped[:-4] + "AAAA"})
    assert bad.status_code == 401 and bad.json()["code"] == "invalid_credentials"
