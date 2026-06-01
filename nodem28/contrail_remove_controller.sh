#!/usr/bin/env bash
set -Eeuo pipefail

# Contrail/Tungsten Fabric controller decommission automation
# Tested flow for removing nodem29 from a 3-controller cluster (nodem28/29/30)

PRIMARY_CONTROLLER="nodem28"
SURVIVOR_CONTROLLER="nodem30"
REMOVED_CONTROLLER="nodem29"

REMOVED_CONTROLLER_IP="10.204.216.3"
REMOVED_CONTROL_IP="10.10.12.29"

CONTROLLER_IP_LIST_OLD="10.204.216.2,10.204.216.3,10.204.216.4"
CONTROLLER_IP_LIST_NEW="10.204.216.2,10.204.216.4"
CONTROL_IP_LIST_OLD="10.10.12.28,10.10.12.29,10.10.12.30"
CONTROL_IP_LIST_NEW="10.10.12.28,10.10.12.30"

SURVIVOR_ZK_MYID="2"
ROOT_PASSWORD="${CONTRAIL_ROOT_PASSWORD:-}"

VROUTER_NODES="nodem31,nodem32,nodem33"
DRY_RUN="false"

usage() {
  cat <<'EOF'
Usage: contrail_remove_controller.sh [options]

Options:
  --password <password>                 Root password used for SSH (required unless CONTRAIL_ROOT_PASSWORD is set)
  --primary <host>                      Primary controller where script runs (default: nodem28)
  --survivor <host>                     Remaining peer controller (default: nodem30)
  --remove <host>                       Controller to remove (default: nodem29)
  --remove-controller-ip <ip>           Removed controller management IP (default: 10.204.216.3)
  --remove-control-ip <ip>              Removed controller control IP (default: 10.10.12.29)
  --controller-old <csv>                Old controller IP list (default: 10.204.216.2,10.204.216.3,10.204.216.4)
  --controller-new <csv>                New controller IP list (default: 10.204.216.2,10.204.216.4)
  --control-old <csv>                   Old control IP list (default: 10.10.12.28,10.10.12.29,10.10.12.30)
  --control-new <csv>                   New control IP list (default: 10.10.12.28,10.10.12.30)
  --survivor-zk-myid <id>               Expected ZooKeeper myid on survivor (default: 2)
  --vrouters <csv-hosts>                vRouter nodes for validation (default: nodem31,nodem32,nodem33)
  --dry-run                             Print actions without executing changes
  -h, --help                            Show this help

Example:
  CONTRAIL_ROOT_PASSWORD='c0ntrail123' ./contrail_remove_controller.sh --dry-run
  ./contrail_remove_controller.sh --password 'c0ntrail123'
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --password) ROOT_PASSWORD="$2"; shift 2 ;;
    --primary) PRIMARY_CONTROLLER="$2"; shift 2 ;;
    --survivor) SURVIVOR_CONTROLLER="$2"; shift 2 ;;
    --remove) REMOVED_CONTROLLER="$2"; shift 2 ;;
    --remove-controller-ip) REMOVED_CONTROLLER_IP="$2"; shift 2 ;;
    --remove-control-ip) REMOVED_CONTROL_IP="$2"; shift 2 ;;
    --controller-old) CONTROLLER_IP_LIST_OLD="$2"; shift 2 ;;
    --controller-new) CONTROLLER_IP_LIST_NEW="$2"; shift 2 ;;
    --control-old) CONTROL_IP_LIST_OLD="$2"; shift 2 ;;
    --control-new) CONTROL_IP_LIST_NEW="$2"; shift 2 ;;
    --survivor-zk-myid) SURVIVOR_ZK_MYID="$2"; shift 2 ;;
    --vrouters) VROUTER_NODES="$2"; shift 2 ;;
    --dry-run) DRY_RUN="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1"; usage; exit 1 ;;
  esac
done

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

run_cmd() {
  local cmd="$1"
  if [[ "$DRY_RUN" == "true" ]]; then
    log "DRY-RUN: $cmd"
    return 0
  fi
  eval "$cmd"
}

