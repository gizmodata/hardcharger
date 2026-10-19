#!/usr/bin/env bash
# Runs on the benchmark VM (Ubuntu 24.04 arm64) from ~/tpch1tb: prepares /data and the three
# servers and two clients. Expects ~/tpch1tb/hardcharger-linux-arm64.tar.gz and
# ~/tpch1tb/adbc_driver_hardcharger-*.whl (copied in by run.sh; both private builds). Every
# other download is public.
set -euo pipefail
GIZMOSQL_VERSION=${GIZMOSQL_VERSION:-v1.42.0}
BFS_REF=${BFS_REF:-28bb41e58bb394c37cc040870486805f223c7e81}   # benchmark-flight-sql 0.0.6
cd ~/tpch1tb

# The self-destruct from provision.sh's user data must be scheduled before anything else runs.
# User data runs late in cloud-init (a canary had SSH 16 s after boot, before it ran), so wait.
cloud-init status --wait >/dev/null || true
if [ -f /run/systemd/shutdown/scheduled ]; then
  echo "self-destruct scheduled: $(sed -n 's/^USEC=//p' /run/systemd/shutdown/scheduled |
    awk '{print strftime("%FT%TZ", $1 / 1000000, 1)}')"
else
  echo "no shutdown is scheduled on this VM; refusing to continue" >&2; exit 1
fi

sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq mdadm python3-venv unzip sysstat openssl git >/dev/null

# 1. Stripe the local NVMe drives into one RAID 0 volume, as published:
#    mdadm --create /dev/md0 --level=0 --raid-devices=3 ...; mkfs.ext4 -F /dev/md0; mount /data
#    (the dry run's r8gd.2xlarge has a single drive, which mdadm only accepts with --force).
if ! mountpoint -q /data; then
  mapfile -t DRIVES < <(lsblk -dn -o NAME,MODEL | awk '/Instance Storage/ {print "/dev/" $1}' | sort)
  [ "${#DRIVES[@]}" -ge 1 ] || { echo "no instance-store NVMe drives" >&2; exit 1; }
  FORCE=(); [ "${#DRIVES[@]}" = 1 ] && FORCE=(--force)
  echo "RAID 0 over ${#DRIVES[@]} drives: ${DRIVES[*]}"
  sudo mdadm --create /dev/md0 --level=0 --raid-devices="${#DRIVES[@]}" ${FORCE[@]+"${FORCE[@]}"} --run "${DRIVES[@]}"
  sudo mkfs.ext4 -q -F /dev/md0
  sudo mkdir -p /data && sudo mount /dev/md0 /data
  sudo chown "$(id -u):$(id -g)" /data
fi
df -h /data | tail -1

# 2. GizmoSQL stable and edge, from the public release.
for channel in stable edge; do
  suffix=""; [ "$channel" = edge ] && suffix=_edge
  rm -rf "gizmosql-$channel" && mkdir "gizmosql-$channel"
  curl -fsSL -o "gizmosql-$channel.zip" \
    "https://github.com/gizmodata/gizmosql/releases/download/$GIZMOSQL_VERSION/gizmosql_cli_linux_arm64$suffix.zip"
  unzip -q -o "gizmosql-$channel.zip" -d "gizmosql-$channel"
  ln -sf "gizmosql_server$suffix" "gizmosql-$channel/server"
done

# 3. Hardcharger, from the tarball copied in.
rm -rf hardcharger && mkdir hardcharger
tar -xzf hardcharger-linux-arm64.tar.gz -C hardcharger --strip-components=1

# 4. benchmark-flight-sql (the published GizmoSQL runner) in its own venv; it reads
#    queries/tpc_h.yaml relative to its working directory, so it runs from its clone.
rm -rf benchmark-flight-sql
git clone -q https://github.com/gizmodata/benchmark-flight-sql.git
git -C benchmark-flight-sql checkout -q "$BFS_REF"
python3 -m venv ~/venv-bfs
~/venv-bfs/bin/pip install -q --upgrade pip
~/venv-bfs/bin/pip install -q ./benchmark-flight-sql

# 5. The Warp SQL runner's venv: the Hardcharger ADBC driver (built from hardcharger-adbc).
python3 -m venv ~/venv-hc
~/venv-hc/bin/pip install -q --upgrade pip
~/venv-hc/bin/pip install -q ./adbc_driver_hardcharger-*.whl "pyyaml==6.0.*"

# 6. One self-signed certificate for all three servers (TLS on every one; clients skip verify).
[ -f tls.crt ] || openssl req -x509 -newkey rsa:2048 -nodes -days 7 -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" -keyout tls.key -out tls.crt 2>/dev/null

echo "== versions"
gizmosql-stable/server --version; gizmosql-stable/server --print-duckdb-version
gizmosql-edge/server --version; gizmosql-edge/server --print-duckdb-version
hardcharger/bin/hardcharger-server --version; hardcharger/bin/hardcharger-server --print-duckdb-version
~/venv-bfs/bin/pip show adbc-driver-gizmosql pyarrow 2>/dev/null | grep -E '^(Name|Version)'
~/venv-hc/bin/pip show adbc-driver-hardcharger pyarrow 2>/dev/null | grep -E '^(Name|Version)'
echo "setup done"
