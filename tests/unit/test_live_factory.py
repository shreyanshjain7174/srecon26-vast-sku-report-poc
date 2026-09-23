from __future__ import annotations

import json
import sys
import types
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
    _load_report_gate,
)
from srecon26_poc.live_dispatch import REPORT_MARGIN
from srecon26_poc.reporting import ReportGate
from srecon26_poc.types import RunIdentity


NOW = datetime(2026, 9, 23, 4, 0, tzinfo=UTC)
NONCE = "nonce_12345678"
LABEL = f"srecon26-smoke--nonce-{NONCE}"


def test_live_factory_rejects_report_adapter_without_authenticated_session_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    class OldAdapter:
        def preflight_exact_instance(self, instance_id, label): pass
        def capture_before(self, fault): pass
        def submit(self, fault): return False
        def capture_after(self, receipt): pass

    module = types.SimpleNamespace(create=lambda: OldAdapter())
    monkeypatch.setitem(sys.modules, "old_report_adapter", module)

    with pytest.raises(LiveFactoryError, match="exact-target adapter"):
        _load_report_gate("old_report_adapter:create")

    module.create = lambda: ReportGate(OldAdapter())
    with pytest.raises(LiveFactoryError, match="authenticated-session preflight"):
        _load_report_gate("old_report_adapter:create")


class GitHubRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.ack_anchors = True
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
        if "/comments?per_page=100&since=" in endpoint:
            return json.dumps(self.comments)
        if endpoint.endswith("/comments"):
            body = next((item.split("=", 1)[1] for item in command if item.startswith("body=")), "")
            anchor = body.replace("SRECON26_GUARD_V1 ANCHOR", "SRECON26_GUARD_V1 ANCHORED", 1)
            if anchor != body and self.ack_anchors:
                self.comments.append(
                    {
                        "body": anchor,
                        "created_at": "2026-09-23T04:00:03Z",
                        "user": {"login": "github-actions[bot]"},
                    }
                )
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
    assert any(
        item == f"body=SRECON26_GUARD_V1 HEARTBEAT nonce={NONCE} root={'b' * 64}"
        for call in runner.calls for item in call
    )
    assert any(
        item == f"body=SRECON26_GUARD_V1 ANCHOR nonce={NONCE} root={'c' * 64}"
        for call in runner.calls for item in call
    )
    assert any("comments?per_page=100&since=2026-09-23T04:00:00Z" in argument for call in runner.calls for argument in call)


def test_github_guard_rejects_anchor_without_remote_workflow_ack() -> None:
    runner = GitHubRunner()
    transport = GitHubGuardTransport(
        GitHubGuardConfig("owner/private-repo", "main", 17, "sunny", arm_timeout_seconds=30),
        runner=runner,
        sleep=lambda _seconds: None,
        now=lambda: NOW,
    )
    client = GuardClient(transport, nonce=NONCE)
    identity = RunIdentity("live-run", LABEL, NOW)
    client.arm(identity, NOW + timedelta(minutes=20))
    runner.ack_anchors = False

    with pytest.raises(LiveFactoryError, match="did not acknowledge"):
        client.anchor("e" * 64)


def test_github_guard_bounds_heartbeat_comment_volume() -> None:
    runner = GitHubRunner()
    transport = GitHubGuardTransport(
        GitHubGuardConfig("owner/private-repo", "main", 17, "sunny", heartbeat_seconds=120),
        runner=runner,
        sleep=lambda _seconds: None,
        now=lambda: NOW,
    )
    client = GuardClient(transport, nonce=NONCE)
    identity = RunIdentity("live-run", LABEL, NOW)
    client.arm(identity, NOW + timedelta(minutes=20))

    client.record_heartbeat(identity, 1_000_000_000)
    client.record_heartbeat(identity, 2_000_000_000)
    client.record_heartbeat(identity, 61_000_000_000)

    heartbeat_calls = [
        call for call in runner.calls
        if any("SRECON26_GUARD_V1 HEARTBEAT" in argument for argument in call)
    ]
    assert len(heartbeat_calls) == 2


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


