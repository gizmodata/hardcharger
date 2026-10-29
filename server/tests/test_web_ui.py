"""The web UI (ui/), embedded in the server and served at /ui when
web_ui is on: on the server's own port, origin and TLS certificate, with
hardcharger-ui's serving rules (its scripts/ui-contract.mjs) and nothing at all when off.

These need a build that embeds the UI (-DHARDCHARGER_WEB_UI=<hardcharger-ui tarball>, as
CI's and the release builds are). Against one without it they fail, never skip; deselect
them with -m "not web_ui".
"""

import datetime
import gzip
import hashlib
import ipaddress
import re
import ssl
import time

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

pytestmark = pytest.mark.web_ui

CSP = ("default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self'; "
       "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
IMMUTABLE = "public, max-age=31536000, immutable"


@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start(args=["--web-ui"])


def get(server, path: str, method: str = "GET") -> httpx.Response:
    return httpx.request(method=method, url=f"{server.base_url}{path}", timeout=30, follow_redirects=False)


def raw(server, path: str, accept_encoding: str | None, method: str = "GET",
        extra: dict[str, str] | None = None) -> tuple[httpx.Response, bytes]:
    """The answer and its body exactly as sent (httpx would decode gzip; this doesn't). With
    accept_encoding None, the request has no Accept-Encoding header at all."""
    headers = dict(extra or {})
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        request = client.build_request(method=method, url=f"{server.base_url}{path}", headers=headers)
        if accept_encoding is None:
            del request.headers["accept-encoding"]
        else:
            request.headers["accept-encoding"] = accept_encoding
        response = client.send(request, stream=True)
        body = b"".join(response.iter_raw())
        response.close()
    return response, body


def assert_ui_headers(response: httpx.Response) -> None:
    assert response.headers["content-security-policy"] == CSP
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"


def assets(server) -> dict[str, httpx.Response]:
    """Every file the UI loads, found from index.html and the scripts it loads (lazily
    loaded pages, the editor's worker, the Parquet WebAssembly)."""
    index = get(server=server, path="/ui/").text
    queue = re.findall(pattern=r'(?:src|href)="/ui/(assets/[^"]+)"', string=index)
    found: dict[str, httpx.Response] = {}
    while queue:
        name = queue.pop()
        if name in found:
            continue
        found[name] = get(server=server, path=f"/ui/{name}")
        if name.endswith((".js", ".css")):
            for ref in re.findall(pattern=r'["\'`/(]([\w.-]+-[\w-]{8}\.(?:js|css|wasm|ttf|woff2|png|svg|webp))\b', string=found[name].text):
                queue.append(f"assets/{ref}")
    return found


def test_ui_redirects_to_its_directory(server):
    response = get(server=server, path="/ui")
    assert response.status_code == 308 and response.headers["location"] == "/ui/", response.text
    assert_ui_headers(response=response)


def test_index_html_and_the_apps_own_routes(server):
    index = get(server=server, path="/ui/")
    assert index.status_code == 200, index.text
    assert index.headers["content-type"] == "text/html; charset=utf-8"
    assert index.headers["cache-control"] == "no-cache"
    assert_ui_headers(response=index)
    assert '<div id="root"></div>' in index.text and "<title>Hardcharger™</title>" in index.text
    for route in ["/ui/sessions", "/ui/query/", "/ui/some/deeper/route"]:
        response = get(server=server, path=route)
        assert response.status_code == 200 and response.text == index.text, route
        assert response.headers["cache-control"] == "no-cache", route


def test_index_html_runs_no_inline_code(server):
    html = get(server=server, path="/ui/").text
    scripts = re.findall(pattern=r"<script\b([^>]*)>(.*?)</script>", string=html, flags=re.S)
    assert scripts, html
    for attributes, body in scripts:
        assert re.search(pattern=r'src="/ui/assets/[^"]+\.js"', string=attributes) and not body.strip(), attributes
    assert "<style" not in html and not re.search(pattern=r"\son[a-z]+=", string=html)


def test_every_file_the_ui_loads_is_served_with_its_type_and_cached_for_good(server):
    files = assets(server=server)
    types = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".wasm": "application/wasm",
             ".ttf": "font/ttf", ".woff2": "font/woff2", ".png": "image/png",
             ".svg": "image/svg+xml", ".webp": "image/webp"}
    kinds = {name.rsplit(".", 1)[1] for name in files}
    assert {"js", "css", "wasm", "ttf", "woff2", "svg", "webp"} <= kinds, sorted(files)
    for name, response in files.items():
        assert response.status_code == 200, name
        assert response.headers["content-type"] == types["." + name.rsplit(".", 1)[1]], name
        assert response.headers["cache-control"] == IMMUTABLE, name
        assert_ui_headers(response=response)
    # The icons and the manifest index.html names, outside assets/: not content-hashed, so revalidated.
    for name, content_type in [("favicon.ico", "image/x-icon"), ("favicon.svg", "image/svg+xml"),
                               ("apple-touch-icon.png", "image/png"), ("manifest.json", "application/json")]:
        icon = get(server=server, path=f"/ui/{name}")
        assert icon.status_code == 200 and icon.headers["content-type"] == content_type, name
        assert icon.headers["cache-control"] == "no-cache", name
    manifest = get(server=server, path="/ui/manifest.json").json()
    for entry in manifest["icons"]:
        assert get(server=server, path=f"/ui/{entry['src']}").headers["content-type"] == "image/png", entry
    # The embedded font's license travels with it.
    license = get(server=server, path="/ui/fonts/Inter-LICENSE.txt")
    assert license.status_code == 200 and "SIL Open Font License" in license.text
    assert license.headers["content-type"] == "text/plain; charset=utf-8"


