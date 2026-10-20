#!/bin/bash -x
# Runs on the VM as root (run.sh start): the steps of ClickBench's cloud-init.sh.in, except that
# the log stays on the VM instead of going to ClickBench's results sink (an internal run must not
# reach play.clickhouse.com), and the hardcharger/ entry comes from ~ubuntu/stage instead of a fork.
#
# HARDCHARGER_VERSION and HARDCHARGER_INSTALL_MIRROR (a directory laid out like the releases, for
# a release that isn't public yet) pass through to the official installer that ./install runs.
STAGE=/home/ubuntu/stage
export HOME=/root
cd /root || exit 1
[ -f "$STAGE/env" ] && . "$STAGE/env"

# As cloud-init.sh.in: 16 GB swap, no background inode-table zeroing, the same packages.
if [ ! -f /swapfile ]; then
    fallocate -l 16G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
fi
mount -o remount,noinit_itable /
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y wget curl git jq earlyoom
systemctl enable --now earlyoom

rm -rf ClickBench
git clone --depth 1 https://github.com/ClickHouse/ClickBench --branch main ClickBench
git -C ClickBench log -1 --format='ClickBench commit: %H %cs' | tee /root/clickbench-commit
rm -rf ClickBench/hardcharger
cp -r "$STAGE/hardcharger" ClickBench/hardcharger
cd ClickBench/hardcharger || exit 1

echo -n 'System name: ' | tee -a log
jq -r '.system' template.json | tee -a log
echo -n 'Proprietary: ' | tee -a log
jq -r '.proprietary' template.json | tee -a log
echo -n 'Tuned: ' | tee -a log
jq -r '.tuned' template.json | tee -a log
echo -n 'Tags: ' | tee -a log
jq -c -r '.tags' template.json | tee -a log
echo 'ClickBench PR: ' | tee -a log
echo -n 'Disk usage before: ' | tee -a log
df -B1 / | tail -n1 | awk '{ print $3 }' | tee -a log

timeout 36000 ./benchmark.sh 2>&1 | tee -a log

echo -n 'Disk usage after: ' | tee -a log
df -B1 / | tail -n1 | awk '{ print $3 }' | tee -a log
echo 'System: hardcharger' | tee -a log
echo -n 'Machine: ' | tee -a log
curl -sS -H "X-aws-ec2-metadata-token: $(curl -X PUT "http://169.254.169.254/latest/api/token" -sS -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")" 'http://169.254.169.254/latest/meta-data/instance-type' | tee -a log
echo | tee -a log
echo "Total time: $SECONDS" | tee -a log
hardcharger-server --version | tee -a log
touch /root/clickbench-done
