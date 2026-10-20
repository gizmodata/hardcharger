#!/usr/bin/env bash
# One VM for a ClickBench run of bench/clickbench/hardcharger, set up like ClickBench's own
# run-benchmark.sh (Ubuntu 24.04, 500 GB gp2 root, on demand). Its own key pair, security group
# and state directory, and the tag Run=clickbench, so `down` never touches another benchmark.
#
#   provision.sh up        launch the VM (TYPE, MARKET, AZ, LIFETIME_MIN below)
#   provision.sh ip        print its public IP
#   provision.sh status    instances tagged Run=clickbench
#   provision.sh down      terminate them, cancel Spot requests, delete the SG and key, then `check`
#   provision.sh check     the teardown proof: everything tagged Purpose=hardcharger-benchmark in
#                          us-east-2 and us-west-2 (instances, volumes, SGs, key pairs, Spot requests)
#
# Self-destruct: --instance-initiated-shutdown-behavior terminate, and user data runs
# `shutdown -h +LIFETIME_MIN` at first boot, so a controller that dies leaves nothing billing past
# that. SSH (22) is open only to this machine's public IP; nothing else is open. Needs AWS
# credentials in the environment; never prints them.
set -euo pipefail
export AWS_PAGER=""

REGION=${REGION:-us-east-2}
AZ=${AZ:-us-east-2a}
TYPE=${TYPE:-c6a.4xlarge}
MARKET=${MARKET:-on-demand}                     # ClickBench runs on demand
LIFETIME_MIN=${LIFETIME_MIN:-120}
[[ "$LIFETIME_MIN" =~ ^[1-9][0-9]*$ ]] || { echo "LIFETIME_MIN must be a positive number" >&2; exit 2; }
STATE=${STATE:-$HOME/.hardcharger-bench-clickbench}   # key and address (never committed)
mkdir -p "$STATE"
NAME=hardcharger-bench-clickbench
KEY_NAME=hardcharger-bench-clickbench-key
KEY_FILE="$STATE/$KEY_NAME"
SG_NAME=hardcharger-bench-clickbench-sg
PURPOSE=hardcharger-benchmark
RUN=clickbench

tags() { # tags <resource-type>
  echo "ResourceType=$1,Tags=[{Key=Name,Value=$NAME},{Key=Purpose,Value=$PURPOSE},{Key=Run,Value=$RUN},{Key=Owner,Value=claude}]"
}

mine() { # instance ids of this run in the given states
  aws ec2 describe-instances --region "$REGION" \
    --filters Name=tag:Purpose,Values=$PURPOSE Name=tag:Run,Values=$RUN \
    Name=instance-state-name,Values="$1" \
    --query 'Reservations[].Instances[].InstanceId' --output text
}

