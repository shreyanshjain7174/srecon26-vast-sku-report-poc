from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from guard.guard_worker import GuardSafetyError, GuardWorker, VastCliGuardProvider, label_binds_nonce, nonce_bound_label
from srecon26_poc.guard import GuardAttestation, GuardRejected, validate_attestation
from srecon26_poc.types import RunIdentity


def test_guard_requires_independent_identity_and_immutable_deadline():
    identity = RunIdentity("run", "label", datetime(2026, 9, 23, tzinfo=UTC))
    attestation = GuardAttestation("guard.example", "abc", "nonce", "label", identity.created_at + timedelta(minutes=5), None)
    validate_attestation(identity, attestation)
    with pytest.raises(GuardRejected):
        validate_attestation(identity, GuardAttestation("localhost", "abc", "nonce", "label", attestation.hard_deadline, None))


def test_vast_raw_record_uses_exact_nonce_bound_label_without_raw_nonce(tmp_path: Path) -> None:
    nonce = "nonce-0123456789abcdef"
    label = nonce_bound_label("srecon26-run-guard", nonce)
    binary = tmp_path / "vastai"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    provider = VastCliGuardProvider(tmp_path / "secret", vast_bin=str(binary))
    provider._run = lambda _args: [{"id": 417, "label": label}]  # type: ignore[method-assign]

    assert provider.find_instances(label) == (provider._instance({"id": 417, "label": label}),)
    assert label_binds_nonce(label, nonce)
    assert not label_binds_nonce("srecon26-run-guard", nonce)


@pytest.mark.parametrize("raw", ([{}], [{"id": 417}], [{"label": "run"}], ["malformed"]))
def test_vast_label_inventory_refuses_malformed_records(tmp_path: Path, raw: object) -> None:
    binary = tmp_path / "vastai"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    provider = VastCliGuardProvider(tmp_path / "secret", vast_bin=str(binary))
    provider._run = lambda _args: raw  # type: ignore[method-assign]

    with pytest.raises(GuardSafetyError, match="invalid"):
        provider.find_instances("run--nonce-12345678")


@pytest.mark.parametrize("raw", ([], {"id": 417}, {"label": "run"}, "malformed"))
def test_vast_exact_instance_refuses_unknown_response_instead_of_claiming_absence(tmp_path: Path, raw: object) -> None:
    binary = tmp_path / "vastai"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    provider = VastCliGuardProvider(tmp_path / "secret", vast_bin=str(binary))
    provider._run = lambda _args: raw  # type: ignore[method-assign]

    with pytest.raises(GuardSafetyError):
        provider.get_instance(417)


def test_vast_exact_instance_accepts_only_explicit_empty_object_as_absence(tmp_path: Path) -> None:
    binary = tmp_path / "vastai"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    provider = VastCliGuardProvider(tmp_path / "secret", vast_bin=str(binary))
    provider._run = lambda _args: {}  # type: ignore[method-assign]

    assert provider.get_instance(417) is None


def test_vast_listing_refuses_error_envelope_even_when_instances_field_is_empty(tmp_path: Path) -> None:
    binary = tmp_path / "vastai"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    provider = VastCliGuardProvider(tmp_path / "secret", vast_bin=str(binary))
    provider._run = lambda _args: {"instances": [], "error": "unauthorized"}  # type: ignore[method-assign]

    with pytest.raises(GuardSafetyError, match="unexpected response"):
        provider.find_instances("run--nonce-12345678")


def test_guard_refuses_missing_or_compatibility_vast_binary(tmp_path: Path) -> None:
    with pytest.raises(GuardSafetyError, match="vastai binary"):
        VastCliGuardProvider(tmp_path / "secret", vast_bin=str(tmp_path / "vastai"))
    with pytest.raises(GuardSafetyError, match="actual vastai"):
        VastCliGuardProvider(tmp_path / "secret", vast_bin=str(tmp_path / "vast"))


def test_provider_preflight_is_read_only_when_authorized_account_is_empty(tmp_path: Path) -> None:
    class ReadOnlyProvider:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def instance_inventory_count(self) -> int:
            self.calls.append("instance_inventory_count")
            return 0

        def find_instances(self, label: str):  # pragma: no cover - must not be called
            raise AssertionError(label)

        def get_instance(self, instance_id: int):  # pragma: no cover - must not be called
            raise AssertionError(instance_id)

        def destroy_exact(self, instance_id: int, expected_label: str) -> None:  # pragma: no cover - must not be called
            raise AssertionError((instance_id, expected_label))

    provider = ReadOnlyProvider()
    worker = GuardWorker(tmp_path, provider, require_root_owner=False)

    assert worker.provider_preflight() == {"instance_count": 0, "status": "PROVIDER_PREFLIGHT_READY"}
    assert provider.calls == ["instance_inventory_count"]
    assert list(tmp_path.iterdir()) == []


def test_provider_preflight_rejects_nonempty_inventory_without_mutation(tmp_path: Path) -> None:
    class ExistingInventoryProvider:
        def instance_inventory_count(self) -> int:
            return 1

    worker = GuardWorker(tmp_path, ExistingInventoryProvider(), require_root_owner=False)

    with pytest.raises(GuardSafetyError, match="existing instances"):
        worker.provider_preflight()
    assert list(tmp_path.iterdir()) == []


def test_provider_preflight_propagates_provider_authorization_failure(tmp_path: Path) -> None:
    class UnauthorizedProvider:
        def instance_inventory_count(self) -> int:
            raise GuardSafetyError("provider credential was rejected")

    worker = GuardWorker(tmp_path, UnauthorizedProvider(), require_root_owner=False)

    with pytest.raises(GuardSafetyError, match="credential was rejected"):
        worker.provider_preflight()
    assert list(tmp_path.iterdir()) == []


def test_vast_inventory_preflight_uses_only_the_read_only_instance_listing(tmp_path: Path) -> None:
    binary = tmp_path / "vastai"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    provider = VastCliGuardProvider(tmp_path / "secret", vast_bin=str(binary))
    calls: list[list[str]] = []

    def read_only_listing(arguments: list[str]) -> object:
        calls.append(arguments)
        return []

    provider._run = read_only_listing  # type: ignore[method-assign]

    assert provider.instance_inventory_count() == 0
    assert calls == [["show", "instances", "--raw"]]
