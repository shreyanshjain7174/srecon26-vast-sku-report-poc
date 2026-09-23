from decimal import Decimal
from pathlib import Path

import pytest

from srecon26_poc.contracts import InstanceContract, OfferContract, ProbeOutcome, classify_fault
from srecon26_poc.types import FaultClass


@pytest.fixture
def offer() -> OfferContract:
    return OfferContract(1, "RTX 3090", 1, 24576, "8.6", 9, Decimal("0.30"), "run-label")


@pytest.fixture
def instance() -> InstanceContract:
    return InstanceContract(42, "RTX 3090", 1, 24576, "8.6", 9, Decimal("0.30"), "run-label")


@pytest.mark.parametrize("field", ["gpu_name", "num_gpus", "gpu_ram_mib", "compute_capability", "machine_id", "dph_total", "label"])
def test_offer_contract_mismatch_is_provider_fault(field, offer, instance):
    changed = {field: "wrong" if field in {"gpu_name", "compute_capability", "label"} else 999}
    if field == "dph_total":
        changed[field] = Decimal("9.99")
    assert classify_fault(offer, instance.__class__(**{**instance.__dict__, **changed}), ProbeOutcome.PASS) is FaultClass.PROVIDER_FAULT_CONFIRMED


def test_model_download_failure_is_unresolved(offer, instance):
    assert classify_fault(offer, instance, ProbeOutcome.MODEL_DOWNLOAD_FAILED) is FaultClass.DIAGNOSIS_UNRESOLVED
