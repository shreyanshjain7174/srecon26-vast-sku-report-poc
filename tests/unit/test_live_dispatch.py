from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import srecon26_poc.live_dispatch as live_dispatch_module
from srecon26_poc.budget import ExposureLedger
from srecon26_poc.canary import CanarySnapshot, KvmFacts, MetricSample, VLLM_IMAGE_DIGEST
from srecon26_poc.contracts import InstanceContract, OfferContract, ProbeOutcome
from srecon26_poc.guard import GuardAttestation
from srecon26_poc.live_dispatch import (
    APPROVED_GPU_PROFILES,
    GateArtifactPaths,
    InferenceAttemptHistory,
    InferenceMeasurementEvidence,
    LiveCanaryDispatcher,
    LiveDispatchError,
    LiveCanaryRequest,
    LiveEvidence,
    ProviderFaultEvidence,
    REAL_GPU_PROVENANCE,
    WorkloadContract,
    FROZEN_MODEL_ID,
    FROZEN_MODEL_REVISION,
    _historical_smoke_machine_ids,
)
from srecon26_poc.provider import AmbiguousCreate
from srecon26_poc.reporting import FaultRecord, ReportGate, ReportReceipt
from srecon26_poc.vast_provider import OFFICIAL_KVM_IMAGE, OFFICIAL_UBUNTU_2204_TEMPLATE_HASH, VastLaunchContract, VastProviderError


class Clock:
    def __init__(self, now: datetime) -> None:
        self.current = now
        self.tick = 0

    def now(self) -> datetime:
        return self.current

    def monotonic_ns(self) -> int:
        self.tick += 1
        return self.tick


def test_verified_blackwell_profile_is_exactly_pinned() -> None:
    assert ("RTX 5090", 32607, "12") in APPROVED_GPU_PROFILES
    assert ("RTX 5090", 32608, "12") not in APPROVED_GPU_PROFILES


class Provider:
    def __init__(self, offer: OfferContract, events: list[str], *, ambiguous: bool = False, mismatch: bool = False, machine_mismatch: bool = False, external_absence: bool = False) -> None:
        self.offer, self.events = offer, events
        self.ambiguous, self.mismatch, self.machine_mismatch, self.external_absence = ambiguous, mismatch, machine_mismatch, external_absence
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
        instance = InstanceContract(417, "wrong GPU" if self.mismatch else contract.gpu_name, contract.num_gpus, contract.gpu_ram_mib, contract.compute_capability, 147086 if self.machine_mismatch else contract.machine_id, contract.dph_total, contract.label)
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
        if self.external_absence:
            self.instances = []
            raise VastProviderError("current instance contract is not uniquely available")
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
    def __init__(self, events: list[str], *, session_ok: bool = True) -> None:
        self.events = events
        self.session_ok = session_ok

    def preflight_authenticated_session(self) -> None:
        self.events.append("report-session-preflight")
        if not self.session_ok:
            raise ValueError("not authenticated")

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
    def __init__(self, *, complete: bool = True, remote_fault: bool = False, direct_inference: bool = True) -> None:
        self.complete, self.remote_fault, self.direct_inference = complete, remote_fault, direct_inference

    def run(self, *, stage: str, workload: WorkloadContract, instance: InstanceContract, run_directory: Path, hard_deadline: datetime, heartbeat) -> LiveEvidence:
        heartbeat()
        evidence = run_directory / "metrics" / "captured.json"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        now = hard_deadline - timedelta(minutes=20)
        measurement = None
        if stage == "inference-smoke" and self.direct_inference:
            evidence.write_text(
                json.dumps(
                    {
                        "schema": "srecon26-direct-inference-measurement/v1",
                        "source": "RemoteWorkload.direct_inference/v1",
                        "run_id": run_directory.name,
                        "instance_id": instance.instance_id,
                        "label": instance.label,
                        "model_id": workload.model_id,
                        "model_revision": workload.model_revision,
                        "vllm_image_digest": workload.vllm_image_digest,
                        "request": {"succeeded": True, "output_tokens": 4},
                        "timing": {"ttft_seconds": "0.01", "latency_seconds": "0.02"},
                        "observed_at": now.isoformat().replace("+00:00", "Z"),
                    }
                ),
                encoding="utf-8",
            )
            measurement = InferenceMeasurementEvidence(evidence)
        else:
            evidence.write_text(json.dumps({"instance_id": instance.instance_id}), encoding="utf-8")
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
            inference_measurement=measurement,
        )


