#!/usr/bin/env bash
# Provisions (and tears down) the remote-throughput benchmark VMs on AWS.
#
#   provision.sh lan       server + LAN client in us-east-2 (cluster placement group)
#   provision.sh wan       WAN client in us-west-2, allowed into the server's SG
#   provision.sh status    every instance tagged Purpose=hardcharger-benchmark
#   provision.sh down      terminate everything tagged Purpose=hardcharger-benchmark and
#                          delete the key pairs, security groups and placement group
#
# Needs AWS credentials in the environment (never printed). Every resource is tagged
# Name=hardcharger-bench-*, Purpose=hardcharger-benchmark, Owner=claude. SSH is open only
# to this machine's public IP; the server's ports only to the client VMs.
#
# Instances are Spot (one-time, terminate on interruption): c8gn.8xlarge was ~$0.30/h
# Spot against $1.90/h on demand. Set MARKET=on-demand to avoid Spot.
set -euo pipefail
export AWS_PAGER=""

EAST=${EAST:-us-east-2}
WEST=${WEST:-us-west-2}
EAST_AZ=${EAST_AZ:-us-east-2c}
WEST_AZ=${WEST_AZ:-us-west-2c}
TYPE=${TYPE:-c8gn.8xlarge}
MARKET=${MARKET:-spot}
STATE=${STATE:-$HOME/.hardcharger-bench}      # key and instance addresses (never committed)
mkdir -p "$STATE"
KEY_NAME=hardcharger-bench-key
KEY_FILE="$STATE/$KEY_NAME"
PURPOSE=hardcharger-benchmark
AMI_PARAM=/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id
SERVER_PORTS=(5201 9494 31337 31338)          # iperf3, Quack, GizmoSQL, Hardcharger

tags() { # tags <resource-type> <name>
  echo "ResourceType=$1,Tags=[{Key=Name,Value=$2},{Key=Purpose,Value=$PURPOSE},{Key=Owner,Value=claude}]"
}

my_ip() {
  local ip
  ip=$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')
  [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "no public IP" >&2; exit 1; }
  echo "$ip"
}

ensure_key() { # ensure_key <region>
  [ -f "$KEY_FILE" ] || ssh-keygen -q -t ed25519 -N "" -C "$KEY_NAME" -f "$KEY_FILE"
  if ! aws ec2 describe-key-pairs --region "$1" --key-names "$KEY_NAME" >/dev/null 2>&1; then
    aws ec2 import-key-pair --region "$1" --key-name "$KEY_NAME" \
      --public-key-material "fileb://$KEY_FILE.pub" \
      --tag-specifications "$(tags key-pair "$KEY_NAME")" >/dev/null
  fi
}

ensure_sg() { # ensure_sg <region> <name> -> group id
  local vpc id
  vpc=$(aws ec2 describe-vpcs --region "$1" --filters Name=is-default,Values=true \
    --query 'Vpcs[0].VpcId' --output text)
  [ -n "$vpc" ] && [ "$vpc" != None ] || { echo "no default VPC in $1" >&2; exit 1; }
  id=$(aws ec2 describe-security-groups --region "$1" \
    --filters Name=group-name,Values="$2" Name=vpc-id,Values="$vpc" \
    --query 'SecurityGroups[0].GroupId' --output text)
  if [ "$id" = None ] || [ -z "$id" ]; then
    id=$(aws ec2 create-security-group --region "$1" --group-name "$2" --vpc-id "$vpc" \
      --description "$2 (hardcharger benchmark)" \
      --tag-specifications "$(tags security-group "$2")" --query GroupId --output text)
  fi
  echo "$id"
}

allow() { # allow <region> <sg> <port> <cidr-or-sg>
  local src
  if [[ "$4" == sg-* ]]; then src="UserIdGroupPairs=[{GroupId=$4}]"; else src="IpRanges=[{CidrIp=$4}]"; fi
  aws ec2 authorize-security-group-ingress --region "$1" --group-id "$2" \
    --ip-permissions "IpProtocol=tcp,FromPort=$3,ToPort=$3,$src" \
    --tag-specifications "$(tags security-group-rule hardcharger-bench-rule)" >/dev/null 2>&1 || true
}

