from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .contracts import InstanceContract, OfferContract


class AmbiguousCreate(RuntimeError):
    """A create result whose provider-side outcome must be reconciled by unique label, never retried."""


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    instance_count: int


class Provider(Protocol):
    def account_snapshot(self) -> AccountSnapshot: ...
    def list_instances(self) -> tuple[InstanceContract, ...]: ...
    def get_offer(self, offer_id: int) -> OfferContract: ...
    def create_once(self, contract: OfferContract, request_key: str) -> InstanceContract: ...
    def get_instance(self, instance_id: int) -> InstanceContract: ...
    def destroy_exact(self, instance_id: int, expected_label: str) -> None: ...