def test_a_missing_file_is_404_and_never_cached(server):
    # A hashed file that isn't there is a stale page asking for an old build, not one of the
    # app's routes: 404, which a browser must not keep (another replica may have it).
    for path in ["/ui/assets/QueryPage-00000000.js", "/ui/assets/route", "/ui/missing.png"]:
        response = get(server=server, path=path)
        assert response.status_code == 404, path
        assert response.headers["cache-control"] == "no-store", path
        assert_ui_headers(response=response)


def test_no_path_leaves_the_ui(server):
    # httpx would normalize "..": these are sent percent-encoded, as a client may.
    for path in ["/ui/%2e%2e/chargewire", "/ui/%2E%2E/%2E%2E/etc/passwd", "/ui/assets/%2e%2e/index.html",
                 "/ui/..%2fchargewire", "/ui/%2e/index.html", "/ui//index.html"]:
        response = get(server=server, path=path)
        assert response.status_code == 404, f"{path}: {response.status_code} {response.text[:100]}"


def test_head_is_get_without_the_body(server):
    for accept in ["gzip", None]:
        full, full_body = raw(server=server, path="/ui/", accept_encoding=accept)
        head, head_body = raw(server=server, path="/ui/", accept_encoding=accept, method="HEAD")
        assert head.status_code == 200 and head_body == b"", accept
        assert int(head.headers["content-length"]) == len(full_body), accept
        assert head.headers.get("content-encoding") == full.headers.get("content-encoding"), accept
        assert head.headers["etag"] == full.headers["etag"], accept
        assert head.headers["content-security-policy"] == CSP


# Formats that are compressed already (an image, a WOFF2 font): gzip doesn't shrink them, so
# the build embeds them as they are, as it does a file too small for gzip's own header and
# trailer to pay off (a 70-byte re-export module). Every other file (HTML, scripts,
# stylesheets, SVG, a TrueType font, WebAssembly) must come gzipped.
ALREADY_COMPRESSED = (".png", ".webp", ".ico", ".woff2")
TOO_SMALL_TO_GZIP = 256


