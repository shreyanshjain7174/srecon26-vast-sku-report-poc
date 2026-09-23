from __future__ import annotations

import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from srecon26_poc.contracts import InstanceContract, OfferContract
from srecon26_poc.provider import AmbiguousCreate
from srecon26_poc.vast_provider import OFFICIAL_KVM_IMAGE, OFFICIAL_UBUNTU_2204_TEMPLATE_HASH, VastCliProvider, VastLaunchContract


def test_ambiguous_create_reconciles_unique_nonce_label_without_retry(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    contract = OfferContract(101, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), "run-nonce-label")
    created = InstanceContract(77, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), "run-nonce-label")

    def runner(args: list[str], timeout: int) -> str:
        calls.append(args)
        if args[3:6] == ["create", "instance", "101"]:
            raise subprocess.TimeoutExpired(args, timeout)
        if args[-2:] == ["show", "instances"]:
            return '[{"id": 77, "gpu_name": "RTX 3090", "num_gpus": 1, "gpu_ram": 24, "compute_cap": 860, "machine_id": 99, "dph_total": 0.30, "label": "run-nonce-label"}]'
        raise AssertionError(args)

    provider = VastCliProvider(Path("vastai"), timeout_seconds=1, runner=runner)

    with pytest.raises(AmbiguousCreate):
        provider.create_once(contract, "run-id", VastLaunchContract(OFFICIAL_UBUNTU_2204_TEMPLATE_HASH, OFFICIAL_KVM_IMAGE))
    assert provider.reconcile_label(contract.label) == created
    assert sum("create" in command for command in calls) == 1


def test_reconcile_label_rejects_zero_or_multiple_matches() -> None:
    provider = VastCliProvider(Path("vastai"), timeout_seconds=1, runner=lambda _args, _timeout: "[]")
    with pytest.raises(AmbiguousCreate, match="zero"):
        provider.reconcile_label("run-nonce-label")

    provider = VastCliProvider(
        Path("vastai"),
        timeout_seconds=1,
        runner=lambda _args, _timeout: """[
            {"id": 77, "gpu_name": "RTX 3090", "num_gpus": 1, "gpu_ram": 24, "compute_cap": 860, "machine_id": 99, "dph_total": 0.30, "label": "run-nonce-label"},
            {"id": 78, "gpu_name": "RTX 3090", "num_gpus": 1, "gpu_ram": 24, "compute_cap": 860, "machine_id": 99, "dph_total": 0.30, "label": "run-nonce-label"}
        ]""",
    )
    with pytest.raises(AmbiguousCreate, match="multiple"):
        provider.reconcile_label("run-nonce-label")
