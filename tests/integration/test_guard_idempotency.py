from __future__ import annotations

import json
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
    confirmed = worker.tick(NONCE, now=NOW + timedelta(seconds=5))
    stable = worker.tick(NONCE, now=NOW + timedelta(seconds=6))

    assert provider.destroy_calls == [(INSTANCE_ID, LABEL)]
    assert first.status == "TEARDOWN_REQUESTED"
    assert second.status == third.status == "ABSENCE_PENDING"
    assert confirmed.status == "ABSENCE_CONFIRMED"
    assert confirmed.root_hash == stable.root_hash


def test_reopening_worker_reads_durable_state_and_never_repeats_destroy(tmp_path) -> None:
    provider = FakeProvider()
    worker = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(seconds=1), require_root_owner=False)
    worker.arm(INSTANCE_ID, LABEL, NONCE, NOW + timedelta(minutes=1), now=NOW)
    worker.tick(NONCE, now=NOW + timedelta(seconds=2))

    reopened = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(seconds=1), require_root_owner=False)
    reopened.tick(NONCE, now=NOW + timedelta(seconds=3))
    reopened.tick(NONCE, now=NOW + timedelta(seconds=4))
    receipt = reopened.tick(NONCE, now=NOW + timedelta(seconds=5))

    assert provider.destroy_calls == [(INSTANCE_ID, LABEL)]
    assert receipt.status == "ABSENCE_CONFIRMED"
    assert receipt.absence_observations == (
        NOW + timedelta(seconds=3),
        NOW + timedelta(seconds=4),
        NOW + timedelta(seconds=5),
    )


def test_reopened_timer_uses_immutable_armed_heartbeat_timeout(tmp_path) -> None:
    provider = FakeProvider()
    armed = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(seconds=120), require_root_owner=False)
    receipt = armed.arm(INSTANCE_ID, LABEL, NONCE, NOW + timedelta(minutes=10), now=NOW)
    assert receipt.heartbeat_timeout_seconds == 120

    reopened = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(seconds=1), require_root_owner=False)
    still_armed = reopened.tick(NONCE, now=NOW + timedelta(seconds=2))

    assert still_armed.status == "ARMED"
    assert still_armed.heartbeat_timeout_seconds == 120
    assert provider.destroy_calls == []
    with pytest.raises(GuardSafetyError, match="immutable"):
        reopened.arm(INSTANCE_ID, LABEL, NONCE, NOW + timedelta(minutes=10), now=NOW)


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
    third = worker.tick(NONCE, now=deadline + timedelta(seconds=2))
    receipt = worker.tick(NONCE, now=deadline + timedelta(seconds=3))

    assert first.status == "AWAITING_INSTANCE"
    assert first.teardown_authority_at == deadline
    assert second.status == third.status == "ABSENCE_PENDING"
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
    worker.tick(NONCE, now=NOW + timedelta(seconds=4))
    absent = worker.tick(NONCE, now=NOW + timedelta(seconds=5))
    disarmed = worker.disarm_after_absence(NONCE, "a" * 64, now=NOW + timedelta(seconds=6))

    assert first.status == "TEARDOWN_AUTHORIZED"
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
    worker.tick(NONCE, now=NOW + timedelta(seconds=5))
    worker.disarm_after_absence(NONCE, "a" * 64, now=NOW + timedelta(seconds=6))

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


def test_destroy_response_error_starts_absence_proof_when_another_finalizer_won_the_race(tmp_path) -> None:
    class ConcurrentFinalizerProvider(FakeProvider):
        def destroy_exact(self, instance_id: int, expected_label: str) -> None:
            self.destroy_calls.append((instance_id, expected_label))
            # Another independently-authorized finalizer removed the exact
            # nonce-bound target while this destroy request was in flight.
            self.instance = None
            raise TimeoutError("provider response lost after request")

    provider = ConcurrentFinalizerProvider()
    worker = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(seconds=1), require_root_owner=False)
    worker.arm(INSTANCE_ID, LABEL, NONCE, NOW + timedelta(seconds=10), now=NOW)

    first = worker.tick(NONCE, now=NOW + timedelta(seconds=11))
    second = worker.tick(NONCE, now=NOW + timedelta(seconds=12))
    third = worker.tick(NONCE, now=NOW + timedelta(seconds=13))
    confirmed = worker.tick(NONCE, now=NOW + timedelta(seconds=14))

    assert provider.destroy_calls == [(INSTANCE_ID, LABEL)]
    assert first.status == "TEARDOWN_REQUESTED"
    assert second.status == "ABSENCE_PENDING"
    assert third.status == "ABSENCE_PENDING"
    assert confirmed.status == "ABSENCE_CONFIRMED"


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
    retrograde = worker.tick(NONCE, now=NOW + timedelta(milliseconds=2500))
    third = worker.tick(NONCE, now=NOW + timedelta(seconds=4))
    confirmed = worker.tick(NONCE, now=NOW + timedelta(seconds=5))

    assert first.teardown_authority_at == NOW + timedelta(seconds=2)
    assert first.absence_observations == ()
    assert duplicate.absence_observations == first.absence_observations
    assert second.status == "ABSENCE_PENDING"
    assert retrograde.absence_observations == second.absence_observations
    assert third.status == "ABSENCE_PENDING"
    assert confirmed.status == "ABSENCE_CONFIRMED"


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
    worker.tick(NONCE, now=NOW + timedelta(seconds=2))
    first = worker.tick(NONCE, now=NOW + timedelta(seconds=3))
    provider.fail_next_read = True

    failed = worker.tick(NONCE, now=NOW + timedelta(seconds=4))
    restarted = worker.tick(NONCE, now=NOW + timedelta(seconds=5))
    worker.tick(NONCE, now=NOW + timedelta(seconds=6))
    confirmed = worker.tick(NONCE, now=NOW + timedelta(seconds=7))

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
    worker.tick(NONCE, now=NOW + timedelta(seconds=3))
    provider.unknown = True

    refused = worker.tick(NONCE, now=NOW + timedelta(seconds=4))

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
    worker.tick(NONCE, now=NOW + timedelta(seconds=3))
    provider.instance = GuardedInstance(INSTANCE_ID, LABEL)
    provider.keep_present = True

    present = worker.tick(NONCE, now=NOW + timedelta(seconds=4))

    assert present.status == "TEARDOWN_ERROR"
    assert present.absence_observations == ()


