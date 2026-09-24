from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.run_two_node_metric_path import (
    build_guards,
    finalize_azure_guard_channels,
    parse_args,
    validate_configuration,
    validate_armed_guard_independence,
    workload_config,
)
from srecon26_poc.live_factory import DynamicAzureGuard


def base_arguments(tmp_path: Path) -> list[str]:
    return [
        "--server-offer", "11", "--server-machine", "101",
        "--worker-offer", "12", "--worker-machine", "102",
        "--guard-backend", "azure",
        "--server-azure-guard-host", "server-guard.example.test",
        "--server-azure-guard-user", "guardrpc",
        "--server-azure-guard-identity", str(tmp_path / "server-key"),
        "--server-azure-guard-known-hosts", str(tmp_path / "server-known-hosts"),
        "--worker-azure-guard-host", "worker-guard.example.test",
        "--worker-azure-guard-user", "guardrpc",
        "--worker-azure-guard-identity", str(tmp_path / "worker-key"),
        "--worker-azure-guard-known-hosts", str(tmp_path / "worker-known-hosts"),
        "--guard-heartbeat-timeout-seconds", "120",
        "--output", str(tmp_path / "run"),
        "--report-adapter-factory", "adapter:create",
    ]


def test_paid_runner_requires_explicit_azure_backend(tmp_path: Path) -> None:
    arguments = base_arguments(tmp_path)
    backend_index = arguments.index("--guard-backend")
    del arguments[backend_index:backend_index + 2]
    with pytest.raises(SystemExit):
        parse_args(arguments)

    arguments.extend(("--guard-backend", "github"))
    with pytest.raises(SystemExit):
        parse_args(arguments)


def test_azure_backend_builds_independent_guard_clients(tmp_path: Path) -> None:
    args = parse_args(base_arguments(tmp_path))
    validate_configuration(args)
    server, worker = build_guards(args, on_server_armed=lambda _receipt: None, on_worker_armed=lambda _receipt: None)

    assert isinstance(server, DynamicAzureGuard)
    assert isinstance(worker, DynamicAzureGuard)
    assert server is not worker
    assert server.config.host != worker.config.host
    assert server.config.identity_file != worker.config.identity_file
    assert server.config.known_hosts_file != worker.config.known_hosts_file


@pytest.mark.parametrize(
    ("worker_option", "server_option", "match"),
    [
        ("--worker-azure-guard-host", "--server-azure-guard-host", "controller hosts"),
        ("--worker-azure-guard-identity", "--server-azure-guard-identity", "SSH identities"),
        ("--worker-azure-guard-known-hosts", "--server-azure-guard-known-hosts", "known-hosts"),
    ],
)
def test_paid_runner_rejects_shared_azure_channel_material(
    tmp_path: Path,
    worker_option: str,
    server_option: str,
    match: str,
) -> None:
    arguments = base_arguments(tmp_path)
    arguments[arguments.index(worker_option) + 1] = arguments[arguments.index(server_option) + 1]
    args = parse_args(arguments)

    with pytest.raises(SystemExit, match=match):
        validate_configuration(args)


