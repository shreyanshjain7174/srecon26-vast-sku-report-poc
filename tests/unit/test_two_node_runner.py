from __future__ import annotations

from pathlib import Path

import pytest

from scripts.run_two_node_metric_path import build_guards, parse_args, validate_configuration
from srecon26_poc.live_factory import DynamicAzureGuard, DynamicGitHubGuard


def base_arguments(tmp_path: Path) -> list[str]:
    return [
        "--server-offer", "11",
        "--server-machine", "101",
        "--worker-offer", "12",
        "--worker-machine", "102",
        "--output", str(tmp_path / "run"),
        "--report-adapter-factory", "adapter:create",
    ]


def test_azure_backend_builds_two_nonce_isolated_guard_clients(tmp_path: Path) -> None:
    arguments = [
        *base_arguments(tmp_path),
        "--guard-backend", "azure",
        "--azure-guard-host", "guard.example.test",
        "--azure-guard-user", "guardrpc",
        "--azure-guard-identity", str(tmp_path / "guard-key"),
        "--azure-guard-known-hosts", str(tmp_path / "known-hosts"),
    ]
    args = parse_args(arguments)

    validate_configuration(args)
    server, worker = build_guards(args, on_server_armed=lambda _receipt: None, on_worker_armed=lambda _receipt: None)

    assert isinstance(server, DynamicAzureGuard)
    assert isinstance(worker, DynamicAzureGuard)
    assert server is not worker
    assert server.config == worker.config


def test_default_github_backend_preserves_distinct_channel_configuration(tmp_path: Path) -> None:
    args = parse_args(
        [
            *base_arguments(tmp_path),
            "--server-guard-repository", "owner/server-guard",
            "--server-guard-issue", "7",
            "--worker-guard-repository", "owner/worker-guard",
            "--worker-guard-issue", "8",
            "--guard-author", "sunny",
        ]
    )

    validate_configuration(args)
    server, worker = build_guards(args, on_server_armed=lambda _receipt: None, on_worker_armed=lambda _receipt: None)

    assert isinstance(server, DynamicGitHubGuard)
    assert isinstance(worker, DynamicGitHubGuard)
    assert server.config.repository == "owner/server-guard"
    assert worker.config.repository == "owner/worker-guard"


def test_paid_runner_refuses_missing_report_adapter_before_guard_or_create(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SRECON26_REPORT_ADAPTER_FACTORY", raising=False)
    args = parse_args(
        [
            "--server-offer", "11",
            "--server-machine", "101",
            "--worker-offer", "12",
            "--worker-machine", "102",
            "--guard-backend", "azure",
            "--azure-guard-host", "guard.example.test",
            "--azure-guard-user", "guardrpc",
            "--azure-guard-identity", str(tmp_path / "guard-key"),
            "--azure-guard-known-hosts", str(tmp_path / "known-hosts"),
            "--output", str(tmp_path / "run"),
        ]
    )

    with pytest.raises(SystemExit, match="report-adapter-factory"):
        validate_configuration(args)


def test_github_backend_still_rejects_a_shared_issue_channel(tmp_path: Path) -> None:
    args = parse_args(
        [
            *base_arguments(tmp_path),
            "--server-guard-repository", "owner/guard",
            "--server-guard-issue", "7",
            "--worker-guard-repository", "owner/guard",
            "--worker-guard-issue", "7",
            "--guard-author", "sunny",
        ]
    )

    with pytest.raises(SystemExit, match="distinct independent"):
        validate_configuration(args)
