"""trusted_proxies: behind a reverse proxy, the client's address comes from the proxy's
X-Forwarded-For (or RFC 7239 Forwarded) header, but only when the TCP peer is a listed
proxy. It shows in hardcharger_active_connections() (client_address, with the proxy's own
address in proxy_address), in security log and audit lines (peer, proxy) and in the
per-address bound on browser sign-ins. Authentication and TLS never see it: sign-in works
as before, and a certificate-bound token still needs the certificate on the connection.
The header parsing itself (every form, malformed values) is the Warp SQL SDK's
forwarded_test.cc."""

import json
import subprocess
import time

import httpx
import pytest
from conftest import BINARY, problem
from mock_idp import MockIdP
from test_mtls_identity import Pki
from test_mtls_identity import start as start_tls
from warp_client import Client

FORWARDED_CLIENT = "203.0.113.9"


def sign_in(server, headers: dict[str, str]) -> Client:
    """A client of the administrator whose every request, the sign-in included, carries `headers`."""
    response = httpx.post(url=f"{server.base_url}/warp/v1/auth/token", headers=headers, timeout=60,
                          json={"grant_type": "password", "username": server.username, "password": server.password})
    assert response.status_code == 200, response.text
    client = Client(base_url=server.base_url, token=response.json()["access_token"])
    client.http.headers.update(headers)
    return client


def own_connection(server, headers: dict[str, str]) -> dict:
    """The hardcharger_active_connections() row of a session opened with `headers`."""
    session = sign_in(server=server, headers=headers).new_session()
    rows = session.rows(sql="SELECT client_address, proxy_address FROM hardcharger_active_connections() "
                            "WHERE is_current")
    assert len(rows) == 1, rows
    return rows[0]


def assert_direct(row: dict) -> None:
    host, port = row["client_address"].rsplit(":", 1)
    assert host == "127.0.0.1" and int(port) > 0, row
    assert row["proxy_address"] is None, row


def assert_forwarded(row: dict, client: str) -> None:
    assert row["client_address"] == client, row
    host, port = row["proxy_address"].rsplit(":", 1)
    assert host == "127.0.0.1" and int(port) > 0, row


def test_an_untrusted_peers_forwarding_headers_are_ignored(launcher):
    server = launcher.start(args=["--trusted-proxies", "10.0.0.0/8"])  # 127.0.0.1 is not in it
    assert_direct(row=own_connection(server=server, headers={"X-Forwarded-For": FORWARDED_CLIENT,
                                                             "Forwarded": f"for={FORWARDED_CLIENT}"}))


def test_no_proxy_is_trusted_by_default(launcher):
    server = launcher.start()
    assert_direct(row=own_connection(server=server, headers={"X-Forwarded-For": FORWARDED_CLIENT}))


def test_a_trusted_proxy_names_the_client(launcher):
    server = launcher.start(args=["--trusted-proxies", "127.0.0.1/32"])
    assert_forwarded(row=own_connection(server=server, headers={"X-Forwarded-For": FORWARDED_CLIENT}),
                     client=FORWARDED_CLIENT)
    # Without X-Forwarded-For, RFC 7239 Forwarded; an IPv6 client is bracketed and has no port.
    assert_forwarded(row=own_connection(server=server, headers={"Forwarded": 'for="[2001:db8::7]:4711";proto=https'}),
                     client="[2001:db8::7]")
    # A malformed header leaves the TCP peer.
    assert_direct(row=own_connection(server=server, headers={"X-Forwarded-For": "not-an-address"}))


