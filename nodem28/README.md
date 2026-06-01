# Contrail ISSU Automation — nodem28 Setup

Automated In-Service Software Upgrade (ISSU) for the nodem28 Contrail cluster.
Upgrades from **21.4.L4.76** to **master.1310** with zero downtime.

## Cluster Topology

| Node     | Mgmt IP         | Ctrl-Data IP   | Role                    |
|----------|-----------------|----------------|-------------------------|
| nodem28  | 10.204.216.2    | 10.10.12.28    | Old Controller + OpenStack |
| nodem30  | 10.204.216.4    | 10.10.12.30    | Old Controller          |
| nodem29  | 10.204.216.3    | 10.10.12.29    | New Controller (master.1310) |
| nodem31  | 10.204.216.176  | 10.10.12.31    | Compute (DPDK) — safe   |
| nodem32  | 10.204.216.158  | 10.10.12.32    | Compute (DPDK) — upgrade |
| nodem33  | 10.204.216.177  | 10.10.12.33    | Compute (DPDK) — upgrade |
| noden30  | 10.204.216.212  | —              | Deployer host           |

## Files

| File | Description |
|------|-------------|
| `automate_issu.py` | Main ISSU automation script (Steps 0–6) |
| `issu_config.yaml` | All ISSU parameters (nodes, auth, DPDK, deployer) |
| `issu_instances_computes.yaml` | Ansible instances file for compute upgrade (Step 5) |
| `contrail_remove_controller.sh` | Controller decommission script (config + analytics Cassandra cleanup) |
| `instances-L5-DPDK-newreg-v4.yaml` | Reference instances file for the full nodem28 cluster |

## ISSU Steps

| Step | Name | Description |
|------|------|-------------|
| 0 | VM Creation | Create test VMs across all computes (canary workload) |
| 1 | Cross-Provisioning | Make old and new controllers aware of each other (BGP peering) |
| 2 | Prepare Sync | Stop conflicting services on new controller, write sync config |
| 3 | Run Sync | Bulk copy + real-time replication of config/analytics data |
| 4 | VM Migration | Live-migrate VMs from upgrade computes to safe compute (nodem31) |
| 5 | Compute Upgrade | Upgrade nodem32/nodem33 vrouter via deployer container on noden30 |
| 6 | VM Redistribution | Migrate VMs back, rebalancing across all computes |

## Usage

```bash
# Dry run — all steps
python3 automate_issu.py --dry-run

# Dry run — single step
python3 automate_issu.py --dry-run --step 0

# Execute single step with custom deployer container
python3 automate_issu.py --step 5 --container-name issu_new_1310

# Full ISSU execution
python3 automate_issu.py --container-name issu_new_1310
```

### CLI Arguments

| Argument | Description |
|----------|-------------|
| `--config` | Config file path (default: `issu_config.yaml`) |
| `--step N` | Run single step 0–6 |
| `--dry-run` | Simulate without executing commands |
| `--container-name` | Deployer container name on noden30 (overrides config) |

## Reports

Each run generates reports under `/var/log/issu_upgrade/run_<timestamp>/`:

- `step_N_<Name>_report.txt` — Per-step report with errors, issues, and output
- `step_N_pre_logs.txt` / `step_N_post_logs.txt` — Node snapshots before/after each step
- `FINAL_REPORT.txt` — Summary table, aggregated errors, and detected issues

Log collection includes contrail-status, docker container states, restart counts, and BGP peers from all nodes.

## Compute DPDK Configuration

All computes run DPDK mode with:
```yaml
vrouter:
  PHYSICAL_INTERFACE: bond0
  CPU_CORE_MASK: "0xff"
  DPDK_UIO_DRIVER: vfio-pci
  HUGE_PAGES: 32000
  AGENT_MODE: dpdk
```

## Prerequisites

- Python 3 with `yaml` module
- `sshpass` installed on the node running the script
- SSH access (root) to all nodes
- Deployer container running on noden30
- OpenStack Keystone accessible at 10.204.216.150
