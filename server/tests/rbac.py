"""Helpers for the access-control (RBAC) tests: a PostgreSQL grant store per server and a
small Warp SQL client with the methods the tests use.

The store comes from HARDCHARGER_TEST_RBAC_STORE_URI (default: the PostgreSQL the README
starts on 127.0.0.1:55433); each server gets its own fresh database on it. An unreachable
PostgreSQL fails the tests, never skips them.
"""

import dataclasses
import datetime
import io
import os
import secrets
import time
import urllib.parse

import httpx
import jwt
import psycopg
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest


STORE_URI = os.environ.get("HARDCHARGER_TEST_RBAC_STORE_URI",
                           "postgresql://hardcharger:hardcharger@127.0.0.1:55433/hardcharger")
SECRET_KEY = "hardcharger-rbac-tests-secret-key-0123456789"
ADMIN = "admin"
ARROW_STREAM = "application/vnd.apache.arrow.stream"
BLOCKED = "Hardcharger blocked"


# ---- Store ----

class Store:
    """Fresh PostgreSQL databases on the test server, one per Hardcharger server (or per
    cluster of them), dropped at the end."""

    def __init__(self):
        self.created: list[str] = []
        try:
            with psycopg.connect(conninfo=STORE_URI, autocommit=True, connect_timeout=5):
                pass
        except psycopg.Error as error:
            pytest.fail(reason=f"the RBAC tests need PostgreSQL at {redact(STORE_URI)} "
                               f"(set HARDCHARGER_TEST_RBAC_STORE_URI): {error}")

    def new_database(self) -> str:
        name = "hc_rbac_" + secrets.token_hex(nbytes=6)
        with psycopg.connect(conninfo=STORE_URI, autocommit=True) as connection:
            connection.execute(query=f'CREATE DATABASE "{name}"')
        self.created.append(name)
        parts = urllib.parse.urlsplit(STORE_URI)
        return urllib.parse.urlunsplit(parts._replace(path="/" + name))

    def drop_all(self) -> None:
        with psycopg.connect(conninfo=STORE_URI, autocommit=True) as connection:
            for name in self.created:
                connection.execute(query=f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def redact(uri: str) -> str:
    parts = urllib.parse.urlsplit(uri)
    if parts.password:
        parts = parts._replace(netloc=parts.netloc.replace(":" + parts.password + "@", ":***@"))
    return urllib.parse.urlunsplit(parts)


def rbac_env(store_uri: str, **extra: str) -> dict[str, str]:
    return {"HARDCHARGER_RBAC_STORE_URI": store_uri, "HARDCHARGER_SECRET_KEY": SECRET_KEY, "HARDCHARGER_ALLOW_CROSS_INSTANCE_TOKENS": "true",
            **extra}


def mint(user: str, role: str = "user", **claims) -> str:
    """An access token this server's secret key signs."""
    now = datetime.datetime.now(tz=datetime.UTC)
    return jwt.encode(payload={"iss": "hardcharger", "sub": user, "role": role, "iat": now,
                               "exp": now + datetime.timedelta(hours=1), **claims},
                      key=SECRET_KEY, algorithm="HS256")


# ---- Client ----

@dataclasses.dataclass
class WarpError(Exception):
    status: int
    code: str
    detail: str
    sqlstate: str | None = None

    def __str__(self) -> str:
        return f"{self.status} {self.code}: {self.detail}"


def raise_problem(response: httpx.Response) -> None:
    try:
        body = response.json()
    except ValueError:
        raise WarpError(status=response.status_code, code="?", detail=response.text) from None
    raise WarpError(status=response.status_code, code=body.get("code", "?"),
                    detail=body.get("detail", ""), sqlstate=body.get("sqlstate"))


def arrow_bytes(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with ipc.new_stream(sink=sink, schema=table.schema) as writer:
        writer.write_table(table=table)
    return sink.getvalue()


class Client:
    """One Warp SQL session for one user."""

    def __init__(self, base_url: str, token: str, options: dict | None = None):
        self.http = httpx.Client(base_url=base_url, timeout=120)
        self.token = token
        response = self.http.post(url="/warp/v1/sessions", headers=self.headers(),
                                  json={"options": options or {}})
        if response.status_code != 201:
            raise_problem(response=response)
        self.session_id = response.json()["session_id"]
        self.url = f"/warp/v1/sessions/{self.session_id}"

    def headers(self, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", **extra}

    def _outcome(self, response: httpx.Response) -> pa.Table:
        if response.status_code >= 400:
            raise_problem(response=response)
        if response.headers.get("content-type", "").startswith(ARROW_STREAM):
            return ipc.open_stream(source=io.BytesIO(initial_bytes=response.content)).read_all()
        doc = response.json()
        while doc["state"] not in ("succeeded", "failed", "cancelled"):
            doc = self.http.get(url=f"{self.url}/statements/{doc['statement_id']}",
                                headers=self.headers(Prefer="wait=5")).json()
        if doc["state"] != "succeeded":
            error = doc.get("error", {})
            raise WarpError(status=error.get("status", 0), code=error.get("code", doc["state"]),
                            detail=error.get("detail", ""), sqlstate=error.get("sqlstate"))
        if doc["result"]["kind"] == "rows":
            result = self.http.get(url=f"{self.url}/statements/{doc['statement_id']}/result",
                                   headers=self.headers())
            return self._outcome(response=result)
        return pa.table({"rows_affected": [doc["result"].get("rows_affected")]})

    def _send(self, url: str, **request) -> pa.Table:
        """POSTs a statement and reads its outcome. A statement that fails while its rows
        stream ends the body early (spec: no end-of-stream marker); its statement
        document then says why."""
        with self.http.stream(method="POST", url=url, **request) as response:
            try:
                response.read()
            except httpx.RemoteProtocolError:
                statement_id = response.headers.get("Warp-Statement-Id")
                if statement_id is None:
                    raise
                doc = self.wait(statement_id=statement_id)
                error = doc.get("error", {})
                raise WarpError(status=error.get("status", 0), code=error.get("code", doc["state"]),
                                detail=error.get("detail", ""), sqlstate=error.get("sqlstate")) from None
        return self._outcome(response=response)

    def run(self, sql: str, parameters=None) -> pa.Table:
        body: dict = {"sql": sql}
        if parameters is not None:
            body["parameters"] = parameters
        return self._send(url=f"{self.url}/statements", json=body, headers=self.headers(Accept=ARROW_STREAM))

    def submit(self, sql: str) -> str:
        """Starts a statement without waiting; returns its id."""
        response = self.http.post(url=f"{self.url}/statements", json={"sql": sql},
                                  headers=self.headers(Prefer="respond-async"))
        if response.status_code >= 400:
            raise_problem(response=response)
        return response.json()["statement_id"]

    def wait(self, statement_id: str, timeout: float = 60) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            response = self.http.get(url=f"{self.url}/statements/{statement_id}",
                                     headers=self.headers(Prefer="wait=2"))
            if response.status_code >= 400:
                raise_problem(response=response)
            doc = response.json()
            if doc["state"] in ("succeeded", "failed", "cancelled") or time.monotonic() > deadline:
                return doc

    def prepare(self, sql: str) -> str:
        response = self.http.post(url=f"{self.url}/prepared", json={"sql": sql}, headers=self.headers())
        if response.status_code != 201:
            raise_problem(response=response)
        return response.json()["prepared_id"]

    def execute_prepared(self, prepared_id: str, parameters=None, rows: pa.Table | None = None) -> pa.Table:
        url = f"{self.url}/prepared/{prepared_id}/execute"
        if rows is not None:
            return self._send(url=url, content=arrow_bytes(table=rows),
                              headers=self.headers(Accept=ARROW_STREAM, **{"Content-Type": ARROW_STREAM}))
        body = {} if parameters is None else {"parameters": parameters}
        return self._send(url=url, json=body, headers=self.headers(Accept=ARROW_STREAM))

    def metadata(self, kind: str, **params) -> pa.Table:
        response = self.http.get(url=f"{self.url}/metadata/{kind}", params=params, headers=self.headers())
        return self._outcome(response=response)

    def ingest(self, table: str, rows: pa.Table, mode: str = "create_append", **params) -> int:
        response = self.http.post(url=f"{self.url}/ingest", params={"table": table, "mode": mode, **params},
                                  content=arrow_bytes(table=rows),
                                  headers=self.headers(**{"Content-Type": ARROW_STREAM}))
        if response.status_code >= 400:
            raise_problem(response=response)
        return response.json()["rows_ingested"]

    def close(self) -> None:
        self.http.delete(url=self.url, headers=self.headers())
        self.http.close()


def expect_blocked(client: Client, sql: str, why: str = "", code: str = "permission_denied") -> WarpError:
    """The statement must be refused by access control, saying `why`."""
    with pytest.raises(WarpError) as raised:
        client.run(sql=sql)
    error = raised.value
    assert error.code == code, f"{sql}: {error}"
    if code == "permission_denied":
        assert BLOCKED in error.detail, f"{sql} failed, but not by access control: {error}"
        assert error.sqlstate == "42501", f"{sql}: {error}"
    assert why in error.detail, f"{sql}: expected '{why}' in: {error.detail}"
    return error


def expect_allowed(client: Client, sql: str, parameters=None) -> pa.Table:
    try:
        return client.run(sql=sql, parameters=parameters)
    except WarpError as error:
        pytest.fail(reason=f"WRONGLY BLOCKED: {sql} -> {error}")


def expect_error(call, code: str, why: str = "") -> WarpError:
    """`call()` must fail with the Warp SQL error `code`, saying `why`."""
    with pytest.raises(WarpError) as raised:
        call()
    assert raised.value.code == code, str(raised.value)
    assert why in raised.value.detail, f"expected '{why}' in: {raised.value.detail}"
    return raised.value


# ---- Servers ----

class RbacServer:
    """A running Hardcharger under access control, and clients for its users."""

    def __init__(self, running, store_uri: str):
        self.running = running
        self.base_url = running.base_url
        self.store_uri = store_uri
        self.clients: list[Client] = []

    def log(self) -> str:
        return self.running.log()

    def password_token(self, user: str, password: str) -> str:
        response = httpx.post(url=f"{self.base_url}/warp/v1/auth/token",
                              json={"grant_type": "password", "username": user, "password": password})
        if response.status_code != 200:
            raise_problem(response=response)
        return response.json()["access_token"]

    def admin(self) -> Client:
        return self.client(token=self.password_token(user=ADMIN, password=self.running.password))

    def user(self, name: str, password: str | None = None, role: str = "user", **claims) -> Client:
        """A session for a store user: by password sign-in, or with a server-signed token."""
        token = (self.password_token(user=name, password=password) if password is not None
                 else mint(user=name, role=role, **claims))
        return self.client(token=token)

    def client(self, token: str, options: dict | None = None) -> Client:
        client = Client(base_url=self.base_url, token=token, options=options)
        self.clients.append(client)
        return client


def start_rbac_server(launcher, store_uri: str, env: dict[str, str] | None = None,
                      args: list[str] | None = None) -> RbacServer:
    # Opening the store (PostgreSQL, the postgres extension) comes first: allow for a
    # busy machine.
    running = launcher.start(args=args, env={**rbac_env(store_uri=store_uri), **(env or {})},
                             startup_seconds=60)
    return RbacServer(running=running, store_uri=store_uri)