def test_github_guard_can_adopt_exact_prearmed_dual_receipts_without_redispatch() -> None:
    runner = GitHubRunner()
    transport = GitHubGuardTransport(
        GitHubGuardConfig("owner/private-repo", "main", 17, "sunny", dispatch_on_arm=False),
        runner=runner,
        sleep=lambda _seconds: None,
        now=lambda: NOW,
    )
    client = GuardClient(transport, nonce=NONCE)

    receipt = client.arm(RunIdentity("live-run", LABEL, NOW), NOW + timedelta(minutes=20))

    assert receipt.host_identity == "github-runner-1+github-runner-2"
    assert not any("/dispatches" in argument for call in runner.calls for argument in call)


def test_ssh_resolver_rejects_an_instance_record_that_does_not_preserve_exact_label() -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)

    def runner(_arguments, *, timeout: int) -> str:
        del timeout
        return json.dumps({"id": 417, "label": "other--nonce-nonce_12345678", "actual_status": "running", "ssh_host": "203.0.113.8", "ssh_port": 22})

    with pytest.raises(LiveFactoryError, match="changed ID or nonce-bound label"):
        VastSshResolver("vastai", runner=runner).resolve(instance)


def test_ssh_resolver_accepts_only_safe_endpoint_from_exact_instance_record() -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)

    def runner(_arguments, *, timeout: int) -> str:
        del timeout
        return json.dumps({"id": 417, "label": LABEL, "actual_status": "running", "public_ipaddr": "203.0.113.8", "ssh_port": "2222"})

    endpoint = VastSshResolver("vastai", runner=runner).resolve(instance)
    assert (endpoint.host, endpoint.port, endpoint.destination("root")) == ("203.0.113.8", 2222, "root@203.0.113.8")


def test_ssh_resolver_waits_for_running_even_when_endpoint_is_published(tmp_path: Path) -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)
    statuses = iter(("loading", "running"))

    def runner(_arguments, *, timeout: int) -> str:
        del timeout
        return json.dumps({"id": 417, "label": LABEL, "actual_status": next(statuses), "ssh_host": "203.0.113.8", "ssh_port": 2222})

    log = tmp_path / "status.ndjson"
    endpoint = VastSshResolver("vastai", runner=runner, interval_seconds=0).resolve(instance, status_log=log)

    assert endpoint.host == "203.0.113.8"
    assert [json.loads(line)["actual_status"] for line in log.read_text().splitlines()] == ["loading", "running"]


def test_ssh_resolver_attaches_public_key_only_after_exact_ownership_check(tmp_path: Path) -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)
    public_key = tmp_path / "id_rsa.pub"
    public_key.write_text("ssh-rsa QUJDRA== test@example")
    calls: list[list[str]] = []

    def runner(arguments, *, timeout: int) -> str:
        del timeout
        calls.append(arguments)
        return json.dumps({"id": 417, "label": LABEL, "actual_status": "loading"}) if "show" in arguments else ""

    beats: list[int] = []
    VastSshResolver("vastai", runner=runner).attach_public_key(instance, public_key, hard_deadline=datetime.now(UTC) + timedelta(minutes=20), heartbeat=lambda: beats.append(1), status_log=tmp_path / "status.ndjson")

    assert calls[-1][-4:] == ["attach", "ssh", "417", str(public_key)]
    assert len(beats) == 2


