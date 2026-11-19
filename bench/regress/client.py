"""The suite's one client: plain Chargewire over HTTP (httpx + pyarrow), the same code for every
server version, so a difference between two runs is the server's.

Every statement goes down the streamed path (POST .../statements with Accept: Arrow). A JSON
answer for a statement that has not finished yet (the server's wait ran out) is followed to its
end and its result read, as a conforming client does; that path exists in every release since
2.0. A failed statement raises StatementError with the server's problem document.
"""

import io
import json
import time

import httpx
import pyarrow as pa
import pyarrow.ipc as ipc

ARROW_STREAM = "application/vnd.apache.arrow.stream"
TERMINAL = ("succeeded", "failed", "cancelled")


class StatementError(RuntimeError):
    pass


class Session:
    """One signed-in principal with one server session."""

    def __init__(self, base_url: str, username: str, password: str, timeout_s: float = 900):
        self.http = httpx.Client(base_url=base_url, timeout=timeout_s)
        token = self.http.post(url="/chargewire/v1/auth/token",
                               json={"grant_type": "password", "username": username, "password": password})
        if token.status_code != 200:
            raise StatementError(f"sign-in failed: {token.status_code}: {token.text[:500]}")
        self.headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
        opened = self.http.post(url="/chargewire/v1/sessions", headers=self.headers, json={})
        if opened.status_code != 201:
            raise StatementError(f"opening a session failed: {opened.status_code}: {opened.text[:500]}")
        self.url = f"/chargewire/v1/sessions/{opened.json()['session_id']}"

    def close(self) -> None:
        try:
            self.http.delete(url=self.url, headers=self.headers)
        except httpx.HTTPError:
            pass
        self.http.close()

    def _wait_done(self, statement_id: str, timeout_s: float) -> dict:
        deadline = time.monotonic() + timeout_s
        while True:
            response = self.http.get(url=f"{self.url}/statements/{statement_id}",
                                     headers={**self.headers, "Prefer": "wait=5"})
            if response.status_code != 200:
                raise StatementError(f"status of {statement_id}: {response.status_code}: {response.text[:500]}")
            document = response.json()
            if document["state"] in TERMINAL:
                return document
            if time.monotonic() > deadline:
                raise StatementError(f"statement {statement_id} still {document['state']} after {timeout_s:.0f} s")

    def query(self, sql: str, timeout_s: float = 900) -> pa.Table | None:
        """Runs sql and returns its rows (None for a statement without a result set)."""
        response = self.http.post(url=f"{self.url}/statements", headers={**self.headers, "Accept": ARROW_STREAM},
                                  json={"sql": sql})
        content_type = response.headers.get("content-type", "")
        if response.status_code == 200 and content_type.startswith(ARROW_STREAM):
            return ipc.open_stream(source=io.BytesIO(initial_bytes=response.content)).read_all()
        if response.status_code in (200, 201) and content_type.startswith("application/json"):
            document = response.json()
            if document.get("state") not in TERMINAL:
                document = self._wait_done(statement_id=document["statement_id"], timeout_s=timeout_s)
            if document["state"] != "succeeded":
                raise StatementError(f"statement {document['state']}: {json.dumps(document.get('error'))[:1000]}")
            if (document.get("result") or {}).get("kind") == "update":
                return None
            result = self.http.get(url=f"{self.url}/statements/{document['statement_id']}/result",
                                   headers=self.headers)
            if result.status_code != 200:
                raise StatementError(f"result: {result.status_code}: {result.text[:500]}")
            return ipc.open_stream(source=io.BytesIO(initial_bytes=result.content)).read_all()
        raise StatementError(f"{response.status_code}: {response.text[:1000]}")

    def scalar(self, sql: str):
        table = self.query(sql=sql)
        if table is None or table.num_rows == 0:
            raise StatementError(f"no rows from {sql}")
        return table.column(0)[0].as_py()