def test_a_chain_is_the_rightmost_hop_that_is_not_a_trusted_proxy(launcher):
    server = launcher.start(args=["--trusted-proxies", "127.0.0.0/8,10.0.0.0/8"])
    # The client's own claim (1.1.1.1) is left of the first untrusted hop and not believed.
    assert_forwarded(row=own_connection(server=server, headers={"X-Forwarded-For": "1.1.1.1, 198.51.100.7, 10.1.2.3"}),
                     client="198.51.100.7")
    # Every hop a trusted proxy: the leftmost.
    assert_forwarded(row=own_connection(server=server, headers={"X-Forwarded-For": "10.0.0.5, 10.1.2.3"}),
                     client="10.0.0.5")
    # X-Forwarded-For wins over Forwarded.
    assert_forwarded(row=own_connection(server=server, headers={"X-Forwarded-For": "198.51.100.7",
                                                                "Forwarded": "for=192.0.2.1"}),
                     client="198.51.100.7")


def test_the_option_is_in_the_reference():
    out = subprocess.run(args=[str(BINARY), "--print-options-json"], capture_output=True, text=True, check=True,
                         timeout=30)
    options = json.loads(out.stdout)["options"]
    option = next(o for o in options if o["name"] == "trusted_proxies")
    assert (option["type"], option["group"], option["default"]) == ("list", "Listener", ""), option
    assert (option["flag"], option["env"]) == ("--trusted-proxies", "HARDCHARGER_TRUSTED_PROXIES"), option
    assert "never used for authentication" in option["description"].lower(), option


@pytest.mark.parametrize(argnames=("value", "message"), argvalues=[
    ("10.0.0.0/33", "trusted_proxies: '10.0.0.0/33' is not an IP network: the prefix length must be 0-32"),
    ("proxy.internal", "trusted_proxies: 'proxy.internal' is not an IP network"),
    ("10.1.2.3/8", "did you mean 10.0.0.0/8?"),
    ("10.0.0.0/8,fd00::/129", "trusted_proxies: 'fd00::/129' is not an IP network: the prefix length must be 0-128"),
])
def test_a_bad_network_stops_startup(launcher, value, message):
    running = launcher.start(args=["--trusted-proxies", value], expect_up=False)
    assert running.process.wait(timeout=30) == 2, running.log()
    assert message in running.log(), running.log()
    # The same check for the environment variable.
    env_running = launcher.start(env={"HARDCHARGER_TRUSTED_PROXIES": value}, expect_up=False)
    assert env_running.process.wait(timeout=30) == 2, env_running.log()
    assert message in env_running.log(), env_running.log()


def test_forwarding_headers_change_nothing_about_signing_in(launcher):
    server = launcher.start(args=["--trusted-proxies", "127.0.0.1"])
    headers = {"X-Forwarded-For": FORWARDED_CLIENT}
    refused = httpx.post(url=f"{server.base_url}/warp/v1/auth/token", headers=headers, timeout=60,
                         json={"grant_type": "password", "username": server.username, "password": "wrong"})
    problem(response=refused, status=401, code="invalid_credentials")
    assert own_connection(server=server, headers=headers)["client_address"] == FORWARDED_CLIENT


def security_records(server, code: str) -> list[dict]:
    return [r for r in server.running.log_records() if r.get("message") == f"security: refused {code}"]