def test_changed_exact_id_label_refuses_pending_absence_quorum(tmp_path) -> None:
    provider = FakeProvider()
    worker = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(seconds=1), require_root_owner=False)
    worker.arm(INSTANCE_ID, LABEL, NONCE, NOW + timedelta(minutes=1), now=NOW)
    worker.tick(NONCE, now=NOW + timedelta(seconds=2))
    worker.tick(NONCE, now=NOW + timedelta(seconds=3))
    provider.instance = GuardedInstance(INSTANCE_ID, f"changed--nonce-{NONCE}")

    refused = worker.tick(NONCE, now=NOW + timedelta(seconds=4))

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
    worker.tick(NONCE, now=NOW + timedelta(seconds=3))
    provider.duplicates = True

    refused = worker.tick(NONCE, now=NOW + timedelta(seconds=4))

    assert refused.status == "OWNERSHIP_MISMATCH"
    assert refused.absence_observations == ()


def test_bound_destroy_requires_matching_exact_id_and_label_inventory(tmp_path) -> None:
    class MissingLabelInventoryProvider(FakeProvider):
        def find_instances(self, label: str) -> tuple[GuardedInstance, ...]:
            return ()

    provider = MissingLabelInventoryProvider()
    worker = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(seconds=1), require_root_owner=False)
    worker.arm(INSTANCE_ID, LABEL, NONCE, NOW + timedelta(minutes=1), now=NOW)

    refused = worker.tick(NONCE, now=NOW + timedelta(seconds=2))

    assert refused.status == "TEARDOWN_ERROR"
    assert provider.destroy_calls == []


def test_bound_destroy_refuses_nonce_label_resolved_to_another_id(tmp_path) -> None:
    class MovedLabelProvider(FakeProvider):
        def find_instances(self, label: str) -> tuple[GuardedInstance, ...]:
            return (GuardedInstance(INSTANCE_ID + 1, label),)

    provider = MovedLabelProvider()
    worker = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(seconds=1), require_root_owner=False)
    worker.arm(INSTANCE_ID, LABEL, NONCE, NOW + timedelta(minutes=1), now=NOW)

    refused = worker.tick(NONCE, now=NOW + timedelta(seconds=2))

    assert refused.status == "OWNERSHIP_MISMATCH"
    assert provider.destroy_calls == []


def test_three_journaled_observations_recover_as_confirmed_without_confirmation_event(tmp_path) -> None:
    provider = FakeProvider()
    worker = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(seconds=1), require_root_owner=False)
    worker.arm(INSTANCE_ID, LABEL, NONCE, NOW + timedelta(minutes=1), now=NOW)
    for seconds in (2, 3, 4, 5):
        worker.tick(NONCE, now=NOW + timedelta(seconds=seconds))

    directory = tmp_path / NONCE
    journal = directory / "journal.ndjson"
    records = journal.read_bytes().splitlines()
    assert json.loads(records[-1])["event"] == "absence_confirmed"
    journal.write_bytes(b"\n".join(records[:-1]) + b"\n")
    state_path = directory / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["status"] = "ABSENCE_PENDING"
    state["absence_observations"] = state["absence_observations"][:2]
    state_path.write_text(json.dumps(state), encoding="utf-8")

    reopened = GuardWorker(tmp_path, provider, heartbeat_timeout=timedelta(seconds=1), require_root_owner=False)
    recovered = reopened.status(NONCE)

    assert recovered.status == "ABSENCE_CONFIRMED"
    assert len(recovered.absence_observations) == 3


def test_read_only_quorum_can_finish_after_retry_window_provider_failure(tmp_path) -> None:
    class WindowProvider(FakeProvider):
        fail_next_read = False

        def get_instance(self, instance_id: int) -> GuardedInstance | None:
            if self.fail_next_read:
                self.fail_next_read = False
                raise TimeoutError("provider unavailable")
            return super().get_instance(instance_id)

        def destroy_exact(self, instance_id: int, expected_label: str) -> None:
            self.destroy_calls.append((instance_id, expected_label))

    provider = WindowProvider()
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
    provider.fail_next_read = True

    exhausted = worker.tick(NONCE, now=deadline + timedelta(seconds=31))
    provider.instance = None
    worker.tick(NONCE, now=deadline + timedelta(seconds=32))
    worker.tick(NONCE, now=deadline + timedelta(seconds=33))
    confirmed = worker.tick(NONCE, now=deadline + timedelta(seconds=34))

    assert exhausted.status == "TEARDOWN_RETRIES_EXHAUSTED"
    assert confirmed.status == "ABSENCE_CONFIRMED"
    assert provider.destroy_calls == [(INSTANCE_ID, LABEL)]


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

    assert receipt.status == "TEARDOWN_RETRIES_EXHAUSTED"
    assert provider.destroy_calls == [(INSTANCE_ID, LABEL)]
