#!/usr/bin/env bash
# fetch_one.sh <dest-dir> <relative-path> <url>: one download for fetch_data.sh. The URL is
# handed to curl through a config on stdin and never printed (it is in this helper's argv on the
# single-user VM, valid a few hours, read-only for one object).
set -uo pipefail
dest=$1 rel=$2 url=$3
for attempt in 1 2 3 4 5; do
  printf 'url = "%s"\n' "$url" | curl -fsS --retry 3 -K - -o "$dest/$rel" && exit 0
  sleep $((attempt * 2))
done
echo "failed: $rel" >&2
exit 1
