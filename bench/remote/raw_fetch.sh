#!/usr/bin/env bash
# Fetches a Warp SQL result with curl into /dev/null, with no ADBC driver involved, to tell
# a slow server from a slow client (Warp SQL's one-round-trip path: POST the statement with
# Accept: application/vnd.apache.arrow.stream).
#
#   raw_fetch.sh https://172.31.41.94:31337 "SELECT * FROM sf1.lineitem" [times] [curl args...]
set -euo pipefail
BASE=${1:?base URL}/warp/v1
SQL=${2:?SQL}
TIMES=${3:-3}
shift 3 || shift $#
C=(curl -fsS "$@")
PASSWORD=${BENCH_PASSWORD:-bench-password-not-secret}
TOKEN=$("${C[@]}" -X POST "$BASE/auth/token" -H 'Content-Type: application/json' \
  -d "{\"grant_type\":\"password\",\"username\":\"bench\",\"password\":\"$PASSWORD\"}" |
  python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])')
SESSION=$("${C[@]}" -X POST "$BASE/sessions" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{}' |
  python3 -c 'import json, sys; print(json.load(sys.stdin)["session_id"])')
BODY=$(python3 -c 'import json, sys; print(json.dumps({"sql": sys.argv[1]}))' "$SQL")
for _ in $(seq 1 "$TIMES"); do
  "${C[@]}" -o /dev/null -w '%{size_download} bytes in %{time_total}s = %{speed_download} B/s\n' \
    -X POST "$BASE/sessions/$SESSION/statements" -H "Authorization: Bearer $TOKEN" \
    -H 'Accept: application/vnd.apache.arrow.stream' -H 'Content-Type: application/json' \
    -d "$BODY"
done
