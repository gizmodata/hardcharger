#!/usr/bin/env bash
# JMESPath literals are backquoted inside single quotes (SC2016); remote commands are meant to
# expand on this side (SC2029).
# shellcheck disable=SC2016,SC2029
#
# Runs the performance regression suite on a throwaway AWS instance, and removes every trace.
#
#   aws.sh up                     launch one instance (TYPE, default c8a.8xlarge) with a new key pair
#                                 and a security group that admits SSH from this machine's address only
#   aws.sh setup                  copy bench/regress to the instance; packages and the client venv
#   aws.sh build <git ref> <label>  upload that commit's source (and its Chargewire submodule) and
#                                 build the server there in the background (manylinux_2_28, as a
#                                 release; JOBS=N caps its parallel jobs, e.g. for two builds at
#                                 once); `aws.sh log build-<label>` follows it
#   aws.sh run -- <run.py args>   start the suite in the background (results in ~/results/<RUN_NAME>)
#   aws.sh log <name>             the tail of ~/<name>.log (run, build-<label>)
#   aws.sh fetch <local dir>      copy ~/results back
#   aws.sh ssh [command]          a shell (or one command) on the instance
#   aws.sh down                   terminate the instance; delete the security group and key pair;
#                                 then `check`
#   aws.sh check                  everything still tagged Purpose=$PURPOSE (empty = clean); exit 1 if any
#   aws.sh cost                   instance hours so far (from launch to now or to termination) x price
#
# Safety: the instance destroys itself LIFETIME_MIN minutes after boot (default 240):
# user data runs `shutdown -h +N` and shutdown terminates it (--instance-initiated-shutdown-behavior
# terminate), with its volume. A controller that goes away mid-run leaves nothing billing past
# that. Every resource is tagged Name=hardcharger-perf-*, Purpose=hardcharger-perf-regress,
# Owner=claude, and `down`/`check` work from the tags, not from local state.
#
# Needs AWS credentials in the environment (never printed) and REGION (default us-east-2).
# On-demand by default: a Spot interruption mid-run would throw away the interleaving (MARKET=spot
# to accept that risk). State (key, address, timestamps) lives in STATE (~/.hardcharger-perf).
set -euo pipefail
export AWS_PAGER=""

REGION=${REGION:-${AWS_REGION:-us-east-2}}
TYPE=${TYPE:-c8a.8xlarge}
MARKET=${MARKET:-on-demand}
LIFETIME_MIN=${LIFETIME_MIN:-240}
VOLUME_GB=${VOLUME_GB:-150}
PURPOSE=hardcharger-perf-regress
STATE=${STATE:-$HOME/.hardcharger-perf}
RUN_NAME=${RUN_NAME:-run}
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
[[ "$LIFETIME_MIN" =~ ^[1-9][0-9]*$ ]] || { echo "LIFETIME_MIN must be a positive number of minutes" >&2; exit 2; }
mkdir -p "$STATE"
chmod 700 "$STATE"

tags() { # tags <resource-type> <name>
  echo "ResourceType=$1,Tags=[{Key=Name,Value=$2},{Key=Purpose,Value=$PURPOSE},{Key=Owner,Value=claude}]"
}

state() { # state <key> -> value written by `up`
  [ -f "$STATE/$1" ] || { echo "no $STATE/$1: run aws.sh up first" >&2; exit 1; }
  cat "$STATE/$1"
}

ssh_args() {
  echo -i "$STATE/key" -o IdentitiesOnly=yes -o IdentityAgent=none -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile="$STATE/known_hosts" -o ServerAliveInterval=30 -o ConnectTimeout=10
}

remote() { # remote <command...>: one command on the instance
  # shellcheck disable=SC2046
  ssh $(ssh_args) "ubuntu@$(state address)" "$@"
}

copy_to() { # copy_to <local path...> <remote dir>
  local dest="${*: -1}"
  # shellcheck disable=SC2046
  rsync -az -e "ssh $(ssh_args)" "${@:1:$#-1}" "ubuntu@$(state address):$dest"
}

