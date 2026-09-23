from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "browser/report-fixture/fixture_runner.mjs"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the non-submitting desktop report fixture")
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    if not args.fixture:
        parser.error("only --fixture is supported; live provider reporting is disabled")
    output_dir = args.json.parent / "report-fixture-evidence"
    completed = subprocess.run(
        [
            "node",
            str(FIXTURE),
            "--output-dir",
            str(output_dir),
            "--instance-id",
            "417",
            "--label",
            "srecon26-run-report",
            "--nonce",
            "nonce-0123456789abcdef",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=70,
    )
    if completed.returncode:
        raise RuntimeError(f"fixture runner failed: {completed.stderr.strip()}")
    receipt = json.loads((output_dir / "receipt.json").read_text(encoding="utf-8"))
    if receipt.get("provider_request_count") != 0 or receipt.get("status") != "SUBMITTED":
        raise RuntimeError("fixture did not produce a non-submitting receipt")
    attestation = {"mode": "fixture", "provider_requests": 0, "receipt": receipt, "evidence_dir": str(output_dir)}
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(attestation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(attestation, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(f"report fixture preflight refused: {exc}", file=sys.stderr)
        raise SystemExit(2)
