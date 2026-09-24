from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import build_evidence_pack
from scripts.finalize_deferred_azure_guards import (
    DeferredFinalizerError,
    _post_deadline_absence,
    finalize_deferred,
)
from srecon26_poc.azure_run_command_transport import AzureRunCommandGuardConfig


DEADLINE = "2026-09-24T12:00:00+00:00"
NOW = datetime(2026, 9, 24, 12, 4, tzinfo=UTC)
NONCE = "server-nonce-12345678"
LABEL = f"srecon26-two-node-server--nonce-{NONCE}"
READS = ["2026-09-24T12:01:00Z", "2026-09-24T12:02:00Z", "2026-09-24T12:03:00Z"]


def journal_fixture(
    role: str, authority: str = DEADLINE, *, with_authority: bool = True,
) -> tuple[str, list[dict[str, object]]]:
    nonce = f"{role}-nonce-12345678"
    label = f"srecon26-two-node-{role}--nonce-{nonce}"
    events = [
        ("armed", {"label": label}),
        ("teardown_authority_activated", {"authority_at": authority, "reason": "heartbeat_missed"}),
        *[("absence_observation", {"label": label, "observed_at": value, "observation_number": i}) for i, value in enumerate(READS, 1)],
        ("absence_confirmed", {"observations": READS, "quorum": 3}),
    ]
    if not with_authority:
        events.pop(1)
    records = []
    root = "0" * 64
    for sequence, (event, payload) in enumerate(events, 1):
        record = {"sequence": sequence, "event": event, "payload": payload, "nonce": nonce, "previous_hash": root}
        root = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
        record["event_hash"] = root
        records.append(record)
    return "".join(json.dumps(record) + "\n" for record in records), records