launch() { # launch <region> <az> <sg> <name> [placement-group] -> instance id
  local ami subnet market=() placement=()
  ami=$(aws ssm get-parameter --region "$1" --name "$AMI_PARAM" --query Parameter.Value --output text)
  subnet=$(aws ec2 describe-subnets --region "$1" \
    --filters Name=availability-zone,Values="$2" Name=default-for-az,Values=true \
    --query 'Subnets[0].SubnetId' --output text)
  [ "$MARKET" = spot ] && market=(--instance-market-options \
    'MarketType=spot,SpotOptions={SpotInstanceType=one-time,InstanceInterruptionBehavior=terminate}')
  [ -n "${5:-}" ] && placement=(--placement "GroupName=$5")
  aws ec2 run-instances --region "$1" --image-id "$ami" --instance-type "$TYPE" \
    --key-name "$KEY_NAME" --subnet-id "$subnet" --security-group-ids "$3" \
    --associate-public-ip-address ${placement[@]+"${placement[@]}"} ${market[@]+"${market[@]}"} \
    --instance-initiated-shutdown-behavior terminate \
    --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=100,VolumeType=gp3,DeleteOnTermination=true}' \
    --tag-specifications "$(tags instance "$4")" "$(tags volume "$4")" "$(tags network-interface "$4")" \
    --query 'Instances[0].InstanceId' --output text
}

record() { # record <region> <instance> <name>: write <name>.public / <name>.private
  aws ec2 wait instance-running --region "$1" --instance-ids "$2"
  aws ec2 describe-instances --region "$1" --instance-ids "$2" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text >"$STATE/$3.public"
  aws ec2 describe-instances --region "$1" --instance-ids "$2" \
    --query 'Reservations[0].Instances[0].PrivateIpAddress' --output text >"$STATE/$3.private"
  echo "$3: $(cat "$STATE/$3.public") (private $(cat "$STATE/$3.private"))"
}

