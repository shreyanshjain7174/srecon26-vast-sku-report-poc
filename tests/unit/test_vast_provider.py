from __future__ import annotations

import json
import stat
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from srecon26_poc.contracts import OfferContract
from srecon26_poc.provider import AccountSnapshot
from srecon26_poc.provider import AmbiguousCreate
from srecon26_poc.vast_provider import (
    OFFICIAL_KVM_IMAGE,
    OFFICIAL_UBUNTU_2204_TEMPLATE_HASH,
    VastCliProvider,
    VastLaunchContract,
    VastPreflightError,
    VastProviderError,
)


FIXTURES = Path(__file__).parents[2] / "providers" / "vast" / "fixtures"


@pytest.fixture
def fixture_cli(tmp_path: Path) -> Path:
    fixture_root = FIXTURES.as_posix()
    script = tmp_path / "vastai-fixture"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"root = {fixture_root!r}\n"
        "args = [arg for arg in sys.argv[1:] if arg not in {'--raw', '--no-color'}]\n"
        "if args[:2] == ['show', 'user']:\n"
        "  print(open(root + '/account.json').read())\n"
        "elif args[:2] == ['show', 'instances']:\n"
        "  print(open(root + '/instances.json').read())\n"
        "elif args[:2] == ['search', 'offers']:\n"
        "  print(open(root + '/offers.json').read())\n"
        "else:\n"
        "  raise SystemExit('unexpected command: ' + repr(args))\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_account_snapshot_and_preflight_redact_secrets(fixture_cli: Path) -> None:
    provider = VastCliProvider(fixture_cli, timeout_seconds=1)

    assert provider.account_snapshot() == AccountSnapshot(instance_count=0)
    snapshot = provider.read_only_preflight("external=false rentable=true verified=true", limit=5)

    encoded = json.dumps(snapshot.to_json(), sort_keys=True)
    assert snapshot.balance_threshold_enabled is False
    assert snapshot.instance_count == 0
    assert snapshot.offer_count == 1
    assert set(snapshot.to_json()) == {"balance_threshold_enabled", "instance_count", "offer_count", "offers"}
    assert "api_key" not in encoded
    assert "must-not-escape-fixture" not in encoded
    assert "operator@example.test" not in encoded
    assert "must-not-escape-user-metadata" not in encoded


def test_offer_contract_is_normalized_from_current_search_result(fixture_cli: Path) -> None:
    provider = VastCliProvider(fixture_cli, timeout_seconds=1)

    contract = provider.get_offer(101, label="phase2-nonce-label")

    assert contract == OfferContract(101, "RTX 3090", 1, 24576, "8.6", 99, contract.dph_total, "phase2-nonce-label")
    assert str(contract.dph_total) == "0.30"


def test_kvm_offer_refreeze_uses_machine_query_then_exact_offer_id(fixture_cli: Path) -> None:
    provider = VastCliProvider(fixture_cli, timeout_seconds=1)

    contract = provider.get_vms_enabled_offer(101, machine_id=99, label="phase2-nonce-label")

    assert contract.offer_id == 101
    assert contract.machine_id == 99


@pytest.mark.parametrize(
    "account_payload",
    [
        {},
        {"balance_threshold_enabled": True},
        {"balance_threshold_enabled": False, "autobill": True},
    ],
)
def test_preflight_rejects_enabled_unknown_or_disagreeing_autorecharge(
    fixture_cli: Path, tmp_path: Path, account_payload: dict[str, object]
) -> None:
    provider = VastCliProvider(fixture_cli, timeout_seconds=1)
    account = tmp_path / "account.json"
    account.write_text(json.dumps(account_payload), encoding="utf-8")
    original = provider._run_json

    def account_with_autobill(args: list[str]) -> object:
        if args == ["show", "user"]:
            return json.loads(account.read_text(encoding="utf-8"))
        return original(args)

    provider._run_json = account_with_autobill  # type: ignore[method-assign]
    with pytest.raises(VastPreflightError, match="auto-recharge|disagrees"):
        provider.read_only_preflight("rentable=true", limit=1, require_ready=True)


def test_legacy_disabled_autobill_cannot_authorize_preflight(fixture_cli: Path) -> None:
    provider = VastCliProvider(fixture_cli, timeout_seconds=1)
    original = provider._run_json

    def legacy_only_account(args: list[str]) -> object:
        if args == ["show", "user"]:
            return {"autobill": False}
        return original(args)

    provider._run_json = legacy_only_account  # type: ignore[method-assign]
    with pytest.raises(VastPreflightError, match="auto-recharge"):
        provider.read_only_preflight("rentable=true", limit=1, require_ready=True)


@pytest.mark.parametrize("limit", [0, 26])
def test_preflight_bounds_offer_inspection(fixture_cli: Path, limit: int) -> None:
    with pytest.raises(VastPreflightError, match="limit"):
        VastCliProvider(fixture_cli, timeout_seconds=1).read_only_preflight("rentable=true", limit=limit)


def test_create_response_may_be_a_single_json_record() -> None:
    record = '{"id": 77, "gpu_name": "RTX 3090", "num_gpus": 1, "gpu_ram": 24, "compute_cap": 860, "machine_id": 99, "dph_total": 0.30, "label": "run-nonce-label"}'
    provider = VastCliProvider("fixture", runner=lambda _args, _timeout: record)
    contract = OfferContract(101, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), "run-nonce-label")

    assert provider.create_once(contract, "run-id", VastLaunchContract(OFFICIAL_UBUNTU_2204_TEMPLATE_HASH, OFFICIAL_KVM_IMAGE)).instance_id == 77


