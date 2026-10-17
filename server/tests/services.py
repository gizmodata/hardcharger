"""The external services some suites need (server/tests/docker-compose.yml): where they
are, and a check that fails the test when they are not there."""

import os
import socket

import pytest

PG_HOST = "127.0.0.1"
PG_PORT = int(os.environ.get("HARDCHARGER_TEST_PG_PORT", "55440"))
PG_USER = os.environ.get("HARDCHARGER_TEST_PG_USER", "postgres")
PG_PASSWORD = os.environ.get("HARDCHARGER_TEST_PG_PASSWORD", "testpassword")
PG_DATABASE = os.environ.get("HARDCHARGER_TEST_PG_DATABASE", "ducklake_catalog")
S3_PORT = int(os.environ.get("HARDCHARGER_TEST_S3_PORT", "59000"))

START = "start them with: docker compose -f server/tests/docker-compose.yml up -d --wait"


def require(port: int, what: str) -> None:
    with socket.socket() as sock:
        sock.settimeout(2)
        if sock.connect_ex((PG_HOST, port)) != 0:
            pytest.fail(reason=f"{what} is not reachable on 127.0.0.1:{port}; {START}")


def pg_conninfo(dbname: str = PG_DATABASE) -> str:
    return f"host={PG_HOST} port={PG_PORT} dbname={dbname} user={PG_USER} password={PG_PASSWORD}"
