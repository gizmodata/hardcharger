"""Failed binding transactions stay local; ordinary sequence use remains valid."""

import pytest

from chargewire_client import read_table


@pytest.fixture(params=["memory", "file"])
def server(launcher, request, tmp_path):
    args = [] if request.param == "memory" else ["--database", str(tmp_path / "binding.duckdb")]
    return launcher.start(args=args)


@pytest.mark.parametrize("function", ["nextval('guard_seq')", "setval('guard_seq', 7)"])
@pytest.mark.parametrize("explicit", [False, True], ids=["autocommit", "transaction"])
def test_failed_binding_transaction_leaves_other_sessions_available(server, function, explicit):
    writer = server.as_role(role="user", subject="sequence-writer",
                            catalog_access=[{"catalog": "*", "access": "write"}])
    observer = server.admin()
    writer.execute(sql="CREATE SEQUENCE guard_seq")
    if explicit:
        writer.execute(sql="BEGIN")
    response = writer.http.post(url=f"{writer.url}/prepared", headers=writer.headers(),
                                json={"sql": f"SELECT * FROM range({function})"})
    if explicit and response.status_code == 201:
        problem = writer.error(sql="COMMIT")
    else:
        assert response.status_code == 422, response.text
        problem = response.json()
        if explicit:
            writer.execute(sql="ROLLBACK")
    assert problem["code"] == "execution_error", problem
    assert "Cannot commit changes made by statement binding" in problem["detail"], problem
    # Sequence values need not be reclaimed by rollback. Availability and ordinary
    # writes on both the existing connection and a fresh connection must survive.
    assert observer.scalar(sql="SELECT 42") == 42
    assert writer.scalar(sql="SELECT nextval('guard_seq')") >= 1
    fresh = server.admin()
    fresh.execute(sql="CREATE TABLE after_failed_bind (n INTEGER)")
    fresh.execute(sql="INSERT INTO after_failed_bind VALUES (43)")
    assert observer.scalar(sql="SELECT n FROM after_failed_bind") == 43


@pytest.mark.parametrize("temporary", [False, True], ids=["persistent", "temporary"])
@pytest.mark.parametrize("explicit", [False, True], ids=["autocommit", "transaction"])
def test_registered_sequence_writes_and_prepared_defaults(server, temporary, explicit):
    writer = server.admin()
    scope = "TEMP " if temporary else ""
    writer.execute(sql=f"CREATE {scope}SEQUENCE normal_seq")
    writer.execute(sql=f"CREATE {scope}TABLE generated_ids (id BIGINT DEFAULT nextval('normal_seq'))")
    if explicit:
        writer.execute(sql="BEGIN")
    prepared = writer.prepare(sql="SELECT nextval('normal_seq') AS n")
    assert writer.scalar(sql="SELECT last_value FROM duckdb_sequences() WHERE sequence_name = 'normal_seq'") is None
    response = writer.execute_prepared(prepared_id=prepared["prepared_id"])
    assert response.status_code == 200, response.text
    assert read_table(content=response.content).column(0).to_pylist() == [1]
    assert writer.scalar(sql="SELECT currval('normal_seq')") == 1
    assert writer.scalar(sql="SELECT setval('normal_seq', 10)") == 10
    insert = writer.prepare(sql="INSERT INTO generated_ids DEFAULT VALUES")
    response = writer.execute_prepared(prepared_id=insert["prepared_id"], arrow=False)
    assert response.status_code in (200, 201), response.text
    assert writer.scalar(sql="SELECT id FROM generated_ids") == 11
    if explicit:
        writer.execute(sql="COMMIT")
    assert writer.scalar(sql="SELECT nextval('normal_seq')") == 12
