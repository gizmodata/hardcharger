#!/usr/bin/env bash
set -euo pipefail
tool="$(basename "$1")"
shift
host="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/build/host-tools/$tool"
[ -x "$host" ] || { echo "Missing host tool: $tool" >&2; exit 1; }
exec "$host" "$@"
