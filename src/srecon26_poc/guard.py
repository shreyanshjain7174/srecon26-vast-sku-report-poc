from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .types import RunIdentity


class GuardRejected(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GuardAttestation:
    host_identity: str
    script_hash: str
    nonce: str
    label: str
    hard_deadline: datetime
    last_heartbeat: datetime | None


class Guard(Protocol):
    def preflight(self) -> GuardAttestation: ...
    def arm(self, identity: RunIdentity, hard_deadline: datetime) -> GuardAttestation: ...
    def record_heartbeat(self, identity: RunIdentity, monotonic_ns: int) -> None: ...
    def anchor(self, root_hash: str) -> str: ...


def validate_attestation(identity: RunIdentity, attestation: GuardAttestation) -> None:
    if not attestation.host_identity or attestation.host_identity in {"localhost", "127.0.0.1", "::1"}:
        raise GuardRejected("guard must attest an independently reachable host")
    if not attestation.script_hash or not attestation.nonce or attestation.label != identity.label:
        raise GuardRejected("guard attestation has invalid ownership material")
    if attestation.hard_deadline <= identity.created_at:
        raise GuardRejected("guard deadline must be fixed after run creation")
