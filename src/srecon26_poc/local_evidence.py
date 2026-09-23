"""Finalize selected local HPA evidence without inventing a guard acknowledgement."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .integrity import IntegrityError, build_integrity_bundle


CAPTURE_STEPS = (
    "negative-0", "negative-1", "negative-2", "negative-3", "negative-4", "negative-5", "negative-6",
    "trigger-before", "scaled",
)
CAPTURE_SUFFIXES = (
    "timestamp", "source.prom", "cpu.json", "queue.custom.json", "kv.custom.json", "hpa.json", "deployment.json", "events.json",
)
SELECTED_ARMS = frozenset({"cpu", "queue", "kv"})
GENERATED_ARTIFACTS = frozenset({"EVIDENCE-MANIFEST.json", "SHA256SUMS", "ROOT-HASH.txt"})


@dataclass(frozen=True)
class FinalizedArm:
    arm: str
    bundle: Path
    run_id: str
    root_hash: str
    manifest_sha256: str


def arm_allowlist(arm: str) -> tuple[str, ...]:
    """Return the full, fixed set of raw files accepted for a selected arm."""
    if arm not in SELECTED_ARMS:
        raise IntegrityError(f"unsupported selected evidence arm: {arm}")
    return tuple(f"{step}.{suffix}" for step in CAPTURE_STEPS for suffix in CAPTURE_SUFFIXES) + ("summary.json",)


def _verify_exact_allowlist(bundle: Path, declared: tuple[str, ...]) -> None:
    if not bundle.is_dir():
        raise IntegrityError("selected evidence bundle is missing")
    actual = {path.name for path in bundle.iterdir() if path.is_file()} - GENERATED_ARTIFACTS
    expected = set(declared)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise IntegrityError(f"selected evidence does not match explicit allowlist; missing={missing}; unexpected={unexpected}")


def finalize_arm_bundle(bundle: Path, arm: str, run_id: str) -> FinalizedArm:
    """Write a manifest, checksums, and a root hash for one immutable selected arm."""
    declared = arm_allowlist(arm)
    _verify_exact_allowlist(bundle, declared)
    manifest = {
        "schema_version": 1,
        "arm": arm,
        "provenance": "local-synthetic",
        "run_id": run_id,
        "artifacts": list(declared),
    }
    manifest_path = bundle / "EVIDENCE-MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    root_hash = build_integrity_bundle(bundle, declared)
    return FinalizedArm(
        arm=arm,
        bundle=bundle,
        run_id=run_id,
        root_hash=root_hash,
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )


def write_pending_anchor_metadata(path: Path, arms: Iterable[FinalizedArm], *, relative_to: Path) -> None:
    """Track local roots while making the missing independent-guard receipt unmistakable."""
    finalized = sorted(arms, key=lambda item: item.arm)
    root = relative_to.resolve()
    try:
        bundle_paths = {item.arm: item.bundle.resolve().relative_to(root).as_posix() for item in finalized}
    except ValueError as exc:
        raise IntegrityError("selected bundle is outside the declared repository root") from exc
    payload = {
        "schema_version": 1,
        "provenance": "local-synthetic",
        "selected_arms": [
            {
                "arm": item.arm,
                "bundle": bundle_paths[item.arm],
                "run_id": item.run_id,
                "allowlist": "EVIDENCE-MANIFEST.json",
                "root_hash": item.root_hash,
                "manifest_sha256": item.manifest_sha256,
            }
            for item in finalized
        ],
        "external_guard_receipt": {
            "status": "PENDING",
            "receipt_path": None,
            "required_fields": ["guard_id", "nonce", "acknowledged_at", "root_hash"],
            "required_root_hashes": {item.arm: item.root_hash for item in finalized},
            "note": "No independent guard acknowledgement exists yet; this metadata is not a receipt.",
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
