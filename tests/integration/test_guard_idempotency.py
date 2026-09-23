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
    third = worker.tick(NONCE, now=NOW + timedelta(seconds=4))
    stable = worker.tick(NONCE, now=NOW + timedelta(seconds=5))

    assert provider.destroy_calls == [(INSTANCE_ID, LABEL)]
    assert first.status == second.status == "ABSENCE_PENDING"
    assert third.status == "ABSENCE_CONFIRMED"
    assert third.root_hash == stable.root_hash


def test_reopening_worker_reads_durable_state_and_never_repeats_destroy(tmp_path) -> None:
    provider = FakeProvider()
    worker = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(seconds=1), require_root_owner=False)
    worker.arm(INSTANCE_ID, LABEL, NONCE, NOW + timedelta(minutes=1), now=NOW)
    worker.tick(NONCE, now=NOW + timedelta(seconds=2))

    reopened = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(seconds=1), require_root_owner=False)
    reopened.tick(NONCE, now=NOW + timedelta(seconds=3))
    receipt = reopened.tick(NONCE, now=NOW + timedelta(seconds=4))

    assert provider.destroy_calls == [(INSTANCE_ID, LABEL)]
    assert receipt.status == "ABSENCE_CONFIRMED"
    assert receipt.absence_observations == (
        NOW + timedelta(seconds=2),
        NOW + timedelta(seconds=3),
        NOW + timedelta(seconds=4),
    )


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


def test_prearmed_backstop_requires_three_label_absence_reads_after_its_deadline(tmp_path) -> None:
    provider = FakeProvider()
    provider.instance = None
    worker = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(minutes=2), require_root_owner=False)
    deadline = NOW + timedelta(minutes=10)
    worker.arm(None, LABEL, NONCE, deadline, now=NOW)

    first = worker.tick(NONCE, now=deadline)
    second = worker.tick(NONCE, now=deadline + timedelta(seconds=1))
    receipt = worker.tick(NONCE, now=deadline + timedelta(seconds=2))

    assert first.status == second.status == "ABSENCE_PENDING"
    assert receipt.status == "ABSENCE_CONFIRMED"
    assert provider.destroy_calls == []


def test_prearmed_bound_target_can_later_observe_absence_and_disarm(tmp_path) -> None:
    provider = FakeProvider()
    worker = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(seconds=1), require_root_owner=False)
    worker.arm(None, LABEL, NONCE, NOW + timedelta(minutes=10), now=NOW)
    worker.tick(NONCE, now=NOW + timedelta(milliseconds=500))
    provider.instance = None

    first = worker.tick(NONCE, now=NOW + timedelta(seconds=2))
    with pytest.raises(GuardSafetyError, match="three-read"):
        worker.disarm_after_absence(NONCE, "a" * 64, now=NOW + timedelta(seconds=3))
    worker.tick(NONCE, now=NOW + timedelta(seconds=3))
    absent = worker.tick(NONCE, now=NOW + timedelta(seconds=4))
    disarmed = worker.disarm_after_absence(NONCE, "a" * 64, now=NOW + timedelta(seconds=5))

    assert first.status == "ABSENCE_PENDING"
    assert absent.status == "ABSENCE_CONFIRMED"
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
    worker.tick(NONCE, now=NOW + timedelta(seconds=3))
    worker.tick(NONCE, now=NOW + timedelta(seconds=4))
    worker.disarm_after_absence(NONCE, "a" * 64, now=NOW + timedelta(seconds=5))

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
    worker.tick(NONCE, now=NOW + timedelta(seconds=13))
    confirmed = worker.tick(NONCE, now=NOW + timedelta(seconds=14))

    assert first.status == "TEARDOWN_ERROR"
    assert second.status == "ABSENCE_PENDING"
    assert confirmed.status == "ABSENCE_CONFIRMED"
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
    worker.tick(NONCE, now=NOW + timedelta(seconds=13))
    confirmed = worker.tick(NONCE, now=NOW + timedelta(seconds=14))

    assert first.status == "TEARDOWN_ERROR"
    assert second.status == "ABSENCE_PENDING"
    assert confirmed.status == "ABSENCE_CONFIRMED"
    assert provider.destroy_calls == [(INSTANCE_ID, LABEL), (INSTANCE_ID, LABEL)]


def test_duplicate_timestamp_does_not_advance_three_read_quorum(tmp_path) -> None:
    provider = FakeProvider()
    worker = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(seconds=1), require_root_owner=False)
    worker.arm(INSTANCE_ID, LABEL, NONCE, NOW + timedelta(minutes=1), now=NOW)

    first = worker.tick(NONCE, now=NOW + timedelta(seconds=2))
    duplicate = worker.tick(NONCE, now=NOW + timedelta(seconds=2))
    second = worker.tick(NONCE, now=NOW + timedelta(seconds=3))
    third = worker.tick(NONCE, now=NOW + timedelta(seconds=4))

    assert len(first.absence_observations) == 1
    assert duplicate.absence_observations == first.absence_observations
    assert second.status == "ABSENCE_PENDING"
    assert third.status == "ABSENCE_CONFIRMED"


