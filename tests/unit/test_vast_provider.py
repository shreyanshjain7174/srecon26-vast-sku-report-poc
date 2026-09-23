from __future__ import annotations

import json
import stat
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from srecon26_poc.contracts import OfferContract
from srecon26_poc.provider import AccountSnapshot
from srecon26_poc.vast_provider import VastCliProvider, VastPreflightError


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
    assert snapshot.autobill_enabled is False
    assert snapshot.instance_count == 0
    assert snapshot.offer_count == 1
    assert "api_key" not in encoded
    assert "must-not-escape-fixture" not in encoded
    assert "operator@example.test" not in encoded


def test_offer_contract_is_normalized_from_current_search_result(fixture_cli: Path) -> None:
    provider = VastCliProvider(fixture_cli, timeout_seconds=1)

    contract = provider.get_offer(101, label="phase2-nonce-label")

    assert contract == OfferContract(101, "RTX 3090", 1, 24576, "8.6", 99, contract.dph_total, "phase2-nonce-label")
    assert str(contract.dph_total) == "0.30"


def test_preflight_rejects_enabled_or_unknown_autobilling(fixture_cli: Path, tmp_path: Path) -> None:
    provider = VastCliProvider(fixture_cli, timeout_seconds=1)
    account = tmp_path / "account.json"
    account.write_text('{"autobill": true}', encoding="utf-8")
    original = provider._run_json

    def account_with_autobill(args: list[str]) -> object:
        if args == ["show", "user"]:
            return json.loads(account.read_text(encoding="utf-8"))
        return original(args)

    provider._run_json = account_with_autobill  # type: ignore[method-assign]
    with pytest.raises(VastPreflightError, match="autobilling"):
        provider.read_only_preflight("rentable=true", limit=1, require_ready=True)


@pytest.mark.parametrize("limit", [0, 26])
def test_preflight_bounds_offer_inspection(fixture_cli: Path, limit: int) -> None:
    with pytest.raises(VastPreflightError, match="limit"):
        VastCliProvider(fixture_cli, timeout_seconds=1).read_only_preflight("rentable=true", limit=limit)


def test_create_response_may_be_a_single_json_record() -> None:
    record = '{"id": 77, "gpu_name": "RTX 3090", "num_gpus": 1, "gpu_ram": 24, "compute_cap": 860, "machine_id": 99, "dph_total": 0.30, "label": "run-nonce-label"}'
    provider = VastCliProvider("fixture", runner=lambda _args, _timeout: record)
    contract = OfferContract(101, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), "run-nonce-label")

    assert provider.create_once(contract, "run-id").instance_id == 77
