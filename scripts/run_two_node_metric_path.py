#!/usr/bin/env python3
"""Run one guarded two-node Vast k3s metric-path experiment.

No defaults identify an offer or guard channel.  This entry point exists so
the paid operation is reviewable as one bounded command rather than an ad-hoc
terminal sequence.  Both guard channels arm before any create; ``TwoNodeLease``
performs exact teardown and three absence reads for every observed VM.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from srecon26_poc.contracts import OfferContract
from srecon26_poc.live_factory import DynamicGitHubGuard, GitHubGuardConfig, SshRemoteWorkload, SshWorkloadConfig, VastSshResolver
from srecon26_poc.two_node import NodeLease, TwoNodeLease
from srecon26_poc.vast_provider import OFFICIAL_KVM_IMAGE, OFFICIAL_UBUNTU_2204_TEMPLATE_HASH, VastCliProvider, VastLaunchContract


def contract_record(contract: OfferContract | object) -> dict[str, object]:
    """Return the non-secret, JSON-safe identity used to bind evidence."""
    fields = (
        "offer_id", "instance_id", "gpu_name", "num_gpus", "gpu_ram_mib",
        "compute_capability", "machine_id", "dph_total", "label",
    )
    return {
        field: (str(value) if field == "dph_total" else value)
        for field in fields
        if (value := getattr(contract, field, None)) is not None
    }


def write_json(path: Path, payload: dict[str, object]) -> None:
    """Atomically preserve evidence even if the controller is interrupted."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-offer", type=int, required=True)
    parser.add_argument("--server-machine", type=int, required=True)
    parser.add_argument("--worker-offer", type=int, required=True)
    parser.add_argument("--worker-machine", type=int, required=True)
    parser.add_argument("--server-guard-repository", required=True)
    parser.add_argument("--server-guard-issue", type=int, required=True)
    parser.add_argument("--worker-guard-repository", required=True)
    parser.add_argument("--worker-guard-issue", type=int, required=True)
    parser.add_argument("--guard-author", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.output.exists():
        raise SystemExit("output path already exists; refusing replay")
    if args.server_offer == args.worker_offer or args.server_machine == args.worker_machine:
        raise SystemExit("server and worker need distinct offers and machines")
    args.output.mkdir(parents=True, mode=0o700)
    deadline = datetime.now(UTC) + timedelta(minutes=42)
    provider = VastCliProvider("vastai", reconcile_attempts=24, reconcile_interval_seconds=5)
    if provider.list_instances():
        raise SystemExit("Vast inventory is not empty")

    server_nonce, worker_nonce = secrets.token_urlsafe(18), secrets.token_urlsafe(18)
    server_label = f"srecon26-two-node-server--nonce-{server_nonce}"
    worker_label = f"srecon26-two-node-worker--nonce-{worker_nonce}"
    server_offer = provider.get_vms_enabled_offer(args.server_offer, machine_id=args.server_machine, label=server_label)
    worker_offer = provider.get_vms_enabled_offer(args.worker_offer, machine_id=args.worker_machine, label=worker_label)
    if args.server_guard_repository == args.worker_guard_repository and args.server_guard_issue == args.worker_guard_issue:
        raise SystemExit("server and worker must use distinct independent guard channels")
    server_guard = DynamicGitHubGuard(GitHubGuardConfig(repository=args.server_guard_repository, ref="main", issue_number=args.server_guard_issue, trusted_author=args.guard_author))
    worker_guard = DynamicGitHubGuard(GitHubGuardConfig(repository=args.worker_guard_repository, ref="main", issue_number=args.worker_guard_issue, trusted_author=args.guard_author))
    work = SshRemoteWorkload(
        VastSshResolver("vastai"),
        SshWorkloadConfig(
            user="root", identity_file=Path.home() / ".ssh/id_rsa", public_key_file=Path.home() / ".ssh/id_rsa.pub",
            known_hosts_file=Path.home() / ".ssh/known_hosts", k3s_binary=ROOT / "artifacts/tools/k3s-v1.36.4+k3s1",
            nvidia_runtime_template=ROOT / "infra/k3s/nvidia-runtime.toml", local_script=ROOT / "scripts/remote_host_canary.sh",
            local_manifest_dir=ROOT / "infra/k3s",
        ),
    )
    launch = VastLaunchContract(OFFICIAL_UBUNTU_2204_TEMPLATE_HASH, OFFICIAL_KVM_IMAGE)
    controller = TwoNodeLease(
        provider=provider,
        server=NodeLease("server", "two-node-server-" + server_nonce, server_nonce, server_offer),
        worker=NodeLease("worker", "two-node-worker-" + worker_nonce, worker_nonce, worker_offer),
        server_guard=server_guard, worker_guard=worker_guard, launch=launch, hard_deadline=deadline,
        now=lambda: datetime.now(UTC),
    )
    manifest: dict[str, object] = {
        "schema": "srecon26.two-node-run.v1",
        "started_at": datetime.now(UTC).isoformat(),
        "hard_deadline": deadline.isoformat(),
        "status": "preflighted",
        "server": {"nonce": server_nonce, "offer": contract_record(server_offer)},
        "worker": {"nonce": worker_nonce, "offer": contract_record(worker_offer)},
    }
    write_json(args.output / "run-manifest.json", manifest)

    def heartbeat() -> None:
        tick = time.monotonic_ns()
        from srecon26_poc.types import RunIdentity
        server_guard.record_heartbeat(RunIdentity(controller.server.run_id, server_label, datetime.now(UTC)), tick)
        worker_guard.record_heartbeat(RunIdentity(controller.worker.run_id, worker_label, datetime.now(UTC)), tick)

    def remote(endpoint, command: list[str], log: str) -> None:
        work._remote(endpoint, command, hard_deadline=deadline, heartbeat=heartbeat, log=args.output / log)

    def stage(endpoint, root: str, role: str) -> None:
        remote(endpoint, ["install", "-d", "-m", "0700", root], f"{role}-mkdir.log")
        work._copy(endpoint, work.config.local_script, f"{root}/remote_host_canary.sh", recursive=False, hard_deadline=deadline, heartbeat=heartbeat, log=args.output / f"{role}-script-copy.log")
        work._copy(endpoint, work.config.k3s_binary, f"{root}/k3s", recursive=False, hard_deadline=deadline, heartbeat=heartbeat, log=args.output / f"{role}-k3s-copy.log")
        work._copy(endpoint, work.config.nvidia_runtime_template, f"{root}/nvidia-runtime.toml", recursive=False, hard_deadline=deadline, heartbeat=heartbeat, log=args.output / f"{role}-runtime-copy.log")
        remote(endpoint, ["env", f"CANARY_EVIDENCE_DIR={root}/evidence", "bash", f"{root}/remote_host_canary.sh", "probe"], f"{role}-probe.log")

    def workload(server, worker) -> None:
        manifest["status"] = "instances-created"
        manifest["server"] = {**manifest["server"], "instance": contract_record(server)}
        manifest["worker"] = {**manifest["worker"], "instance": contract_record(worker)}
        write_json(args.output / "run-manifest.json", manifest)
        resolver = work.resolver
        for instance, role in ((server, "server"), (worker, "worker")):
            resolver.attach_public_key(instance, work.config.public_key_file, hard_deadline=deadline, heartbeat=heartbeat, status_log=args.output / f"{role}-provider-status.ndjson")
        server_ep = resolver.resolve(server, hard_deadline=deadline, heartbeat=heartbeat, status_log=args.output / "server-provider-status.ndjson")
        worker_ep = resolver.resolve(worker, hard_deadline=deadline, heartbeat=heartbeat, status_log=args.output / "worker-provider-status.ndjson")
        work._wait_for_ssh(server_ep, hard_deadline=deadline, heartbeat=heartbeat, transport=args.output / "server-transport")
        work._wait_for_ssh(worker_ep, hard_deadline=deadline, heartbeat=heartbeat, transport=args.output / "worker-transport")
        server_root, worker_root = "/var/tmp/srecon26-server", "/var/tmp/srecon26-worker"
        stage(server_ep, server_root, "server")
        stage(worker_ep, worker_root, "worker")
        remote(server_ep, ["env", f"K3S_BINARY_PATH={server_root}/k3s", f"NVIDIA_RUNTIME_TEMPLATE={server_root}/nvidia-runtime.toml", "bash", f"{server_root}/remote_host_canary.sh", "install"], "server-install.log")
        server_name = subprocess.check_output([*work._ssh_prefix(server_ep), "hostname"], text=True).strip()
        worker_name = subprocess.check_output([*work._ssh_prefix(worker_ep), "hostname"], text=True).strip()
        work._copy(server_ep, work.config.local_manifest_dir, f"{server_root}/manifests", recursive=True, hard_deadline=deadline, heartbeat=heartbeat, log=args.output / "server-manifests-copy.log")
        with tempfile.TemporaryDirectory(prefix="srecon26-two-node-") as temporary:
            staging = Path(temporary)
            bridge = staging / "server-bridge"
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(bridge)], check=True)
            known_hosts = staging / "server-known-hosts"
            known_hosts.write_bytes(subprocess.check_output(["ssh-keyscan", "-p", str(server_ep.port), server_ep.host], stderr=subprocess.DEVNULL))
            if not known_hosts.read_bytes():
                raise RuntimeError("server SSH host key scan returned no key")
            work._copy(server_ep, bridge.with_suffix(".pub"), f"{server_root}/bridge.pub", recursive=False, hard_deadline=deadline, heartbeat=heartbeat, log=args.output / "bridge-public-copy.log")
            remote(server_ep, ["sh", "-ceu", f"install -d -m 0700 /root/.ssh; cat {server_root}/bridge.pub >> /root/.ssh/authorized_keys; chmod 0600 /root/.ssh/authorized_keys; rm -f {server_root}/bridge.pub"], "bridge-authorize.log")
            work._copy(worker_ep, bridge, f"{worker_root}/server-bridge.key", recursive=False, hard_deadline=deadline, heartbeat=heartbeat, log=args.output / "bridge-key-copy.log")
            work._copy(worker_ep, known_hosts, f"{worker_root}/server-known-hosts", recursive=False, hard_deadline=deadline, heartbeat=heartbeat, log=args.output / "bridge-known-hosts-copy.log")
            remote(worker_ep, ["chmod", "0600", f"{worker_root}/server-bridge.key", f"{worker_root}/server-known-hosts"], "bridge-file-modes.log")
            remote(worker_ep, ["env", f"CANARY_K3S_TUNNEL_HOST={server_ep.host}", f"CANARY_K3S_TUNNEL_PORT={server_ep.port}", "CANARY_K3S_TUNNEL_USER=root", f"CANARY_K3S_TUNNEL_IDENTITY_FILE={worker_root}/server-bridge.key", f"CANARY_K3S_TUNNEL_KNOWN_HOSTS={worker_root}/server-known-hosts", "bash", f"{worker_root}/remote_host_canary.sh", "install-tunnel"], "worker-tunnel-install.log")
            remote(worker_ep, ["sh", "-ceu", f"install -d -m 0700 {worker_root}/run; scp -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile={worker_root}/server-known-hosts -o GlobalKnownHostsFile=/dev/null -i {worker_root}/server-bridge.key -P {server_ep.port} root@{server_ep.host}:/var/lib/rancher/k3s/server/node-token {worker_root}/run/node-token; chown root:root {worker_root}/run/node-token; chmod 0600 {worker_root}/run/node-token"], "worker-token-copy.log")
            remote(worker_ep, ["env", f"K3S_BINARY_PATH={worker_root}/k3s", f"NVIDIA_RUNTIME_TEMPLATE={worker_root}/nvidia-runtime.toml", "CANARY_K3S_SERVER_URL=https://127.0.0.1:6443", f"CANARY_K3S_RUN_ROOT={worker_root}/run", f"CANARY_K3S_TOKEN_FILE={worker_root}/run/node-token", "bash", f"{worker_root}/remote_host_canary.sh", "install-agent"], "worker-install-agent.log")
        manifest_hash = work._manifest_hash(work.config.local_manifest_dir)
        remote(server_ep, ["env", f"CANARY_EVIDENCE_DIR={server_root}/evidence", f"CANARY_EXPECTED_NODE_NAMES={server_name},{worker_name}", "bash", f"{server_root}/remote_host_canary.sh", "verify-two-node"], "two-node-verify.log")
        remote(server_ep, ["env", f"CANARY_EVIDENCE_DIR={server_root}/evidence", f"CANARY_MANIFEST_DIR={server_root}/manifests", f"CANARY_MANIFEST_SHA256={manifest_hash}", "bash", f"{server_root}/remote_host_canary.sh", "deploy"], "deploy.log")
        remote(server_ep, ["env", f"CANARY_EVIDENCE_DIR={server_root}/evidence", "bash", f"{server_root}/remote_host_canary.sh", "collect"], "collect.log")
        work._fetch(server_ep, f"{server_root}/evidence", args.output / "server-evidence", hard_deadline=deadline, heartbeat=heartbeat, log=args.output / "evidence-fetch.log")
        remote(server_ep, ["env", f"CANARY_EVIDENCE_DIR={server_root}/evidence", f"CANARY_MANIFEST_DIR={server_root}/manifests", f"CANARY_MANIFEST_SHA256={manifest_hash}", "bash", f"{server_root}/remote_host_canary.sh", "cleanup"], "cleanup.log")

    try:
        result = controller.run(workload, heartbeat=heartbeat)
        manifest["status"] = "completed"
        manifest["workload_completed"] = result.workload_completed
        manifest["absence_reads"] = result.absence_reads
        write_json(args.output / "run-manifest.json", manifest)
    except BaseException as error:
        manifest["status"] = "failed"
        manifest["failure"] = {"error_type": type(error).__name__, "error": str(error)}
        write_json(args.output / "run-manifest.json", manifest)
        write_json(args.output / "terminal-failure.json", {
            "error_type": type(error).__name__, "error": str(error), "traceback": traceback.format_exc(),
        })
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
