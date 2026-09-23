from decimal import Decimal

import pytest

from srecon26_poc.budget import BudgetExceeded, ExposureLedger


def test_cumulative_reservation_above_five_dollars_fails(tmp_path):
    ledger = ExposureLedger(tmp_path / "ledger.json")
    ledger.reserve("run-a", Decimal("2.00"), "paired-comparison")
    with pytest.raises(BudgetExceeded):
        ledger.reserve("run-b", Decimal("3.01"), "paired-comparison")


def test_exact_cap_and_category_caps_are_decimal_and_durable(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = ExposureLedger(path)
    ledger.reserve("gpu", Decimal("1.00"), "gpu-smoke")
    ledger.reserve("canary", Decimal("1.00"), "canary")
    ledger.reserve("comparison", Decimal("3.00"), "paired-comparison")
    assert ledger.headroom() == Decimal("0.00")
    assert ExposureLedger(path).headroom() == Decimal("0.00")
    with pytest.raises(BudgetExceeded):
        ledger.reserve("another", Decimal("0.01"), "paired-comparison")


def test_actual_spend_does_not_release_reservation_without_absence_proof(tmp_path):
    ledger = ExposureLedger(tmp_path / "ledger.json")
    ledger.reserve("run-a", Decimal("1.00"), "gpu-smoke")
    with pytest.raises(ValueError, match="absence proof"):
        ledger.commit_actual("run-a", Decimal("0.01"), None)
    assert ledger.headroom() == Decimal("4.00")
    ledger.commit_actual("run-a", Decimal("0.01"), {"reads": 3})
    assert ledger.headroom() == Decimal("4.00")
