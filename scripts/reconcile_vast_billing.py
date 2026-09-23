#!/usr/bin/env python3
"""Reconcile one closed Vast reservation from exact invoice and journal evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from srecon26_poc.budget import ExposureLedger
from srecon26_poc.vast_provider import VastCliProvider


def _reserve_fresh_outputs(*, ledger: Path, reconciliation: Path, journal: Path, invoice: Path, absence: Path) -> None:
    """Exclusively reserve new evidence paths without touching any input."""

    inputs = {ledger.resolve(), reconciliation.resolve(), journal.resolve()}
    outputs = (invoice.resolve(), absence.resolve())
    if len(set(outputs)) != len(outputs) or inputs.intersection(outputs):
        raise ValueError("reconciliation evidence outputs must be distinct from each other and every input")
    ledger_root = ledger.parent.resolve()
    if any(ledger_root not in path.parents for path in outputs):
        raise ValueError("reconciliation evidence outputs must stay under the ledger directory")
    created: list[Path] = []
    try:
        for path in outputs:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            created.append(path)
    except OSError as error:
        for path in created:
            if path.is_file() and not path.is_symlink() and path.stat().st_size == 0:
                path.unlink()
        raise ValueError("reconciliation evidence outputs must be fresh, exclusively created paths") from error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--reconciliation", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--invoice-artifact", type=Path, required=True)
    parser.add_argument("--absence-artifact", type=Path, required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--vast-cli", default="vastai")
    args = parser.parse_args()

    ledger = ExposureLedger(args.ledger)
    ledger.headroom()  # Fully validate ledger and every bound artifact before any evidence write.
    receipt = json.loads(args.reconciliation.read_text())
    run_id = str(receipt["run_id"])
    instance_id = int(receipt["instance_id"])
    label = str(receipt["label"])
    _reserve_fresh_outputs(
        ledger=args.ledger,
        reconciliation=args.reconciliation,
        journal=args.journal,
        invoice=args.invoice_artifact,
        absence=args.absence_artifact,
    )
    provider = VastCliProvider(args.vast_cli)
    absence = provider.capture_absence_evidence(run_id=run_id, instance_id=instance_id, label=label, artifact=args.absence_artifact)
    evidence = provider.capture_invoice_charge(
        run_id=run_id,
        instance_id=instance_id,
        label=label,
        start_date=args.start_date,
        end_date=args.end_date,
        artifact=args.invoice_artifact,
    )
    proof = dict(receipt["absence_proof"])
    proof.update(
        {
            "instance_id": instance_id,
            "label": label,
            "billing": {
                "source": "provider_invoice",
                "artifact": str(args.invoice_artifact.relative_to(args.ledger.parent)),
                "sha256": evidence.sha256,
                "journal_artifact": str(args.journal.relative_to(args.ledger.parent)),
                "journal_sha256": hashlib.sha256(args.journal.read_bytes()).hexdigest(),
                "absence_artifact": str(args.absence_artifact.relative_to(args.ledger.parent)),
                "absence_sha256": absence.sha256,
            },
        }
    )
    ledger.commit_actual(run_id, evidence.amount, proof)
    print(json.dumps({"run_id": run_id, "amount_usd": str(evidence.amount), "invoice_sha256": evidence.sha256, "headroom": str(ledger.headroom())}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
