"""HUGEINT and UHUGEINT: every value arrives exactly, or the statement says why it can't.

DuckDB sends both types as Arrow decimal128(38, 0) (unless the session sets
arrow_lossless_conversion), which holds 38 digits. DuckDB itself copies the 16 bytes as
they are, so before this a UHUGEINT of 2^127 or more arrived as a negative number
(UHUGEINT's maximum as -1) and any value of 10^38 or more as a decimal128(38, 0) that isn't
one. Now (server/scripts/patch-duckdb-arrow-hugeint.cmake) such a value fails the
statement with a conversion error naming the value and the two ways around it: CAST to
VARCHAR, or SET SESSION arrow_lossless_conversion = true (DuckDB's 16-byte Arrow types,
which hold every value; a plain SET of it is global). Every value that fits is sent exactly as before.

Bind parameters: a JSON integer outside the 64-bit range used to be read as a double and
bound rounded (10^38 - 1 became 1e38); it now binds exactly, as HUGEINT or UHUGEINT (as
DuckDB's own clients bind a large integer), and one outside 128 bits is refused. Arrow
parameters and bulk ingest carry decimal128(38, 0) exactly, refuse a decimal128 value
beyond its precision, and take DuckDB's lossless types back exactly.
"""

import decimal

import pyarrow as pa
import pytest

from warp_client import read_table

MAX_U = 2**128 - 1
MIN_H = -(2**127)
FITS = [0, 2**63, 2**64, 10**38 - 1]  # every one of these fits decimal128(38, 0)
TOO_WIDE_U = [10**38, 2**127 - 1, 2**127, MAX_U]
TOO_WIDE_H = [10**38, 2**127 - 1, -(10**38), MIN_H]


def lit(value: int, type_name: str) -> str:
    """The value as a SQL literal of the type (from text, so -2^127 is one literal)."""
    return f"CAST('{value}' AS {type_name})"


def refusal(type_name: str, value: int) -> str:
    return (f"Hardcharger cannot send the {type_name} value {value} as Arrow decimal128(38, 0), which holds at most "
            f"38 digits. CAST it to VARCHAR, or run SET SESSION arrow_lossless_conversion = true (DuckDB's 16-byte "
            f"{type_name} Arrow type, which holds every value)")


@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start()


@pytest.fixture
def session(server):
    return server.admin()


def lossless_value(raw: bytes | None, signed: bool) -> int | None:
    return None if raw is None else int.from_bytes(raw, byteorder="little", signed=signed)


@pytest.mark.parametrize("type_name", ["HUGEINT", "UHUGEINT"])
def test_every_value_that_fits_arrives_exactly(session, type_name):
    values = FITS + ([-(10**38 - 1), -(2**63) - 1] if type_name == "HUGEINT" else [])
    literals = ", ".join(f"({lit(v, type_name)})" for v in values)
    sql = f"SELECT x FROM (VALUES {literals}, (NULL::{type_name})) t(x)"
    table = session.query(sql=sql)  # streamed
    assert table.schema.field("x").type == pa.decimal128(38, 0)
    assert table.column("x").to_pylist() == [decimal.Decimal(v) for v in values] + [None]
    # The same through the statement document and its retained result.
    doc = session.execute(sql=sql)
    rows = read_table(content=session.result(statement_id=doc["statement_id"]).content)
    assert rows.column("x").to_pylist() == [decimal.Decimal(v) for v in values] + [None]


@pytest.mark.parametrize("type_name, value", [("UHUGEINT", v) for v in TOO_WIDE_U] +
                         [("HUGEINT", v) for v in TOO_WIDE_H])
def test_a_value_decimal128_cannot_hold_is_refused_never_sent_wrong(session, type_name, value):
    # The statement's error response (Warp SQL §6.1: a statement that has failed by the time
    # the server answers is its error, never a success status with a failed document).
    problem = session.error(sql=f"SELECT {lit(value, type_name)} AS x")
    assert problem["status"] == 500 and problem["code"] == "execution_error", problem
    assert "state" not in problem, problem  # an error response, not a statement document
    assert refusal(type_name=type_name, value=value) in problem["detail"], problem
    # The two ways around it, which carry the value exactly: VARCHAR,
    assert session.scalar(sql=f"SELECT {lit(value, type_name)}::VARCHAR") == str(value)
    # and DuckDB's lossless 16-byte type.
    assert session.set_options(options={"arrow_lossless_conversion": True}).status_code == 200
    raw = session.query(sql=f"SELECT {lit(value, type_name)} AS x").column("x").combine_chunks().storage[0].as_py()
    assert lossless_value(raw=raw, signed=type_name == "HUGEINT") == value


