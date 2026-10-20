#!/usr/bin/env bash
# CMAKE_CROSSCOMPILING_EMULATOR of the iOS server build (build-server-ios.sh). CMake runs a
# build-time tool the project builds itself (warp-sql's warpsql_openapi_json, which turns
# the protocol's OpenAPI document into C++) as `<this script> <the tool, built for iOS>
# <its arguments>`; an iOS binary doesn't run on the Mac, so this runs the Mac build of the
# same tool, which build-server-ios.sh compiles into ios/build/host-tools first.
set -euo pipefail
tool="$(basename "$1")"
shift
host="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/build/host-tools/$tool"
[ -x "$host" ] || { echo "run-host-tool.sh: no host build of $tool at $host (build-server-ios.sh makes it)" >&2; exit 1; }
exec "$host" "$@"
