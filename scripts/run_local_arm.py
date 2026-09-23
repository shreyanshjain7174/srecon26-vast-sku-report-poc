#!/usr/bin/env python3
"""Refuse unowned cluster mutation; deterministic fixtures are evaluated offline only."""
import argparse
import os
import subprocess


parser = argparse.ArgumentParser()
parser.add_argument("arm", choices=("cpu", "queue", "kv"))
parser.add_argument("--owner-label", required=True)
args = parser.parse_args()
context = subprocess.check_output(["kubectl", "config", "current-context"], text=True).strip()
if not context.startswith("srecon26-owned-") or not args.owner_label.startswith("srecon26-run-"):
    raise SystemExit("refusing to mutate an unowned Kubernetes context or resource")
print(f"owned local rehearsal required for {args.arm}; no synthetic success is emitted")
