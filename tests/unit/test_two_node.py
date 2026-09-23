from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from srecon26_poc.contracts import InstanceContract, OfferContract
from srecon26_poc.guard import GuardAttestation
from srecon26_poc.two_node import NodeLease, TwoNodeError, TwoNodeLease
from srecon26_poc.vast_provider import OFFICIAL_KVM_IMAGE, OFFICIAL_UBUNTU_2204_TEMPLATE_HASH, VastLaunchContract


NOW = datetime(2026, 9, 24, tzinfo=UTC)


def offer(role: str, nonce: str, offer_id: int, machine_id: int) -> OfferContract:
    return OfferContract(offer_id, "RTX 4090", 1, 24564, "8.9", machine_id, Decimal("0.5"), f"two-node-{role}--nonce-{nonce}")


class Guard:
    def __init__(self, nonce: str) -> None:
        self.nonce, self.armed = nonce, False

    def arm(self, identity, deadline):
        self.armed = True
        return GuardAttestation("remote-guard", "a" * 64, self.nonce, identity.label, deadline, NOW)


class Provider:
    def __init__(self) -> None:
        self.live: list[InstanceContract] = []
        self.create_labels: list[str] = []
        self.destroyed: list[int] = []

    def create_once(self, contract, request_key, launch):
        self.create_labels.append(contract.label)
        instance = InstanceContract(100 + len(self.live), contract.gpu_name, contract.num_gpus, contract.gpu_ram_mib, contract.compute_capability, contract.machine_id, contract.dph_total, contract.label)
        self.live.append(instance)
        return instance

    def reconcile_label(self, label):
        return next(item for item in self.live if item.label == label)

    def destroy_exact(self, instance_id, expected_label):
        self.destroyed.append(instance_id)
        self.live[:] = [item for item in self.live if item.instance_id != instance_id and item.label != expected_label]

    def list_instances(self):
        return tuple(self.live)


def lease(provider: Provider | None = None) -> tuple[TwoNodeLease, Provider]:
    provider = provider or Provider()
    server_nonce, worker_nonce = "servernonce1", "workernonce1"
    return TwoNodeLease(
        provider=provider,
        server=NodeLease("server", "two-node-server", server_nonce, offer("server", server_nonce, 11, 101)),
        worker=NodeLease("worker", "two-node-worker", worker_nonce, offer("worker", worker_nonce, 12, 102)),
        server_guard=Guard(server_nonce), worker_guard=Guard(worker_nonce),
        launch=VastLaunchContract(OFFICIAL_UBUNTU_2204_TEMPLATE_HASH, OFFICIAL_KVM_IMAGE),
        hard_deadline=NOW + timedelta(minutes=30), now=lambda: NOW,
    ), provider


def test_two_nodes_arm_before_first_create_and_clean_both_after_workload() -> None:
    controller, provider = lease()
    heartbeats: list[str] = []
    result = controller.run(lambda server, worker: None, heartbeat=lambda: heartbeats.append("sent"))
    assert provider.create_labels == ["two-node-server--nonce-servernonce1", "two-node-worker--nonce-workernonce1"]
    assert provider.live == []
    assert result.workload_completed is True
    assert set(result.absence_reads.values()) == {3}
    assert len(heartbeats) == 2


def test_worker_failure_cleans_server_and_never_retries_create() -> None:
    controller, provider = lease()
    with pytest.raises(TwoNodeError, match="workload failed"):
        controller.run(lambda server, worker: (_ for _ in ()).throw(RuntimeError("join failed")))
    assert len(provider.create_labels) == 2
    assert provider.live == []
    assert len(provider.destroyed) == 2


def test_external_guard_teardown_race_preserves_workload_failure() -> None:
    controller, provider = lease()
    original_destroy = provider.destroy_exact

    def guard_wins(instance_id, expected_label):
        provider.live.clear()
        raise RuntimeError("already absent")

    provider.destroy_exact = guard_wins  # type: ignore[method-assign]
    with pytest.raises(TwoNodeError, match="workload failed"):
        controller.run(lambda server, worker: (_ for _ in ()).throw(RuntimeError("startup failed")))
    provider.destroy_exact = original_destroy  # type: ignore[method-assign]


def test_duplicate_machine_or_nonce_is_rejected_before_any_create() -> None:
    controller, provider = lease()
    controller.worker = NodeLease("worker", "two-node-worker", controller.server.nonce, offer("worker", controller.server.nonce, 12, 101))
    with pytest.raises(TwoNodeError, match="distinct nonce-bound labels"):
        controller.run(lambda server, worker: None)
    assert provider.create_labels == []