def test_gzip_and_identity_answers_are_the_files_bytes(server):
    """The UI is embedded gzipped: a client that takes gzip gets those bytes, any other the
    inflated file. Both are the file the UI build made: its SHA-256 is the identity ETag."""
    names = ["index.html", *assets(server=server)]
    compressed = 0
    for name in names:
        gz, gz_body = raw(server=server, path=f"/ui/{name}", accept_encoding="gzip, deflate, br")
        plain, plain_body = raw(server=server, path=f"/ui/{name}", accept_encoding="identity")
        bare, bare_body = raw(server=server, path=f"/ui/{name}", accept_encoding=None)
        refused, refused_body = raw(server=server, path=f"/ui/{name}", accept_encoding="gzip;q=0, *;q=1")
        assert gz.status_code == plain.status_code == bare.status_code == refused.status_code == 200, name
        tag = plain.headers["etag"].strip('"')
        assert hashlib.sha256(plain_body).hexdigest().startswith(tag), name
        assert bare_body == plain_body and refused_body == plain_body, name
        assert "content-encoding" not in plain.headers and "content-encoding" not in bare.headers, name
        if gz.headers.get("content-encoding") == "gzip":
            compressed += 1
            assert gzip.decompress(gz_body) == plain_body, name
            assert len(gz_body) < len(plain_body), name
            assert gz.headers["vary"] == plain.headers["vary"] == "Accept-Encoding", name
            assert gz.headers["etag"] == f'"{tag}-gzip"', name
        else:
            assert name.endswith(ALREADY_COMPRESSED) or len(plain_body) < TOO_SMALL_TO_GZIP, \
                f"{name} ({len(plain_body)} bytes) came without gzip"
            assert gz_body == plain_body, name  # stored as is: gzip wouldn't shrink it
    # The build's gzip decision is exercised both ways: files it compressed, and ones it kept.
    assert compressed >= 1 and compressed < len(names), f"{compressed} of {len(names)} files came gzipped"


def test_a_matching_etag_answers_304(server):
    full, _ = raw(server=server, path="/ui/", accept_encoding="gzip")
    again, body = raw(server=server, path="/ui/", accept_encoding="gzip", extra={"If-None-Match": full.headers["etag"]})
    assert again.status_code == 304 and body == b"" and again.headers["etag"] == full.headers["etag"]
    # The gzip representation's tag doesn't validate the identity one.
    other, other_body = raw(server=server, path="/ui/", accept_encoding=None,
                            extra={"If-None-Match": full.headers["etag"]})
    assert other.status_code == 200 and other_body.startswith(b"<!doctype html>")


def test_only_get_and_head_and_only_under_ui(server):
    post = get(server=server, path="/ui/", method="POST")
    assert post.status_code == 404 and post.json()["code"] == "not_found", post.text
    for path in ["/", "/index.html", "/favicon.ico", "/uix/", "/assets/x.js"]:
        response = get(server=server, path=path)
        assert response.status_code == 404 and response.json()["code"] == "not_found", path
        assert "content-security-policy" not in response.headers, path


def test_chargewire_is_unchanged_beside_it(server):
    discovery = get(server=server, path="/chargewire")
    assert discovery.status_code == 200 and discovery.json()["protocol"] == "chargewire"
    token = server.token()
    session = server.session(token=token)
    # Without Accept: Arrow, a submit answers with the statement document (Chargewire §6.1).
    statement = server.run(token=token, session_id=session, sql="SELECT 1")
    assert statement.status_code == 201 and statement.json()["state"] in {"running", "succeeded"}, statement.text


def test_off_by_default(launcher):
    server = launcher.start()
    for path in ["/ui", "/ui/", "/ui/sessions", "/ui/favicon.ico"]:
        response = get(server=server, path=path)
        assert response.status_code == 404 and response.json()["code"] == "not_found", path
    # Positive twin: the option turns it on.
    on = launcher.start(args=["--web-ui"])
    assert get(server=on, path="/ui/").status_code == 200


def test_the_environment_variable_and_the_config_file_turn_it_on(launcher, tmp_path):
    by_env = launcher.start(env={"HARDCHARGER_WEB_UI": "true"})
    assert get(server=by_env, path="/ui/").status_code == 200
    config = tmp_path / "hardcharger.yaml"
    config.write_text("web_ui: true\n")
    by_file = launcher.start(args=["--config", str(config)])
    assert get(server=by_file, path="/ui/").status_code == 200
    off = tmp_path / "off.yaml"
    off.write_text("web_ui: false\n")
    assert get(server=launcher.start(args=["--config", str(off)]), path="/ui/").status_code == 404


