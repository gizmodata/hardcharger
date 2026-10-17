"""Port of GizmoSQL 1.x tests/integration/test_system_ca_certs.cpp (3 cases).

The server's outbound HTTPS (OpenID discovery, JWKS, the OAuth code exchange) verifies
against the system's CA bundle: $SSL_CERT_FILE when it names a file, else the first
well-known bundle. The 3 cases are C++ unit tests (SystemCaCerts); 1.x's "nonexistent
SSL_CERT_FILE" case passed when nothing was found, and now must find the system bundle.

Added, end to end: `--verify-tls URL` (1.x's self-check of a container's trust store)
against a local HTTPS server whose certificate is trusted only through SSL_CERT_FILE.
"""

import subprocess

from conftest import BINARY, clean_environment, run_unit_tests
from mock_idp import self_signed_https


def test_ca_bundle_unit_tests():
    assert run_unit_tests(gtest_filter="SystemCaCerts.*:UrlEncode.*") == {
        "SystemCaCerts.RespectsSslCertFileEnv": True,
        "SystemCaCerts.IgnoresNonexistentSslCertFileEnv": True,
        "SystemCaCerts.FindsASystemBundle": True,
        "UrlEncode.KeepsUnreservedAndEncodesTheRest": True,
    }


def verify_tls(url: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(args=[str(BINARY), "--verify-tls", url], env={**clean_environment(), **env},
                          capture_output=True, text=True, timeout=60)


def test_verify_tls_uses_ssl_cert_file(tmp_path):
    url, ca_file, server = self_signed_https(tmp_path=tmp_path)
    try:
        trusted = verify_tls(url=url, env={"SSL_CERT_FILE": str(ca_file)})
        assert trusted.returncode == 0, trusted.stdout + trusted.stderr
        assert f"CA bundle {ca_file}" in trusted.stdout and f"OK {url} -> HTTP 200" in trusted.stdout, trusted.stdout
        # The system bundle doesn't know this certificate: the handshake must fail.
        untrusted = verify_tls(url=url, env={"SSL_CERT_FILE": str(tmp_path / "missing.pem")})
        assert untrusted.returncode == 1, untrusted.stdout + untrusted.stderr
        assert "FAILED" in untrusted.stdout and "certificate verify failed" in untrusted.stdout, untrusted.stdout
    finally:
        server.shutdown()
        server.server_close()


def test_verify_tls_checks_the_host_name(tmp_path):
    # A trusted certificate for another name: reached as 127.0.0.1 it must not verify.
    url, ca_file, server = self_signed_https(tmp_path=tmp_path, dns_name="other.example.test")
    try:
        result = verify_tls(url=url, env={"SSL_CERT_FILE": str(ca_file)})
        assert result.returncode == 1 and "certificate verify failed" in result.stdout, result.stdout + result.stderr
    finally:
        server.shutdown()
        server.server_close()