def test_one_value_too_wide_fails_the_whole_result_and_nested_values_are_checked(session):
    # A column of values that fit but one: the statement fails, rather than stream wrong rows.
    problem = session.error(sql=f"SELECT x FROM (VALUES (1::UHUGEINT), ({lit(MAX_U, 'UHUGEINT')}), (2::UHUGEINT)) t(x)")
    assert refusal(type_name="UHUGEINT", value=MAX_U) in problem["detail"], problem
    for sql in [f"SELECT [{lit(MAX_U, 'UHUGEINT')}] AS l", f"SELECT {{'k': {lit(MAX_U, 'UHUGEINT')}}} AS s",
                f"SELECT MAP {{1: {lit(MAX_U, 'UHUGEINT')}}} AS m"]:
        assert refusal(type_name="UHUGEINT", value=MAX_U) in session.error(sql=sql)["detail"], sql
    # The twin: nested values that fit arrive exactly.
    nested = session.query(sql=f"SELECT [{lit(10**38 - 1, 'UHUGEINT')}, NULL] AS l").column("l")[0].as_py()
    assert nested == [decimal.Decimal(10**38 - 1), None]


@pytest.mark.parametrize("type_name, signed", [("HUGEINT", True), ("UHUGEINT", False)])
def test_lossless_conversion_carries_every_boundary_value(session, type_name, signed):
    values = [0, 2**63, 2**64, 10**38 - 1, 10**38, 2**127 - 1] + (
        [MIN_H, -1] if signed else [2**127, MAX_U])
    assert session.set_options(options={"arrow_lossless_conversion": True}).status_code == 200
    literals = ", ".join(f"({lit(v, type_name)})" for v in values)
    column = session.query(sql=f"SELECT x FROM (VALUES {literals}, (NULL::{type_name})) t(x)").column("x")
    assert column.type.extension_name == "arrow.opaque" and column.type.type_name == type_name.lower()
    assert [lossless_value(raw=v, signed=signed) for v in column.combine_chunks().storage.to_pylist()] == values + [None]


BOUNDARIES = [0, 2**63, 2**64, 10**38 - 1, 10**38, 2**127 - 1, 2**127, MAX_U, -(2**63) - 1, MIN_H]


@pytest.mark.parametrize("value", BOUNDARIES)
def test_json_parameters_bind_wide_integers_exactly(session, value):
    rows = session.rows(sql="SELECT CAST(? AS VARCHAR) AS text, typeof(?) AS type", parameters=[value, value])
    expected_type = ("BIGINT" if -(2**63) <= value < 2**63 else "UBIGINT" if 0 <= value < 2**64
                     else "HUGEINT" if MIN_H <= value < 2**127 else "UHUGEINT")
    assert rows == [{"text": str(value), "type": expected_type}]
    # Into a column of its type, and back.
    column_type = "HUGEINT" if value < 0 else "UHUGEINT"
    session.execute(sql=f"CREATE OR REPLACE TEMP TABLE p (x {column_type})")
    session.execute(sql="INSERT INTO p VALUES (?)", parameters=[value])
    assert session.scalar(sql="SELECT x::VARCHAR FROM p") == str(value)
    # Named parameters too.
    assert session.scalar(sql="SELECT CAST($v AS VARCHAR)", parameters={"v": value}) == str(value)


def test_json_parameters_beyond_128_bits_are_refused(session):
    for value in [2**128, MIN_H - 1]:
        problem = session.error(sql="SELECT CAST(? AS VARCHAR)", parameters=[value])
        assert problem["status"] == 400 and problem["code"] == "bad_request", problem
        assert (f"the JSON parameter {value} is outside the 128-bit integer range DuckDB has: pass it as a string "
                "and CAST it in SQL") in problem["detail"], problem
    # The twins: as a string, and the widest that fits.
    assert session.scalar(sql="SELECT CAST(? AS VARCHAR)", parameters=[str(2**128)]) == str(2**128)
    assert session.scalar(sql="SELECT CAST(? AS VARCHAR)", parameters=[MAX_U]) == str(MAX_U)
    # A double stays a double, and a string that looks like the marker is just a string.
    assert session.scalar(sql="SELECT typeof(?)", parameters=[1e40]) == "DOUBLE"
    assert session.scalar(sql="SELECT ?", parameters=["\u0000integer"]) == "\u0000integer"


