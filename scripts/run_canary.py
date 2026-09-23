#!/usr/bin/env python3
"""Fixture simulator plus injected, gate-bound live canary entry point.

The default command stays fixture-only.  A paid dispatch has no defaults and
needs an explicitly supplied integration factory that constructs independently
reviewed provider, guard, report, and remote-workload dependencies.
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from srecon26_poc.budget import BudgetExceeded, ExposureLedger
from srecon26_poc.canary import (
    VLLM_IMAGE_DIGEST,
    CanarySnapshot,
    KvmFacts,
    MetricSample,
    evaluate_canary_readiness,
    evaluate_kvm_capabilities,
)
from srecon26_poc.contracts import InstanceContract, OfferContract, ProbeOutcome, classify_fault
from srecon26_poc.controller import ExperimentController, LifecycleHalted
from srecon26_poc.guard import GuardAttestation
from srecon26_poc.journal import RunJournal
from srecon26_poc.live_dispatch import GateArtifactPaths, LiveCanaryDispatcher, LiveCanaryRequest, WorkloadContract
from srecon26_poc.provider import AmbiguousCreate
from srecon26_poc.reporting import FaultRecord, ReportGate, ReportReceipt
from srecon26_poc.types import FaultClass, RunIdentity, RunState
from srecon26_poc.vast_provider import VastLaunchContract


NON_GPU_PROVENANCE = "offline-fixture"
REPORT_MARGIN = timedelta(seconds=165)  # 60 report + 60 teardown + 45 absence
REQUIRED_GATES = (
    "phase1_passed",
    "semgrep_current",
    "no_spend_preflight_current",
    "independent_guard_attested",
    "report_fixture_verified",
    "exact_offer_contract",
    "inventory_safe",
    "ledger_available",
    "security_current",
    "no_safety_breach",
    "smoke_finalized_for_metric_path",
)


class FixtureScenario(StrEnum):
    SUCCESS = "success"
    SKU_FAULT = "sku-fault"
    UNRESOLVED_DIAGNOSIS = "unresolved-diagnosis"
    REPORT_TIMEOUT = "report-timeout"
    CREATE_AMBIGUITY = "create-ambiguity"
    DEADLINE_TEARDOWN = "deadline-teardown"
    KVM_FAILURE = "kvm-failure"
    READINESS_FAILURE = "readiness-failure"


@dataclass(frozen=True, slots=True)
class GateEvidence:
    """Fresh, explicit inputs required before a lifecycle could ever create."""

    phase1_status: str = "passed"
    semgrep_current: bool = True
    no_spend_preflight_current: bool = True
    independent_guard_attested: bool = True
    report_fixture_verified: bool = True
    offer: OfferContract | None = None
    inventory: tuple[InstanceContract, ...] = ()
    ledger_available: bool = True
    security_current: bool = True
    safety_breach: bool = False
    smoke_finalized: bool = True


@dataclass(frozen=True, slots=True)
class CanaryResult:
    status: str
    limitation: str | None
    manifest_path: Path
    provider_create_calls: int
    provider_destroy_calls: tuple[tuple[int, str], ...]
    absence_reads: int
    provenance: str
    hard_deadline: datetime | None
    report_start_by: datetime | None


class FixtureClock:
    def __init__(self, current: datetime) -> None:
        if current.tzinfo is None:
            raise ValueError("fixture clock must be timezone-aware")
        self.current = current.astimezone(UTC)
        self.monotonic_ns = 0

    def now(self) -> datetime:
        return self.current

    def tick(self, seconds: int = 1) -> int:
        self.current += timedelta(seconds=seconds)
        self.monotonic_ns += seconds * 1_000_000_000
        return self.monotonic_ns


class FixtureProvider:
    """In-memory provider that makes fixture mutation observable and harmless."""

    def __init__(self, scenario: FixtureScenario, offer: OfferContract) -> None:
        self.scenario, self.offer = scenario, offer
        self.instances: list[InstanceContract] = []
        self.create_calls = 0
        self.destroy_calls: list[tuple[int, str]] = []

    def create_once(self, contract: OfferContract, request_key: str) -> InstanceContract:
        del request_key
        self.create_calls += 1
        if self.create_calls != 1:
            raise AssertionError("fixture attempted a forbidden create retry")
        if self.scenario is FixtureScenario.CREATE_AMBIGUITY:
            raise AmbiguousCreate("fixture create response timed out before an instance was observed")
        instance = InstanceContract(
            417,
            contract.gpu_name,
            contract.num_gpus,
            contract.gpu_ram_mib,
            contract.compute_capability,
            contract.machine_id,
            contract.dph_total,
            contract.label,
        )
        if self.scenario in {FixtureScenario.SKU_FAULT, FixtureScenario.REPORT_TIMEOUT}:
            instance = replace(instance, gpu_name="fixture-mismatched-gpu")
        self.instances = [instance]
        return instance

    def list_instances(self) -> tuple[InstanceContract, ...]:
        return tuple(self.instances)

    def destroy_exact(self, instance_id: int, expected_label: str) -> None:
        matches = [item for item in self.instances if item.instance_id == instance_id and item.label == expected_label]
        if len(matches) != 1:
            raise LifecycleHalted("fixture refused a non-exact teardown")
        self.destroy_calls.append((instance_id, expected_label))
        self.instances = [item for item in self.instances if item.instance_id != instance_id]


class FixtureGuard:
    def __init__(self, nonce: str, clock: FixtureClock) -> None:
        self.nonce, self.clock = nonce, clock
        self.identity: RunIdentity | None = None
        self.deadline: datetime | None = None
        self.heartbeats: list[int] = []
        self.anchors: list[str] = []

    def arm(self, identity: RunIdentity, hard_deadline: datetime) -> GuardAttestation:
        self.identity, self.deadline = identity, hard_deadline
        return self.preflight()

    def preflight(self) -> GuardAttestation:
        if self.identity is None or self.deadline is None:
            raise LifecycleHalted("fixture guard was not armed")
        return GuardAttestation("fixture-independent-guard", "fixture-script-sha256", self.nonce, self.identity.label, self.deadline, self.clock.now())

    def record_heartbeat(self, identity: RunIdentity, monotonic_ns: int) -> None:
        if self.identity != identity or self.clock.now() >= (self.deadline or self.clock.now()):
            raise LifecycleHalted("fixture guard rejected heartbeat after deadline")
        self.heartbeats.append(monotonic_ns)

    def anchor(self, root_hash: str) -> str:
        self.anchors.append(root_hash)
        return root_hash


class FixtureReportAdapter:
    def __init__(self, *, timeout: bool = False) -> None:
        self.timeout, self.events = timeout, []

    def preflight_exact_instance(self, instance_id: int, label: str) -> None:
        self.events.append("preflight")
        if instance_id <= 0 or not label:
            raise ValueError("fixture report target is invalid")

    def capture_before(self, fault: FaultRecord) -> Path:
        self.events.append("before")
        return Path("fixture-before.json")

    def submit(self, fault: FaultRecord) -> bool:
        self.events.append("submit")
        if self.timeout:
            raise TimeoutError("fixture report timed out")
        return True

    def capture_after(self, receipt: ReportReceipt) -> Path:
        self.events.append("after")
        return Path("fixture-after.json")


def fixture_offer(label: str) -> OfferContract:
    return OfferContract(101, "fixture-RTX", 1, 24576, "8.6", 99, Decimal("0.30"), label)


def default_gate_evidence(label: str) -> GateEvidence:
    return GateEvidence(offer=fixture_offer(label))


def evaluate_gates(evidence: GateEvidence, *, label: str, stage: str) -> tuple[str, ...]:
    """Return every failed prerequisite.  An empty tuple is the only pass."""

    failures: list[str] = []
    if evidence.phase1_status != "passed":
        failures.append("phase1_passed")
    if not evidence.semgrep_current:
        failures.append("semgrep_current")
    if not evidence.no_spend_preflight_current:
        failures.append("no_spend_preflight_current")
    if not evidence.independent_guard_attested:
        failures.append("independent_guard_attested")
    if not evidence.report_fixture_verified:
        failures.append("report_fixture_verified")
    if evidence.offer is None:
        failures.append("exact_offer_contract")
    if any(item.label != label for item in evidence.inventory):
        failures.append("inventory_safe")
    if not evidence.ledger_available:
        failures.append("ledger_available")
    if not evidence.security_current:
        failures.append("security_current")
    if evidence.safety_breach:
        failures.append("no_safety_breach")
    if stage == "metric-path" and not evidence.smoke_finalized:
        failures.append("smoke_finalized_for_metric_path")
    return tuple(failures)


def _stamp(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def _append(journal: RunJournal, clock: FixtureClock, state: RunState, event: str, payload: Mapping[str, object]) -> None:
    clock.tick()
    journal.append(state, event, dict(payload), clock.now(), clock.monotonic_ns)


def _write_manifest(path: Path, payload: Mapping[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return path


def _fixture_facts(now: datetime) -> KvmFacts:
    return KvmFacts(True, True, True, True, True, True, True, VLLM_IMAGE_DIGEST, now)


def _fixture_snapshot(now: datetime) -> CanarySnapshot:
    sample = MetricSample(Decimal("1"), now)
    return CanarySnapshot(1, True, True, 1, True, sample, sample, sample, True, True)


def run_fixture(
    *,
    output_root: Path,
    stage: str,
    scenario: FixtureScenario = FixtureScenario.SUCCESS,
    reserve: Decimal = Decimal("1.00"),
    evidence: GateEvidence | None = None,
    now: datetime | None = None,
    run_id: str = "fixture-run",
) -> CanaryResult:
    """Exercise a complete offline lifecycle with an in-memory provider only."""

    if stage not in {"gpu-smoke", "metric-path"}:
        raise ValueError("stage must be gpu-smoke or metric-path")
    if not isinstance(reserve, Decimal) or not reserve.is_finite() or reserve <= 0:
        raise ValueError("reserve must be a positive Decimal")
    if reserve > Decimal("1.00"):
        raise ValueError("reserve exceeds the stage ceiling")
    started = (now or datetime.now(UTC)).astimezone(UTC)
    clock = FixtureClock(started)
    label = f"srecon26-{stage}--nonce-fixture-nonce-01234567"
    run_dir = Path(output_root) / run_id
    manifest_path = run_dir / "run-manifest.json"
    identity = RunIdentity(run_id, label, clock.now())
    journal = RunJournal.create(run_dir / "journal", identity)
    evidence = evidence or default_gate_evidence(label)
    gate_failures = evaluate_gates(evidence, label=label, stage=stage)
    common: dict[str, object] = {
        "run_id": run_id,
        "stage": stage,
        "scenario": scenario.value,
        "provenance": NON_GPU_PROVENANCE,
        "real_gpu_claim": False,
        "provider_dispatch": "fixture-in-memory-only",
        "gate_failures": list(gate_failures),
        "required_gates": list(REQUIRED_GATES),
        "created_at": _stamp(started),
    }
    if gate_failures:
        _append(journal, clock, RunState.TERMINAL, "gate.blocked", {"failed_gates": list(gate_failures)})
        manifest_path = _write_manifest(manifest_path, {**common, "status": "BLOCKED", "limitation": "missing current gate evidence", "journal_state": journal.state().value, "provider_create_calls": 0, "provider_destroy_calls": [], "absence_reads": 0})
        return CanaryResult("BLOCKED", "missing current gate evidence", manifest_path, 0, (), 0, NON_GPU_PROVENANCE, None, None)

    offer = evidence.offer
    assert offer is not None  # established by evaluate_gates
    hard_deadline = started + timedelta(minutes=20)
    report_start_by = hard_deadline - REPORT_MARGIN
    provider = FixtureProvider(scenario, offer)
    guard = FixtureGuard("fixture-nonce-01234567", clock)
    report_adapter = FixtureReportAdapter(timeout=scenario is FixtureScenario.REPORT_TIMEOUT)
    controller = ExperimentController(journal, provider, guard, ReportGate(report_adapter))
    ledger = ExposureLedger(Path(output_root) / "fixture-exposure-ledger.json")
    category = "gpu-smoke" if stage == "gpu-smoke" else "canary"
    status, limitation, instance, absence_reads = "FAILED_SAFE", None, None, 0
    report_receipt: ReportReceipt | None = None
    try:
        _append(journal, clock, RunState.OFFLINE_VALIDATED, "gates.passed", {"gates": list(REQUIRED_GATES)})
        ledger.reserve(run_id, reserve, category)
        _append(journal, clock, RunState.BUDGET_RESERVED, "budget.reserved", {"reserve": str(reserve), "category": category})
        _append(journal, clock, RunState.OFFER_PINNED, "offer.pinned", {"offer_id": offer.offer_id, "label": offer.label})
        _append(journal, clock, RunState.REPORT_ADAPTER_READY, "report.fixture_ready", {"fixture": True})
        guard.arm(identity, hard_deadline)
        _append(journal, clock, RunState.GUARD_ARMED, "guard.armed", {"hard_deadline": _stamp(hard_deadline), "report_start_by": _stamp(report_start_by)})
        try:
            instance = controller.create_or_reconcile(offer)
        except AmbiguousCreate:
            # Reconciliation may observe exactly one nonce-bound instance; it
            # must never issue a second create.
            try:
                instance = controller.create_or_reconcile(offer)
            except LifecycleHalted as exc:
                limitation = str(exc)
        except LifecycleHalted as exc:
            limitation = str(exc)
        if instance is None:
            # A zero/multiple reconciliation result has no exact destruction
            # target.  It still gets three independent absence reads.
            for _ in range(3):
                if any(item.label == label for item in provider.list_instances()):
                    raise LifecycleHalted("ambiguous create left an owned instance visible")
                clock.tick()
                absence_reads += 1
            _append(journal, clock, RunState.TERMINAL, "create.ambiguous_safe", {"limitation": limitation or "create outcome is ambiguous"})
            status, limitation = "HALTED", limitation or "create outcome is ambiguous"
        else:
            _append(journal, clock, RunState.RUNNING_CANARY, "canary.started", {"instance_id": instance.instance_id})
            guard.record_heartbeat(identity, clock.tick())
            if scenario is FixtureScenario.DEADLINE_TEARDOWN:
                # Reserve the final three simulated seconds for exact destroy,
                # three-read proof, and terminal journal commit.  This models
                # the independently armed guard taking over at, not after, the
                # immutable deadline.
                clock.current = hard_deadline - timedelta(seconds=3)
                limitation = "hard deadline reached before canary completion"
            elif scenario is FixtureScenario.KVM_FAILURE:
                decision = evaluate_kvm_capabilities(replace(_fixture_facts(clock.now()), systemd=False), now=clock.now())
                limitation = "; ".join(decision.blockers)
            else:
                capability = evaluate_kvm_capabilities(_fixture_facts(clock.now()), now=clock.now())
                if not capability.allowed:
                    limitation = "; ".join(capability.blockers)
                elif stage == "metric-path":
                    snapshot = _fixture_snapshot(clock.now())
                    if scenario is FixtureScenario.READINESS_FAILURE:
                        snapshot = replace(snapshot, device_plugin_ready=False)
                    readiness = evaluate_canary_readiness(snapshot, now=clock.now())
                    if not readiness.measurement_complete:
                        limitation = "; ".join(readiness.blockers) or "measured vLLM request did not complete"

            if limitation is None and scenario in {FixtureScenario.SKU_FAULT, FixtureScenario.REPORT_TIMEOUT}:
                _append(journal, clock, RunState.CAPTURING_FAULT, "fault.confirmed", {"class": FaultClass.PROVIDER_FAULT_CONFIRMED.value})
                if clock.now() >= report_start_by:
                    limitation, status = "report start deadline elapsed", "REPORT_UNCONFIRMED"
                else:
                    _append(journal, clock, RunState.REPORTING_FAULT, "report.started", {"report_start_by": _stamp(report_start_by)})
                    try:
                        report_receipt = controller.handle_probe_fault(offer, instance, ProbeOutcome.PASS, "fixture-nonce-01234567", report_start_by)
                        status = "FAILED_SAFE" if report_receipt and report_receipt.confirmed else "REPORT_UNCONFIRMED"
                    except (TimeoutError, ValueError):
                        limitation, status = "report fixture timed out", "REPORT_UNCONFIRMED"
            elif limitation is None and scenario is FixtureScenario.UNRESOLVED_DIAGNOSIS:
                assert classify_fault(offer, instance, ProbeOutcome.NETWORK_FAILED) is FaultClass.DIAGNOSIS_UNRESOLVED
                _append(journal, clock, RunState.CAPTURING_FAULT, "fault.unresolved", {"class": FaultClass.DIAGNOSIS_UNRESOLVED.value})
                limitation, status = "diagnosis unresolved; provider report forbidden", "FAILED_SAFE"
            elif limitation is None:
                _append(journal, clock, RunState.COLLECTING, "canary.evidence_captured", {"fixture": True})
                status = "COMPLETED"

            if journal.state() is not RunState.DESTROYING:
                _append(journal, clock, RunState.DESTROYING, "teardown.exact", {"instance_id": instance.instance_id, "label": label})
            provider.destroy_exact(instance.instance_id, label)
            proof = controller.prove_absent(instance.instance_id, label)
            absence_reads = len(proof.reads)
            _append(journal, clock, RunState.ABSENCE_VERIFYING, "absence.proved", {"reads": absence_reads})
            _append(journal, clock, RunState.TERMINAL, "terminal.safe", {"status": status, "limitation": limitation})
            ledger.commit_actual(run_id, Decimal("0.00"), {"reads": absence_reads, "fixture": True})
    except (BudgetExceeded, LifecycleHalted, TimeoutError, ValueError) as exc:
        limitation, status = str(exc), "FAILED_SAFE"
        if journal.state() is not RunState.TERMINAL:
            _append(journal, clock, RunState.TERMINAL, "runner.halted", {"limitation": limitation})

    payload = {
        **common,
        "status": status,
        "limitation": limitation,
        "hard_deadline": _stamp(hard_deadline),
        "report_start_by": _stamp(report_start_by),
        "offer_contract": {"offer_id": offer.offer_id, "label": offer.label, "dph_total": str(offer.dph_total)},
        "contract_and_gpu_facts": {"fixture_only": True, "gpu_name": instance.gpu_name if instance else None, "cuda": scenario not in {FixtureScenario.KVM_FAILURE}},
        "report": {"attempted": bool(report_receipt), "confirmed": report_receipt.confirmed if report_receipt else False, "events": report_adapter.events},
        "provider_create_calls": provider.create_calls,
        "provider_destroy_calls": [list(item) for item in provider.destroy_calls],
        "absence_reads": absence_reads,
        "journal_state": journal.state().value,
        "lifecycle_timestamps": {"started": _stamp(started), "terminal": _stamp(clock.now())},
        "cost": {"reserved": str(reserve), "actual": "0.00", "currency": "USD", "fixture_only": True},
    }
    manifest_path = _write_manifest(manifest_path, payload)
    return CanaryResult(status, limitation, manifest_path, provider.create_calls, tuple(provider.destroy_calls), absence_reads, NON_GPU_PROVENANCE, hard_deadline, report_start_by)


def run_blocked_paid_invocation(*, output_root: Path, stage: str, reserve: Decimal, now: datetime | None = None) -> CanaryResult:
    """Write an evidence-bearing refusal for every non-fixture CLI invocation."""

    started = (now or datetime.now(UTC)).astimezone(UTC)
    run_dir = Path(output_root) / "paid-dispatch-disabled"
    manifest_path = _write_manifest(
        run_dir / "run-manifest.json",
        {
            "stage": stage,
            "provenance": NON_GPU_PROVENANCE,
            "real_gpu_claim": False,
            "status": "BLOCKED",
            "limitation": "paid dispatch is intentionally unavailable until Plan 02-06",
            "requested_reserve": str(reserve),
            "created_at": _stamp(started),
            "provider_create_calls": 0,
            "provider_destroy_calls": [],
            "absence_reads": 0,
        },
    )
    return CanaryResult("BLOCKED", "paid dispatch is intentionally unavailable until Plan 02-06", manifest_path, 0, (), 0, NON_GPU_PROVENANCE, None, None)


def run_live_dispatch(*, dispatcher: LiveCanaryDispatcher, request: LiveCanaryRequest):
    """Run code-only injected dependencies through the fail-closed paid lifecycle.

    This deliberately accepts no credential, provider URL, shell command, or
    browser object.  Those privileged capabilities live behind the injected
    implementation and are reachable only after every artifact gate passes.
    """

    return dispatcher.run(request)


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _load_dispatcher(spec: str) -> LiveCanaryDispatcher:
    """Load an operator-owned integration factory; no repository default exists."""

    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("--dispatcher-factory must be module:callable")
    factory = getattr(importlib.import_module(module_name), attribute)
    dispatcher = factory()
    if not isinstance(dispatcher, LiveCanaryDispatcher):
        raise ValueError("dispatcher factory did not return LiveCanaryDispatcher")
    return dispatcher


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline-only bounded canary lifecycle simulator")
    parser.add_argument("--stage", choices=("gpu-smoke", "metric-path"), required=True)
    parser.add_argument("--reserve", type=Decimal, default=Decimal("1.00"))
    parser.add_argument("--require-zero-instances", action="store_true")
    parser.add_argument("--fixture", action="store_true", help="run only an in-memory fixture")
    parser.add_argument("--live", action="store_true", help="dispatch only through explicit gate artifacts and an injected integration factory")
    parser.add_argument("--scenario", choices=[item.value for item in FixtureScenario], default=FixtureScenario.SUCCESS.value)
    parser.add_argument("--output-root", type=Path, default=ROOT / "artifacts" / "runs")
    parser.add_argument("--run-id", default="fixture-run")
    parser.add_argument("--nonce")
    parser.add_argument("--label")
    parser.add_argument("--offer-id", type=int)
    parser.add_argument("--hard-deadline", type=_parse_utc)
    parser.add_argument("--phase1-verification", type=Path)
    parser.add_argument("--semgrep-artifact", type=Path)
    parser.add_argument("--provider-preflight", type=Path)
    parser.add_argument("--guard-attestation", type=Path)
    parser.add_argument("--report-fixture", type=Path)
    parser.add_argument("--smoke-manifest", type=Path)
    parser.add_argument("--vm-image-contract")
    parser.add_argument("--ubuntu-template-hash")
    parser.add_argument("--model-id")
    parser.add_argument("--model-revision")
    parser.add_argument("--vllm-image-digest")
    parser.add_argument("--dispatcher-factory", help="operator-owned module:callable returning injected live dependencies")
    args = parser.parse_args()
    try:
        if args.fixture and args.live:
            parser.error("--fixture and --live are mutually exclusive")
        if args.live:
            missing = [name for name, value in {
                "--nonce": args.nonce,
                "--label": args.label,
                "--offer-id": args.offer_id,
                "--hard-deadline": args.hard_deadline,
                "--phase1-verification": args.phase1_verification,
                "--semgrep-artifact": args.semgrep_artifact,
                "--provider-preflight": args.provider_preflight,
                "--guard-attestation": args.guard_attestation,
                "--report-fixture": args.report_fixture,
                "--ubuntu-template-hash": args.ubuntu_template_hash,
                "--vm-image-contract": args.vm_image_contract,
                "--model-id": args.model_id,
                "--model-revision": args.model_revision,
                "--vllm-image-digest": args.vllm_image_digest,
                "--dispatcher-factory": args.dispatcher_factory,
            }.items() if value is None]
            if not args.require_zero_instances:
                missing.append("--require-zero-instances")
            if args.stage == "metric-path" and args.smoke_manifest is None:
                missing.append("--smoke-manifest")
            if missing:
                parser.error("live dispatch requires " + ", ".join(missing))
            request = LiveCanaryRequest(
                args.stage,
                args.run_id,
                args.nonce,
                args.label,
                args.offer_id,
                args.reserve,
                args.hard_deadline,
                GateArtifactPaths(args.phase1_verification, args.semgrep_artifact, args.provider_preflight, args.guard_attestation, args.report_fixture, args.smoke_manifest),
                VastLaunchContract(ubuntu_template_hash=args.ubuntu_template_hash, image_contract=args.vm_image_contract),
                WorkloadContract(args.model_id, args.model_revision, args.vllm_image_digest),
            )
            result = run_live_dispatch(dispatcher=_load_dispatcher(args.dispatcher_factory), request=request)
            print(json.dumps({"status": result.status, "manifest": str(result.manifest_path), "provenance": result.provenance}, sort_keys=True))
            return 0 if result.status in {"COMPLETED", "FAILED_SAFE", "REPORT_UNCONFIRMED", "HALTED", "BLOCKED"} else 2
        result = run_fixture(output_root=args.output_root, stage=args.stage, scenario=FixtureScenario(args.scenario), reserve=args.reserve, run_id=args.run_id) if args.fixture else run_blocked_paid_invocation(output_root=args.output_root, stage=args.stage, reserve=args.reserve)
    except (InvalidOperation, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps({"status": result.status, "manifest": str(result.manifest_path), "provenance": result.provenance}, sort_keys=True))
    return 0 if args.fixture and result.status in {"COMPLETED", "FAILED_SAFE", "REPORT_UNCONFIRMED", "HALTED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
