from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .contracts import InstanceContract, OfferContract, ProbeOutcome, classify_fault
from .guard import Guard, GuardAttestation, validate_attestation
from .journal import RunJournal
from .reporting import FaultRecord, ReportGate, ReportReceipt
from .types import FaultClass, RunState


class LifecycleHalted(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AbsenceProof:
    instance_id: int
    label: str
    reads: tuple[datetime, ...]


class _Provider(Protocol):
    def create_once(self, contract: OfferContract, request_key: str) -> InstanceContract: ...
    def list_instances(self) -> tuple[InstanceContract, ...]: ...
    def destroy_exact(self, instance_id: int, expected_label: str) -> None: ...


class ExperimentController:
    def __init__(self, journal: RunJournal, provider: _Provider, guard: Guard, report_gate: ReportGate | None) -> None:
        self.journal, self.provider, self.guard, self.report_gate = journal, provider, guard, report_gate

    def create_or_reconcile(self, contract: OfferContract) -> InstanceContract:
        state = self.journal.state()
        if state is RunState.GUARD_ARMED:
            attestation = self.guard.preflight()
            validate_attestation(self.journal._identity, attestation)
            self.journal.append(RunState.CREATE_REQUESTED, "provider.create_intent", {"label": contract.label}, datetime.now(UTC), 100)
            instance = self.provider.create_once(contract, self.journal._identity.run_id)
            self.journal.append(RunState.CREATED_VERIFYING, "provider.create_observed", {"instance_id": instance.instance_id}, datetime.now(UTC), 101)
            return instance
        if state in {RunState.CREATE_REQUESTED, RunState.CREATED_VERIFYING}:
            matches = [item for item in self.provider.list_instances() if item.label == contract.label]
            if len(matches) == 1:
                return matches[0]
            raise LifecycleHalted("create result is ambiguous and cannot be retried")
        raise LifecycleHalted(f"create is not allowed from {state.value}")

    def prove_absent(self, instance_id: int, label: str, reads: int = 3) -> AbsenceProof:
        if reads != 3:
            raise LifecycleHalted("exactly three absence reads are required")
        observed: list[datetime] = []
        for number in range(1, 4):
            if any(item.instance_id == instance_id or item.label == label for item in self.provider.list_instances()):
                raise LifecycleHalted("instance remains present during absence proof")
            observed.append(datetime.now(UTC))
        return AbsenceProof(instance_id, label, tuple(observed))

    def handle_probe_fault(self, offer: OfferContract, instance: InstanceContract, outcome: ProbeOutcome, nonce: str, report_start_by: datetime) -> ReportReceipt | None:
        fault = classify_fault(offer, instance, outcome)
        if fault is FaultClass.PROVIDER_FAULT_CONFIRMED and self.report_gate is not None:
            return self.report_gate.handle(FaultRecord(instance.instance_id, instance.label, nonce, "contract mismatch"), report_start_by)
        return None


def load_provider_secret(path: Path) -> str:
    """Load only a root-owned 0600 external secret; never read repo, env, argv, or logs."""
    resolved = path.resolve()
    if ".git" in resolved.parts or (os.stat(resolved).st_mode & 0o077) or os.stat(resolved).st_uid != 0:
        raise LifecycleHalted("provider credential source is unsafe")
    return resolved.read_text(encoding="utf-8").strip()
