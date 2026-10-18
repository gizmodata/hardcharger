"""A small OpenID Connect identity provider for the sign-in tests: discovery, a JWKS, an
authorization page, and a token endpoint that trades one code for a signed ID token.
GizmoSQL 1.x's OAuth tests ran the same kind of mock in C++."""

import base64
import datetime
import http.server
import ipaddress
import json
import ssl
import threading
import urllib.parse

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID


def b64url_uint(value: int, length: int | None = None) -> str:
    """`length`: the fixed size in bytes, which RFC 7518 §6.2.1.2 requires of an EC
    coordinate (32 for P-256). Without it, a coordinate with a leading zero byte (2 keys
    in 256) came out 31 bytes long and the server refused every token of that key."""
    raw = value.to_bytes(length=length or (value.bit_length() + 7) // 8, byteorder="big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class MockIdP:
    """Serves on 127.0.0.1 (a free port). `audience`, `client_id` and `client_secret` are
    what the server under test must be configured with; `code` and `email` decide what
    the next token request gets."""

    def __init__(self, audience: str = "hardcharger-oauth-test", client_id: str = "test-client-id",
                 client_secret: str = "test-client-secret", algorithm: str = "RS256"):
        self.audience = audience
        self.client_id = client_id
        self.client_secret = client_secret
        self.algorithm = algorithm
        self.kid = "test-key-1"
        if algorithm.startswith("ES"):
            self.key = ec.generate_private_key(curve=ec.SECP256R1())
        else:
            self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.valid_code = "test-auth-code-123"
        self.email = "testuser@gizmodata.com"
        self.token_requests: list[dict] = []
        self.jwks_requests = 0
        idp = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # quiet
                pass

            def reply(self, status: int, body, content_type: str = "application/json"):
                data = (json.dumps(body) if content_type == "application/json" else body).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = urllib.parse.urlparse(self.path).path
                if path == "/.well-known/openid-configuration":
                    self.reply(status=200, body={"issuer": idp.issuer, "jwks_uri": f"{idp.issuer}/jwks",
                                                 "authorization_endpoint": f"{idp.issuer}/authorize",
                                                 "token_endpoint": f"{idp.issuer}/token"})
                elif path == "/jwks":
                    idp.jwks_requests += 1
                    self.reply(status=200, body={"keys": [idp.jwk()]})
                elif path == "/authorize":
                    self.reply(status=200, body="Mock authorization page", content_type="text/html")
                else:
                    self.reply(status=404, body={"error": "not_found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                form = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode()))
                idp.token_requests.append(form)
                if urllib.parse.urlparse(self.path).path != "/token":
                    return self.reply(status=404, body={"error": "not_found"})
                if form.get("grant_type") != "authorization_code":
                    return self.reply(status=400, body={"error": "unsupported_grant_type"})
                if form.get("client_id") != idp.client_id or form.get("client_secret") != idp.client_secret:
                    return self.reply(status=401, body={"error": "invalid_client"})
                if form.get("code") != idp.valid_code:
                    return self.reply(status=400, body={"error": "invalid_grant",
                                                        "error_description": "the code is not valid"})
                self.reply(status=200, body={"access_token": "unused", "token_type": "Bearer",
                                             "id_token": idp.id_token(email=idp.email)})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.issuer = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def jwk(self) -> dict:
        public = self.key.public_key().public_numbers()
        if self.algorithm.startswith("ES"):
            return {"kty": "EC", "kid": self.kid, "alg": self.algorithm, "use": "sig", "crv": "P-256",
                    "x": b64url_uint(public.x, length=32), "y": b64url_uint(public.y, length=32)}
        return {"kty": "RSA", "kid": self.kid, "alg": self.algorithm, "use": "sig",
                "n": b64url_uint(public.n), "e": b64url_uint(public.e)}

    def id_token(self, email: str, kid: str | None = None, key=None, **claims) -> str:
        now = datetime.datetime.now(tz=datetime.UTC)
        payload = {"iss": self.issuer, "aud": self.audience, "sub": email, "email": email, "iat": now,
                   "exp": now + datetime.timedelta(minutes=5), **claims}
        return jwt.encode(payload=payload, key=key or self.key, algorithm=self.algorithm,
                          headers={"kid": kid or self.kid})

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def self_signed_https(tmp_path, body: bytes = b"{}", dns_name: str | None = None):
    """An HTTPS server on 127.0.0.1 with a self-signed certificate for that address (or
    only for `dns_name`); returns (url, ca_file, server). Trusting ca_file makes it verify."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(oid=NameOID.COMMON_NAME, value="127.0.0.1")])
    now = datetime.datetime.now(tz=datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(extval=x509.SubjectAlternativeName(
                [x509.DNSName(dns_name)] if dns_name else [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                critical=False)
            .add_extension(extval=x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(private_key=key, algorithm=hashes.SHA256()))
    cert_file = tmp_path / "https-cert.pem"
    key_file = tmp_path / "https-key.pem"
    cert_file.write_bytes(cert.public_bytes(encoding=serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(encoding=serialization.Encoding.PEM,
                                           format=serialization.PrivateFormat.PKCS8,
                                           encryption_algorithm=serialization.NoEncryption()))

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(protocol=ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    server.socket = context.wrap_socket(sock=server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"https://127.0.0.1:{server.server_address[1]}/probe", cert_file, server
