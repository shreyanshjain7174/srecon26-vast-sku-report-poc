from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from guard.guard_worker import GuardSafetyError, GuardedInstance, GuardWorker


NOW = datetime(2026, 9, 23, tzinfo=UTC)
INSTANCE_ID = 417
NONCE = "nonce-0123456789abcdef"
LABEL = f"srecon26-run-guard--nonce-{NONCE}"


class FakeProvider:
    def __init__(self) -> None:
        self.instance: GuardedInstance | None = GuardedInstance(INSTANCE_ID, LABEL)
        self.destroy_calls: list[tuple[int, str]] = []

    def find_instances(self, label: str) -> tuple[GuardedInstance, ...]:
        return (self.instance,) if self.instance and self.instance.label == label else ()

    def get_instance(self, instance_id: int) -> GuardedInstance | None:
        return self.instance if self.instance and self.instance.instance_id == instance_id else None

    def destroy_exact(self, instance_id: int, expected_label: str) -> None:
        self.destroy_calls.append((instance_id, expected_label))
        self.instance = None


def test_repeated_tick_after_teardown_issues_one_destroy_and_preserves_root_hash(tmp_path) -> None:
    provider = FakeProvider()
    worker = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(seconds=1), require_root_owner=False)
    worker.arm(INSTANCE_ID, LABEL, NONCE, NOW + timedelta(minutes=1), now=NOW)

    first = worker.tick(NONCE, now=NOW + timedelta(seconds=2))
    second = worker.tick(NONCE, now=NOW + timedelta(seconds=3))

    assert provider.destroy_calls == [(INSTANCE_ID, LABEL)]
    assert first.root_hash == second.root_hash
    assert second.status == "TEARDOWN_CONFIRMED"


def test_reopening_worker_reads_durable_state_and_never_repeats_destroy(tmp_path) -> None:
    provider = FakeProvider()
    worker = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(seconds=1), require_root_owner=False)
    worker.arm(INSTANCE_ID, LABEL, NONCE, NOW + timedelta(minutes=1), now=NOW)
    worker.tick(NONCE, now=NOW + timedelta(seconds=2))

    reopened = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(seconds=1), require_root_owner=False)
    receipt = reopened.tick(NONCE, now=NOW + timedelta(seconds=3))

    assert provider.destroy_calls == [(INSTANCE_ID, LABEL)]
    assert receipt.status == "TEARDOWN_CONFIRMED"


def test_same_nonce_cannot_change_target_or_deadline(tmp_path) -> None:
    provider = FakeProvider()
    worker = GuardWorker(tmp_path, provider, require_root_owner=False)
    worker.arm(INSTANCE_ID, LABEL, NONCE, NOW + timedelta(minutes=1), now=NOW)

    with pytest.raises(GuardSafetyError, match="immutable"):
        worker.arm(INSTANCE_ID + 1, LABEL, NONCE, NOW + timedelta(minutes=2), now=NOW)


def test_delayed_visibility_is_reconciled_by_nonce_before_deadline(tmp_path) -> None:
    provider = FakeProvider()
    provider.instance = None
    worker = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(seconds=1), require_root_owner=False)
    worker.arm(None, LABEL, NONCE, NOW + timedelta(minutes=1), now=NOW)
    provider.instance = GuardedInstance(INSTANCE_ID, LABEL)

    worker.tick(NONCE, now=NOW + timedelta(seconds=2))

    assert provider.destroy_calls == [(INSTANCE_ID, LABEL)]


def test_healthy_prearmed_guard_binds_exact_created_instance_before_deadline(tmp_path) -> None:
    provider = FakeProvider()
    provider.instance = None
    worker = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(minutes=2), require_root_owner=False)
    worker.arm(None, LABEL, NONCE, NOW + timedelta(minutes=10), now=NOW)
    provider.instance = GuardedInstance(INSTANCE_ID, LABEL)

    receipt = worker.tick(NONCE, now=NOW + timedelta(seconds=30))

    assert receipt.status == "ARMED"
    assert receipt.instance_id == INSTANCE_ID
    assert provider.destroy_calls == []


def test_healthy_prearmed_guard_stays_awaiting_when_no_exact_instance_exists(tmp_path) -> None:
    provider = FakeProvider()
    provider.instance = None
    worker = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(minutes=2), require_root_owner=False)
    worker.arm(None, LABEL, NONCE, NOW + timedelta(minutes=10), now=NOW)

    receipt = worker.tick(NONCE, now=NOW + timedelta(seconds=30))

    assert receipt.status == "AWAITING_INSTANCE"
    assert receipt.instance_id is None


def test_prearmed_backstop_confirms_label_absence_at_its_deadline(tmp_path) -> None:
    provider = FakeProvider()
    provider.instance = None
    worker = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(minutes=2), require_root_owner=False)
    deadline = NOW + timedelta(minutes=10)
    worker.arm(None, LABEL, NONCE, deadline, now=NOW)

    receipt = worker.tick(NONCE, now=deadline)

    assert receipt.status == "ABSENT_OBSERVED"
    assert provider.destroy_calls == []


