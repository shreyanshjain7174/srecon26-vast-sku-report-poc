#!/usr/bin/env python3
"""Plan or execute one guarded single-host 8x4090 TP8 benchmark."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TypeVar

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from srecon26_poc.azure_run_command_transport import (
    AzureRunCommandGuardConfig,
    AzureRunCommandGuardTransport,
    AzureRunCommandTransportError,
)
from srecon26_poc.guard import GuardAttestation, validate_attestation
from srecon26_poc.guard_client import GuardClient, GuardClientError
from srecon26_poc.multigpu_contracts import (
    DISK_GIB,
    MAX_DEADLINE_MINUTES,
    MIN_DEADLINE_MINUTES,
    MODEL_ID,
    MODEL_REVISION,
    REQUIRED_TENSOR_PARALLEL,
    VLLM_IMAGE,
    MultiGpuPlan,
)
from srecon26_poc.single_host_multigpu import HEARTBEAT_FAILURE_BUDGET_SECONDS, SingleHostError, SingleHostLease
from srecon26_poc.types import RunIdentity
from srecon26_poc.vast_sdk_adapter import (
    SdkInstance,
    SdkLaunchContract,
    SdkOffer,
    VastSdkError,
    VastSdkProvider,
    create_vast_sdk_client,
)

PLAN_SCHEMA = "run-single-host-multigpu-benchmark/v1"
DEFAULT_QUERY = "verified=true rentable=true external=false gpu_name=RTX_4090 num_gpus=8"
DEFAULT_MATRIX = "smoke"
ALLOWED_PRECISIONS = ("bf16", "fp8", "fp8-kv")
DEFAULT_PRECISIONS = ("bf16",)
DEFAULT_MAX_DPH = Decimal("3.50")
DEFAULT_DEADLINE_MINUTES = 100
DEFAULT_MAX_USD = Decimal("6.00")
NETWORK_DOWNLOAD_ALLOWANCE_GB = Decimal("128")
NETWORK_UPLOAD_ALLOWANCE_GB = Decimal("2")
DEFAULT_OFFER_LIMIT = 25
DEFAULT_ENDPOINT_WAIT_SECONDS = 300.0
DEFAULT_ENDPOINT_POLL_SECONDS = 5.0
DEFAULT_SSH_USER = "root"
DEFAULT_AZ_PATH = Path(shutil.which("az") or "az")
DEFAULT_KNOWN_HOSTS = ROOT / "artifacts" / "live-known-hosts"
REMOTE_SCRIPT = ROOT / "scripts" / "remote_multigpu_benchmark.py"
DEFAULT_GUARD_WORKER = ROOT / "guard" / "guard_worker.py"
REMOTE_OUTPUT_PLACEHOLDER = "__REMOTE_OUTPUT_DIR__"
REMOTE_PRECISION_PLACEHOLDER = "__PRECISION__"
TEARDOWN_MARGIN_SECONDS = 900.0
HEARTBEAT_INTERVAL_SECONDS = 45.0
AZURE_GUARD_RPC_TIMEOUT_SECONDS = 150
AZURE_GUARD_CLI_TIMEOUT_SECONDS = 30
GUARD_HEARTBEAT_TIMEOUT_SECONDS = 600

NowFactory = Callable[[], datetime]
SleepFn = Callable[[float], None]
NonceFactory = Callable[[], str]
ProviderFactory = Callable[[Any], VastSdkProvider]
GuardFactory = Callable[[AzureRunCommandGuardConfig, str, str], "RunCommandGuard"]
CompletedRunner = Callable[..., subprocess.CompletedProcess[str]]
ResultT = TypeVar("ResultT")

NOW_FACTORY: NowFactory = lambda: datetime.now(UTC)
NONCE_FACTORY: NonceFactory = lambda: secrets.token_hex(8)
SDK_CLIENT_FACTORY = create_vast_sdk_client
SDK_PROVIDER_FACTORY: ProviderFactory = lambda client: VastSdkProvider(client)
SUBPROCESS_RUNNER: CompletedRunner = subprocess.run
SLEEP: SleepFn = time.sleep


class BenchmarkOrchestratorError(RuntimeError):
    pass


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _parse_decimal(raw: str, field: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation as error:
        raise BenchmarkOrchestratorError(f"{field} must be a decimal") from error
    if not value.is_finite() or value <= 0:
        raise BenchmarkOrchestratorError(f"{field} must be positive")
    return value


def _utc_stamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise BenchmarkOrchestratorError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_stamp(raw: str, field: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise BenchmarkOrchestratorError(f"{field} must be an ISO-8601 timestamp") from error
    if value.tzinfo is None or value.utcoffset() is None:
        raise BenchmarkOrchestratorError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    if path.exists():
        raise BenchmarkOrchestratorError(f"refusing to overwrite existing file {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    encoded = (_canonical_json(payload) + "\n").encode("utf-8")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise BenchmarkOrchestratorError(f"atomic write stalled for {path}")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _default_plan_path(run_id: str) -> Path:
    return ROOT / "artifacts" / "live-runs" / run_id / "single-host-multigpu-plan.json"


def _require_file(path: Path, field: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise BenchmarkOrchestratorError(f"{field} must exist: {resolved}")
    return resolved

def _require_executable(path: Path, field: str) -> Path:
    resolved = _require_file(path, field)
    if not os.access(resolved, os.X_OK):
        raise BenchmarkOrchestratorError(f"{field} must be executable: {resolved}")
    return resolved


def _resolved_guard_worker(path: str | None) -> Path:
    candidate = DEFAULT_GUARD_WORKER if path is None else Path(path)
    return _require_file(candidate, "guard worker source")


def _run_id_and_label(now: datetime, nonce: str) -> tuple[str, str]:
    if not nonce or "--nonce-" in nonce:
        raise BenchmarkOrchestratorError("nonce is invalid")
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"single-host-multigpu-{stamp}-{nonce[:8]}"
    return run_id, f"srecon26-mgpu-{stamp.lower()}-{nonce[:8]}--nonce-{nonce}"


def _offer_payload(offer: SdkOffer) -> dict[str, object]:
    return {
        "offer_id": offer.offer_id,
        "machine_id": offer.machine_id,
        "gpu_name": offer.gpu_name,
        "gpu_count": offer.num_gpus,
        "gpu_ram_mib": offer.gpu_ram_mib,
        "compute_capability": offer.compute_capability,
        "dph": _decimal_text(offer.dph_total),
        "reliability": _decimal_text(offer.reliability),
        "direct_port_count": offer.direct_port_count,
        "inet_down_mbps": _decimal_text(offer.inet_down_mbps),
        "inet_down_cost": _decimal_text(offer.inet_down_cost),
        "inet_up_cost": _decimal_text(offer.inet_up_cost),
        "label": offer.label,
    }


def _launch_payload(contract: SdkLaunchContract) -> dict[str, object]:
    return {
        "image": contract.image,
        "disk_gib": contract.disk_gib,
        "ssh": contract.ssh,
        "direct": contract.direct,
        "cancel_unavail": contract.cancel_unavail,
        "runtype": contract.runtype,
    }


def _remote_benchmark_payload(script_path: Path, *, matrix: str, precisions: Sequence[str], readiness_timeout_s: float, max_run_seconds: float, request_timeout_s: float, metrics_interval_s: float) -> dict[str, object]:
    return {
        "local_script_path": str(script_path),
        "script_sha256": _sha256_file(script_path),
        "matrix": matrix,
        "precisions": list(precisions),
        "argv_template": [
            "python3",
            "remote_multigpu_benchmark.py",
            "--output-dir",
            REMOTE_OUTPUT_PLACEHOLDER,
            "--matrix",
            matrix,
            "--precision",
            REMOTE_PRECISION_PLACEHOLDER,
            "--model-revision",
            MODEL_REVISION,
            "--vllm-image",
            VLLM_IMAGE,
            "--readiness-timeout-s",
            str(readiness_timeout_s),
            "--max-run-seconds",
            str(max_run_seconds),
            "--request-timeout-s",
            str(request_timeout_s),
            "--metrics-interval-s",
            str(metrics_interval_s),
        ],
        "readiness_timeout_s": readiness_timeout_s,
        "max_run_seconds": max_run_seconds,
        "request_timeout_s": request_timeout_s,
        "metrics_interval_s": metrics_interval_s,
    }


def _remote_root(label: str) -> str:
    return f"/var/tmp/srecon26-mgpu-{hashlib.sha256(label.encode('utf-8')).hexdigest()[:20]}"


def _validated_precisions(values: object) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)) or not values:
        raise BenchmarkOrchestratorError("precisions must be a non-empty list")
    if any(value not in ALLOWED_PRECISIONS for value in values) or len(set(values)) != len(values):
        raise BenchmarkOrchestratorError(f"precisions must be unique values from {ALLOWED_PRECISIONS}")
    return tuple(values)


def _spend_payload(plan: MultiGpuPlan, offer: SdkOffer, *, max_usd: Decimal) -> dict[str, object]:
    deadline_hours = Decimal(plan.deadline_minutes) / Decimal(60)
    network_allowance = (
        offer.inet_down_cost * NETWORK_DOWNLOAD_ALLOWANCE_GB
        + offer.inet_up_cost * NETWORK_UPLOAD_ALLOWANCE_GB
    )
    estimated_offer = plan.dph * deadline_hours + network_allowance
    max_allowed = plan.max_dph * deadline_hours + network_allowance
    if estimated_offer > max_usd:
        raise BenchmarkOrchestratorError(
            f"estimated spend {_decimal_text(estimated_offer)} exceeds --max-usd {_decimal_text(max_usd)}"
        )
    if estimated_offer > max_allowed:
        raise BenchmarkOrchestratorError("estimated spend exceeds the pinned max_dph bound")
    if max_allowed > max_usd:
        raise BenchmarkOrchestratorError("max_dph plus network allowance exceeds --max-usd")
    return {
        "deadline_hours": _decimal_text(deadline_hours),
        "offer_dph": _decimal_text(plan.dph),
        "max_dph": _decimal_text(plan.max_dph),
        "network_download_allowance_gb": _decimal_text(NETWORK_DOWNLOAD_ALLOWANCE_GB),
        "network_upload_allowance_gb": _decimal_text(NETWORK_UPLOAD_ALLOWANCE_GB),
        "network_allowance_usd": _decimal_text(network_allowance),
        "estimated_offer_spend_usd": _decimal_text(estimated_offer),
        "max_allowed_spend_usd": _decimal_text(max_allowed),
        "operator_max_usd": _decimal_text(max_usd),
    }


def _select_offer(provider: VastSdkProvider, *, query: str, limit: int, label: str, max_dph: Decimal, deadline_minutes: int, matrix: str) -> tuple[SdkOffer, MultiGpuPlan]:
    candidates: list[tuple[SdkOffer, MultiGpuPlan]] = []
    for offer in provider.search_offers(query=query, limit=limit, label=label):
        try:
            plan = MultiGpuPlan(
                offer_id=offer.offer_id,
                machine_id=offer.machine_id,
                gpu_name=offer.gpu_name,
                gpu_count=offer.num_gpus,
                gpu_ram_mib=offer.gpu_ram_mib,
                compute_capability=offer.compute_capability,
                dph=offer.dph_total,
                max_dph=max_dph,
                reliability=offer.reliability,
                direct_port_count=offer.direct_port_count,
                inet_down_mbps=offer.inet_down_mbps,
                tensor_parallel=REQUIRED_TENSOR_PARALLEL,
                max_model_len=4096,
                max_num_seqs=64,
                disk_gib=DISK_GIB,
                deadline_minutes=deadline_minutes,
                matrix_name=matrix,
            )
        except ValueError:
            continue
        candidates.append((offer, plan))
    if not candidates:
        raise BenchmarkOrchestratorError("no eligible 8x RTX 4090 offer satisfies the pinned contract")
    hours = Decimal(deadline_minutes) / Decimal(60)
    candidates.sort(key=lambda item: (
        item[0].dph_total * hours
        + item[0].inet_down_cost * NETWORK_DOWNLOAD_ALLOWANCE_GB
        + item[0].inet_up_cost * NETWORK_UPLOAD_ALLOWANCE_GB,
        -item[0].reliability,
        item[0].offer_id,
    ))
    return candidates[0]


def build_plan_document(
    *,
    provider: VastSdkProvider,
    query: str,
    limit: int,
    deadline_minutes: int,
    matrix: str,
    max_dph: Decimal,
    max_usd: Decimal,
    subscription_id: str,
    resource_group: str,
    vm_name: str,
    az_path: Path,
    guard_worker: Path,
    plan_path: Path | None,
    remote_script_path: Path,
    readiness_timeout_s: float,
    max_run_seconds: float,
    request_timeout_s: float,
    metrics_interval_s: float,
    remote_command_timeout_seconds: float,
    now: datetime,
    nonce: str,
    precisions: Sequence[str] = DEFAULT_PRECISIONS,
) -> tuple[dict[str, object], Path, str]:
    if not MIN_DEADLINE_MINUTES <= deadline_minutes <= MAX_DEADLINE_MINUTES:
        raise BenchmarkOrchestratorError(
            f"deadline_minutes must be in [{MIN_DEADLINE_MINUTES}, {MAX_DEADLINE_MINUTES}]"
        )
    precisions = _validated_precisions(precisions)
    arms = len(precisions)
    timing_values = {
        "readiness_timeout_s": readiness_timeout_s,
        "max_run_seconds": max_run_seconds,
        "request_timeout_s": request_timeout_s,
        "metrics_interval_s": metrics_interval_s,
        "remote_command_timeout_seconds": remote_command_timeout_seconds,
    }
    if any(not math.isfinite(value) or value <= 0 for value in timing_values.values()):
        raise BenchmarkOrchestratorError("remote benchmark timeouts and intervals must be finite and positive")
    if readiness_timeout_s > 1800:
        raise BenchmarkOrchestratorError("readiness timeout exceeds the remote benchmark maximum")
    remote_budget = arms * remote_command_timeout_seconds + TEARDOWN_MARGIN_SECONDS
    if remote_budget > deadline_minutes * 60:
        raise BenchmarkOrchestratorError("remote benchmark timeouts leave less than 15 minutes for teardown")
    if remote_command_timeout_seconds < readiness_timeout_s + max_run_seconds:
        raise BenchmarkOrchestratorError("remote command timeout is shorter than the benchmark budget")
    if remote_command_timeout_seconds > deadline_minutes * 60 - TEARDOWN_MARGIN_SECONDS:
        raise BenchmarkOrchestratorError("remote command timeout leaves less than 15 minutes for teardown")
    run_id, label = _run_id_and_label(now, nonce)
    remote_script = _require_file(remote_script_path, "remote benchmark script")
    resolved_az_path = _require_executable(az_path, "Azure CLI")
    offer, plan = _select_offer(
        provider,
        query=query,
        limit=limit,
        label=label,
        max_dph=max_dph,
        deadline_minutes=deadline_minutes,
        matrix=matrix,
    )
    launch = SdkLaunchContract(image=VLLM_IMAGE, disk_gib=DISK_GIB)
    launch.validate()
    hard_deadline = now.astimezone(UTC) + timedelta(minutes=deadline_minutes)
    document: dict[str, object] = {
        "schema": PLAN_SCHEMA,
        "run_identity": {
            "run_id": run_id,
            "nonce": nonce,
            "label": label,
            "created_at": _utc_stamp(now),
        },
        "offer": _offer_payload(offer),
        "multigpu_plan": plan.canonical_dict(),
        "multigpu_plan_sha256": plan.sha256(),
        "launch_contract": _launch_payload(launch),
        "guard_binding": {
            "subscription_id": subscription_id,
            "resource_group": resource_group,
            "vm_name": vm_name,
            "rpc_timeout_seconds": AZURE_GUARD_RPC_TIMEOUT_SECONDS,
            "cli_timeout_seconds": AZURE_GUARD_CLI_TIMEOUT_SECONDS,
            "heartbeat_timeout_seconds": GUARD_HEARTBEAT_TIMEOUT_SECONDS,
            "az_path": str(resolved_az_path),
            "az_sha256": _sha256_file(resolved_az_path),
            "worker_source_path": str(guard_worker),
            "worker_sha256": _sha256_file(guard_worker),
        },
        "remote_benchmark": {
            **_remote_benchmark_payload(
                remote_script,
                matrix=matrix,
                precisions=precisions,
                readiness_timeout_s=readiness_timeout_s,
                max_run_seconds=max_run_seconds,
                request_timeout_s=request_timeout_s,
                metrics_interval_s=metrics_interval_s,
            ),
            "remote_root": _remote_root(label),
            "remote_output_dir": f"{_remote_root(label)}/output",
            "remote_command_timeout_seconds": remote_command_timeout_seconds,
        },
        "deadline": {
            "deadline_minutes": deadline_minutes,
            "hard_deadline": _utc_stamp(hard_deadline),
        },
        "spend_limits": _spend_payload(plan, offer, max_usd=max_usd),
        "model_contract": {
            "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION,
            "vllm_image": VLLM_IMAGE,
            "tensor_parallel": REQUIRED_TENSOR_PARALLEL,
        },
    }
    document_sha256 = _sha256_text(_canonical_json(document))
    envelope = {"document": document, "document_sha256": document_sha256}
    target_path = _default_plan_path(run_id) if plan_path is None else plan_path.expanduser().resolve()
    return envelope, target_path, document_sha256


def _read_plan(path: Path) -> tuple[dict[str, object], str]:
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise BenchmarkOrchestratorError(f"plan file not found: {path}") from error
    except json.JSONDecodeError as error:
        raise BenchmarkOrchestratorError(f"plan file is not valid JSON: {path}") from error
    if not isinstance(envelope, dict):
        raise BenchmarkOrchestratorError("plan file envelope is invalid")
    document = envelope.get("document")
    recorded_hash = envelope.get("document_sha256")
    if not isinstance(document, dict) or not isinstance(recorded_hash, str):
        raise BenchmarkOrchestratorError("plan file is missing document or document_sha256")
    actual_hash = _sha256_text(_canonical_json(document))
    if recorded_hash != actual_hash:
        raise BenchmarkOrchestratorError("plan file content does not match its recorded sha256")
    return document, actual_hash


def _require_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise BenchmarkOrchestratorError(f"{field} must be an object")
    return value


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise BenchmarkOrchestratorError(f"{field} must be a non-empty string")
    return value


def _multigpu_plan_from_payload(payload: Mapping[str, object]) -> MultiGpuPlan:
    normalized = dict(payload)
    normalized.pop("matrix", None)
    normalized.pop("model_config", None)
    normalized.pop("model_params", None)
    normalized.pop("model_used_storage_bytes", None)
    for field in ("dph", "max_dph", "reliability", "inet_down_mbps"):
        normalized[field] = Decimal(_require_text(normalized.get(field), f"multigpu_plan.{field}"))
    return MultiGpuPlan(**normalized)


def _sdk_offer_from_plan(document: Mapping[str, object]) -> tuple[SdkOffer, MultiGpuPlan, SdkLaunchContract, dict[str, object], dict[str, object], dict[str, object]]:
    offer_payload = _require_mapping(document.get("offer"), "offer")
    run_identity = _require_mapping(document.get("run_identity"), "run_identity")
    label = _require_text(run_identity.get("label"), "run_identity.label")
    offer = SdkOffer(
        offer_id=int(offer_payload["offer_id"]),
        machine_id=int(offer_payload["machine_id"]),
        gpu_name=_require_text(offer_payload.get("gpu_name"), "offer.gpu_name"),
        num_gpus=int(offer_payload["gpu_count"]),
        gpu_ram_mib=int(offer_payload["gpu_ram_mib"]),
        compute_capability=_require_text(offer_payload.get("compute_capability"), "offer.compute_capability"),
        dph_total=Decimal(_require_text(offer_payload.get("dph"), "offer.dph")),
        reliability=Decimal(_require_text(offer_payload.get("reliability"), "offer.reliability")),
        direct_port_count=int(offer_payload["direct_port_count"]),
        inet_down_mbps=Decimal(_require_text(offer_payload.get("inet_down_mbps"), "offer.inet_down_mbps")),
        label=label,
        inet_down_cost=Decimal(_require_text(offer_payload.get("inet_down_cost"), "offer.inet_down_cost")),
        inet_up_cost=Decimal(_require_text(offer_payload.get("inet_up_cost"), "offer.inet_up_cost")),
    )
    multigpu_plan = _multigpu_plan_from_payload(_require_mapping(document.get("multigpu_plan"), "multigpu_plan"))
    launch_payload = _require_mapping(document.get("launch_contract"), "launch_contract")
    launch = SdkLaunchContract(
        image=_require_text(launch_payload.get("image"), "launch_contract.image"),
        disk_gib=int(launch_payload["disk_gib"]),
        ssh=bool(launch_payload["ssh"]),
        direct=bool(launch_payload["direct"]),
        cancel_unavail=bool(launch_payload["cancel_unavail"]),
        runtype=_require_text(launch_payload.get("runtype"), "launch_contract.runtype"),
    )
    guard_binding = dict(_require_mapping(document.get("guard_binding"), "guard_binding"))
    remote_benchmark = dict(_require_mapping(document.get("remote_benchmark"), "remote_benchmark"))
    deadline = dict(_require_mapping(document.get("deadline"), "deadline"))
    return offer, multigpu_plan, launch, guard_binding, remote_benchmark, deadline


def _compare_offer_fields(current: SdkOffer, planned: SdkOffer) -> None:
    if _offer_payload(current) != _offer_payload(planned):
        raise BenchmarkOrchestratorError("planned offer drifted from current provider offer")


def _normalized_host(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > 253 or text.startswith("-") or any(character.isspace() for character in text):
        raise BenchmarkOrchestratorError("instance published an invalid SSH host")
    try:
        ipaddress.ip_address(text)
    except ValueError:
        if not all(part and len(part) <= 63 and part.replace("-", "").isalnum() for part in text.rstrip(".").split(".")):
            raise BenchmarkOrchestratorError("instance published an invalid SSH host") from None
    return text


def _host_port(ports: Sequence[tuple[str, int]]) -> int | None:
    for container_port, host_port in ports:
        if container_port == "22/tcp":
            return host_port
    return None


def _ssh_endpoint(instance: SdkInstance) -> tuple[str, int, str]:
    direct_host = _normalized_host(instance.public_ipaddr)
    direct_port = _host_port(instance.ports)
    proxy_host = _normalized_host(instance.ssh_host)
    proxy_port = instance.ssh_port
    if direct_host is not None and direct_port is not None:
        return direct_host, direct_port, "direct_public_ipaddr_22_tcp_hostport"
    if proxy_host is not None and proxy_port is not None:
        return proxy_host, proxy_port, "proxy_ssh_host_ssh_port"
    raise BenchmarkOrchestratorError("instance does not publish a safe SSH endpoint")


def resolve_ssh_endpoint(
    *,
    provider: VastSdkProvider,
    expected: SdkInstance,
    wait_seconds: float,
    poll_seconds: float,
    now: NowFactory,
    sleep: SleepFn,
) -> tuple[str, int, str, SdkInstance]:
    deadline = now().astimezone(UTC) + timedelta(seconds=wait_seconds)
    while True:
        try:
            current = provider.get_instance(expected.instance_id)
        except VastSdkError:
            if now().astimezone(UTC) >= deadline:
                raise BenchmarkOrchestratorError("exact instance was unreadable before SSH deadline") from None
            sleep(poll_seconds)
            continue
        if current.label != expected.label:
            raise BenchmarkOrchestratorError("exact instance label changed while resolving SSH endpoint")
        if current.actual_status != "running":
            if now().astimezone(UTC) >= deadline:
                raise BenchmarkOrchestratorError("exact instance did not reach running before SSH deadline")
            sleep(poll_seconds)
            continue
        try:
            host, port, route = _ssh_endpoint(current)
            return host, port, route, current
        except BenchmarkOrchestratorError:
            if now().astimezone(UTC) >= deadline:
                raise
            sleep(poll_seconds)


def _run_subprocess(arguments: Sequence[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return SUBPROCESS_RUNNER(
            list(arguments),
            text=True,
            capture_output=True,
            check=False,
            shell=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise BenchmarkOrchestratorError(f"subprocess failed: {arguments[0]}") from error


def _ssh_base(identity_file: Path, known_hosts_file: Path, *, port: int, scp: bool = False) -> list[str]:
    return [
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={known_hosts_file}",
        "-i",
        str(identity_file),
        "-P" if scp else "-p",
        str(port),
    ]


def _ssh_target(user: str, host: str) -> str:
    return f"{user}@{host}"


def _ssh(identity_file: Path, known_hosts_file: Path, *, user: str, host: str, port: int, remote_argv: Sequence[str], timeout: float | None) -> subprocess.CompletedProcess[str]:
    arguments = ["ssh", *_ssh_base(identity_file, known_hosts_file, port=port), _ssh_target(user, host), *remote_argv]
    completed = _run_subprocess(arguments, timeout=timeout)
    if completed.returncode != 0:
        raise BenchmarkOrchestratorError(f"ssh command failed: {completed.stderr.strip() or completed.stdout.strip() or 'unknown error'}")
    return completed


def _scp_to_remote(identity_file: Path, known_hosts_file: Path, *, user: str, host: str, port: int, local_path: Path, remote_path: str, timeout: float | None) -> None:
    arguments = [
        "scp",
        *_ssh_base(identity_file, known_hosts_file, port=port, scp=True),
        str(local_path),
        f"{_ssh_target(user, host)}:{remote_path}",
    ]
    completed = _run_subprocess(arguments, timeout=timeout)
    if completed.returncode != 0:
        raise BenchmarkOrchestratorError(f"scp upload failed: {completed.stderr.strip() or completed.stdout.strip() or 'unknown error'}")


def _scp_from_remote(identity_file: Path, known_hosts_file: Path, *, user: str, host: str, port: int, remote_path: str, local_path: Path, timeout: float | None) -> None:
    if local_path.exists():
        raise BenchmarkOrchestratorError(f"refusing to overwrite existing artifact directory {local_path}")
    local_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = local_path.with_name(f".{local_path.name}.{secrets.token_hex(8)}.tmp")
    arguments = [
        "scp",
        *_ssh_base(identity_file, known_hosts_file, port=port, scp=True),
        "-r",
        f"{_ssh_target(user, host)}:{remote_path}",
        str(temporary),
    ]
    completed = _run_subprocess(arguments, timeout=timeout)
    if completed.returncode != 0:
        raise BenchmarkOrchestratorError(f"scp download failed: {completed.stderr.strip() or completed.stdout.strip() or 'unknown error'}")
    os.replace(temporary, local_path)


def _fetch_artifacts(
    identity_file: Path,
    known_hosts_file: Path,
    *,
    user: str,
    host: str,
    port: int,
    remote_path: str,
    local_path: Path,
) -> subprocess.CompletedProcess[str]:
    _scp_from_remote(
        identity_file,
        known_hosts_file,
        user=user,
        host=host,
        port=port,
        remote_path=remote_path,
        local_path=local_path,
        timeout=120,
    )
    return subprocess.CompletedProcess(["scp"], 0, stdout="", stderr="")


class RunCommandGuard:
    def __init__(self, config: AzureRunCommandGuardConfig, nonce: str, expected_script_hash: str) -> None:
        self.config = config.validated()
        self.transport = AzureRunCommandGuardTransport(self.config)
        self.client = GuardClient(self.transport, nonce=nonce)
        self.expected_script_hash = expected_script_hash

    def preflight(self) -> GuardAttestation:
        return self.client.preflight()

    def arm(self, identity: RunIdentity, hard_deadline: datetime) -> GuardAttestation:
        self.client.arm(identity, hard_deadline, heartbeat_timeout_seconds=self.config.heartbeat_timeout_seconds)
        attestation = self.client.preflight()
        validate_attestation(identity, attestation)
        if (
            attestation.nonce != self.client.nonce
            or attestation.heartbeat_timeout_seconds != self.config.heartbeat_timeout_seconds
            or attestation.script_hash != self.expected_script_hash
        ):
            raise BenchmarkOrchestratorError("Azure guard attestation did not bind nonce, timeout, and worker hash")
        return attestation

    def record_heartbeat(self, identity: RunIdentity, monotonic_ns: int) -> None:
        self.client.record_heartbeat(identity, monotonic_ns)

    def anchor(self, root_hash: str) -> str:
        return self.client.anchor(root_hash)


RUN_COMMAND_GUARD_FACTORY: GuardFactory = lambda config, nonce, script_hash: RunCommandGuard(config, nonce, script_hash)


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
    thread = threading.Thread(target=pump, name="multigpu-guard-heartbeat", daemon=True)
    thread.start()
    try:
        completed = action()
    finally:
        stop.set()
        thread.join()
    if failures:
        raise BenchmarkOrchestratorError("Azure guard heartbeat failed during remote benchmark") from failures[0]
    heartbeat()
    return completed


def execute_plan(args: argparse.Namespace) -> dict[str, object]:
    plan_document, actual_hash = _read_plan(args.plan.expanduser().resolve())
    if not args.confirm_plan_sha256:
        raise BenchmarkOrchestratorError("--confirm-plan-sha256 is required with --execute")
    if args.confirm_plan_sha256 != actual_hash:
        raise BenchmarkOrchestratorError("--confirm-plan-sha256 does not match the signed execution document")
    offer, multigpu_plan, launch, guard_binding, remote_benchmark, deadline = _sdk_offer_from_plan(plan_document)
    if multigpu_plan.sha256() != _require_text(plan_document.get("multigpu_plan_sha256"), "multigpu_plan_sha256"):
        raise BenchmarkOrchestratorError("multigpu plan sha256 does not match its canonical payload")
    remote_script_path = _require_file(Path(_require_text(remote_benchmark.get("local_script_path"), "remote_benchmark.local_script_path")), "remote benchmark script")
    if _sha256_file(remote_script_path) != _require_text(remote_benchmark.get("script_sha256"), "remote_benchmark.script_sha256"):
        raise BenchmarkOrchestratorError("remote benchmark script sha256 no longer matches the signed plan")
    hard_deadline = _parse_stamp(_require_text(deadline.get("hard_deadline"), "deadline.hard_deadline"), "deadline.hard_deadline")
    created_at = _parse_stamp(_require_text(_require_mapping(plan_document.get("run_identity"), "run_identity").get("created_at"), "run_identity.created_at"), "run_identity.created_at")
    run_id = _require_text(_require_mapping(plan_document.get("run_identity"), "run_identity").get("run_id"), "run_identity.run_id")
    nonce = _require_text(_require_mapping(plan_document.get("run_identity"), "run_identity").get("nonce"), "run_identity.nonce")
    label = _require_text(_require_mapping(plan_document.get("run_identity"), "run_identity").get("label"), "run_identity.label")
    worker_source = _require_file(
        Path(_require_text(guard_binding.get("worker_source_path"), "guard_binding.worker_source_path")),
        "guard worker source",
    )
    expected_worker_hash = _require_text(guard_binding.get("worker_sha256"), "guard_binding.worker_sha256")
    if _sha256_file(worker_source) != expected_worker_hash:
        raise BenchmarkOrchestratorError("guard worker source sha256 no longer matches the signed plan")
    signed_az_path = _require_executable(
        Path(_require_text(guard_binding.get("az_path"), "guard_binding.az_path")),
        "signed Azure CLI",
    )
    if _sha256_file(signed_az_path) != _require_text(guard_binding.get("az_sha256"), "guard_binding.az_sha256"):
        raise BenchmarkOrchestratorError("Azure CLI sha256 no longer matches the signed plan")
    if _require_executable(args.az_path, "Azure CLI") != signed_az_path:
        raise BenchmarkOrchestratorError("--az-path does not match the signed plan")
    for key, expected in (
        ("rpc_timeout_seconds", AZURE_GUARD_RPC_TIMEOUT_SECONDS),
        ("cli_timeout_seconds", AZURE_GUARD_CLI_TIMEOUT_SECONDS),
        ("heartbeat_timeout_seconds", GUARD_HEARTBEAT_TIMEOUT_SECONDS),
    ):
        if type(guard_binding.get(key)) is not int or guard_binding[key] != expected:
            raise BenchmarkOrchestratorError(f"guard_binding.{key} differs from the signed safety contract")
    remaining_seconds = (hard_deadline - NOW_FACTORY().astimezone(UTC)).total_seconds()
    precisions = _validated_precisions(remote_benchmark.get("precisions", list(DEFAULT_PRECISIONS)))
    benchmark_seconds = len(precisions) * float(remote_benchmark["remote_command_timeout_seconds"])
    if remaining_seconds < benchmark_seconds + TEARDOWN_MARGIN_SECONDS:
        raise BenchmarkOrchestratorError("signed deadline no longer leaves enough time for benchmark and teardown")

    client = SDK_CLIENT_FACTORY(api_key=os.environ.get("VAST_API_KEY"))
    provider = SDK_PROVIDER_FACTORY(client)
    current_offer = provider.get_offer(offer.offer_id, offer.machine_id, offer.label)
    _compare_offer_fields(current_offer, offer)

    identity_file = _require_file(args.ssh_identity_file, "ssh identity file")
    known_hosts_file = args.known_hosts_file.expanduser().resolve()
    known_hosts_file.parent.mkdir(parents=True, exist_ok=True)
    guard_config = AzureRunCommandGuardConfig(
        subscription_id=_require_text(guard_binding.get("subscription_id"), "guard_binding.subscription_id"),
        resource_group=_require_text(guard_binding.get("resource_group"), "guard_binding.resource_group"),
        vm_name=_require_text(guard_binding.get("vm_name"), "guard_binding.vm_name"),
        az_path=signed_az_path,
        timeout_seconds=AZURE_GUARD_RPC_TIMEOUT_SECONDS,
        cli_timeout_seconds=AZURE_GUARD_CLI_TIMEOUT_SECONDS,
        heartbeat_timeout_seconds=GUARD_HEARTBEAT_TIMEOUT_SECONDS,
    )
    guard = RUN_COMMAND_GUARD_FACTORY(guard_config, nonce, expected_worker_hash)
    remote_root = _require_text(remote_benchmark.get("remote_root"), "remote_benchmark.remote_root")
    remote_output_dir = _require_text(remote_benchmark.get("remote_output_dir"), "remote_benchmark.remote_output_dir")
    artifacts_dir = (args.artifacts_dir.expanduser().resolve() if args.artifacts_dir else args.plan.expanduser().resolve().parent / "remote-artifacts")
    execution_evidence = args.plan.expanduser().resolve().parent / "single-host-multigpu-execution.json"
    failure_evidence = args.plan.expanduser().resolve().parent / "single-host-multigpu-failure.json"
    heartbeat_identity = RunIdentity(run_id, label, created_at)
    stage = "create-or-verify"

    def heartbeat() -> None:
        guard.record_heartbeat(heartbeat_identity, time.monotonic_ns())

    def workload(instance: SdkInstance) -> None:
        nonlocal stage
        stage = "resolve-ssh"
        host, port, route, exact_instance = resolve_ssh_endpoint(
            provider=provider,
            expected=instance,
            wait_seconds=args.endpoint_wait_seconds,
            poll_seconds=args.endpoint_poll_seconds,
            now=NOW_FACTORY,
            sleep=SLEEP,
        )
        remote_script_target = f"{remote_root}/remote_multigpu_benchmark.py"

        def arm_command(precision: str) -> list[str]:
            return [
                args.remote_python,
                remote_script_target,
                "--output-dir",
                f"{remote_output_dir}/{precision}",
                "--matrix",
                _require_text(remote_benchmark.get("matrix"), "remote_benchmark.matrix"),
                "--precision",
                precision,
                "--model-revision",
                _require_text(plan_document["model_contract"].get("model_revision"), "model_contract.model_revision"),  # type: ignore[index]
                "--vllm-image",
                _require_text(plan_document["model_contract"].get("vllm_image"), "model_contract.vllm_image"),  # type: ignore[index]
                "--readiness-timeout-s",
                str(remote_benchmark["readiness_timeout_s"]),
                "--max-run-seconds",
                str(remote_benchmark["max_run_seconds"]),
                "--request-timeout-s",
                str(remote_benchmark["request_timeout_s"]),
                "--metrics-interval-s",
                str(remote_benchmark["metrics_interval_s"]),
            ]

        run_error: BaseException | None = None
        arm_results: dict[str, str] = {}
        try:
            stage = "prepare-remote"
            _ssh(identity_file, known_hosts_file, user=args.ssh_user, host=host, port=port, remote_argv=["mkdir", "-p", remote_output_dir], timeout=60)
            stage = "upload-script"
            _scp_to_remote(identity_file, known_hosts_file, user=args.ssh_user, host=host, port=port, local_path=remote_script_path, remote_path=remote_script_target, timeout=60)
            stage = "remote-benchmark"
            first_arm_error: BaseException | None = None
            for precision in precisions:
                try:
                    _ssh(
                        identity_file,
                        known_hosts_file,
                        user=args.ssh_user,
                        host=host,
                        port=port,
                        remote_argv=arm_command(precision),
                        timeout=float(remote_benchmark["remote_command_timeout_seconds"]),
                    )
                    arm_results[precision] = "completed"
                except BenchmarkOrchestratorError as arm_error:
                    arm_results[precision] = "failed"
                    first_arm_error = first_arm_error or arm_error
            if first_arm_error is not None and "completed" not in arm_results.values():
                raise first_arm_error
        except BaseException as error:
            run_error = error
            remote_error_stage = stage
        try:
            stage = "fetch-artifacts"
            _fetch_artifacts(
                identity_file,
                known_hosts_file,
                user=args.ssh_user,
                host=host,
                port=port,
                remote_path=remote_output_dir,
                local_path=artifacts_dir,
            )
        except BaseException as fetch_error:
            if run_error is not None:
                stage = remote_error_stage
                raise BenchmarkOrchestratorError("remote benchmark failed and artifacts could not be fetched") from fetch_error
            raise
        if run_error is not None:
            stage = remote_error_stage
            raise run_error
        stage = "write-evidence"
        _atomic_write_json(
            execution_evidence,
            {
                "plan_sha256": actual_hash,
                "instance_id": exact_instance.instance_id,
                "machine_id": exact_instance.machine_id,
                "ssh_route": route,
                "ssh_host": host,
                "ssh_port": port,
                "artifacts_dir": str(artifacts_dir),
                "run_id": run_id,
                "arms": arm_results,
            },
        )

    lease = SingleHostLease(
        provider=provider,
        offer=current_offer,
        plan=multigpu_plan,
        launch=launch,
        guard=guard,
        run_id=run_id,
        nonce=nonce,
        hard_deadline=hard_deadline,
        now=NOW_FACTORY,
        sleep=SLEEP,
    )
    try:
        result = lease.run(
            lambda instance: _run_with_heartbeats(lambda: workload(instance), heartbeat),
            heartbeat=heartbeat,
        )
    except SingleHostError as error:
        chain: list[str] = []
        current: BaseException | None = error
        while current is not None and len(chain) < 8:
            chain.append(f"{type(current).__name__}: {current}"[:500])
            current = current.__cause__
        _atomic_write_json(failure_evidence, {
            "run_id": run_id,
            "plan_sha256": actual_hash,
            "stage": stage,
            "error_type": type(error.__cause__).__name__ if error.__cause__ is not None else type(error).__name__,
            "error_chain": chain,
        })
        raise BenchmarkOrchestratorError(str(error)) from error
    return {
        "status": "executed",
        "plan_path": str(args.plan.expanduser().resolve()),
        "confirm_plan_sha256": actual_hash,
        "run_id": run_id,
        "instance_id": result.instance.instance_id,
        "absence_reads": result.absence_reads,
        "artifacts_dir": str(artifacts_dir),
        "created_at": _utc_stamp(created_at),
        "label": label,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, help="plan path; default writes a generated path, execute reads this file")
    parser.add_argument("--execute", action="store_true", help="execute an existing signed plan")
    parser.add_argument("--confirm-plan-sha256", help="exact sha256 printed by dry-run plan output")
    parser.add_argument("--matrix", default=DEFAULT_MATRIX, choices=("smoke", "full"))
    parser.add_argument("--precisions", default=",".join(DEFAULT_PRECISIONS), help=f"comma list from {','.join(ALLOWED_PRECISIONS)}; one serve arm each")
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
    parser.add_argument("--ssh-user", default=DEFAULT_SSH_USER)
    parser.add_argument("--known-hosts-file", type=Path, default=DEFAULT_KNOWN_HOSTS)
    parser.add_argument("--artifacts-dir", type=Path)
    parser.add_argument("--endpoint-wait-seconds", type=float, default=DEFAULT_ENDPOINT_WAIT_SECONDS)
    parser.add_argument("--endpoint-poll-seconds", type=float, default=DEFAULT_ENDPOINT_POLL_SECONDS)
    parser.add_argument("--remote-python", default="python3")
    parser.add_argument("--remote-command-timeout-seconds", type=float, default=4860.0)
    parser.add_argument("--readiness-timeout-s", type=float, default=1800.0)
    parser.add_argument("--max-run-seconds", type=float, default=3000.0)
    parser.add_argument("--request-timeout-s", type=float, default=600.0)
    parser.add_argument("--metrics-interval-s", type=float, default=1.0)
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
            required = {
                "--azure-subscription-id": args.azure_subscription_id,
                "--azure-resource-group": args.azure_resource_group,
                "--azure-vm-name": args.azure_vm_name,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                parser.error(f"plan mode requires {' '.join(missing)}")
            max_dph = _parse_decimal(args.max_dph, "--max-dph")
            max_usd = _parse_decimal(args.max_usd, "--max-usd")
            client = SDK_CLIENT_FACTORY(api_key=os.environ.get("VAST_API_KEY"))
            provider = SDK_PROVIDER_FACTORY(client)
            envelope, path, sha256 = build_plan_document(
                provider=provider,
                query=args.offer_query,
                limit=args.offer_limit,
                deadline_minutes=args.deadline_minutes,
                matrix=args.matrix,
                max_dph=max_dph,
                max_usd=max_usd,
                subscription_id=args.azure_subscription_id,
                resource_group=args.azure_resource_group,
                vm_name=args.azure_vm_name,
                az_path=args.az_path,
                guard_worker=_resolved_guard_worker(args.azure_guard_worker_source),
                plan_path=args.plan,
                remote_script_path=REMOTE_SCRIPT,
                readiness_timeout_s=args.readiness_timeout_s,
                max_run_seconds=args.max_run_seconds,
                request_timeout_s=args.request_timeout_s,
                metrics_interval_s=args.metrics_interval_s,
                remote_command_timeout_seconds=args.remote_command_timeout_seconds,
                now=NOW_FACTORY(),
                nonce=NONCE_FACTORY(),
                precisions=tuple(item.strip() for item in args.precisions.split(",")),
            )
            _atomic_write_json(path, envelope)
            result = {
                "status": "planned",
                "plan_path": str(path),
                "confirm_plan_sha256": sha256,
                "run_id": envelope["document"]["run_identity"]["run_id"],
            }
        print(json.dumps(result, sort_keys=True))
        return 0
    except (BenchmarkOrchestratorError, VastSdkError, AzureRunCommandTransportError, GuardClientError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())