"""A sequence used while a statement is bound is a write like any other: registered where it
happens, committed with its transaction, refused where DuckDB refuses a write."""

import pytest

from chargewire_client import read_table


@pytest.fixture(params=["memory", "file"])
def server(launcher, request, tmp_path):
    args = [] if request.param == "memory" else ["--database", str(tmp_path / "binding.duckdb")]
    return launcher.start(args=args)


@pytest.mark.parametrize("function", ["nextval('guard_seq')", "setval('guard_seq', 7)"])
@pytest.mark.parametrize("explicit", [False, True], ids=["autocommit", "transaction"])
def test_a_statement_that_is_only_prepared_may_move_a_sequence_and_its_transaction_commits(server, function, explicit):
    # A statement that is only prepared is bound and never executed, and binding it advances or
    # sets the sequence (DuckDB evaluates a table function's arguments while it binds). The
    # sequence's use is registered as a write where it happens, so the transaction it is in
    # commits like any other: an earlier server answered 422 ("Cannot commit changes made by
    # statement binding") to the prepare with autocommit, and to the COMMIT in a transaction.
    writer = server.as_role(role="user", subject="sequence-writer",
                            catalog_access=[{"catalog": "*", "access": "write"}])
    observer = server.admin()
    writer.execute(sql="CREATE SEQUENCE guard_seq")
    writer.execute(sql="CREATE TABLE guard_rows (n INTEGER)")
    if explicit:
        writer.execute(sql="BEGIN")
        writer.execute(sql="INSERT INTO guard_rows VALUES (1)")
    response = writer.http.post(url=f"{writer.url}/prepared", headers=writer.headers(),
                                json={"sql": f"SELECT * FROM range({function})"})
    assert response.status_code == 201, response.text
    if explicit:
        assert transaction_of(writer) == "open"
        assert observer.scalar(sql="SELECT count(*) FROM guard_rows") == 0
        writer.execute(sql="COMMIT")
        assert observer.scalar(sql="SELECT count(*) FROM guard_rows") == 1
    assert observer.scalar(sql="SELECT last_value FROM duckdb_sequences() WHERE sequence_name = 'guard_seq'") >= 1
    # Availability and ordinary writes on both the existing connection and a fresh one.
    assert observer.scalar(sql="SELECT 42") == 42
    assert writer.scalar(sql="SELECT nextval('guard_seq')") >= 1
    fresh = server.admin()
    fresh.execute(sql="CREATE TABLE after_bind (n INTEGER)")
    fresh.execute(sql="INSERT INTO after_bind VALUES (43)")
    assert observer.scalar(sql="SELECT n FROM after_bind") == 43


def transaction_of(session) -> str:
    return session.http.get(url=session.url, headers=session.headers()).json()["transaction"]


ONE_DATABASE = "single transaction can only write to a single attached database"


@pytest.fixture
def two_databases(launcher, tmp_path):
    """A server with two attached databases: a table in `a`, a table and a sequence in `b`."""
    server = launcher.start(args=["--database", str(tmp_path / "first.duckdb")])
    admin = server.admin()
    for sql in (f"ATTACH '{tmp_path}/a.duckdb' AS a", f"ATTACH '{tmp_path}/b.duckdb' AS b", "CREATE TABLE a.main.t (i INTEGER)",
                "CREATE TABLE b.main.r (i INTEGER)", "CREATE SEQUENCE b.main.s"):
        admin.execute(sql=sql)
    return server


def sequence_in_b(session):
    return session.scalar(sql="SELECT last_value FROM system.main.duckdb_sequences() WHERE database_name = 'b' AND sequence_name = 's'")


@pytest.mark.parametrize("first", ["the write", "a read of the other database, then the write"])
@pytest.mark.parametrize("how", ["prepared", "sent to run and failing while it is bound"])
def test_a_binding_that_would_write_a_second_database_is_refused_before_the_sequence_moves(two_databases, first, how):
    # DuckDB writes one database per transaction. An earlier server let a statement that was
    # only bound move a sequence in `b` after the transaction had written `a`, and the COMMIT
    # then answered 422 after it had stored the INSERT into `a`, or without it, depending on
    # which database the transaction touched first. Now the sequence's use is refused where
    # it happens, with DuckDB's own error and before anything moves, and the transaction is
    # aborted like one in which any other statement failed: nothing of it is stored.
    session, observer = two_databases.admin(), two_databases.admin()
    session.execute(sql="BEGIN")
    if first != "the write":
        assert session.scalar(sql="SELECT count(*) FROM b.main.r") == 0
    session.execute(sql="INSERT INTO a.main.t VALUES (1)")
    if how == "prepared":
        response = session.http.post(url=f"{session.url}/prepared", headers=session.headers(),
                                     json={"sql": "SELECT * FROM range(nextval('b.main.s'))"})
        assert response.status_code == 422, response.text
        problem = response.json()
    else:
        problem = session.error(sql="SELECT * FROM range(nextval('b.main.s')), no_such_table_in_this_database")
    assert problem["code"] == "execution_error" and ONE_DATABASE in problem["detail"], problem
    assert sequence_in_b(observer) is None  # it did not move
    assert transaction_of(session) == "aborted"
    assert session.http.post(url=f"{session.url}/rollback", headers=session.headers()).status_code == 204
    assert transaction_of(session) == "none"
    assert observer.scalar(sql="SELECT count(*) FROM a.main.t") == 0
    # The twin: the same transaction without that statement is stored, on the same session.
    session.execute(sql="BEGIN")
    session.execute(sql="INSERT INTO a.main.t VALUES (2)")
    session.execute(sql="COMMIT")
    assert observer.scalar(sql="SELECT i FROM a.main.t") == 2
    assert observer.scalar(sql="SELECT nextval('b.main.s')") == 1  # the sequence and its database go on, from the start


def test_a_binding_first_makes_its_database_the_one_the_transaction_writes(two_databases):
    # The other order: the prepare's use of the sequence makes `b` the database this
    # transaction writes, and DuckDB refuses the INSERT into `a` with the same error, at the
    # INSERT (an earlier server accepted it, stored it at COMMIT, and answered 422 to that COMMIT).
    session, observer = two_databases.admin(), two_databases.admin()
    session.execute(sql="BEGIN")
    response = session.http.post(url=f"{session.url}/prepared", headers=session.headers(),
                                 json={"sql": "SELECT * FROM range(nextval('b.main.s'))"})
    assert response.status_code == 201, response.text
    problem = session.error(sql="INSERT INTO a.main.t VALUES (1)")
    assert ONE_DATABASE in problem["detail"], problem
    assert transaction_of(session) == "aborted"
    assert session.http.post(url=f"{session.url}/rollback", headers=session.headers()).status_code == 204
    assert observer.scalar(sql="SELECT count(*) FROM a.main.t") == 0
    # The twin: prepare and COMMIT alone store the sequence's movement, and nothing fails.
    session.execute(sql="BEGIN")
    assert session.http.post(url=f"{session.url}/prepared", headers=session.headers(),
                             json={"sql": "SELECT * FROM range(nextval('b.main.s'))"}).status_code == 201
    session.execute(sql="COMMIT")
    assert sequence_in_b(observer) >= 1


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
