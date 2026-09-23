from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from guard.guard_worker import GuardSafetyError, VastCliGuardProvider, label_binds_nonce, nonce_bound_label
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


def test_guard_refuses_missing_or_compatibility_vast_binary(tmp_path: Path) -> None:
    with pytest.raises(GuardSafetyError, match="vastai binary"):
        VastCliGuardProvider(tmp_path / "secret", vast_bin=str(tmp_path / "vastai"))
    with pytest.raises(GuardSafetyError, match="actual vastai"):
        VastCliGuardProvider(tmp_path / "secret", vast_bin=str(tmp_path / "vast"))