ssh_cmd() {
  local host="$1"
  local cmd="$2"
  if [[ "$DRY_RUN" == "true" ]]; then
    log "DRY-RUN SSH ${host}: $cmd"
    return 0
  fi
  sshpass -p "$ROOT_PASSWORD" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 "root@${host}" "$cmd"
}

require_tools() {
  local tools=(docker ssh sshpass awk sed grep)
  for t in "${tools[@]}"; do
    command -v "$t" >/dev/null 2>&1 || { echo "Missing required tool: $t"; exit 1; }
  done
}

preflight() {
  [[ -n "$ROOT_PASSWORD" ]] || { echo "Root password is required. Use --password or CONTRAIL_ROOT_PASSWORD"; exit 1; }
  require_tools

  log "Preflight checks"
  run_cmd "hostname -f"
  ssh_cmd "$SURVIVOR_CONTROLLER" "hostname -f"
  ssh_cmd "$REMOVED_CONTROLLER" "hostname -f"
}

apply_config_edits_local() {
  log "Applying local config edits on $(hostname -f)"
  local files=(
    /etc/contrail/analytics_alarm/docker-compose.yaml
    /etc/contrail/analytics_database/docker-compose.yaml
    /etc/contrail/common_analytics_database.env
    /etc/contrail/common_analytics.env
    /etc/contrail/common_config_database.env
    /etc/contrail/common_config.env
    /etc/contrail/common_config_rabbitmq.env
    /etc/contrail/common_control.env
    /etc/contrail/common.env
    /etc/contrail/common_webui.env
    /etc/contrail/config_database/docker-compose.yaml
    /etc/contrail/config_rabbitmq/docker-compose.yaml
  )

  local ts
  ts="$(date +%Y%m%d-%H%M%S)"
  run_cmd "mkdir -p /root/contrail-backup-${ts}"

  local f
  for f in "${files[@]}"; do
    run_cmd "[[ -f '$f' ]] && cp -a '$f' '/root/contrail-backup-${ts}/$(basename "$f").bak' || true"
    run_cmd "[[ -f '$f' ]] && sed -i 's/${CONTROLLER_IP_LIST_OLD}/${CONTROLLER_IP_LIST_NEW}/g' '$f' || true"
    run_cmd "[[ -f '$f' ]] && sed -i 's/${CONTROL_IP_LIST_OLD}/${CONTROL_IP_LIST_NEW}/g' '$f' || true"
  done
}

apply_config_edits_remote() {
  log "Applying remote config edits on ${SURVIVOR_CONTROLLER}"
  local cmd
  cmd=$(cat <<EOF
set -Eeuo pipefail
ts=\$(date +%Y%m%d-%H%M%S)
mkdir -p /root/contrail-backup-\${ts}
files="/etc/contrail/analytics_alarm/docker-compose.yaml /etc/contrail/analytics_database/docker-compose.yaml /etc/contrail/common_analytics_database.env /etc/contrail/common_analytics.env /etc/contrail/common_config_database.env /etc/contrail/common_config.env /etc/contrail/common_config_rabbitmq.env /etc/contrail/common_control.env /etc/contrail/common.env /etc/contrail/common_webui.env /etc/contrail/config_database/docker-compose.yaml /etc/contrail/config_rabbitmq/docker-compose.yaml"
for f in \$files; do
  [[ -f "\$f" ]] || continue
  cp -a "\$f" "/root/contrail-backup-\${ts}/\$(basename \"\$f\").bak"
  sed -i 's/${CONTROLLER_IP_LIST_OLD}/${CONTROLLER_IP_LIST_NEW}/g' "\$f"
  sed -i 's/${CONTROL_IP_LIST_OLD}/${CONTROL_IP_LIST_NEW}/g' "\$f"
done
EOF
)
  ssh_cmd "$SURVIVOR_CONTROLLER" "$cmd"
}

restart_stacks_local() {
  log "Restarting stacks locally"
  local dirs=(
    /etc/contrail/redis
    /etc/contrail/config_rabbitmq
    /etc/contrail/config_database
    /etc/contrail/analytics_database
    /etc/contrail/control
    /etc/contrail/config
    /etc/contrail/analytics
    /etc/contrail/analytics_alarm
    /etc/contrail/analytics_snmp
    /etc/contrail/webui
  )
  local d
  for d in "${dirs[@]}"; do
    run_cmd "[[ -f '$d/docker-compose.yaml' ]] && (cd '$d' && (docker compose -f docker-compose.yaml up -d --force-recreate || docker-compose -f docker-compose.yaml up -d --force-recreate)) || true"
  done
}

