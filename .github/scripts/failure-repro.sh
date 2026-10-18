#!/usr/bin/env bash
# Runs the Warp SQL conformance cases for a statement that fails while it runs (streamed,
# split into partitions, prepared) 200 times against Hardcharger and against warp-duckdb.
# Each case accepts only the spec's answers: the error response (execution_error, "boom")
# or an Arrow stream that ends without its end-of-stream marker. Run from
# third_party/warp-sql/conformance, after its venv is set up.
#
#   failure-repro.sh <hardcharger-server> <warp-duckdb> <venv python>
set -uo pipefail

hardcharger=$1
warp_duckdb=$2
python=$3
count=${FAILURE_REPRO_COUNT:-200}
logs=${RUNNER_TEMP:-${TMPDIR:-/tmp}}

uv pip install --python "$python" pytest-repeat || exit 1
cases=(
  "tests/test_results.py::test_failure_mid_stream_leaves_the_stream_incomplete"
  "tests/test_results.py::test_a_failure_before_the_answer_is_an_error_response"
  "tests/test_results.py::test_a_failed_prepared_execution_is_never_a_success"
)

status=0
echo "== Hardcharger ($count times each)"
WARP_SQL_TEST_SERVER_BINARY="$hardcharger" \
WARP_SQL_TEST_SERVER_PASSWORD_ENV=HARDCHARGER_ADMIN_PASSWORD WARP_SQL_TEST_SERVER_USERNAME=warp \
WARP_SQL_TEST_SERVER_LOG="$logs/failure-repro-hardcharger.log" \
  "$python" -m pytest -q -rfE -p no:cacheprovider --count "$count" "${cases[@]}" || status=1
echo "== warp-duckdb ($count times each)"
WARP_SQL_TEST_SERVER_BINARY="$warp_duckdb" \
WARP_SQL_TEST_SERVER_LOG="$logs/failure-repro-warp-duckdb.log" \
  "$python" -m pytest -q -rfE -p no:cacheprovider --count "$count" "${cases[@]}" || status=1
exit $status
