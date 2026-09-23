from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.reconcile_vast_billing import _reserve_fresh_outputs


def test_reconciliation_refuses_input_aliases_and_duplicate_outputs(tmp_path: Path) -> None:
    ledger = tmp_path / "exposure-ledger.json"
    reconciliation = tmp_path / "reconciliation.json"
    journal = tmp_path / "journal.ndjson"
    ledger.write_text(json.dumps({"reservations": {}}))
    reconciliation.write_text("{}")
    journal.write_text("")

    with pytest.raises(ValueError, match="distinct"):
        _reserve_fresh_outputs(ledger=ledger, reconciliation=reconciliation, journal=journal, invoice=ledger, absence=tmp_path / "absence.json")
    with pytest.raises(ValueError, match="distinct"):
        _reserve_fresh_outputs(ledger=ledger, reconciliation=reconciliation, journal=journal, invoice=tmp_path / "same.json", absence=tmp_path / "same.json")


def test_reconciliation_exclusively_reserves_only_fresh_output_paths(tmp_path: Path) -> None:
    ledger = tmp_path / "exposure-ledger.json"
    reconciliation = tmp_path / "reconciliation.json"
    journal = tmp_path / "journal.ndjson"
    ledger.write_text(json.dumps({"reservations": {}}))
    reconciliation.write_text("{}")
    journal.write_text("")
    invoice = tmp_path / "invoice-refresh.json"
    absence = tmp_path / "absence-refresh.json"

    _reserve_fresh_outputs(ledger=ledger, reconciliation=reconciliation, journal=journal, invoice=invoice, absence=absence)
    assert invoice.exists() and absence.exists()
    with pytest.raises(ValueError, match="fresh, exclusively created"):
        _reserve_fresh_outputs(ledger=ledger, reconciliation=reconciliation, journal=journal, invoice=invoice, absence=tmp_path / "another.json")
    assert not (tmp_path / "another.json").exists()


def test_reconciliation_refuses_outputs_outside_ledger_directory(tmp_path: Path) -> None:
    root = tmp_path / "ledger-root"
    root.mkdir()
    with pytest.raises(ValueError, match="under the ledger directory"):
        _reserve_fresh_outputs(
            ledger=root / "ledger.json",
            reconciliation=root / "reconciliation.json",
            journal=root / "journal.ndjson",
            invoice=tmp_path / "outside.json",
            absence=root / "absence.json",
        )
