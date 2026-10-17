"""A small Warp SQL client for Hardcharger's tests: plain HTTP calls, so a failing test
points at the server, not at a driver. GizmoSQL 1.x's battery used a Flight SQL
client; the ported tests use this one through the same few verbs (sign in, open a
session, run SQL, read rows, prepare, ingest, read metadata)."""

import datetime
import io
import time

import httpx
import jwt
import pyarrow as pa
import pyarrow.ipc as ipc

ARROW_STREAM = "application/vnd.apache.arrow.stream"
TERMINAL = ("succeeded", "failed", "cancelled")


def read_table(content: bytes) -> pa.Table:
    return ipc.open_stream(source=io.BytesIO(initial_bytes=content)).read_all()


def read_batches(content: bytes) -> list[pa.RecordBatch]:
    return list(ipc.open_stream(source=io.BytesIO(initial_bytes=content)))


def ipc_bytes(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with ipc.new_stream(sink=sink, schema=table.schema) as writer:
        writer.write_table(table=table)
    return sink.getvalue()


def mint_server_token(secret_key: str, subject: str = "admin", role: str = "admin",
                      issuer: str = "gizmosql", lifetime_s: int = 3600, **claims) -> str:
    """An access token in the server's own format (HS256 over secret_key), as GizmoSQL
    1.x tooling mints them; 1.x's tests used these to act as non-admin principals."""
    now = datetime.datetime.now(tz=datetime.UTC)
    payload = {"iss": issuer, "sub": subject, "role": role, "auth_method": "Basic",
               "iat": now, "exp": now + datetime.timedelta(seconds=lifetime_s), **claims}
    return jwt.encode(payload=payload, key=secret_key, algorithm="HS256")


class WarpError(AssertionError):
    pass


class Client:
    """One principal: a bearer token, and helpers that act with it."""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url
        self.http = httpx.Client(base_url=base_url, timeout=120)
        self.token = token

    def close(self) -> None:
        self.http.close()

    def headers(self, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", **extra}

    # ---- Sessions ----

    def open_session(self, options: dict | None = None) -> httpx.Response:
        return self.http.post(url="/warp/v1/sessions", headers=self.headers(),
                              json={"options": options or {}})

    def new_session(self, options: dict | None = None) -> "Session":
        response = self.open_session(options=options)
        assert response.status_code == 201, response.text
        return Session(client=self, session_id=response.json()["session_id"])


class Session:
    def __init__(self, client: Client, session_id: str):
        self.client = client
        self.id = session_id
        self.url = f"/warp/v1/sessions/{session_id}"

    @property
    def http(self) -> httpx.Client:
        return self.client.http

    def headers(self, **extra: str) -> dict[str, str]:
        return self.client.headers(**extra)

    # ---- Statements ----

    def submit(self, sql: str, parameters=None, arrow: bool = False,
               headers: dict[str, str] | None = None, timeout: float = 120) -> httpx.Response:
        body: dict = {"sql": sql}
        if parameters is not None:
            body["parameters"] = parameters
        all_headers = self.headers(**(headers or {}))
        if arrow:
            all_headers["Accept"] = ARROW_STREAM
        return self.http.post(url=f"{self.url}/statements", headers=all_headers, json=body,
                              timeout=timeout)

    def status(self, statement_id: str, wait: int | None = None) -> dict:
        headers = self.headers()
        if wait is not None:
            headers["Prefer"] = f"wait={wait}"
        response = self.http.get(url=f"{self.url}/statements/{statement_id}", headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    def wait_done(self, statement_id: str, timeout: float = 120) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            doc = self.status(statement_id=statement_id, wait=5)
            if doc["state"] in TERMINAL:
                return doc
            assert time.monotonic() < deadline, f"statement still {doc['state']} after {timeout}s"

    def result(self, statement_id: str, from_batch: int | None = None) -> httpx.Response:
        params = {} if from_batch is None else {"from_batch": str(from_batch)}
        return self.http.get(url=f"{self.url}/statements/{statement_id}/result", params=params,
                             headers=self.headers())

    def query(self, sql: str, parameters=None, headers: dict[str, str] | None = None) -> pa.Table:
        """Runs SQL on the streamed path and returns its rows; raises WarpError with the
        problem document's detail when the statement fails."""
        response = self.submit(sql=sql, parameters=parameters, arrow=True, headers=headers)
        if response.status_code != 200 or not response.headers.get("content-type", "").startswith(ARROW_STREAM):
            if response.headers.get("content-type", "").startswith("application/json"):
                doc = response.json()
                if doc.get("state") == "succeeded":
                    raise WarpError(f"statement returned no rows: {doc}")
                if "statement_id" in doc and doc.get("state") not in TERMINAL:
                    doc = self.wait_done(statement_id=doc["statement_id"])
                    if doc["state"] == "succeeded":
                        return read_table(content=self.result(statement_id=doc["statement_id"]).content)
            raise WarpError(f"{response.status_code}: {response.text}")
        return read_table(content=response.content)

    def rows(self, sql: str, parameters=None) -> list[dict]:
        return self.query(sql=sql, parameters=parameters).to_pylist()

    def scalar(self, sql: str, parameters=None):
        table = self.query(sql=sql, parameters=parameters)
        assert table.num_rows >= 1, f"no rows from {sql}"
        return table.column(0)[0].as_py()

    def execute(self, sql: str, parameters=None, headers: dict[str, str] | None = None) -> dict:
        """Runs SQL to its end; returns the statement document. Raises WarpError on failure."""
        response = self.submit(sql=sql, parameters=parameters, headers=headers)
        if response.status_code not in (200, 201):
            raise WarpError(f"{response.status_code}: {response.text}")
        doc = response.json()
        if doc["state"] not in TERMINAL:
            doc = self.wait_done(statement_id=doc["statement_id"])
        if doc["state"] != "succeeded":
            raise WarpError(f"statement {doc['state']}: {doc}")
        return doc

    def error(self, sql: str, parameters=None) -> dict:
        """Runs SQL that must fail; returns the problem (code, detail, status)."""
        response = self.submit(sql=sql, parameters=parameters)
        if response.status_code in (200, 201):
            doc = response.json()
            if doc["state"] not in TERMINAL:
                doc = self.wait_done(statement_id=doc["statement_id"])
            if doc["state"] == "succeeded":
                raise WarpError(f"expected {sql!r} to fail, but it succeeded")
            problem = dict(doc.get("error") or {})
            problem["status"] = response.status_code
            problem.setdefault("detail", str(doc))
            problem["state"] = doc["state"]
            return problem
        problem = response.json()
        problem["status"] = response.status_code
        return problem

    # ---- Prepared statements ----

    def prepare(self, sql: str) -> dict:
        response = self.http.post(url=f"{self.url}/prepared", headers=self.headers(), json={"sql": sql})
        assert response.status_code == 201, response.text
        return response.json()

    def execute_prepared(self, prepared_id: str, parameters=None, arrow_parameters: pa.Table | None = None,
                         arrow: bool = True) -> httpx.Response:
        headers = self.headers()
        if arrow:
            headers["Accept"] = ARROW_STREAM
        if arrow_parameters is not None:
            headers["Content-Type"] = ARROW_STREAM
            return self.http.post(url=f"{self.url}/prepared/{prepared_id}/execute", headers=headers,
                                  content=ipc_bytes(table=arrow_parameters))
        body = {} if parameters is None else {"parameters": parameters}
        return self.http.post(url=f"{self.url}/prepared/{prepared_id}/execute", headers=headers, json=body)

    # ---- Ingest and metadata ----

    def ingest(self, table: pa.Table, name: str, mode: str = "create_append", temporary: bool = False,
               catalog: str | None = None, schema: str | None = None) -> httpx.Response:
        params = {"table": name, "mode": mode, "temporary": "true" if temporary else "false"}
        if catalog is not None:
            params["catalog"] = catalog
        if schema is not None:
            params["schema"] = schema
        return self.http.post(url=f"{self.url}/ingest", params=params,
                              headers=self.headers(**{"Content-Type": ARROW_STREAM}),
                              content=ipc_bytes(table=table))

    def metadata(self, kind: str, **params) -> pa.Table:
        response = self.http.get(url=f"{self.url}/metadata/{kind}", headers=self.headers(),
                                 params={k: v for k, v in params.items() if v is not None})
        assert response.status_code == 200, response.text
        if kind == "server-info":
            return response.json()
        return read_table(content=response.content)

    # ---- Session lifecycle ----

    def options(self) -> dict:
        response = self.http.get(url=self.url, headers=self.headers())
        assert response.status_code == 200, response.text
        return response.json()["options"]

    def set_options(self, options: dict) -> httpx.Response:
        return self.http.patch(url=self.url, headers=self.headers(), json={"options": options})

    def close(self) -> httpx.Response:
        return self.http.delete(url=self.url, headers=self.headers())