restart_stacks_remote() {
  log "Restarting stacks on ${SURVIVOR_CONTROLLER}"
  local cmd
  cmd=$(cat <<'EOF'
set -Eeuo pipefail
dirs="/etc/contrail/redis /etc/contrail/config_rabbitmq /etc/contrail/config_database /etc/contrail/analytics_database /etc/contrail/control /etc/contrail/config /etc/contrail/analytics /etc/contrail/analytics_alarm /etc/contrail/analytics_snmp /etc/contrail/webui"
for d in $dirs; do
  [[ -f "$d/docker-compose.yaml" ]] || continue
  (cd "$d" && (docker compose -f docker-compose.yaml up -d --force-recreate || docker-compose -f docker-compose.yaml up -d --force-recreate))
done
EOF
)
  ssh_cmd "$SURVIVOR_CONTROLLER" "$cmd"
}

remove_cassandra_stale_node() {
  log "Removing stale config-database Cassandra node for ${REMOVED_CONTROLLER_IP}"

  if [[ "$DRY_RUN" == "true" ]]; then
    log "DRY-RUN: docker exec config_database-cassandra-1 nodetool -p 7201 removenode <host-id>"
    return 0
  fi

  local host_id
  host_id=$(docker exec config_database-cassandra-1 nodetool -p 7201 status | awk -v ip="$REMOVED_CONTROLLER_IP" '$2 == ip {print $6}')

  if [[ -n "$host_id" ]]; then
    log "Found stale node host-id: ${host_id}"
    docker exec config_database-cassandra-1 nodetool -p 7201 removenode "$host_id" || true
  else
    log "No stale node found for ${REMOVED_CONTROLLER_IP}"
  fi

  docker exec config_database-cassandra-1 nodetool -p 7201 status
}

remove_analytics_cassandra_stale_node() {
  log "Removing stale analytics-database Cassandra node for ${REMOVED_CONTROLLER_IP}"

  if [[ "$DRY_RUN" == "true" ]]; then
    log "DRY-RUN SSH ${REMOVED_CONTROLLER}: docker compose down (analytics_database stack)"
    log "DRY-RUN: docker exec analytics_database-cassandra-1 nodetool -p 7200 removenode <host-id>"
    log "DRY-RUN SSH ${REMOVED_CONTROLLER}: rm -rf /var/lib/docker/volumes/analytics_database_analytics_cassandra/_data/*"
    return 0
  fi

  # Step 1 — stop analytics_database on the node being removed so it enters DN state
  log "Stopping analytics_database stack on ${REMOVED_CONTROLLER}"
  ssh_cmd "$REMOVED_CONTROLLER" \
    'cd /etc/contrail/analytics_database && (docker compose -f docker-compose.yaml down || docker-compose -f docker-compose.yaml down || true)'

  # Step 2 — wait up to 120 s for the node to show up as DN in the ring
  log "Waiting for ${REMOVED_CONTROLLER_IP} to appear as DN in analytics ring..."
  local retries=24
  local found_dn="false"
  while (( retries-- > 0 )); do
    if docker exec analytics_database-cassandra-1 nodetool -p 7200 status 2>/dev/null | awk '{print $1, $2}' | grep -q "^DN ${REMOVED_CONTROLLER_IP}"; then
      found_dn="true"
      break
    fi
    sleep 5
  done

  # Step 3 — removenode from ring (works whether node is DN or already gone)
  local host_id
  host_id=$(docker exec analytics_database-cassandra-1 nodetool -p 7200 status 2>/dev/null | awk -v ip="$REMOVED_CONTROLLER_IP" '$2 == ip {print $6}')

  if [[ -n "$host_id" ]]; then
    log "Found stale analytics node host-id: ${host_id} (dn_detected=${found_dn})"
    docker exec analytics_database-cassandra-1 nodetool -p 7200 removenode "$host_id" || true
  else
    log "No stale analytics node found for ${REMOVED_CONTROLLER_IP} — already removed or not present"
  fi

  # Step 4 — wipe stale gossip/data on removed node so it can rejoin clean if reused
  log "Wiping analytics_database Cassandra data volume on ${REMOVED_CONTROLLER}"
  ssh_cmd "$REMOVED_CONTROLLER" \
    'rm -rf /var/lib/docker/volumes/analytics_database_analytics_cassandra/_data/*'

  log "Analytics ring post-cleanup status:"
  docker exec analytics_database-cassandra-1 nodetool -p 7200 status || true
}

