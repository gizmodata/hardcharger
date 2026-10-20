#!/usr/bin/env bash
# The app against a Hardcharger server elsewhere: the HardchargerRemoteTests UI tests (add
# a server, its web UI and console with a password, then browser sign-in through a mock
# identity provider). They need a desktop hardcharger-server with the web UI, which they
# start on the Mac's loopback (ios/scripts/remote-test-servers.py, Python with server/tests'
# requirements: pyjwt, cryptography):
#
#   ios/scripts/test-remote.sh <hardcharger-server> [simulator name]
#
# Not part of the CI gate (it needs the desktop build too); run before changing remote
# connections or sign-in. Fails, never skips, when the server can't start.
set -euo pipefail
IOS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BINARY="${1:?usage: test-remote.sh <hardcharger-server> [simulator]}"
SIMULATOR="${2:-iPhone 17 Pro}"
PYTHON="${HARDCHARGER_TEST_PYTHON:-python3}"
port_file=$(mktemp)
"$PYTHON" "$IOS/scripts/remote-test-servers.py" "$BINARY" "$port_file" &
servers=$!
trap 'kill $servers 2>/dev/null || true; rm -f "$port_file"' EXIT
for _ in $(seq 1 300); do [ -s "$port_file" ] && break; kill -0 $servers 2>/dev/null || exit 1; sleep 0.2; done
[ -s "$port_file" ] || { echo "the remote server didn't start" >&2; exit 1; }
read -r port password <"$port_file"
# xcodebuild hands TEST_RUNNER_* variables to the test runner (without the prefix).
TEST_RUNNER_HC_REMOTE_PORT="$port" TEST_RUNNER_HC_REMOTE_PASSWORD="$password" \
  xcodebuild test -project "$IOS/Hardcharger.xcodeproj" -scheme HardchargerRemote \
    -destination "platform=iOS Simulator,name=$SIMULATOR" -derivedDataPath "$IOS/build/DerivedData"