def test_create_acknowledgement_requires_label_reconciliation() -> None:
    provider = VastCliProvider("fixture", runner=lambda _args, _timeout: '{"success": true, "new_contract": 77}')
    contract = OfferContract(101, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), "run-nonce-label")

    with pytest.raises(AmbiguousCreate, match="reconcile"):
        provider.create_once(contract, "run-id", VastLaunchContract(OFFICIAL_UBUNTU_2204_TEMPLATE_HASH, OFFICIAL_KVM_IMAGE))


def test_create_requires_explicit_frozen_kvm_launch_contract_and_exact_arguments() -> None:
    calls: list[list[str]] = []
    record = '{"id": 77, "gpu_name": "RTX 3090", "num_gpus": 1, "gpu_ram": 24, "compute_cap": 860, "machine_id": 99, "dph_total": 0.30, "label": "run-nonce-label"}'
    provider = VastCliProvider("fixture", runner=lambda args, _timeout: calls.append(args) or record)
    contract = OfferContract(101, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), "run-nonce-label")

    created = provider.create_once(contract, "run-id", VastLaunchContract(OFFICIAL_UBUNTU_2204_TEMPLATE_HASH, OFFICIAL_KVM_IMAGE))

    assert created.instance_id == 77
    assert calls == [[
        "fixture", "--raw", "--no-color", "create", "instance", "101",
        "--template_hash", OFFICIAL_UBUNTU_2204_TEMPLATE_HASH, "--disk", "130", "--ssh", "--cancel-unavail", "--label", "run-nonce-label",
    ]]


def test_destroy_uses_noninteractive_yes_after_exact_label_check() -> None:
    calls: list[list[str]] = []
    record = '[{"id": 77, "gpu_name": "RTX 3090", "num_gpus": 1, "gpu_ram": 24, "compute_cap": 860, "machine_id": 99, "dph_total": 0.30, "label": "run-nonce-label"}]'

    def runner(args: list[str], _timeout: int) -> str:
        calls.append(args)
        return record if "show" in args else ""

    provider = VastCliProvider("fixture", runner=runner)
    provider.destroy_exact(77, "run-nonce-label")

    assert calls[-1][-4:] == ["destroy", "instance", "77", "--yes"]


def test_invoice_capture_requires_and_records_exact_instance_and_label(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    response = json.dumps([
        {"amount": 0.004, "source": "instance-77", "type": "instance", "metadata": {"label": "run-nonce-label"}},
        {"amount": 1.0, "source": "instance-88", "type": "instance", "metadata": {"label": "other"}},
    ])
    provider = VastCliProvider("fixture", runner=lambda args, _timeout: calls.append(args) or response)
    artifact = tmp_path / "invoice.json"

    evidence = provider.capture_invoice_charge(run_id="run-id", instance_id=77, label="run-nonce-label", start_date="2026-09-23", end_date="2026-09-24", artifact=artifact)

    payload = json.loads(artifact.read_text())
    assert evidence.amount == Decimal("0.004")
    assert payload["source"] == "VastCliProvider.capture_invoice_charge/v1"
    assert payload["provider_charge"]["source"] == "instance-77"
    assert payload["provider_charge"]["metadata"]["label"] == "run-nonce-label"
    assert calls[0][:3] == ["fixture", "--raw", "--no-color"]


def test_invoice_capture_refuses_label_mismatch(tmp_path: Path) -> None:
    response = '[{"amount":0.004,"source":"instance-77","type":"instance","metadata":{"label":"other"}}]'
    provider = VastCliProvider("fixture", runner=lambda _args, _timeout: response)

    with pytest.raises(VastProviderError, match="exact"):
        provider.capture_invoice_charge(run_id="run-id", instance_id=77, label="run-nonce-label", start_date="2026-09-23", end_date="2026-09-24", artifact=tmp_path / "invoice.json")


def test_absence_capture_persists_three_fresh_zero_match_reads(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    provider = VastCliProvider("fixture", runner=lambda args, _timeout: calls.append(args) or "[]", sleeper=lambda _seconds: None)
    artifact = tmp_path / "absence.json"

    evidence = provider.capture_absence_evidence(run_id="run-id", instance_id=77, label="run-nonce-label", artifact=artifact, interval_seconds=0)

    payload = json.loads(artifact.read_text())
    assert evidence.sha256
    assert len(payload["reads"]) == 3
    assert [read["matching_instances"] for read in payload["reads"]] == [0, 0, 0]
    assert len(calls) == 3


@pytest.mark.parametrize(
    "launch",
    [
        VastLaunchContract(),
        VastLaunchContract(OFFICIAL_UBUNTU_2204_TEMPLATE_HASH, OFFICIAL_KVM_IMAGE, disk_gib=129),
        VastLaunchContract(OFFICIAL_UBUNTU_2204_TEMPLATE_HASH, "docker.io/vastai/kvm:latest"),
        VastLaunchContract(OFFICIAL_UBUNTU_2204_TEMPLATE_HASH, OFFICIAL_KVM_IMAGE, ssh=False),
        VastLaunchContract(ubuntu_template_hash="not-a-template", image_contract=OFFICIAL_KVM_IMAGE),
    ],
)
def test_create_refuses_missing_or_relaxed_vm_contract_before_cli_mutation(launch: VastLaunchContract) -> None:
    calls: list[list[str]] = []
    provider = VastCliProvider("fixture", runner=lambda args, _timeout: calls.append(args) or "[]")
    contract = OfferContract(101, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), "run-nonce-label")

    with pytest.raises(VastProviderError):
        provider.create_once(contract, "run-id", launch)

    assert calls == []
