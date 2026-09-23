from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import re
from typing import Mapping, Protocol

from .guard import GuardAttestation
from .types import RunIdentity


class GuardClientError(RuntimeError):
    pass


class GuardTransport(Protocol):
    def call(self, command: str, payload: dict[str, object]) -> dict[str, object]: ...


def _stamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise GuardClientError("guard timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class GuardRemoteReceipt:
    status: str
    root_hash: str
    nonce: str | None = None
    label: str | None = None
    hard_deadline: datetime | None = None
    last_heartbeat: datetime | None = None
    host_identity: str | None = None
    script_hash: str | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "GuardRemoteReceipt":
        root_hash = str(payload.get("root_hash", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", root_hash):
            raise GuardClientError("guard receipt lacks a sha256 root hash")
        return cls(
            status=str(payload.get("status", "")),
            root_hash=root_hash,
            nonce=str(payload["nonce"]) if "nonce" in payload else None,
            label=str(payload["label"]) if "label" in payload else None,
            hard_deadline=_parse(payload["hard_deadline"]) if "hard_deadline" in payload else None,
            last_heartbeat=_parse(payload["last_heartbeat"]) if payload.get("last_heartbeat") else None,
            host_identity=str(payload["host_identity"]) if "host_identity" in payload else None,
            script_hash=str(payload["script_hash"]) if "script_hash" in payload else None,
        )


class GuardClient:
    """Typed controller-side client; credentials are deliberately absent."""

    def __init__(self, transport: GuardTransport, *, nonce: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", nonce):
            raise GuardClientError("nonce must be 8-128 URL-safe characters")
        self.transport = transport
        self.nonce = nonce
        self._arm_receipt: GuardRemoteReceipt | None = None

    def preflight(self) -> GuardAttestation:
        receipt = GuardRemoteReceipt.from_mapping(self.transport.call("preflight", {}))
        armed = self._arm_receipt
        if not receipt.host_identity or not receipt.script_hash or armed is None or armed.label is None or armed.hard_deadline is None:
            raise GuardClientError("guard preflight receipt is incomplete")
        return GuardAttestation(receipt.host_identity, receipt.script_hash, self.nonce, armed.label, armed.hard_deadline, armed.last_heartbeat)

    def arm(self, identity: RunIdentity, hard_deadline: datetime) -> GuardRemoteReceipt:
        receipt = GuardRemoteReceipt.from_mapping(
            self.transport.call(
                "arm",
                {"run_id": identity.run_id, "label": identity.label, "nonce": self.nonce, "hard_deadline": _stamp(hard_deadline)},
            )
        )
        if receipt.nonce != self.nonce or receipt.label != identity.label or receipt.hard_deadline != hard_deadline.astimezone(UTC):
            raise GuardClientError("guard arm receipt does not bind the requested nonce, label, and deadline")
        self._arm_receipt = receipt
        return receipt

    def record_heartbeat(self, identity: RunIdentity, monotonic_ns: int) -> None:
        if monotonic_ns < 0:
            raise GuardClientError("monotonic heartbeat must be nonnegative")
        receipt = GuardRemoteReceipt.from_mapping(self.transport.call("heartbeat", {"run_id": identity.run_id, "label": identity.label, "nonce": self.nonce, "monotonic_ns": monotonic_ns}))
        if receipt.nonce != self.nonce or receipt.label != identity.label:
            raise GuardClientError("guard heartbeat receipt changed ownership")

    def anchor(self, root_hash: str) -> str:
        receipt = GuardRemoteReceipt.from_mapping(self.transport.call("anchor", {"nonce": self.nonce, "root_hash": root_hash}))
        return receipt.root_hash
