from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from guard.guard_worker import GuardedInstance, GuardWorker


INSTANCE_ID = 417
NONCE = "nonce-0123456789abcdef"
LABEL = f"srecon26-run-guard--nonce-{NONCE}"


@dataclass
class Clock:
    current: datetime

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


class FakeProvider:
    def __init__(self) -> None:
        self.instance: GuardedInstance | None = GuardedInstance(INSTANCE_ID, LABEL)
        self.destroy_calls: list[tuple[int, str]] = []

    def find_instances(self, label: str) -> tuple[GuardedInstance, ...]:
        if self.instance is not None and self.instance.label == label:
            return (self.instance,)
        return ()

    def get_instance(self, instance_id: int) -> GuardedInstance | None:
        return self.instance if self.instance is not None and self.instance.instance_id == instance_id else None

    def destroy_exact(self, instance_id: int, expected_label: str) -> None:
        self.destroy_calls.append((instance_id, expected_label))
        self.instance = None


def harness(tmp_path):
    clock = Clock(datetime(2026, 9, 23, tzinfo=UTC))
    provider = FakeProvider()
    worker = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(minutes=2), require_root_owner=False)
    return clock, provider, worker


def test_guard_destroys_exact_owned_instance_after_heartbeat_loss(tmp_path) -> None:
    clock, provider, worker = harness(tmp_path)
    worker.arm(INSTANCE_ID, LABEL, NONCE, clock.now() + timedelta(minutes=10), now=clock.now())

    clock.advance(worker.heartbeat_timeout + timedelta(seconds=1))
    first = worker.tick(NONCE, now=clock.now())
    clock.advance(timedelta(seconds=1))
    worker.tick(NONCE, now=clock.now())
    clock.advance(timedelta(seconds=1))
    worker.tick(NONCE, now=clock.now())
    clock.advance(timedelta(seconds=1))
    confirmed = worker.tick(NONCE, now=clock.now())

    assert provider.destroy_calls == [(INSTANCE_ID, LABEL)]
    assert first.status == "TEARDOWN_REQUESTED"
    assert confirmed.status == "ABSENCE_CONFIRMED"
    assert worker.status(NONCE).status == "ABSENCE_CONFIRMED"


def test_guard_refuses_changed_label_or_nonce(tmp_path) -> None:
    clock, provider, worker = harness(tmp_path)
    worker.arm(INSTANCE_ID, LABEL, NONCE, clock.now() + timedelta(minutes=10), now=clock.now())
    assert provider.instance is not None
    provider.instance = GuardedInstance(INSTANCE_ID, f"unowned--nonce-{NONCE}")

    worker.tick(NONCE, now=clock.now() + timedelta(minutes=10))

    assert provider.destroy_calls == []
    assert worker.status(NONCE).status == "OWNERSHIP_MISMATCH"


def test_hard_deadline_is_immutable_and_beats_recent_heartbeat(tmp_path) -> None:
    clock, provider, worker = harness(tmp_path)
    deadline = clock.now() + timedelta(minutes=10)
    worker.arm(INSTANCE_ID, LABEL, NONCE, deadline, now=clock.now())
    worker.heartbeat(NONCE, now=deadline - timedelta(seconds=1))

    worker.tick(NONCE, now=deadline)

    assert provider.destroy_calls == [(INSTANCE_ID, LABEL)]
    assert worker.status(NONCE).hard_deadline == deadline
