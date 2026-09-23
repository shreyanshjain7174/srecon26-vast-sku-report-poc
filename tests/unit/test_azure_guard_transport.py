from __future__ import annotations

import json
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from srecon26_poc.azure_guard_transport import (
    AzureGuardSshConfig,
    AzureGuardTransportError,
    AzureSshGuardTransport,
    _run,
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


@pytest.mark.parametrize(
    "response_override",
    [
        {"status": "ABSENCE_PENDING"},
        {"nonce": "other_nonce_1234"},
        {"label": "other--nonce-nonce_12345678"},
        {"hard_deadline": "2026-09-24T04:00:01Z"},
    ],
)
def test_arm_response_must_be_armed_and_exactly_bound_to_the_request(tmp_path: Path, response_override: dict[str, object]) -> None:
    response: dict[str, object] = {
        "status": "ARMED",
        "root_hash": ROOT,
        "nonce": NONCE,
        "label": LABEL,
        "hard_deadline": "2026-09-24T04:00:00Z",
    }
    response.update(response_override)

    def runner(arguments, request: str, timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 0, stdout=json.dumps(response), stderr="")

    with pytest.raises(AzureGuardTransportError):
        _transport(tmp_path, runner).call(
            "arm",
            {"run_id": "run-1", "label": LABEL, "nonce": NONCE, "hard_deadline": "2026-09-24T04:00:00Z"},
        )


def test_preflight_must_restate_the_validated_arm_binding(tmp_path: Path) -> None:
    responses = iter(
        [
            {
                "status": "ARMED",
                "root_hash": ROOT,
                "nonce": NONCE,
                "label": LABEL,
                "hard_deadline": "2026-09-24T04:00:00Z",
            },
            {
                "status": "ARMED",
                "root_hash": "b" * 64,
                "nonce": NONCE,
                "label": LABEL,
                "hard_deadline": "2026-09-24T04:00:00Z",
                "host_identity": "azure-guard-01",
                "script_hash": "c" * 64,
            },
        ]
    )

    def runner(arguments, request: str, timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 0, stdout=json.dumps(next(responses)), stderr="")

    transport = _transport(tmp_path, runner)
    transport.call(
        "arm",
        {"run_id": "run-1", "label": LABEL, "nonce": NONCE, "hard_deadline": "2026-09-24T04:00:00Z"},
    )
    assert transport.call("preflight", {})["host_identity"] == "azure-guard-01"


@pytest.mark.parametrize(
    "observations",
    [
        ["2026-09-24T04:01:00Z", "2026-09-24T04:02:00Z"],
        ["2026-09-24T04:01:00Z", "2026-09-24T04:01:00Z", "2026-09-24T04:03:00Z"],
        ["2026-09-24T04:02:00Z", "2026-09-24T04:01:00Z", "2026-09-24T04:03:00Z"],
    ],
)
def test_absence_confirmed_requires_three_distinct_ordered_observations(tmp_path: Path, observations: list[str]) -> None:
    def runner(arguments, request: str, timeout: int) -> subprocess.CompletedProcess[str]:
        response = {
            "status": "ABSENCE_CONFIRMED",
            "root_hash": ROOT,
            "nonce": NONCE,
            "label": LABEL,
            "hard_deadline": "2026-09-24T04:00:00Z",
            "teardown_authority_at": "2026-09-24T04:00:00Z",
            "absence_observations": observations,
        }
        return subprocess.CompletedProcess(arguments, 0, stdout=json.dumps(response), stderr="")

    with pytest.raises(AzureGuardTransportError, match="observations"):
        _transport(tmp_path, runner).call("status", {"nonce": NONCE})


def test_absence_confirmed_accepts_exactly_three_distinct_ordered_observations(tmp_path: Path) -> None:
    observations = ["2026-09-24T04:01:00Z", "2026-09-24T04:02:00Z", "2026-09-24T04:03:00Z"]

    def runner(arguments, request: str, timeout: int) -> subprocess.CompletedProcess[str]:
        response = {
            "status": "ABSENCE_CONFIRMED",
            "root_hash": ROOT,
            "nonce": NONCE,
            "label": LABEL,
            "hard_deadline": "2026-09-24T04:00:00Z",
            "teardown_authority_at": "2026-09-24T04:00:00Z",
            "absence_observations": observations,
        }
        return subprocess.CompletedProcess(arguments, 0, stdout=json.dumps(response), stderr="")

    assert _transport(tmp_path, runner).call("status", {"nonce": NONCE})["absence_observations"] == observations


def test_evidence_export_is_bounded_and_hash_verified(tmp_path: Path) -> None:
    journal = '{"event":"armed","root_hash":"' + ROOT + '"}\n'
    journal_sha256 = hashlib.sha256(journal.encode()).hexdigest()

    def runner(arguments, request: str, timeout: int) -> subprocess.CompletedProcess[str]:
        response = {
            "status": "EVIDENCE_EXPORTED",
            "root_hash": ROOT,
            "nonce": NONCE,
            "journal": journal,
            "journal_sha256": journal_sha256,
            "journal_encoding": "utf-8-jsonl",
        }
        return subprocess.CompletedProcess(arguments, 0, stdout=json.dumps(response), stderr="")

    exported = _transport(tmp_path, runner).export_evidence(nonce=NONCE, root_hash=ROOT)

    assert exported.journal == journal
    assert exported.journal_sha256 == journal_sha256


def test_evidence_export_rejects_a_journal_hash_mismatch(tmp_path: Path) -> None:
    journal = '{"event":"armed"}\n'

    def runner(arguments, request: str, timeout: int) -> subprocess.CompletedProcess[str]:
        response = {
            "status": "EVIDENCE_EXPORTED",
            "root_hash": ROOT,
            "nonce": NONCE,
            "journal": journal,
            "journal_sha256": "f" * 64,
            "journal_encoding": "utf-8-jsonl",
        }
        return subprocess.CompletedProcess(arguments, 0, stdout=json.dumps(response), stderr="")

    with pytest.raises(AzureGuardTransportError, match="hash verification"):
        _transport(tmp_path, runner).export_evidence(nonce=NONCE, root_hash=ROOT)


def test_streaming_runner_terminates_when_stdout_exceeds_the_hard_cap() -> None:
    with pytest.raises(AzureGuardTransportError, match="size limit"):
        _run([sys.executable, "-c", "import sys; sys.stdout.write('x' * 70000)"], "{}\n", 5)
