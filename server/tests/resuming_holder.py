"""A stand-in for a GizmoData Cloud auto-resume holder (Chargewire §11.2, server_resuming):
it answers chosen requests with 503 server_resuming and a Retry-After, as a holder does
while the server behind it starts, and passes every other request through to that server.
With `stopped`, its discovery document says "state": "stopped" (§3.1) until the first
sign-in, as a holder's does for a stopped server; `requests` records every request."""

import http.client
import http.server
import json
import threading

PASSED_THROUGH = ("/chargewire", "/chargewire/", "/chargewire/v1/health")  # never held (§11.2)


class Holder:
    """A stand-in for an auto-resume holder in front of `upstream` (host, port)."""

    def __init__(self, upstream_port: int, retry_after: str = "1", resuming: int = 0,
                 statements_only: bool = False, detail: str | None = None, stale_transaction: bool = False,
                 lose_session_on: str | None = None, stopped: bool = False, expires_in: int | None = None):
        self.upstream_port = upstream_port
        self.retry_after = retry_after
        self.resuming = resuming  # answers server_resuming to this many requests; -1: to all
        self.statements_only = statements_only  # hold only statement submits
        self.detail = detail  # None: the spec's stock "the server is resuming; retry after N s"
        # Beyond resuming, for lost-session tests: session documents that say "transaction":
        # "none" whatever the server says (as servers before Chargewire 2.2.1 could right after
        # a BEGIN), and a statement containing this text answered session_not_found once.
        self.stale_transaction = stale_transaction
        self.lose_session_on = lose_session_on
        self.stopped = stopped  # discovery says "state": "stopped" until the first sign-in
        self.expires_in = expires_in  # rewrites a sign-in's expires_in (a token late in its life)
        # Every request, in order: (method, path, body, User-Agent).
        self.requests: list[tuple[str, str, bytes, str]] = []
        self.held: list[tuple[str, str, bytes]] = []
        self.lock = threading.Lock()
        holder = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):  # quiet
                pass

            def handle_any(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                path = self.path.split("?", 1)[0]
                with holder.lock:
                    holder.requests.append((self.command, path, body, self.headers.get("User-Agent") or ""))
                    if self.command == "POST" and path == "/chargewire/v1/auth/token":
                        holder.stopped = False  # a sign-in is what starts a stopped server
                    stopped = holder.stopped
                if holder.should_hold(method=self.command, path=path, body=body):
                    doc = json.dumps({"type": "urn:chargewire:error:server_resuming", "title": "Server resuming",
                                      "status": 503, "code": "server_resuming",
                                      "detail": holder.detail or f"the server is resuming; retry after {holder.retry_after} s",
                                      "request_id": "r_holder"}).encode()
                    self.send_response(503)
                    self.send_header("Content-Type", "application/problem+json")
                    if holder.retry_after:
                        self.send_header("Retry-After", holder.retry_after)
                    self.send_header("Content-Length", str(len(doc)))
                    self.end_headers()
                    self.wfile.write(doc)
                    return
                if (holder.lose_session_on and self.command == "POST" and path.endswith("/statements")
                        and holder.lose_session_on.encode() in body):
                    holder.lose_session_on = None  # once
                    doc = json.dumps({"type": "urn:chargewire:error:session_not_found", "title": "Session not found",
                                      "status": 404, "code": "session_not_found", "detail": "no such session"}).encode()
                    self.send_response(404)
                    self.send_header("Content-Type", "application/problem+json")
                    self.send_header("Content-Length", str(len(doc)))
                    self.end_headers()
                    self.wfile.write(doc)
                    return
                upstream = http.client.HTTPConnection(host="127.0.0.1", port=holder.upstream_port, timeout=60)
                headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection")}
                upstream.request(method=self.command, url=self.path, body=body, headers=headers)
                response = upstream.getresponse()
                data = response.read()
                parts = path.strip("/").split("/")
                if stopped and self.command == "GET" and path in ("/chargewire", "/chargewire/") and response.status == 200:
                    doc = json.loads(data)
                    doc["state"] = "stopped"
                    data = json.dumps(doc).encode()
                if (holder.expires_in is not None and self.command == "POST" and path == "/chargewire/v1/auth/token"
                        and response.status == 200):
                    doc = json.loads(data)
                    doc["expires_in"] = holder.expires_in
                    data = json.dumps(doc).encode()
                if (holder.stale_transaction and self.command == "GET" and len(parts) == 4
                        and parts[:3] == ["chargewire", "v1", "sessions"] and response.status == 200):
                    doc = json.loads(data)
                    doc["transaction"] = "none"
                    data = json.dumps(doc).encode()
                self.send_response(response.status)
                for k, v in response.getheaders():
                    if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                upstream.close()

            do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = handle_any

        self.server = http.server.ThreadingHTTPServer(server_address=("127.0.0.1", 0), RequestHandlerClass=Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def should_hold(self, method: str, path: str, body: bytes) -> bool:
        if path in PASSED_THROUGH:
            return False
        if self.statements_only and not (method == "POST" and path.endswith("/statements")):
            return False
        with self.lock:
            if self.resuming == 0:
                return False
            if self.resuming > 0:
                self.resuming -= 1
            self.held.append((method, path, body))
            return True

    def calls(self) -> list[tuple[str, str]]:
        """Every request so far, as (method, path)."""
        with self.lock:
            return [(method, path) for method, path, _, _ in self.requests]

    def sign_ins(self) -> int:
        return sum(1 for method, path in self.calls() if method == "POST" and path == "/chargewire/v1/auth/token")

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
