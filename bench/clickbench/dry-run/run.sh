#!/usr/bin/env bash
# Drives a ClickBench run of ../hardcharger on the VM that provision.sh launched.
#
#   run.sh push <version> [<release-dir>]  copy the entry (and, for a release that isn't public
#                                          yet, the tarball + SHA256SUMS in <release-dir>, laid
#                                          out as v<version>/<asset>) to the VM
#   run.sh start                           start vm-run.sh as root in the background
#   run.sh wait                            poll until it finishes (prints the log's tail)
#   run.sh pull <dir>                      copy the log, result.csv and server log back
#
# The VM never gets GitHub or AWS credentials: everything it needs is either public (ClickBench,
# the dataset, apt) or copied here over scp.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE=${STATE:-$HOME/.hardcharger-bench-clickbench}
KEY="$STATE/hardcharger-bench-clickbench-key"
IP=$(cat "$STATE/vm.public")
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$STATE/known_hosts"
  -o ConnectTimeout=15 -o ServerAliveInterval=30 "ubuntu@$IP")
SCP=(scp -i "$KEY" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$STATE/known_hosts" -q)

case "${1:-}" in
  push)
    version=${2:?version, e.g. 2.0.0-rc.1}
    "${SSH[@]}" 'rm -rf ~/stage && mkdir -p ~/stage'
    "${SCP[@]}" -r "$HERE/../hardcharger" "$HERE/vm-run.sh" "ubuntu@$IP:stage/"
    env="HARDCHARGER_VERSION=$version"
    if [ -n "${3:-}" ]; then
      "${SSH[@]}" "mkdir -p ~/stage/mirror/v$version"
      "${SCP[@]}" "$3/v$version/"* "ubuntu@$IP:stage/mirror/v$version/"
      env="$env HARDCHARGER_INSTALL_MIRROR=/home/ubuntu/stage/mirror"
    fi
    "${SSH[@]}" "printf 'export %s\n' '$env' > ~/stage/env && rm -rf ~/stage/hardcharger/results && ls -R ~/stage | head -40"
    ;;
  start)
    "${SSH[@]}" 'sudo rm -f /root/clickbench-done; sudo nohup bash /home/ubuntu/stage/vm-run.sh > /home/ubuntu/vm-run.out 2>&1 < /dev/null & sleep 2; tail -n 3 /home/ubuntu/vm-run.out'
    ;;
  wait)
    until "${SSH[@]}" 'sudo test -f /root/clickbench-done'; do
      "${SSH[@]}" 'sudo tail -n 2 /root/ClickBench/hardcharger/log 2>/dev/null; uptime' || true
      sleep 60
    done
    "${SSH[@]}" 'sudo tail -n 12 /root/ClickBench/hardcharger/log'
    ;;
  pull)
    out=${2:?output directory}
    mkdir -p "$out"
    "${SSH[@]}" 'sudo tar -C /root -czf /tmp/cb-out.tgz ClickBench/hardcharger/log ClickBench/hardcharger/result.csv ClickBench/hardcharger/hardcharger-server.log clickbench-commit; sudo chmod 644 /tmp/cb-out.tgz; cp /home/ubuntu/vm-run.out /tmp/'
    "${SCP[@]}" "ubuntu@$IP:/tmp/cb-out.tgz" "ubuntu@$IP:/tmp/vm-run.out" "$out/"
    tar -xzf "$out/cb-out.tgz" -C "$out" && rm "$out/cb-out.tgz"
    ls -la "$out/ClickBench/hardcharger"
    ;;
  *) sed -n '2,13p' "$0"; exit 2 ;;
esac