def test_arrow_parameters(session):
    prepared = session.prepare(sql="SELECT CAST(? AS UHUGEINT)::VARCHAR AS u")["prepared_id"]

    def run(array: pa.Array):
        response = session.execute_prepared(prepared_id=prepared, arrow_parameters=pa.table({"p": array}))
        if response.headers.get("content-type", "").startswith("application/vnd.apache.arrow.stream"):
            return read_table(content=response.content).column("u").to_pylist()
        return response.status_code, response.json()

    assert run(pa.array([decimal.Decimal(10**38 - 1)], type=pa.decimal128(38, 0))) == [str(10**38 - 1)]
    assert run(pa.array([str(MAX_U)])) == [str(MAX_U)]
    # A decimal128 holding more digits than its precision is not valid Arrow: refused.
    invalid = pa.Array.from_buffers(pa.decimal128(38, 0), 1,
                                    [None, pa.py_buffer((10**38).to_bytes(16, byteorder="little", signed=True))])
    status, problem = run(invalid)
    assert status == 415 and "does not fit in precision of decimal128(38, 0)" in problem["detail"], problem
    status, problem = run(pa.array([decimal.Decimal(MAX_U)], type=pa.decimal256(39, 0)))
    assert status == 415 and "Arrow parameters of type decimal256(39, 0) are not supported" in problem["detail"]


def test_bulk_ingest(session):
    session.execute(sql="CREATE OR REPLACE TABLE wide_in (u UHUGEINT, h HUGEINT)")
    rows = pa.table({"u": pa.array([decimal.Decimal(10**38 - 1), None], type=pa.decimal128(38, 0)),
                     "h": pa.array([decimal.Decimal(-(10**38 - 1)), decimal.Decimal(0)], type=pa.decimal128(38, 0))})
    assert session.ingest(table=rows, name="wide_in", mode="append").status_code == 200
    # DuckDB's lossless types, as a lossless query returns them, carry the extremes.
    assert session.set_options(options={"arrow_lossless_conversion": True}).status_code == 200
    extremes = session.query(sql=f"SELECT {lit(MAX_U, 'UHUGEINT')} AS u, {lit(MIN_H, 'HUGEINT')} AS h")
    assert session.ingest(table=extremes, name="wide_in", mode="append").status_code == 200
    assert session.set_options(options={"arrow_lossless_conversion": False}).status_code == 200
    assert session.rows(sql="SELECT u::VARCHAR AS u, h::VARCHAR AS h FROM wide_in ORDER BY h") == [
        {"u": str(MAX_U), "h": str(MIN_H)}, {"u": str(10**38 - 1), "h": str(-(10**38 - 1))}, {"u": None, "h": "0"}]
    # A decimal128 beyond its precision is refused, and so is decimal256, which DuckDB
    # can't import: never stored wrong.
    invalid = pa.Array.from_buffers(pa.decimal128(38, 0), 1,
                                    [None, pa.py_buffer((10**38).to_bytes(16, byteorder="little", signed=True))])
    response = session.ingest(table=pa.table({"u": invalid, "h": pa.array([0], type=pa.decimal128(38, 0))}),
                              name="wide_in", mode="append")
    assert response.status_code == 415 and "does not fit in precision" in response.json()["detail"], response.text
    response = session.ingest(table=pa.table({"u": pa.array([decimal.Decimal(MAX_U)], type=pa.decimal256(39, 0)),
                                              "h": pa.array([0], type=pa.decimal128(38, 0))}),
                              name="wide_in", mode="append")
    assert response.status_code == 400 and "Unsupported Internal Arrow Type for Decimal d:39,0,256" in response.text
    assert session.scalar(sql="SELECT count(*) FROM wide_in") == 3


