from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from srecon26_poc.contracts import InstanceContract
from srecon26_poc.guard_client import GuardClient
from srecon26_poc.live_factory import (
    GitHubGuardConfig,
    GitHubGuardTransport,
    LiveFactoryError,
    VastSshResolver,
)
from srecon26_poc.types import RunIdentity


NOW = datetime(2026, 9, 23, 4, 0, tzinfo=UTC)
NONCE = "nonce_12345678"
LABEL = f"srecon26-smoke--nonce-{NONCE}"


class GitHubRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.comments: list[dict[str, object]] = [
            {
                "body": f"SRECON26_GUARD_V1 ARMED nonce={NONCE} label={LABEL} deadline=2026-09-23T04:20:00Z host=github-runner-1 script={'a' * 64} root={'b' * 64}",
                "created_at": "2026-09-23T04:00:01Z",
                "user": {"login": "github-actions[bot]"},
            },
            {
                "body": f"SRECON26_GUARD_V1 BACKSTOP_ARMED nonce={NONCE} label={LABEL} deadline=2026-09-23T04:20:00Z host=github-runner-2 script={'c' * 64} root={'d' * 64}",
                "created_at": "2026-09-23T04:00:02Z",
                "user": {"login": "github-actions[bot]"},
            },
        ]

    def __call__(self, arguments, *, timeout: int) -> str:
        del timeout
        command = list(arguments)
        self.calls.append(command)
        endpoint = command[4]
        if endpoint.endswith("/comments?per_page=100"):
            return json.dumps(self.comments)
        if endpoint.endswith("/comments"):
            return json.dumps({"id": 99})
        return ""


def test_github_guard_dispatch_verifies_bound_action_attestation_then_comments_heartbeats() -> None:
    runner = GitHubRunner()
    transport = GitHubGuardTransport(
        GitHubGuardConfig("owner/private-repo", "main", 17, "sunny"),
        runner=runner,
        sleep=lambda _seconds: None,
        now=lambda: NOW,
    )
    client = GuardClient(transport, nonce=NONCE)
    identity = RunIdentity("live-run", LABEL, NOW)

    armed = client.arm(identity, NOW + timedelta(minutes=20))
    client.record_heartbeat(identity, 7)
    root = client.anchor("c" * 64)

    assert armed.host_identity == "github-runner-1+github-runner-2"
    assert root == "c" * 64
    dispatch = runner.calls[0]
    assert "/actions/workflows/independent-guard.yml/dispatches" in dispatch[4]
    assert f"inputs[nonce]={NONCE}" in dispatch
    assert any(item == f"body=SRECON26_GUARD_V1 HEARTBEAT nonce={NONCE} root={'b' * 64}" for item in runner.calls[-2])
    assert any(item == f"body=SRECON26_GUARD_V1 ANCHOR nonce={NONCE} root={'c' * 64}" for item in runner.calls[-1])


def test_github_guard_rejects_an_attestation_from_any_non_workflow_actor() -> None:
    runner = GitHubRunner()
    runner.comments[0]["user"] = {"login": "attacker"}
    ticks = iter((NOW, NOW + timedelta(minutes=3)))
    transport = GitHubGuardTransport(
        GitHubGuardConfig("owner/private-repo", "main", 17, "sunny", arm_timeout_seconds=30),
        runner=runner,
        sleep=lambda _seconds: None,
        now=lambda: next(ticks),
    )

    with pytest.raises(LiveFactoryError, match="did not publish"):
        transport.call("arm", {"nonce": NONCE, "label": LABEL, "hard_deadline": "2026-09-23T04:20:00Z"})


def test_ssh_resolver_rejects_an_instance_record_that_does_not_preserve_exact_label() -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)

    def runner(_arguments, *, timeout: int) -> str:
        del timeout
        return json.dumps({"id": 417, "label": "other--nonce-nonce_12345678", "ssh_host": "203.0.113.8", "ssh_port": 22})

    with pytest.raises(LiveFactoryError, match="changed ID or nonce-bound label"):
        VastSshResolver("vastai", runner=runner).resolve(instance)


def test_ssh_resolver_accepts_only_safe_endpoint_from_exact_instance_record() -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)

    def runner(_arguments, *, timeout: int) -> str:
        del timeout
        return json.dumps({"id": 417, "label": LABEL, "public_ipaddr": "203.0.113.8", "ssh_port": "2222"})

    endpoint = VastSshResolver("vastai", runner=runner).resolve(instance)
    assert (endpoint.host, endpoint.port, endpoint.destination("root")) == ("203.0.113.8", 2222, "root@203.0.113.8")


def test_guard_workflow_publishes_machine_parseable_bound_arm_receipt() -> None:
    workflow = Path(".github/workflows/independent-guard.yml").read_text(encoding="utf-8")

    assert "guard_worker.py status" in workflow
    assert "SRECON26_GUARD_V1 ARMED nonce=" in workflow
    assert "SRECON26_GUARD_V1 BACKSTOP_ARMED nonce=" in workflow
    assert "script=${script_hash} root=${root_hash}" in workflow