def test_served_over_the_servers_own_tls(launcher, tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(oid=NameOID.COMMON_NAME, value="127.0.0.1")])
    now = datetime.datetime.now(tz=datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(extval=x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .add_extension(extval=x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(private_key=key, algorithm=hashes.SHA256()))
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(encoding=serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(encoding=serialization.Encoding.PEM,
                                           format=serialization.PrivateFormat.PKCS8,
                                           encryption_algorithm=serialization.NoEncryption()))
    server = launcher.start(args=["--web-ui", "--tls-cert", str(cert_file), "--tls-key", str(key_file)],
                            expect_up=False)
    url = f"https://127.0.0.1:{server.port}"
    trust = ssl.create_default_context(cafile=str(cert_file))
    deadline = time.monotonic() + 30
    while True:
        try:
            response = httpx.get(url=f"{url}/ui/", verify=trust, timeout=5)
            break
        except httpx.HTTPError:
            if server.process.poll() is not None or time.monotonic() > deadline:
                pytest.fail(reason=f"the TLS server did not come up: {server.log()}")
            time.sleep(0.2)
    assert response.status_code == 200 and '<div id="root"></div>' in response.text
    assert response.headers["content-security-policy"] == CSP
    # The page's protocol calls go to the same origin, over the same certificate.
    assert httpx.get(url=f"{url}/chargewire", verify=trust, timeout=5).json()["protocol"] == "chargewire"


# ---- Under mutual TLS ----
# With mtls_ca_cert the browser must present a client certificate to load /ui at all (the
# TLS handshake asks for one before any HTTP), and every request the page makes goes over
# connections presenting it, so certificate sign-in and its bound tokens work from the page.

from test_mtls_identity import Pki, start as start_mtls  # noqa: E402


def test_under_mtls_the_page_needs_a_client_certificate(launcher, tmp_path):
    pki = Pki(directory=tmp_path)
    ops = pki.issue(name="ops@example.com")
    server = start_mtls(launcher=launcher, pki=pki, probe=ops, args=["--web-ui"])
    with server.http(identity=ops) as http:
        page = http.get(url="/ui/")
        assert page.status_code == 200 and '<div id="root"></div>' in page.text
        assert page.headers["content-security-policy"] == CSP
    with pytest.raises(httpx.HTTPError):
        with server.http(identity=None) as http:
            http.get(url="/ui/")


def test_under_mtls_certificate_sign_in_works_from_the_pages_origin(launcher, tmp_path):
    pki = Pki(directory=tmp_path)
    ops = pki.issue(name="ops@example.com")
    server = start_mtls(launcher=launcher, pki=pki, probe=ops,
                        args=["--web-ui", "--mtls-certificate-sign-in", "--mtls-admin-identities", "ops@example.com"])
    with server.http(identity=ops) as http:
        assert http.get(url="/ui/").status_code == 200
        discovery = http.get(url="/chargewire").json()
        assert "client_certificate" in discovery["auth"]["grant_types"]
        token = http.post(url="/chargewire/v1/auth/token", json={"grant_type": "client_certificate"}).json()["access_token"]
    # The page's later requests come over other connections of the same browser, presenting
    # the same certificate: the bound token works on them.
    session = server.client(identity=ops, token=token)
    assert session.run(sql="SELECT HARDCHARGER_USER() AS u").to_pylist() == [{"u": "ops@example.com"}]
    # Positive twin of the binding: another certificate can't use the token.
    other = pki.issue(name="other@example.com")
    with server.http(identity=other) as http:
        refused = http.post(url="/chargewire/v1/sessions", headers={"Authorization": f"Bearer {token}"}, json={})
    assert refused.status_code == 401 and refused.json()["code"] == "certificate_mismatch", refused.text


def test_with_optional_client_certificates_the_page_loads_without_one(launcher, tmp_path):
    pki = Pki(directory=tmp_path)
    server = start_mtls(launcher=launcher, pki=pki, probe=None, args=["--web-ui", "--mtls-require-client-cert=false"])
    with server.http(identity=None) as http:
        assert http.get(url="/ui/").status_code == 200
        token = http.post(url="/chargewire/v1/auth/token",
                          json={"grant_type": "password", "username": server.running.username,
                                "password": server.running.password})
    assert token.status_code == 200, token.text
