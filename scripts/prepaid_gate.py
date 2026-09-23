#!/usr/bin/env python3
"""Read-only evidence checker. It cannot create, destroy, report, or read credentials."""
from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping


def evaluate(evidence: Mapping[str, object]) -> dict[str, object]:
    missing = [key for key in ("tests", "semgrep", "secret_checks", "guard_attestation", "report_fixture") if not evidence.get(key)]
    if evidence.get("inventory") not in {"empty", "same-run-owned"}:
        missing.append("inventory")
    try:
        if Decimal(str(evidence.get("budget_headroom", "0"))) <= Decimal("0"):
            missing.append("budget_headroom")
    except (InvalidOperation, ValueError):
        missing.append("budget_headroom")
    if not missing:
        missing.append("authorization_not_granted")
    return {"eligible": False, "missing": missing}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", dest="path", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(evaluate(json.loads(args.path.read_text(encoding="utf-8"))), sort_keys=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
