from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts.build_evidence_pack import (
    validate_absence_artifact,
    validate_billing_artifact,
    validate_bound_guard_receipts,
    validate_guard_journal,
)


DEADLINE = "2026-09-24T12:00:00Z"


def manifest_fixture() -> dict[str, object]:
    manifest: dict[str, object] = {
        "guard_backend": "azure",
        "hard_deadline": DEADLINE,
        "guard_heartbeat_timeout_seconds": 120,
        "guards": {},
    }
    guards = manifest["guards"]
    assert isinstance(guards, dict)
    for role, host in (("server", "server-guard.example.test"), ("worker", "worker-guard.example.test")):
        nonce = f"{role}-nonce-12345678"
        label = f"srecon26-two-node-{role}--nonce-{nonce}"
        manifest[role] = {
            "nonce": nonce,
            "offer": {"label": label},
            "instance": {"instance_id": 41 if role == "server" else 42, "label": label},
        }
        guards[role] = {
            "backend": "azure",
            "role": role,
            "status": "ARMED",
            "root_hash": "a" * 64,
            "nonce": nonce,
            "label": label,
            "hard_deadline": DEADLINE,
            "last_heartbeat": "2026-09-24T11:00:00Z",
            "host_identity": host,
            "script_hash": "b" * 64,
            "azure_resource_id": f"/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/{role}-rg/providers/Microsoft.Compute/virtualMachines/{role}-guard",
            "azure_vm_id": "22222222-2222-2222-2222-222222222221" if role == "server" else "22222222-2222-2222-2222-222222222222",
            "host_key_fingerprint": "SHA256:" + ("A" if role == "server" else "B") * 43,
            "heartbeat_timeout_seconds": 120,
        }
    return manifest


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("backend", "github"),
        ("role", "worker"),
        ("nonce", "wrong-nonce"),
        ("label", "wrong-label"),
        ("hard_deadline", "2026-09-24T12:00:01Z"),
        ("root_hash", "not-a-hash"),
        ("host_identity", "localhost"),
        ("script_hash", "not-a-hash"),
    ],
)
def test_bound_guard_receipt_requires_every_exact_binding(field: str, value: str) -> None:
    manifest = manifest_fixture()
    manifest["guards"]["server"][field] = value  # type: ignore[index]

    valid, errors = validate_bound_guard_receipts(manifest)

    assert valid is False
    assert "server" in errors


def test_bound_guard_receipts_require_independent_host_identities() -> None:
    manifest = manifest_fixture()
    manifest["guards"]["worker"]["host_identity"] = "server-guard.example.test"  # type: ignore[index]

    valid, errors = validate_bound_guard_receipts(manifest)

    assert valid is False
    assert "independence_host_identity" in errors


def write_artifact(path: Path, payload: dict[str, object]) -> dict[str, object]:
    raw = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    path.write_bytes(raw)
    return {"artifact": path.name, "sha256": hashlib.sha256(raw).hexdigest()}


def test_absence_and_billing_require_hash_bound_exact_artifacts(tmp_path: Path) -> None:
    manifest = manifest_fixture()
    target = manifest["server"]
    assert isinstance(target, dict)
    instance = target["instance"]
    assert isinstance(instance, dict)
    nonce, instance_id, label = target["nonce"], instance["instance_id"], instance["label"]
    absence = write_artifact(
        tmp_path / "server-absence.json",
        {
            "schema": "srecon26-vast-absence-evidence/v1",
            "source": "VastCliProvider.capture_absence_evidence/v1",
            "run_id": f"two-node-server-{nonce}",
            "instance_id": instance_id,
            "label": label,
            "reads": [
                {"observed_at": f"2026-09-24T11:00:0{index}Z", "matching_instances": 0}
                for index in range(3)
            ],
        },
    )
    absence["status"] = "THREE_READS_CONFIRMED"
    billing = write_artifact(
        tmp_path / "server-invoice.json",
        {
            "schema": "srecon26-vast-invoice-evidence/v1",
            "source": "VastCliProvider.capture_invoice_charge/v1",
            "observed_at": "2026-09-24T11:30:00Z",
            "run_id": f"two-node-server-{nonce}",
            "instance_id": instance_id,
            "label": label,
            "amount_usd": "0.42",
            "provider_charge": {
                "type": "instance", "source": f"instance-{instance_id}", "amount": "0.42",
                "metadata": {"label": label},
            },
        },
    )
    billing.update({"status": "AUTHORITATIVE_INVOICE_CAPTURED", "amount_usd": "0.42"})

    assert validate_absence_artifact(tmp_path, "server", target, absence)
    assert validate_billing_artifact(tmp_path, "server", target, billing)

    (tmp_path / "server-absence.json").write_text("{}\n", encoding="utf-8")
    assert validate_absence_artifact(tmp_path, "server", target, absence) is False


def guard_event(sequence: int, event: str, nonce: str, previous: str, payload: dict[str, object]) -> dict[str, object]:
    record: dict[str, object] = {
        "sequence": sequence,
        "event": event,
        "nonce": nonce,
        "wall_time": f"2026-09-24T11:00:0{sequence}Z",
        "previous_hash": previous,
        "payload": payload,
    }
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    record["event_hash"] = hashlib.sha256(encoded).hexdigest()
    return record


def test_guard_journal_verifies_file_hash_chain_and_arm_root(tmp_path: Path) -> None:
    manifest = manifest_fixture()
    receipt = manifest["guards"]["server"]  # type: ignore[index]
    assert isinstance(receipt, dict)
    first = guard_event(1, "armed", str(receipt["nonce"]), "0" * 64, {"instance_id": None, "label": receipt["label"]})
    receipt["root_hash"] = first["event_hash"]
    second = guard_event(2, "heartbeat", str(receipt["nonce"]), str(first["event_hash"]), {})
    raw = "".join(json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n" for item in (first, second)).encode()
    path = tmp_path / "server-azure-guard-journal.ndjson"
    path.write_bytes(raw)
    finalization = {
        "root_hash": second["event_hash"],
        "journal_artifact": path.name,
        "journal_sha256": hashlib.sha256(raw).hexdigest(),
    }

    assert validate_guard_journal(tmp_path, role="server", receipt=receipt, finalization=finalization)

    tampered = copy.deepcopy(finalization)
    tampered["root_hash"] = "f" * 64
    assert validate_guard_journal(tmp_path, role="server", receipt=receipt, finalization=tampered) is False


def test_mere_finalization_statuses_are_not_artifact_proof(tmp_path: Path) -> None:
    target = manifest_fixture()["server"]
    assert isinstance(target, dict)

    assert validate_absence_artifact(tmp_path, "server", target, {"status": "THREE_READS_CONFIRMED"}) is False
    assert validate_billing_artifact(tmp_path, "server", target, {"status": "AUTHORITATIVE_INVOICE_CAPTURED"}) is False
