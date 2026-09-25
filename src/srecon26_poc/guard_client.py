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


def _label_binds_nonce(label: str, nonce: str) -> bool:
    """Check the provider-visible nonce-bound label protocol.

    Vast raw instance records have a label but no independent nonce field, so
    a remote guard can only verify ownership through this exact label.
    """
    delimiter = f"--nonce-{nonce}"
    prefix = label.removesuffix(delimiter)
    return bool(prefix) and prefix != label and "--nonce-" not in prefix


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
    azure_resource_id: str | None = None
    azure_vm_id: str | None = None
    host_key_fingerprint: str | None = None
    heartbeat_timeout_seconds: int | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "GuardRemoteReceipt":
        root_hash = str(payload.get("root_hash", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", root_hash):
            raise GuardClientError("guard receipt lacks a sha256 root hash")
        heartbeat_timeout = payload.get("heartbeat_timeout_seconds")
        if heartbeat_timeout is not None and (
            isinstance(heartbeat_timeout, bool)
            or not isinstance(heartbeat_timeout, int)
            or not 1 <= heartbeat_timeout <= 600
        ):
            raise GuardClientError("guard receipt has an invalid heartbeat timeout")
        return cls(
            status=str(payload.get("status", "")),
            root_hash=root_hash,
            nonce=str(payload["nonce"]) if "nonce" in payload else None,
            label=str(payload["label"]) if "label" in payload else None,
            hard_deadline=_parse(payload["hard_deadline"]) if "hard_deadline" in payload else None,
            last_heartbeat=_parse(payload["last_heartbeat"]) if payload.get("last_heartbeat") else None,
            host_identity=str(payload["host_identity"]) if "host_identity" in payload else None,
            script_hash=str(payload["script_hash"]) if "script_hash" in payload else None,
            azure_resource_id=str(payload["azure_resource_id"]) if "azure_resource_id" in payload else None,
            azure_vm_id=str(payload["azure_vm_id"]) if "azure_vm_id" in payload else None,
            host_key_fingerprint=str(payload["host_key_fingerprint"]) if "host_key_fingerprint" in payload else None,
            heartbeat_timeout_seconds=heartbeat_timeout,
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
        return GuardAttestation(
            receipt.host_identity,
            receipt.script_hash,
            self.nonce,
            armed.label,
            armed.hard_deadline,
            armed.last_heartbeat,
            receipt.azure_resource_id,
            receipt.azure_vm_id,
            receipt.host_key_fingerprint,
            receipt.heartbeat_timeout_seconds,
        )

    def arm(
        self,
        identity: RunIdentity,
        hard_deadline: datetime,
        *,
        heartbeat_timeout_seconds: int | None = None,
    ) -> GuardRemoteReceipt:
        if not _label_binds_nonce(identity.label, self.nonce):
            raise GuardClientError("run label does not use the exact nonce-bound provider label protocol")
        payload: dict[str, object] = {
            "run_id": identity.run_id,
            "label": identity.label,
            "nonce": self.nonce,
            "hard_deadline": _stamp(hard_deadline),
        }
        if heartbeat_timeout_seconds is not None:
            payload["heartbeat_timeout_seconds"] = heartbeat_timeout_seconds
        receipt = GuardRemoteReceipt.from_mapping(self.transport.call("arm", payload))
        if receipt.nonce != self.nonce or receipt.label != identity.label or receipt.hard_deadline != hard_deadline.astimezone(UTC):
            raise GuardClientError("guard arm receipt does not bind the requested nonce, label, and deadline")
        if heartbeat_timeout_seconds is not None and receipt.heartbeat_timeout_seconds != heartbeat_timeout_seconds:
            raise GuardClientError("guard arm receipt does not attest the configured heartbeat timeout")
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