def test_security_events_name_the_forwarded_client_and_the_proxy(launcher, pki):
    """A refusal that may be an attack is logged and audited with the forwarded client as its
    peer and the proxy's own address:port as proxy, while the certificate checks stay on the
    connection: a certificate-bound token used over a connection without the certificate is
    refused whatever the headers say."""
    ops = pki.issue(name="ops@example.com")
    server = start_tls(launcher=launcher, pki=pki, probe=ops,
                       args=["--trusted-proxies", "127.0.0.1", "--mtls-require-identity-match",
                             "--mtls-certificate-sign-in", "--mtls-admin-identities", "ops@example.com",
                             "--mtls-require-client-cert=false"])
    headers = {"X-Forwarded-For": FORWARDED_CLIENT}
    with server.http(identity=ops) as http:
        mismatch = http.post(url="/warp/v1/auth/token", headers=headers,
                             json={"grant_type": "password", "username": server.running.username,
                                   "password": server.running.password})
        problem(response=mismatch, status=401, code="certificate_identity_mismatch")
        granted = http.post(url="/warp/v1/auth/token", headers=headers, json={"grant_type": "client_certificate"})
        assert granted.status_code == 200, granted.text
    token = granted.json()["access_token"]
    with server.http(identity=None) as http:  # the bound token without its certificate
        replay = http.post(url="/warp/v1/sessions", json={},
                           headers={**headers, "Authorization": f"Bearer {token}"})
        problem(response=replay, status=401, code="certificate_mismatch")
    time.sleep(0.5)
    for code in ("certificate_identity_mismatch", "certificate_mismatch"):
        records = security_records(server=server, code=code)
        assert len(records) == 1, server.running.log()
        assert records[0]["peer"] == FORWARDED_CLIENT, records
        host, port = records[0]["proxy"].rsplit(":", 1)
        assert host == "127.0.0.1" and int(port) > 0, records
    audited = [r for r in server.running.log_records()
               if r.get("message") == "client certificate check refused a request"]
    assert {r["code"] for r in audited} == {"certificate_identity_mismatch", "certificate_mismatch"}, audited
    assert all(r["peer"] == FORWARDED_CLIENT for r in audited), audited


def test_a_direct_clients_security_event_has_no_proxy(launcher, pki):
    ops = pki.issue(name="ops@example.com")
    server = start_tls(launcher=launcher, pki=pki, probe=ops,
                       args=["--trusted-proxies", "10.0.0.0/8", "--mtls-require-identity-match"])
    with server.http(identity=ops) as http:
        refused = http.post(url="/warp/v1/auth/token", headers={"X-Forwarded-For": FORWARDED_CLIENT},
                            json={"grant_type": "password", "username": server.running.username,
                                  "password": server.running.password})
    problem(response=refused, status=401, code="certificate_identity_mismatch")
    time.sleep(0.5)
    records = security_records(server=server, code="certificate_identity_mismatch")
    assert len(records) == 1 and records[0]["peer"].startswith("127.0.0.1:") and "proxy" not in records[0], records


@pytest.fixture
def pki(tmp_path):
    return Pki(directory=tmp_path)


# ---- The per-address bound on browser sign-ins (the SDK's: 100 at once per address) ----

PER_ADDRESS = 100
AUDIENCE = "hardcharger-proxy-test"


@pytest.fixture(scope="module")
def idp():
    provider = MockIdP(audience=AUDIENCE, client_id="proxy-client", client_secret="proxy-secret")
    yield provider
    provider.stop()


def oauth_server(launcher, idp, trusted: str):
    return launcher.start(args=["--trusted-proxies", trusted, "--token-allowed-issuer", idp.issuer,
                                "--token-allowed-audience", AUDIENCE, "--oauth-client-id", "proxy-client",
                                "--oauth-client-secret", "proxy-secret"])


def new_flow(http: httpx.Client, client: str) -> int:
    return http.post(url="/warp/v1/auth/oauth/flows", json={}, headers={"X-Forwarded-For": client}).status_code


def test_browser_sign_ins_are_bounded_per_forwarded_client(launcher, idp):
    server = oauth_server(launcher=launcher, idp=idp, trusted="127.0.0.1")
    with httpx.Client(base_url=server.base_url, timeout=30) as http:
        assert [new_flow(http=http, client="203.0.113.1") for _ in range(PER_ADDRESS)] == [201] * PER_ADDRESS
        assert new_flow(http=http, client="203.0.113.1") == 429
        # Another client behind the same proxy has its own bound.
        assert new_flow(http=http, client="203.0.113.2") == 201


def test_an_untrusted_client_cannot_escape_the_bound_with_invented_addresses(launcher, idp):
    server = oauth_server(launcher=launcher, idp=idp, trusted="10.0.0.0/8")
    with httpx.Client(base_url=server.base_url, timeout=30) as http:
        statuses = [new_flow(http=http, client=f"203.0.113.{i % 250 + 1}") for i in range(PER_ADDRESS + 1)]
    assert statuses == [201] * PER_ADDRESS + [429], statuses
