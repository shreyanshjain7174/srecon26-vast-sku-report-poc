from datetime import UTC, datetime, timedelta

import pytest

from srecon26_poc.guard import GuardAttestation, GuardRejected, validate_attestation
from srecon26_poc.types import RunIdentity


def test_guard_requires_independent_identity_and_immutable_deadline():
    identity = RunIdentity("run", "label", datetime(2026, 9, 23, tzinfo=UTC))
    attestation = GuardAttestation("guard.example", "abc", "nonce", "label", identity.created_at + timedelta(minutes=5), None)
    validate_attestation(identity, attestation)
    with pytest.raises(GuardRejected):
        validate_attestation(identity, GuardAttestation("localhost", "abc", "nonce", "label", attestation.hard_deadline, None))
