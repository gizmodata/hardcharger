"""Uploads an Arrow IPC stream to Warp SQL's ingest endpoint over one raw (TLS) socket, in
4 MiB writes: what the server's ingest path takes, with no ADBC driver encoding the upload.

    python3 raw_ingest.py --url https://127.0.0.1:31337 --file data/lineitem_sf1.arrow \
        [--cacert tls.crt] [--times 3] [--compression zstd]

The file (an Arrow IPC file, as prepare_data.py writes) is turned into one IPC stream in
memory first; only the upload is timed. Each run replaces the table bench_raw_ingest.
"""

import argparse
import json
import socket
import ssl
import time
import urllib.parse

import pyarrow as pa

from raw_fetch import PASSWORD, api


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--url", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--cacert")
    parser.add_argument("--times", type=int, default=3)
    parser.add_argument("--compression", choices=["none", "zstd"], default="none")
    parser.add_argument("--parts", type=int, default=1,
                        help="upload as a partitioned ingest (spec §8.3) of this many parts at once")
    args = parser.parse_args()
    url = urllib.parse.urlparse(args.url)
    context = ssl.create_default_context(cafile=args.cacert) if url.scheme == "https" else None
    with pa.OSFile(args.file) as f:
        table = pa.ipc.open_file(f).read_all()
    sink = pa.BufferOutputStream()
    options = pa.ipc.IpcWriteOptions(compression=None if args.compression == "none" else "zstd")
    with pa.ipc.new_stream(sink, table.schema, options=options) as writer:
        for batch in table.to_batches():
            writer.write_batch(batch)
    body = memoryview(sink.getvalue())
    part_bodies = []
    if args.parts > 1:
        batches = table.to_batches()
        for p in range(args.parts):
            part_sink = pa.BufferOutputStream()
            with pa.ipc.new_stream(part_sink, table.schema, options=options) as writer:
                for batch in batches[p::args.parts]:
                    writer.write_batch(batch)
            part_bodies.append(memoryview(part_sink.getvalue()))
    token = api(args.url, context, "/warp/v1/auth/token",
                {"grant_type": "password", "username": "bench", "password": PASSWORD})["access_token"]
    session = api(args.url, context, "/warp/v1/sessions", {}, token)["session_id"]
    def send(path: str, payload, content_type: str) -> bytes:
        sock = socket.create_connection((url.hostname, url.port))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 << 20)
        if context is not None:
            sock = context.wrap_socket(sock, server_hostname=url.hostname)
        sock.sendall((f"POST {path} HTTP/1.1\r\nHost: {url.hostname}\r\nAuthorization: Bearer {token}\r\n"
                      f"Content-Type: {content_type}\r\nContent-Length: {len(payload)}\r\n"
                      f"Connection: close\r\n\r\n").encode())
        for offset in range(0, len(payload), 4 << 20):
            sock.sendall(payload[offset:offset + (4 << 20)])
        answer = b""
        while chunk := sock.recv(65536):
            answer += chunk
        sock.close()
        return answer

    if args.parts > 1:
        import concurrent.futures
        for _ in range(args.times):
            started = time.perf_counter()
            opened = api(args.url, context, f"/warp/v1/sessions/{session}/ingests",
                         {"table": "bench_raw_ingest", "mode": "replace"}, token)
            base = f"/warp/v1/sessions/{session}/ingests/{opened['ingest_id']}"
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.parts) as pool:
                answers = list(pool.map(lambda p: send(f"{base}/parts/{p}", part_bodies[p],
                                                       "application/vnd.apache.arrow.stream"),
                                        range(args.parts)))
            assert all(a.startswith(b"HTTP/1.1 200") for a in answers), [a[:200] for a in answers]
            committed = api(args.url, context, f"{base}/commit", {"parts": args.parts}, token)
            elapsed = time.perf_counter() - started
            print(f"{len(body)} bytes in {elapsed:.3f}s = {len(body) / elapsed / 1e9:.2f} GB/s over "
                  f"{args.parts} parts: {committed['rows_ingested']} rows", flush=True)
        return
    for _ in range(args.times):
        sock = socket.create_connection((url.hostname, url.port))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 << 20)
        if context is not None:
            sock = context.wrap_socket(sock, server_hostname=url.hostname)
        head = (f"POST /warp/v1/sessions/{session}/ingest?table=bench_raw_ingest&mode=replace HTTP/1.1\r\n"
                f"Host: {url.hostname}\r\nAuthorization: Bearer {token}\r\n"
                f"Content-Type: application/vnd.apache.arrow.stream\r\nContent-Length: {len(body)}\r\n"
                f"Connection: close\r\n\r\n").encode()
        started = time.perf_counter()
        sock.sendall(head)
        for offset in range(0, len(body), 4 << 20):
            sock.sendall(body[offset:offset + (4 << 20)])
        answer = b""
        while chunk := sock.recv(65536):
            answer += chunk
        elapsed = time.perf_counter() - started
        sock.close()
        status = answer.split(b"\r\n", 1)[0].decode()
        rows = json.loads(answer.split(b"\r\n\r\n", 1)[1] or b"{}").get("rows_ingested")
        print(f"{len(body)} bytes in {elapsed:.3f}s = {len(body) / elapsed / 1e9:.2f} GB/s: {status}, "
              f"{rows} rows", flush=True)


if __name__ == "__main__":
    main()
