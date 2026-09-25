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
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from scripts.build_evidence_pack import post_deadline_absence
from srecon26_poc.azure_guard_transport import AzureGuardSshConfig
from srecon26_poc.azure_run_command_transport import AzureRunCommandGuardConfig, AzureRunCommandGuardTransport
from srecon26_poc.contracts import InstanceContract, OfferContract, ProbeOutcome, classify_fault
from srecon26_poc.live_dispatch import REPORT_MARGIN, ProviderFaultEvidence
from srecon26_poc.live_dispatch import (
    FROZEN_MODEL_ID,
    FROZEN_MODEL_REVISION,
    VLLM_IMAGE_DIGEST,
)
from srecon26_poc.live_factory import (
    DynamicAzureGuard,
    LiveFactoryError,
    ProviderStartupFault,
    SshRemoteWorkload,
    SshWorkloadConfig,
    VastSshResolver,
    _load_report_gate,
)
from srecon26_poc.reporting import FaultRecord
from srecon26_poc.two_node import NodeLease, TwoNodeLease
from srecon26_poc.types import FaultClass, RunIdentity
from srecon26_poc.vast_provider import (
    OFFICIAL_KVM_IMAGE,
    OFFICIAL_UBUNTU_2204_TEMPLATE_HASH,
    OFFICIAL_UBUNTU_DESKTOP_IMAGE,
    OFFICIAL_UBUNTU_DESKTOP_TEMPLATE_HASH,
    VastCliProvider,
    VastLaunchContract,
)


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


class PairedGuardHeartbeat:
    """Coalesce frequent pulses while updating independent guards in parallel."""

    def __init__(
        self,
        *,
        server_guard: DynamicAzureGuard,
        worker_guard: DynamicAzureGuard,
        server_identity: Callable[[], RunIdentity],
        worker_identity: Callable[[], RunIdentity],
        coalesce_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if coalesce_seconds <= 0:
            raise ValueError("guard heartbeat coalescing window must be positive")
        self.server_guard = server_guard
        self.worker_guard = worker_guard
        self.server_identity = server_identity
        self.worker_identity = worker_identity
        self.coalesce_seconds = coalesce_seconds
        self.monotonic = monotonic
        self.monotonic_ns = monotonic_ns
        self._last_successful_dispatch: float | None = None
        self._lock = Lock()

    def __call__(self) -> None:
        with self._lock:
            dispatch_started = self.monotonic()
            if (
                self._last_successful_dispatch is not None
                and dispatch_started - self._last_successful_dispatch < self.coalesce_seconds
            ):
                return
            tick = self.monotonic_ns()
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="srecon26-guard-heartbeat") as pool:
                futures = (
                    pool.submit(self.server_guard.record_heartbeat, self.server_identity(), tick),
                    pool.submit(self.worker_guard.record_heartbeat, self.worker_identity(), tick),
                )
                failures: list[BaseException] = []
                for future in futures:
                    try:
                        future.result()
                    except BaseException as error:
                        failures.append(error)
            if failures:
                raise LiveFactoryError("paired Azure guard heartbeat failed") from failures[0]
            # Cache dispatch start, not completion. Guard mutation happens
            # during each RPC, so completion time would overstate freshness.
            self._last_successful_dispatch = dispatch_started


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-offer", type=int, required=True)
    parser.add_argument("--server-machine", type=int, required=True)
    parser.add_argument("--worker-offer", type=int, required=True)
    parser.add_argument("--worker-machine", type=int, required=True)
    parser.add_argument(
        "--guard-backend",
        choices=("azure",),
        required=True,
        help="required paid-run safety backend; GitHub guards are not accepted by this entry point",
    )
    parser.add_argument("--server-azure-guard-host")
    parser.add_argument("--server-azure-guard-user")
    parser.add_argument("--server-azure-guard-identity", type=Path)
    parser.add_argument("--server-azure-guard-known-hosts", type=Path)
    parser.add_argument("--server-azure-guard-port", type=int, default=22)
    parser.add_argument("--worker-azure-guard-host")
    parser.add_argument("--worker-azure-guard-user")
    parser.add_argument("--worker-azure-guard-identity", type=Path)
    parser.add_argument("--worker-azure-guard-known-hosts", type=Path)
    parser.add_argument("--worker-azure-guard-port", type=int, default=22)
    parser.add_argument("--azure-guard-transport", choices=("ssh", "run-command"), default="ssh")
    parser.add_argument("--azure-subscription-id")
    parser.add_argument("--azure-guard-resource-group")
    parser.add_argument("--server-azure-guard-vm")
    parser.add_argument("--worker-azure-guard-vm")
    parser.add_argument("--azure-cli", type=Path, default=Path("/opt/homebrew/bin/az"))
    parser.add_argument("--azure-guard-timeout-seconds", type=int, default=20)
    parser.add_argument("--azure-guard-absence-timeout-seconds", type=int, default=240)
    parser.add_argument(
        "--guard-heartbeat-timeout-seconds",
        type=int,
        required=True,
        help="expiry configured on both guard workers; must be >= --heartbeat-seconds",
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=int,
        default=120,
        help="nominal workload heartbeat interval; blocking SSH emits at half this interval",
    )
    parser.add_argument("--report-adapter-factory", default=os.environ.get("SRECON26_REPORT_ADAPTER_FACTORY"))
    parser.add_argument("--vast-cli", default=os.environ.get("SRECON26_VAST_CLI", "vastai"))
    parser.add_argument("--vm-template", choices=("ubuntu-cli", "ubuntu-desktop"), default="ubuntu-cli")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def validate_configuration(args: argparse.Namespace) -> None:
    required = (
        {
            "--server-azure-guard-host": args.server_azure_guard_host,
            "--server-azure-guard-user": args.server_azure_guard_user,
            "--server-azure-guard-identity": args.server_azure_guard_identity,
            "--server-azure-guard-known-hosts": args.server_azure_guard_known_hosts,
            "--worker-azure-guard-host": args.worker_azure_guard_host,
            "--worker-azure-guard-user": args.worker_azure_guard_user,
            "--worker-azure-guard-identity": args.worker_azure_guard_identity,
            "--worker-azure-guard-known-hosts": args.worker_azure_guard_known_hosts,
        }
        if args.azure_guard_transport == "ssh"
        else {
            "--azure-subscription-id": args.azure_subscription_id,
            "--azure-guard-resource-group": args.azure_guard_resource_group,
            "--server-azure-guard-vm": args.server_azure_guard_vm,
            "--worker-azure-guard-vm": args.worker_azure_guard_vm,
            "--azure-cli": args.azure_cli,
        }
    )
    missing = [name for name, value in required.items() if value in (None, "")]
    if missing:
        raise SystemExit(f"Azure guard backend requires {', '.join(missing)}")
    if args.azure_guard_transport == "ssh":
        if args.server_azure_guard_host.casefold() == args.worker_azure_guard_host.casefold():
            raise SystemExit("server and worker Azure guards require distinct controller hosts")
        if args.server_azure_guard_identity.expanduser().resolve() == args.worker_azure_guard_identity.expanduser().resolve():
            raise SystemExit("server and worker Azure guards require distinct SSH identities")
        if args.server_azure_guard_known_hosts.expanduser().resolve() == args.worker_azure_guard_known_hosts.expanduser().resolve():
            raise SystemExit("server and worker Azure guards require distinct pinned known-hosts files")
        for role, port in (("server", args.server_azure_guard_port), ("worker", args.worker_azure_guard_port)):
            if not 1 <= port <= 65535:
                raise SystemExit(f"{role} Azure guard port must be from 1 to 65535")
    elif args.server_azure_guard_vm.casefold() == args.worker_azure_guard_vm.casefold():
        raise SystemExit("server and worker Azure Run Command guards require distinct managed VMs")
    maximum_guard_timeout = 300 if args.azure_guard_transport == "run-command" else 60
    if not 1 <= args.azure_guard_timeout_seconds <= maximum_guard_timeout:
        raise SystemExit(f"Azure guard timeout must be from 1 to {maximum_guard_timeout} seconds")
    if not 30 <= args.azure_guard_absence_timeout_seconds <= 600:
        raise SystemExit("Azure guard absence timeout must be from 30 to 600 seconds")
    if not 30 <= args.heartbeat_seconds <= 600:
        raise SystemExit("heartbeat seconds must be from 30 to 600")
    if not 30 <= args.guard_heartbeat_timeout_seconds <= 600:
        raise SystemExit("guard heartbeat timeout must be from 30 to 600 seconds")
    if args.heartbeat_seconds > args.guard_heartbeat_timeout_seconds:
        raise SystemExit("heartbeat seconds must not exceed the configured guard heartbeat timeout")
    if not args.report_adapter_factory:
        raise SystemExit("--report-adapter-factory is required before a paid two-node run")


def build_guards(
    args: argparse.Namespace,
    *,
    on_server_armed: Callable[[Mapping[str, object]], None],
    on_worker_armed: Callable[[Mapping[str, object]], None],
) -> tuple[DynamicAzureGuard, DynamicAzureGuard]:
    """Build two nonce-isolated clients without accepting secret material."""
    if args.azure_guard_transport == "run-command":
        server_config = AzureRunCommandGuardConfig(
            subscription_id=args.azure_subscription_id,
            resource_group=args.azure_guard_resource_group,
            vm_name=args.server_azure_guard_vm,
            az_path=args.azure_cli,
            timeout_seconds=args.azure_guard_timeout_seconds,
            heartbeat_timeout_seconds=args.guard_heartbeat_timeout_seconds,
        )
        worker_config = AzureRunCommandGuardConfig(
            subscription_id=args.azure_subscription_id,
            resource_group=args.azure_guard_resource_group,
            vm_name=args.worker_azure_guard_vm,
            az_path=args.azure_cli,
            timeout_seconds=args.azure_guard_timeout_seconds,
            heartbeat_timeout_seconds=args.guard_heartbeat_timeout_seconds,
        )
        return (
            DynamicAzureGuard(server_config, on_armed=on_server_armed, transport_factory=AzureRunCommandGuardTransport),
            DynamicAzureGuard(worker_config, on_armed=on_worker_armed, transport_factory=AzureRunCommandGuardTransport),
        )
    server_config = AzureGuardSshConfig(
        host=args.server_azure_guard_host,
        user=args.server_azure_guard_user,
        identity_file=args.server_azure_guard_identity,
        known_hosts_file=args.server_azure_guard_known_hosts,
        port=args.server_azure_guard_port,
        timeout_seconds=args.azure_guard_timeout_seconds,
        heartbeat_timeout_seconds=args.guard_heartbeat_timeout_seconds,
    )
    worker_config = AzureGuardSshConfig(
        host=args.worker_azure_guard_host,
        user=args.worker_azure_guard_user,
        identity_file=args.worker_azure_guard_identity,
        known_hosts_file=args.worker_azure_guard_known_hosts,
        port=args.worker_azure_guard_port,
        timeout_seconds=args.azure_guard_timeout_seconds,
        heartbeat_timeout_seconds=args.guard_heartbeat_timeout_seconds,
    )
    # Each transport permanently binds later RPCs to one nonce receipt and a
    # different pinned controller host, key, and known-hosts trust root.
    return (
        DynamicAzureGuard(server_config, on_armed=on_server_armed),
        DynamicAzureGuard(worker_config, on_armed=on_worker_armed),
    )


def validate_armed_guard_independence(
    *,
    role: str,
    receipt: Mapping[str, object],
    existing: Mapping[str, object],
    expected_heartbeat_timeout_seconds: int,
) -> None:
    """Refuse paid work unless remote Azure and SSH identities are distinct."""

    required_strings = (
        "host_identity", "azure_resource_id", "azure_vm_id", "host_key_fingerprint",
    )
    if any(not isinstance(receipt.get(field), str) or not receipt.get(field) for field in required_strings):
        raise LiveFactoryError(f"{role} Azure guard receipt lacks attested infrastructure identity")
    if receipt.get("heartbeat_timeout_seconds") != expected_heartbeat_timeout_seconds:
        raise LiveFactoryError(f"{role} Azure guard receipt attests a different heartbeat timeout")
    for other_role, other in existing.items():
        if other_role == role or not isinstance(other, Mapping) or other.get("status") == "PENDING":
            continue
        for field in required_strings:
            if str(receipt[field]).casefold() == str(other.get(field, "")).casefold():
                raise LiveFactoryError(f"{role} and {other_role} Azure guards share attested {field}")


def finalize_azure_guard_channels(
    *,
    guards: Mapping[str, DynamicAzureGuard],
    guard_receipts: Mapping[str, object],
    labels: Mapping[str, str],
    observed_labels: set[str],
    output: Path,
    absence_timeout_seconds: int,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[dict[str, object], list[str]]:
    """Status and export every armed channel, polling absence when needed."""

    evidence: dict[str, object] = {}
    errors: list[str] = []
    deferred_roles: dict[str, object] = {}
    unsafe = {"OWNERSHIP_MISMATCH", "TEARDOWN_RETRIES_EXHAUSTED", "DISARMED"}
    for role in ("server", "worker"):
        guard = guards[role]
        label = labels[role]
        observed = label in observed_labels
        if guard.arm_receipt is None:
            evidence[role] = {
                "status": "NOT_ARMED",
                "instance_observed_locally": observed,
                "absence_required": observed,
            }
            continue

        last_status: dict[str, object] | None = None
        arm_deadline = getattr(guard.arm_receipt, "hard_deadline", None)
        deadline_reached = isinstance(arm_deadline, datetime) and now() >= arm_deadline
        absence_required_now = observed or deadline_reached
        stop_at = monotonic() + absence_timeout_seconds
        while True:
            try:
                last_status = guard.status()
            except Exception as error:
                errors.append(f"{role} Azure guard status failed: {error}")
                break
            status = last_status.get("status")
            if not absence_required_now or status == "ABSENCE_CONFIRMED" or status in unsafe or monotonic() >= stop_at:
                if status in unsafe:
                    errors.append(f"{role} Azure guard reached unsafe terminal status {status}")
                break
            sleep(5)

        role_evidence: dict[str, object] = {
            **(last_status or {"status": "STATUS_UNAVAILABLE"}),
            "instance_observed_locally": observed,
            "absence_required": True,
        }
        export_root = last_status.get("root_hash") if last_status is not None else getattr(guard.arm_receipt, "root_hash", None)
        if isinstance(export_root, str):
            try:
                exported = guard.export_evidence(export_root)
                journal_path = output / f"{role}-azure-guard-journal.ndjson"
                journal_path.write_text(exported.journal, encoding="utf-8")
                role_evidence.update(
                    {
                        "exported_root_hash": export_root,
                        "journal_artifact": journal_path.name,
                        "journal_sha256": exported.journal_sha256,
                    }
                )
            except Exception as error:
                errors.append(f"{role} Azure guard evidence export failed: {error}")
                role_evidence["export_error"] = str(error)
        else:
            errors.append(f"{role} Azure guard evidence export lacked a bound root")
        terminal_absence = last_status is not None and last_status.get("status") == "ABSENCE_CONFIRMED"
        if terminal_absence and not observed:
            terminal_absence = (
                isinstance(arm_deadline, datetime)
                and post_deadline_absence(last_status, arm_deadline, now())
            )
        if not terminal_absence:
            if not observed:
                endpoint = (
                    {
                        "transport": "azure-run-command",
                        "subscription_id": guard.config.subscription_id,
                        "resource_group": guard.config.resource_group,
                        "vm_name": guard.config.vm_name,
                        "az_path": str(guard.config.az_path),
                        "timeout_seconds": guard.config.timeout_seconds,
                    }
                    if isinstance(guard.config, AzureRunCommandGuardConfig)
                    else {
                        "transport": "ssh",
                        "host": guard.config.host,
                        "user": guard.config.user,
                        "port": guard.config.port,
                        "timeout_seconds": guard.config.timeout_seconds,
                        "identity_file": str(guard.config.identity_file),
                        "known_hosts_file": str(guard.config.known_hosts_file),
                    }
                )
                deferred_roles[role] = {
                    "run_id": f"two-node-{role}-{getattr(guard.arm_receipt, 'nonce', '')}",
                    "nonce": getattr(guard.arm_receipt, "nonce", None),
                    "label": label,
                    "hard_deadline": arm_deadline.isoformat() if isinstance(arm_deadline, datetime) else None,
                    "finalize_after": arm_deadline.isoformat() if isinstance(arm_deadline, datetime) else None,
                    "endpoint": endpoint,
                    "expected_heartbeat_timeout_seconds": guard.config.heartbeat_timeout_seconds,
                    "arm_receipt": {
                        **guard_receipts.get(role, {}),
                        "run_id": f"two-node-{role}-{getattr(guard.arm_receipt, 'nonce', '')}",
                    },
                    "latest_status": last_status,
                }
                role_evidence["deferred_finalizer_artifact"] = "deferred-azure-guard-finalizer.json"
                errors.append(f"{role} Azure guard requires deferred post-deadline ABSENCE_CONFIRMED finalization")
            else:
                errors.append(f"{role} Azure guard did not publish three-read ABSENCE_CONFIRMED")
        if observed and not terminal_absence:
            role_evidence["absence_proof_valid"] = False
        else:
            role_evidence["absence_proof_valid"] = terminal_absence
        evidence[role] = role_evidence
    if deferred_roles:
        write_json(
            output / "deferred-azure-guard-finalizer.json",
            {
                "schema": "srecon26.azure-guard-deferred-finalizer.v1",
                "status": "PENDING_POST_DEADLINE_ABSENCE",
                "strategy": "INDEPENDENT_AZURE_GUARD_TIMER",
                "created_at": now().isoformat(),
                "credential_material_included": False,
                "required_terminal_status": "ABSENCE_CONFIRMED",
                "required_post_deadline_absence_reads": 3,
                "manifest_artifact": "run-manifest.json",
                "roles": deferred_roles,
            },
        )
    return evidence, errors


def workload_config(args: argparse.Namespace) -> SshWorkloadConfig:
    """Bind the paid runner's heartbeat cadence to every blocking SSH call."""

    return SshWorkloadConfig(
        user="root",
        identity_file=Path.home() / ".ssh/id_rsa",
        public_key_file=Path.home() / ".ssh/id_rsa.pub",
        known_hosts_file=Path.home() / ".ssh/known_hosts",
        k3s_binary=ROOT / "artifacts/tools/k3s-v1.36.4+k3s1",
        nvidia_runtime_template=ROOT / "infra/k3s/nvidia-runtime.toml",
        local_script=ROOT / "scripts/remote_host_canary.sh",
        local_manifest_dir=ROOT / "infra/k3s",
        heartbeat_seconds=args.heartbeat_seconds,
    )


class RecordingProvider:
    """Record every exact create reconciliation without changing provider IO."""

    def __init__(self, provider: VastCliProvider, observer: Callable[[InstanceContract], None]) -> None:
        self.provider = provider
        self.observer = observer

    def create_once(self, contract: OfferContract, request_key: str, launch: VastLaunchContract) -> InstanceContract:
        instance = self.provider.create_once(contract, request_key, launch)
        self.observer(instance)
        return instance

    def reconcile_label(self, label: str) -> InstanceContract:
        instance = self.provider.reconcile_label(label)
        self.observer(instance)
        return instance

    def destroy_exact(self, instance_id: int, expected_label: str) -> None:
        self.provider.destroy_exact(instance_id, expected_label)

    def list_instances(self) -> tuple[InstanceContract, ...]:
        return self.provider.list_instances()


def main() -> int:
    args = parse_args()
    validate_configuration(args)
    if args.output.exists():
        raise SystemExit("output path already exists; refusing replay")
    if args.server_offer == args.worker_offer or args.server_machine == args.worker_machine:
        raise SystemExit("server and worker need distinct offers and machines")
    args.output.mkdir(parents=True, mode=0o700)
    deadline = datetime.now(UTC) + timedelta(minutes=42)
    provider = VastCliProvider(args.vast_cli, reconcile_attempts=24, reconcile_interval_seconds=5)
    if provider.list_instances():
        raise SystemExit("Vast inventory is not empty")
    report_gate = _load_report_gate(args.report_adapter_factory)

    server_nonce, worker_nonce = secrets.token_urlsafe(18), secrets.token_urlsafe(18)
    server_label = f"srecon26-two-node-server--nonce-{server_nonce}"
    worker_label = f"srecon26-two-node-worker--nonce-{worker_nonce}"
    server_offer = provider.get_vms_enabled_offer(
        args.server_offer, machine_id=args.server_machine, label=server_label, allow_offer_rollover=True,
    )
    worker_offer = provider.get_vms_enabled_offer(
        args.worker_offer, machine_id=args.worker_machine, label=worker_label, allow_offer_rollover=True,
    )
    work = SshRemoteWorkload(VastSshResolver(args.vast_cli), workload_config(args))
    template = (
        (OFFICIAL_UBUNTU_DESKTOP_TEMPLATE_HASH, OFFICIAL_UBUNTU_DESKTOP_IMAGE)
        if args.vm_template == "ubuntu-desktop"
        else (OFFICIAL_UBUNTU_2204_TEMPLATE_HASH, OFFICIAL_KVM_IMAGE)
    )
    launch = VastLaunchContract(*template)
    manifest: dict[str, object] = {
        "schema": "srecon26.two-node-run.v1",
        "started_at": datetime.now(UTC).isoformat(),
        "hard_deadline": deadline.isoformat(),
        "real_run_contingent": True,
        "offer_refreeze_policy": "exact-id-or-single-current-machine-offer-on-provider-id-rollover",
        "guard_backend": args.guard_backend,
        "azure_guard_transport": args.azure_guard_transport,
        "heartbeat_seconds": args.heartbeat_seconds,
        "guard_heartbeat_timeout_seconds": args.guard_heartbeat_timeout_seconds,
        "vm_template": args.vm_template,
        "launch": launch.to_json(),
        "workload": {
            "model_id": FROZEN_MODEL_ID,
            "model_revision": FROZEN_MODEL_REVISION,
            "vllm_image": f"docker.io/vllm/vllm-openai@{VLLM_IMAGE_DIGEST}",
        },
        "status": "preflighted",
        "guards": {"server": {"status": "PENDING"}, "worker": {"status": "PENDING"}},
        "report": {"session_preflighted_after_both_guards": False, "attempted": False, "confirmed": False},
        "server": {"nonce": server_nonce, "offer": contract_record(server_offer)},
        "worker": {"nonce": worker_nonce, "offer": contract_record(worker_offer)},
    }
    write_json(args.output / "run-manifest.json", manifest)

    armed_roles: set[str] = set()

    def record_guard(role: str, receipt: Mapping[str, object]) -> None:
        guards = manifest["guards"]
        assert isinstance(guards, dict)
        candidate = {**receipt, "role": role}
        validate_armed_guard_independence(
            role=role,
            receipt=candidate,
            existing=guards,
            expected_heartbeat_timeout_seconds=args.guard_heartbeat_timeout_seconds,
        )
        guards[role] = candidate
        armed_roles.add(role)
        manifest["status"] = "guards-arming" if len(armed_roles) == 1 else "guards-armed"
        write_json(args.output / "run-manifest.json", manifest)
        if armed_roles == {"server", "worker"}:
            # This is deliberately inside the second arm callback.  If the
            # authenticated exact Report path is stale, TwoNodeLease receives
            # the exception before it can issue the first paid create.
            report_gate.preflight_authenticated_session()
            report = manifest["report"]
            assert isinstance(report, dict)
            report["session_preflighted_after_both_guards"] = True
            report["preflighted_at"] = datetime.now(UTC).isoformat()
            manifest["status"] = "armed-and-report-ready"
            write_json(args.output / "run-manifest.json", manifest)

    server_guard, worker_guard = build_guards(
        args,
        on_server_armed=lambda receipt: record_guard("server", receipt),
        on_worker_armed=lambda receipt: record_guard("worker", receipt),
    )

    observed_instances: dict[str, InstanceContract] = {}

    def record_instance(instance: InstanceContract) -> None:
        observed_instances[instance.label] = instance
        role = "server" if instance.label == server_label else "worker" if instance.label == worker_label else None
        if role is None:
            raise LiveFactoryError("provider returned an instance outside the exact two-node labels")
        current = manifest[role]
        assert isinstance(current, dict)
        manifest[role] = {**current, "instance": contract_record(instance)}
        manifest["real_run_contingent"] = False
        manifest["paid_create_observed"] = True
        manifest["status"] = "instance-observed"
        write_json(args.output / "run-manifest.json", manifest)

    controller = TwoNodeLease(
        provider=RecordingProvider(provider, record_instance),
        server=NodeLease("server", "two-node-server-" + server_nonce, server_nonce, server_offer),
        worker=NodeLease("worker", "two-node-worker-" + worker_nonce, worker_nonce, worker_offer),
        server_guard=server_guard, worker_guard=worker_guard, launch=launch, hard_deadline=deadline,
        now=lambda: datetime.now(UTC),
    )

    heartbeat = PairedGuardHeartbeat(
        server_guard=server_guard,
        worker_guard=worker_guard,
        server_identity=lambda: RunIdentity(controller.server.run_id, server_label, datetime.now(UTC)),
        worker_identity=lambda: RunIdentity(controller.worker.run_id, worker_label, datetime.now(UTC)),
        # SSH wrappers offer pulse opportunities every half interval. A
        # conservative half-timeout ceiling keeps repeated local calls cheap
        # without treating an old heartbeat as fresh.
        coalesce_seconds=min(args.heartbeat_seconds, args.guard_heartbeat_timeout_seconds / 2),
    )

    def remote(endpoint, command: list[str], log: str) -> str:
        return work._remote(endpoint, command, hard_deadline=deadline, heartbeat=heartbeat, log=args.output / log)

    def stage(endpoint, root: str, role: str) -> None:
        remote(endpoint, ["install", "-d", "-m", "0700", root], f"{role}-mkdir.log")
        work._copy(endpoint, work.config.local_script, f"{root}/remote_host_canary.sh", recursive=False, hard_deadline=deadline, heartbeat=heartbeat, log=args.output / f"{role}-script-copy.log")
        work._copy(endpoint, work.config.k3s_binary, f"{root}/k3s", recursive=False, hard_deadline=deadline, heartbeat=heartbeat, log=args.output / f"{role}-k3s-copy.log")
        work._copy(endpoint, work.config.nvidia_runtime_template, f"{root}/nvidia-runtime.toml", recursive=False, hard_deadline=deadline, heartbeat=heartbeat, log=args.output / f"{role}-runtime-copy.log")
        remote(endpoint, ["env", f"CANARY_EVIDENCE_DIR={root}/evidence", "bash", f"{root}/remote_host_canary.sh", "probe"], f"{role}-probe.log")

    def report_fault(instance: InstanceContract, description: str, *, evidence: Path | None = None) -> None:
        report = manifest["report"]
        assert isinstance(report, dict)
        if report.get("attempted") is True:
            raise LiveFactoryError("a provider fault report was already attempted for this two-node run")
        if datetime.now(UTC) >= deadline - REPORT_MARGIN:
            raise LiveFactoryError("confirmed provider fault reached the immutable Report cutoff")
        report.update(
            {
                "attempted": True,
                "confirmed": False,
                "instance_id": instance.instance_id,
                "label": instance.label,
                "description": description,
                "evidence": str(evidence.relative_to(args.output)) if evidence is not None else None,
                "started_at": datetime.now(UTC).isoformat(),
            }
        )
        manifest["status"] = "reporting-confirmed-provider-fault"
        write_json(args.output / "run-manifest.json", manifest)
        try:
            receipt = report_gate.handle(
                FaultRecord(instance.instance_id, instance.label, instance.label.rsplit("--nonce-", 1)[1], description),
                deadline - REPORT_MARGIN,
            )
        except (TimeoutError, ValueError) as error:
            report["error"] = str(error)
            write_json(args.output / "run-manifest.json", manifest)
            raise LiveFactoryError("confirmed provider fault Report action did not finish before teardown") from error
        report.update(
            {
                "confirmed": receipt.confirmed,
                "before": str(receipt.before_path),
                "after": str(receipt.after_path) if receipt.after_path else None,
                "finished_at": datetime.now(UTC).isoformat(),
            }
        )
        write_json(args.output / "run-manifest.json", manifest)

    def resolve_instance(instance: InstanceContract, role: str):
        status_log = args.output / f"{role}-provider-status.ndjson"
        try:
            work.resolver.attach_public_key(
                instance,
                work.config.public_key_file,
                hard_deadline=deadline,
                heartbeat=heartbeat,
                status_log=status_log,
            )
            return work.resolver.resolve(
                instance,
                hard_deadline=deadline,
                heartbeat=heartbeat,
                status_log=status_log,
            )
        except ProviderStartupFault as error:
            transport = args.output / f"{role}-transport"
            artifact = work._write_startup_fault_evidence(
                transport,
                run_id=getattr(controller, role).run_id,
                instance=instance,
                fault=error,
            )
            declared = tuple(path for path in args.output.rglob("*") if path.is_file())
            provider_fault = ProviderFaultEvidence("host", str(error), artifact)
            blockers = provider_fault.report_blockers(
                run_id=getattr(controller, role).run_id,
                instance=instance,
                evidence_files=declared,
            )
            if blockers:
                raise LiveFactoryError("; ".join(blockers)) from error
            report_fault(instance, provider_fault.description, evidence=artifact)
            raise LiveFactoryError("confirmed provider startup fault was handled before teardown") from error

    def workload(server, worker) -> None:
        manifest["status"] = "instances-created"
        manifest["server"] = {**manifest["server"], "instance": contract_record(server)}
        manifest["worker"] = {**manifest["worker"], "instance": contract_record(worker)}
        write_json(args.output / "run-manifest.json", manifest)
        for offer, instance in ((server_offer, server), (worker_offer, worker)):
            if classify_fault(offer, instance, ProbeOutcome.PASS) is FaultClass.PROVIDER_FAULT_CONFIRMED:
                report_fault(instance, "confirmed provider contract mismatch immediately after create")
                raise LiveFactoryError("confirmed provider contract fault was handled before teardown")
        server_ep = resolve_instance(server, "server")
        worker_ep = resolve_instance(worker, "worker")
        work._wait_for_ssh(server_ep, hard_deadline=deadline, heartbeat=heartbeat, transport=args.output / "server-transport")
        work._wait_for_ssh(worker_ep, hard_deadline=deadline, heartbeat=heartbeat, transport=args.output / "worker-transport")
        server_root, worker_root = "/var/tmp/srecon26-server", "/var/tmp/srecon26-worker"
        stage(server_ep, server_root, "server")
        stage(worker_ep, worker_root, "worker")
        remote(server_ep, ["env", f"K3S_BINARY_PATH={server_root}/k3s", f"NVIDIA_RUNTIME_TEMPLATE={server_root}/nvidia-runtime.toml", "bash", f"{server_root}/remote_host_canary.sh", "install"], "server-install.log")
        server_name = remote(server_ep, ["hostname"], "server-hostname.log").strip()
        worker_name = remote(worker_ep, ["hostname"], "worker-hostname.log").strip()
        if not server_name or not worker_name:
            raise LiveFactoryError("heartbeat-aware hostname lookup returned an empty node name")
        work._copy(server_ep, work.config.local_manifest_dir, f"{server_root}/manifests", recursive=True, hard_deadline=deadline, heartbeat=heartbeat, log=args.output / "server-manifests-copy.log")
        with tempfile.TemporaryDirectory(prefix="srecon26-two-node-") as temporary:
            staging = Path(temporary)
            bridge = staging / "server-bridge"
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(bridge)], check=True)
            known_hosts = staging / "server-known-hosts"
            lookup = server_ep.host if server_ep.port == 22 else f"[{server_ep.host}]:{server_ep.port}"
            pinned = subprocess.run(
                ["ssh-keygen", "-F", lookup, "-f", str(work.config.known_hosts_file)],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
            entries = [line for line in pinned.splitlines() if line and not line.startswith("#")]
            if not entries:
                raise LiveFactoryError("server SSH host key is absent from the controller's pinned known-hosts file")
            known_hosts.write_text("\n".join(entries) + "\n", encoding="utf-8")
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
        # The remote script refuses mutable model/image inputs.  Keep this
        # contract beside the run manifest and explicitly carry it over SSH;
        # environment in this controller process is intentionally irrelevant.
        workload_env = [
            f"CANARY_VLLM_IMAGE=docker.io/vllm/vllm-openai@{VLLM_IMAGE_DIGEST}",
            f"CANARY_MODEL={FROZEN_MODEL_ID}",
            f"CANARY_MODEL_REVISION={FROZEN_MODEL_REVISION}",
            f"CANARY_HARD_DEADLINE={deadline.isoformat()}",
            f"CANARY_HARD_DEADLINE_MARGIN_SECONDS={int(REPORT_MARGIN.total_seconds())}",
        ]
        remote(server_ep, ["env", f"CANARY_EVIDENCE_DIR={server_root}/evidence", f"CANARY_MANIFEST_DIR={server_root}/manifests", f"CANARY_MANIFEST_SHA256={manifest_hash}", *workload_env, "bash", f"{server_root}/remote_host_canary.sh", "deploy"], "deploy.log")
        remote(server_ep, ["env", f"CANARY_EVIDENCE_DIR={server_root}/evidence", "bash", f"{server_root}/remote_host_canary.sh", "collect"], "collect.log")
        work._fetch(server_ep, f"{server_root}/evidence", args.output / "server-evidence", hard_deadline=deadline, heartbeat=heartbeat, log=args.output / "evidence-fetch.log")
        remote(server_ep, ["env", f"CANARY_EVIDENCE_DIR={server_root}/evidence", f"CANARY_MANIFEST_DIR={server_root}/manifests", f"CANARY_MANIFEST_SHA256={manifest_hash}", "bash", f"{server_root}/remote_host_canary.sh", "cleanup"], "cleanup.log")

    def finalize_provider_evidence() -> list[str]:
        errors: list[str] = []
        summaries: dict[str, object] = {}
        started = datetime.fromisoformat(str(manifest["started_at"]))
        start_date = started.date().isoformat()
        end_date = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
        for role, label in (("server", server_label), ("worker", worker_label)):
            instance = observed_instances.get(label)
            if instance is None:
                continue
            role_summary: dict[str, object] = {"instance_id": instance.instance_id, "label": instance.label}
            try:
                absence_path = args.output / f"{role}-absence-evidence.json"
                absence = provider.capture_absence_evidence(
                    run_id=getattr(controller, role).run_id,
                    instance_id=instance.instance_id,
                    label=instance.label,
                    artifact=absence_path,
                )
                role_summary["absence"] = {
                    "status": "THREE_READS_CONFIRMED",
                    "artifact": absence_path.name,
                    "sha256": absence.sha256,
                }
            except Exception as error:
                role_summary["absence"] = {"status": "FAILED", "error": str(error)}
                errors.append(f"{role} three-read absence evidence failed: {error}")
            try:
                invoice_path = args.output / f"{role}-invoice-evidence.json"
                invoice = provider.capture_invoice_charge(
                    run_id=getattr(controller, role).run_id,
                    instance_id=instance.instance_id,
                    label=instance.label,
                    start_date=start_date,
                    end_date=end_date,
                    artifact=invoice_path,
                )
                role_summary["billing"] = {
                    "status": "AUTHORITATIVE_INVOICE_CAPTURED",
                    "amount_usd": str(invoice.amount),
                    "artifact": invoice_path.name,
                    "sha256": invoice.sha256,
                }
            except Exception as error:
                role_summary["billing"] = {"status": "PENDING", "error": str(error)}
                errors.append(f"{role} authoritative invoice is pending: {error}")
            summaries[role] = role_summary
        manifest["provider_finalization"] = summaries
        return errors

    def finalize_azure_guards() -> list[str]:
        guard_evidence, errors = finalize_azure_guard_channels(
            guards={"server": server_guard, "worker": worker_guard},
            guard_receipts=manifest["guards"],
            labels={"server": server_label, "worker": worker_label},
            observed_labels=set(observed_instances),
            output=args.output,
            absence_timeout_seconds=args.azure_guard_absence_timeout_seconds,
        )
        manifest["azure_guard_finalization"] = guard_evidence
        return errors

    result = None
    run_error: BaseException | None = None
    run_traceback: str | None = None
    try:
        result = controller.run(workload, heartbeat=heartbeat)
    except BaseException as error:
        run_error = error
        run_traceback = traceback.format_exc()

    finalization_errors = finalize_provider_evidence()
    finalization_errors.extend(finalize_azure_guards())
    manifest["finished_at"] = datetime.now(UTC).isoformat()
    manifest["evidence_status"] = "incomplete" if run_error is not None or finalization_errors else "complete"
    if result is not None:
        manifest["workload_completed"] = result.workload_completed
        manifest["absence_reads"] = result.absence_reads
    if run_error is not None:
        manifest["status"] = "failed"
        manifest["failure"] = {"error_type": type(run_error).__name__, "error": str(run_error)}
        write_json(args.output / "terminal-failure.json", {
            "error_type": type(run_error).__name__, "error": str(run_error), "traceback": run_traceback,
        })
    elif finalization_errors:
        manifest["status"] = "evidence-incomplete"
    else:
        manifest["status"] = "completed"
    if finalization_errors:
        manifest["finalization_errors"] = finalization_errors
    write_json(args.output / "run-manifest.json", manifest)
    if run_error is None and not finalization_errors:
        try:
            subprocess.run(
                [sys.executable, str(ROOT / "scripts/build_evidence_pack.py"), "--two-node-run", str(args.output)],
                cwd=ROOT,
                check=True,
                timeout=90,
            )
            manifest["evidence_pack"] = {
                "status": "REBUILT",
                "summary": "artifacts/evidence-pack/evidence-summary.json",
            }
        except (OSError, subprocess.SubprocessError) as error:
            finalization_errors.append(f"evidence pack rebuild failed: {type(error).__name__}")
            manifest["status"] = "evidence-incomplete"
            manifest["finalization_errors"] = finalization_errors
            manifest["evidence_pack"] = {"status": "FAILED"}
        write_json(args.output / "run-manifest.json", manifest)
    if run_error is not None:
        raise run_error
    if finalization_errors:
        raise LiveFactoryError("two-node resources are absent but evidence finalization is incomplete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
