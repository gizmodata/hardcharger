#!/usr/bin/env bash
# Benchmark-only GizmoSQL edge build against the exact DuckDB 2.0.0 release commit.
# Runs on the disposable VM; never publishes a GizmoSQL release.
set -euo pipefail
B=~/tpch1tb
SOURCE=/data/gizmosql-duckdb2-ga
GIZMOSQL_COMMIT=a3f79b5e26e7ebae2ebbe88fd2482978e6467a61
DUCKDB_COMMIT=d54cbbe3df17e8e5ce68312f483b4155836112aa
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io >/dev/null
sudo systemctl start docker
if [ ! -d "$SOURCE/.git" ]; then
  git clone --quiet --depth 1 --branch v1.42.0 https://github.com/gizmodata/gizmosql.git "$SOURCE"
fi
[ "$(git -C "$SOURCE" rev-parse HEAD)" = "$GIZMOSQL_COMMIT" ]
# The final 2.0 release removed two C++ API names used by the older edge alpha.
# Keep these benchmark-only compatibility edits explicit in the provenance.
python3 - "$SOURCE" <<'PY'
from pathlib import Path
import sys
source = Path(sys.argv[1])
path = source / 'src/duckdb/duckdb_compat.h'
text = path.read_text()
old = '''        submitted->GetStatementProperties().result_eagerness !=
            duckdb::ResultEagerness::FORCED &&
'''
if old in text:
    path.write_text(data=text.replace(old, ''))
else:
    assert 'result_eagerness' not in text
path = source / 'src/enterprise/metrics/metrics_service.cpp'
text = path.read_text()
text = text.replace('DBConfig::ParseMemoryLimit(', 'DBConfig::ParseMemoryLimitOrMaximum(')
path.write_text(data=text)
PY
mkdir -p "$SOURCE/build"
if [ ! -f "$SOURCE/build/CMakeCache.txt" ]; then
cat > "$SOURCE/build/CMakeCache.txt" <<EOF
DUCKDB_EDGE_VERSION:STRING=v2.0.0
DUCKDB_EDGE_GIT_REF:STRING=$DUCKDB_COMMIT
CMAKE_CXX_FLAGS:STRING=-DDUCKDB_V2_API_ALLOW_UNSTABLE=1
EOF
fi
sudo docker run --rm -v "$SOURCE:$SOURCE" -e REPO_ROOT="$SOURCE" \
  -e CMAKE_BUILD_TYPE=Release -e CMAKE_BUILD_PARALLEL_LEVEL=64 \
  quay.io/pypa/manylinux_2_28_aarch64 \
  bash "$SOURCE/scripts/build_portable_linux.sh" edge build
mkdir -p "$B/gizmosql-edge"
cp "$SOURCE/build/gizmosql_server_edge" "$B/gizmosql-edge/gizmosql_server_edge"
ln -sf gizmosql_server_edge "$B/gizmosql-edge/server"
"$B/gizmosql-edge/server" --version
"$B/gizmosql-edge/server" --print-duckdb-version
{
  printf 'GizmoSQL commit: %s\nDuckDB ref: %s\n' "$GIZMOSQL_COMMIT" "$DUCKDB_COMMIT"
  printf 'Build: Release, edge channel, GCC manylinux_2_28, DUCKDB_V2_API_ALLOW_UNSTABLE=1\n'
} > "$B/gizmosql-edge/build-provenance.txt"
git -C "$SOURCE" diff >> "$B/gizmosql-edge/build-provenance.txt"