def test_provider_failure_resets_pending_absence_quorum(tmp_path) -> None:
    class FailingReadProvider(FakeProvider):
        fail_next_read = False

        def get_instance(self, instance_id: int) -> GuardedInstance | None:
            if self.fail_next_read:
                self.fail_next_read = False
                raise TimeoutError("provider unavailable")
            return super().get_instance(instance_id)

    provider = FailingReadProvider()
    worker = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(seconds=1), require_root_owner=False)
    worker.arm(INSTANCE_ID, LABEL, NONCE, NOW + timedelta(minutes=1), now=NOW)
    first = worker.tick(NONCE, now=NOW + timedelta(seconds=2))
    provider.fail_next_read = True

    failed = worker.tick(NONCE, now=NOW + timedelta(seconds=3))
    restarted = worker.tick(NONCE, now=NOW + timedelta(seconds=4))
    worker.tick(NONCE, now=NOW + timedelta(seconds=5))
    confirmed = worker.tick(NONCE, now=NOW + timedelta(seconds=6))

    assert first.status == "ABSENCE_PENDING"
    assert failed.status == "TEARDOWN_ERROR"
    assert failed.absence_observations == ()
    assert len(restarted.absence_observations) == 1
    assert confirmed.status == "ABSENCE_CONFIRMED"


def test_unknown_provider_inventory_resets_and_refuses_pending_quorum(tmp_path) -> None:
    class UnknownInventoryProvider(FakeProvider):
        unknown = False

        def find_instances(self, label: str) -> tuple[GuardedInstance, ...]:
            if self.unknown:
                return []  # type: ignore[return-value]
            return super().find_instances(label)

    provider = UnknownInventoryProvider()
    worker = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(seconds=1), require_root_owner=False)
    worker.arm(INSTANCE_ID, LABEL, NONCE, NOW + timedelta(minutes=1), now=NOW)
    worker.tick(NONCE, now=NOW + timedelta(seconds=2))
    provider.unknown = True

    refused = worker.tick(NONCE, now=NOW + timedelta(seconds=3))

    assert refused.status == "TEARDOWN_ERROR"
    assert refused.absence_observations == ()


def test_reappearing_exact_target_resets_pending_absence_quorum(tmp_path) -> None:
    class StaleDestroyProvider(FakeProvider):
        keep_present = False

        def destroy_exact(self, instance_id: int, expected_label: str) -> None:
            self.destroy_calls.append((instance_id, expected_label))
            if not self.keep_present:
                self.instance = None

    provider = StaleDestroyProvider()
    worker = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(seconds=1), require_root_owner=False)
    worker.arm(INSTANCE_ID, LABEL, NONCE, NOW + timedelta(minutes=1), now=NOW)
    worker.tick(NONCE, now=NOW + timedelta(seconds=2))
    provider.instance = GuardedInstance(INSTANCE_ID, LABEL)
    provider.keep_present = True

    present = worker.tick(NONCE, now=NOW + timedelta(seconds=3))

    assert present.status == "TEARDOWN_ERROR"
    assert present.absence_observations == ()


def test_changed_exact_id_label_refuses_pending_absence_quorum(tmp_path) -> None:
    provider = FakeProvider()
    worker = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(seconds=1), require_root_owner=False)
    worker.arm(INSTANCE_ID, LABEL, NONCE, NOW + timedelta(minutes=1), now=NOW)
    worker.tick(NONCE, now=NOW + timedelta(seconds=2))
    provider.instance = GuardedInstance(INSTANCE_ID, f"changed--nonce-{NONCE}")

    refused = worker.tick(NONCE, now=NOW + timedelta(seconds=3))

    assert refused.status == "OWNERSHIP_MISMATCH"
    assert refused.absence_observations == ()
    assert provider.destroy_calls == [(INSTANCE_ID, LABEL)]


def test_duplicate_nonce_bound_labels_refuse_pending_absence_quorum(tmp_path) -> None:
    class DuplicateLabelProvider(FakeProvider):
        duplicates = False

        def find_instances(self, label: str) -> tuple[GuardedInstance, ...]:
            if self.duplicates:
                return (GuardedInstance(INSTANCE_ID + 1, label), GuardedInstance(INSTANCE_ID + 2, label))
            return super().find_instances(label)

    provider = DuplicateLabelProvider()
    worker = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(seconds=1), require_root_owner=False)
    worker.arm(INSTANCE_ID, LABEL, NONCE, NOW + timedelta(minutes=1), now=NOW)
    worker.tick(NONCE, now=NOW + timedelta(seconds=2))
    provider.duplicates = True

    refused = worker.tick(NONCE, now=NOW + timedelta(seconds=3))

    assert refused.status == "OWNERSHIP_MISMATCH"
    assert refused.absence_observations == ()


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