case "${1:-}" in
  lan)
    ip=$(my_ip)
    ensure_key "$EAST"
    server_sg=$(ensure_sg "$EAST" hardcharger-bench-server-sg)
    client_sg=$(ensure_sg "$EAST" hardcharger-bench-client-sg)
    allow "$EAST" "$server_sg" 22 "$ip/32"
    allow "$EAST" "$client_sg" 22 "$ip/32"
    for p in "${SERVER_PORTS[@]}"; do allow "$EAST" "$server_sg" "$p" "$client_sg"; done
    aws ec2 create-placement-group --region "$EAST" --group-name hardcharger-bench-pg \
      --strategy cluster --tag-specifications "$(tags placement-group hardcharger-bench-pg)" \
      >/dev/null 2>&1 || true
    s=$(launch "$EAST" "$EAST_AZ" "$server_sg" hardcharger-bench-server hardcharger-bench-pg)
    c=$(launch "$EAST" "$EAST_AZ" "$client_sg" hardcharger-bench-client-lan hardcharger-bench-pg)
    record "$EAST" "$s" server
    record "$EAST" "$c" client-lan
    ;;
  wan)
    ip=$(my_ip)
    ensure_key "$WEST"
    wan_sg=$(ensure_sg "$WEST" hardcharger-bench-client-sg)
    allow "$WEST" "$wan_sg" 22 "$ip/32"
    w=$(launch "$WEST" "$WEST_AZ" "$wan_sg" hardcharger-bench-client-wan)
    record "$WEST" "$w" client-wan
    server_sg=$(ensure_sg "$EAST" hardcharger-bench-server-sg)
    for p in "${SERVER_PORTS[@]}"; do allow "$EAST" "$server_sg" "$p" "$(cat "$STATE/client-wan.public")/32"; done
    ;;
  status)
    for r in "$EAST" "$WEST"; do
      aws ec2 describe-instances --region "$r" --filters Name=tag:Purpose,Values=$PURPOSE \
        --query 'Reservations[].Instances[].[Tags[?Key==`Name`]|[0].Value,InstanceId,State.Name,InstanceType,PublicIpAddress]' \
        --output text
    done
    ;;
  down)
    for r in "$EAST" "$WEST"; do
      ids=$(aws ec2 describe-instances --region "$r" \
        --filters Name=tag:Purpose,Values=$PURPOSE \
        Name=instance-state-name,Values=pending,running,stopping,stopped \
        --query 'Reservations[].Instances[].InstanceId' --output text)
      if [ -n "$ids" ]; then
        # shellcheck disable=SC2086
        aws ec2 terminate-instances --region "$r" --instance-ids $ids >/dev/null
        # shellcheck disable=SC2086
        aws ec2 wait instance-terminated --region "$r" --instance-ids $ids
      fi
      # A one-time Spot request can stay "active" for a while after its instance is gone.
      sirs=$(aws ec2 describe-spot-instance-requests --region "$r" \
        --filters Name=state,Values=open,active Name=launch.key-name,Values=$KEY_NAME \
        --query 'SpotInstanceRequests[].SpotInstanceRequestId' --output text)
      # shellcheck disable=SC2086
      [ -n "$sirs" ] && aws ec2 cancel-spot-instance-requests --region "$r" \
        --spot-instance-request-ids $sirs >/dev/null
    done
    for r in "$EAST" "$WEST"; do
      # Rules that name another group must go before that group can be deleted.
      for sg in $(aws ec2 describe-security-groups --region "$r" \
          --filters Name=tag:Purpose,Values=$PURPOSE --query 'SecurityGroups[].GroupId' --output text); do
        rules=$(aws ec2 describe-security-group-rules --region "$r" \
          --filters Name=group-id,Values="$sg" --query 'SecurityGroupRules[?!IsEgress].SecurityGroupRuleId' --output text)
        # shellcheck disable=SC2086
        [ -n "$rules" ] && aws ec2 revoke-security-group-ingress --region "$r" --group-id "$sg" \
          --security-group-rule-ids $rules >/dev/null
      done
      for sg in $(aws ec2 describe-security-groups --region "$r" \
          --filters Name=tag:Purpose,Values=$PURPOSE --query 'SecurityGroups[].GroupId' --output text); do
        aws ec2 delete-security-group --region "$r" --group-id "$sg" >/dev/null
      done
      aws ec2 delete-key-pair --region "$r" --key-name "$KEY_NAME" >/dev/null 2>&1 || true
    done
    aws ec2 delete-placement-group --region "$EAST" --group-name hardcharger-bench-pg 2>/dev/null || true
    bash "${BASH_SOURCE[0]}" check
    ;;
  check)
    for r in "$EAST" "$WEST"; do
      echo "== $r"
      aws ec2 describe-instances --region "$r" --filters Name=tag:Purpose,Values=$PURPOSE \
        --query 'Reservations[].Instances[].[InstanceId,State.Name]' --output text
      aws ec2 describe-security-groups --region "$r" --filters Name=tag:Purpose,Values=$PURPOSE \
        --query 'SecurityGroups[].GroupId' --output text
      aws ec2 describe-key-pairs --region "$r" --filters Name=tag:Purpose,Values=$PURPOSE \
        --query 'KeyPairs[].KeyName' --output text
      aws ec2 describe-placement-groups --region "$r" --filters Name=tag:Purpose,Values=$PURPOSE \
        --query 'PlacementGroups[].GroupName' --output text
      aws ec2 describe-volumes --region "$r" --filters Name=tag:Purpose,Values=$PURPOSE \
        --query 'Volumes[].[VolumeId,State]' --output text
      aws ec2 describe-spot-instance-requests --region "$r" \
        --filters Name=state,Values=open,active Name=launch.key-name,Values=$KEY_NAME \
        --query 'SpotInstanceRequests[].SpotInstanceRequestId' --output text
    done
    ;;
  *) sed -n '2,15p' "$0"; exit 2 ;;
esac
