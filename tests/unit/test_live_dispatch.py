from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from srecon26_poc.budget import ExposureLedger
from srecon26_poc.canary import CanarySnapshot, KvmFacts, MetricSample, VLLM_IMAGE_DIGEST
from srecon26_poc.contracts import InstanceContract, OfferContract, ProbeOutcome
from srecon26_poc.guard import GuardAttestation
from srecon26_poc.live_dispatch import (
    GateArtifactPaths,
    LiveCanaryDispatcher,
    LiveCanaryRequest,
    LiveEvidence,
    ProviderFaultEvidence,
    REAL_GPU_PROVENANCE,
    WorkloadContract,
    FROZEN_MODEL_ID,
    FROZEN_MODEL_REVISION,
)
from srecon26_poc.provider import AmbiguousCreate
from srecon26_poc.reporting import FaultRecord, ReportGate, ReportReceipt
from srecon26_poc.vast_provider import OFFICIAL_KVM_IMAGE, OFFICIAL_UBUNTU_2204_TEMPLATE_HASH, VastLaunchContract


class Clock:
    def __init__(self, now: datetime) -> None:
        self.current = now
        self.tick = 0

    def now(self) -> datetime:
        return self.current

    def monotonic_ns(self) -> int:
        self.tick += 1
        return self.tick


class Provider:
    def __init__(self, offer: OfferContract, events: list[str], *, ambiguous: bool = False, mismatch: bool = False) -> None:
        self.offer, self.events = offer, events
        self.ambiguous, self.mismatch = ambiguous, mismatch
        self.instances: list[InstanceContract] = []
        self.create_calls = 0

    def get_vms_enabled_offer(self, offer_id: int, *, machine_id: int, label: str) -> OfferContract:
        self.events.append("read-offer")
        assert offer_id == self.offer.offer_id and machine_id == self.offer.machine_id
        return replace(self.offer, label=label)

    def create_once(self, contract: OfferContract, request_key: str, launch: VastLaunchContract) -> InstanceContract:
        self.events.append("create")
        self.create_calls += 1
        assert self.create_calls == 1
        assert request_key
        assert launch.disk_gib == 130 and launch.ubuntu_template_hash == OFFICIAL_UBUNTU_2204_TEMPLATE_HASH
        instance = InstanceContract(417, "wrong GPU" if self.mismatch else contract.gpu_name, contract.num_gpus, contract.gpu_ram_mib, contract.compute_capability, contract.machine_id, contract.dph_total, contract.label)
        self.instances = [instance]
        if self.ambiguous:
            raise AmbiguousCreate("timeout after create")
        return instance

    def reconcile_label(self, label: str) -> InstanceContract:
        self.events.append("reconcile")
        matches = [item for item in self.instances if item.label == label]
        if len(matches) != 1:
            raise AmbiguousCreate("not unique")
        return matches[0]

    def destroy_exact(self, instance_id: int, expected_label: str) -> None:
        self.events.append("destroy")
        assert [(item.instance_id, item.label) for item in self.instances] == [(instance_id, expected_label)]
        self.instances = []

    def list_instances(self) -> tuple[InstanceContract, ...]:
        self.events.append("list")
        return tuple(self.instances)


class Guard:
    def __init__(self, nonce: str, events: list[str], *, anchor_error: bool = False) -> None:
        self.nonce, self.events = nonce, events
        self.anchor_error = anchor_error
        self.identity = None
        self.deadline = None

    def preflight(self) -> GuardAttestation:
        assert self.identity is not None and self.deadline is not None
        return GuardAttestation("independent.guard.test", "guard-sha256", self.nonce, self.identity.label, self.deadline, None)

    def arm(self, identity, hard_deadline: datetime) -> GuardAttestation:
        self.events.append("arm")
        self.identity, self.deadline = identity, hard_deadline
        return self.preflight()

    def record_heartbeat(self, identity, monotonic_ns: int) -> None:
        self.events.append("heartbeat")
        assert identity == self.identity and monotonic_ns > 0

    def anchor(self, root_hash: str) -> str:
        self.events.append("anchor")
        if self.anchor_error:
            raise RuntimeError("anchor unavailable")
        return root_hash


