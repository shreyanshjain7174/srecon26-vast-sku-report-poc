from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.run_two_node_metric_path import (
    build_guards,
    finalize_azure_guard_channels,
    parse_args,
    validate_configuration,
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
        self.arm_receipt = SimpleNamespace(root_hash="a" * 64)
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
        labels={"server": "server-label", "worker": "worker-label"},
        observed_labels=set(), output=tmp_path, absence_timeout_seconds=30,
        sleep=lambda _seconds: None,
    )

    assert errors == []
    assert server.status_calls == worker.status_calls == 1
    assert server.export_calls == worker.export_calls == 1
    assert evidence["server"]["instance_observed_locally"] is False
    assert evidence["worker"]["journal_artifact"] == "worker-azure-guard-journal.ndjson"


def test_armed_guard_export_is_attempted_even_when_status_fails(tmp_path: Path) -> None:
    server = FakeArmedGuard(status="ARMED", status_error=True)
    worker = FakeArmedGuard(status="AWAITING_INSTANCE")

    _evidence, errors = finalize_azure_guard_channels(
        guards={"server": server, "worker": worker},  # type: ignore[arg-type]
        labels={"server": "server-label", "worker": "worker-label"},
        observed_labels=set(), output=tmp_path, absence_timeout_seconds=30,
        sleep=lambda _seconds: None,
    )

    assert server.status_calls == 1
    assert server.export_calls == 1
    assert any("server Azure guard status failed" in error for error in errors)
