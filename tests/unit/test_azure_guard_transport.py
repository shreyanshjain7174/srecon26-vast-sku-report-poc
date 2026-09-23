from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from srecon26_poc.azure_guard_transport import (
    AzureGuardSshConfig,
    AzureGuardTransportError,
    AzureSshGuardTransport,
)


NONCE = "nonce_12345678"
LABEL = f"srecon26-run-1--nonce-{NONCE}"
ROOT = "a" * 64


def _transport(tmp_path: Path, runner) -> AzureSshGuardTransport:
    identity = tmp_path / "guard-key"
    identity.write_text("test-private-key-placeholder", encoding="utf-8")
    identity.chmod(0o600)
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("guard.example.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest\n", encoding="utf-8")
    known_hosts.chmod(0o600)
    return AzureSshGuardTransport(
        AzureGuardSshConfig(
            host="guard.example.test",
            user="guardrpc",
            identity_file=identity,
            known_hosts_file=known_hosts,
            port=2222,
        ),
        runner=runner,
    )


def test_transport_uses_pinned_noninteractive_fixed_command_and_one_json_request(tmp_path: Path) -> None:
    calls: list[tuple[list[str], str, int]] = []

    def runner(arguments, request: str, timeout: int) -> subprocess.CompletedProcess[str]:
        calls.append((list(arguments), request, timeout))
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout=json.dumps(
                {
                    "status": "ARMED",
                    "root_hash": ROOT,
                    "nonce": NONCE,
                    "label": LABEL,
                    "hard_deadline": "2026-09-24T04:00:00Z",
                }
            )
            + "\n",
            stderr="",
        )

    response = _transport(tmp_path, runner).call(
        "arm",
        {"run_id": "run-1", "label": LABEL, "nonce": NONCE, "hard_deadline": "2026-09-24T04:00:00Z"},
    )

    assert response["status"] == "ARMED"
    arguments, request, timeout = calls[0]
    assert arguments[-1] == "guardctl"
    assert arguments[-2] == "guardrpc@guard.example.test"
    assert "StrictHostKeyChecking=yes" in arguments
    assert "GlobalKnownHostsFile=/dev/null" in arguments
    assert "ClearAllForwardings=yes" in arguments
    assert "ForwardAgent=no" in arguments
    assert "accept-new" not in arguments
    assert all(item not in arguments for item in ("-L", "-R", "-D", "-A", "-t"))
    assert json.loads(request) == {
        "command": "arm",
        "payload": {"hard_deadline": "2026-09-24T04:00:00Z", "label": LABEL, "nonce": NONCE, "run_id": "run-1"},
        "protocol": "srecon26-guard-v1",
    }
    assert request.count("\n") == 1 and request.endswith("\n")
    assert timeout == 20


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [
        ("not-json\n", ""),
        (json.dumps({"status": "ARMED", "root_hash": "bad"}) + "\n", ""),
        (json.dumps({"status": "ARMED", "root_hash": ROOT}) + "\n{}\n", ""),
        (json.dumps({"status": "ARMED", "root_hash": ROOT}) + "\n", "unexpected warning"),
    ],
)
def test_transport_rejects_noncanonical_or_noisy_responses(tmp_path: Path, stdout: str, stderr: str) -> None:
    def runner(arguments, request: str, timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 0, stdout=stdout, stderr=stderr)

    with pytest.raises(AzureGuardTransportError):
        _transport(tmp_path, runner).call("status", {"nonce": NONCE})


def test_transport_rejects_unknown_verbs_and_request_fields_before_ssh(tmp_path: Path) -> None:
    called = False

    def runner(arguments, request: str, timeout: int) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        return subprocess.CompletedProcess(arguments, 0, stdout="{}", stderr="")

    transport = _transport(tmp_path, runner)
    with pytest.raises(AzureGuardTransportError, match="unsupported"):
        transport.call("shell", {"command": "id"})
    with pytest.raises(AzureGuardTransportError, match="unexpected fields"):
        transport.call("preflight", {"command": "id"})
    assert called is False


def test_transport_refuses_missing_known_hosts_file(tmp_path: Path) -> None:
    identity = tmp_path / "guard-key"
    identity.write_text("placeholder", encoding="utf-8")
    identity.chmod(0o600)
    with pytest.raises(AzureGuardTransportError, match="known-hosts"):
        AzureSshGuardTransport(
            AzureGuardSshConfig(
                host="guard.example.test",
                user="guardrpc",
                identity_file=identity,
                known_hosts_file=tmp_path / "missing-known-hosts",
            )
        )
