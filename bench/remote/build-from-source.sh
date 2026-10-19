#!/usr/bin/env bash
# Builds what is under test from source on a benchmark VM (Ubuntu 24.04 arm64), for
# trying changes between CI runs. The published numbers use CI's artifacts instead.
#
#   build-from-source.sh server   ~/src/hardcharger (with third_party/warp-sql) ->
#                                 ~/bench/hardcharger-linux-arm64/bin/hardcharger-server
#   build-from-source.sh client   ~/src/warp-sql-adbc -> the Go driver's shared library,
#                                 installed into ~/venv as adbc-driver-warpsql (editable)
#
# The sources are copied there first (rsync from the operator's machine, without build
# trees or .deps). Run setup-vm.sh before this.
set -euo pipefail
ROLE=${1:?server or client}
GO_VERSION=${GO_VERSION:-1.27.2}

if [ "$ROLE" = server ]; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq build-essential cmake ninja-build \
    git libssl-dev pkg-config bison flex >/dev/null
  cd ~/src/hardcharger/server
  scripts/build-deps.sh arrow replxx duckdb >~/bench/build-deps.log 2>&1 ||
    { tail -40 ~/bench/build-deps.log; exit 1; }
  cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release >~/bench/build-server.log 2>&1
  cmake --build build >>~/bench/build-server.log 2>&1 || { tail -40 ~/bench/build-server.log; exit 1; }
  mkdir -p ~/bench/hardcharger-linux-arm64/bin
  ln -sf ~/src/hardcharger/server/build/hardcharger-server ~/bench/hardcharger-linux-arm64/bin/hardcharger-server
  ~/bench/hardcharger-linux-arm64/bin/hardcharger-server --version || true
else
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq build-essential >/dev/null  # cgo
  if [ ! -x /usr/local/go/bin/go ] || ! /usr/local/go/bin/go version | grep -q "go$GO_VERSION "; then
    curl -fsSL "https://go.dev/dl/go$GO_VERSION.linux-arm64.tar.gz" -o /tmp/go.tgz
    sudo rm -rf /usr/local/go && sudo tar -C /usr/local -xzf /tmp/go.tgz
  fi
  export PATH=/usr/local/go/bin:$PATH GOTOOLCHAIN=local
  make -C ~/src/warp-sql-adbc/go lib
  ~/venv/bin/pip install -q -e ~/src/warp-sql-adbc/python
  ~/venv/bin/python -c "import adbc_driver_warpsql._driver as d; print(d._driver_path())"
fi
echo "built $ROLE"
