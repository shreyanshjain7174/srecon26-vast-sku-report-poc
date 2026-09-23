#!/usr/bin/env python3
"""Verify a guard root-hash acknowledgement before exposing a signed commit command."""
import argparse
from pathlib import Path

from srecon26_poc.integrity import validate_integrity_bundle


parser = argparse.ArgumentParser()
parser.add_argument("bundle", type=Path)
parser.add_argument("--artifact", action="append", required=True)
parser.add_argument("--guard-receipt", required=True)
args = parser.parse_args()
validate_integrity_bundle(args.bundle, args.artifact, args.guard_receipt)
print("integrity anchor acknowledged; prepare only bundle paths with: git commit -s")