def test_sums_of_bigints_are_hugeints_that_arrive_exactly(session):
    # The everyday HUGEINT: SUM over BIGINT. Unchanged.
    total = session.query(sql=f"SELECT sum(x) AS s FROM (VALUES ({2**62}::BIGINT), ({2**62}::BIGINT), (1::BIGINT)) t(x)")
    assert total.schema.field("s").type == pa.decimal128(38, 0)
    assert total.column("s")[0].as_py() == decimal.Decimal(2**63 + 1)


def test_arrow_lossless_conversion_is_one_sessions_own(server):
    # The session option used to run a plain SET, which for this setting changes the
    # instance's default: one session (any user's) switching it changed every other
    # session's HUGEINT, UUID and JSON results, while their options still said false.
    other = server.admin()
    user = server.as_role(role="user", subject="lossless-user")
    assert user.set_options(options={"arrow_lossless_conversion": True}).status_code == 200
    assert user.query(sql="SELECT 1::HUGEINT AS x").schema.field("x").type.extension_name == "arrow.opaque"
    for session in [other, server.admin()]:  # one open before, one opened after
        assert session.query(sql="SELECT 1::HUGEINT AS x").schema.field("x").type == pa.decimal128(38, 0)
        assert session.options()["arrow_lossless_conversion"] is False
    # Switching it back is the session's own too.
    assert user.set_options(options={"arrow_lossless_conversion": False}).status_code == 200
    assert user.query(sql="SELECT 1::HUGEINT AS x").schema.field("x").type == pa.decimal128(38, 0)


def lossless_in(session) -> bool:
    """Whether `session`'s results carry DuckDB's lossless HUGEINT (else decimal128(38, 0))."""
    field_type = session.query(sql="SELECT 1::HUGEINT AS x").schema.field("x").type
    assert field_type in (pa.decimal128(38, 0),) or field_type.extension_name == "arrow.opaque", field_type
    return field_type != pa.decimal128(38, 0)


def reported(session) -> bool:
    """The session document's arrow_lossless_conversion, read again (an empty PATCH)."""
    response = session.set_options(options={})
    assert response.status_code == 200, response.text
    return response.json()["options"]["arrow_lossless_conversion"]


def test_set_session_is_the_sessions_own_and_a_plain_set_is_global(launcher):
    """What the refusal's advice and the settings reference say: SET SESSION changes one
    session; a plain SET of arrow_lossless_conversion is DuckDB's SET GLOBAL (the option's
    default scope), which only an administrator may run, and it reaches every session that
    has no value of its own. The session option reports the value the session's results
    use, also after SQL changed it."""
    server = launcher.start()
    one, other = server.admin(), server.admin()
    user = server.as_role(role="user", subject="plain-set-user")
    # SET SESSION in SQL: this session only, and its option says so.
    one.execute(sql="SET SESSION arrow_lossless_conversion = true")
    assert lossless_in(session=one) and one.options()["arrow_lossless_conversion"] is True
    for session in (other, user):
        assert not lossless_in(session=session) and session.options()["arrow_lossless_conversion"] is False
    # A non-administrator's plain SET is a global one, and refused.
    refused = user.error(sql="SET arrow_lossless_conversion = true")
    assert refused["detail"].startswith("Permission Error: Hardcharger blocked this statement: SET GLOBAL needs the "
                                        "MANAGE CONFIG privilege, which user plain-set-user does not hold"), refused
    assert not lossless_in(session=other)
    # An administrator's plain SET changes the instance's default: every session without a
    # value of its own (one open before, one opened after).
    other.execute(sql="SET arrow_lossless_conversion = true")
    later = server.admin()
    for session in (user, later, other):
        assert lossless_in(session=session) and reported(session=session) is True
    # A session's own value still wins over the default.
    assert user.set_options(options={"arrow_lossless_conversion": False}).status_code == 200
    assert not lossless_in(session=user) and user.options()["arrow_lossless_conversion"] is False
    one.execute(sql="SET SESSION arrow_lossless_conversion = false")
    assert not lossless_in(session=one) and one.options()["arrow_lossless_conversion"] is False
    # RESET GLOBAL brings back the default (false) for those without their own.
    other.execute(sql="RESET GLOBAL arrow_lossless_conversion")
    assert not lossless_in(session=later) and reported(session=later) is False
