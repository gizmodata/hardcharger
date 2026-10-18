#!/usr/bin/env bash
# Installs a Linux release tarball and checks it runs: --version, outbound TLS against
# the system's CA bundle (--verify-tls), then a server whose health endpoint answers and
# which runs a query through the Warp SQL endpoints (sign-in, session, statement, Arrow
# result), an HTTPS read inside DuckDB (httpfs), and the tarball's command-line client. Meant for a clean old-glibc container (CI: oraclelinux:8, glibc 2.28):
#
#   docker run --rm -v "$PWD/dist:/dist:ro" oraclelinux:8 \
#     bash /dist/smoke-tarball.sh /dist/hardcharger-linux-amd64.tar.gz
#
# Needs only bash, tar, curl and a Python 3 for reading JSON (EL8's platform-python).
# (The server listens on 31337, health 31338.)
set -euo pipefail
tarball="${1:?tarball}"
prefix=$(mktemp -d)
tar -C "$prefix" -xzf "$tarball"
bin=$(echo "$prefix"/hardcharger-*/bin/hardcharger-server)
python=$(command -v python3 || echo /usr/libexec/platform-python)
json() { "$python" -c 'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }

"$bin" --version
"$bin" --verify-tls https://token.actions.githubusercontent.com/.well-known/openid-configuration

work=$(mktemp -d)
cd "$work"
HARDCHARGER_ADMIN_USERNAME=smoke HARDCHARGER_ADMIN_PASSWORD=smoke-password HARDCHARGER_HEALTH_PORT=31338 \
  "$bin" --host 127.0.0.1 --port 31337 2> server.log &
server=$!
trap 'kill "$server" 2>/dev/null || true; cat "$work/server.log"' EXIT
for _ in $(seq 1 30); do curl -sf http://127.0.0.1:31338/health && break; sleep 1; done
echo
curl -sf http://127.0.0.1:31338/health | grep -q serving

base=http://127.0.0.1:31337/warp/v1
token=$(curl -sf -X POST "$base/auth/token" -H 'Content-Type: application/json' \
          -d '{"grant_type": "password", "username": "smoke", "password": "smoke-password"}' | json access_token)
auth=(-H "Authorization: Bearer $token" -H 'Content-Type: application/json')
session=$(curl -sf -X POST "$base/sessions" "${auth[@]}" -d '{}' | json session_id)
statement=$(curl -sf -X POST "$base/sessions/$session/statements" "${auth[@]}" -H 'Accept: application/json' \
              -H 'Prefer: wait=30' -d "{\"sql\": \"SELECT 'portable-' || (40 + 2) AS answer\"}")
echo "$statement"
[ "$(echo "$statement" | json state)" = succeeded ]
id=$(echo "$statement" | json statement_id)
curl -sf "$base/sessions/$session/statements/$id/result" "${auth[@]}" \
  -H 'Accept: application/vnd.apache.arrow.stream' -o result.arrows
grep -aq 'portable-42' result.arrows || { echo "the result has no portable-42"; exit 1; }
# Outbound HTTPS from inside DuckDB (the built-in httpfs, with the static OpenSSL and the
# system's CA bundle), through the same endpoints.
statement=$(curl -sf -X POST "$base/sessions/$session/statements" "${auth[@]}" -H 'Accept: application/json' \
              -H 'Prefer: wait=60' -d "{\"sql\": \"SELECT 'https-' || (length(content) > 100)::VARCHAR FROM read_text('https://token.actions.githubusercontent.com/.well-known/openid-configuration')\"}")
[ "$(echo "$statement" | json state)" = succeeded ] || { echo "the httpfs read failed: $statement"; exit 1; }
id=$(echo "$statement" | json statement_id)
curl -sf "$base/sessions/$session/statements/$id/result" "${auth[@]}" \
  -H 'Accept: application/vnd.apache.arrow.stream' -o https.arrows
grep -aq 'https-true' https.arrows || { echo "the httpfs read returned no https-true"; exit 1; }
# The command-line client from the same tarball, against the same server.
client="$(dirname "$bin")/hardcharger"
"$client" --version
answer=$(HARDCHARGER_PASSWORD=smoke-password "$client" --host 127.0.0.1 --port 31337 --tls=false --username smoke \
           --csv --no-header -c "SELECT 'client-' || (40 + 2)")
[ "$answer" = client-42 ] || { echo "the client answered '$answer'"; exit 1; }
echo "smoke test passed: $("$bin" --version | head -1)"
