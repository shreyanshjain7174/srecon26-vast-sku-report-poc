from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Event

import pytest

from srecon26_poc.guard import GuardAttestation, GuardRejected
from srecon26_poc.multigpu_contracts import VLLM_IMAGE, MultiGpuPlan
from srecon26_poc.provider import AmbiguousCreate
from srecon26_poc import single_host_multigpu
from srecon26_poc.single_host_multigpu import SingleHostError, SingleHostLease, SingleHostResult
from srecon26_poc.vast_sdk_adapter import SdkInstance, SdkLaunchContract, SdkOffer

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
DEADLINE = NOW + timedelta(minutes=120)
NONCE = "abc123def456"
LABEL = f"srecon26-mgpu--nonce-{NONCE}"
WORKLOAD_FAILED = "multi-GPU workload failed after safe cleanup"
TEARDOWN_FAILED = "multi-GPU teardown or absence proof failed"


def plan() -> MultiGpuPlan:
    return MultiGpuPlan(
        offer_id=101, machine_id=202, gpu_name="RTX 4090", gpu_count=8, gpu_ram_mib=24564,
        compute_capability="8.9", dph=Decimal("3.20"), max_dph=Decimal("3.50"), reliability=Decimal("0.995"),
        direct_port_count=4, inet_down_mbps=Decimal("800"), tensor_parallel=8, max_model_len=4096,
        max_num_seqs=64, disk_gib=250, deadline_minutes=120, matrix_name="smoke",
    )


def offer(**changes: object) -> SdkOffer:
    base = SdkOffer(101, 202, "RTX 4090", 8, 24564, "8.9", Decimal("3.20"), Decimal("0.995"), 4, Decimal("800"), LABEL)
    return replace(base, **changes)


def instance(**changes: object) -> SdkInstance:
    base = SdkInstance(9001, 202, "RTX 4090", 8, 24564, "8.9", Decimal("3.20"), LABEL, "running", None, None, None, ())
    return replace(base, **changes)


def launch(**changes: object) -> SdkLaunchContract:
    return replace(SdkLaunchContract(image=VLLM_IMAGE, disk_gib=250), **changes)


class Provider:
    def __init__(self, events: list, *, created: SdkInstance | None = None, live: list[SdkInstance] | None = None) -> None:
        self.events = events
        self.created = instance() if created is None else created
        self.live = [] if live is None else list(live)
        self.ambiguous = False
        self.ambiguous_leaves_instance = True
        self.destroy_error: Exception | None = None
        self.destroy_removes = True
        self.creates = 0
        self.destroys: list[tuple[int, str]] = []

    def create_once(self, offer: SdkOffer, contract: SdkLaunchContract) -> SdkInstance:
        self.events.append("create")
        self.creates += 1
        if self.ambiguous:
            if self.ambiguous_leaves_instance:
                self.live.append(self.created)
            raise AmbiguousCreate("outcome unknown")
        self.live.append(self.created)
        return self.created

    def reconcile_label(self, label: str) -> SdkInstance | None:
        self.events.append("reconcile")
        matches = [item for item in self.live if item.label == label]
        return matches[0] if matches else None

    def get_instance(self, instance_id: int) -> SdkInstance:
        self.events.append("get")
        return next(item for item in self.live if item.instance_id == instance_id)

    def list_instances(self) -> tuple[SdkInstance, ...]:
        self.events.append("list")
        return tuple(self.live)

    def destroy_exact(self, instance_id: int, expected_label: str) -> None:
        self.events.append("destroy")
        self.destroys.append((instance_id, expected_label))
        if self.destroy_removes:
            self.live = [item for item in self.live if item.instance_id != instance_id]
        if self.destroy_error is not None:
            raise self.destroy_error


class Guard:
    def __init__(self, events: list, **overrides: object) -> None:
        self.events = events
        self.overrides = overrides
        self.identities: list = []

    def preflight(self) -> GuardAttestation:  # pragma: no cover - protocol shape only
        raise NotImplementedError

    def arm(self, identity, hard_deadline):
        self.events.append("arm")
        self.identities.append((identity, hard_deadline))
        values = {
            "host_identity": "guard.example.net", "script_hash": "a" * 64, "nonce": NONCE,
            "label": identity.label, "hard_deadline": hard_deadline, "last_heartbeat": None,
        }
        values.update(self.overrides)
        return GuardAttestation(**values)

    def record_heartbeat(self, identity, monotonic_ns: int) -> None:  # pragma: no cover
        pass

    def anchor(self, root_hash: str) -> str:  # pragma: no cover
        return root_hash


def lease(events: list, provider: Provider, guard: Guard | None = None, **changes: object) -> SingleHostLease:
    kwargs: dict[str, object] = {
        "provider": provider, "offer": offer(), "plan": plan(), "launch": launch(),
        "guard": Guard(events) if guard is None else guard, "run_id": "run-1", "nonce": NONCE,
        "hard_deadline": DEADLINE, "now": lambda: NOW, "sleep": lambda seconds: events.append(("sleep", seconds)),
    }
    kwargs.update(changes)
    return SingleHostLease(**kwargs)  # type: ignore[arg-type]


