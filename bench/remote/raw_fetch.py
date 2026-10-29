"""Fetches a Chargewire result over one raw (TLS) socket and drops the bytes: what one
connection can carry from the server, with no Arrow decoding and no ADBC driver.

    python3 raw_fetch.py --url https://127.0.0.1:31337 --sql "SELECT * FROM sf1.lineitem" \
        [--cacert tls.crt] [--times 3] [--partitions N]

curl does the same (raw_fetch.sh), but the macOS curl is an x86_64 binary on LibreSSL that
decrypts slower than the server encrypts; this one uses Python's OpenSSL 3 and reads in
4 MiB pieces. With --partitions N it asks for a split result (spec §6.10) and reads
every partition at once, one connection each.
"""

import argparse
import json
import os
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request

PASSWORD = os.environ.get("BENCH_PASSWORD", "bench-password-not-secret")
END = b"\r\n0\r\n\r\n"  # the last chunk of a chunked body (a complete response)


def api(base: str, context, path: str, body: dict | None, token: str | None = None) -> dict:
    request = urllib.request.Request(url=base + path, data=json.dumps(body or {}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, context=context) as response:
        return json.loads(response.read())


def open_request(url, context, request: bytes):
    """Sends one request and reads its response head: (socket, head, body bytes so far)."""
    sock = socket.create_connection((url.hostname, url.port))
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    if context is not None:
        sock = context.wrap_socket(sock, server_hostname=url.hostname)
    sock.sendall(request)
    data = b""
    while b"\r\n\r\n" not in data:
        more = sock.recv(65536)
        if not more:
            raise RuntimeError("the server closed the connection before answering")
        data += more
    head, rest = data.split(b"\r\n\r\n", 1)
    return sock, head, rest


def drain(sock, rest: bytes) -> tuple[int, bool]:
    """Reads the rest of the body; returns its size and whether it ended with the last chunk."""
    buffer = bytearray(4 << 20)
    view = memoryview(buffer)
    total = len(rest)
    tail = rest[-8:]
    while True:
        got = sock.recv_into(view)
        if got == 0:
            break
        total += got
        tail = (tail + bytes(view[max(0, got - 8):got]))[-8:]
    sock.close()
    return total, tail.endswith(END[-5:])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--url", required=True)
    parser.add_argument("--sql", required=True)
    parser.add_argument("--cacert")
    parser.add_argument("--times", type=int, default=3)
    parser.add_argument("--partitions", type=int, default=1)
    args = parser.parse_args()
    url = urllib.parse.urlparse(args.url)
    context = None
    if url.scheme == "https":
        context = ssl.create_default_context(cafile=args.cacert)
    token = api(args.url, context, "/chargewire/v1/auth/token",
                {"grant_type": "password", "username": "bench", "password": PASSWORD})["access_token"]
    session = api(args.url, context, "/chargewire/v1/sessions", {}, token)["session_id"]
    body = json.dumps({"sql": args.sql}).encode()
    for _ in range(args.times):
        started = time.perf_counter()
        extra = f"Chargewire-Result-Partitions: {args.partitions}\r\n" if args.partitions > 1 else ""
        request = (f"POST /chargewire/v1/sessions/{session}/statements HTTP/1.1\r\nHost: {url.hostname}\r\n"
                   f"Authorization: Bearer {token}\r\nAccept: application/vnd.apache.arrow.stream\r\n"
                   f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n{extra}"
                   f"Connection: close\r\n\r\n").encode() + body
        sock, head, rest = open_request(url, context, request)
        lower = {k.lower(): v for k, v in (line.split(": ", 1) for line in head.decode().split("\r\n")[1:] if ": " in line)}
        k = int(lower.get("chargewire-result-partitions", "1"))
        results = []

        def fetch(i: int) -> None:
            get = (f"GET /chargewire/v1/sessions/{session}/statements/{lower['chargewire-statement-id']}/result"
                   f"?partition={i} HTTP/1.1\r\nHost: {url.hostname}\r\nAuthorization: Bearer {token}\r\n"
                   f"Connection: close\r\n\r\n").encode()
            results.append(drain(*open_request(url, context, get)[::2]))

        # Every partition is read at once (spec §6.10), partition 0 on this thread.
        threads = [threading.Thread(target=fetch, args=(i,)) for i in range(1, k)]
        for t in threads:
            t.start()
        results.append(drain(sock, rest))
        for t in threads:
            t.join()
        elapsed = time.perf_counter() - started
        size = sum(n for n, _ in results)
        print(f"{size} bytes in {elapsed:.3f}s = {size / elapsed / 1e9:.2f} GB/s over {k} "
              f"connection(s), {'complete' if all(ok for _, ok in results) else 'INCOMPLETE'}", flush=True)


if __name__ == "__main__":
    main()
