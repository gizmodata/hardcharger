#!/usr/bin/env bash
# Runs ON the benchmark instance (Ubuntu 24.04), driven by aws.sh over ssh. Lives next to the
# suite in ~/regress.
#
#   instance.sh setup            packages, and a venv with the suite's pinned client side
#   instance.sh build <label>    builds the server from ~/src-<label>.tar (aws.sh build uploads it)
#                                in the manylinux_2_28 image server.yml pins, as the release does;
#                                the binary lands at ~/build/<label>/server/build/hardcharger-server
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

apt_install() {
  for attempt in 1 2 3; do
    if sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$@" >/dev/null; then return 0; fi
    echo "apt-get install failed (attempt $attempt of 3)" >&2
    sudo apt-get update -q >/dev/null || true
    sleep 15
  done
  return 1
}

case "${1:-}" in
  setup)
    cloud-init status --wait >/dev/null 2>&1 || true
    # The EC2 regional Ubuntu mirror served ~66 KB/s on two runs (2026-11-19): use the main archive.
    sudo sed -i -E 's#http://[a-z0-9-]+\.ec2\.archive\.ubuntu\.com/#http://archive.ubuntu.com/#' \
      /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null || true
    sudo apt-get update -q >/dev/null
    apt_install python3-venv python3-pip rsync util-linux
    python3 -m venv "$HERE/venv"
    "$HERE/venv/bin/pip" install -q --upgrade pip
    "$HERE/venv/bin/pip" install -q -r "$HERE/requirements.txt"
    "$HERE/venv/bin/python" -c "import httpx, pyarrow, psutil; print('client ok:', httpx.__version__, pyarrow.__version__, psutil.__version__)"
    ;;
  build)
    label="${2:?label}"
    tarball="$HOME/src-$label.tar"
    [ -f "$tarball" ] || { echo "no $tarball (aws.sh build uploads it)" >&2; exit 1; }
    if ! command -v docker >/dev/null; then
      apt_install docker.io
      # A throwaway instance: let the login user reach the daemon without a new login session.
      sudo chmod 666 /var/run/docker.sock
    fi
    root="$HOME/build/$label"
    rm -rf "$root"
    mkdir -p "$root"
    tar -xf "$tarball" -C "$root"
    cd "$root"
    case "$(uname -m)" in
      x86_64) pattern='manylinux: quay.io/pypa/manylinux_2_28_x86_64' ;;
      aarch64) pattern='manylinux: quay.io/pypa/manylinux_2_28_aarch64' ;;
      *) echo "no manylinux image for $(uname -m)" >&2; exit 1 ;;
    esac
    image=$(grep -m1 "$pattern" .github/workflows/server.yml | sed -E 's/.*manylinux: //')
    [ -n "$image" ] || { echo "no pinned manylinux image in server.yml" >&2; exit 1; }
    export HARDCHARGER_MANYLINUX_IMAGE="$image"
    CMAKE_BUILD_PARALLEL_LEVEL="${JOBS:-$(nproc)}"
    export CMAKE_BUILD_PARALLEL_LEVEL
    echo "== $(date -u +%H:%M:%S) $label: dependencies in $image ($CMAKE_BUILD_PARALLEL_LEVEL jobs)"
    server/scripts/in-manylinux.sh deps openssl arrow replxx duckdb
    echo "== $(date -u +%H:%M:%S) $label: server"
    server/scripts/in-manylinux.sh server server/build -DHARDCHARGER_STATIC=ON
    server/build/hardcharger-server --version
    server/build/hardcharger-server --print-duckdb-version
    echo "BUILD-DONE $label $root/server/build/hardcharger-server"
    ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac
