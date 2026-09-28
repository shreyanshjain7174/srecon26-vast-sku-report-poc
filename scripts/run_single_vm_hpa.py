#!/usr/bin/env python3
"""Plan or execute one guarded single-VM (2 GPU) k3s + vLLM + Prometheus + HPA metric-path run.

One Vast KVM VM with two GPUs hosts the whole k3s cluster, so the ``vllm``
Deployment can scale 1 -> 2 replicas (one GPU each) on a single node without
the cross-host NAT problem of the two-node design.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TypeVar

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "src", ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from scripts.run_single_host_multigpu_benchmark import (
    BenchmarkOrchestratorError,
    RunCommandGuard,
    _atomic_write_json,
    _canonical_json,
    _decimal_text,
    _parse_decimal,
    _parse_stamp,
    _require_executable,
    _require_file,
    _sha256_file,
    _sha256_text,
    _utc_stamp,
    resolve_ssh_endpoint,
)
from srecon26_poc.azure_run_command_transport import AzureRunCommandGuardConfig, AzureRunCommandTransportError
from srecon26_poc.guard_client import GuardClientError
from srecon26_poc.live_dispatch import FROZEN_MODEL_ID, FROZEN_MODEL_REVISION, VLLM_IMAGE_DIGEST
from srecon26_poc.live_factory import SshRemoteWorkload
from srecon26_poc.single_host_multigpu import HEARTBEAT_FAILURE_BUDGET_SECONDS, SingleHostError
from srecon26_poc.single_vm_hpa import (
    K3S_AMD64_SHA256,
    MAX_DEADLINE_MINUTES,
    MIN_DEADLINE_MINUTES,
    REQUIRED_GPU_COUNT,
    VmHpaLease,
    VmLaunchContract,
    VmOffer,
    VmPlan,
    VmSdkProvider,
    vm_offer_rejection,
)
from srecon26_poc.types import RunIdentity
from srecon26_poc.vast_sdk_adapter import SdkInstance, SdkOffer, VastSdkError, create_vast_sdk_client

PLAN_SCHEMA = "run-single-vm-hpa/v1"
PLAN_NAME = "single-vm-hpa-plan.json"
EXECUTION_NAME = "single-vm-hpa-execution.json"
FAILURE_NAME = "single-vm-hpa-failure.json"
EVIDENCE_DIR_NAME = "server-evidence"
DEFAULT_QUERY = (
    "verified=true rentable=true external=false vms_enabled=true num_gpus=2 reliability>0.99 "
    "gpu_ram>=24 cpu_ram>=48 cpu_cores_effective>=8 inet_down>=200 direct_port_count>=1 compute_cap>=860"
)
DEFAULT_MAX_DPH = Decimal("1.20")
DEFAULT_MAX_USD = Decimal("4.00")
DEFAULT_DEADLINE_MINUTES = 90
DEFAULT_OFFER_LIMIT = 25
NETWORK_DOWNLOAD_ALLOWANCE_GB = Decimal("64")
NETWORK_UPLOAD_ALLOWANCE_GB = Decimal("2")
SSH_USER = "root"
DEFAULT_AZ_PATH = Path(shutil.which("az") or "az")
DEFAULT_KNOWN_HOSTS = ROOT / "artifacts" / "live-known-hosts"
DEFAULT_GUARD_WORKER = ROOT / "guard" / "guard_worker.py"
CANARY_SCRIPT = ROOT / "scripts" / "remote_host_canary.sh"
K3S_BINARY = ROOT / "artifacts" / "tools" / "k3s-v1.36.4+k3s1"
K3S_RELEASE_URL = "https://github.com/k3s-io/k3s/releases/download/v1.36.4%2Bk3s1/k3s"
NVIDIA_RUNTIME_TEMPLATE = ROOT / "infra" / "k3s" / "nvidia-runtime.toml"
MANIFEST_DIR = ROOT / "infra" / "k3s"
REQUIRED_MANIFESTS = (
    "namespace.yaml", "device-plugin.yaml", "metrics-server.yaml", "vllm.yaml",
    "prometheus.yaml", "prometheus-adapter.yaml", "hpa-observer.yaml",
)
KUBECTL = ("/usr/local/bin/k3s", "kubectl")
NAMESPACE = "srecon26-canary"
FIELD_MANAGER = "srecon26-remote-canary"
NVIDIA_RUNTIME_DROPIN = "/etc/rancher/k3s/config.yaml.d/50-srecon26-nvidia-default-runtime.yaml"
REMOTE_DEADLINE_MARGIN_SECONDS = 600
TEARDOWN_MARGIN_SECONDS = 900
SMALL_COMMAND_TIMEOUT_SECONDS = 60
SMALL_COMMAND_COUNT = 3
STAGED_COPY_COUNT = 5
HEARTBEAT_INTERVAL_SECONDS = 45.0
POLL_INTERVAL_SECONDS = 5.0
SCALE_POLL_INTERVAL_SECONDS = 10.0
GPU_ALLOCATABLE_POLL_ATTEMPTS = 24
AZURE_GUARD_RPC_TIMEOUT_SECONDS = 150
AZURE_GUARD_CLI_TIMEOUT_SECONDS = 30
GUARD_HEARTBEAT_TIMEOUT_SECONDS = 600
DEFAULT_TIMEOUTS: dict[str, int] = {
    "endpoint_wait": 420,
    "ssh_ready": 150,
    "stage": 90,
    "probe": 90,
    "install": 420,
    "gpu_check": 300,
    "deploy": 1500,
    "scale_observe": 240,
    "collect": 300,
    "fetch": 120,
    "cleanup": 300,
}
# The remote pressure phase ends at now + CANARY_LOAD_SECONDS, so use its 120s maximum to span several HPA syncs.
DEFAULT_LOAD = {"CANARY_LOAD_CONCURRENCY": 32, "CANARY_LOAD_MAX_TOKENS": 512, "CANARY_LOAD_SECONDS": 120}
LOAD_BOUNDS = {"CANARY_LOAD_CONCURRENCY": 32, "CANARY_LOAD_MAX_TOKENS": 1024, "CANARY_LOAD_SECONDS": 120}

NowFactory = Callable[[], datetime]
ResultT = TypeVar("ResultT")

NOW_FACTORY: NowFactory = lambda: datetime.now(UTC)
NONCE_FACTORY: Callable[[], str] = lambda: secrets.token_hex(8)
SDK_CLIENT_FACTORY = create_vast_sdk_client
VM_PROVIDER_FACTORY: Callable[[Any], Any] = lambda client: VmSdkProvider(client)
SUBPROCESS_RUNNER: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
SLEEP: Callable[[float], None] = time.sleep
RUN_COMMAND_GUARD_FACTORY: Callable[[AzureRunCommandGuardConfig, str, str], Any] = (
    lambda config, nonce, script_hash: RunCommandGuard(config, nonce, script_hash)
)


class SingleVmHpaError(BenchmarkOrchestratorError):
    pass


def _run_id_and_label(now: datetime, nonce: str) -> tuple[str, str]:
    if len(nonce) < 8 or not nonce.isalnum():
        raise SingleVmHpaError("nonce is invalid")
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"single-vm-hpa-{stamp}-{nonce[:8]}", f"srecon26-vm2-{stamp.lower()}-{nonce[:8]}--nonce-{nonce}"


def _remote_root(label: str) -> str:
    return f"/var/tmp/srecon26-vm-{_sha256_text(label)[:20]}"


def _offer_payload(offer: VmOffer) -> dict[str, object]:
    sdk = offer.sdk
    return {
        "offer_id": sdk.offer_id,
        "machine_id": sdk.machine_id,
        "gpu_name": sdk.gpu_name,
        "gpu_count": sdk.num_gpus,
        "gpu_ram_mib": sdk.gpu_ram_mib,
        "compute_capability": sdk.compute_capability,
        "dph": _decimal_text(sdk.dph_total),
        "reliability": _decimal_text(sdk.reliability),
        "direct_port_count": sdk.direct_port_count,
        "inet_down_mbps": _decimal_text(sdk.inet_down_mbps),
        "inet_down_cost": _decimal_text(sdk.inet_down_cost),
        "inet_up_cost": _decimal_text(sdk.inet_up_cost),
        "cpu_ram_mib": offer.cpu_ram_mib,
        "cpu_cores": _decimal_text(offer.cpu_cores),
        "vms_enabled": offer.vms_enabled,
        "label": sdk.label,
    }


def _offer_from_payload(payload: Mapping[str, object]) -> VmOffer:
    def dec(name: str) -> Decimal:
        return Decimal(_text(payload.get(name), f"offer.{name}"))

    sdk = SdkOffer(
        offer_id=int(payload["offer_id"]),  # type: ignore[arg-type]
        machine_id=int(payload["machine_id"]),  # type: ignore[arg-type]
        gpu_name=_text(payload.get("gpu_name"), "offer.gpu_name"),
        num_gpus=int(payload["gpu_count"]),  # type: ignore[arg-type]
        gpu_ram_mib=int(payload["gpu_ram_mib"]),  # type: ignore[arg-type]
        compute_capability=_text(payload.get("compute_capability"), "offer.compute_capability"),
        dph_total=dec("dph"),
        reliability=dec("reliability"),
        direct_port_count=int(payload["direct_port_count"]),  # type: ignore[arg-type]
        inet_down_mbps=dec("inet_down_mbps"),
        label=_text(payload.get("label"), "offer.label"),
        inet_down_cost=dec("inet_down_cost"),
        inet_up_cost=dec("inet_up_cost"),
    )
    return VmOffer(sdk, int(payload["cpu_ram_mib"]), dec("cpu_cores"), payload.get("vms_enabled") is True)  # type: ignore[arg-type]


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise SingleVmHpaError(f"{field} must be a non-empty string")
    return value


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise SingleVmHpaError(f"{field} must be an object")
    return value


def _network_allowance(offer: SdkOffer) -> Decimal:
    return offer.inet_down_cost * NETWORK_DOWNLOAD_ALLOWANCE_GB + offer.inet_up_cost * NETWORK_UPLOAD_ALLOWANCE_GB


def _spend_payload(plan: VmPlan, offer: SdkOffer, *, max_usd: Decimal) -> dict[str, object]:
    hours = Decimal(plan.deadline_minutes) / Decimal(60)
    allowance = _network_allowance(offer)
    estimated = plan.dph * hours + allowance
    max_allowed = plan.max_dph * hours + allowance
    if estimated > max_usd:
        raise SingleVmHpaError(f"estimated spend {_decimal_text(estimated)} exceeds --max-usd {_decimal_text(max_usd)}")
    if max_allowed > max_usd:
        raise SingleVmHpaError("max_dph plus network allowance exceeds --max-usd")
    return {
        "deadline_hours": _decimal_text(hours),
        "offer_dph": _decimal_text(plan.dph),
        "max_dph": _decimal_text(plan.max_dph),
        "network_download_allowance_gb": _decimal_text(NETWORK_DOWNLOAD_ALLOWANCE_GB),
        "network_upload_allowance_gb": _decimal_text(NETWORK_UPLOAD_ALLOWANCE_GB),
        "network_allowance_usd": _decimal_text(allowance),
        "estimated_offer_spend_usd": _decimal_text(estimated),
        "max_allowed_spend_usd": _decimal_text(max_allowed),
        "operator_max_usd": _decimal_text(max_usd),
    }


def select_offer(provider: Any, *, query: str, limit: int, label: str, max_dph: Decimal, deadline_minutes: int) -> tuple[VmOffer, VmPlan]:
    candidates: list[tuple[VmOffer, VmPlan]] = []
    for offer in provider.search_vm_offers(query, limit=limit, label=label):
        if vm_offer_rejection(offer) is not None:
            continue
        try:
            plan = VmPlan.for_offer(offer, max_dph=max_dph, deadline_minutes=deadline_minutes)
        except ValueError:
            continue
        candidates.append((offer, plan))
    if not candidates:
        raise SingleVmHpaError("no eligible 2-GPU vms_enabled offer satisfies the single-VM HPA contract")
    hours = Decimal(deadline_minutes) / Decimal(60)
    candidates.sort(key=lambda item: (
        item[0].sdk.dph_total * hours + _network_allowance(item[0].sdk),
        -item[0].sdk.reliability,
        item[0].sdk.offer_id,
    ))
    return candidates[0]


def _validated_timeouts(timeouts: Mapping[str, object], deadline_minutes: int) -> dict[str, int]:
    if set(timeouts) != set(DEFAULT_TIMEOUTS):
        raise SingleVmHpaError("remote timeouts must name exactly the signed steps")
    values: dict[str, int] = {}
    for name, value in timeouts.items():
        if type(value) is not int or value <= 0:
            raise SingleVmHpaError(f"timeout {name} must be a positive integer")
        values[name] = value
    budget = (
        sum(values.values())
        + values["stage"] * (STAGED_COPY_COUNT - 1)
        + SMALL_COMMAND_TIMEOUT_SECONDS * SMALL_COMMAND_COUNT
        + TEARDOWN_MARGIN_SECONDS
    )
    if budget > deadline_minutes * 60:
        raise SingleVmHpaError(
            f"remote step timeouts ({budget}s incl. {TEARDOWN_MARGIN_SECONDS}s teardown) exceed the {deadline_minutes}-minute deadline"
        )
    return values


def _validated_load(load: Mapping[str, object]) -> dict[str, int]:
    if set(load) != set(LOAD_BOUNDS) or any(type(value) is not int or not 1 <= value <= LOAD_BOUNDS[name] for name, value in load.items()):
        raise SingleVmHpaError(f"load env must be exactly {sorted(LOAD_BOUNDS)} with remote-script bounds {LOAD_BOUNDS}")
    return dict(load)  # type: ignore[arg-type]


def _workload_files() -> dict[str, dict[str, str]]:
    script = _require_file(CANARY_SCRIPT, "remote canary script")
    if f'readonly K3S_AMD64_SHA256="{K3S_AMD64_SHA256}"' not in script.read_text(encoding="utf-8"):
        raise SingleVmHpaError("remote canary script does not pin the expected k3s checksum")
    k3s = _require_executable(K3S_BINARY, "k3s binary")
    if _sha256_file(k3s) != K3S_AMD64_SHA256:
        raise SingleVmHpaError("local k3s binary does not match the pinned checksum")
    runtime = _require_file(NVIDIA_RUNTIME_TEMPLATE, "NVIDIA runtime template")
    if "runtimes.nvidia" not in runtime.read_text(encoding="utf-8"):
        raise SingleVmHpaError("NVIDIA runtime template does not configure the nvidia runtime")
    manifests = MANIFEST_DIR.resolve()
    missing = [name for name in REQUIRED_MANIFESTS if not (manifests / name).is_file()]
    if missing:
        raise SingleVmHpaError(f"manifest bundle is missing {', '.join(missing)}")
    if VLLM_IMAGE_DIGEST not in (manifests / "vllm.yaml").read_text(encoding="utf-8"):
        raise SingleVmHpaError("vllm.yaml does not pin the frozen vLLM image digest")
    if "maxReplicas: 2" not in (manifests / "hpa-observer.yaml").read_text(encoding="utf-8"):
        raise SingleVmHpaError("hpa-observer.yaml must allow exactly 2 replicas")
    return {
        "canary_script": {"path": str(script), "sha256": _sha256_file(script)},
        "k3s_binary": {"path": str(k3s), "sha256": _sha256_file(k3s)},
        "nvidia_runtime": {"path": str(runtime), "sha256": _sha256_file(runtime)},
        "device_plugin": {"path": str(manifests / "device-plugin.yaml"), "sha256": _sha256_file(manifests / "device-plugin.yaml")},
        "manifest_bundle": {"path": str(manifests), "sha256": SshRemoteWorkload._manifest_hash(manifests)},
    }


def build_plan_document(
    *,
    provider: Any,
    query: str,
    limit: int,
    deadline_minutes: int,
    max_dph: Decimal,
    max_usd: Decimal,
    subscription_id: str,
    resource_group: str,
    vm_name: str,
    az_path: Path,
    guard_worker: Path,
    plan_path: Path | None,
    timeouts: Mapping[str, object],
    load: Mapping[str, int],
    nvidia_default_runtime: bool,
    now: datetime,
    nonce: str,
) -> tuple[dict[str, object], Path, str]:
    if type(deadline_minutes) is not int or not MIN_DEADLINE_MINUTES <= deadline_minutes <= MAX_DEADLINE_MINUTES:
        raise SingleVmHpaError(f"deadline_minutes must be in [{MIN_DEADLINE_MINUTES}, {MAX_DEADLINE_MINUTES}]")
    signed_timeouts = _validated_timeouts(timeouts, deadline_minutes)
    _validated_load(load)
    run_id, label = _run_id_and_label(now, nonce)
    resolved_az = _require_executable(az_path, "Azure CLI")
    files = _workload_files()
    offer, plan = select_offer(provider, query=query, limit=limit, label=label, max_dph=max_dph, deadline_minutes=deadline_minutes)
    launch = VmLaunchContract()
    launch.validate()
    hard_deadline = now.astimezone(UTC) + timedelta(minutes=deadline_minutes)
    root = _remote_root(label)
    document: dict[str, object] = {
        "schema": PLAN_SCHEMA,
        "run_identity": {"run_id": run_id, "nonce": nonce, "label": label, "created_at": _utc_stamp(now)},
        "offer": _offer_payload(offer),
        "vm_plan": plan.canonical_dict(),
        "vm_plan_sha256": plan.sha256(),
        "launch_contract": launch.payload(),
        "guard_binding": {
            "subscription_id": subscription_id,
            "resource_group": resource_group,
            "vm_name": vm_name,
            "rpc_timeout_seconds": AZURE_GUARD_RPC_TIMEOUT_SECONDS,
            "cli_timeout_seconds": AZURE_GUARD_CLI_TIMEOUT_SECONDS,
            "heartbeat_timeout_seconds": GUARD_HEARTBEAT_TIMEOUT_SECONDS,
            "az_path": str(resolved_az),
            "az_sha256": _sha256_file(resolved_az),
            "worker_source_path": str(guard_worker),
            "worker_sha256": _sha256_file(guard_worker),
        },
        "remote_workload": {
            "ssh_user": SSH_USER,
            "remote_root": root,
            "files": files,
            "timeouts_seconds": signed_timeouts,
            "load_env": dict(load),
            "nvidia_default_runtime_dropin": NVIDIA_RUNTIME_DROPIN if nvidia_default_runtime else None,
            "required_allocatable_gpus": REQUIRED_GPU_COUNT,
        },
        "model_contract": {
            "vllm_image": f"docker.io/vllm/vllm-openai@{VLLM_IMAGE_DIGEST}",
            "model_id": FROZEN_MODEL_ID,
            "model_revision": FROZEN_MODEL_REVISION,
            "remote_deadline_margin_seconds": REMOTE_DEADLINE_MARGIN_SECONDS,
        },
        "deadline": {"deadline_minutes": deadline_minutes, "hard_deadline": _utc_stamp(hard_deadline)},
        "spend_limits": _spend_payload(plan, offer.sdk, max_usd=max_usd),
    }
    document_sha256 = _sha256_text(_canonical_json(document))
    target = (ROOT / "artifacts" / "live-runs" / run_id / PLAN_NAME) if plan_path is None else plan_path.expanduser().resolve()
    return {"document": document, "document_sha256": document_sha256}, target, document_sha256


def _read_plan(path: Path) -> tuple[dict[str, object], str]:
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SingleVmHpaError(f"plan file not found: {path}") from error
    except json.JSONDecodeError as error:
        raise SingleVmHpaError(f"plan file is not valid JSON: {path}") from error
    if not isinstance(envelope, dict) or not isinstance(envelope.get("document"), dict) or not isinstance(envelope.get("document_sha256"), str):
        raise SingleVmHpaError("plan file is missing document or document_sha256")
    actual = _sha256_text(_canonical_json(envelope["document"]))
    if envelope["document_sha256"] != actual:
        raise SingleVmHpaError("plan file content does not match its recorded sha256")
    if envelope["document"].get("schema") != PLAN_SCHEMA:
        raise SingleVmHpaError("plan schema is not the single-VM HPA schema")
    return envelope["document"], actual


def _verify_signed_files(files: Mapping[str, object]) -> dict[str, Path]:
    resolved: dict[str, Path] = {}
    for key in ("canary_script", "k3s_binary", "nvidia_runtime", "device_plugin"):
        entry = _mapping(files.get(key), f"files.{key}")
        path = _require_file(Path(_text(entry.get("path"), f"files.{key}.path")), key)
        if _sha256_file(path) != _text(entry.get("sha256"), f"files.{key}.sha256"):
            raise SingleVmHpaError(f"{key} sha256 no longer matches the signed plan")
        resolved[key] = path
    if _text(_mapping(files.get("k3s_binary"), "files.k3s_binary").get("sha256"), "k3s sha") != K3S_AMD64_SHA256:
        raise SingleVmHpaError("signed k3s binary checksum is not the pinned release")
    bundle = _mapping(files.get("manifest_bundle"), "files.manifest_bundle")
    bundle_dir = Path(_text(bundle.get("path"), "files.manifest_bundle.path"))
    if not bundle_dir.is_dir() or SshRemoteWorkload._manifest_hash(bundle_dir) != _text(bundle.get("sha256"), "bundle sha"):
        raise SingleVmHpaError("manifest bundle sha256 no longer matches the signed plan")
    resolved["manifest_bundle"] = bundle_dir
    return resolved


def _run_subprocess(arguments: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return SUBPROCESS_RUNNER(list(arguments), text=True, capture_output=True, check=False, shell=False, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        raise SingleVmHpaError(f"subprocess failed or timed out: {arguments[0]}") from error


def _run_with_heartbeats(action: Callable[[], ResultT], heartbeat: Callable[[], None]) -> ResultT:
    stop = threading.Event()
    failures: list[BaseException] = []

    def pump() -> None:
        last_success = time.monotonic()
        while not stop.wait(HEARTBEAT_INTERVAL_SECONDS):
            try:
                heartbeat()
                last_success = time.monotonic()
            except BaseException as error:
                if time.monotonic() - last_success > HEARTBEAT_FAILURE_BUDGET_SECONDS:
                    failures.append(error)
                    return

    heartbeat()
    thread = threading.Thread(target=pump, name="single-vm-hpa-heartbeat", daemon=True)
    thread.start()
    try:
        result = action()
    finally:
        stop.set()
        thread.join()
    if failures:
        raise SingleVmHpaError("Azure guard heartbeat failed during the VM workload") from failures[0]
    heartbeat()
    return result


class RemoteSession:
    """SSH/SCP to one resolved endpoint; every step writes ``<step>.log`` and is deadline-capped."""

    def __init__(self, *, identity: Path, known_hosts: Path, host: str, port: int, out: Path, hard_deadline: datetime) -> None:
        self.identity, self.known_hosts, self.host, self.port = identity, known_hosts, host, port
        self.out, self.hard_deadline = out, hard_deadline
        self.steps: list[str] = []

    def _base(self, *, scp: bool) -> list[str]:
        return [
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={self.known_hosts}",
            "-o", "ConnectTimeout=20",
            "-i", str(self.identity),
            "-P" if scp else "-p", str(self.port),
        ]

    def _target(self) -> str:
        return f"{SSH_USER}@{self.host}"

    def cap(self, timeout: float) -> float:
        remaining = (self.hard_deadline - NOW_FACTORY().astimezone(UTC)).total_seconds() - TEARDOWN_MARGIN_SECONDS
        if remaining <= 0:
            raise SingleVmHpaError("remote work reached the teardown margin before the hard deadline")
        return min(float(timeout), remaining)

    def _log(self, step: str, argv: Sequence[str], completed: subprocess.CompletedProcess[str] | None) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        with (self.out / f"{step}.log").open("a", encoding="utf-8") as handle:
            handle.write(f"$ {shlex.join(argv)}\n")
            if completed is not None:
                handle.write(f"exit={completed.returncode}\n{completed.stdout or ''}{completed.stderr or ''}\n")

    def run(self, step: str, remote_argv: Sequence[str], *, timeout: float, record: bool = True) -> str:
        argv = ["ssh", *self._base(scp=False), self._target(), shlex.join(remote_argv)]
        try:
            completed = _run_subprocess(argv, timeout=self.cap(timeout))
        except SingleVmHpaError:
            self._log(step, remote_argv, None)
            raise
        self._log(step, remote_argv, completed)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "unknown error").strip()[-300:]
            raise SingleVmHpaError(f"remote step {step} failed: {detail}")
        if record:
            self.steps.append(step)
        return completed.stdout or ""

    def upload(self, step: str, local: Path, remote: str, *, timeout: float, recursive: bool = False) -> None:
        argv = ["scp", *self._base(scp=True), *(["-r"] if recursive else []), str(local), f"{self._target()}:{remote}"]
        completed = _run_subprocess(argv, timeout=self.cap(timeout))
        self._log(step, argv, completed)
        if completed.returncode != 0:
            raise SingleVmHpaError(f"upload {step} failed: {(completed.stderr or completed.stdout or 'unknown error').strip()[-300:]}")
        self.steps.append(step)

    def fetch(self, step: str, remote: str, local: Path, *, timeout: float) -> None:
        if local.exists():
            raise SingleVmHpaError(f"refusing to overwrite existing evidence directory {local}")
        temporary = local.with_name(f".{local.name}.{secrets.token_hex(8)}.tmp")
        argv = ["scp", *self._base(scp=True), "-r", f"{self._target()}:{remote}", str(temporary)]
        completed = _run_subprocess(argv, timeout=self.cap(timeout))
        self._log(step, argv, completed)
        if completed.returncode != 0:
            raise SingleVmHpaError(f"evidence fetch failed: {(completed.stderr or completed.stdout or 'unknown error').strip()[-300:]}")
        os.replace(temporary, local)
        self.steps.append(step)


def _wait_for_attach_status(provider: Any, instance: SdkInstance, *, wait_seconds: int, poll_seconds: float) -> None:
    attempts = max(1, math.ceil(wait_seconds / poll_seconds))
    for attempt in range(attempts):
        try:
            current = provider.get_instance(instance.instance_id)
        except VastSdkError:
            current = None
        if current is not None:
            if current.label != instance.label:
                raise SingleVmHpaError("exact instance label changed before SSH key attach")
            if current.actual_status in {"loading", "running"}:
                return
        if attempt + 1 < attempts:
            SLEEP(poll_seconds)
    raise SingleVmHpaError("exact instance did not reach loading/running before SSH key attach deadline")


def _gpu_check(session: RemoteSession, root: str, *, timeout: int) -> dict[str, object]:
    """Fail before deploy unless the one node is Ready with exactly 2 allocatable GPUs."""
    step = "gpu-check"
    gpu_lines = [line for line in session.run(step, ["nvidia-smi", "--query-gpu=index,name,memory.total", "--format=csv,noheader"], timeout=60, record=False).splitlines() if line.strip()]
    if len(gpu_lines) != REQUIRED_GPU_COUNT:
        raise SingleVmHpaError(f"host nvidia-smi reports {len(gpu_lines)} GPUs, expected {REQUIRED_GPU_COUNT}")
    containerd = session.run(step, ["sh", "-c", "grep -n -i -E 'default_runtime_name|runtimes.nvidia' /var/lib/rancher/k3s/agent/etc/containerd/config*.toml 2>&1 || true"], timeout=60, record=False)
    listeners = session.run(step, ["sh", "-c", "ss -H -lntu 2>&1 || true"], timeout=60, record=False)
    session.run(step, [*KUBECTL, "apply", "--server-side", f"--field-manager={FIELD_MANAGER}", "-f", f"{root}/device-plugin.yaml"], timeout=60, record=False)
    rollout = max(30, timeout - 120)
    session.run(step, [*KUBECTL, "-n", "kube-system", "rollout", "status", "daemonset/nvidia-device-plugin", f"--timeout={rollout}s"], timeout=rollout + 30, record=False)
    node: dict[str, object] | None = None
    for attempt in range(GPU_ALLOCATABLE_POLL_ATTEMPTS):
        raw = session.run(step, [*KUBECTL, "get", "nodes", "-o", "json"], timeout=60, record=False)
        try:
            items = json.loads(raw).get("items", [])
        except (ValueError, AttributeError):
            items = []
        if len(items) != 1:
            raise SingleVmHpaError(f"expected exactly one k3s node, found {len(items)}")
        item = items[0]
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in item.get("status", {}).get("conditions", []))
        allocatable = item.get("status", {}).get("allocatable", {}).get("nvidia.com/gpu")
        labelled = item.get("metadata", {}).get("labels", {}).get("srecon26.io/vllm-gpu") == "true"
        node = {"name": item.get("metadata", {}).get("name"), "ready": ready, "labelled": labelled, "allocatable_gpus": allocatable}
        if ready and labelled and allocatable == str(REQUIRED_GPU_COUNT):
            break
        if attempt + 1 < GPU_ALLOCATABLE_POLL_ATTEMPTS:
            SLEEP(POLL_INTERVAL_SECONDS)
    else:
        raise SingleVmHpaError(f"node never reported Ready with nvidia.com/gpu == \"{REQUIRED_GPU_COUNT}\": {node}")
    # Mirror remote_host_canary.sh assert_ssh_only_public_listeners so a deploy refusal is diagnosable.
    wildcard = [
        fields[4] for fields in (line.split() for line in listeners.splitlines())
        if len(fields) >= 5 and fields[4].startswith(("*:", "0.0.0.0:", "[::]:")) and not fields[4].endswith(":22")
    ]
    session.steps.append(step)
    return {"host_gpus": gpu_lines, "node": node, "containerd_runtime_lines": containerd.strip().splitlines(), "non_ssh_wildcard_listeners": wildcard}


def _scale_observe(session: RemoteSession, *, seconds: int) -> dict[str, object]:
    """Record HPA desired and Deployment ready replicas until 2 are ready or the window ends."""
    step = "scale-observe"
    samples: list[dict[str, object]] = []
    attempts = max(1, math.ceil(seconds / SCALE_POLL_INTERVAL_SECONDS))
    reached = False
    for attempt in range(attempts):
        try:
            deployment = json.loads(session.run(step, [*KUBECTL, "-n", NAMESPACE, "get", "deployment", "vllm", "-o", "json"], timeout=60, record=False))
            hpa = json.loads(session.run(step, [*KUBECTL, "-n", NAMESPACE, "get", "hpa", "vllm-observer", "-o", "json"], timeout=60, record=False))
        except (SingleVmHpaError, ValueError) as error:
            samples.append({"attempt": attempt, "error": str(error)[:300]})
        else:
            sample = {
                "attempt": attempt,
                "hpa_desired_replicas": hpa.get("status", {}).get("desiredReplicas"),
                "hpa_current_replicas": hpa.get("status", {}).get("currentReplicas"),
                "deployment_replicas": deployment.get("spec", {}).get("replicas"),
                "deployment_ready_replicas": deployment.get("status", {}).get("readyReplicas", 0),
            }
            samples.append(sample)
            if sample["deployment_ready_replicas"] == REQUIRED_GPU_COUNT:
                reached = True
                break
        if attempt + 1 < attempts:
            SLEEP(SCALE_POLL_INTERVAL_SECONDS)
    session.steps.append(step)
    return {"reached_two_ready_replicas": reached, "samples": samples}


def execute_plan(args: argparse.Namespace) -> dict[str, object]:
    plan_file = args.plan.expanduser().resolve()
    document, actual_hash = _read_plan(plan_file)
    if not args.confirm_plan_sha256:
        raise SingleVmHpaError("--confirm-plan-sha256 is required with --execute")
    if args.confirm_plan_sha256 != actual_hash:
        raise SingleVmHpaError("--confirm-plan-sha256 does not match the signed execution document")
    identity_doc = _mapping(document.get("run_identity"), "run_identity")
    run_id = _text(identity_doc.get("run_id"), "run_identity.run_id")
    nonce = _text(identity_doc.get("nonce"), "run_identity.nonce")
    label = _text(identity_doc.get("label"), "run_identity.label")
    created_at = _parse_stamp(_text(identity_doc.get("created_at"), "run_identity.created_at"), "run_identity.created_at")
    offer = _offer_from_payload(_mapping(document.get("offer"), "offer"))
    if offer.sdk.label != label:
        raise SingleVmHpaError("offer label differs from the run identity label")
    try:
        plan = VmPlan.from_payload(_mapping(document.get("vm_plan"), "vm_plan"))
    except (ValueError, TypeError) as error:
        raise SingleVmHpaError(f"vm_plan is invalid: {error}") from error
    if plan.sha256() != _text(document.get("vm_plan_sha256"), "vm_plan_sha256"):
        raise SingleVmHpaError("vm plan sha256 does not match its canonical payload")
    launch_doc = _mapping(document.get("launch_contract"), "launch_contract")
    launch = VmLaunchContract(
        template_hash=_text(launch_doc.get("template_hash"), "launch_contract.template_hash"),
        image_contract=_text(launch_doc.get("image_contract"), "launch_contract.image_contract"),
        disk_gib=launch_doc.get("disk_gib"),  # type: ignore[arg-type]
        ssh=launch_doc.get("ssh") is True,
        direct=launch_doc.get("direct") is True,
        cancel_unavail=launch_doc.get("cancel_unavail") is True,
    )
    launch.validate()
    guard_binding = _mapping(document.get("guard_binding"), "guard_binding")
    worker_source = _require_file(Path(_text(guard_binding.get("worker_source_path"), "guard_binding.worker_source_path")), "guard worker source")
    expected_worker_hash = _text(guard_binding.get("worker_sha256"), "guard_binding.worker_sha256")
    if _sha256_file(worker_source) != expected_worker_hash:
        raise SingleVmHpaError("guard worker source sha256 no longer matches the signed plan")
    signed_az = _require_executable(Path(_text(guard_binding.get("az_path"), "guard_binding.az_path")), "signed Azure CLI")
    if _sha256_file(signed_az) != _text(guard_binding.get("az_sha256"), "guard_binding.az_sha256"):
        raise SingleVmHpaError("Azure CLI sha256 no longer matches the signed plan")
    if _require_executable(args.az_path, "Azure CLI") != signed_az:
        raise SingleVmHpaError("--az-path does not match the signed plan")
    for key, expected in (
        ("rpc_timeout_seconds", AZURE_GUARD_RPC_TIMEOUT_SECONDS),
        ("cli_timeout_seconds", AZURE_GUARD_CLI_TIMEOUT_SECONDS),
        ("heartbeat_timeout_seconds", GUARD_HEARTBEAT_TIMEOUT_SECONDS),
    ):
        if type(guard_binding.get(key)) is not int or guard_binding[key] != expected:
            raise SingleVmHpaError(f"guard_binding.{key} differs from the signed safety contract")
    workload_doc = _mapping(document.get("remote_workload"), "remote_workload")
    files = _verify_signed_files(_mapping(workload_doc.get("files"), "remote_workload.files"))
    files_doc = _mapping(workload_doc.get("files"), "remote_workload.files")
    deadline_doc = _mapping(document.get("deadline"), "deadline")
    deadline_minutes = deadline_doc.get("deadline_minutes")
    if deadline_minutes != plan.deadline_minutes:
        raise SingleVmHpaError("deadline minutes differ from the signed VM plan")
    timeouts = _validated_timeouts(_mapping(workload_doc.get("timeouts_seconds"), "timeouts_seconds"), plan.deadline_minutes)
    load_env = _validated_load(_mapping(workload_doc.get("load_env"), "remote_workload.load_env"))
    dropin = workload_doc.get("nvidia_default_runtime_dropin")
    if dropin not in (None, NVIDIA_RUNTIME_DROPIN):
        raise SingleVmHpaError("nvidia default runtime drop-in path is not the signed path")
    root = _text(workload_doc.get("remote_root"), "remote_workload.remote_root")
    if root != _remote_root(label):
        raise SingleVmHpaError("remote root is not derived from the signed label")
    model = _mapping(document.get("model_contract"), "model_contract")
    if (
        model.get("vllm_image") != f"docker.io/vllm/vllm-openai@{VLLM_IMAGE_DIGEST}"
        or model.get("model_id") != FROZEN_MODEL_ID
        or model.get("model_revision") != FROZEN_MODEL_REVISION
    ):
        raise SingleVmHpaError("model contract differs from the frozen vLLM contract")
    hard_deadline = _parse_stamp(_text(deadline_doc.get("hard_deadline"), "deadline.hard_deadline"), "deadline.hard_deadline")
    remaining = (hard_deadline - NOW_FACTORY().astimezone(UTC)).total_seconds()
    if remaining < timeouts["install"] + timeouts["deploy"] + TEARDOWN_MARGIN_SECONDS:
        raise SingleVmHpaError("signed deadline no longer leaves enough time for install, deploy, and teardown")

    out = (args.artifacts_dir.expanduser().resolve() if args.artifacts_dir else plan_file.parent)
    if any(out.glob("*.log")) or (out / EXECUTION_NAME).exists() or (out / FAILURE_NAME).exists() or (out / EVIDENCE_DIR_NAME).exists():
        raise SingleVmHpaError(f"run output directory already has execution artifacts; refusing replay: {out}")
    identity_file = _require_file(args.ssh_identity_file, "ssh identity file")
    public_key_file = _require_file(args.ssh_public_key_file or Path(f"{identity_file}.pub"), "ssh public key file")
    public_key = public_key_file.read_text(encoding="utf-8").strip()
    known_hosts = args.known_hosts_file.expanduser().resolve()
    known_hosts.parent.mkdir(parents=True, exist_ok=True)

    client = SDK_CLIENT_FACTORY(api_key=os.environ.get("VAST_API_KEY"))
    provider = VM_PROVIDER_FACTORY(client)
    current = provider.get_vm_offer(offer.sdk.offer_id, offer.sdk.machine_id, label)
    if _offer_payload(current) != _offer_payload(offer):
        raise SingleVmHpaError("planned VM offer drifted from the current provider offer")

    guard_config = AzureRunCommandGuardConfig(
        subscription_id=_text(guard_binding.get("subscription_id"), "guard_binding.subscription_id"),
        resource_group=_text(guard_binding.get("resource_group"), "guard_binding.resource_group"),
        vm_name=_text(guard_binding.get("vm_name"), "guard_binding.vm_name"),
        az_path=signed_az,
        timeout_seconds=AZURE_GUARD_RPC_TIMEOUT_SECONDS,
        cli_timeout_seconds=AZURE_GUARD_CLI_TIMEOUT_SECONDS,
        heartbeat_timeout_seconds=GUARD_HEARTBEAT_TIMEOUT_SECONDS,
    )
    guard = RUN_COMMAND_GUARD_FACTORY(guard_config, nonce, expected_worker_hash)
    heartbeat_identity = RunIdentity(run_id, label, created_at)
    manifest_sha = _text(_mapping(files_doc.get("manifest_bundle"), "manifest_bundle").get("sha256"), "manifest sha")
    staged_sha = {
        f"{root}/remote_host_canary.sh": _text(_mapping(files_doc.get("canary_script"), "canary_script").get("sha256"), "script sha"),
        f"{root}/k3s": _text(_mapping(files_doc.get("k3s_binary"), "k3s_binary").get("sha256"), "k3s sha"),
        f"{root}/nvidia-runtime.toml": _text(_mapping(files_doc.get("nvidia_runtime"), "nvidia_runtime").get("sha256"), "runtime sha"),
        f"{root}/device-plugin.yaml": _text(_mapping(files_doc.get("device_plugin"), "device_plugin").get("sha256"), "plugin sha"),
    }
    script = f"{root}/remote_host_canary.sh"
    evidence = f"{root}/evidence"
    manifest_env = [f"CANARY_EVIDENCE_DIR={evidence}", f"CANARY_MANIFEST_DIR={root}/manifests", f"CANARY_MANIFEST_SHA256={manifest_sha}"]
    workload_env = [
        f"CANARY_VLLM_IMAGE={model['vllm_image']}",
        f"CANARY_MODEL={FROZEN_MODEL_ID}",
        f"CANARY_MODEL_REVISION={FROZEN_MODEL_REVISION}",
        f"CANARY_HARD_DEADLINE={_utc_stamp(hard_deadline)}",
        f"CANARY_HARD_DEADLINE_MARGIN_SECONDS={REMOTE_DEADLINE_MARGIN_SECONDS}",
        *(f"{name}={value}" for name, value in sorted(load_env.items())),
    ]
    stage = "create-or-verify"
    outcome: dict[str, object] = {}

    def heartbeat() -> None:
        guard.record_heartbeat(heartbeat_identity, time.monotonic_ns())

    def workload(instance: SdkInstance) -> None:
        nonlocal stage
        stage = "attach-ssh-key"
        _wait_for_attach_status(provider, instance, wait_seconds=timeouts["endpoint_wait"], poll_seconds=args.endpoint_poll_seconds)
        provider.attach_ssh_key(instance.instance_id, instance.label, public_key)
        stage = "resolve-ssh"
        host, port, route, exact = resolve_ssh_endpoint(
            provider=provider, expected=instance, wait_seconds=timeouts["endpoint_wait"],
            poll_seconds=args.endpoint_poll_seconds, now=NOW_FACTORY, sleep=SLEEP,
        )
        outcome.update({"instance_id": exact.instance_id, "machine_id": exact.machine_id, "ssh_route": route, "ssh_host": host, "ssh_port": port})
        session = RemoteSession(identity=identity_file, known_hosts=known_hosts, host=host, port=port, out=out, hard_deadline=hard_deadline)
        outcome["steps"] = session.steps
        run_error: BaseException | None = None
        error_stage = stage
        manifests_staged = False
        try:
            stage = "wait-ssh"
            attempts = max(1, math.ceil(timeouts["ssh_ready"] / POLL_INTERVAL_SECONDS))
            for attempt in range(attempts):
                try:
                    session.run("wait-ssh", ["true"], timeout=30)
                    break
                except SingleVmHpaError:
                    if attempt + 1 == attempts:
                        raise
                    SLEEP(POLL_INTERVAL_SECONDS)
            stage = "stage"
            session.run("mkdir", ["install", "-d", "-m", "0700", root], timeout=SMALL_COMMAND_TIMEOUT_SECONDS)
            session.upload("stage-script", files["canary_script"], script, timeout=timeouts["stage"])
            # Fetch the ~79 MB k3s release on the VM itself; a long upload from the
            # controller over a home uplink proved flaky.  verify-staged below still
            # enforces the signed sha256, so the source does not change trust.
            try:
                session.run(
                    "fetch-k3s",
                    ["curl", "-fsSL", "--retry", "3", "--max-time", str(timeouts["stage"]), "-o", f"{root}/k3s", K3S_RELEASE_URL],
                    timeout=timeouts["stage"] + 30,
                )
            except SingleVmHpaError:
                session.upload("stage-k3s", files["k3s_binary"], f"{root}/k3s", timeout=timeouts["stage"])
            session.upload("stage-runtime", files["nvidia_runtime"], f"{root}/nvidia-runtime.toml", timeout=timeouts["stage"])
            session.upload("stage-device-plugin", files["device_plugin"], f"{root}/device-plugin.yaml", timeout=timeouts["stage"])
            staged = session.run("verify-staged", ["sha256sum", *staged_sha], timeout=SMALL_COMMAND_TIMEOUT_SECONDS)
            observed = {parts[1]: parts[0] for parts in (line.split() for line in staged.splitlines()) if len(parts) == 2}
            if observed != staged_sha:
                raise SingleVmHpaError("staged remote files do not match the signed sha256 values")
            stage = "probe"
            session.run("probe", ["env", f"CANARY_EVIDENCE_DIR={evidence}", "bash", script, "probe"], timeout=timeouts["probe"])
            stage = "install"
            if dropin is not None:
                session.run("runtime-dropin", ["sh", "-ceu", f"install -d -m 0755 /etc/rancher/k3s/config.yaml.d && printf 'default-runtime: \"nvidia\"\\n' > {dropin}"], timeout=SMALL_COMMAND_TIMEOUT_SECONDS)
            session.run("install", ["env", f"CANARY_EVIDENCE_DIR={evidence}", f"K3S_BINARY_PATH={root}/k3s", f"NVIDIA_RUNTIME_TEMPLATE={root}/nvidia-runtime.toml", "bash", script, "install"], timeout=timeouts["install"])
            stage = "gpu-check"
            outcome["gpu_check"] = _gpu_check(session, root, timeout=timeouts["gpu_check"])
            stage = "stage-manifests"
            session.upload("stage-manifests", files["manifest_bundle"], f"{root}/manifests", timeout=timeouts["stage"], recursive=True)
            manifests_staged = True
            stage = "deploy"
            session.run("deploy", ["env", *manifest_env, *workload_env, "bash", script, "deploy"], timeout=timeouts["deploy"])
            stage = "scale-observe"
            outcome["scale_observation"] = _scale_observe(session, seconds=timeouts["scale_observe"])
            stage = "collect"
            session.run("collect", ["env", f"CANARY_EVIDENCE_DIR={evidence}", "bash", script, "collect"], timeout=timeouts["collect"])
        except BaseException as error:
            run_error, error_stage = error, stage
        fetch_error: BaseException | None = None
        cleanup_error: BaseException | None = None
        try:
            stage = "fetch-evidence"
            session.fetch("fetch-evidence", evidence, out / EVIDENCE_DIR_NAME, timeout=timeouts["fetch"])
        except BaseException as error:
            fetch_error = error
        if manifests_staged:
            try:
                stage = "cleanup"
                session.run("cleanup", ["env", *manifest_env, "bash", script, "cleanup"], timeout=timeouts["cleanup"])
            except BaseException as error:
                cleanup_error = error
        outcome["evidence_fetched"] = fetch_error is None
        if run_error is not None:
            stage = error_stage
            raise run_error
        if fetch_error is not None:
            stage = "fetch-evidence"
            raise fetch_error
        if cleanup_error is not None:
            stage = "cleanup"
            raise cleanup_error

    lease = VmHpaLease(
        provider=provider, offer=current.sdk, plan=plan, launch=launch, guard=guard,
        run_id=run_id, nonce=nonce, hard_deadline=hard_deadline, now=NOW_FACTORY, sleep=SLEEP,
    )
    try:
        result = lease.run(lambda instance: _run_with_heartbeats(lambda: workload(instance), heartbeat), heartbeat=heartbeat)
    except SingleHostError as error:
        chain: list[str] = []
        cursor: BaseException | None = error
        while cursor is not None and len(chain) < 8:
            chain.append(f"{type(cursor).__name__}: {cursor}"[:500])
            cursor = cursor.__cause__
        _atomic_write_json(out / FAILURE_NAME, {
            "run_id": run_id,
            "plan_sha256": actual_hash,
            "stage": stage,
            "error_type": type(error.__cause__).__name__ if error.__cause__ is not None else type(error).__name__,
            "error_chain": chain,
            "teardown_and_absence_confirmed": str(error).startswith("multi-GPU workload failed after safe cleanup"),
            **{key: value for key, value in outcome.items() if key != "steps"},
            "steps": list(outcome.get("steps", [])),  # type: ignore[arg-type]
        })
        raise SingleVmHpaError(str(error)) from error
    execution = {
        "run_id": run_id,
        "plan_sha256": actual_hash,
        "label": label,
        "absence_reads": result.absence_reads,
        "workload_completed": result.workload_completed,
        "evidence_dir": str(out / EVIDENCE_DIR_NAME),
        **{key: value for key, value in outcome.items() if key != "steps"},
        "steps": list(outcome.get("steps", [])),  # type: ignore[arg-type]
    }
    _atomic_write_json(out / EXECUTION_NAME, execution)
    return {"status": "executed", "confirm_plan_sha256": actual_hash, **{k: execution[k] for k in ("run_id", "instance_id", "absence_reads", "evidence_dir")}}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, help="plan path; plan mode writes it, --execute reads it")
    parser.add_argument("--execute", action="store_true", help="execute an existing signed plan")
    parser.add_argument("--confirm-plan-sha256", help="exact sha256 printed by plan mode")
    parser.add_argument("--deadline-minutes", type=int, default=DEFAULT_DEADLINE_MINUTES)
    parser.add_argument("--max-dph", default=str(DEFAULT_MAX_DPH))
    parser.add_argument("--max-usd", default=str(DEFAULT_MAX_USD))
    parser.add_argument("--offer-query", default=DEFAULT_QUERY)
    parser.add_argument("--offer-limit", type=int, default=DEFAULT_OFFER_LIMIT)
    parser.add_argument("--azure-subscription-id")
    parser.add_argument("--azure-resource-group")
    parser.add_argument("--azure-vm-name")
    parser.add_argument("--azure-guard-worker-source")
    parser.add_argument("--az-path", type=Path, default=DEFAULT_AZ_PATH)
    parser.add_argument("--ssh-identity-file", type=Path)
    parser.add_argument("--ssh-public-key-file", type=Path, help="default: <identity>.pub; attached to the exact VM")
    parser.add_argument("--known-hosts-file", type=Path, default=DEFAULT_KNOWN_HOSTS)
    parser.add_argument("--artifacts-dir", type=Path, help="run output dir; default is the plan's directory")
    parser.add_argument("--endpoint-poll-seconds", type=float, default=POLL_INTERVAL_SECONDS)
    parser.add_argument("--no-nvidia-default-runtime", action="store_true", help="do not add the k3s default-runtime=nvidia drop-in before install")
    for name, value in DEFAULT_TIMEOUTS.items():
        flag = "--endpoint-wait-seconds" if name == "endpoint_wait" else f"--{name.replace('_', '-')}-{'seconds' if name in {'ssh_ready', 'scale_observe'} else 'timeout-seconds'}"
        parser.add_argument(flag, dest=f"timeout_{name}", type=int, default=value)
    parser.add_argument("--load-concurrency", type=int, default=DEFAULT_LOAD["CANARY_LOAD_CONCURRENCY"])
    parser.add_argument("--load-max-tokens", type=int, default=DEFAULT_LOAD["CANARY_LOAD_MAX_TOKENS"])
    parser.add_argument("--load-seconds", type=int, default=DEFAULT_LOAD["CANARY_LOAD_SECONDS"])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.execute:
            if args.plan is None:
                parser.error("--execute requires --plan")
            if args.ssh_identity_file is None:
                parser.error("--execute requires --ssh-identity-file")
            result = execute_plan(args)
        else:
            missing = [flag for flag, value in (
                ("--azure-subscription-id", args.azure_subscription_id),
                ("--azure-resource-group", args.azure_resource_group),
                ("--azure-vm-name", args.azure_vm_name),
            ) if not value]
            if missing:
                parser.error(f"plan mode requires {' '.join(missing)}")
            guard_worker = _require_file(DEFAULT_GUARD_WORKER if args.azure_guard_worker_source is None else Path(args.azure_guard_worker_source), "guard worker source")
            provider = VM_PROVIDER_FACTORY(SDK_CLIENT_FACTORY(api_key=os.environ.get("VAST_API_KEY")))
            envelope, path, sha256 = build_plan_document(
                provider=provider,
                query=args.offer_query,
                limit=args.offer_limit,
                deadline_minutes=args.deadline_minutes,
                max_dph=_parse_decimal(args.max_dph, "--max-dph"),
                max_usd=_parse_decimal(args.max_usd, "--max-usd"),
                subscription_id=args.azure_subscription_id,
                resource_group=args.azure_resource_group,
                vm_name=args.azure_vm_name,
                az_path=args.az_path,
                guard_worker=guard_worker,
                plan_path=args.plan,
                timeouts={name: getattr(args, f"timeout_{name}") for name in DEFAULT_TIMEOUTS},
                load={
                    "CANARY_LOAD_CONCURRENCY": args.load_concurrency,
                    "CANARY_LOAD_MAX_TOKENS": args.load_max_tokens,
                    "CANARY_LOAD_SECONDS": args.load_seconds,
                },
                nvidia_default_runtime=not args.no_nvidia_default_runtime,
                now=NOW_FACTORY(),
                nonce=NONCE_FACTORY(),
            )
            _atomic_write_json(path, envelope)
            result = {
                "status": "planned",
                "plan_path": str(path),
                "confirm_plan_sha256": sha256,
                "run_id": envelope["document"]["run_identity"]["run_id"],  # type: ignore[index]
                "offer": envelope["document"]["offer"],  # type: ignore[index]
                "spend_limits": envelope["document"]["spend_limits"],  # type: ignore[index]
            }
        print(json.dumps(result, sort_keys=True))
        return 0
    except (BenchmarkOrchestratorError, VastSdkError, AzureRunCommandTransportError, GuardClientError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
