from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.finalize_deferred_azure_guards import DeferredFinalizerError, finalize_deferred


DEADLINE = "2026-09-24T12:00:00+00:00"
NONCE = "server-nonce-12345678"
LABEL = f"srecon26-two-node-server--nonce-{NONCE}"


def write_fixture(tmp_path: Path) -> Path:
    descriptor = {
        "schema": "srecon26.azure-guard-deferred-finalizer.v1",
        "status": "PENDING_POST_DEADLINE_ABSENCE",
        "manifest_artifact": "run-manifest.json",
        "roles": {
            "server": {
                "nonce": NONCE,
                "label": LABEL,
                "hard_deadline": DEADLINE,
                "finalize_after": DEADLINE,
                "expected_heartbeat_timeout_seconds": 120,
                "endpoint": {
                    "host": "guard.example.test",
                    "user": "guardrpc",
                    "port": 22,
                    "timeout_seconds": 20,
                    "identity_file": str(tmp_path / "guard-key"),
                    "known_hosts_file": str(tmp_path / "known-hosts"),
                },
                "arm_receipt": {
                    "backend": "azure",
                    "status": "ARMED",
                    "run_id": f"two-node-server-{NONCE}",
                    "root_hash": "a" * 64,
                    "nonce": NONCE,
                    "label": LABEL,
                    "hard_deadline": DEADLINE,
                    "heartbeat_timeout_seconds": 120,
                    "host_key_fingerprint": "SHA256:" + "A" * 43,
                },
            }
        },
    }
    manifest = {
        "schema": "srecon26.two-node-run.v1",
        "hard_deadline": DEADLINE,
        "status": "failed",
        "failure": {"error_type": "AmbiguousCreate"},
        "evidence_status": "incomplete",
        "finalization_errors": [
            "server Azure guard requires deferred post-deadline ABSENCE_CONFIRMED finalization"
        ],
        "azure_guard_finalization": {"server": {"status": "AWAITING_INSTANCE"}},
        "server": {"nonce": NONCE, "offer": {"label": LABEL}},
    }
    (tmp_path / "run-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    path = tmp_path / "deferred-azure-guard-finalizer.json"
    path.write_text(json.dumps(descriptor), encoding="utf-8")
    return path


class FakeTransport:
    def __init__(self, _config) -> None:
        self.resumed: dict[str, object] | None = None

    def resume_arm_binding(self, receipt) -> None:
        self.resumed = dict(receipt)

    def call(self, command: str, payload: dict[str, object]) -> dict[str, object]:
        assert command == "status"
        assert self.resumed is not None and payload["nonce"] == self.resumed["nonce"]
        return {
            "status": "ABSENCE_CONFIRMED",
            "root_hash": "b" * 64,
            "nonce": NONCE,
            "label": LABEL,
            "hard_deadline": DEADLINE,
            "teardown_authority_at": DEADLINE,
            "absence_observations": [
                "2026-09-24T12:01:00Z",
                "2026-09-24T12:02:00Z",
                "2026-09-24T12:03:00Z",
            ],
        }

    def export_evidence(self, *, nonce: str, root_hash: str) -> SimpleNamespace:
        assert nonce == NONCE and root_hash == "b" * 64
        journal = '{"event":"absence_confirmed"}\n'
        return SimpleNamespace(journal=journal, journal_sha256=hashlib.sha256(journal.encode()).hexdigest())


def test_deferred_finalizer_resumes_exports_and_updates_bound_manifest(tmp_path: Path) -> None:
    descriptor = write_fixture(tmp_path)
    rebuilds: list[bool] = []

    evidence = finalize_deferred(
        descriptor,
        transport_factory=FakeTransport,
        now=lambda: datetime(2026, 9, 24, 12, 4, tzinfo=UTC),
        sleep=lambda _seconds: None,
        rebuild=lambda: rebuilds.append(True),
    )

    assert evidence["server"]["absence_proof_valid"] is True
    assert (tmp_path / "server-azure-guard-journal.ndjson").is_file()
    deferred = json.loads(descriptor.read_text())
    manifest = json.loads((tmp_path / "run-manifest.json").read_text())
    assert deferred["status"] == "COMPLETED"
    assert manifest["azure_guard_evidence_status"] == "complete"
    assert manifest["azure_guard_finalization"]["server"]["status"] == "ABSENCE_CONFIRMED"
    assert manifest["evidence_status"] == "incomplete"
    assert "finalization_errors" not in manifest
    assert manifest["evidence_pack"]["status"] == "REBUILT"
    assert rebuilds == [True]


def test_deferred_finalizer_refuses_resume_before_immutable_deadline(tmp_path: Path) -> None:
    descriptor = write_fixture(tmp_path)

    with pytest.raises(DeferredFinalizerError, match="deadline has not arrived"):
        finalize_deferred(
            descriptor,
            transport_factory=FakeTransport,
            now=lambda: datetime(2026, 9, 24, 11, 59, tzinfo=UTC),
            sleep=lambda _seconds: None,
            rebuild=lambda: None,
        )


def test_deferred_finalizer_rejects_a_descriptor_not_bound_to_the_manifest(tmp_path: Path) -> None:
    descriptor = write_fixture(tmp_path)
    payload = json.loads(descriptor.read_text())
    payload["roles"]["server"]["arm_receipt"]["label"] = (
        "srecon26-two-node-server--nonce-other-nonce-12345678"
    )
    descriptor.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(DeferredFinalizerError, match="binding is inconsistent"):
        finalize_deferred(
            descriptor,
            transport_factory=FakeTransport,
            now=lambda: datetime(2026, 9, 24, 12, 4, tzinfo=UTC),
            sleep=lambda _seconds: None,
            rebuild=lambda: None,
        )
