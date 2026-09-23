#!/usr/bin/env python3
"""Create integrity bundles for the three selected, local-only Phase 1 HPA runs."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))
from srecon26_poc.local_evidence import finalize_arm_bundle, write_pending_anchor_metadata


SELECTED = {
    "cpu": "phase1-live-202609d03531790135589z",
    "queue": "phase1-live-202609d04271790137679z",
    "kv": "phase1-live-202609d04031790136227z",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.repo_root.resolve()
    evidence_root = root / "local-evidence"
    finalized = [
        finalize_arm_bundle(evidence_root / run_id / arm, arm, run_id)
        for arm, run_id in SELECTED.items()
    ]
    write_pending_anchor_metadata(
        root / ".planning/phases/01-safety-foundation-and-local-evidence/01-LOCAL-EVIDENCE-ANCHOR.json",
        finalized,
        relative_to=root,
    )
    print("finalized local integrity bundles; external guard receipt remains PENDING")


if __name__ == "__main__":
    main()