case "${1:-}" in
  up)
    if [ -f "$STATE/instance" ] && [ "$(aws ec2 describe-instances --region "$REGION" --instance-ids "$(cat "$STATE/instance")" \
        --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null)" = running ]; then
      echo "already up: $(cat "$STATE/instance") at $(cat "$STATE/address")"; exit 0
    fi
    stamp=$(date -u +%Y%m%d%H%M%S)
    name="hardcharger-perf-$stamp"
    ip=$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')
    [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "no public IP" >&2; exit 1; }
    rm -f "$STATE/key" "$STATE/key.pub" "$STATE/known_hosts"
    ssh-keygen -q -t ed25519 -N "" -C "$name" -f "$STATE/key"
    aws ec2 import-key-pair --region "$REGION" --key-name "$name" --public-key-material "fileb://$STATE/key.pub" \
      --tag-specifications "$(tags key-pair "$name")" >/dev/null
    echo "$name" > "$STATE/key_name"
    vpc=$(aws ec2 describe-vpcs --region "$REGION" --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)
    [ -n "$vpc" ] && [ "$vpc" != None ] || { echo "no default VPC in $REGION" >&2; exit 1; }
    sg=$(aws ec2 create-security-group --region "$REGION" --group-name "$name" --vpc-id "$vpc" \
      --description "$name: SSH from the operator only (hardcharger perf regression)" \
      --tag-specifications "$(tags security-group "$name")" --query GroupId --output text)
    echo "$sg" > "$STATE/security_group"
    aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$sg" \
      --ip-permissions "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$ip/32}]" \
      --tag-specifications "$(tags security-group-rule "$name")" >/dev/null
    arch=$(aws ec2 describe-instance-types --region "$REGION" --instance-types "$TYPE" \
      --query 'InstanceTypes[0].ProcessorInfo.SupportedArchitectures[0]' --output text)
    case "$arch" in x86_64) ami_arch=amd64 ;; arm64) ami_arch=arm64 ;; *) echo "unknown architecture $arch" >&2; exit 1 ;; esac
    ami=$(aws ssm get-parameter --region "$REGION" \
      --name "/aws/service/canonical/ubuntu/server/24.04/stable/current/$ami_arch/hvm/ebs-gp3/ami-id" \
      --query Parameter.Value --output text)
    market=()
    [ "$MARKET" = spot ] && market=(--instance-market-options \
      'MarketType=spot,SpotOptions={SpotInstanceType=one-time,InstanceInterruptionBehavior=terminate}')
    # cloud-init runs this as root at first boot: the self-destruct (see the top).
    user_data=$(printf '#!/bin/bash\nshutdown -h +%s "hardcharger-perf: lifetime of %s minutes reached"\n' \
      "$LIFETIME_MIN" "$LIFETIME_MIN")
    instance=$(aws ec2 run-instances --region "$REGION" --image-id "$ami" --instance-type "$TYPE" \
      --key-name "$name" --security-group-ids "$sg" --associate-public-ip-address \
      ${market[@]+"${market[@]}"} --instance-initiated-shutdown-behavior terminate --user-data "$user_data" \
      --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=$VOLUME_GB,VolumeType=gp3,Iops=6000,Throughput=500,DeleteOnTermination=true}" \
      --tag-specifications "$(tags instance "$name")" "$(tags volume "$name")" "$(tags network-interface "$name")" \
      --query 'Instances[0].InstanceId' --output text)
    echo "$instance" > "$STATE/instance"
    date -u +%Y-%m-%dT%H:%M:%SZ > "$STATE/launched_at"
    echo "$TYPE" > "$STATE/type"
    aws ec2 wait instance-running --region "$REGION" --instance-ids "$instance"
    aws ec2 describe-instances --region "$REGION" --instance-ids "$instance" \
      --query 'Reservations[0].Instances[0].PublicIpAddress' --output text > "$STATE/address"
    for _ in $(seq 1 60); do remote true 2>/dev/null && break; sleep 5; done
    remote true
    echo "up: $instance ($TYPE, $REGION) at $(cat "$STATE/address"); terminates itself $LIFETIME_MIN min after boot"
    ;;
  setup)
    remote mkdir -p regress results
    copy_to "$HERE"/*.py "$HERE"/*.sh "$HERE"/requirements.txt "$HERE"/thresholds.json regress/
    remote bash regress/instance.sh setup
    ;;
  build)
    ref="${2:?git ref}"; label="${3:?label}"
    work=$(mktemp -d)
    trap 'rm -rf "$work"' EXIT
    git -C "$REPO" archive --format=tar -o "$work/src-$label.tar" "$ref"
    sub=$(git -C "$REPO" rev-parse "$ref:third_party/chargewire")
    git -C "$REPO/third_party/chargewire" archive --format=tar --prefix=third_party/chargewire/ -o "$work/sub.tar" "$sub"
    tar -Af "$work/src-$label.tar" "$work/sub.tar" 2>/dev/null || {
      # BSD tar has no -A: unpack both and pack again.
      mkdir "$work/tree"; tar -xf "$work/src-$label.tar" -C "$work/tree"; tar -xf "$work/sub.tar" -C "$work/tree"
      tar -cf "$work/src-$label.tar" -C "$work/tree" .; }
    echo "$label: $(git -C "$REPO" rev-parse "$ref") (chargewire $sub), $(du -h "$work/src-$label.tar" | cut -f1)"
    copy_to "$work/src-$label.tar" ./
    remote "JOBS=${JOBS:-} nohup bash regress/instance.sh build $label > build-$label.log 2>&1 < /dev/null &"
    echo "building in the background: aws.sh log build-$label"
    ;;
  run)
    shift
    [ "${1:-}" = "--" ] && shift
    args=$(printf '%q ' "$@")
    # `cd ...;` not `cd ... &&`: with &&, the `&` backgrounds the whole list in a subshell that keeps
    # the ssh session's descriptors, and ssh does not return.
    remote "cd regress || exit 1; nohup venv/bin/python run.py --out ~/results/$RUN_NAME $args > ~/$RUN_NAME.log 2>&1 < /dev/null &"
    echo "running in the background: aws.sh log $RUN_NAME"
    ;;
  log)
    remote tail -n "${LINES_:-40}" "${2:?log name}.log"
    ;;
  fetch)
    dest="${2:?local directory}"
    mkdir -p "$dest"
    # shellcheck disable=SC2046
    rsync -az -e "ssh $(ssh_args)" "ubuntu@$(state address):results/" "$dest/"
    # shellcheck disable=SC2046
    rsync -az -e "ssh $(ssh_args)" "ubuntu@$(state address):*.log" "$dest/" || true
    echo "fetched into $dest"
    ;;
  ssh)
    shift
    # shellcheck disable=SC2046
    exec ssh $(ssh_args) "ubuntu@$(state address)" "$@"
    ;;
  down)
    ids=$(aws ec2 describe-instances --region "$REGION" --filters Name=tag:Purpose,Values=$PURPOSE \
      Name=instance-state-name,Values=pending,running,stopping,stopped,shutting-down \
      --query 'Reservations[].Instances[].InstanceId' --output text)
    if [ -n "$ids" ]; then
      # shellcheck disable=SC2086
      aws ec2 terminate-instances --region "$REGION" --instance-ids $ids >/dev/null
      # shellcheck disable=SC2086
      aws ec2 wait instance-terminated --region "$REGION" --instance-ids $ids
      date -u +%Y-%m-%dT%H:%M:%SZ > "$STATE/terminated_at"
    fi
    for sg in $(aws ec2 describe-security-groups --region "$REGION" --filters Name=tag:Purpose,Values=$PURPOSE \
        --query 'SecurityGroups[].GroupId' --output text); do
      for _ in 1 2 3 4 5 6; do  # the network interface can outlive the instance by a minute
        aws ec2 delete-security-group --region "$REGION" --group-id "$sg" >/dev/null 2>&1 && break
        sleep 10
      done
    done
    for key in $(aws ec2 describe-key-pairs --region "$REGION" --filters Name=tag:Purpose,Values=$PURPOSE \
        --query 'KeyPairs[].KeyName' --output text); do
      aws ec2 delete-key-pair --region "$REGION" --key-name "$key" >/dev/null
    done
    rm -f "$STATE/key" "$STATE/key.pub" "$STATE/address"
    bash "${BASH_SOURCE[0]}" check
    ;;
  check)
    left=$( {
      aws ec2 describe-instances --region "$REGION" --filters Name=tag:Purpose,Values=$PURPOSE \
        Name=instance-state-name,Values=pending,running,stopping,stopped,shutting-down \
        --query 'Reservations[].Instances[].[`instance`,InstanceId,State.Name]' --output text
      aws ec2 describe-volumes --region "$REGION" --filters Name=tag:Purpose,Values=$PURPOSE \
        --query 'Volumes[].[`volume`,VolumeId,State]' --output text
      aws ec2 describe-security-groups --region "$REGION" --filters Name=tag:Purpose,Values=$PURPOSE \
        --query 'SecurityGroups[].[`security-group`,GroupId]' --output text
      aws ec2 describe-key-pairs --region "$REGION" --filters Name=tag:Purpose,Values=$PURPOSE \
        --query 'KeyPairs[].[`key-pair`,KeyName]' --output text
      aws ec2 describe-network-interfaces --region "$REGION" --filters Name=tag:Purpose,Values=$PURPOSE \
        --query 'NetworkInterfaces[].[`network-interface`,NetworkInterfaceId,Status]' --output text
    } )
    if [ -n "$left" ]; then echo "still tagged Purpose=$PURPOSE in $REGION:"; echo "$left"; exit 1; fi
    echo "clean: nothing tagged Purpose=$PURPOSE in $REGION"
    ;;
  cost)
    launched=$(state launched_at)
    ended=$(cat "$STATE/terminated_at" 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)
    type=$(state type)
    location=""
    case "$REGION" in
      us-east-1) location="US East (N. Virginia)" ;;
      us-east-2) location="US East (Ohio)" ;;
      us-west-2) location="US West (Oregon)" ;;
    esac
    price=${PRICE_PER_HOUR:-}
    if [ -z "$price" ] && [ -n "$location" ]; then
      price=$(aws pricing get-products --region us-east-1 --service-code AmazonEC2 --filters \
        "Type=TERM_MATCH,Field=instanceType,Value=$type" "Type=TERM_MATCH,Field=location,Value=$location" \
        Type=TERM_MATCH,Field=operatingSystem,Value=Linux Type=TERM_MATCH,Field=tenancy,Value=Shared \
        Type=TERM_MATCH,Field=preInstalledSw,Value=NA Type=TERM_MATCH,Field=capacitystatus,Value=Used \
        --query 'PriceList[0]' --output text | python3 -c '
import json, sys
terms = json.loads(sys.stdin.read())["terms"]["OnDemand"]
print(next(iter(next(iter(terms.values()))["priceDimensions"].values()))["pricePerUnit"]["USD"])')
    fi
    python3 - "$launched" "$ended" "$type" "${price:-0}" "$VOLUME_GB" <<'EOF'
import datetime, sys
launched, ended, kind, price, volume_gb = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4]), int(sys.argv[5])
parse = lambda s: datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
hours = (parse(ended) - parse(launched)).total_seconds() / 3600
# gp3: $0.08/GB-month, 3000 IOPS and 125 MB/s free; above that $0.005/IOPS-month, $0.04/MBps-month (us-east)
volume = (volume_gb * 0.08 + 3000 * 0.005 + 375 * 0.04) * hours / 730
print(f"{kind}: {launched} to {ended} = {hours:.2f} h x ${price:.4f}/h = ${hours * price:.2f}; "
      f"volume ~${volume:.2f}; total ~${hours * price + volume:.2f} (on-demand list price)")
EOF
    ;;
  *) sed -n '6,/^set -euo/p' "$0" | sed '$d'; exit 2 ;;
esac
