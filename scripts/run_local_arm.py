#!/usr/bin/env python3
"""Run one real arm only through the dedicated owned-Kind rehearsal harness."""
import argparse
import subprocess
from pathlib import Path


parser = argparse.ArgumentParser()
parser.add_argument("arm", choices=("cpu", "queue", "kv"))
parser.add_argument("--owner-label", required=True, help="explicit Phase 1 owner marker")
args = parser.parse_args()
if not args.owner_label.startswith("srecon26-phase1-"):
    raise SystemExit("refusing an unowned local rehearsal")
runner = Path(__file__).with_name("run_live_local_rehearsal.sh")
raise SystemExit(subprocess.run([str(runner), args.arm], check=False).returncode)