def write_fixture(tmp_path: Path) -> Path:
    manifest = {
        "schema": "srecon26.two-node-run.v1",
        "hard_deadline": DEADLINE,
        "guard_backend": "azure",
        "guard_heartbeat_timeout_seconds": 120,
        "guards": {},
        "status": "failed",
        "failure": {"error_type": "AmbiguousCreate"},
        "evidence_status": "incomplete",
        "finalization_errors": ["server Azure guard requires deferred post-deadline ABSENCE_CONFIRMED finalization"],
        "azure_guard_finalization": {"server": {"status": "AWAITING_INSTANCE"}},
    }
    for role in ("server", "worker"):
        nonce = f"{role}-nonce-12345678"
        label = f"srecon26-two-node-{role}--nonce-{nonce}"
        journal, records = journal_fixture(role)
        manifest[role] = {"nonce": nonce, "offer": {"label": label}}
        manifest["guards"][role] = {
            "backend": "azure", "role": role, "status": "ARMED", "nonce": nonce, "label": label,
            "hard_deadline": DEADLINE, "heartbeat_timeout_seconds": 120,
            "root_hash": records[0]["event_hash"], "last_heartbeat": "2026-09-24T11:00:00Z",
            "host_identity": f"{role}-guard.example.test", "script_hash": "c" * 64,
            "azure_resource_id": f"/subscriptions/sub/resourceGroups/{role}/providers/Microsoft.Compute/virtualMachines/guard",
            "azure_vm_id": "11111111-1111-1111-1111-111111111111" if role == "server" else "22222222-2222-2222-2222-222222222222",
            "host_key_fingerprint": "SHA256:" + ("A" if role == "server" else "B") * 43,
        }
        if role == "worker":
            path = tmp_path / "worker-azure-guard-journal.ndjson"
            path.write_text(journal, encoding="utf-8")
            manifest["azure_guard_finalization"][role] = {
                "root_hash": records[-1]["event_hash"], "journal_artifact": path.name,
                "journal_sha256": hashlib.sha256(journal.encode()).hexdigest(),
            }
    descriptor = {
        "schema": "srecon26.azure-guard-deferred-finalizer.v1",
        "status": "PENDING_POST_DEADLINE_ABSENCE",
        "manifest_artifact": "run-manifest.json",
        "roles": {"server": {
            "nonce": NONCE, "label": LABEL, "hard_deadline": DEADLINE, "finalize_after": DEADLINE,
            "expected_heartbeat_timeout_seconds": 120,
            "endpoint": {
                "host": "guard.example.test", "user": "guardrpc", "port": 22, "timeout_seconds": 20,
                "identity_file": str(tmp_path / "guard-key"), "known_hosts_file": str(tmp_path / "known-hosts"),
            },
            "arm_receipt": {**manifest["guards"]["server"], "run_id": f"two-node-server-{NONCE}"},
        }},
    }
    (tmp_path / "run-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    path = tmp_path / "deferred-azure-guard-finalizer.json"
    path.write_text(json.dumps(descriptor), encoding="utf-8")
    return path


class FakeTransport:
    authority = DEADLINE

    def __init__(self, _config) -> None:
        self.resumed = None

    def resume_arm_binding(self, receipt) -> None:
        self.resumed = dict(receipt)

    def call(self, command: str, payload: dict[str, object]) -> dict[str, object]:
        assert command == "status"
        assert self.resumed is not None and payload["nonce"] == self.resumed["nonce"]
        _, records = journal_fixture(self.resumed["role"], self.authority)
        return {
            "status": "ABSENCE_CONFIRMED", "root_hash": records[-1]["event_hash"],
            "nonce": self.resumed["nonce"], "label": self.resumed["label"], "hard_deadline": DEADLINE,
            "teardown_authority_at": self.authority, "absence_observations": READS,
        }

    def export_evidence(self, *, nonce: str, root_hash: str) -> SimpleNamespace:
        journal, records = journal_fixture(self.resumed["role"], self.authority)
        assert nonce == self.resumed["nonce"] and root_hash == records[-1]["event_hash"]
        return SimpleNamespace(journal=journal, journal_sha256=hashlib.sha256(journal.encode()).hexdigest())


@pytest.fixture
def scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    run = tmp_path / "artifacts/runs/two-node-fixture"
    run.mkdir(parents=True)
    output = tmp_path / "artifacts/evidence-pack"
    output.mkdir()
    monkeypatch.setattr(build_evidence_pack, "ROOT", tmp_path)
    monkeypatch.setattr(build_evidence_pack, "OUT", output)
    descriptor = write_fixture(run)
    rebuilds = []

    def rebuild():
        pending = json.loads(descriptor.read_text())
        manifest = json.loads((run / "run-manifest.json").read_text())
        assert pending["status"] == manifest["deferred_finalizer"]["status"] == "PENDING_EVIDENCE_REBUILD"
        assert manifest["evidence_status"] == "incomplete"
        rebuilds.append(True)
        return {"two_node_canary": build_evidence_pack.two_node_startup_chart(current=NOW)}

    return SimpleNamespace(descriptor=descriptor, run=run, rebuild=rebuild, rebuilds=rebuilds)


def finalize(scenario, **kwargs):
    return finalize_deferred(scenario.descriptor, now=lambda: NOW, sleep=lambda _seconds: None,
                             **{"transport_factory": FakeTransport, "rebuild": scenario.rebuild, **kwargs})


def test_deferred_finalizer_resumes_exports_and_updates_bound_manifest(scenario) -> None:
    evidence = finalize(scenario)
    assert evidence["server"]["absence_proof_valid"] is True
    deferred = json.loads(scenario.descriptor.read_text())
    manifest = json.loads((scenario.run / "run-manifest.json").read_text())
    assert deferred["status"] == manifest["deferred_finalizer"]["status"] == "COMPLETED"
    assert manifest["azure_guard_evidence_status"] == "complete"
    assert manifest["azure_guard_finalization"]["server"]["status"] == "ABSENCE_CONFIRMED"
    assert manifest["evidence_status"] == "incomplete"
    assert "finalization_errors" not in manifest
    assert manifest["evidence_pack"]["status"] == "REBUILT"
    assert scenario.rebuilds == [True]


def test_deferred_finalizer_rebuilds_managed_run_command_channel(scenario) -> None:
    descriptor = json.loads(scenario.descriptor.read_text())
    endpoint = descriptor["roles"]["server"]["endpoint"]
    endpoint.clear()
    endpoint.update({
        "transport": "azure-run-command",
        "subscription_id": "11111111-1111-1111-1111-111111111111",
        "resource_group": "SRECON26-GUARD-EASTUS-V2-RG",
        "vm_name": "srecon26-guard-eastus-primary-b",
        "az_path": sys.executable,
        "timeout_seconds": 20,
    })
    scenario.descriptor.write_text(json.dumps(descriptor))
    received = []

    class RunCommandTransport(FakeTransport):
        def __init__(self, config) -> None:
            assert isinstance(config, AzureRunCommandGuardConfig)
            assert config.vm_name == "srecon26-guard-eastus-primary-b"
            received.append(config)
            super().__init__(config)

    finalize(scenario, transport_factory=RunCommandTransport)
    assert len(received) == 1


def test_deferred_finalizer_refuses_unknown_serialized_transport(scenario) -> None:
    descriptor = json.loads(scenario.descriptor.read_text())
    descriptor["roles"]["server"]["endpoint"]["transport"] = "unsupported"
    scenario.descriptor.write_text(json.dumps(descriptor))
    with pytest.raises(DeferredFinalizerError, match="transport is invalid"):
        finalize(scenario)


def test_heartbeat_loss_before_deadline_accepts_later_durable_absence(scenario, monkeypatch) -> None:
    monkeypatch.setattr(FakeTransport, "authority", "2026-09-24T11:59:00Z")
    evidence = finalize(scenario)
    assert evidence["server"]["teardown_authority_at"] == "2026-09-24T11:59:00Z"


@pytest.mark.parametrize("authority,reads", [
    ("2026-09-24T12:01:00Z", READS),
    (DEADLINE, ["2026-09-24T11:59:00Z", *READS[1:]]),
    (DEADLINE, [READS[0], READS[0], READS[2]]),
    (DEADLINE, list(reversed(READS))),
    (DEADLINE, [*READS[:2], "2026-09-24T12:05:00Z"]),
])
def test_deferred_absence_rejects_invalid_authority_or_reads(authority, reads) -> None:
    assert not _post_deadline_absence({"status": "ABSENCE_CONFIRMED", "teardown_authority_at": authority,
                                       "absence_observations": reads}, datetime.fromisoformat(DEADLINE), NOW)


def test_deferred_finalizer_refuses_resume_before_immutable_deadline(scenario) -> None:
    with pytest.raises(DeferredFinalizerError, match="deadline has not arrived"):
        finalize_deferred(scenario.descriptor, now=lambda: datetime(2026, 9, 24, 11, 59, tzinfo=UTC))


@pytest.mark.parametrize("field,value", [
    ("role", "worker"), ("host_identity", "replacement-guard.example.test"),
    ("azure_resource_id", "/subscriptions/sub/resourceGroups/other/providers/Microsoft.Compute/virtualMachines/guard"),
    ("azure_vm_id", "33333333-3333-3333-3333-333333333333"),
    ("script_hash", "d" * 64), ("root_hash", "e" * 64),
    ("hard_deadline", "2026-09-24T12:00:01Z"), ("last_heartbeat", "2026-09-24T11:01:00Z"),
    ("heartbeat_timeout_seconds", 60), ("host_key_fingerprint", "SHA256:" + "C" * 43),
])
def test_descriptor_cannot_substitute_original_attestation(scenario, field, value) -> None:
    descriptor = json.loads(scenario.descriptor.read_text())
    descriptor["roles"]["server"]["arm_receipt"][field] = value
    scenario.descriptor.write_text(json.dumps(descriptor))

    def no_transport(_config):
        pytest.fail("substituted descriptor must fail before connecting")

    with pytest.raises(DeferredFinalizerError, match="binding is inconsistent|original manifest attestation"):
        finalize(scenario, transport_factory=no_transport)


@pytest.mark.parametrize("failure", [subprocess.CalledProcessError(1, "rebuild"), RuntimeError("unavailable")])
def test_failed_rebuild_preserves_proof_and_retries_offline(scenario, failure) -> None:
    def failed_rebuild():
        raise failure

    with pytest.raises(DeferredFinalizerError, match="rebuild failed"):
        finalize(scenario, rebuild=failed_rebuild)
    deferred = json.loads(scenario.descriptor.read_text())
    manifest = json.loads((scenario.run / "run-manifest.json").read_text())
    assert deferred["status"] == manifest["deferred_finalizer"]["status"] == "PENDING_EVIDENCE_REBUILD"
    assert "completed_at" not in deferred
    assert manifest["evidence_status"] == "incomplete"
    journal = (scenario.run / "server-azure-guard-journal.ndjson").read_bytes()
    assert deferred["evidence"]["server"]["journal_sha256"] == hashlib.sha256(journal).hexdigest()

    def no_transport(_config):
        pytest.fail("validated saved proof must not need a reachable guard on retry")

    finalize(scenario, transport_factory=no_transport)
    assert (scenario.run / "server-azure-guard-journal.ndjson").read_bytes() == journal
    assert json.loads(scenario.descriptor.read_text())["status"] == "COMPLETED"


@pytest.mark.parametrize("field,value", [
    ("both_bound_arm_receipts", False), ("both_guard_journals_hash_chain_verified", False),
    ("deferred_guard_evidence_validated", {"server": False}), ("manifest_sha256", "f" * 64),
])
def test_successful_process_without_semantic_evidence_stays_retryable(scenario, field, value) -> None:
    def invalid_rebuild():
        summary = scenario.rebuild()
        summary["two_node_canary"]["attempts"][0][field] = value
        return summary

    with pytest.raises(DeferredFinalizerError, match="rebuild failed"):
        finalize(scenario, rebuild=invalid_rebuild)
    assert json.loads(scenario.descriptor.read_text())["status"] == "PENDING_EVIDENCE_REBUILD"
    finalize(scenario)


def test_saved_proof_cannot_be_replaced_after_failed_rebuild(scenario) -> None:
    with pytest.raises(DeferredFinalizerError, match="rebuild failed"):
        finalize(scenario, rebuild=lambda: {})
    (scenario.run / "server-azure-guard-journal.ndjson").write_text("{}\n")
    with pytest.raises(DeferredFinalizerError, match="saved guard evidence is invalid"):
        finalize(scenario)


def test_complete_run_claim_requires_rebuilt_provider_evidence(scenario) -> None:
    path = scenario.run / "run-manifest.json"
    manifest = json.loads(path.read_text())
    manifest["status"] = "completed"
    manifest.pop("failure")
    path.write_text(json.dumps(manifest))

    with pytest.raises(DeferredFinalizerError, match="rebuild failed"):
        finalize(scenario)
    assert json.loads(path.read_text())["evidence_status"] == "incomplete"
    assert json.loads(scenario.descriptor.read_text())["status"] == "PENDING_EVIDENCE_REBUILD"


def test_valid_hash_chain_without_durable_absence_cannot_finalize(scenario) -> None:
    class MissingEvents(FakeTransport):
        def call(self, command, payload):
            status = super().call(command, payload)
            _journal, records = journal_fixture("server", with_authority=False)
            return {**status, "root_hash": records[-1]["event_hash"]}

        def export_evidence(self, **kwargs):
            journal, _records = journal_fixture("server", with_authority=False)
            return SimpleNamespace(journal=journal, journal_sha256=hashlib.sha256(journal.encode()).hexdigest())

    with pytest.raises(DeferredFinalizerError, match="semantic validation failed"):
        finalize(scenario, transport_factory=MissingEvents)
    assert json.loads(scenario.descriptor.read_text())["status"] == "PENDING_POST_DEADLINE_ABSENCE"


def test_partial_role_failure_preserves_completed_channel_for_retry(scenario) -> None:
    descriptor = json.loads(scenario.descriptor.read_text())
    manifest = json.loads((scenario.run / "run-manifest.json").read_text())
    worker = manifest["guards"]["worker"]
    descriptor["roles"]["worker"] = {
        **descriptor["roles"]["server"], "nonce": worker["nonce"], "label": worker["label"],
        "arm_receipt": {**worker, "run_id": f"two-node-worker-{worker['nonce']}"},
    }
    scenario.descriptor.write_text(json.dumps(descriptor))

    class WorkerUnavailable(FakeTransport):
        def call(self, command, payload):
            if self.resumed["role"] == "worker":
                raise RuntimeError("worker unreachable")
            return super().call(command, payload)

    with pytest.raises(RuntimeError, match="worker unreachable"):
        finalize(scenario, transport_factory=WorkerUnavailable)
    descriptor = json.loads(scenario.descriptor.read_text())
    assert descriptor["status"] == "PENDING_POST_DEADLINE_ABSENCE"
    assert set(descriptor["evidence"]) == {"server"}

    class OnlyWorker(FakeTransport):
        def resume_arm_binding(self, receipt):
            assert receipt["role"] == "worker", "completed server must be reused offline"
            super().resume_arm_binding(receipt)

    proof = finalize(scenario, transport_factory=OnlyWorker)
    assert set(proof) == {"server", "worker"}
    assert json.loads(scenario.descriptor.read_text())["status"] == "COMPLETED"