case "${1:-}" in
  up)
    [ -z "$(mine pending,running,stopping,stopped)" ] || { echo "a $RUN instance already exists" >&2; exit 1; }
    ip=$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')
    [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "no public IP" >&2; exit 1; }
    [ -f "$KEY_FILE" ] || ssh-keygen -q -t ed25519 -N "" -C "$KEY_NAME" -f "$KEY_FILE"
    aws ec2 describe-key-pairs --region "$REGION" --key-names "$KEY_NAME" >/dev/null 2>&1 ||
      aws ec2 import-key-pair --region "$REGION" --key-name "$KEY_NAME" \
        --public-key-material "fileb://$KEY_FILE.pub" --tag-specifications "$(tags key-pair)" >/dev/null
    vpc=$(aws ec2 describe-vpcs --region "$REGION" --filters Name=is-default,Values=true \
      --query 'Vpcs[0].VpcId' --output text)
    [ -n "$vpc" ] && [ "$vpc" != None ] || { echo "no default VPC" >&2; exit 1; }
    sg=$(aws ec2 describe-security-groups --region "$REGION" \
      --filters Name=group-name,Values=$SG_NAME Name=vpc-id,Values="$vpc" \
      --query 'SecurityGroups[0].GroupId' --output text)
    if [ -z "$sg" ] || [ "$sg" = None ]; then
      sg=$(aws ec2 create-security-group --region "$REGION" --group-name $SG_NAME --vpc-id "$vpc" \
        --description "$SG_NAME (hardcharger ClickBench run)" \
        --tag-specifications "$(tags security-group)" --query GroupId --output text)
    fi
    aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$sg" \
      --ip-permissions "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$ip/32}]" \
      --tag-specifications "$(tags security-group-rule)" >/dev/null 2>&1 || true
    arch=$(aws ec2 describe-instance-types --region "$REGION" --instance-types "$TYPE" \
      --query 'InstanceTypes[0].ProcessorInfo.SupportedArchitectures[0]' --output text)
    case "$arch" in x86_64) a=amd64 ;; arm64) a=arm64 ;; *) echo "unknown architecture $arch" >&2; exit 1 ;; esac
    ami=$(aws ssm get-parameter --region "$REGION" \
      --name "/aws/service/canonical/ubuntu/server/24.04/stable/current/$a/hvm/ebs-gp3/ami-id" \
      --query Parameter.Value --output text)
    subnet=$(aws ec2 describe-subnets --region "$REGION" \
      --filters Name=availability-zone,Values="$AZ" Name=default-for-az,Values=true \
      --query 'Subnets[0].SubnetId' --output text)
    market=()
    spot_tags=()
    if [ "$MARKET" = spot ]; then
      market=(--instance-market-options
        'MarketType=spot,SpotOptions={SpotInstanceType=one-time,InstanceInterruptionBehavior=terminate}')
      spot_tags=("$(tags spot-instances-request)")
    fi
    user_data=$(printf '#!/bin/bash\nshutdown -h +%s "hardcharger-bench-clickbench: lifetime of %s minutes reached"\n' \
      "$LIFETIME_MIN" "$LIFETIME_MIN")
    id=$(aws ec2 run-instances --region "$REGION" --image-id "$ami" --instance-type "$TYPE" \
      --key-name "$KEY_NAME" --subnet-id "$subnet" --security-group-ids "$sg" \
      --associate-public-ip-address ${market[@]+"${market[@]}"} \
      --instance-initiated-shutdown-behavior terminate --user-data "$user_data" \
      --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=500,VolumeType=gp2,DeleteOnTermination=true}' \
      --tag-specifications "$(tags instance)" "$(tags volume)" "$(tags network-interface)" \
      ${spot_tags[@]+"${spot_tags[@]}"} \
      --query 'Instances[0].InstanceId' --output text)
    echo "launched $id ($TYPE, $MARKET, $AZ, $ami); it terminates itself $LIFETIME_MIN minutes after boot"
    aws ec2 wait instance-running --region "$REGION" --instance-ids "$id"
    aws ec2 describe-instances --region "$REGION" --instance-ids "$id" \
      --query 'Reservations[0].Instances[0].PublicIpAddress' --output text >"$STATE/vm.public"
    echo "$id" >"$STATE/vm.id"
    echo "public IP $(cat "$STATE/vm.public")"
    ;;
  ip) cat "$STATE/vm.public" ;;
  status)
    aws ec2 describe-instances --region "$REGION" \
      --filters Name=tag:Purpose,Values=$PURPOSE Name=tag:Run,Values=$RUN \
      --query 'Reservations[].Instances[].[InstanceId,State.Name,InstanceType,LaunchTime,PublicIpAddress]' \
      --output text
    ;;
  down)
    ids=$(mine pending,running,stopping,stopped)
    if [ -n "$ids" ]; then
      # shellcheck disable=SC2086
      aws ec2 terminate-instances --region "$REGION" --instance-ids $ids >/dev/null
      # shellcheck disable=SC2086
      aws ec2 wait instance-terminated --region "$REGION" --instance-ids $ids
    fi
    sirs=$(aws ec2 describe-spot-instance-requests --region "$REGION" \
      --filters Name=state,Values=open,active Name=launch.key-name,Values=$KEY_NAME \
      --query 'SpotInstanceRequests[].SpotInstanceRequestId' --output text)
    if [ -n "$sirs" ]; then
      # shellcheck disable=SC2086
      aws ec2 cancel-spot-instance-requests --region "$REGION" --spot-instance-request-ids $sirs >/dev/null
    fi
    for sg in $(aws ec2 describe-security-groups --region "$REGION" \
        --filters Name=tag:Purpose,Values=$PURPOSE Name=tag:Run,Values=$RUN \
        --query 'SecurityGroups[].GroupId' --output text); do
      # The ENI can take a few seconds to go after termination.
      for _ in $(seq 1 30); do
        aws ec2 delete-security-group --region "$REGION" --group-id "$sg" >/dev/null 2>&1 && break
        sleep 5
      done
    done
    aws ec2 delete-key-pair --region "$REGION" --key-name "$KEY_NAME" >/dev/null 2>&1 || true
    rm -f "$STATE/vm.public" "$STATE/vm.id"
    bash "${BASH_SOURCE[0]}" check
    ;;
  check)
    for r in us-east-2 us-west-2; do
      echo "== $r, everything tagged Purpose=$PURPOSE (and key pairs / Spot requests by name)"
      echo "instances (not terminated):"
      aws ec2 describe-instances --region "$r" --filters Name=tag:Purpose,Values=$PURPOSE \
        Name=instance-state-name,Values=pending,running,shutting-down,stopping,stopped \
        --query 'Reservations[].Instances[].[InstanceId,State.Name]' --output text
      echo "volumes:"
      aws ec2 describe-volumes --region "$r" --filters Name=tag:Purpose,Values=$PURPOSE \
        --query 'Volumes[].[VolumeId,State]' --output text
      echo "security groups:"
      aws ec2 describe-security-groups --region "$r" --filters Name=tag:Purpose,Values=$PURPOSE \
        --query 'SecurityGroups[].[GroupId,GroupName]' --output text
      echo "key pairs:"
      aws ec2 describe-key-pairs --region "$r" --filters Name=tag:Purpose,Values=$PURPOSE \
        --query 'KeyPairs[].KeyName' --output text
      aws ec2 describe-key-pairs --region "$r" --filters Name=key-name,Values=$KEY_NAME \
        --query 'KeyPairs[].KeyName' --output text
      echo "open/active Spot requests:"
      aws ec2 describe-spot-instance-requests --region "$r" \
        --filters Name=state,Values=open,active Name=tag:Purpose,Values=$PURPOSE \
        --query 'SpotInstanceRequests[].[SpotInstanceRequestId,State]' --output text
    done
    echo "== end of check"
    ;;
  *) sed -n '2,16p' "$0"; exit 2 ;;
esac
