"""Port of GizmoSQL 1.x tests/integration/test_max_metadata_size.cpp (4 cases).

1.x capped gRPC metadata (--max-metadata-size, GIZMOSQL_MAX_METADATA_SIZE). Hardcharger
caps request header blocks (max_header_bytes, default 16 KiB) and accepts the 1.x flag
and variable as aliases; an oversized block gets 431 headers_too_large (Warp SQL §2.5).
Because the default is 16 KiB rather than gRPC's 8 KiB, the "raised" cases send 40 KiB.
All 4 apply.
"""

import httpx

from conftest import problem


def probe(server, header_bytes: int) -> httpx.Response:
    token = server.token()
    return httpx.post(url=f"{server.base_url}/warp/v1/sessions",
                      headers={"Authorization": f"Bearer {token}", "X-Custom-Header": "x" * header_bytes}, json={})


def test_small_header_succeeds(launcher):
    assert probe(server=launcher.start(), header_bytes=1024).status_code == 201


def test_oversized_header_rejected(launcher):
    problem(response=probe(server=launcher.start(), header_bytes=40 * 1024), status=431, code="headers_too_large")


def test_oversized_header_accepted_when_limit_raised(launcher):
    server = launcher.start(args=["--max-metadata-size", "65536"])
    assert probe(server=server, header_bytes=40 * 1024).status_code == 201


def test_env_var_raises_limit(launcher):
    server = launcher.start(env={"GIZMOSQL_MAX_METADATA_SIZE": "65536"})
    assert probe(server=server, header_bytes=40 * 1024).status_code == 201