def workload(events: list):
    def run(observed: SdkInstance) -> None:
        events.append(("workload", observed.instance_id))
    return run


ABSENCE_TAIL = [("sleep", 5.0), "list", ("sleep", 5.0), "list", ("sleep", 5.0), "list"]


def test_happy_path_call_order_and_result() -> None:
    events: list = []
    provider = Provider(events)
    guard = Guard(events)
    result = lease(events, provider, guard).run(workload(events), heartbeat=lambda: events.append("heartbeat"))
    assert events == ["list", "arm", "heartbeat", "create", "heartbeat", ("workload", 9001), "destroy", *ABSENCE_TAIL]
    assert result == SingleHostResult(instance(), True, 3)
    assert provider.destroys == [(9001, LABEL)]
    identity, deadline = guard.identities[0]
    assert (identity.run_id, identity.label, deadline) == ("run-1", LABEL, DEADLINE)


def test_heartbeat_is_optional() -> None:
    events: list = []
    result = lease(events, Provider(events)).run(workload(events))
    assert events == ["list", "arm", "create", ("workload", 9001), "destroy", *ABSENCE_TAIL]
    assert result.workload_completed


def test_heartbeat_failure_before_create_never_creates() -> None:
    events: list = []
    provider = Provider(events)

    def heartbeat() -> None:
        raise RuntimeError("guard unreachable")

    with pytest.raises(RuntimeError, match="guard unreachable"):
        lease(events, provider).run(workload(events), heartbeat=heartbeat)
    assert provider.creates == 0 and "destroy" not in events


def test_heartbeat_continues_while_create_is_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list = []
    provider = Provider(events)
    next_heartbeat = Event()
    calls = 0
    original_create = provider.create_once
    monkeypatch.setattr(single_host_multigpu, "CREATE_HEARTBEAT_INTERVAL_SECONDS", 0.005, raising=False)

    def heartbeat() -> None:
        nonlocal calls
        calls += 1
        if calls >= 2:
            next_heartbeat.set()

    def slow_create(offer: SdkOffer, contract: SdkLaunchContract) -> SdkInstance:
        assert next_heartbeat.wait(0.2), "guard heartbeat stopped while create was pending"
        return original_create(offer, contract)

    monkeypatch.setattr(provider, "create_once", slow_create)
    result = lease(events, provider).run(workload(events), heartbeat=heartbeat)
    assert result.absence_reads == 3
    assert calls >= 2


def test_nonempty_inventory_blocks_arm_and_create() -> None:
    events: list = []
    provider = Provider(events, live=[instance(instance_id=1, label="other")])
    with pytest.raises(SingleHostError, match="inventory must be empty"):
        lease(events, provider).run(workload(events))
    assert events == ["list"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"nonce": "wrong"},
        {"hard_deadline": DEADLINE + timedelta(seconds=1)},
        {"hard_deadline": DEADLINE.replace(tzinfo=None)},
        {"label": "srecon26-mgpu--nonce-other"},
        {"host_identity": "localhost"},
    ],
)
def test_guard_attestation_must_bind_before_create(overrides: dict) -> None:
    events: list = []
    provider = Provider(events)
    with pytest.raises((SingleHostError, GuardRejected)):
        lease(events, provider, Guard(events, **overrides)).run(workload(events))
    assert events == ["list", "arm"]
    assert provider.creates == 0


def test_ambiguous_create_reconciles_by_label_and_never_recreates() -> None:
    events: list = []
    provider = Provider(events)
    provider.ambiguous = True
    result = lease(events, provider).run(workload(events))
    assert provider.creates == 1
    assert events == ["list", "arm", "create", "reconcile", ("workload", 9001), "destroy", *ABSENCE_TAIL]
    assert result.instance == instance()


def test_ambiguous_create_with_no_instance_proves_label_absence() -> None:
    events: list = []
    provider = Provider(events)
    provider.ambiguous = True
    provider.ambiguous_leaves_instance = False
    with pytest.raises(SingleHostError, match=WORKLOAD_FAILED):
        lease(events, provider).run(workload(events))
    assert provider.creates == 1
    assert events == ["list", "arm", "create", "reconcile", *ABSENCE_TAIL]


def test_ambiguous_create_late_label_is_destroyed_during_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list = []
    provider = Provider(events)
    provider.ambiguous = True
    provider.ambiguous_leaves_instance = False
    list_calls = 0
    original_list = provider.list_instances

    def delayed_inventory() -> tuple[SdkInstance, ...]:
        nonlocal list_calls
        list_calls += 1
        if list_calls == 2:
            provider.live.append(provider.created)
        return original_list()

    monkeypatch.setattr(provider, "list_instances", delayed_inventory)
    with pytest.raises(SingleHostError, match=WORKLOAD_FAILED):
        lease(events, provider).run(workload(events))
    assert provider.creates == 1
    assert provider.destroys == [(9001, LABEL)]
    assert events[-len(ABSENCE_TAIL):] == ABSENCE_TAIL