def test_paid_runner_refuses_missing_report_adapter_before_guard_or_create(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SRECON26_REPORT_ADAPTER_FACTORY", raising=False)
    arguments = base_arguments(tmp_path)
    adapter_index = arguments.index("--report-adapter-factory")
    del arguments[adapter_index:adapter_index + 2]
    args = parse_args(arguments)

    with pytest.raises(SystemExit, match="report-adapter-factory"):
        validate_configuration(args)


def test_heartbeat_cadence_is_bounded_by_guard_timer_and_wired_to_ssh(tmp_path: Path) -> None:
    args = parse_args([*base_arguments(tmp_path), "--heartbeat-seconds", "121"])
    with pytest.raises(SystemExit, match="must not exceed"):
        validate_configuration(args)

    args = parse_args([*base_arguments(tmp_path), "--heartbeat-seconds", "90"])
    validate_configuration(args)
    assert workload_config(args).heartbeat_seconds == 90


class FakeArmedGuard:
    def __init__(self, *, status: str, status_error: bool = False) -> None:
        self.arm_receipt = SimpleNamespace(
            root_hash="a" * 64,
            nonce="nonce-12345678",
            label="guard-label",
            hard_deadline=datetime(2099, 1, 1, tzinfo=UTC),
            heartbeat_timeout_seconds=120,
        )
        self.config = SimpleNamespace(
            host="guard.example.test", user="guardrpc", port=22,
            timeout_seconds=20,
            identity_file=Path("/secure/key"), known_hosts_file=Path("/secure/known-hosts"),
            heartbeat_timeout_seconds=120,
        )
        self.attestation = None
        self.transport = SimpleNamespace(host_key_fingerprint="SHA256:" + "A" * 43)
        self.response = {"status": status, "root_hash": "a" * 64}
        self.status_error = status_error
        self.status_calls = 0
        self.export_calls = 0

    def status(self) -> dict[str, object]:
        self.status_calls += 1
        if self.status_error:
            raise RuntimeError("status unavailable")
        return self.response

    def export_evidence(self, root_hash: str) -> SimpleNamespace:
        self.export_calls += 1
        assert root_hash == "a" * 64
        journal = '{"event":"armed"}\n'
        return SimpleNamespace(journal=journal, journal_sha256=hashlib.sha256(journal.encode()).hexdigest())


def test_every_armed_guard_is_statused_and_exported_without_local_instance(tmp_path: Path) -> None:
    server = FakeArmedGuard(status="AWAITING_INSTANCE")
    worker = FakeArmedGuard(status="AWAITING_INSTANCE")

    evidence, errors = finalize_azure_guard_channels(
        guards={"server": server, "worker": worker},  # type: ignore[arg-type]
        guard_receipts={"server": {"role": "server", "script_hash": "c" * 64}, "worker": {"role": "worker"}},
        labels={"server": "server-label", "worker": "worker-label"},
        observed_labels=set(), output=tmp_path, absence_timeout_seconds=30,
        sleep=lambda _seconds: None,
    )

    assert len(errors) == 2
    assert all("deferred post-deadline" in error for error in errors)
    assert server.status_calls == worker.status_calls == 1
    assert server.export_calls == worker.export_calls == 1
    assert evidence["server"]["instance_observed_locally"] is False
    assert evidence["worker"]["journal_artifact"] == "worker-azure-guard-journal.ndjson"
    deferred = json.loads((tmp_path / "deferred-azure-guard-finalizer.json").read_text())
    assert deferred["status"] == "PENDING_POST_DEADLINE_ABSENCE"
    assert set(deferred["roles"]) == {"server", "worker"}
    assert deferred["roles"]["server"]["arm_receipt"]["script_hash"] == "c" * 64
    assert deferred["roles"]["server"]["arm_receipt"]["role"] == "server"


def test_armed_guard_export_is_attempted_even_when_status_fails(tmp_path: Path) -> None:
    server = FakeArmedGuard(status="ARMED", status_error=True)
    worker = FakeArmedGuard(status="AWAITING_INSTANCE")

    _evidence, errors = finalize_azure_guard_channels(
        guards={"server": server, "worker": worker},  # type: ignore[arg-type]
        guard_receipts={},
        labels={"server": "server-label", "worker": "worker-label"},
        observed_labels=set(), output=tmp_path, absence_timeout_seconds=30,
        sleep=lambda _seconds: None,
    )

    assert server.status_calls == 1
    assert server.export_calls == 1
    assert any("server Azure guard status failed" in error for error in errors)


@pytest.mark.parametrize("authority", ["2026-09-24T11:59:00Z", "2026-09-24T12:00:00Z"])
def test_ambiguous_create_accepts_only_post_deadline_terminal_absence(tmp_path: Path, authority: str) -> None:
    deadline = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    server = FakeArmedGuard(status="ABSENCE_CONFIRMED")
    server.arm_receipt.hard_deadline = deadline
    server.response.update(
        {
            "teardown_authority_at": authority,
            "absence_observations": [
                "2026-09-24T12:01:00Z", "2026-09-24T12:02:00Z", "2026-09-24T12:03:00Z",
            ],
        }
    )
    worker = FakeArmedGuard(status="ARMED")
    worker.arm_receipt = None

    evidence, errors = finalize_azure_guard_channels(
        guards={"server": server, "worker": worker},  # type: ignore[arg-type]
        guard_receipts={},
        labels={"server": "server-label", "worker": "worker-label"},
        observed_labels=set(), output=tmp_path, absence_timeout_seconds=30,
        sleep=lambda _seconds: None,
        now=lambda: datetime(2026, 9, 24, 12, 4, tzinfo=UTC),
    )

    assert errors == []
    assert evidence["server"]["absence_proof_valid"] is True
    assert not (tmp_path / "deferred-azure-guard-finalizer.json").exists()


@pytest.mark.parametrize(
    "shared_field",
    ["host_identity", "azure_resource_id", "azure_vm_id", "host_key_fingerprint"],
)
def test_paid_create_gate_rejects_shared_attested_resource_or_host_key(shared_field: str) -> None:
    server = {
        "status": "ARMED",
        "host_identity": "server-guard",
        "azure_resource_id": "/subscriptions/s/resourceGroups/a/providers/Microsoft.Compute/virtualMachines/server",
        "azure_vm_id": "11111111-1111-1111-1111-111111111111",
        "host_key_fingerprint": "SHA256:" + "A" * 43,
        "heartbeat_timeout_seconds": 120,
    }
    worker = {
        **server,
        "host_identity": "worker-guard",
        "azure_resource_id": "/subscriptions/s/resourceGroups/b/providers/Microsoft.Compute/virtualMachines/worker",
        "azure_vm_id": "22222222-2222-2222-2222-222222222222",
        "host_key_fingerprint": "SHA256:" + "B" * 43,
    }
    worker[shared_field] = server[shared_field]

    with pytest.raises(Exception, match=shared_field):
        validate_armed_guard_independence(
            role="worker", receipt=worker, existing={"server": server},
            expected_heartbeat_timeout_seconds=120,
        )


def test_paid_create_gate_rejects_unattested_heartbeat_timeout() -> None:
    receipt = {
        "host_identity": "server-guard",
        "azure_resource_id": "/subscriptions/s/resourceGroups/a/providers/Microsoft.Compute/virtualMachines/server",
        "azure_vm_id": "11111111-1111-1111-1111-111111111111",
        "host_key_fingerprint": "SHA256:" + "A" * 43,
        "heartbeat_timeout_seconds": 60,
    }
    with pytest.raises(Exception, match="heartbeat timeout"):
        validate_armed_guard_independence(
            role="server", receipt=receipt, existing={}, expected_heartbeat_timeout_seconds=120,
        )
