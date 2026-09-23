#!/usr/bin/env python3
"""Produce the claim-gating verdict consumed by the presentation build."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from srecon26_poc.evidence_verdict import analyze_selected_evidence, write_verdict


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--local-anchor", type=Path)
    parser.add_argument("--live-manifest", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.repo_root.resolve()
    output = args.output or root / "artifacts/presentation/verdict.json"
    verdict = analyze_selected_evidence(
        root,
        local_anchor=args.local_anchor,
        live_manifest=args.live_manifest,
    )
    write_verdict(output, verdict)
    print(output)
    return 0 if verdict["verdict"] == "VALID" else 1


if __name__ == "__main__":
    raise SystemExit(main())
