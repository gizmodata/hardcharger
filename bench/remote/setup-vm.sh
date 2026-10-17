#!/usr/bin/env bash
# Runs on each benchmark VM (Ubuntu 24.04 arm64): setup-vm.sh server|client
# Expects in ~/bench: the adbc_driver_warpsql wheel (client), the Hardcharger tarball
# (server), and the scripts from bench/remote. No GitHub token is ever needed here: the
# GizmoSQL release and every Python package are public downloads.
set -euo pipefail
ROLE=${1:?server or client}
GIZMOSQL_VERSION=${GIZMOSQL_VERSION:-v1.42.0}
DUCKDB2=2.0.0.dev2610161340        # DuckDB v2.0.0-alpha48624 (ffe0b8b7a2), Hardcharger's pin
cd ~/bench

sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq iperf3 python3-venv unzip sysstat \
  linux-tools-common "linux-tools-$(uname -r)" >/dev/null
sudo sysctl -q -w kernel.perf_event_paranoid=-1 kernel.kptr_restrict=0

python3 -m venv ~/venv
~/venv/bin/pip install -q --upgrade pip
~/venv/bin/pip install -q "duckdb==$DUCKDB2" "pyarrow==26.0.0" "adbc-driver-manager==1.12.0" \
  "adbc-driver-gizmosql==2.0.15"
# Quack is a DuckDB extension, installed from DuckDB's repository for this exact build.
~/venv/bin/python -c "import duckdb; c = duckdb.connect(); c.execute(query='INSTALL quack'); c.execute(query='INSTALL tpch')"

if [ "$ROLE" = server ]; then
  # DuckDB 1.5.6 writes the shared database file, so all three servers read one storage format.
  python3 -m venv ~/venv15
  ~/venv15/bin/pip install -q "duckdb==1.5.6"
  ~/venv15/bin/python -c "import duckdb; c = duckdb.connect(); c.execute(query='INSTALL tpch')"
  curl -fsSL -o gizmosql.zip \
    "https://github.com/gizmodata/gizmosql/releases/download/$GIZMOSQL_VERSION/gizmosql_cli_linux_arm64.zip"
  rm -rf gizmosql && mkdir gizmosql && unzip -q -o gizmosql.zip -d gizmosql
  # The Hardcharger tarball may arrive later; install-hardcharger.sh unpacks it then.
  for t in hardcharger-*.tar.gz; do [ -f "$t" ] && tar -xzf "$t" -C ~/bench; done
else
  ~/venv/bin/pip install -q ~/bench/adbc_driver_warpsql-*.whl
fi
echo "setup $ROLE done"