class HostStartupFaultWorkload:
    def __init__(self, *, tamper_label: bool = False) -> None:
        self.tamper_label = tamper_label

    def run(self, *, stage: str, workload: WorkloadContract, instance: InstanceContract, run_directory: Path, hard_deadline: datetime, heartbeat) -> LiveEvidence:
        del stage, workload, hard_deadline
        heartbeat()
        artifact = run_directory / "remote-transport" / "provider-startup-fault.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(
            json.dumps(
                {
                    "schema": "srecon26-provider-startup-fault/v1",
                    "source": "VastSshResolver.startup_observation/v1",
                    "category": "host",
                    "run_id": run_directory.name,
                    "instance_id": instance.instance_id,
                    "label": "tampered-label" if self.tamper_label else instance.label,
                    "confirmed": {
                        "all_bounded_provider_reads_succeeded": True,
                        "exact_instance_and_label_preserved": True,
                        "endpoint_published_on_every_read": True,
                        "no_actual_status_running": True,
                        "only_nonterminal_startup_statuses": True,
                        "desktop_report_reserve_seconds": 90,
                    },
                    "bounded_reads": 2,
                    "observations": [
                        {"attempt": 1, "actual_status": "created", "endpoint_published": True, "selected_ssh_route": "proxy", "observed_at": "2026-09-23T04:00:00Z"},
                        {"attempt": 2, "actual_status": "loading", "endpoint_published": True, "selected_ssh_route": "proxy", "observed_at": "2026-09-23T04:00:01Z"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        return LiveEvidence(
            None,
            None,
            None,
            probe_outcome=ProbeOutcome.CONTROLLER_FAILED,
            provider_fault=ProviderFaultEvidence("host", "exact startup fault", artifact),
            evidence_files=(artifact,),
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


def _dispatcher(tmp_path: Path, request: LiveCanaryRequest, offer: OfferContract, *, complete: bool = True, ambiguous: bool = False, mismatch: bool = False, machine_mismatch: bool = False, remote_fault: bool = False, anchor_error: bool = False, external_absence: bool = False, report_session_ok: bool = True, direct_inference: bool = True):
    events: list[str] = []
    provider = Provider(offer, events, ambiguous=ambiguous, mismatch=mismatch, machine_mismatch=machine_mismatch, external_absence=external_absence)
    dispatcher = LiveCanaryDispatcher(provider=provider, guard=Guard(request.nonce, events, anchor_error=anchor_error), report_gate=ReportGate(Reporter(events, session_ok=report_session_ok)), workload=Workload(complete=complete, remote_fault=remote_fault, direct_inference=direct_inference), ledger=ExposureLedger(tmp_path / "ledger.json"), output_root=tmp_path / "runs", clock=Clock(request.hard_deadline - timedelta(minutes=20) + timedelta(seconds=1)), absence_interval_seconds=0)
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
    assert "budget category override is restricted to an audited gpu-smoke entitlement" in failures
    assert "run id must be 8-128 URL-safe characters" in replace(request, run_id="../../escape").validate(now=now)
    assert not replace(request, reserve=Decimal("0.25"), budget_category="gpu-smoke-distinct-machine").validate(now=now)
    assert "audited distinct-machine smoke reserve must be no greater than 0.25" in replace(request, reserve=Decimal("0.250001"), budget_category="gpu-smoke-distinct-machine").validate(now=now)
    failures = replace(request, stage="metric-path", reserve=Decimal("0.25"), budget_category="gpu-smoke-distinct-machine").validate(now=now)
    assert "budget category override is restricted to an audited gpu-smoke entitlement" in failures


def test_inference_smoke_is_a_separate_half_dollar_entitlement(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    request, _ = _request(tmp_path, now)

    request = replace(request, stage="inference-smoke", reserve=Decimal("0.50"), budget_category="gpu-inference-smoke")
    assert not request.validate(now=now)
    assert "inference-smoke requires the gpu-inference-smoke budget category" in replace(request, budget_category=None).validate(now=now)
    assert "direct inference smoke reserve must be no greater than 1.00" in replace(request, reserve=Decimal("1.000001")).validate(now=now)


def test_inference_smoke_completes_only_with_direct_inference_measurement(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    request, offer = _request(tmp_path, now)
    request = replace(request, stage="inference-smoke", reserve=Decimal("0.50"), budget_category="gpu-inference-smoke")
    dispatcher, provider, _events = _dispatcher(tmp_path, request, offer)

    result = dispatcher.run(request)

    assert result.status == "COMPLETED"
    assert result.real_gpu_claim is True
    assert provider.create_calls == 1
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["budget_category"] == "gpu-inference-smoke"


def test_inference_smoke_rejects_gpu_kvm_only_evidence(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    request, offer = _request(tmp_path, now)
    request = replace(request, stage="inference-smoke", reserve=Decimal("0.50"), budget_category="gpu-inference-smoke")
    dispatcher, provider, _events = _dispatcher(tmp_path, request, offer, direct_inference=False)

    result = dispatcher.run(request)

    assert result.status == "FAILED_SAFE"
    assert provider.create_calls == 1
    assert "missing actual direct inference measurement evidence" in (result.limitation or "")
    assert result.real_gpu_claim is False


def test_inference_smoke_rejects_a_machine_from_a_finalized_prior_inference_manifest(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    first, offer = _request(tmp_path, now)
    first = replace(first, stage="inference-smoke", run_id="inference-run-one", reserve=Decimal("0.50"), budget_category="gpu-inference-smoke")
    first_dispatcher, first_provider, _first_events = _dispatcher(tmp_path, first, offer)

    first_result = first_dispatcher.run(first)

    assert first_result.status == "COMPLETED"
    assert first_provider.create_calls == 1
    second = replace(first, run_id="inference-run-two", nonce="nonce_87654321", label="srecon26-inference-smoke--nonce-nonce_87654321")
    guard_payload = json.loads(second.gates.guard_attestation.read_text())
    guard_payload.update({"nonce": second.nonce, "label": second.label})
    second.gates.guard_attestation.write_text(json.dumps(guard_payload), encoding="utf-8")
    second_dispatcher, second_provider, second_events = _dispatcher(tmp_path, second, replace(offer, label=second.label))

    second_result = second_dispatcher.run(second)

    assert second_result.status == "BLOCKED"
    assert second_result.limitation == "inference-smoke offer reuses a prior inference machine"
    assert second_provider.create_calls == 0
    assert "arm" not in second_events


def test_inference_smoke_rejects_a_machine_in_explicit_request_history(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    request, offer = _request(tmp_path, now)
    request = replace(
        request,
        stage="inference-smoke",
        reserve=Decimal("0.50"),
        budget_category="gpu-inference-smoke",
        inference_history=(InferenceAttemptHistory("inference-run-prior", offer.machine_id),),
    )
    dispatcher, provider, events = _dispatcher(tmp_path, request, offer)

    result = dispatcher.run(request)

    assert result.status == "BLOCKED"
    assert result.limitation == "inference-smoke offer reuses a prior inference machine"
    assert provider.create_calls == 0
    assert "arm" not in events


@pytest.mark.parametrize("machine_id", (99239, 17545, 150513, 147086))
def test_inference_smoke_rejects_excluded_historical_machines_before_guard_or_create(tmp_path: Path, machine_id: int) -> None:
    now = datetime.now(UTC)
    request, offer = _request(tmp_path, now)
    request = replace(request, stage="inference-smoke", reserve=Decimal("0.50"), budget_category="gpu-inference-smoke")
    provider_payload = json.loads(request.gates.provider_preflight.read_text())
    provider_payload["offers"][0]["machine_id"] = machine_id
    request.gates.provider_preflight.write_text(json.dumps(provider_payload), encoding="utf-8")
    offer = replace(offer, machine_id=machine_id)
    dispatcher, provider, events = _dispatcher(tmp_path, request, offer)

    result = dispatcher.run(request)

    assert result.status == "BLOCKED"
    assert result.limitation == "inference-smoke offer uses an excluded historical machine"
    assert provider.create_calls == 0
    assert "arm" not in events


def test_historical_machine_ids_require_hash_sealed_ledger_bound_manifests(tmp_path: Path) -> None:
    run_id = "gpu-smoke-history"
    directory = tmp_path / run_id
    directory.mkdir()
    manifest = directory / "run-manifest.json"
    manifest.write_text(json.dumps({"run_id": run_id, "stage": "gpu-smoke", "provider_create_calls": 1, "offer_contract": {"machine_id": 99239}}) + "\n")
    sums = f"{hashlib.sha256(manifest.read_bytes()).hexdigest()}  run-manifest.json\n"
    (directory / "SHA256SUMS").write_text(sums)
    root_hash = hashlib.sha256(sums.encode()).hexdigest()
    (directory / "ROOT-HASH.txt").write_text(root_hash + "\n")
    (directory / "guard-anchor.json").write_text(json.dumps({"root_hash": root_hash, "acknowledged": root_hash}) + "\n")
    pins = {run_id: (root_hash, 99239)}

    assert _historical_smoke_machine_ids(tmp_path, (run_id,), expected_anchors=pins) == frozenset({99239})
    manifest.write_text(manifest.read_text().replace("99239", "147086"))
    forged_sums = f"{hashlib.sha256(manifest.read_bytes()).hexdigest()}  run-manifest.json\n"
    forged_root = hashlib.sha256(forged_sums.encode()).hexdigest()
    (directory / "SHA256SUMS").write_text(forged_sums)
    (directory / "ROOT-HASH.txt").write_text(forged_root + "\n")
    (directory / "guard-anchor.json").write_text(json.dumps({"root_hash": forged_root, "acknowledged": forged_root}) + "\n")
    with pytest.raises(LiveDispatchError, match="signed-source pin"):
        _historical_smoke_machine_ids(tmp_path, (run_id,), expected_anchors=pins)


def test_distinct_machine_dispatch_blocks_historical_machine_before_guard_or_create(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    request, offer = _request(tmp_path, now)
    request = replace(request, reserve=Decimal("0.25"), budget_category="gpu-smoke-distinct-machine")
    dispatcher, provider, events = _dispatcher(tmp_path, request, offer)
    prior_run = "gpu-smoke-prior"
    dispatcher.ledger.reserve(prior_run, Decimal("0.50"), "gpu-smoke")
    monkeypatch.setattr(live_dispatch_module, "_historical_smoke_machine_ids", lambda *_args, **_kwargs: frozenset({offer.machine_id}))

    result = dispatcher.run(request)

    assert result.status == "BLOCKED"
    assert result.limitation == "distinct-machine smoke offer reuses a historical paid-smoke machine"
    assert provider.create_calls == 0
    assert "arm" not in events


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
    assert events.index("arm") < events.index("report-session-preflight") < events.index("create")
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
    journal = (result.manifest_path.parent / "journal" / "journal.ndjson").read_text()
    assert f'"machine_id":{offer.machine_id}' in journal
    assert f'"dph_total":"{offer.dph_total}"' in journal


def test_unauthenticated_report_session_blocks_after_guard_before_paid_create(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    request, offer = _request(tmp_path, now)
    dispatcher, provider, events = _dispatcher(tmp_path, request, offer, report_session_ok=False)

    result = dispatcher.run(request)

    assert result.status == "FAILED_SAFE"
    assert provider.create_calls == 0
    assert "arm" in events
    assert "report-session-preflight" in events
    assert "create" not in events
    assert "authenticated provider session" in (result.limitation or "")


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


def test_tampered_host_startup_fault_artifact_never_reaches_report_gate(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    request, offer = _request(tmp_path, now)
    events: list[str] = []
    provider = Provider(offer, events)
    dispatcher = LiveCanaryDispatcher(
        provider=provider,
        guard=Guard(request.nonce, events),
        report_gate=ReportGate(Reporter(events)),
        workload=HostStartupFaultWorkload(tamper_label=True),
        ledger=ExposureLedger(tmp_path / "host-fault-ledger.json"),
        output_root=tmp_path / "host-fault-runs",
        clock=Clock(request.hard_deadline - timedelta(minutes=20) + timedelta(seconds=1)),
        absence_interval_seconds=0,
    )

    result = dispatcher.run(request)

    assert result.status == "FAILED_SAFE"
    assert "not bound to the exact run and instance" in (result.limitation or "")
    assert "report-submit" not in events
    assert events.index("destroy") > events.index("create")


def test_post_create_machine_mismatch_reports_before_teardown_without_running_workload(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    request, offer = _request(tmp_path, now)
    dispatcher, provider, events = _dispatcher(tmp_path, request, offer, machine_mismatch=True)

    result = dispatcher.run(request)

    assert result.status == "FAILED_SAFE"
    assert provider.create_calls == 1
    assert events.index("report-submit") < events.index("destroy")
    journal = (result.manifest_path.parent / "journal" / "journal.ndjson").read_text()
    assert '"expected_machine_id":99' in journal
    assert '"observed_machine_id":147086' in journal
    assert "canary.started" not in journal


def test_external_guard_teardown_race_proves_absence_without_claiming_controller_destroy(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    request, offer = _request(tmp_path, now)
    dispatcher, _provider, _events = _dispatcher(tmp_path, request, offer, external_absence=True)

    result = dispatcher.run(request)

    assert result.status == "FAILED_SAFE"
    assert result.provider_destroy_calls == 0
    assert result.absence_reads == 3
    assert "external guard teardown not locally attributable" in (result.limitation or "")
    journal = (result.manifest_path.parent / "journal" / "journal.ndjson").read_text()
    assert "absence.proved_external_teardown" in journal


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
