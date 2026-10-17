#!/usr/bin/env bash
# Measures what kernel TLS (kTLS) would save on the server VM: OpenSSL's own s_server
# sends a file over one TLS connection with user-space encryption, then with kTLS
# (SSL_OP_ENABLE_KTLS + sendfile), and reports throughput and the server's CPU seconds.
# The client is curl (OpenSSL 3) on the same machine, or on another one with CLIENT=.
#
#   ktls_check.sh [size_gb]          on the server VM, after setup-vm.sh
#   CLIENT=ubuntu@1.2.3.4 SERVER_IP=172.31.x.y ktls_check.sh 8
#
# Hardcharger's TlsStream encrypts in user space; using kTLS would mean switching the
# connection to a socket BIO after the handshake. This tells whether that is worth it.
set -euo pipefail
SIZE_GB=${1:-8}
B=$HOME/bench
FILE=/dev/shm/ktls-payload.bin
PORT=${PORT:-44330}
sudo modprobe tls 2>/dev/null || true
command -v /usr/bin/time >/dev/null || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq time >/dev/null
lsmod | grep -q '^tls ' && echo "kernel tls module: loaded" || echo "kernel tls module: NOT loaded"
# Ubuntu's OpenSSL is built without kTLS (s_server has no -ktls). Build one that has it.
OPENSSL=openssl
if ! openssl s_server -help 2>&1 | grep -q -- '-ktls'; then
  echo "system $(openssl version) has no kTLS; building OpenSSL ${KTLS_OPENSSL:-3.5.0} with enable-ktls"
  if [ ! -x /opt/openssl-ktls/bin/openssl ]; then
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq build-essential perl >/dev/null
    rm -rf /tmp/openssl-src && mkdir -p /tmp/openssl-src
    curl -fsSL "https://github.com/openssl/openssl/releases/download/openssl-${KTLS_OPENSSL:-3.5.0}/openssl-${KTLS_OPENSSL:-3.5.0}.tar.gz" |
      tar -xz -C /tmp/openssl-src --strip-components=1
    (cd /tmp/openssl-src && ./Configure enable-ktls --prefix=/opt/openssl-ktls --libdir=lib >/dev/null &&
      make -s -j"$(nproc)" >/dev/null && sudo make -s install_sw >/dev/null)
  fi
  OPENSSL=/opt/openssl-ktls/bin/openssl
  export LD_LIBRARY_PATH=/opt/openssl-ktls/lib
fi
"$OPENSSL" version
[ -f "$FILE" ] || head -c "$((SIZE_GB * 1024 * 1024 * 1024))" /dev/urandom >"$FILE"
cd /dev/shm
run() { # run <label> <s_server args...>
  local label=$1
  shift
  # One connection, then s_server exits; GNU time reports its CPU seconds.
  /usr/bin/time -f '%U %S' -o /tmp/s_server.time "$OPENSSL" s_server -accept "$PORT" -naccept 1 \
    -cert "$B/tls.crt" -key "$B/tls.key" -WWW -quiet "$@" >/tmp/s_server.log 2>&1 &
  local pid=$!
  sleep 1
  local url="https://${SERVER_IP:-127.0.0.1}:$PORT/$(basename "$FILE")" out
  if [ -n "${CLIENT:-}" ]; then
    out=$(ssh "$CLIENT" "curl -sk -o /dev/null -w '%{size_download} %{time_total}' $url") || true
  else
    out=$(curl -sk -o /dev/null -w '%{size_download} %{time_total}' "$url") || true
  fi
  wait "$pid" 2>/dev/null || true
  if [ -z "$out" ] || [ "${out%% *}" = 0 ]; then
    echo "$label: failed"; tail -3 /tmp/s_server.log
    return
  fi
  python3 - "$label" $out $(cat /tmp/s_server.time) <<'EOF2'
import sys
label, size, secs, user, system = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4]), float(sys.argv[5])
cpu = user + system
print(f"{label:34} {size / secs / 1e9:6.2f} GB/s, server CPU {cpu:6.2f} s ({cpu / (size / 1e9):.3f} s per GB)")
EOF2
  grep -i ktls /tmp/s_server.log | head -2 || true
}
for cipher in TLS_AES_128_GCM_SHA256 TLS_AES_256_GCM_SHA384; do
  run "user space, $cipher" -ciphersuites "$cipher"
  run "kTLS + sendfile, $cipher" -ciphersuites "$cipher" -ktls -sendfile
done
