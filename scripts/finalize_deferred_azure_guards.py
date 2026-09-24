#!/usr/bin/env python3
"""Resume deferred Azure guard proof after an ambiguous provider create."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from srecon26_poc.azure_guard_transport import AzureGuardSshConfig, AzureSshGuardTransport


class DeferredFinalizerError(RuntimeError):
    """Deferred evidence cannot yet be finalized safely."""


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


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _post_deadline_absence(status: Mapping[str, object], deadline: datetime, current: datetime) -> bool:
    if status.get("status") != "ABSENCE_CONFIRMED" or current.astimezone(UTC) < deadline:
        return False
    authority = _time(status.get("teardown_authority_at"))
    observations = status.get("absence_observations")
    if authority < deadline or not isinstance(observations, list) or len(observations) != 3:
        return False
    parsed = [_time(value) for value in observations]
    return parsed == sorted(set(parsed)) and all(value > deadline for value in parsed)


def _default_rebuild() -> None:
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/build_evidence_pack.py")],
        cwd=ROOT,
        check=True,
        timeout=90,
    )


def finalize_deferred(
    descriptor_path: Path,
    *,
    timeout_seconds: int = 600,
    poll_seconds: float = 5,
    transport_factory: Callable[[AzureGuardSshConfig], AzureSshGuardTransport] = AzureSshGuardTransport,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    rebuild: Callable[[], None] = _default_rebuild,
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
    if descriptor.get("status") != "PENDING_POST_DEADLINE_ABSENCE":
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

    completed: dict[str, object] = {}
    for role in sorted(roles):
        record = _object(roles[role], f"{role} deferred record")
        deadline = _time(record.get("hard_deadline"))
        if now().astimezone(UTC) < deadline:
            raise DeferredFinalizerError(f"{role} immutable deadline has not arrived")
        endpoint = _object(record.get("endpoint"), f"{role} endpoint")
        arm_receipt = _object(record.get("arm_receipt"), f"{role} arm receipt")
        _validate_record_binding(role, record, arm_receipt, manifest)
        config = AzureGuardSshConfig(
            host=str(endpoint.get("host", "")),
            user=str(endpoint.get("user", "")),
            identity_file=Path(str(endpoint.get("identity_file", ""))),
            known_hosts_file=Path(str(endpoint.get("known_hosts_file", ""))),
            port=_integer(endpoint.get("port"), f"{role} SSH port", minimum=1, maximum=65535),
            timeout_seconds=_integer(endpoint.get("timeout_seconds"), f"{role} SSH timeout", minimum=1, maximum=60),
            heartbeat_timeout_seconds=_integer(
                record.get("expected_heartbeat_timeout_seconds"),
                f"{role} expected heartbeat timeout",
                minimum=1,
                maximum=600,
            ),
        )
        transport = transport_factory(config)
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
    manifest["azure_guard_evidence_status"] = "complete"
    manifest["deferred_finalizer"] = {
        "status": "COMPLETED",
        "artifact": resolved.name,
        "completed_at": now().isoformat(),
    }
    manifest["evidence_status"] = (
        "complete"
        if not remaining and manifest.get("status") == "completed" and "failure" not in manifest
        else "incomplete"
    )
    _atomic_json(manifest_path, manifest)

    descriptor.update({"status": "COMPLETED", "completed_at": now().isoformat(), "evidence": completed})
    _atomic_json(resolved, descriptor)
    try:
        rebuild()
        manifest["evidence_pack"] = {"status": "REBUILT", "summary": "artifacts/evidence-pack/evidence-summary.json"}
    except (OSError, subprocess.SubprocessError) as error:
        manifest["evidence_pack"] = {"status": "FAILED", "error_type": type(error).__name__}
        manifest["evidence_status"] = "incomplete"
        _atomic_json(manifest_path, manifest)
        raise DeferredFinalizerError("guard finalized but evidence-pack rebuild failed") from error
    _atomic_json(manifest_path, manifest)
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