def test_label_drift_is_not_destroyed_and_fails_cleanup() -> None:
    events: list = []
    provider = Provider(events, created=instance(label="srecon26-mgpu--nonce-foreign"))
    with pytest.raises(SingleHostError, match=TEARDOWN_FAILED):
        lease(events, provider).run(workload(events))
    assert provider.destroys == []
    assert not any(isinstance(event, tuple) and event[0] == "workload" for event in events)


@pytest.mark.parametrize(
    "drift",
    [{"machine_id": 999}, {"gpu_name": "RTX 3090"}, {"num_gpus": 4}, {"gpu_ram_mib": 12288}, {"compute_capability": "8.6"}, {"dph_total": Decimal("3.21")}],
)
def test_hardware_or_price_drift_is_destroyed_before_workload(drift: dict) -> None:
    events: list = []
    provider = Provider(events, created=instance(**drift))
    with pytest.raises(SingleHostError, match=WORKLOAD_FAILED) as raised:
        lease(events, provider).run(workload(events))
    assert isinstance(raised.value.__cause__, SingleHostError)
    assert events == ["list", "arm", "create", "destroy", *ABSENCE_TAIL]


def test_price_decrease_is_accepted() -> None:
    events: list = []
    result = lease(events, Provider(events, created=instance(dph_total=Decimal("3.00")))).run(workload(events))
    assert result.workload_completed


def test_workload_failure_still_destroys_and_proves_absence() -> None:
    events: list = []
    provider = Provider(events)
    boom = RuntimeError("vllm crashed")

    def failing(_: SdkInstance) -> None:
        raise boom

    with pytest.raises(SingleHostError, match=WORKLOAD_FAILED) as raised:
        lease(events, provider).run(failing)
    assert raised.value.__cause__ is boom
    assert provider.destroys == [(9001, LABEL)]
    assert events[-7:] == ["destroy", *ABSENCE_TAIL]


def test_destroy_race_with_guard_continues_absence_proof() -> None:
    events: list = []
    provider = Provider(events)
    provider.destroy_error = RuntimeError("already destroyed by guard")
    result = lease(events, provider).run(workload(events))
    assert result == SingleHostResult(instance(), True, 3)
    assert events[-8:] == ["destroy", "list", *ABSENCE_TAIL]


def test_destroy_failure_with_instance_remaining_raises() -> None:
    events: list = []
    provider = Provider(events)
    provider.destroy_error = RuntimeError("api down")
    provider.destroy_removes = False
    with pytest.raises(SingleHostError, match=TEARDOWN_FAILED):
        lease(events, provider).run(workload(events))
    assert provider.destroys == [(9001, LABEL)]
    assert events[-2:] == ["destroy", "list"]


def test_instance_present_during_absence_proof_raises() -> None:
    events: list = []
    provider = Provider(events)
    provider.destroy_removes = False
    with pytest.raises(SingleHostError, match=TEARDOWN_FAILED):
        lease(events, provider).run(workload(events))
    assert events[-3:] == ["destroy", ("sleep", 5.0), "list"]


def test_absence_proof_uses_exactly_three_five_second_sleeps() -> None:
    events: list = []
    lease(events, Provider(events)).run(workload(events))
    sleeps = [event for event in events if isinstance(event, tuple) and event[0] == "sleep"]
    assert sleeps == [("sleep", 5.0)] * 3
    assert events.index("destroy") < events.index(("sleep", 5.0))
    assert events.count("list") == 4


@pytest.mark.parametrize(
    "changes",
    [
        {"offer": offer(offer_id=102)},
        {"offer": offer(machine_id=203)},
        {"offer": offer(gpu_name="RTX 3090")},
        {"offer": offer(num_gpus=4)},
        {"offer": offer(gpu_ram_mib=24576)},
        {"offer": offer(compute_capability="8.6")},
        {"offer": offer(dph_total=Decimal("3.19"))},
        {"offer": offer(reliability=Decimal("0.999"))},
        {"offer": offer(direct_port_count=3)},
        {"offer": offer(inet_down_mbps=Decimal("900"))},
        {"offer": offer(label="srecon26-mgpu")},
        {"offer": offer(label=f"a--nonce-x--nonce-{NONCE}")},
        {"nonce": "other"},
        {"run_id": ""},
        {"hard_deadline": DEADLINE.replace(tzinfo=None)},
        {"hard_deadline": NOW},
        {"launch": launch(disk_gib=300)},
        {"launch": launch(image="docker.io/vllm/vllm-openai@sha256:" + "0" * 64)},
        {"launch": launch(image="docker.io/vllm/vllm-openai:latest")},
    ],
)
def test_invalid_lease_inputs_fail_before_any_provider_call(changes: dict) -> None:
    events: list = []
    with pytest.raises(SingleHostError):
        lease(events, Provider(events), **changes).run(workload(events))
    assert events == []
