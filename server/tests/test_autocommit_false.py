"""Port of GizmoSQL 1.x tests/test_autocommit_false.py (3 cases; issue #155).

DDL and DML with autocommit off (Chargewire §5.5: the session option autocommit false),
committed through the API (1.x: conn.commit(), here POST .../commit) or with SQL COMMIT,
and discarded through the API's rollback, after which the table does not exist. 1.x ran
these against a server on port 31337 through ADBC; here against a server the test starts.
"""

import uuid

import pytest


@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start()


def manual_session(server):
    session = server.admin()
    response = session.set_options(options={"autocommit": False})
    assert response.status_code == 200 and response.json()["options"]["autocommit"] is False, response.text
    return session


def count_rows(server, table: str) -> int:
    return server.admin().scalar(sql=f"SELECT count(*) FROM {table}")


def test_conn_commit_path(server):
    table = f"it155_conn_commit_{uuid.uuid4().hex[:8]}"
    session = manual_session(server=server)
    session.execute(sql=f"CREATE TABLE {table} (id INTEGER, name VARCHAR)")
    session.execute(sql=f"INSERT INTO {table} VALUES (1, 'alice'), (2, 'bob'), (3, 'carol')")
    assert session.http.post(url=f"{session.url}/commit", headers=session.headers()).status_code == 204
    assert count_rows(server=server, table=table) == 3


def test_sql_commit_path(server):
    table = f"it155_sql_commit_{uuid.uuid4().hex[:8]}"
    session = manual_session(server=server)
    session.execute(sql=f"CREATE TABLE {table} (id INTEGER, name VARCHAR)")
    session.execute(sql=f"INSERT INTO {table} VALUES (1, 'alice'), (2, 'bob'), (3, 'carol')")
    session.execute(sql="COMMIT")
    assert count_rows(server=server, table=table) == 3


def test_rollback_path(server):
    table = f"it155_rollback_{uuid.uuid4().hex[:8]}"
    session = manual_session(server=server)
    session.execute(sql=f"CREATE TABLE {table} (id INTEGER, name VARCHAR)")
    session.execute(sql=f"INSERT INTO {table} VALUES (1, 'alice')")
    assert session.http.post(url=f"{session.url}/rollback", headers=session.headers()).status_code == 204
    problem = server.admin().error(sql=f"SELECT count(*) FROM {table}")
    assert "does not exist" in problem["detail"], problem
