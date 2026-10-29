"""The server's table functions (hardcharger_settings(), hardcharger_sessions(), ...) inside
prepared statements.

A prepared statement is bound once and may run many times. Each run must read the rows from
the start and as they are then: the functions once built their rows at bind time, so a second
execution returned nothing and every execution showed the rows of the moment it was prepared.
"""

import pytest
from chargewire_client import read_table


@pytest.fixture(scope="module")
def admin(module_launcher):
    return module_launcher.start().admin()


def run(session, prepared_id: str, parameters=None) -> list[dict]:
    response = session.execute_prepared(prepared_id=prepared_id, parameters=parameters)
    assert response.status_code == 200, response.text
    return read_table(content=response.content).to_pylist()


def test_each_execution_reads_the_rows_from_the_start(admin):
    prepared = admin.prepare(sql="SELECT name FROM hardcharger_settings() WHERE name = ?")
    for name in ["hardcharger.query_timeout", "hardcharger.query_log_level", "hardcharger.query_timeout"]:
        assert run(session=admin, prepared_id=prepared["prepared_id"], parameters=[name]) == [{"name": name}]


def test_each_execution_sees_the_rows_as_they_are_then(admin, module_launcher):
    prepared = admin.prepare(sql="SELECT count(*) AS sessions FROM hardcharger_sessions()")
    before = run(session=admin, prepared_id=prepared["prepared_id"])[0]["sessions"]
    other = module_launcher.started[0].admin()  # one more session on the same server
    try:
        assert run(session=admin, prepared_id=prepared["prepared_id"])[0]["sessions"] == before + 1
    finally:
        other.close()


def test_a_direct_query_reads_the_rows_too(admin):
    # The positive twin of the prepared path: no prepare, one execution.
    assert admin.query(sql="SELECT name FROM hardcharger_settings() WHERE name = 'hardcharger.query_timeout'"
                       ).to_pylist() == [{"name": "hardcharger.query_timeout"}]
