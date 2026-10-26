"""Limits apply before IPC body allocation or decompression on every Arrow upload path."""

import io
import struct

import pyarrow as pa
import pyarrow.ipc as ipc
import pytest

from warp_client import ARROW_STREAM


def stream(table, compression=None, batches=1):
    sink = io.BytesIO()
    with ipc.new_stream(sink=sink, schema=table.schema, options=ipc.IpcWriteOptions(compression=compression)) as writer:
        for _ in range(batches):
            writer.write_table(table=table)
    return sink.getvalue()


def oversized_body():
    data = bytearray(stream(table=pa.table({"x": pa.array([0], type=pa.int64())})))
    frame = 8 + struct.unpack_from("<I", data, 4)[0]
    metadata = frame + 8
    table = metadata + struct.unpack_from("<I", data, metadata)[0]
    vtable = table - struct.unpack_from("<i", data, table)[0]
    field = struct.unpack_from("<H", data, vtable + 4 + 2 * 3)[0]
    assert field
    struct.pack_into("<q", data, table + field, 1 << 40)
    return bytes(data)


def upload(session, body, path):
    headers = session.headers(**{"Content-Type": ARROW_STREAM})
    if path == "ingest":
        return session.http.post(url=f"{session.url}/ingest", params={"table": "limited_upload", "mode": "create"},
                                 headers=headers, content=body)
    if path == "part":
        opened = session.http.post(url=f"{session.url}/ingests", headers=session.headers(),
                                   json={"table": "limited_upload", "mode": "create"})
        assert opened.status_code == 201, opened.text
        return session.http.post(url=f"{session.url}/ingests/{opened.json()['ingest_id']}/parts/0",
                                 headers=headers, content=body)
    prepared = session.prepare(sql="SELECT ?")
    return session.http.post(url=f"{session.url}/prepared/{prepared['prepared_id']}/execute",
                             headers={**headers, "Accept": ARROW_STREAM}, content=body)


@pytest.mark.parametrize("path", ["ingest", "part", "prepared"])
@pytest.mark.parametrize("payload", ["metadata", "body", "lz4", "zstd"])
def test_oversized_arrow_messages_get_a_clear_problem_and_leave_the_session_usable(launcher, path, payload):
    server = launcher.start(args=["--max-ingest-message-bytes", "4096"])
    session = server.admin()
    if payload == "metadata":
        body = struct.pack("<ii", -1, 0x7fffffff)
    elif payload == "body":
        body = oversized_body()
    else:
        body = stream(table=pa.table({"x": pa.array([0] * 600, type=pa.int64())}), compression=payload)
    assert len(body) < 4096
    response = upload(session=session, body=body, path=path)
    assert response.status_code == 413, response.text
    assert response.json()["code"] == "payload_too_large", response.text
    assert "max_ingest_message_bytes" in response.json()["detail"], response.text
    assert session.scalar(sql="SELECT 42") == 42


def test_upload_length_can_exceed_the_per_message_limit(launcher):
    session = launcher.start(args=["--max-ingest-message-bytes", "4096"]).admin()
    body = stream(table=pa.table({"x": pa.array([0] * 64, type=pa.int64())}), batches=100)
    assert len(body) > 4096
    response = upload(session=session, body=body, path="ingest")
    assert response.status_code == 200, response.text
    assert response.json()["rows_ingested"] == 6400


@pytest.mark.parametrize("payload", ["metadata", "body", "zstd"])
def test_later_ingest_parts_use_the_same_limit_and_roll_back_all_parts(launcher, payload):
    session = launcher.start(args=["--max-ingest-message-bytes", "4096"]).admin()
    opened = session.http.post(url=f"{session.url}/ingests", headers=session.headers(),
                               json={"table": "atomic_limited", "mode": "create"})
    assert opened.status_code == 201, opened.text
    url = f"{session.url}/ingests/{opened.json()['ingest_id']}"
    headers = session.headers(**{"Content-Type": ARROW_STREAM})
    first = session.http.post(url=f"{url}/parts/0", headers=headers,
                              content=stream(table=pa.table({"x": pa.array([1], type=pa.int64())})))
    assert first.status_code == 200, first.text
    if payload == "metadata":
        body = struct.pack("<ii", -1, 0x7fffffff)
    elif payload == "body":
        body = oversized_body()
    else:
        body = stream(table=pa.table({"x": pa.array([0] * 600, type=pa.int64())}), compression=payload)
    second = session.http.post(url=f"{url}/parts/1", headers=headers, content=body)
    assert second.status_code == 413, second.text
    assert second.json()["code"] == "payload_too_large"
    assert session.scalar(sql="SELECT count(*) FROM information_schema.tables WHERE table_name = 'atomic_limited'") == 0


@pytest.mark.parametrize("source", ["flag", "env", "yaml"])
def test_ingest_limit_is_configurable_and_visible_in_server_settings(launcher, tmp_path, source):
    args, env = [], {}
    if source == "flag":
        args = ["--max-ingest-message-bytes", "8192"]
    elif source == "env":
        env = {"HARDCHARGER_MAX_INGEST_MESSAGE_BYTES": "8192"}
    else:
        config = tmp_path / "limit.yaml"
        config.write_text("max_ingest_message_bytes: 8192\n")
        args = ["--config", str(config)]
    session = launcher.start(args=args, env=env).admin()
    setting = session.rows(sql="SELECT value, scope, settable FROM hardcharger_server_settings() "
                               "WHERE name = 'hardcharger.max_ingest_message_bytes'")[0]
    assert setting == {"value": "8192", "scope": "STARTUP", "settable": False}


@pytest.mark.parametrize("limit", ["0", "-1"])
def test_ingest_limit_must_be_positive(launcher, limit):
    server = launcher.start(args=["--max-ingest-message-bytes", limit], expect_up=False)
    assert server.process.wait(timeout=10) != 0
    assert "max_ingest_message_bytes must be positive" in server.log()