class Reporter:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def preflight_exact_instance(self, instance_id: int, label: str) -> None:
        self.events.append("report-preflight")
        assert instance_id == 417 and label.endswith("nonce_12345678")

    def capture_before(self, fault: FaultRecord) -> Path:
        self.events.append("report-before")
        return Path("before.png")

    def submit(self, fault: FaultRecord) -> bool:
        self.events.append("report-submit")
        return True

    def capture_after(self, receipt: ReportReceipt) -> Path:
        self.events.append("report-after")
        return Path("after.png")


class Workload:
    def __init__(self, *, complete: bool = True, remote_fault: bool = False) -> None:
        self.complete, self.remote_fault = complete, remote_fault

    def run(self, *, stage: str, workload: WorkloadContract, instance: InstanceContract, run_directory: Path, hard_deadline: datetime, heartbeat) -> LiveEvidence:
        heartbeat()
        evidence = run_directory / "metrics" / "captured.json"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text(json.dumps({"instance_id": instance.instance_id}), encoding="utf-8")
        now = hard_deadline - timedelta(minutes=20)
        facts = KvmFacts(True, True, True, True, True, True, True, VLLM_IMAGE_DIGEST, now)
        if not self.complete:
            facts = replace(facts, cuda=False)
        sample = MetricSample(Decimal("1"), now)
        snapshot = CanarySnapshot(1, True, True, 1, True, sample, sample, sample, True, True)
        fault = ProviderFaultEvidence("cuda", "confirmed CUDA driver mismatch", evidence) if self.remote_fault else None
        return LiveEvidence(
            gpu_identity="GPU-0: RTX 3090",
            cuda_version="12.4",
            kvm_facts=facts,
            model_id=workload.model_id,
            model_revision=workload.model_revision,
            vllm_image_digest=workload.vllm_image_digest,
            snapshot=snapshot,
            resource_metrics_api=True,
            custom_metrics_api=True,
            hpa_observed=True,
            events_captured=True,
            timing_captured=True,
            probe_outcome=ProbeOutcome.PASS,
            provider_fault=fault,
            evidence_files=(evidence,),
        )


def _write(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _request(tmp_path: Path, now: datetime, *, bad_semgrep: bool = False) -> tuple[LiveCanaryRequest, OfferContract]:
    label = "srecon26-gpu-smoke--nonce-nonce_12345678"
    deadline = now + timedelta(minutes=20)
    offer = OfferContract(101, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), label)
    phase1 = tmp_path / "01-VERIFICATION.md"
    phase1.write_text("---\nstatus: passed\n---\n", encoding="utf-8")
    semgrep = _write(tmp_path / "semgrep.json", {"results": ["finding"] if bad_semgrep else [], "errors": []})
    provider = _write(tmp_path / "provider.json", {"eligible": True, "balance_threshold_enabled": False, "instance_count": 0, "offers": [{"offer_id": 101, "gpu_name": "RTX 3090", "num_gpus": 1, "gpu_ram_mib": 24576, "compute_capability": "8.6", "machine_id": 99, "dph_total": "0.30", "vms_enabled": True}]})
    guard = _write(tmp_path / "guard.json", {"status": "ARMED", "nonce": "nonce_12345678", "label": label, "hard_deadline": deadline.isoformat().replace("+00:00", "Z"), "host_identity": "independent.guard.test", "script_hash": "abc"})
    report = _write(tmp_path / "report.json", {"provider_requests": 0, "receipt": {"status": "SUBMITTED"}})
    return (
        LiveCanaryRequest("gpu-smoke", "live-test-run", "nonce_12345678", label, 101, Decimal("1.00"), deadline, GateArtifactPaths(phase1, semgrep, provider, guard, report), VastLaunchContract(OFFICIAL_UBUNTU_2204_TEMPLATE_HASH, OFFICIAL_KVM_IMAGE), WorkloadContract(FROZEN_MODEL_ID, FROZEN_MODEL_REVISION, VLLM_IMAGE_DIGEST)),
        offer,
    )


def _dispatcher(tmp_path: Path, request: LiveCanaryRequest, offer: OfferContract, *, complete: bool = True, ambiguous: bool = False, mismatch: bool = False, remote_fault: bool = False, anchor_error: bool = False):
    events: list[str] = []
    provider = Provider(offer, events, ambiguous=ambiguous, mismatch=mismatch)
    dispatcher = LiveCanaryDispatcher(provider=provider, guard=Guard(request.nonce, events, anchor_error=anchor_error), report_gate=ReportGate(Reporter(events)), workload=Workload(complete=complete, remote_fault=remote_fault), ledger=ExposureLedger(tmp_path / "ledger.json"), output_root=tmp_path / "runs", clock=Clock(request.hard_deadline - timedelta(minutes=20) + timedelta(seconds=1)), absence_interval_seconds=0)
    return dispatcher, provider, events


