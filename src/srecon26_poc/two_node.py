"""Fail-closed two-node lease for the live Kubernetes autoscaling experiment.

Both independent guards must attest before the first paid create.  A workload
callback runs only after both exact labels are observed.  Cleanup always proves
absence for each observed instance, so a failed worker never strands server.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Protocol

from .contracts import InstanceContract, OfferContract
from .guard import Guard, validate_attestation
from .provider import AmbiguousCreate
from .types import RunIdentity
from .vast_provider import VastLaunchContract


class TwoNodeError(RuntimeError):
    pass


class TwoNodeProvider(Protocol):
    def create_once(self, contract: OfferContract, request_key: str, launch: VastLaunchContract) -> InstanceContract: ...
    def reconcile_label(self, label: str) -> InstanceContract: ...
    def destroy_exact(self, instance_id: int, expected_label: str) -> None: ...
    def list_instances(self) -> tuple[InstanceContract, ...]: ...


@dataclass(frozen=True, slots=True)
class NodeLease:
    role: str
    run_id: str
    nonce: str
    offer: OfferContract

    def validate(self) -> None:
        if self.role not in {"server", "worker"}:
            raise TwoNodeError("node role must be server or worker")
        if not self.run_id or not self.nonce or not self.offer.label.endswith(f"--nonce-{self.nonce}"):
            raise TwoNodeError("node lease lacks a nonce-bound label")


@dataclass(frozen=True, slots=True)
class TwoNodeResult:
    server: InstanceContract | None
    worker: InstanceContract | None
    workload_completed: bool
    absence_reads: dict[str, int]


class TwoNodeLease:
    def __init__(
        self,
        *,
        provider: TwoNodeProvider,
        server: NodeLease,
        worker: NodeLease,
        server_guard: Guard,
        worker_guard: Guard,
        launch: VastLaunchContract,
        hard_deadline: datetime,
        now: Callable[[], datetime],
    ) -> None:
        self.provider, self.server, self.worker = provider, server, worker
        self.server_guard, self.worker_guard = server_guard, worker_guard
        self.launch, self.hard_deadline, self.now = launch, hard_deadline, now

    def _validate(self) -> None:
        self.server.validate()
        self.worker.validate()
        if self.server.nonce == self.worker.nonce or self.server.offer.label == self.worker.offer.label:
            raise TwoNodeError("two nodes require distinct nonce-bound labels")
        if self.server.offer.offer_id == self.worker.offer.offer_id or self.server.offer.machine_id == self.worker.offer.machine_id:
            raise TwoNodeError("two nodes require distinct offers and machines")
        if self.hard_deadline.tzinfo is None or self.hard_deadline.astimezone(UTC) <= self.now().astimezone(UTC):
            raise TwoNodeError("two-node deadline must be future and timezone-aware")
        self.launch.validate()

    def _arm(self, lease: NodeLease, guard: Guard) -> None:
        identity = RunIdentity(lease.run_id, lease.offer.label, self.now())
        attestation = guard.arm(identity, self.hard_deadline)
        validate_attestation(identity, attestation)
        if attestation.nonce != lease.nonce or attestation.hard_deadline.astimezone(UTC) != self.hard_deadline.astimezone(UTC):
            raise TwoNodeError("guard attestation does not bind exact node lease")

    def _create(self, lease: NodeLease) -> InstanceContract:
        try:
            observed = self.provider.create_once(lease.offer, lease.run_id, self.launch)
        except AmbiguousCreate:
            observed = self.provider.reconcile_label(lease.offer.label)
        if observed.label != lease.offer.label:
            raise TwoNodeError("provider observed a mismatched node label")
        return observed

    def _prove_absent(self, instance: InstanceContract) -> int:
        for _ in range(3):
            if any(current.instance_id == instance.instance_id or current.label == instance.label for current in self.provider.list_instances()):
                raise TwoNodeError("node remains present during three-read absence proof")
        return 3

    def run(self, workload: Callable[[InstanceContract, InstanceContract], None], *, heartbeat: Callable[[], None] | None = None) -> TwoNodeResult:
        self._validate()
        # Never create a server until both independently operated guards attest.
        self._arm(self.server, self.server_guard)
        self._arm(self.worker, self.worker_guard)
        server: InstanceContract | None = None
        worker: InstanceContract | None = None
        completed = False
        absence: dict[str, int] = {}
        error: BaseException | None = None
        try:
            if heartbeat is not None:
                heartbeat()
            server = self._create(self.server)
            if heartbeat is not None:
                heartbeat()
            worker = self._create(self.worker)
            workload(server, worker)
            completed = True
        except BaseException as caught:
            error = caught
        finally:
            cleanup_error: BaseException | None = None
            for instance in (worker, server):
                if instance is None:
                    continue
                try:
                    self.provider.destroy_exact(instance.instance_id, instance.label)
                    absence[instance.label] = self._prove_absent(instance)
                except BaseException as caught:
                    cleanup_error = cleanup_error or caught
            if cleanup_error is not None:
                raise TwoNodeError("two-node teardown or absence proof failed") from cleanup_error
        if error is not None:
            raise TwoNodeError("two-node workload failed after safe cleanup") from error
        return TwoNodeResult(server, worker, completed, absence)
