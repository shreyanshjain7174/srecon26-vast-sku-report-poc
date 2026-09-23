from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from .types import FaultClass


class ProbeOutcome(StrEnum):
    PASS = "PASS"
    MODEL_DOWNLOAD_FAILED = "MODEL_DOWNLOAD_FAILED"
    CONTROLLER_FAILED = "CONTROLLER_FAILED"
    NETWORK_FAILED = "NETWORK_FAILED"


@dataclass(frozen=True, slots=True)
class OfferContract:
    offer_id: int
    gpu_name: str
    num_gpus: int
    gpu_ram_mib: int
    compute_capability: str
    machine_id: int
    dph_total: Decimal
    label: str


@dataclass(frozen=True, slots=True)
class InstanceContract:
    instance_id: int
    gpu_name: str
    num_gpus: int
    gpu_ram_mib: int
    compute_capability: str
    machine_id: int
    dph_total: Decimal
    label: str


def classify_fault(offer: OfferContract, instance: InstanceContract, probe: ProbeOutcome) -> FaultClass:
    if probe is not ProbeOutcome.PASS:
        return FaultClass.DIAGNOSIS_UNRESOLVED
    expected = (offer.gpu_name, offer.num_gpus, offer.gpu_ram_mib, offer.compute_capability, offer.machine_id, offer.label)
    actual = (instance.gpu_name, instance.num_gpus, instance.gpu_ram_mib, instance.compute_capability, instance.machine_id, instance.label)
    harmful_price_change = instance.dph_total > offer.dph_total
    return FaultClass.PROVIDER_FAULT_CONFIRMED if actual != expected or harmful_price_change else FaultClass.DIAGNOSIS_UNRESOLVED