def test_prearmed_bound_target_can_later_observe_absence_and_disarm(tmp_path) -> None:
    provider = FakeProvider()
    worker = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(seconds=1), require_root_owner=False)
    worker.arm(None, LABEL, NONCE, NOW + timedelta(minutes=10), now=NOW)
    worker.tick(NONCE, now=NOW + timedelta(milliseconds=500))
    provider.instance = None

    absent = worker.tick(NONCE, now=NOW + timedelta(seconds=2))
    disarmed = worker.disarm_after_absence(NONCE, "a" * 64, now=NOW + timedelta(seconds=3))

    assert absent.status == "ABSENT_OBSERVED"
    assert disarmed.status == "DISARMED"


def test_healthy_prearmed_guard_refuses_multiple_exact_matches(tmp_path) -> None:
    class MultipleProvider(FakeProvider):
        def find_instances(self, label: str) -> tuple[GuardedInstance, ...]:
            return (GuardedInstance(INSTANCE_ID, label), GuardedInstance(INSTANCE_ID + 1, label))

    worker = GuardWorker(tmp_path, MultipleProvider(), heartbeat_timeout=timedelta(minutes=2), require_root_owner=False)
    worker.arm(None, LABEL, NONCE, NOW + timedelta(minutes=10), now=NOW)

    receipt = worker.tick(NONCE, now=NOW + timedelta(seconds=30))

    assert receipt.status == "OWNERSHIP_MISMATCH"


def test_reopened_disarmed_guard_stays_disarmed(tmp_path) -> None:
    provider = FakeProvider()
    worker = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(seconds=1), require_root_owner=False)
    worker.arm(INSTANCE_ID, LABEL, NONCE, NOW + timedelta(minutes=1), now=NOW)
    worker.tick(NONCE, now=NOW + timedelta(seconds=2))
    worker.disarm_after_absence(NONCE, "a" * 64, now=NOW + timedelta(seconds=3))

    reopened = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(seconds=1), require_root_owner=False)

    assert reopened.status(NONCE).status == "DISARMED"


def test_teardown_error_reconciles_and_retries_only_the_exact_owned_target(tmp_path) -> None:
    class TransientDestroyProvider(FakeProvider):
        def destroy_exact(self, instance_id: int, expected_label: str) -> None:
            self.destroy_calls.append((instance_id, expected_label))
            if len(self.destroy_calls) == 1:
                raise TimeoutError("provider response lost after request")
            assert self.instance == GuardedInstance(INSTANCE_ID, LABEL)
            self.instance = None

    provider = TransientDestroyProvider()
    worker = GuardWorker(
        tmp_path,
        provider,
        heartbeat_timeout=timedelta(seconds=1),
        post_deadline_window=timedelta(seconds=30),
        require_root_owner=False,
    )
    worker.arm(INSTANCE_ID, LABEL, NONCE, NOW + timedelta(seconds=10), now=NOW)

    first = worker.tick(NONCE, now=NOW + timedelta(seconds=11))
    second = worker.tick(NONCE, now=NOW + timedelta(seconds=12))

    assert first.status == "TEARDOWN_ERROR"
    assert second.status == "TEARDOWN_CONFIRMED"
    assert provider.destroy_calls == [(INSTANCE_ID, LABEL), (INSTANCE_ID, LABEL)]


def test_destroy_success_requires_provider_confirmed_absence_before_terminal_receipt(tmp_path) -> None:
    class DelayedAbsenceProvider(FakeProvider):
        def destroy_exact(self, instance_id: int, expected_label: str) -> None:
            self.destroy_calls.append((instance_id, expected_label))
            # The provider accepted the request but its read model is stale.
            if len(self.destroy_calls) == 2:
                self.instance = None

    provider = DelayedAbsenceProvider()
    worker = GuardWorker(
        tmp_path,
        provider,
        heartbeat_timeout=timedelta(seconds=1),
        post_deadline_window=timedelta(seconds=30),
        require_root_owner=False,
    )
    worker.arm(INSTANCE_ID, LABEL, NONCE, NOW + timedelta(seconds=10), now=NOW)

    first = worker.tick(NONCE, now=NOW + timedelta(seconds=11))
    second = worker.tick(NONCE, now=NOW + timedelta(seconds=12))

    assert first.status == "TEARDOWN_ERROR"
    assert second.status == "TEARDOWN_CONFIRMED"
    assert provider.destroy_calls == [(INSTANCE_ID, LABEL), (INSTANCE_ID, LABEL)]


def test_no_destroy_is_issued_after_bounded_post_deadline_window(tmp_path) -> None:
    class NeverAbsentProvider(FakeProvider):
        def destroy_exact(self, instance_id: int, expected_label: str) -> None:
            self.destroy_calls.append((instance_id, expected_label))

    provider = NeverAbsentProvider()
    worker = GuardWorker(
        tmp_path,
        provider,
        heartbeat_timeout=timedelta(seconds=1),
        post_deadline_window=timedelta(seconds=30),
        require_root_owner=False,
    )
    deadline = NOW + timedelta(seconds=10)
    worker.arm(INSTANCE_ID, LABEL, NONCE, deadline, now=NOW)

    worker.tick(NONCE, now=deadline)
    receipt = worker.tick(NONCE, now=deadline + timedelta(seconds=31))

    assert receipt.status == "TEARDOWN_UNCONFIRMED"
    assert provider.destroy_calls == [(INSTANCE_ID, LABEL)]