def test_ssh_resolver_waits_for_loading_before_attaching_public_key(tmp_path: Path) -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)
    public_key = tmp_path / "id_rsa.pub"
    public_key.write_text("ssh-rsa QUJDRA== test@example")
    statuses = iter(("created", "loading"))
    calls: list[list[str]] = []

    def runner(arguments, *, timeout: int) -> str:
        calls.append(arguments)
        return json.dumps({"id": 417, "label": LABEL, "actual_status": next(statuses)}) if "show" in arguments else ""

    VastSshResolver("vastai", runner=runner, interval_seconds=0).attach_public_key(instance, public_key, hard_deadline=datetime.now(UTC) + timedelta(minutes=20), heartbeat=lambda: None, status_log=tmp_path / "status.ndjson")
    assert sum("show" in call for call in calls) == 2
    assert calls[-1][-4:] == ["attach", "ssh", "417", str(public_key)]


def test_ssh_resolver_does_not_start_cli_lookup_without_full_deadline_window() -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)
    calls: list[list[str]] = []

    def runner(arguments, *, timeout: int) -> str:
        calls.append(arguments)
        return ""

    with pytest.raises(LiveFactoryError, match="cannot fit before immutable teardown margin"):
        VastSshResolver("vastai", runner=runner).resolve(instance, hard_deadline=datetime.now(UTC) + REPORT_MARGIN + timedelta(seconds=21))
    assert calls == []


def test_ssh_resolver_does_not_start_key_attach_lookup_without_full_deadline_window(tmp_path: Path) -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)
    public_key = tmp_path / "id_rsa.pub"
    public_key.write_text("ssh-rsa QUJDRA== test@example")
    calls: list[list[str]] = []

    def runner(arguments, *, timeout: int) -> str:
        calls.append(arguments)
        return ""

    with pytest.raises(LiveFactoryError, match="cannot fit before immutable teardown margin"):
        VastSshResolver("vastai", runner=runner).attach_public_key(instance, public_key, hard_deadline=datetime.now(UTC) + REPORT_MARGIN + timedelta(seconds=21), heartbeat=lambda: None, status_log=tmp_path / "status.ndjson")
    assert calls == []


@pytest.mark.parametrize("status", ["error", "stopped"])
def test_ssh_resolver_refuses_key_attach_without_nonterminal_status(tmp_path: Path, status: str) -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)
    public_key = tmp_path / "id_rsa.pub"
    public_key.write_text("ssh-rsa QUJDRA== test@example")
    calls: list[list[str]] = []

    def runner(arguments, *, timeout: int) -> str:
        del timeout
        calls.append(arguments)
        return json.dumps({"id": 417, "label": LABEL, "actual_status": status})

    with pytest.raises(LiveFactoryError, match="terminal provider status"):
        VastSshResolver("vastai", runner=runner).attach_public_key(instance, public_key, hard_deadline=datetime.now(UTC) + timedelta(minutes=20), heartbeat=lambda: None, status_log=tmp_path / "status.ndjson")
    assert not any("attach" in call for call in calls)


def test_ssh_resolver_never_attaches_key_from_unknown_status(tmp_path: Path) -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)
    public_key = tmp_path / "id_rsa.pub"
    public_key.write_text("ssh-rsa QUJDRA== test@example")
    calls: list[list[str]] = []

    def runner(arguments, *, timeout: int) -> str:
        calls.append(arguments)
        return json.dumps({"id": 417, "label": LABEL, "actual_status": ""})

    with pytest.raises(LiveFactoryError, match="did not reach a safe SSH key attach status"):
        VastSshResolver("vastai", runner=runner, attempts=2, interval_seconds=0).attach_public_key(instance, public_key, hard_deadline=datetime.now(UTC) + timedelta(minutes=20), heartbeat=lambda: None, status_log=tmp_path / "status.ndjson")
    assert not any("attach" in call for call in calls)


def test_guard_workflow_publishes_machine_parseable_bound_arm_receipt() -> None:
    workflow = Path(".github/workflows/independent-guard.yml").read_text(encoding="utf-8")

    assert "guard_worker.py status" in workflow
    assert "SRECON26_GUARD_V1 ARMED nonce=" in workflow
    assert "SRECON26_GUARD_V1 BACKSTOP_ARMED nonce=" in workflow
    assert "script=${script_hash} root=${root_hash}" in workflow
