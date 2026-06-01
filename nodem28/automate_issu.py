#!/usr/bin/env python3
"""
ISSU Automation Script
Automates In-Service Software Upgrade for Contrail controllers
Cluster: nodem28/30 (21.4.L4.76) → nodem29 (master.1310)
Computes: nodem31/32/33

Usage:
  python3 automate_issu.py                    # Full ISSU (Steps 0-6)
  python3 automate_issu.py --step 0           # Individual step
  python3 automate_issu.py --dry-run          # Dry-run mode
  python3 automate_issu.py --config custom.yaml
"""

import subprocess
import yaml
import logging
import sys
import time
import json
import random
import shlex
from datetime import datetime
from pathlib import Path
import argparse


class ISSUAutomation:
    def __init__(self, config_file, dry_run=False, container_name=None):
        self.config = self._load_config(config_file)
        if dry_run:
            self.config['upgrade_settings']['dry_run'] = True
        self.dry_run = self.config['upgrade_settings'].get('dry_run', False)
        # Override deployer container name if provided via CLI
        if container_name:
            self.config['deployer']['container_name'] = container_name
        self._setup_logging()
        self.ssh_user = self.config['ssh']['username']
        self.ssh_pass = self.config['ssh']['password']
        self.step_times = {}
        # Error/output tracking
        self.step_errors = {}      # step_num -> [error strings]
        self.step_outputs = {}     # step_num -> [output lines]
        self.step_logs = {}        # step_num -> {pre: str, post: str}
        self._current_step = None
        self.ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.report_dir = Path(self.config['upgrade_settings']['log_directory']) / f'run_{self.ts}'
        self.report_dir.mkdir(parents=True, exist_ok=True)

    def _load_config(self, path):
        try:
            with open(path) as f:
                cfg = yaml.safe_load(f)
            print(f"[OK] Config loaded: {path}")
            return cfg
        except (FileNotFoundError, yaml.YAMLError) as e:
            print(f"[FAIL] Config error: {e}")
            sys.exit(1)

    def _setup_logging(self):
        log_dir = self.config['upgrade_settings']['log_directory']
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.log_file = f"{log_dir}/issu_{ts}.log"
        logging.basicConfig(
            level=getattr(logging, self.config['upgrade_settings']['log_level']),
            format='%(asctime)s %(levelname)s %(message)s',
            handlers=[logging.FileHandler(self.log_file), logging.StreamHandler(sys.stdout)]
        )
        self.logger = logging.getLogger(__name__)
        self.logger.info(f"Log file: {self.log_file}")
        self.logger.info(f"Dry run: {self.dry_run}")

    # ─── SSH helpers ──────────────────────────────────────────────────

    def _record_output(self, line):
        """Record a line of output for the current step."""
        if self._current_step is not None:
            self.step_outputs.setdefault(self._current_step, []).append(line)

    def _record_error(self, line):
        """Record an error for the current step."""
        if self._current_step is not None:
            self.step_errors.setdefault(self._current_step, []).append(line)

    def ssh(self, host, cmd, description="", timeout=None):
        """Run command on remote host via sshpass. Returns (success, stdout)."""
        timeout = timeout or self.config['upgrade_settings']['timeout_seconds']
        ssh_cmd = (
            f"sshpass -p {shlex.quote(self.ssh_pass)} "
            f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 "
            f"{self.ssh_user}@{host} {shlex.quote(cmd)}"
        )
        if description:
            self.logger.info(f"  {description}")
            self._record_output(f"[{host}] {description}")
        self.logger.debug(f"  SSH {host}: {cmd[:120]}")

        if self.dry_run:
            self.logger.info(f"  [DRY-RUN] SSH {host}: {cmd[:200]}")
            self._record_output(f"  [DRY-RUN] SSH {host}: {cmd[:200]}")
            return True, "DRY_RUN"

        try:
            r = subprocess.run(ssh_cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            if r.returncode == 0:
                self.logger.debug(f"  OK ({len(r.stdout)} bytes)")
                self._record_output(f"  OK: {r.stdout[:300]}")
                return True, r.stdout
            else:
                err_msg = f"FAIL on {host} (rc={r.returncode}): {r.stderr[:500]}"
                self.logger.error(f"  {err_msg}")
                self._record_error(err_msg)
                self._record_output(f"  ERROR: {err_msg}")
                return False, r.stderr
        except subprocess.TimeoutExpired:
            err_msg = f"TIMEOUT on {host} ({timeout}s): {cmd[:100]}"
            self.logger.error(f"  {err_msg}")
            self._record_error(err_msg)
            return False, "TIMEOUT"
        except Exception as e:
            err_msg = f"EXCEPTION on {host}: {e}"
            self.logger.error(f"  {err_msg}")
            self._record_error(err_msg)
            return False, str(e)

    def local(self, cmd, description="", timeout=300):
        """Run command locally. Returns (success, stdout)."""
        if description:
            self.logger.info(f"  {description}")
            self._record_output(f"[local] {description}")
        if self.dry_run:
            self.logger.info(f"  [DRY-RUN] LOCAL: {cmd[:200]}")
            self._record_output(f"  [DRY-RUN] LOCAL: {cmd[:200]}")
            return True, "DRY_RUN"
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            if r.returncode == 0:
                self._record_output(f"  OK: {r.stdout[:300]}")
                return True, r.stdout
            err_msg = f"LOCAL FAIL (rc={r.returncode}): {r.stderr[:500]}"
            self.logger.error(f"  {err_msg}")
            self._record_error(err_msg)
            return False, r.stderr
        except Exception as e:
            err_msg = f"LOCAL EXCEPTION: {e}"
            self.logger.error(f"  {err_msg}")
            self._record_error(err_msg)
            return False, str(e)

    def _get_openstack_env(self):
        """Return export lines for OpenStack credentials."""
        auth = self.config['auth']
        return (
            f"export OS_PROJECT_DOMAIN_NAME=default; "
            f"export OS_USER_DOMAIN_NAME=default; "
            f"export OS_PROJECT_NAME={auth['admin_tenant_name']}; "
            f"export OS_USERNAME={auth['admin_user']}; "
            f"export OS_PASSWORD={auth['admin_password']}; "
            f"export OS_AUTH_URL={auth['keystone_auth_url']}; "
            f"export OS_IDENTITY_API_VERSION=3"
        )

    def os_cmd(self, openstack_cmd, description=""):
        """Run an OpenStack CLI command inside kolla_toolbox on the primary old controller."""
        host = self.config['old_controllers'][0]['mgmt_ip']
        full = f"docker exec -u 0 kolla_toolbox bash -c '{self._get_openstack_env()}; {openstack_cmd}'"
        return self.ssh(host, full, description)

    def _check_error(self, success, step_name):
        """Abort if not success and continue_on_error is false."""
        if not success:
            self._record_error(f"FATAL: {step_name} failed")
            if not self.config['upgrade_settings'].get('continue_on_error', False):
                self._write_step_report(self._current_step)
                self._write_final_report()
                self.logger.error(f"FATAL: {step_name} failed — aborting")
                sys.exit(1)

    # ─── Log Collection ──────────────────────────────────────────────

    def _collect_node_snapshot(self, host, label):
        """Collect contrail-status, docker state, BGP peers from a node. Returns multiline string."""
        lines = [f"===== {label} ({host}) ====="]
        checks = [
            ("contrail-status", 'contrail-status 2>/dev/null'),
            ("docker-ps", 'docker ps --format "table {{.Names}}\\t{{.Status}}" 2>/dev/null'),
            ("restart-counts", 'docker inspect $(docker ps -q) --format "{{.Name}} RestartCount={{.RestartCount}}" 2>/dev/null | grep -v "=0$" || echo "All RestartCount=0"'),
            ("bgp-peers", 'curl -sm5 "http://127.0.0.1:8083/Snh_ShowBgpNeighborSummaryReq" 2>/dev/null | grep -oP "(?<=<peer_address>)[^<]+" || echo "No BGP peers or no control"'),
        ]
        for name, cmd in checks:
            ok, out = self.ssh(host, cmd, timeout=30)
            lines.append(f"--- {name} ---")
            lines.append(out.strip() if ok else f"FAILED: {out[:200]}")
            lines.append("")
        return "\n".join(lines)

    def _collect_logs(self, phase="pre"):
        """Collect snapshots from all controllers and computes."""
        self.logger.info(f"  Collecting {phase}-step logs from all nodes...")
        snapshots = []
        all_nodes = []
        for c in self.config['old_controllers']:
            all_nodes.append((c['mgmt_ip'], f"old-ctrl-{c['hostname']}"))
        for c in self.config['new_controllers']:
            all_nodes.append((c['mgmt_ip'], f"new-ctrl-{c['hostname']}"))
        for c in self.config['compute_nodes']:
            all_nodes.append((c['mgmt_ip'], f"compute-{c['hostname']}"))

        for ip, label in all_nodes:
            snap = self._collect_node_snapshot(ip, label)
            snapshots.append(snap)

        combined = f"\n{'='*70}\n".join(snapshots)
        if self._current_step is not None:
            self.step_logs.setdefault(self._current_step, {})[phase] = combined

            # Write to file
            fname = self.report_dir / f"step_{self._current_step}_{phase}_logs.txt"
            with open(fname, 'w') as f:
                f.write(f"Collected at: {datetime.now().isoformat()}\n")
                f.write(f"Phase: {phase}\n\n")
                f.write(combined)
            self.logger.info(f"  Logs saved: {fname}")

    def _scan_for_issues(self, text):
        """Scan log text for common Contrail errors. Returns list of issue strings."""
        issues = []
        error_patterns = [
            'initializing', 'inactive', 'failed', 'error', 'timeout',
            'not connected', 'DOWN', 'crash', 'RestartCount=',
            'TIMEOUT', 'refused', 'unreachable'
        ]
        for line in text.split('\n'):
            low = line.lower()
            # skip lines that just say "All RestartCount=0"
            if 'all restartcount=0' in low:
                continue
            for pat in error_patterns:
                if pat.lower() in low and 'restartcount=0' not in low:
                    issues.append(line.strip())
                    break
        return issues

    # ─── Step Report Writer ──────────────────────────────────────────

    def _write_step_report(self, step_num):
        """Write a per-step output report file."""
        if step_num is None:
            return
        steps = {
            0: "VM_Creation", 1: "Cross_Provisioning", 2: "Prepare_Sync",
            3: "Run_Sync", 4: "VM_Migration", 5: "Compute_Upgrade",
            6: "VM_Redistribution"
        }
        name = steps.get(step_num, f"Step_{step_num}")
        fname = self.report_dir / f"step_{step_num}_{name}_report.txt"

        errors = self.step_errors.get(step_num, [])
        outputs = self.step_outputs.get(step_num, [])
        elapsed = self.step_times.get(step_num, 0)
        status = "FAILED" if errors else "SUCCESS"

        # Scan pre/post logs for issues
        log_issues = []
        for phase in ['pre', 'post']:
            log_text = self.step_logs.get(step_num, {}).get(phase, '')
            if log_text:
                found = self._scan_for_issues(log_text)
                if found:
                    log_issues.extend([f"[{phase}] {i}" for i in found])

        with open(fname, 'w') as f:
            f.write(f"{'='*70}\n")
            f.write(f"STEP {step_num}: {name}\n")
            f.write(f"Status: {status}\n")
            f.write(f"Duration: {elapsed:.0f}s\n")
            f.write(f"Timestamp: {datetime.now().isoformat()}\n")
            f.write(f"{'='*70}\n\n")

            # Errors section
            f.write(f"--- ERRORS ({len(errors)}) ---\n")
            if errors:
                for e in errors:
                    f.write(f"  ✗ {e}\n")
            else:
                f.write("  None\n")
            f.write("\n")

            # Issues detected in logs
            f.write(f"--- ISSUES DETECTED IN LOGS ({len(log_issues)}) ---\n")
            if log_issues:
                for issue in log_issues:
                    f.write(f"  ⚠ {issue}\n")
            else:
                f.write("  None\n")
            f.write("\n")

            # Output section
            f.write(f"--- OUTPUT ({len(outputs)} entries) ---\n")
            for line in outputs:
                f.write(f"  {line}\n")
            f.write("\n")

        self.logger.info(f"  Step report: {fname}")
        # Also print error summary to console
        if errors:
            self.logger.error(f"  ERRORS in Step {step_num}:")
            for e in errors:
                self.logger.error(f"    ✗ {e}")
        if log_issues:
            self.logger.warning(f"  ISSUES detected in Step {step_num} logs:")
            for i in log_issues[:10]:  # cap at 10 to avoid spam
                self.logger.warning(f"    ⚠ {i}")

    def _write_final_report(self):
        """Write a final summary report across all steps."""
        fname = self.report_dir / "FINAL_REPORT.txt"
        total_errors = sum(len(v) for v in self.step_errors.values())
        steps = {
            0: "VM_Creation", 1: "Cross_Provisioning", 2: "Prepare_Sync",
            3: "Run_Sync", 4: "VM_Migration", 5: "Compute_Upgrade",
            6: "VM_Redistribution"
        }

        with open(fname, 'w') as f:
            f.write(f"{'#'*70}\n")
            f.write(f"ISSU FINAL REPORT\n")
            f.write(f"Generated: {datetime.now().isoformat()}\n")
            f.write(f"Log file: {self.log_file}\n")
            f.write(f"Report dir: {self.report_dir}\n")
            f.write(f"Dry run: {self.dry_run}\n")
            f.write(f"Total errors: {total_errors}\n")
            f.write(f"{'#'*70}\n\n")

            # Per-step summary table
            f.write(f"{'Step':<6} {'Name':<25} {'Status':<10} {'Duration':<10} {'Errors':<8}\n")
            f.write(f"{'-'*6} {'-'*25} {'-'*10} {'-'*10} {'-'*8}\n")
            for snum in sorted(set(list(self.step_times.keys()) + list(self.step_errors.keys()))):
                sname = steps.get(snum, f"Step_{snum}")
                errs = len(self.step_errors.get(snum, []))
                elapsed = self.step_times.get(snum, 0)
                status = "FAILED" if errs else "SUCCESS"
                f.write(f"{snum:<6} {sname:<25} {status:<10} {elapsed:<10.0f} {errs:<8}\n")
            f.write("\n")

            # All errors
            if total_errors > 0:
                f.write(f"{'='*70}\n")
                f.write(f"ALL ERRORS\n")
                f.write(f"{'='*70}\n")
                for snum in sorted(self.step_errors.keys()):
                    sname = steps.get(snum, f"Step_{snum}")
                    f.write(f"\n--- Step {snum} ({sname}) ---\n")
                    for e in self.step_errors[snum]:
                        f.write(f"  ✗ {e}\n")
                f.write("\n")

            # All issues from log scans
            all_issues = []
            for snum in sorted(self.step_logs.keys()):
                for phase in ['pre', 'post']:
                    text = self.step_logs.get(snum, {}).get(phase, '')
                    if text:
                        found = self._scan_for_issues(text)
                        all_issues.extend([(snum, phase, i) for i in found])

            if all_issues:
                f.write(f"{'='*70}\n")
                f.write(f"ALL ISSUES DETECTED IN LOGS ({len(all_issues)})\n")
                f.write(f"{'='*70}\n")
                for snum, phase, issue in all_issues:
                    f.write(f"  Step {snum} [{phase}]: ⚠ {issue}\n")
                f.write("\n")
            else:
                f.write("No issues detected in collected logs.\n\n")

            # Files generated
            f.write(f"{'='*70}\n")
            f.write(f"FILES GENERATED\n")
            f.write(f"{'='*70}\n")
            for p in sorted(self.report_dir.iterdir()):
                f.write(f"  {p.name} ({p.stat().st_size / 1024:.1f} KB)\n")

        self.logger.info(f"Final report: {fname}")
        # Print summary to console
        self.logger.info(f"\n{'#'*70}")
        self.logger.info(f"FINAL REPORT: {total_errors} errors across {len(self.step_times)} steps")
        if total_errors > 0:
            for snum in sorted(self.step_errors.keys()):
                for e in self.step_errors[snum]:
                    self.logger.error(f"  Step {snum}: ✗ {e}")
        self.logger.info(f"Reports: {self.report_dir}")
        self.logger.info(f"{'#'*70}")

    # ─── Step 0: VM Creation ─────────────────────────────────────────

    def step_0_create_vms(self):
        """Create test VMs on compute nodes."""
        self.logger.info("=" * 70)
        self.logger.info("STEP 0: CREATE VMs ON COMPUTE NODES")
        self.logger.info("=" * 70)

        vm_cfg = self.config['vm_creation']
        vms_per = vm_cfg['vms_per_compute']

        # 0a: Upload cirros image if not present
        self.logger.info("Step 0a: Ensure cirros image exists")
        ok, out = self.os_cmd("openstack image list -f json", "Listing images")
        if ok and out != "DRY_RUN":
            images = json.loads(out) if out.strip() else []
            if not any(vm_cfg['image_name'] in img.get('Name', '') for img in images):
                self.logger.info("  Uploading cirros image...")
                self.os_cmd(
                    f"curl -sL {vm_cfg['image_url']} -o /tmp/cirros.img && "
                    f"openstack image create {vm_cfg['image_name']} "
                    f"--disk-format qcow2 --container-format bare "
                    f"--file /tmp/cirros.img --public",
                    "Upload cirros image"
                )
            else:
                self.logger.info("  Image already exists")

        # 0b: Create flavor if not present
        self.logger.info("Step 0b: Ensure flavor exists")
        ok, out = self.os_cmd("openstack flavor list -f json", "Listing flavors")
        if ok and out != "DRY_RUN":
            flavors = json.loads(out) if out.strip() else []
            if not any(vm_cfg['flavor_name'] in f.get('Name', '') for f in flavors):
                self.os_cmd(
                    f"openstack flavor create {vm_cfg['flavor_name']} "
                    f"--ram {vm_cfg['flavor_ram']} --vcpus {vm_cfg['flavor_vcpus']} "
                    f"--disk {vm_cfg['flavor_disk']} --public",
                    "Create flavor"
                )

        # 0c: Create test network/subnet if not present
        self.logger.info("Step 0c: Ensure test network exists")
        vn_name = vm_cfg['network_name']
        ok, out = self.os_cmd("openstack network list -f json", "Listing networks")
        if ok and out != "DRY_RUN":
            nets = json.loads(out) if out.strip() else []
            if not any(vn_name in n.get('Name', '') for n in nets):
                self.os_cmd(
                    f"openstack network create {vn_name}",
                    f"Create network {vn_name}"
                )
                self.os_cmd(
                    f"openstack subnet create {vn_name}-subnet "
                    f"--network {vn_name} --subnet-range {vm_cfg['subnet_cidr']}",
                    f"Create subnet {vm_cfg['subnet_cidr']}"
                )

        # 0d: Create VMs on each compute
        self.logger.info("Step 0d: Create VMs on computes")
        computes = self.config['compute_nodes']
        for compute in computes:
            for i in range(1, vms_per + 1):
                vm_name = f"issu-vm-{compute['hostname']}-{i}"
                self.os_cmd(
                    f"openstack server create {vm_name} "
                    f"--image {vm_cfg['image_name']} "
                    f"--flavor {vm_cfg['flavor_name']} "
                    f"--network {vn_name} "
                    f"--availability-zone nova:{compute['hostname']}",
                    f"Create {vm_name}"
                )
                time.sleep(3)

        # 0e: Verify VMs
        time.sleep(30)
        self.logger.info("Step 0e: Verify VMs")
        ok, out = self.os_cmd("openstack server list --all -f json", "List all VMs")
        if ok and out != "DRY_RUN":
            vms = json.loads(out) if out.strip() else []
            active = sum(1 for v in vms if v.get('Status') == 'ACTIVE')
            total = len(vms)
            self.logger.info(f"  VMs: {active}/{total} ACTIVE")
            if active < total:
                self.logger.warning(f"  {total - active} VMs not ACTIVE — check manually")

        self.logger.info("STEP 0 COMPLETE")
        return True

    # ─── Step 1: Cross-Provisioning ──────────────────────────────────

    def step_1_cross_provision(self):
        """Cross-provision old and new controllers."""
        self.logger.info("=" * 70)
        self.logger.info("STEP 1: CONTROLLER CROSS-PROVISIONING")
        self.logger.info("=" * 70)

        old_ctrls = self.config['old_controllers']
        new_ctrl = self.config['new_controllers'][0]
        auth = self.config['auth']
        net = self.config['network']

        auth_params = (
            f"--admin_user {auth['admin_user']} "
            f"--admin_password {auth['admin_password']} "
            f"--admin_tenant_name {auth['admin_tenant_name']}"
        )

        # 1a: Provision each old controller on the new controller
        self.logger.info("Phase 1a: Register OLD controllers on NEW controller")
        for old in old_ctrls:
            provision_cmd = (
                f"docker exec {self.config['docker']['config_api_container']} "
                f"python /opt/contrail/utils/provision_control.py "
                f"--host_name {old['hostname']} "
                f"--host_ip {old['control_data_ip']} "
                f"--api_server_ip {new_ctrl['api_server_ip']} "
                f"--api_server_port {net['api_server_port']} "
                f"--oper add "
                f"--router_asn {net['router_asn']} "
                f"--ibgp_auto_mesh "
                f"{auth_params}"
            )
            ok, _ = self.ssh(new_ctrl['mgmt_ip'], provision_cmd,
                             f"Provision {old['hostname']} on {new_ctrl['hostname']}")
            self._check_error(ok, f"Provision {old['hostname']} on new ctrl")
            time.sleep(5)

        # 1b: Provision the new controller on each old controller
        self.logger.info("Phase 1b: Register NEW controller on OLD controllers")
        for old in old_ctrls:
            api_container = self.config['docker']['config_api_container']
            provision_cmd = (
                f"docker exec {api_container} "
                f"python /opt/contrail/utils/provision_control.py "
                f"--host_name {new_ctrl['hostname']} "
                f"--host_ip {new_ctrl['control_data_ip']} "
                f"--api_server_ip {old['api_server_ip']} "
                f"--api_server_port {net['api_server_port']} "
                f"--oper add "
                f"--router_asn {net['router_asn']} "
                f"--ibgp_auto_mesh "
                f"{auth_params}"
            )
            ok, _ = self.ssh(old['mgmt_ip'], provision_cmd,
                             f"Provision {new_ctrl['hostname']} on {old['hostname']}")
            self._check_error(ok, f"Provision new ctrl on {old['hostname']}")
            time.sleep(5)

        # 1c: Verify BGP sessions establish
        self.logger.info("Phase 1c: Verify BGP sessions")
        time.sleep(15)
        for ctrl_ip in [new_ctrl['mgmt_ip']] + [o['mgmt_ip'] for o in old_ctrls]:
            ok, out = self.ssh(ctrl_ip,
                               'curl -sm10 "http://127.0.0.1:8083/Snh_ShowBgpNeighborSummaryReq" 2>/dev/null | '
                               'grep -oP "(?<=<peer_address>)[^<]+" | head -10',
                               f"Check BGP peers on {ctrl_ip}")
            if ok and out.strip():
                self.logger.info(f"    BGP peers on {ctrl_ip}: {out.strip()}")

        self.logger.info("STEP 1 COMPLETE")
        return True

    # ─── Step 2: Stop config services + write issu.conf ──────────────

    def step_2_prepare_sync(self):
        """Stop config services on new controller and write contrail-issu.conf."""
        self.logger.info("=" * 70)
        self.logger.info("STEP 2: STOP CONFIG SERVICES & PREPARE ISSU CONF")
        self.logger.info("=" * 70)

        new = self.config['new_controllers'][0]

        # 2a: Stop config-schema, svc-monitor, device-manager on new
        services_to_stop = ['config-schema-1', 'config-svcmonitor-1', 'config-devicemgr-1']
        for svc in services_to_stop:
            ok, _ = self.ssh(new['mgmt_ip'],
                             f"docker stop {svc}",
                             f"Stop {svc} on {new['hostname']}")
            self._check_error(ok, f"Stop {svc}")
            time.sleep(2)

        # 2b: Write contrail-issu.conf
        issu_cfg = self.config['issu_pre_sync']
        conf_content = (
            "[DEFAULTS]\\n\\n"
            f"old_rabbit_user = {issu_cfg['old_rabbit_user']}\\n"
            f"old_rabbit_password = {issu_cfg['old_rabbit_password']}\\n"
            f"old_rabbit_q_name = {issu_cfg['old_rabbit_q_name']}\\n"
            f"old_rabbit_vhost = {issu_cfg['old_rabbit_vhost']}\\n"
            f"old_rabbit_port = {issu_cfg['old_rabbit_port']}\\n"
            f"old_rabbit_address_list = {issu_cfg['old_rabbit_address_list']}\\n\\n"
            f"new_rabbit_user = {issu_cfg['new_rabbit_user']}\\n"
            f"new_rabbit_password = {issu_cfg['new_rabbit_password']}\\n"
            f"new_rabbit_q_name = {issu_cfg['new_rabbit_q_name']}\\n"
            f"new_rabbit_vhost = {issu_cfg['new_rabbit_vhost']}\\n"
            f"new_rabbit_port = {issu_cfg['new_rabbit_port']}\\n"
            f"new_rabbit_address_list = {issu_cfg['new_rabbit_address_list']}\\n\\n"
            f"old_alter_table = {str(issu_cfg['old_alter_table']).lower()}\\n"
            f"new_alter_table = {str(issu_cfg['new_alter_table']).lower()}\\n\\n"
            f"old_cassandra_address_list = {issu_cfg['old_cassandra_address_list']}\\n"
            f"old_zookeeper_address_list = {issu_cfg['old_zookeeper_address_list']}\\n"
            f"new_cassandra_address_list = {issu_cfg['new_cassandra_address_list']}\\n"
            f"new_zookeeper_address_list = {issu_cfg['new_zookeeper_address_list']}\\n\\n"
            f"new_api_info = {issu_cfg['new_api_info']}\\n"
        )

        write_cmd = f"echo -e '{conf_content}' > /etc/contrail/contrail-issu.conf"
        ok, _ = self.ssh(new['mgmt_ip'], write_cmd,
                         f"Write contrail-issu.conf on {new['hostname']}")
        self._check_error(ok, "Write contrail-issu.conf")

        # Verify
        self.ssh(new['mgmt_ip'], "cat /etc/contrail/contrail-issu.conf",
                 "Verify contrail-issu.conf")

        self.logger.info("STEP 2 COMPLETE")
        return True

    # ─── Step 3: Run issu-pre-sync + run-sync ────────────────────────

    def step_3_run_sync(self):
        """Run containerized pre-sync and run-sync on new controller."""
        self.logger.info("=" * 70)
        self.logger.info("STEP 3: RUN ISSU SYNC PROCESSES")
        self.logger.info("=" * 70)

        new = self.config['new_controllers'][0]
        api_container = self.config['docker']['config_api_container']

        # 3a: Get config-api image for running ISSU containers
        ok, image = self.ssh(new['mgmt_ip'],
                             f"docker inspect {api_container} --format '{{{{.Config.Image}}}}'",
                             "Get config-api image")
        if ok:
            image = image.strip()
        else:
            image = f"{self.config['docker']['registry']}/contrail-controller-config-api:{new['contrail_version']}"
        self.logger.info(f"  Using image: {image}")

        # 3b: Run pre-sync (blocking)
        pre_sync_cmd = (
            f"docker run --rm --network host "
            f"-v /etc/contrail/contrail-issu.conf:/etc/contrail/contrail-issu.conf:ro "
            f"-v /root/.ssh:/root/.ssh "
            f"--name issu-pre-sync "
            f"{image} "
            f'/bin/bash -c "/usr/bin/contrail-issu-pre-sync -c /etc/contrail/contrail-issu.conf"'
        )
        self.logger.info("Step 3a: Running contrail-issu-pre-sync (blocking)...")
        ok, out = self.ssh(new['mgmt_ip'], pre_sync_cmd,
                           "Run pre-sync", timeout=1800)
        self._check_error(ok, "pre-sync")
        self.logger.info("  pre-sync complete")

        # 3c: Run run-sync (detached)
        run_sync_cmd = (
            f"docker run -d --network host "
            f"-v /etc/contrail/contrail-issu.conf:/etc/contrail/contrail-issu.conf:ro "
            f"-v /root/.ssh:/root/.ssh "
            f"--name issu-run-sync "
            f"{image} "
            f'/bin/bash -c "/usr/bin/contrail-issu-run-sync -c /etc/contrail/contrail-issu.conf"'
        )
        self.logger.info("Step 3b: Starting contrail-issu-run-sync (detached)...")
        ok, out = self.ssh(new['mgmt_ip'], run_sync_cmd,
                           "Start run-sync container")
        self._check_error(ok, "run-sync")

        # 3d: Verify run-sync is running
        time.sleep(10)
        ok, out = self.ssh(new['mgmt_ip'],
                           "docker ps --format '{{.Names}} {{.Status}}' | grep issu-run-sync",
                           "Verify run-sync running")
        if ok and out.strip():
            self.logger.info(f"  run-sync status: {out.strip()}")

        self.logger.info("STEP 3 COMPLETE")
        return True

    # ─── Step 4: VM Migration to safe compute ────────────────────────

    def step_4_migrate_vms(self):
        """Migrate all VMs to the safe compute before upgrading others."""
        self.logger.info("=" * 70)
        self.logger.info("STEP 4: PRE-UPGRADE VM MIGRATION")
        self.logger.info("=" * 70)

        mig = self.config['vm_migration']
        safe = mig['safe_compute']
        safe_ip = mig['safe_compute_ip']
        safe_fqdn = mig['safe_compute_fqdn']
        nova_path = mig['nova_instances_path']
        computes_to_upgrade = mig['computes_to_upgrade']
        old_ctrl = self.config['old_controllers'][0]

        # Find compute details for each
        compute_map = {c['hostname']: c for c in self.config['compute_nodes']}

        migrated_vms = []
        for compute_name in computes_to_upgrade:
            compute = compute_map.get(compute_name)
            if not compute:
                self.logger.warning(f"  Compute {compute_name} not found in config")
                continue

            self.logger.info(f"  Migrating VMs from {compute_name} → {safe}")

            # Get VM UUIDs on this compute
            ok, out = self.ssh(
                compute['mgmt_ip'],
                f"ls -1 {nova_path} 2>/dev/null | grep -E '^[0-9a-f]{{8}}-' || true",
                f"List VMs on {compute_name}"
            )
            if not ok or not out.strip() or out == "DRY_RUN":
                self.logger.info(f"  No VMs on {compute_name}")
                continue

            vm_uuids = [u.strip() for u in out.strip().split('\n') if u.strip()]
            self.logger.info(f"  Found {len(vm_uuids)} VMs on {compute_name}")

            for vm_uuid in vm_uuids:
                self.logger.info(f"    Migrating {vm_uuid}...")

                # Stop VM
                self.os_cmd(f"openstack server stop {vm_uuid}", f"Stop {vm_uuid}")
                time.sleep(3)

                # SCP VM files
                self.ssh(
                    compute['mgmt_ip'],
                    f"scp -r -o StrictHostKeyChecking=no {nova_path}/{vm_uuid} "
                    f"{self.ssh_user}@{safe_ip}:{nova_path}/",
                    f"Copy {vm_uuid} files"
                )

                # Update Nova DB
                sql = (
                    f"UPDATE instances SET node='{safe_fqdn}', host='{safe}' "
                    f"WHERE uuid='{vm_uuid}'"
                )
                db_cmd = (
                    f"docker exec -i {mig['mariadb_container']} "
                    f"mysql -u {mig['mariadb_user']} -p{mig['mariadb_password']} nova "
                    f'-e "{sql}"'
                )
                self.ssh(old_ctrl['mgmt_ip'], db_cmd, f"Update Nova DB for {vm_uuid}")

                # Start VM
                self.os_cmd(f"openstack server start {vm_uuid}", f"Start {vm_uuid}")
                migrated_vms.append(vm_uuid)
                time.sleep(3)

        # Verify all VMs ACTIVE
        if migrated_vms:
            self.logger.info(f"  Verifying {len(migrated_vms)} migrated VMs...")
            time.sleep(30)
            ok, out = self.os_cmd("openstack server list --all -f json", "List VMs")
            if ok and out != "DRY_RUN":
                vms = json.loads(out) if out.strip() else []
                for vm in vms:
                    if vm.get('ID') in migrated_vms and vm.get('Status') != 'ACTIVE':
                        self.logger.warning(f"  VM {vm.get('ID')} is {vm.get('Status')}, not ACTIVE")

        self.logger.info("STEP 4 COMPLETE")
        return True

    # ─── Step 5: Compute Upgrade via Ansible ─────────────────────────

    def step_5_upgrade_computes(self):
        """Upgrade computes using deployer container on noden30."""
        self.logger.info("=" * 70)
        self.logger.info("STEP 5: COMPUTE UPGRADE")
        self.logger.info("=" * 70)

        dep = self.config['deployer']
        mig = self.config['vm_migration']
        container = dep['container_name']
        deployer_ip = dep['host_ip']
        instances_path = "/root/contrail-ansible-deployer/config/instances.yaml"

        self.logger.info(f"  Deployer host: {dep['host']} ({deployer_ip})")
        self.logger.info(f"  Deployer container: {container}")
        self.logger.info(f"  Computes to upgrade: {mig['computes_to_upgrade']}")
        self.logger.info(f"  Excluded (has VMs): {mig['safe_compute']}")

        # 5a: Pull existing instances.yaml from inside the container
        self.logger.info("Step 5a: Pull existing instances.yaml from deployer container")
        ok, _ = self.ssh(deployer_ip,
                         f"docker cp {container}:{instances_path} /tmp/instances_current.yaml",
                         "Extract instances.yaml from container")
        self._check_error(ok, "docker cp instances out of container")

        # 5b: Add compute entries to the existing instances.yaml
        self.logger.info("Step 5b: Add compute entries to instances.yaml")
        compute_map = {c['hostname']: c for c in self.config['compute_nodes']}
        computes_to_add = []
        for cname in mig['computes_to_upgrade']:
            c = compute_map.get(cname)
            if c:
                computes_to_add.append(c)

        if not computes_to_add:
            self.logger.error("  No computes to upgrade found in config")
            self._record_error("No computes found for upgrade")
            return False

        # Build a Python snippet that loads the YAML, adds compute entries, writes back
        # This runs on noden30 via SSH — uses full vrouter params (DPDK, hugepages, etc.)
        add_script = (
            "python3 -c \"\n"
            "import yaml, sys, json\n"
            "with open('/tmp/instances_current.yaml') as f:\n"
            "    data = yaml.safe_load(f)\n"
            "if 'instances' not in data:\n"
            "    data['instances'] = {}\n"
        )
        for c in computes_to_add:
            vrouter_params = c.get('vrouter', {'PHYSICAL_INTERFACE': 'bond0'})
            add_script += (
                f"data['instances']['{c['hostname']}'] = {{"
                f"'provider': 'bms', "
                f"'ip': '{c['mgmt_ip']}', "
                f"'roles': {{"
                f"'openstack_compute': None, "
                f"'vrouter': {repr(vrouter_params)}"
                f"}}}}\n"
            )
        add_script += (
            "with open('/tmp/instances_current.yaml', 'w') as f:\n"
            "    yaml.dump(data, f, default_flow_style=False)\n"
            "print('Computes added:', list(data['instances'].keys()))\n"
            "for h in " + repr([c['hostname'] for c in computes_to_add]) + ":\n"
            "    print(f'  {h}: {data[\"instances\"][h][\"roles\"][\"vrouter\"]}')\n"
            "\""
        )
        ok, out = self.ssh(deployer_ip, add_script,
                           f"Add computes {[c['hostname'] for c in computes_to_add]} to instances.yaml")
        self._check_error(ok, "Add compute entries to instances.yaml")
        if ok:
            self.logger.info(f"    {out.strip()}")

        # 5c: Push updated instances.yaml back into the container
        self.logger.info("Step 5c: Push updated instances.yaml back into container")
        ok, _ = self.ssh(deployer_ip,
                         f"docker cp /tmp/instances_current.yaml {container}:{instances_path}",
                         "Copy updated instances.yaml into container")
        self._check_error(ok, "docker cp instances into container")

        # 5d: Run configure_instances.yml
        self.logger.info("Step 5d: Run configure_instances.yml")
        ok, _ = self.ssh(deployer_ip,
                         f"docker exec {container} bash -c '"
                         f"cd /root/contrail-ansible-deployer && "
                         f"ansible-playbook -i inventory/ -e orchestrator=openstack "
                         f"playbooks/configure_instances.yml -v'",
                         "Run configure_instances playbook",
                         timeout=1800)
        self._check_error(ok, "configure_instances.yml")

        # 5e: Run install_contrail.yml
        self.logger.info("Step 5e: Run install_contrail.yml")
        ok, _ = self.ssh(deployer_ip,
                         f"docker exec {container} bash -c '"
                         f"cd /root/contrail-ansible-deployer && "
                         f"ansible-playbook -i inventory/ -e orchestrator=openstack "
                         f"playbooks/install_contrail.yml -vvv'",
                         "Run install_contrail playbook",
                         timeout=3600)
        self._check_error(ok, "install_contrail.yml")

        # 5f: Verify contrail-status on upgraded computes
        self.logger.info("Step 5f: Verify contrail-status on upgraded computes")
        for c in computes_to_add:
            ok, out = self.ssh(c['mgmt_ip'],
                               "contrail-status 2>/dev/null | grep -E 'active|initializing|inactive'",
                               f"contrail-status on {c['hostname']}")
            if ok:
                self.logger.info(f"    {c['hostname']}: {out.strip()[:200]}")

        self.logger.info("STEP 5 COMPLETE")
        return True

    # ─── Step 6: VM Redistribution ───────────────────────────────────

    def step_6_redistribute_vms(self):
        """Migrate VMs from safe compute back to upgraded computes."""
        self.logger.info("=" * 70)
        self.logger.info("STEP 6: POST-UPGRADE VM REDISTRIBUTION")
        self.logger.info("=" * 70)

        mig = self.config['vm_migration']
        safe = mig['safe_compute']
        nova_path = mig['nova_instances_path']
        old_ctrl = self.config['old_controllers'][0]
        compute_map = {c['hostname']: c for c in self.config['compute_nodes']}

        safe_compute = compute_map[safe]

        # Get target computes
        target_computes = [compute_map[c] for c in mig['computes_to_upgrade'] if c in compute_map]

        if not target_computes:
            self.logger.warning("No target computes available")
            return True

        # Get VMs on safe compute
        ok, out = self.ssh(safe_compute['mgmt_ip'],
                           f"ls -1 {nova_path} 2>/dev/null | grep -E '^[0-9a-f]{{8}}-' || true",
                           f"List VMs on {safe}")
        if not ok or not out.strip() or out == "DRY_RUN":
            self.logger.info(f"  No VMs on {safe}")
            return True

        vm_uuids = [u.strip() for u in out.strip().split('\n') if u.strip()]
        self.logger.info(f"  {len(vm_uuids)} VMs to redistribute from {safe}")

        for vm_uuid in vm_uuids:
            target = random.choice(target_computes)
            target_fqdn = f"{target['hostname']}.englab.juniper.net"
            self.logger.info(f"    {vm_uuid} → {target['hostname']}")

            self.os_cmd(f"openstack server stop {vm_uuid}", f"Stop {vm_uuid}")
            time.sleep(3)

            self.ssh(safe_compute['mgmt_ip'],
                     f"scp -r -o StrictHostKeyChecking=no "
                     f"{nova_path}/{vm_uuid} {self.ssh_user}@{target['mgmt_ip']}:{nova_path}/",
                     f"Copy files to {target['hostname']}")

            sql = (
                f"UPDATE instances SET node='{target_fqdn}', host='{target['hostname']}' "
                f"WHERE uuid='{vm_uuid}'"
            )
            self.ssh(old_ctrl['mgmt_ip'],
                     f"docker exec -i {mig['mariadb_container']} "
                     f"mysql -u {mig['mariadb_user']} -p{mig['mariadb_password']} nova "
                     f'-e "{sql}"',
                     f"Update DB for {vm_uuid}")

            self.os_cmd(f"openstack server start {vm_uuid}", f"Start {vm_uuid}")
            time.sleep(3)

        # Verify
        time.sleep(30)
        ok, out = self.os_cmd("openstack server list --all -f json", "Verify VMs")
        if ok and out != "DRY_RUN":
            vms = json.loads(out) if out.strip() else []
            active = sum(1 for v in vms if v.get('Status') == 'ACTIVE')
            self.logger.info(f"  VMs: {active}/{len(vms)} ACTIVE")

        self.logger.info("STEP 6 COMPLETE")
        return True

    # ─── Full ISSU ───────────────────────────────────────────────────

    def run_step(self, step_num):
        """Run a single ISSU step by number."""
        steps = {
            0: ("VM Creation", self.step_0_create_vms),
            1: ("Cross-Provisioning", self.step_1_cross_provision),
            2: ("Prepare Sync Config", self.step_2_prepare_sync),
            3: ("Run Sync Processes", self.step_3_run_sync),
            4: ("VM Migration", self.step_4_migrate_vms),
            5: ("Compute Upgrade", self.step_5_upgrade_computes),
            6: ("VM Redistribution", self.step_6_redistribute_vms),
        }
        name, func = steps[step_num]
        self._current_step = step_num
        start = datetime.now()
        self.logger.info(f"\n{'#' * 70}")
        self.logger.info(f"EXECUTING STEP {step_num}: {name}")
        self.logger.info(f"{'#' * 70}\n")

        # Collect pre-step logs
        self._collect_logs(phase="pre")

        result = func()

        elapsed = (datetime.now() - start).total_seconds()
        self.step_times[step_num] = elapsed

        # Collect post-step logs
        self._collect_logs(phase="post")

        # Write per-step report
        self._write_step_report(step_num)

        status = "SUCCESS" if result else "FAILED"
        self.logger.info(f"\nStep {step_num} ({name}): {status} in {elapsed:.0f}s\n")
        self._current_step = None
        return result

    def run_full_issu(self):
        """Execute complete ISSU process (Steps 0-6)."""
        start = datetime.now()
        self.logger.info("=" * 70)
        self.logger.info("STARTING FULL ISSU UPGRADE")
        self.logger.info(f"Old: {[c['hostname'] for c in self.config['old_controllers']]}")
        self.logger.info(f"New: {[c['hostname'] for c in self.config['new_controllers']]}")
        self.logger.info(f"Computes: {[c['hostname'] for c in self.config['compute_nodes']]}")
        self.logger.info(f"Reports: {self.report_dir}")
        self.logger.info("=" * 70)

        for step in range(7):
            if not self.run_step(step):
                self.logger.error(f"ISSU FAILED at Step {step}")
                self._write_final_report()
                return False

        elapsed = (datetime.now() - start).total_seconds()
        self.logger.info("=" * 70)
        self.logger.info(f"ISSU COMPLETE in {elapsed:.0f}s")
        for s, t in self.step_times.items():
            self.logger.info(f"  Step {s}: {t:.0f}s")
        self.logger.info("=" * 70)

        self._write_final_report()
        return True


def main():
    parser = argparse.ArgumentParser(description="Contrail ISSU Automation")
    parser.add_argument('--config', default='issu_config.yaml', help='Config file path')
    parser.add_argument('--step', type=int, choices=range(7), help='Run single step (0-6)')
    parser.add_argument('--dry-run', action='store_true', help='Dry-run mode')
    parser.add_argument('--container-name', type=str, default=None,
                        help='Deployer container name on noden30 (overrides config)')
    args = parser.parse_args()

    issu = ISSUAutomation(args.config, dry_run=args.dry_run,
                          container_name=args.container_name)

    if args.step is not None:
        success = issu.run_step(args.step)
        issu._write_final_report()
    else:
        success = issu.run_full_issu()

    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
