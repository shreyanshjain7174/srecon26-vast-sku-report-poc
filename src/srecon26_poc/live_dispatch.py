"""Fail-closed paid canary dispatcher.

This module contains the only lifecycle that may call ``create instance``.  It
has no default remote executor, no credential loading, and no fallback offer or
image.  Callers must inject the independently-attested guard, exact-target
report adapter, and remote workload implementation.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Mapping, Protocol

from .budget import BudgetWriteFailure, ExposureLedger
from .canary import VLLM_IMAGE_DIGEST, CanarySnapshot, KvmFacts, evaluate_canary_readiness, evaluate_kvm_capabilities
from .contracts import InstanceContract, OfferContract, ProbeOutcome, classify_fault
from .guard import Guard, GuardAttestation, validate_attestation
from .journal import RunJournal
from .provider import AmbiguousCreate
from .reporting import FaultRecord, ReportGate, ReportReceipt
from .types import FaultClass, RunIdentity, RunState
from .vast_provider import VastCliProvider, VastLaunchContract, VastProviderError


REAL_GPU_PROVENANCE = "real-gpu"
LIMITATION_PROVENANCE = "live-limitation"
MAX_STAGE_RESERVE = Decimal("1.00")
PROJECT_CAP = Decimal("5.00")
REPORT_MARGIN = timedelta(seconds=420)  # report 180s + teardown 180s + absence 60s
MAX_GATE_AGE = timedelta(minutes=5)
MAX_STAGE_RUNTIME = timedelta(minutes=45)
INFERENCE_SMOKE_RESERVATION_CAP = Decimal("0.75")
INFERENCE_SMOKE_EXCLUDED_MACHINE_IDS = frozenset({99239, 17545, 150513, 147086})
APPROVED_GPU_PROFILES = frozenset(
    {
        ("RTX 3090", 24576, "8.6"),
        ("RTX 4090", 24564, "8.9"),
        # Capacity fallback for the same small-model metric-path PoC.  This is
        # never presented as 3090 evidence; the frozen offer and manifest retain
        # the observed SKU verbatim.
        ("RTX 4000Ada", 20475, "8.9"),
    }
)
# Signed-source pins for the one-time distinct-machine recovery attempt.  The
# ledger must name exactly these paid smoke runs, and each manifest must still
# hash to the independently submitted guard root.  This avoids trusting a
# mutable, self-consistent local checksum bundle as machine history.
AUDITED_HISTORICAL_SMOKE_ANCHORS: Mapping[str, tuple[str, int]] = {
    "gpu-smoke-smoke20260923060249": ("1a5615c7a1f7df8d8a99e7920de8289a23bde233aa9e4f3cc6f4c79886f5cf3f", 99239),
    "gpu-smoke-smoke320260923063827": ("cc16a1ea8f4687fc19726c03727375071d1fbea29495c779f3dd060053e39d9f", 17545),
    "gpu-smoke-smoke420260923065604": ("c4acaadbd36097d806a087a687fb110d856c401ab572d9720a342191d96bb7df", 150513),
    "gpu-smoke-smoke520260923072321": ("5344f5a8d6a61a504516d66b360062bf02b815e97670a98b8795300d9518d8bc", 150513),
}
FROZEN_MODEL_ID = "Qwen/Qwen2.5-1.5B-Instruct"
FROZEN_MODEL_REVISION = "989aa7980e4cf806f80c7fef2b1adb7bc71aa306"


class LiveDispatchError(RuntimeError):
    """A paid lifecycle cannot safely continue."""


class _Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic_ns(self) -> int: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


class RemoteWorkload(Protocol):
    """Execution boundary for SSH/KVM/k3s work.

    The dispatcher deliberately does not know how to connect to a rental.  A
    production executor owns SSH/tunnels and calls ``heartbeat`` while it is
    working; tests inject a fake without network access.
    """

    def run(
        self,
        *,
        stage: str,
        workload: "WorkloadContract",
        instance: InstanceContract,
        run_directory: Path,
        hard_deadline: datetime,
        heartbeat: Callable[[], None],
    ) -> "LiveEvidence": ...


@dataclass(frozen=True, slots=True)
class WorkloadContract:
    """Model and image values frozen for this short, bounded PoC."""

    model_id: str
    model_revision: str
    vllm_image_digest: str

    def validate(self) -> None:
        if self.model_id != FROZEN_MODEL_ID:
            raise LiveDispatchError("model id differs from the frozen canary contract")
        if self.model_revision != FROZEN_MODEL_REVISION:
            raise LiveDispatchError("model revision differs from the frozen canary contract")
        if self.vllm_image_digest != VLLM_IMAGE_DIGEST:
            raise LiveDispatchError("vLLM image digest differs from the frozen canary contract")

    def to_json(self) -> dict[str, str]:
        self.validate()
        return {
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "vllm_image_digest": self.vllm_image_digest,
        }


@dataclass(frozen=True, slots=True)
class GateArtifactPaths:
    """Explicit, current, outside-repository gate artifacts for one run."""

    phase1_verification: Path
    semgrep: Path
    provider_preflight: Path
    guard_attestation: Path
    report_fixture: Path
    smoke_manifest: Path | None = None


@dataclass(frozen=True, slots=True)
class InferenceAttemptHistory:
    """Operator-supplied machine identity when a prior manifest is unavailable."""

    run_id: str
    machine_id: int

    def valid(self) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9_-]{8,128}", self.run_id)) and not isinstance(self.machine_id, bool) and isinstance(self.machine_id, int) and self.machine_id > 0


@dataclass(frozen=True, slots=True)
class ProviderFaultEvidence:
    """A resolved, attributable fault emitted by the injected remote probe."""

    category: str
    description: str
    artifact: Path

    def valid(self) -> bool:
        return self.category in {"contract", "gpu", "cuda", "image", "host"} and bool(self.description) and self.artifact.is_file()


@dataclass(frozen=True, slots=True)
class InferenceMeasurementEvidence:
    """A remote vLLM response measurement, distinct from host/GPU probes."""

    artifact: Path

    def blockers_for(self, workload: WorkloadContract, *, now: datetime, evidence_files: tuple[Path, ...], run_id: str, instance: InstanceContract) -> tuple[str, ...]:
        blockers: list[str] = []
        try:
            artifact = self.artifact.resolve(strict=True)
            declared = {path.resolve(strict=True) for path in evidence_files if path.is_file()}
            if artifact not in declared:
                blockers.append("direct inference measurement is not a declared evidence file")
            payload = _json(artifact)
        except (OSError, LiveDispatchError):
            return ("missing readable direct inference measurement artifact",)
        if payload.get("schema") != "srecon26-direct-inference-measurement/v1":
            blockers.append("direct inference measurement schema is invalid")
        if payload.get("source") != "RemoteWorkload.direct_inference/v1":
            blockers.append("direct inference measurement source is invalid")
        if payload.get("model_id") != workload.model_id or payload.get("model_revision") != workload.model_revision or payload.get("vllm_image_digest") != workload.vllm_image_digest:
            blockers.append("direct inference measurement is not bound to the frozen workload contract")
        if payload.get("run_id") != run_id or payload.get("instance_id") != instance.instance_id or payload.get("label") != instance.label:
            blockers.append("direct inference measurement is not bound to the exact created instance")
        request = payload.get("request")
        timing = payload.get("timing")
        if not isinstance(request, Mapping) or request.get("succeeded") is not True or not isinstance(request.get("output_tokens"), int) or isinstance(request.get("output_tokens"), bool) or request["output_tokens"] <= 0:
            blockers.append("direct inference measurement lacks a successful non-empty response")
        if not isinstance(timing, Mapping):
            blockers.append("direct inference measurement lacks timing")
        else:
            try:
                ttft = Decimal(str(timing.get("ttft_seconds")))
                latency = Decimal(str(timing.get("latency_seconds")))
                if not ttft.is_finite() or not latency.is_finite() or ttft < 0 or latency < ttft:
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                blockers.append("direct inference measurement timing is invalid")
        observed_at = payload.get("observed_at")
        try:
            observed = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
            if observed.tzinfo is None or observed.astimezone(UTC) > now.astimezone(UTC):
                raise ValueError
        except ValueError:
            blockers.append("direct inference measurement timestamp is invalid")
        return tuple(dict.fromkeys(blockers))


@dataclass(frozen=True, slots=True)
class LiveEvidence:
    """Facts returned by a remote executor, never inferred from marketplace data."""

    gpu_identity: str | None
    cuda_version: str | None
    kvm_facts: KvmFacts | None
    model_id: str | None = None
    model_revision: str | None = None
    vllm_image_digest: str | None = None
    snapshot: CanarySnapshot | None = None
    resource_metrics_api: bool = False
    custom_metrics_api: bool = False
    hpa_observed: bool = False
    events_captured: bool = False
    timing_captured: bool = False
    probe_outcome: ProbeOutcome = ProbeOutcome.PASS
    provider_fault: ProviderFaultEvidence | None = None
    evidence_files: tuple[Path, ...] = ()
    inference_measurement: InferenceMeasurementEvidence | None = None

    def complete_for(self, stage: str, *, now: datetime, workload: WorkloadContract, run_id: str | None = None, instance: InstanceContract | None = None) -> tuple[bool, tuple[str, ...]]:
        blockers: list[str] = []
        if not self.gpu_identity:
            blockers.append("missing directly observed GPU identity")
        if not self.cuda_version:
            blockers.append("missing directly observed CUDA version")
        if self.model_id != workload.model_id:
            blockers.append("observed model id differs from frozen workload contract")
        if self.model_revision != workload.model_revision:
            blockers.append("observed model revision differs from frozen workload contract")
        if self.vllm_image_digest != workload.vllm_image_digest:
            blockers.append("observed vLLM image digest differs from frozen workload contract")
        if self.kvm_facts is None:
            blockers.append("missing KVM capability probe")
        else:
            decision = evaluate_kvm_capabilities(self.kvm_facts, now=now)
            blockers.extend(decision.blockers)
        if not self.evidence_files or any(not path.is_file() for path in self.evidence_files):
            blockers.append("missing remote evidence files")
        if stage == "inference-smoke":
            if self.inference_measurement is None:
                blockers.append("missing actual direct inference measurement evidence")
            elif run_id is None or instance is None:
                blockers.append("direct inference measurement has no exact run and instance binding")
            else:
                blockers.extend(self.inference_measurement.blockers_for(workload, now=now, evidence_files=self.evidence_files, run_id=run_id, instance=instance))
        if stage == "metric-path":
            if self.snapshot is None:
                blockers.append("missing metric-path readiness snapshot")
            else:
                readiness = evaluate_canary_readiness(self.snapshot, now=now)
                blockers.extend(readiness.blockers)
                if not readiness.measured_request_succeeded:
                    blockers.append("measured vLLM request did not complete")
            for name, present in (
                ("resource metrics API", self.resource_metrics_api),
                ("custom metrics API", self.custom_metrics_api),
                ("HPA state", self.hpa_observed),
                ("Kubernetes events", self.events_captured),
                ("request timing", self.timing_captured),
            ):
                if not present:
                    blockers.append(f"missing {name} evidence")
        return not blockers, tuple(dict.fromkeys(blockers))


@dataclass(frozen=True, slots=True)
class LiveCanaryRequest:
    stage: str
    run_id: str
    nonce: str
    label: str
    offer_id: int
    reserve: Decimal
    hard_deadline: datetime
    gates: GateArtifactPaths
    launch: VastLaunchContract
    workload: WorkloadContract
    budget_category: str | None = None
    inference_history: tuple[InferenceAttemptHistory, ...] = ()

    def validate(self, *, now: datetime) -> tuple[str, ...]:
        failures: list[str] = []
        if self.stage not in {"gpu-smoke", "inference-smoke", "metric-path"}:
            failures.append("stage must be gpu-smoke, inference-smoke, or metric-path")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", self.run_id):
            failures.append("run id must be 8-128 URL-safe characters")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", self.nonce):
            failures.append("nonce must be 8-128 URL-safe characters")
        if not self.label.endswith(f"--nonce-{self.nonce}") or self.label.count("--nonce-") != 1:
            failures.append("label must bind exactly this nonce")
        if not isinstance(self.reserve, Decimal) or not self.reserve.is_finite() or not Decimal("0") < self.reserve <= MAX_STAGE_RESERVE:
            failures.append("reserve must be a positive Decimal no greater than 1.00")
        if self.stage == "inference-smoke":
            if self.reserve > INFERENCE_SMOKE_RESERVATION_CAP:
                failures.append("direct inference smoke reserve must be no greater than 0.75")
            if self.budget_category != "gpu-inference-smoke":
                failures.append("inference-smoke requires the gpu-inference-smoke budget category")
            if len({item.run_id for item in self.inference_history}) != len(self.inference_history) or any(not item.valid() for item in self.inference_history):
                failures.append("inference history must contain unique valid run and machine identities")
            if any(item.run_id == self.run_id for item in self.inference_history):
                failures.append("inference history cannot contain the current run")
            if len({item.machine_id for item in self.inference_history}) != len(self.inference_history):
                failures.append("inference history machine identities must be distinct")
        elif self.inference_history:
            failures.append("inference history is restricted to inference-smoke")
        elif self.budget_category is not None:
            if self.stage != "gpu-smoke" or self.budget_category not in {"gpu-smoke-retry", "gpu-smoke-distinct-machine"}:
                failures.append("budget category override is restricted to an audited gpu-smoke entitlement")
            elif self.budget_category == "gpu-smoke-retry" and self.reserve > Decimal("0.90"):
                failures.append("audited gpu-smoke retry reserve must be no greater than 0.90")
            elif self.budget_category == "gpu-smoke-distinct-machine" and self.reserve > Decimal("0.25"):
                failures.append("audited distinct-machine smoke reserve must be no greater than 0.25")
        if self.hard_deadline.tzinfo is None or self.hard_deadline.astimezone(UTC) <= now:
            failures.append("hard deadline must be a future timezone-aware timestamp")
        elif self.hard_deadline.astimezone(UTC) - now > MAX_STAGE_RUNTIME:
            failures.append("hard deadline exceeds the 45-minute paid-stage runtime cap")
        if self.report_start_by <= now:
            failures.append("hard deadline leaves no immutable report/teardown margin")
        try:
            self.launch.validate()
        except VastProviderError as error:
            failures.append(str(error))
        try:
            self.workload.validate()
        except LiveDispatchError as error:
            failures.append(str(error))
        return tuple(failures)

    @property
    def report_start_by(self) -> datetime:
        return self.hard_deadline.astimezone(UTC) - REPORT_MARGIN


@dataclass(frozen=True, slots=True)
class LiveDispatchResult:
    status: str
    limitation: str | None
    manifest_path: Path
    provider_create_calls: int
    provider_destroy_calls: int
    absence_reads: int
    provenance: str
    real_gpu_claim: bool


def _stamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise LiveDispatchError("timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LiveDispatchError(f"invalid gate artifact: {path}") from error
    if not isinstance(value, Mapping):
        raise LiveDispatchError(f"gate artifact is not a JSON object: {path}")
    return value


def _fresh(path: Path, *, now: datetime) -> bool:
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return False
    return timedelta(0) <= now - modified <= MAX_GATE_AGE


def _artifact_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _to_decimal(value: object, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise LiveDispatchError(f"invalid {field} in provider preflight") from error
    if not parsed.is_finite():
        raise LiveDispatchError(f"invalid {field} in provider preflight")
    return parsed


def _to_int(value: object, field: str) -> int:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise LiveDispatchError(f"invalid {field} in provider preflight") from error
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        raise LiveDispatchError(f"invalid {field} in provider preflight")
    return int(parsed)


def _frozen_offer(payload: Mapping[str, object], *, offer_id: int, label: str) -> OfferContract:
    offers = payload.get("offers")
    if not isinstance(offers, list):
        raise LiveDispatchError("provider preflight lacks offer list")
    matches = [item for item in offers if isinstance(item, Mapping) and _to_int(item.get("offer_id"), "offer id") == offer_id]
    if len(matches) != 1:
        raise LiveDispatchError("provider preflight lacks one exact offer contract")
    record = matches[0]
    if record.get("vms_enabled") is not True:
        raise LiveDispatchError("frozen offer is not KVM/vms_enabled")
    offer = OfferContract(
        offer_id=offer_id,
        gpu_name=str(record.get("gpu_name", "")),
        num_gpus=_to_int(record.get("num_gpus"), "num_gpus"),
        gpu_ram_mib=_to_int(record.get("gpu_ram_mib"), "gpu_ram_mib"),
        compute_capability=str(record.get("compute_capability", "")),
        machine_id=_to_int(record.get("machine_id"), "machine_id"),
        dph_total=_to_decimal(record.get("dph_total"), "dph_total"),
        label=label,
    )
    if offer.num_gpus != 1 or (offer.gpu_name, offer.gpu_ram_mib, offer.compute_capability) not in APPROVED_GPU_PROFILES:
        raise LiveDispatchError("frozen offer is not an exact approved single-GPU PoC contract")
    return offer


def _phase1_passed(path: Path) -> bool:
    try:
        contents = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return bool(re.search(r"(?m)^status:\s*passed\s*$", contents))


def _semgrep_passed(payload: Mapping[str, object]) -> bool:
    if payload.get("passed") is True:
        return True
    results, errors = payload.get("results"), payload.get("errors")
    return isinstance(results, list) and not results and isinstance(errors, list) and not errors


def _report_fixture_passed(payload: Mapping[str, object]) -> bool:
    receipt = payload.get("receipt")
    return payload.get("provider_requests") == 0 and isinstance(receipt, Mapping) and receipt.get("status") == "SUBMITTED"


def _guard_artifact_passed(payload: Mapping[str, object], request: LiveCanaryRequest) -> bool:
    deadline = _stamp(request.hard_deadline)
    return (
        payload.get("status") in {"ARMED", "ACTIVE"}
        and payload.get("nonce") == request.nonce
        and payload.get("label") == request.label
        and payload.get("hard_deadline") == deadline
        and isinstance(payload.get("host_identity"), str)
        and payload.get("host_identity") not in {"", "localhost", "127.0.0.1", "::1"}
        and isinstance(payload.get("script_hash"), str)
        and bool(payload.get("script_hash"))
    )


def _smoke_finalized(payload: Mapping[str, object]) -> bool:
    return (
        payload.get("stage") == "gpu-smoke"
        and payload.get("status") == "COMPLETED"
        and payload.get("provenance") == REAL_GPU_PROVENANCE
        and payload.get("real_gpu_claim") is True
        and payload.get("absence_reads") == 3
    )


def validate_live_gates(request: LiveCanaryRequest, *, now: datetime) -> tuple[tuple[str, ...], OfferContract | None, Mapping[str, str]]:
    """Validate every file gate before even a read-only provider contract fetch."""

    failures = list(request.validate(now=now))
    paths = request.gates
    required = (paths.phase1_verification, paths.semgrep, paths.provider_preflight, paths.guard_attestation, paths.report_fixture)
    for path in required:
        if not path.is_file():
            failures.append(f"missing gate artifact: {path.name}")
        elif not _fresh(path, now=now):
            failures.append(f"stale gate artifact: {path.name}")
    if request.stage == "metric-path":
        if paths.smoke_manifest is None or not paths.smoke_manifest.is_file():
            failures.append("missing finalized gpu-smoke manifest")
        elif not _fresh(paths.smoke_manifest, now=now):
            failures.append("stale finalized gpu-smoke manifest")
    if failures:
        return tuple(dict.fromkeys(failures)), None, {}

    try:
        semgrep = _json(paths.semgrep)
        provider = _json(paths.provider_preflight)
        guard = _json(paths.guard_attestation)
        report = _json(paths.report_fixture)
        frozen = _frozen_offer(provider, offer_id=request.offer_id, label=request.label)
    except LiveDispatchError as error:
        return (str(error),), None, {}
    if not _phase1_passed(paths.phase1_verification):
        failures.append("Phase 1 verification is not passed")
    if not _semgrep_passed(semgrep):
        failures.append("current Semgrep artifact did not pass")
    if provider.get("eligible") is not True or provider.get("balance_threshold_enabled") is not False or provider.get("instance_count") != 0:
        failures.append("provider preflight is not eligible with zero inventory and auto-recharge disabled")
    if not _guard_artifact_passed(guard, request):
        failures.append("live guard artifact does not attest exact nonce, label, deadline, and host")
    if not _report_fixture_passed(report):
        failures.append("report fixture did not prove a zero-request exact-target submission")
    if request.stage == "inference-smoke" and frozen.machine_id in INFERENCE_SMOKE_EXCLUDED_MACHINE_IDS:
        failures.append("inference-smoke offer uses an excluded historical machine")
    if request.stage == "metric-path":
        assert paths.smoke_manifest is not None
        try:
            if not _smoke_finalized(_json(paths.smoke_manifest)):
                failures.append("gpu-smoke is not finalized real-GPU evidence with absence proof")
        except LiveDispatchError as error:
            failures.append(str(error))
    hashes = {name: _artifact_hash(path) for name, path in (("phase1", paths.phase1_verification), ("semgrep", paths.semgrep), ("provider", paths.provider_preflight), ("guard", paths.guard_attestation), ("report", paths.report_fixture))}
    if paths.smoke_manifest is not None and paths.smoke_manifest.is_file():
        hashes["smoke"] = _artifact_hash(paths.smoke_manifest)
    return tuple(dict.fromkeys(failures)), frozen, hashes


def _write_json(path: Path, payload: Mapping[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return path


def _seal_bundle(run_directory: Path, declared: tuple[Path, ...], *, sums_relative: str = "SHA256SUMS", root_relative: str = "ROOT-HASH.txt") -> str:
    """Checksum named records without including mutable receipts in their root."""

    root = run_directory.resolve()
    normalized: list[Path] = []
    for path in declared:
        resolved = path.resolve()
        if not resolved.is_file() or root not in (resolved, *resolved.parents):
            raise LiveDispatchError("bundle declaration contains a missing or outside artifact")
        normalized.append(resolved)
    lines = [f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root).as_posix()}" for path in sorted(set(normalized))]
    sums = "\n".join(lines) + "\n"
    sums_path = run_directory / sums_relative
    root_path = run_directory / root_relative
    sums_path.parent.mkdir(parents=True, exist_ok=True)
    root_path.parent.mkdir(parents=True, exist_ok=True)
    sums_path.write_text(sums, encoding="utf-8")
    digest = hashlib.sha256(sums.encode("utf-8")).hexdigest()
    root_path.write_text(digest + "\n", encoding="utf-8")
    return digest


def _historical_smoke_machine_ids(
    output_root: Path,
    run_ids: tuple[str, ...],
    *,
    expected_anchors: Mapping[str, tuple[str, int]] = AUDITED_HISTORICAL_SMOKE_ANCHORS,
) -> frozenset[int]:
    """Read machine IDs only from independently pinned manifest roots."""

    if set(run_ids) != set(expected_anchors):
        raise LiveDispatchError("ledger smoke history differs from signed-source anchor pins")
    root = Path(output_root).resolve()
    machines: set[int] = set()
    for run_id in run_ids:
        if not run_id or Path(run_id).name != run_id:
            raise LiveDispatchError("ledger smoke run id is not a safe directory name")
        directory = root / run_id
        if directory.is_symlink() or directory.resolve().parent != root:
            raise LiveDispatchError(f"historical smoke directory is not an exact child: {run_id}")
        anchor_path = directory / "guard-anchor.json"
        if anchor_path.is_symlink() or not anchor_path.is_file():
            raise LiveDispatchError(f"historical smoke bundle is incomplete: {run_id}")
        anchor = _json(anchor_path)
        pinned_root, pinned_machine = expected_anchors[run_id]
        if anchor.get("root_hash") != pinned_root or anchor.get("acknowledged") != pinned_root:
            raise LiveDispatchError(f"historical smoke guard anchor differs from signed-source pin: {run_id}")

        pre_anchor_manifest = anchor.get("pre_anchor_manifest")
        pre_anchor_sums = anchor.get("pre_anchor_sums")
        if pre_anchor_manifest is None and pre_anchor_sums is None:
            manifest_path = directory / "run-manifest.json"
            sums_path = directory / "SHA256SUMS"
            root_path = directory / "ROOT-HASH.txt"
        elif pre_anchor_manifest == "pre-anchor/run-manifest.json" and pre_anchor_sums == "pre-anchor/SHA256SUMS":
            manifest_path = directory / pre_anchor_manifest
            sums_path = directory / pre_anchor_sums
            root_path = directory / "pre-anchor/ROOT-HASH.txt"
        else:
            raise LiveDispatchError(f"historical smoke guard anchor paths are invalid: {run_id}")
        if manifest_path.parent.is_symlink() or any(path.is_symlink() or not path.is_file() or directory not in path.resolve().parents for path in (manifest_path, sums_path, root_path)):
            raise LiveDispatchError(f"historical smoke anchored bundle is incomplete: {run_id}")
        sums = sums_path.read_bytes()
        root_hash = root_path.read_text(encoding="utf-8").strip()
        if root_hash != pinned_root or hashlib.sha256(sums).hexdigest() != pinned_root:
            raise LiveDispatchError(f"historical smoke root is invalid: {run_id}")
        entries: dict[str, str] = {}
        for line in sums.decode("utf-8").splitlines():
            digest, separator, relative = line.partition("  ")
            if not separator or not re.fullmatch(r"[0-9a-f]{64}", digest) or relative in entries:
                raise LiveDispatchError(f"historical smoke checksum list is invalid: {run_id}")
            entries[relative] = digest
        relative_manifest = str(manifest_path.relative_to(directory))
        expected = entries.get(relative_manifest)
        if expected is None or hashlib.sha256(manifest_path.read_bytes()).hexdigest() != expected:
            raise LiveDispatchError(f"historical smoke manifest is not root-bound: {run_id}")
        manifest = _json(manifest_path)
        offer = manifest.get("offer_contract")
        if manifest.get("run_id") != run_id or manifest.get("stage") != "gpu-smoke" or manifest.get("provider_create_calls") != 1 or not isinstance(offer, Mapping):
            raise LiveDispatchError(f"historical smoke manifest identity is invalid: {run_id}")
        machine_id = _to_int(offer.get("machine_id"), "historical machine id")
        if machine_id != pinned_machine:
            raise LiveDispatchError(f"historical smoke machine differs from signed-source pin: {run_id}")
        machines.add(machine_id)
    return frozenset(machines)


def _inference_history_machine_ids(
    output_root: Path,
    run_ids: tuple[str, ...],
    explicit_history: tuple[InferenceAttemptHistory, ...],
) -> frozenset[int]:
    """Resolve prior inference machine IDs from finalized bundles or supplied history.

    A durable reservation with a created instance cannot be silently ignored:
    its terminal manifest must prove exact teardown and guard anchoring unless
    the operator supplies an explicit exact-run machine record.
    """

    explicit = {item.run_id: item.machine_id for item in explicit_history}
    root = Path(output_root).resolve()
    machines = set(explicit.values())
    for run_id in run_ids:
        if not run_id or Path(run_id).name != run_id:
            raise LiveDispatchError("inference reservation run id is not a safe directory name")
        if run_id in explicit:
            continue
        directory = root / run_id
        if directory.is_symlink() or directory.resolve().parent != root:
            raise LiveDispatchError(f"inference history is missing a safe finalized bundle for {run_id}; provide explicit history")
        manifest_path = directory / "run-manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise LiveDispatchError(f"inference history is missing a finalized manifest for {run_id}; provide explicit history")
        manifest = _json(manifest_path)
        if manifest.get("run_id") != run_id or manifest.get("stage") != "inference-smoke" or manifest.get("budget_category") != "gpu-inference-smoke":
            raise LiveDispatchError(f"inference manifest identity is invalid for {run_id}")
        creates = manifest.get("provider_create_calls")
        if creates == 0:
            continue
        if creates != 1 or manifest.get("status") not in {"COMPLETED", "FAILED_SAFE", "REPORT_UNCONFIRMED", "HALTED"} or manifest.get("absence_reads") != 3:
            raise LiveDispatchError(f"inference manifest is not finalized with exact absence proof for {run_id}")
        anchor_path = directory / "guard-anchor.json"
        if anchor_path.is_symlink() or not anchor_path.is_file():
            raise LiveDispatchError(f"inference manifest lacks independent guard anchor for {run_id}")
        anchor = _json(anchor_path)
        guard_root = manifest.get("guard_anchor_root")
        if not isinstance(guard_root, str) or not guard_root or anchor.get("root_hash") != guard_root or anchor.get("acknowledged") != guard_root:
            raise LiveDispatchError(f"inference manifest guard anchor is invalid for {run_id}")
        offer = manifest.get("offer_contract")
        if not isinstance(offer, Mapping):
            raise LiveDispatchError(f"inference manifest lacks exact offer contract for {run_id}")
        machines.add(_to_int(offer.get("machine_id"), "historical inference machine id"))
    return frozenset(machines)


class LiveCanaryDispatcher:
    """Exactly-once paid dispatcher with injected guard, report, and workload."""

    def __init__(
        self,
        *,
        provider: VastCliProvider,
        guard: Guard,
        report_gate: ReportGate | None,
        workload: RemoteWorkload,
        ledger: ExposureLedger,
        output_root: Path,
        clock: _Clock | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        absence_interval_seconds: float = 5.0,
    ) -> None:
        self.provider = provider
        self.guard = guard
        self.report_gate = report_gate
        self.workload = workload
        self.ledger = ledger
        self.output_root = Path(output_root)
        self.clock = clock or SystemClock()
        if absence_interval_seconds < 0 or absence_interval_seconds > 15:
            raise ValueError("absence interval must be between 0 and 15 seconds")
        self.sleeper = sleeper
        self.absence_interval_seconds = absence_interval_seconds

    def _manifest(self, request: LiveCanaryRequest, *, status: str, limitation: str | None, provider_create_calls: int, provider_destroy_calls: int, absence_reads: int, provenance: str, real_gpu_claim: bool, gate_hashes: Mapping[str, str] | None = None, offer: OfferContract | None = None, report: Mapping[str, object] | None = None, guard_root: str | None = None, evidence_blockers: tuple[str, ...] = (), relative_path: str = "run-manifest.json") -> Path:
        run_directory = self.output_root / request.run_id
        payload: dict[str, object] = {
            "run_id": request.run_id,
            "stage": request.stage,
            "status": status,
            "limitation": limitation,
            "provenance": provenance,
            "real_gpu_claim": real_gpu_claim,
            "nonce": request.nonce,
            "label": request.label,
            "hard_deadline": _stamp(request.hard_deadline),
            "report_start_by": _stamp(request.report_start_by),
            "reserve": str(request.reserve),
            "budget_category": request.budget_category,
            "project_cap": str(PROJECT_CAP),
            "provider_create_calls": provider_create_calls,
            "provider_destroy_calls": provider_destroy_calls,
            "absence_reads": absence_reads,
            "gate_hashes": dict(gate_hashes or {}),
            "evidence_blockers": list(evidence_blockers),
            "launch": request.launch.to_json(),
            "workload_contract": request.workload.to_json(),
        }
        if offer is not None:
            payload["offer_contract"] = {
                "offer_id": offer.offer_id,
                "gpu_name": offer.gpu_name,
                "num_gpus": offer.num_gpus,
                "gpu_ram_mib": offer.gpu_ram_mib,
                "compute_capability": offer.compute_capability,
                "machine_id": offer.machine_id,
                "dph_total": str(offer.dph_total),
                "label": offer.label,
            }
        if report is not None:
            payload["report"] = dict(report)
        if guard_root is not None:
            payload["guard_anchor_root"] = guard_root
        return _write_json(run_directory / relative_path, payload)

    def _blocked(self, request: LiveCanaryRequest, limitation: str, *, gate_hashes: Mapping[str, str] | None = None) -> LiveDispatchResult:
        manifest = self._manifest(request, status="BLOCKED", limitation=limitation, provider_create_calls=0, provider_destroy_calls=0, absence_reads=0, provenance=LIMITATION_PROVENANCE, real_gpu_claim=False, gate_hashes=gate_hashes)
        _write_json(manifest.parent / "limitation.json", {"status": "BLOCKED", "limitation": limitation, "provenance": LIMITATION_PROVENANCE})
        _seal_bundle(manifest.parent, (manifest, manifest.parent / "limitation.json"))
        return LiveDispatchResult("BLOCKED", limitation, manifest, 0, 0, 0, LIMITATION_PROVENANCE, False)

    def _append(self, journal: RunJournal, state: RunState, event: str, payload: Mapping[str, object]) -> None:
        # Monotonic timestamps are required by RunJournal.  System monotonic
        # time is strictly increasing in practice; test clocks supply it.
        journal.append(state, event, dict(payload), self.clock.now(), self.clock.monotonic_ns())

    def _prove_absent(self, instance_id: int, label: str) -> tuple[str, ...]:
        reads: list[str] = []
        for index in range(3):
            remaining = self.provider.list_instances()
            if any(item.instance_id == instance_id or item.label == label for item in remaining):
                raise LiveDispatchError("instance remains present during three-read absence proof")
            reads.append(_stamp(self.clock.now()))
            if index < 2 and self.absence_interval_seconds:
                self.sleeper(self.absence_interval_seconds)
        return tuple(reads)

    def run(self, request: LiveCanaryRequest) -> LiveDispatchResult:
        now = self.clock.now()
        failures, frozen_offer, gate_hashes = validate_live_gates(request, now=now)
        if failures:
            return self._blocked(request, "; ".join(failures), gate_hashes=gate_hashes)
        assert frozen_offer is not None

        # Account/preflight evidence must pass before this live read.  This
        # fetch is still read-only and binds the frozen preflight record to the
        # exact current vms_enabled offer immediately before the sole create.
        try:
            current_offer = self.provider.get_vms_enabled_offer(request.offer_id, machine_id=frozen_offer.machine_id, label=request.label)
        except VastProviderError as error:
            return self._blocked(request, f"current exact offer cannot be frozen: {error}", gate_hashes=gate_hashes)
        if current_offer != frozen_offer:
            return self._blocked(request, "current offer differs from frozen vms_enabled contract", gate_hashes=gate_hashes)
        if request.stage == "inference-smoke" and current_offer.machine_id in INFERENCE_SMOKE_EXCLUDED_MACHINE_IDS:
            return self._blocked(request, "inference-smoke offer uses an excluded historical machine", gate_hashes=gate_hashes)
        if request.stage == "inference-smoke":
            try:
                historical_machines = _inference_history_machine_ids(
                    self.output_root,
                    self.ledger.run_ids_for_category("gpu-inference-smoke"),
                    request.inference_history,
                )
            except (BudgetWriteFailure, LiveDispatchError, OSError, UnicodeError) as error:
                return self._blocked(request, f"cannot verify prior inference machines: {error}", gate_hashes=gate_hashes)
            if current_offer.machine_id in historical_machines:
                return self._blocked(request, "inference-smoke offer reuses a prior inference machine", gate_hashes=gate_hashes)
        if request.budget_category == "gpu-smoke-distinct-machine":
            try:
                historical_machines = _historical_smoke_machine_ids(self.output_root, self.ledger.smoke_run_ids())
            except (BudgetWriteFailure, LiveDispatchError, OSError, UnicodeError) as error:
                return self._blocked(request, f"cannot verify historical smoke machines: {error}", gate_hashes=gate_hashes)
            if current_offer.machine_id in historical_machines:
                return self._blocked(request, "distinct-machine smoke offer reuses a historical paid-smoke machine", gate_hashes=gate_hashes)

        run_directory = self.output_root / request.run_id
        identity = RunIdentity(request.run_id, request.label, now)
        try:
            journal = RunJournal.create(run_directory / "journal", identity)
        except FileExistsError:
            return self._blocked(request, "run id already has a durable journal; refusing replay", gate_hashes=gate_hashes)

        instance: InstanceContract | None = None
        creates = destroys = absence_reads = 0
        absence_timestamps: tuple[str, ...] = ()
        report_data: dict[str, object] = {"attempted": False, "confirmed": False}
        limitation: str | None = None
        status = "FAILED_SAFE"
        evidence_blockers: tuple[str, ...] = ()
        evidence_files: tuple[Path, ...] = ()
        guard_root: str | None = None
        guard_armed = False
        try:
            self._append(journal, RunState.OFFLINE_VALIDATED, "gates.passed", {"gate_hashes": dict(gate_hashes)})
            reservation_category = request.budget_category or ("gpu-smoke" if request.stage == "gpu-smoke" else "canary")
            self.ledger.reserve(
                request.run_id,
                request.reserve,
                reservation_category,
                machine_id=current_offer.machine_id if request.stage == "inference-smoke" else None,
            )
            self._append(journal, RunState.BUDGET_RESERVED, "budget.reserved", {"reserve": str(request.reserve), "project_cap": str(PROJECT_CAP)})
            self._append(journal, RunState.OFFER_PINNED, "offer.pinned", {"offer_id": current_offer.offer_id, "machine_id": current_offer.machine_id, "dph_total": str(current_offer.dph_total), "label": current_offer.label, "vms_enabled": True})
            if self.report_gate is None:
                raise LiveDispatchError("exact-target report adapter is unavailable")
            self._append(journal, RunState.REPORT_ADAPTER_READY, "report.adapter_ready", {"fixture_gate": gate_hashes.get("report")})
            attestation = self.guard.arm(identity, request.hard_deadline)
            validate_attestation(identity, attestation)
            if attestation.nonce != request.nonce or attestation.hard_deadline.astimezone(UTC) != request.hard_deadline.astimezone(UTC):
                raise LiveDispatchError("live guard arm receipt does not bind exact nonce and immutable deadline")
            guard_armed = True
            self._append(journal, RunState.GUARD_ARMED, "guard.armed", {"hard_deadline": _stamp(request.hard_deadline), "report_start_by": _stamp(request.report_start_by), "host_identity": attestation.host_identity})

            # Recheck the live desktop session after guard arm and immediately
            # before any paid create.  Factory-time checks alone can go stale.
            self.report_gate.preflight_authenticated_session()
            if self.clock.now() >= request.report_start_by:
                raise LiveDispatchError("guard setup consumed the paid-stage execution window")

            worst_case = (current_offer.dph_total * Decimal(str(MAX_STAGE_RUNTIME.total_seconds())) / Decimal("3600")).quantize(Decimal("0.000001"))
            buffered_worst_case = (worst_case * Decimal("1.25") + Decimal("0.05")).quantize(Decimal("0.000001"))
            if buffered_worst_case > request.reserve:
                raise LiveDispatchError("frozen offer plus billing and teardown buffer can exceed the stage reservation")

            # Exactly one provider create can occur.  Any transport uncertainty
            # is reconciled only by this run's unique nonce-bound label.
            self._append(journal, RunState.CREATE_REQUESTED, "provider.create_intent", {"label": request.label, "offer_id": current_offer.offer_id, "launch": request.launch.to_json()})
            creates = 1
            try:
                observed = self.provider.create_once(current_offer, request.run_id, request.launch)
            except AmbiguousCreate:
                observed = self.provider.reconcile_label(request.label)
            if observed.label != request.label:
                raise LiveDispatchError("provider create did not return exact nonce-bound label")
            instance = observed
            self._append(
                journal,
                RunState.CREATED_VERIFYING,
                "provider.create_observed",
                {"instance_id": instance.instance_id, "machine_id": instance.machine_id},
            )

            def heartbeat() -> None:
                self.guard.record_heartbeat(identity, self.clock.monotonic_ns())

            immediate_contract_fault = classify_fault(current_offer, instance, ProbeOutcome.PASS) is FaultClass.PROVIDER_FAULT_CONFIRMED
            if immediate_contract_fault:
                self._append(
                    journal,
                    RunState.CAPTURING_FAULT,
                    "provider.fault_confirmed",
                    {
                        "automatic_contract_mismatch": True,
                        "expected_machine_id": current_offer.machine_id,
                        "observed_machine_id": instance.machine_id,
                    },
                )
                if self.clock.now() < request.report_start_by:
                    self._append(journal, RunState.REPORTING_FAULT, "provider.report_started", {"report_start_by": _stamp(request.report_start_by)})
                    try:
                        receipt = self.report_gate.handle(
                            FaultRecord(instance.instance_id, instance.label, request.nonce, "confirmed provider contract mismatch immediately after create"),
                            request.report_start_by,
                        )
                        report_data = {"attempted": True, "confirmed": receipt.confirmed, "before": str(receipt.before_path), "after": str(receipt.after_path) if receipt.after_path else None}
                    except (TimeoutError, ValueError) as error:
                        report_data = {"attempted": True, "confirmed": False, "error": str(error)}
                        limitation = "confirmed provider fault report did not finish before teardown"
                else:
                    limitation = "confirmed provider fault reached report cutoff; report skipped for teardown margin"
                status = "FAILED_SAFE" if report_data.get("confirmed") else "REPORT_UNCONFIRMED"
            else:
                heartbeat()
                self._append(journal, RunState.RUNNING_CANARY, "canary.started", {"instance_id": instance.instance_id})
                evidence = self.workload.run(stage=request.stage, workload=request.workload, instance=instance, run_directory=run_directory, hard_deadline=request.hard_deadline, heartbeat=heartbeat)
                evidence_files = evidence.evidence_files
                completed, evidence_blockers = evidence.complete_for(request.stage, now=self.clock.now(), workload=request.workload, run_id=request.run_id, instance=instance)
                remote_fault = evidence.provider_fault is not None and evidence.provider_fault.valid()
                if remote_fault:
                    self._append(journal, RunState.CAPTURING_FAULT, "provider.fault_confirmed", {"automatic_contract_mismatch": False, "remote_category": evidence.provider_fault.category if evidence.provider_fault else None})
                    if self.clock.now() < request.report_start_by:
                        self._append(journal, RunState.REPORTING_FAULT, "provider.report_started", {"report_start_by": _stamp(request.report_start_by)})
                        try:
                            receipt = self.report_gate.handle(FaultRecord(instance.instance_id, instance.label, request.nonce, evidence.provider_fault.description if evidence.provider_fault else "confirmed provider fault"), request.report_start_by)
                            report_data = {"attempted": True, "confirmed": receipt.confirmed, "before": str(receipt.before_path), "after": str(receipt.after_path) if receipt.after_path else None}
                        except (TimeoutError, ValueError) as error:
                            report_data = {"attempted": True, "confirmed": False, "error": str(error)}
                            limitation = "confirmed provider fault report did not finish before teardown"
                    else:
                        limitation = "confirmed provider fault reached report cutoff; report skipped for teardown margin"
                    status = "FAILED_SAFE" if report_data.get("confirmed") else "REPORT_UNCONFIRMED"
                elif not completed:
                    limitation = "; ".join(evidence_blockers)
                    status = "FAILED_SAFE"
                else:
                    self._append(journal, RunState.COLLECTING, "canary.evidence_complete", {"stage": request.stage})
                    status = "COMPLETED"
        except Exception as error:
            limitation = str(error)
            status = "FAILED_SAFE"
        finally:
            if instance is not None:
                try:
                    if journal.state() is not RunState.DESTROYING:
                        self._append(journal, RunState.DESTROYING, "teardown.exact", {"instance_id": instance.instance_id, "label": instance.label})
                    try:
                        self.provider.destroy_exact(instance.instance_id, instance.label)
                        destroys += 1
                    except VastProviderError as error:
                        if str(error) != "current instance contract is not uniquely available":
                            raise
                        # The independently armed guard can win the teardown
                        # race.  Never count that as a controller destroy, but
                        # still prove exact ID/label absence with fresh reads.
                        limitation = (
                            f"{limitation + '; ' if limitation else ''}"
                            "target became absent before controller teardown; external guard teardown not locally attributable"
                        )
                        status = "FAILED_SAFE"
                    absence_timestamps = self._prove_absent(instance.instance_id, instance.label)
                    absence_reads = len(absence_timestamps)
                    event = "absence.proved" if destroys else "absence.proved_external_teardown"
                    self._append(journal, RunState.ABSENCE_VERIFYING, event, {"reads": absence_reads, "timestamps": list(absence_timestamps)})
                except Exception as error:
                    limitation = f"{limitation + '; ' if limitation else ''}teardown/absence failure: {error}"
                    status = "FAILED_SAFE"
            if journal.state() is not RunState.TERMINAL:
                try:
                    self._append(journal, RunState.TERMINAL, "terminal.safe", {"status": status, "limitation": limitation})
                except ValueError:
                    # Journal integrity failure is itself a limitation; no
                    # retry may happen because create intent was durable.
                    status = "FAILED_SAFE"
                    limitation = f"{limitation + '; ' if limitation else ''}journal terminal transition failed"
            # The full reservation remains charged after teardown. It may be
            # reconciled only from provider credit evidence plus this run's
            # three-read absence proof; a local elapsed-time estimate cannot
            # release the project or stage cap.

        evidence_complete = status == "COMPLETED" and absence_reads == 3 and not evidence_blockers
        if not guard_armed:
            manifest = self._manifest(request, status=status, limitation=limitation, provider_create_calls=creates, provider_destroy_calls=destroys, absence_reads=absence_reads, provenance=LIMITATION_PROVENANCE, real_gpu_claim=False, gate_hashes=gate_hashes, offer=current_offer, report=report_data, evidence_blockers=evidence_blockers)
            limitation_record = _write_json(run_directory / "limitation.json", {"status": status, "limitation": limitation, "provenance": LIMITATION_PROVENANCE, "real_gpu_claim": False})
            _seal_bundle(run_directory, (manifest, limitation_record, *(path for path in evidence_files if path.is_file())))
            return LiveDispatchResult(status, limitation, manifest, creates, destroys, absence_reads, LIMITATION_PROVENANCE, False)

        pre_anchor_status = "PENDING_ANCHOR" if evidence_complete else status
        pre_anchor_limitation = "awaiting independent guard anchor" if evidence_complete else limitation
        pre_anchor_manifest = self._manifest(request, status=pre_anchor_status, limitation=pre_anchor_limitation, provider_create_calls=creates, provider_destroy_calls=destroys, absence_reads=absence_reads, provenance=LIMITATION_PROVENANCE, real_gpu_claim=False, gate_hashes=gate_hashes, offer=current_offer, report=report_data, evidence_blockers=evidence_blockers, relative_path="pre-anchor/run-manifest.json")
        pre_anchor_limitation_record = _write_json(run_directory / "pre-anchor/limitation.json", {"status": pre_anchor_status, "limitation": pre_anchor_limitation, "provenance": LIMITATION_PROVENANCE, "real_gpu_claim": False})
        pre_anchor_sums = run_directory / "pre-anchor/SHA256SUMS"
        pre_anchor_root = run_directory / "pre-anchor/ROOT-HASH.txt"
        try:
            root_hash = _seal_bundle(run_directory, (pre_anchor_manifest, pre_anchor_limitation_record, *(path for path in evidence_files if path.is_file())), sums_relative="pre-anchor/SHA256SUMS", root_relative="pre-anchor/ROOT-HASH.txt")
        except Exception as error:
            limitation = f"{limitation + '; ' if limitation else ''}integrity bundle failure: {error}"
            status = "FAILED_SAFE"
            manifest = self._manifest(request, status=status, limitation=limitation, provider_create_calls=creates, provider_destroy_calls=destroys, absence_reads=absence_reads, provenance=LIMITATION_PROVENANCE, real_gpu_claim=False, gate_hashes=gate_hashes, offer=current_offer, report=report_data, evidence_blockers=evidence_blockers)
            _write_json(run_directory / "limitation.json", {"status": status, "limitation": limitation, "provenance": LIMITATION_PROVENANCE, "real_gpu_claim": False})
            return LiveDispatchResult(status, limitation, manifest, creates, destroys, absence_reads, LIMITATION_PROVENANCE, False)
        try:
            guard_root = self.guard.anchor(root_hash)
            if guard_root != root_hash:
                raise LiveDispatchError("guard acknowledged a different bundle root")
            anchor_record = _write_json(run_directory / "guard-anchor.json", {"root_hash": root_hash, "acknowledged": guard_root, "pre_anchor_sums": "pre-anchor/SHA256SUMS", "pre_anchor_manifest": "pre-anchor/run-manifest.json"})
            provenance = REAL_GPU_PROVENANCE if evidence_complete else LIMITATION_PROVENANCE
            real_gpu_claim = evidence_complete
            manifest = self._manifest(request, status=status, limitation=limitation, provider_create_calls=creates, provider_destroy_calls=destroys, absence_reads=absence_reads, provenance=provenance, real_gpu_claim=real_gpu_claim, gate_hashes=gate_hashes, offer=current_offer, report=report_data, guard_root=guard_root, evidence_blockers=evidence_blockers)
            limitation_record = _write_json(run_directory / "limitation.json", {"status": status, "limitation": limitation, "provenance": provenance, "real_gpu_claim": real_gpu_claim, "guard_anchor_root": guard_root})
            _seal_bundle(run_directory, (manifest, limitation_record, anchor_record, pre_anchor_manifest, pre_anchor_limitation_record, pre_anchor_sums, pre_anchor_root, *(path for path in evidence_files if path.is_file())))
        except Exception as error:
            limitation = f"{limitation + '; ' if limitation else ''}guard anchor failure: {error}"
            status = "FAILED_SAFE"
            manifest = self._manifest(request, status=status, limitation=limitation, provider_create_calls=creates, provider_destroy_calls=destroys, absence_reads=absence_reads, provenance=LIMITATION_PROVENANCE, real_gpu_claim=False, gate_hashes=gate_hashes, offer=current_offer, report=report_data, evidence_blockers=evidence_blockers)
            limitation_record = _write_json(run_directory / "limitation.json", {"status": status, "limitation": limitation, "provenance": LIMITATION_PROVENANCE, "real_gpu_claim": False})
            try:
                _seal_bundle(run_directory, (manifest, limitation_record, pre_anchor_manifest, pre_anchor_limitation_record, pre_anchor_sums, pre_anchor_root, *(path for path in evidence_files if path.is_file())))
            except Exception:
                pass
        real_gpu_claim = status == "COMPLETED" and evidence_complete and guard_root is not None
        return LiveDispatchResult(status, limitation, manifest, creates, destroys, absence_reads, REAL_GPU_PROVENANCE if real_gpu_claim else LIMITATION_PROVENANCE, real_gpu_claim)
