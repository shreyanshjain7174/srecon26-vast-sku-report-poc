"""Fail-closed lease for one 8-GPU Vast host.

Order is fixed: empty inventory, one armed guard, one paid create (ambiguity is
reconciled by label only), exact verification, workload, one exact destroy, and
a three-read absence proof.  Cleanup always runs once a create was attempted.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Event, Thread
from time import monotonic
from typing import Callable, Protocol

from .guard import Guard, validate_attestation
from .multigpu_contracts import MultiGpuPlan
from .provider import AmbiguousCreate
from .types import RunIdentity
from .vast_sdk_adapter import SdkInstance, SdkLaunchContract, SdkOffer, VastSdkError

ABSENCE_READS = 3
ABSENCE_INTERVAL_SECONDS = 5.0
CREATE_HEARTBEAT_INTERVAL_SECONDS = 45.0
# Half the guard's 600 s heartbeat timeout: transient RPC failures inside it are retried.
HEARTBEAT_FAILURE_BUDGET_SECONDS = 300.0

_OFFER_PLAN_FIELDS = (
    ("offer_id", "offer_id"),
    ("machine_id", "machine_id"),
    ("gpu_name", "gpu_name"),
    ("num_gpus", "gpu_count"),
    ("gpu_ram_mib", "gpu_ram_mib"),
    ("compute_capability", "compute_capability"),
    ("dph_total", "dph"),
    ("reliability", "reliability"),
    ("direct_port_count", "direct_port_count"),
    ("inet_down_mbps", "inet_down_mbps"),
)
_INSTANCE_OFFER_FIELDS = ("machine_id", "gpu_name", "num_gpus", "gpu_ram_mib", "compute_capability")


class SingleHostError(RuntimeError):
    pass


class SingleHostProvider(Protocol):
    def create_once(self, offer: SdkOffer, contract: SdkLaunchContract) -> SdkInstance: ...
    def reconcile_label(self, label: str) -> SdkInstance | None: ...
    def get_instance(self, instance_id: int) -> SdkInstance: ...
    def list_instances(self) -> tuple[SdkInstance, ...]: ...
    def destroy_exact(self, instance_id: int, expected_label: str) -> None: ...


@dataclass(frozen=True, slots=True)
class SingleHostResult:
    instance: SdkInstance
    workload_completed: bool
    absence_reads: int


def _label_binds_nonce(label: str, nonce: str) -> bool:
    prefix = label.removesuffix(f"--nonce-{nonce}")
    return bool(prefix) and prefix != label and "--nonce-" not in prefix


class SingleHostLease:
    def __init__(
        self,
        *,
        provider: SingleHostProvider,
        offer: SdkOffer,
        plan: MultiGpuPlan,
        launch: SdkLaunchContract,
        guard: Guard,
        run_id: str,
        nonce: str,
        hard_deadline: datetime,
        now: Callable[[], datetime],
        sleep: Callable[[float], None],
    ) -> None:
        self.provider, self.offer, self.plan, self.launch, self.guard = provider, offer, plan, launch, guard
        self.run_id, self.nonce, self.hard_deadline = run_id, nonce, hard_deadline
        self.now, self.sleep = now, sleep

    def _validate(self) -> None:
        if not isinstance(self.offer, SdkOffer) or not isinstance(self.plan, MultiGpuPlan) or not isinstance(self.launch, SdkLaunchContract):
            raise SingleHostError("single-host lease requires SdkOffer, MultiGpuPlan, and SdkLaunchContract")
        if not isinstance(self.run_id, str) or not self.run_id.strip() or not isinstance(self.nonce, str) or not self.nonce.strip():
            raise SingleHostError("single-host lease requires a run id and nonce")
        if not isinstance(self.offer.label, str) or not _label_binds_nonce(self.offer.label, self.nonce):
            raise SingleHostError("offer label is not bound to this exact nonce")
        for offer_field, plan_field in _OFFER_PLAN_FIELDS:
            if getattr(self.offer, offer_field) != getattr(self.plan, plan_field):
                raise SingleHostError(f"offer {offer_field} does not match plan {plan_field}")
        if (
            not isinstance(self.hard_deadline, datetime)
            or self.hard_deadline.tzinfo is None
            or self.hard_deadline.utcoffset() is None
            or self.hard_deadline.astimezone(UTC) <= self.now().astimezone(UTC)
        ):
            raise SingleHostError("single-host deadline must be future and timezone-aware")
        try:
            self.launch.validate()
        except VastSdkError as error:
            raise SingleHostError("launch contract is invalid") from error
        if self.launch.image != self.plan.vllm_image or self.launch.disk_gib != self.plan.disk_gib:
            raise SingleHostError("launch image and disk must match the plan")

    def _arm(self) -> None:
        identity = RunIdentity(self.run_id, self.offer.label, self.now())
        attestation = self.guard.arm(identity, self.hard_deadline)
        deadline = attestation.hard_deadline
        if not isinstance(deadline, datetime) or deadline.tzinfo is None or deadline.utcoffset() is None:
            raise SingleHostError("guard attestation deadline must be timezone-aware")
        validate_attestation(identity, attestation)
        if (
            attestation.nonce != self.nonce
            or attestation.label != self.offer.label
            or deadline.astimezone(UTC) != self.hard_deadline.astimezone(UTC)
        ):
            raise SingleHostError("guard attestation does not bind exact nonce, label, and deadline")

    def _verify(self, observed: SdkInstance) -> None:
        if observed.label != self.offer.label:
            raise SingleHostError("provider observed a mismatched instance label")
        for name in _INSTANCE_OFFER_FIELDS:
            if getattr(observed, name) != getattr(self.offer, name):
                raise SingleHostError(f"observed instance {name} does not match offer")
        if observed.dph_total > self.offer.dph_total:
            raise SingleHostError("observed instance price increased above offer")

    def _present(self, ids: frozenset[int]) -> bool:
        return any(current.instance_id in ids or current.label == self.offer.label for current in self.provider.list_instances())

    def _cleanup(self, observed: SdkInstance | None, owned: SdkInstance | None) -> int:
        ids = frozenset() if observed is None else frozenset({observed.instance_id})
        if owned is not None:
            try:
                self.provider.destroy_exact(owned.instance_id, owned.label)
            except BaseException as caught:
                # The independently armed guard may have won the destroy race.
                if self._present(ids):
                    raise SingleHostError("instance remains after failed destroy") from caught
        for _ in range(ABSENCE_READS):
            self.sleep(ABSENCE_INTERVAL_SECONDS)
            current = self.provider.list_instances()
            if observed is None and owned is None:
                matches = [instance for instance in current if instance.label == self.offer.label]
                if len(matches) == 1:
                    return self._cleanup(matches[0], matches[0])
            if any(instance.instance_id in ids or instance.label == self.offer.label for instance in current):
                raise SingleHostError("instance remains during three-read absence proof")
        return ABSENCE_READS

    def run(self, workload: Callable[[SdkInstance], None], *, heartbeat: Callable[[], None] | None = None) -> SingleHostResult:
        self._validate()
        if self.provider.list_instances():
            raise SingleHostError("provider inventory must be empty before arm and create")
        self._arm()
        if heartbeat is not None:
            heartbeat()
        observed: SdkInstance | None = None
        # Only an instance carrying our exact label is ours to destroy.
        owned: SdkInstance | None = None
        completed = False
        error: BaseException | None = None
        beat_stop = Event()
        beat_errors: list[BaseException] = []
        beat_thread: Thread | None = None
        if heartbeat is not None:
            def keep_alive() -> None:
                last_success = monotonic()
                while not beat_stop.wait(CREATE_HEARTBEAT_INTERVAL_SECONDS):
                    try:
                        heartbeat()
                        last_success = monotonic()
                    except BaseException as caught:
                        if monotonic() - last_success > HEARTBEAT_FAILURE_BUDGET_SECONDS:
                            beat_errors.append(caught)
                            return

            beat_thread = Thread(target=keep_alive, name="multigpu-create-heartbeat", daemon=True)
            beat_thread.start()
        try:
            try:
                try:
                    observed = self.provider.create_once(self.offer, self.launch)
                except AmbiguousCreate:
                    observed = self.provider.reconcile_label(self.offer.label)
            finally:
                beat_stop.set()
                if beat_thread is not None:
                    beat_thread.join()
            if observed is None:
                raise SingleHostError("ambiguous create reconciled to no instance")
            if observed.label == self.offer.label:
                owned = observed
            if beat_errors:
                raise SingleHostError("guard heartbeat failed during create") from beat_errors[0]
            self._verify(observed)
            if heartbeat is not None:
                heartbeat()
            workload(observed)
            completed = True
        except BaseException as caught:
            error = caught
        try:
            absence = self._cleanup(observed, owned)
        except BaseException as caught:
            raise SingleHostError("multi-GPU teardown or absence proof failed") from caught
        if error is not None:
            raise SingleHostError("multi-GPU workload failed after safe cleanup") from error
        assert observed is not None
        return SingleHostResult(observed, completed, absence)