def test_missing_current_gate_never_reads_or_mutates_provider(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    request, offer = _request(tmp_path, now, bad_semgrep=True)
    dispatcher, provider, events = _dispatcher(tmp_path, request, offer)

    result = dispatcher.run(request)

    assert result.status == "BLOCKED"
    assert provider.create_calls == 0
    assert events == []
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["provider_create_calls"] == 0
    assert manifest["provenance"] == "live-limitation"


def test_retry_budget_override_is_bounded_and_smoke_only(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    request, _ = _request(tmp_path, now)
    assert not replace(request, reserve=Decimal("0.90"), budget_category="gpu-smoke-retry").validate(now=now)
    assert "audited gpu-smoke retry reserve must be no greater than 0.90" in replace(request, reserve=Decimal("1.00"), budget_category="gpu-smoke-retry").validate(now=now)
    failures = replace(request, stage="metric-path", reserve=Decimal("0.90"), budget_category="gpu-smoke-retry").validate(now=now)
    assert "budget category override is restricted to the audited gpu-smoke retry" in failures


def test_budget_failure_before_guard_arm_does_not_attempt_guard_anchor(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    request, offer = _request(tmp_path, now)
    dispatcher, provider, events = _dispatcher(tmp_path, request, offer)
    dispatcher.ledger.reserve("prior-run", Decimal("1.00"), "gpu-smoke")

    result = dispatcher.run(request)

    assert result.status == "FAILED_SAFE"
    assert result.limitation == "reservation would exceed approved exposure"
    assert provider.create_calls == 0
    assert "arm" not in events
    assert "anchor" not in events


def test_dispatcher_reconciles_ambiguous_create_without_a_second_create(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    request, offer = _request(tmp_path, now)
    dispatcher, provider, events = _dispatcher(tmp_path, request, offer, ambiguous=True)

    result = dispatcher.run(request)

    assert result.status == "COMPLETED"
    assert provider.create_calls == 1
    assert events.count("create") == 1
    assert events.index("reconcile") > events.index("create")
    assert events.count("list") == 3
    assert result.absence_reads == 3
    manifest = json.loads(result.manifest_path.read_text())
    receipt = json.loads((result.manifest_path.parent / "guard-anchor.json").read_text())
    assert manifest["guard_anchor_root"] == receipt["acknowledged"]
    assert "guard-anchor.json" in (result.manifest_path.parent / "SHA256SUMS").read_text()
    pre_anchor_sums = result.manifest_path.parent / receipt["pre_anchor_sums"]
    assert hashlib.sha256(pre_anchor_sums.read_bytes()).hexdigest() == receipt["acknowledged"]
    for line in pre_anchor_sums.read_text().splitlines():
        expected, relative = line.split("  ", 1)
        assert hashlib.sha256((result.manifest_path.parent / relative).read_bytes()).hexdigest() == expected


def test_anchor_failure_never_publishes_real_gpu_claim(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    request, offer = _request(tmp_path, now)
    dispatcher, _provider, events = _dispatcher(tmp_path, request, offer, anchor_error=True)

    result = dispatcher.run(request)

    assert result.status == "FAILED_SAFE"
    assert result.real_gpu_claim is False
    assert events.count("anchor") == 1
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["real_gpu_claim"] is False
    assert manifest["provenance"] != REAL_GPU_PROVENANCE


def test_confirmed_provider_fault_reports_before_exact_teardown(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    request, offer = _request(tmp_path, now)
    dispatcher, provider, events = _dispatcher(tmp_path, request, offer, mismatch=True)

    result = dispatcher.run(request)

    assert result.status == "FAILED_SAFE"
    assert provider.create_calls == 1
    assert events.index("report-submit") < events.index("destroy")
    assert events.index("report-after") < events.index("destroy")
    assert result.absence_reads == 3


def test_only_complete_live_evidence_gets_real_gpu_provenance(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    request, offer = _request(tmp_path, now)
    dispatcher, _provider, _events = _dispatcher(tmp_path, request, offer, complete=False)

    result = dispatcher.run(request)

    assert result.status == "FAILED_SAFE"
    assert result.real_gpu_claim is False
    assert result.provenance != REAL_GPU_PROVENANCE
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["real_gpu_claim"] is False