fix_survivor_zk_myid() {
  log "Ensuring ZooKeeper myid=${SURVIVOR_ZK_MYID} on ${SURVIVOR_CONTROLLER}"
  local cmd
  cmd=$(cat <<EOF
set -Eeuo pipefail
myid_file=/var/lib/docker/volumes/config_database_config_zookeeper/_data/myid
if [[ -f "\$myid_file" ]]; then
  current=\$(cat "\$myid_file")
  if [[ "\$current" != "${SURVIVOR_ZK_MYID}" ]]; then
    cp -a "\$myid_file" "\${myid_file}.bak.\$(date +%Y%m%d-%H%M%S)"
    echo "${SURVIVOR_ZK_MYID}" > "\$myid_file"
  fi
fi
(cd /etc/contrail/config_database && (docker compose -f docker-compose.yaml restart zookeeper || docker-compose -f docker-compose.yaml restart zookeeper))
EOF
)
  ssh_cmd "$SURVIVOR_CONTROLLER" "$cmd"
}

stop_removed_controller_services() {
  log "Stopping Contrail stacks on ${REMOVED_CONTROLLER}"
  local cmd
  cmd=$(cat <<'EOF'
set -Eeuo pipefail
dirs="/etc/contrail/webui /etc/contrail/analytics_alarm /etc/contrail/analytics /etc/contrail/config /etc/contrail/control /etc/contrail/analytics_database /etc/contrail/config_database /etc/contrail/config_rabbitmq /etc/contrail/analytics_snmp /etc/contrail/redis"
for d in $dirs; do
  [[ -f "$d/docker-compose.yaml" ]] || continue
  (cd "$d" && (docker compose -f docker-compose.yaml down || docker-compose -f docker-compose.yaml down || true))
done
EOF
)
  ssh_cmd "$REMOVED_CONTROLLER" "$cmd"
}

validate_cluster() {
  log "Validating local controller status"
  run_cmd "contrail-status | grep -Ei 'inactive|initializing|failed|error' || true"

  log "Validating survivor controller status"
  ssh_cmd "$SURVIVOR_CONTROLLER" "contrail-status | grep -Ei 'inactive|initializing|failed|error' || true"

  log "Validating RabbitMQ cluster membership"
  if [[ "$DRY_RUN" == "false" ]]; then
    local rabbit
    rabbit=$(docker ps --format '{{.Names}}' | grep -E 'config_rabbitmq.*rabbitmq' | head -n 1)
    if [[ -n "$rabbit" ]]; then
      docker exec "$rabbit" rabbitmqctl cluster_status | sed -n '1,70p'
    fi
  fi

  log "Validating vRouter nodes"
  IFS=',' read -r -a vr_arr <<< "$VROUTER_NODES"
  local n
  for n in "${vr_arr[@]}"; do
    ssh_cmd "$n" "echo '=== ${n} ==='; contrail-status | grep -Ei '^== Contrail vrouter ==|^agent:|^nodemgr:|inactive|initializing|failed|error' || true"
  done
}

main() {
  log "Starting controller decommission automation"
  log "Primary=${PRIMARY_CONTROLLER} Survivor=${SURVIVOR_CONTROLLER} Remove=${REMOVED_CONTROLLER}"

  preflight
  apply_config_edits_local
  apply_config_edits_remote

  remove_cassandra_stale_node
  remove_analytics_cassandra_stale_node
  fix_survivor_zk_myid

  restart_stacks_local
  restart_stacks_remote

  stop_removed_controller_services
  validate_cluster

  log "Completed controller decommission automation"
}

main
