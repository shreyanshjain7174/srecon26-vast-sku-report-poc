#!/usr/bin/env python3
"""Reconcile one closed Vast reservation from exact invoice and journal evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from srecon26_poc.budget import ExposureLedger
from srecon26_poc.vast_provider import VastCliProvider


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

    receipt = json.loads(args.reconciliation.read_text())
    run_id = str(receipt["run_id"])
    instance_id = int(receipt["instance_id"])
    label = str(receipt["label"])
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
    ledger = ExposureLedger(args.ledger)
    ledger.commit_actual(run_id, evidence.amount, proof)
    print(json.dumps({"run_id": run_id, "amount_usd": str(evidence.amount), "invoice_sha256": evidence.sha256, "headroom": str(ledger.headroom())}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
