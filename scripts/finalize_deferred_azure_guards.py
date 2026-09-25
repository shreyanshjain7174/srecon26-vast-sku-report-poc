#!/usr/bin/env python3
"""Resume deferred Azure guard proof after an ambiguous provider create."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from scripts.build_evidence_pack import (
    post_deadline_absence,
    validate_bound_guard_receipts,
    validate_deferred_guard_evidence,
)
from srecon26_poc.azure_guard_transport import AzureGuardSshConfig, AzureSshGuardTransport
from srecon26_poc.azure_run_command_transport import AzureRunCommandGuardConfig, AzureRunCommandGuardTransport


class DeferredFinalizerError(RuntimeError):
    """Deferred evidence cannot yet be finalized safely."""


def _transport_for_endpoint(
    role: str,
    endpoint: Mapping[str, object],
    record: Mapping[str, object],
    transport_factory: Callable[[object], object] | None,
) -> object:
    """Rebuild exactly the transport serialized with a deferred arm receipt."""

    heartbeat_timeout_seconds = _integer(
        record.get("expected_heartbeat_timeout_seconds"),
        f"{role} expected heartbeat timeout",
        minimum=1,
        maximum=600,
    )
    transport_kind = endpoint.get("transport", "ssh")
    if transport_kind == "ssh":
        config: object = AzureGuardSshConfig(
            host=str(endpoint.get("host", "")),
            user=str(endpoint.get("user", "")),
            identity_file=Path(str(endpoint.get("identity_file", ""))),
            known_hosts_file=Path(str(endpoint.get("known_hosts_file", ""))),
            port=_integer(endpoint.get("port"), f"{role} SSH port", minimum=1, maximum=65535),
            timeout_seconds=_integer(endpoint.get("timeout_seconds"), f"{role} SSH timeout", minimum=1, maximum=60),
            heartbeat_timeout_seconds=heartbeat_timeout_seconds,
        )
        return transport_factory(config) if transport_factory else AzureSshGuardTransport(config)
    if transport_kind == "azure-run-command":
        config = AzureRunCommandGuardConfig(
            subscription_id=str(endpoint.get("subscription_id", "")),
            resource_group=str(endpoint.get("resource_group", "")),
            vm_name=str(endpoint.get("vm_name", "")),
            az_path=Path(str(endpoint.get("az_path", ""))),
            timeout_seconds=_integer(
                endpoint.get("timeout_seconds"), f"{role} Azure Run Command timeout", minimum=1, maximum=300,
            ),
            heartbeat_timeout_seconds=heartbeat_timeout_seconds,
        ).validated()
        return transport_factory(config) if transport_factory else AzureRunCommandGuardTransport(config)
    raise DeferredFinalizerError(f"{role} deferred guard transport is invalid")


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise DeferredFinalizerError("deferred finalizer timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise DeferredFinalizerError("deferred finalizer timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise DeferredFinalizerError("deferred finalizer timestamp lacks a timezone")
    return parsed.astimezone(UTC)


def _object(value: object, description: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise DeferredFinalizerError(f"{description} is invalid")
    return value


def _integer(value: object, description: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise DeferredFinalizerError(f"{description} is invalid")
    return value


def _validate_record_binding(
    role: str,
    record: Mapping[str, object],
    receipt: Mapping[str, object],
    manifest: Mapping[str, object],
) -> None:
    nonce, label = record.get("nonce"), record.get("label")
    deadline = _time(record.get("hard_deadline"))
    expected_timeout = _integer(
        record.get("expected_heartbeat_timeout_seconds"),
        f"{role} expected heartbeat timeout",
        minimum=1,
        maximum=600,
    )
    expected_run_id = f"two-node-{role}-{nonce}"
    expected_label = f"srecon26-two-node-{role}--nonce-{nonce}"
    if (
        receipt.get("backend") != "azure"
        or receipt.get("status") != "ARMED"
        or receipt.get("run_id") != expected_run_id
        or not isinstance(nonce, str)
        or receipt.get("nonce") != nonce
        or label != expected_label
        or receipt.get("label") != label
        or _time(receipt.get("hard_deadline")) != deadline
        or receipt.get("heartbeat_timeout_seconds") != expected_timeout
        or record.get("finalize_after") != record.get("hard_deadline")
    ):
        raise DeferredFinalizerError(f"{role} deferred arm binding is inconsistent")
    target = _object(manifest.get(role), f"bound manifest {role} target")
    offer = _object(target.get("offer"), f"bound manifest {role} offer")
    if (
        _time(manifest.get("hard_deadline")) != deadline
        or target.get("nonce") != nonce
        or offer.get("label") != label
    ):
        raise DeferredFinalizerError(f"{role} deferred arm is not bound to the run manifest")
    guards = _object(manifest.get("guards"), "bound manifest guard attestations")
    original = _object(guards.get(role), f"bound manifest {role} guard attestation")
    if {field: value for field, value in receipt.items() if field != "run_id"} != original:
        raise DeferredFinalizerError(f"{role} deferred arm differs from the original manifest attestation")


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _post_deadline_absence(status: Mapping[str, object], deadline: datetime, current: datetime) -> bool:
    return post_deadline_absence(status, deadline, current.astimezone(UTC))


def _default_rebuild() -> Mapping[str, object]:
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/build_evidence_pack.py")],
        cwd=ROOT,
        check=True,
        timeout=90,
    )
    return _object(
        json.loads((ROOT / "artifacts/evidence-pack/evidence-summary.json").read_text(encoding="utf-8")),
        "rebuilt evidence summary",
    )


def _validate_rebuild(
    summary: Mapping[str, object], manifest_path: Path, roles: Mapping[str, object], *, require_complete_run: bool,
) -> None:
    canary = _object(summary.get("two_node_canary"), "rebuilt two-node evidence")
    attempts = canary.get("attempts")
    matches = [
        item for item in attempts if isinstance(item, Mapping) and item.get("run") == manifest_path.parent.name
    ] if isinstance(attempts, list) else []
    if len(matches) != 1:
        raise DeferredFinalizerError("rebuilt evidence lacks the exact deferred run")
    attempt = matches[0]
    checks = _object(attempt.get("deferred_guard_evidence_validated"), "rebuilt deferred guard validation")
    if (
        attempt.get("manifest_sha256") != hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        or attempt.get("both_bound_arm_receipts") is not True
        or attempt.get("both_guard_journals_hash_chain_verified") is not True
        or any(checks.get(role) is not True for role in roles)
        or (require_complete_run and any(attempt.get(field) is not True for field in (
            "kubernetes_completed", "three_read_absence_proved_for_both", "authoritative_billing_captured_for_both",
        )))
    ):
        raise DeferredFinalizerError("rebuilt evidence semantic validation failed")


def finalize_deferred(
    descriptor_path: Path,
    *,
    timeout_seconds: int = 600,
    poll_seconds: float = 5,
    transport_factory: Callable[[object], object] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    rebuild: Callable[[], Mapping[str, object]] = _default_rebuild,
) -> dict[str, object]:
    """Complete every deferred channel and update its original run evidence."""

    if not 1 <= timeout_seconds <= 1800 or not 0 <= poll_seconds <= 30:
        raise DeferredFinalizerError("deferred finalizer polling bounds are invalid")
    if descriptor_path.is_symlink():
        raise DeferredFinalizerError("deferred finalizer descriptor must not be a symlink")
    resolved = descriptor_path.resolve(strict=True)
    if not resolved.is_file():
        raise DeferredFinalizerError("deferred finalizer descriptor must be a regular file")
    descriptor = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(descriptor, dict) or descriptor.get("schema") != "srecon26.azure-guard-deferred-finalizer.v1":
        raise DeferredFinalizerError("deferred finalizer descriptor schema is invalid")
    if descriptor.get("status") not in {"PENDING_POST_DEADLINE_ABSENCE", "PENDING_EVIDENCE_REBUILD"}:
        raise DeferredFinalizerError("deferred finalizer is not pending")
    roles = _object(descriptor.get("roles"), "deferred roles")
    if not roles or not set(roles).issubset({"server", "worker"}):
        raise DeferredFinalizerError("deferred finalizer roles are invalid")
    run_dir = resolved.parent
    manifest_name = descriptor.get("manifest_artifact")
    if manifest_name != "run-manifest.json":
        raise DeferredFinalizerError("deferred finalizer manifest binding is invalid")
    manifest_path = run_dir / manifest_name
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise DeferredFinalizerError("bound run manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise DeferredFinalizerError("bound run manifest is invalid")
    guards_valid, _errors = validate_bound_guard_receipts(manifest)
    if not guards_valid:
        raise DeferredFinalizerError("bound run manifest guard attestations are invalid")
    for role, value in roles.items():
        record = _object(value, f"{role} deferred record")
        _validate_record_binding(role, record, _object(record.get("arm_receipt"), f"{role} arm receipt"), manifest)

    completed: dict[str, object] = {}
    saved = _object(descriptor.get("evidence", {}), "saved deferred evidence")
    for role in sorted(roles):
        record = _object(roles[role], f"{role} deferred record")
        deadline = _time(record.get("hard_deadline"))
        if now().astimezone(UTC) < deadline:
            raise DeferredFinalizerError(f"{role} immutable deadline has not arrived")
        endpoint = _object(record.get("endpoint"), f"{role} endpoint")
        arm_receipt = _object(record.get("arm_receipt"), f"{role} arm receipt")
        if role in saved:
            proof = _object(saved[role], f"{role} saved guard evidence")
            if not validate_deferred_guard_evidence(run_dir, role, arm_receipt, proof, current=now()):
                raise DeferredFinalizerError(f"{role} saved guard evidence is invalid")
            completed[role] = dict(proof)
            continue
        transport = _transport_for_endpoint(role, endpoint, record, transport_factory)
        transport.resume_arm_binding(arm_receipt)
        stop_at = monotonic() + timeout_seconds
        status: dict[str, object] | None = None
        while monotonic() <= stop_at:
            status = transport.call("status", {"nonce": arm_receipt["nonce"]})
            if status.get("status") == "ABSENCE_CONFIRMED":
                break
            if status.get("status") in {"OWNERSHIP_MISMATCH", "TEARDOWN_RETRIES_EXHAUSTED", "DISARMED"}:
                raise DeferredFinalizerError(f"{role} guard reached unsafe terminal status")
            sleep(poll_seconds)
        if status is None or not _post_deadline_absence(status, deadline, now()):
            raise DeferredFinalizerError(f"{role} guard lacks post-deadline ABSENCE_CONFIRMED proof")
        root_hash = status.get("root_hash")
        if not isinstance(root_hash, str):
            raise DeferredFinalizerError(f"{role} guard final root is missing")
        exported = transport.export_evidence(nonce=str(arm_receipt["nonce"]), root_hash=root_hash)
        journal = run_dir / f"{role}-azure-guard-journal.ndjson"
        temporary = journal.with_suffix(journal.suffix + ".tmp")
        temporary.write_text(exported.journal, encoding="utf-8")
        temporary.replace(journal)
        completed[role] = {
            **status,
            "instance_observed_locally": False,
            "absence_required": True,
            "absence_proof_valid": True,
            "exported_root_hash": root_hash,
            "journal_artifact": journal.name,
            "journal_sha256": exported.journal_sha256,
        }
        if not validate_deferred_guard_evidence(run_dir, role, arm_receipt, completed[role], current=now()):
            raise DeferredFinalizerError(f"{role} exported guard evidence semantic validation failed")
        descriptor["evidence"] = {**saved, **completed}
        _atomic_json(resolved, descriptor)

    finalization = manifest.get("azure_guard_finalization")
    if not isinstance(finalization, dict):
        finalization = {}
    finalization.update(completed)
    manifest["azure_guard_finalization"] = finalization
    prior_errors = manifest.get("finalization_errors")
    remaining = [
        error for error in prior_errors if isinstance(error, str) and "requires deferred post-deadline" not in error
    ] if isinstance(prior_errors, list) else []
    if remaining:
        manifest["finalization_errors"] = remaining
    else:
        manifest.pop("finalization_errors", None)
    manifest["azure_guard_evidence_status"] = "pending-evidence-rebuild"
    manifest["deferred_finalizer"] = {
        "status": "PENDING_EVIDENCE_REBUILD",
        "artifact": resolved.name,
    }
    manifest["evidence_status"] = "incomplete"
    manifest["evidence_pack"] = {"status": "PENDING_REBUILD"}
    _atomic_json(manifest_path, manifest)

    descriptor.update({"status": "PENDING_EVIDENCE_REBUILD", "evidence": completed})
    _atomic_json(resolved, descriptor)
    complete_run = not remaining and manifest.get("status") == "completed" and "failure" not in manifest
    try:
        summary = _object(rebuild(), "rebuilt evidence summary")
        _validate_rebuild(summary, manifest_path, roles, require_complete_run=complete_run)
    except Exception as error:
        manifest["evidence_pack"] = {"status": "FAILED", "error_type": type(error).__name__}
        _atomic_json(manifest_path, manifest)
        raise DeferredFinalizerError("guard finalized but evidence-pack rebuild failed") from error
    completed_at = now().isoformat()
    manifest["azure_guard_evidence_status"] = "complete"
    manifest["deferred_finalizer"].update({"status": "COMPLETED", "completed_at": completed_at})
    manifest["evidence_pack"] = {"status": "REBUILT", "summary": "artifacts/evidence-pack/evidence-summary.json"}
    manifest["evidence_status"] = "complete" if complete_run else "incomplete"
    _atomic_json(manifest_path, manifest)
    descriptor.update({"status": "COMPLETED", "completed_at": completed_at})
    _atomic_json(resolved, descriptor)
    return completed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deferred", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--poll-seconds", type=float, default=5)
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    finalize_deferred(args.deferred, timeout_seconds=args.timeout_seconds, poll_seconds=args.poll_seconds)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DeferredFinalizerError as error:
        print(f"Deferred Azure guard finalization refused: {error}", file=sys.stderr)
        raise SystemExit(2)
