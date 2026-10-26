"""The sessions list reports the effective client IP and locality to the server host."""

import httpx
import pytest


def test_local_session_has_an_ip_without_an_ephemeral_port(launcher):
    session = launcher.start().admin()
    row = session.rows(sql="SELECT client_ip, client_is_local FROM hardcharger_sessions() WHERE is_current")[0]
    assert row == {"client_ip": "127.0.0.1", "client_is_local": True}


@pytest.mark.parametrize("address, expected, local", [
    ("198.51.100.20", "198.51.100.20", False),
    ("2001:db8::20", "2001:db8::20", False),
    ("127.0.0.2", "127.0.0.2", True),
    ("::ffff:127.0.0.1", "127.0.0.1", True),
])
def test_trusted_proxy_reports_effective_client_rather_than_local_proxy(launcher, address, expected, local):
    server = launcher.start(args=["--trusted-proxies", "127.0.0.1"])
    token = server.token()
    opened = httpx.post(url=f"{server.base_url}/warp/v1/sessions",
                        headers={"Authorization": f"Bearer {token}", "X-Forwarded-For": address}, json={})
    assert opened.status_code == 201, opened.text
    session_id = opened.json()["session_id"]
    row = server.admin().rows(sql=f"SELECT client_ip, client_is_local FROM hardcharger_sessions() WHERE session_id = '{session_id}'")[0]
    assert row == {"client_ip": expected, "client_is_local": local}
