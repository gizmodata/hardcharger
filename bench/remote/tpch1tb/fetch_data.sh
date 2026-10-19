#!/usr/bin/env bash
# On the VM: fetch_data.sh <scale-factor> — downloads the manifest's Parquet files (urls.tsv,
# from presign.py) to /data/tpch/sf<N>/<table>/, 64 at a time, checks every file's size against
# the manifest, then deletes the manifest. Not part of any timed run.
set -euo pipefail
SF=${1:?scale factor}
MANIFEST=~/tpch1tb/urls.tsv
DEST=/data/tpch/sf$SF
[ -s "$MANIFEST" ] || { echo "no manifest at $MANIFEST" >&2; exit 1; }
cut -f1 "$MANIFEST" | xargs -n1 dirname | sort -u | while read -r d; do mkdir -p "$DEST/$d"; done
start=$(date +%s)
# Each line: <relative path>\t<size>\t<url>. The URLs are never printed or logged.
awk -F'\t' '{print $1; print $3}' "$MANIFEST" |
  xargs -P 64 -d '\n' -n2 bash ~/tpch1tb/fetch_one.sh "$DEST"
bad=0
while IFS=$'\t' read -r rel size _; do
  actual=$(stat -c %s "$DEST/$rel" 2>/dev/null || echo missing)
  [ "$actual" = "$size" ] || { echo "size mismatch: $rel ($actual, expected $size)" >&2; bad=1; }
done <"$MANIFEST"
[ "$bad" = 0 ] || exit 1
rm -f "$MANIFEST"
echo "$(find "$DEST" -name '*.parquet' | wc -l) files, $(du -sh "$DEST" | cut -f1), in $(( $(date +%s) - start )) s; sizes verified"
