from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import subprocess
import sys

import pytest

from srecon26_poc.guard_client import GuardClient, GuardClientError, GuardRemoteReceipt
from srecon26_poc.guard import validate_attestation
from srecon26_poc.types import RunIdentity


NOW = datetime(2026, 9, 23, tzinfo=UTC)
NONCE = "nonce-123"
LABEL = f"srecon26-run-1--nonce-{NONCE}"


class Transport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call(self, command: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append((command, payload))
        if command == "preflight":
            return {
                "host_identity": "guard.example",
                "script_hash": "a" * 64,
                "root_hash": "b" * 64,
                "status": "READY",
            }
        if command == "arm":
            return {
                "host_identity": "guard.example",
                "script_hash": "a" * 64,
                "nonce": payload["nonce"],
                "label": payload["label"],
                "hard_deadline": payload["hard_deadline"],
                "last_heartbeat": None,
                "root_hash": "c" * 64,
                "status": "ARMED",
            }
        if command == "anchor":
            return {"root_hash": "d" * 64, "status": "ANCHORED"}
        raise AssertionError(command)


def test_client_serializes_utc_identity_without_instance_or_secret() -> None:
    transport = Transport()
    identity = RunIdentity("run-1", LABEL, NOW)
    client = GuardClient(transport, nonce=NONCE)

    receipt = client.arm(identity, NOW + timedelta(minutes=10))

    assert receipt.status == "ARMED"
    assert transport.calls[0] == (
        "arm",
        {
            "run_id": "run-1",
            "label": LABEL,
            "nonce": NONCE,
            "hard_deadline": "2026-09-23T00:10:00Z",
        },
    )


def test_client_rejects_remote_receipt_with_wrong_nonce() -> None:
    class WrongNonceTransport(Transport):
        def call(self, command: str, payload: dict[str, object]) -> dict[str, object]:
            receipt = super().call(command, payload)
            if command == "arm":
                receipt["nonce"] = "other"
            return receipt

    client = GuardClient(WrongNonceTransport(), nonce=NONCE)
    with pytest.raises(GuardClientError, match="nonce"):
        client.arm(RunIdentity("run-1", LABEL, NOW), NOW + timedelta(minutes=10))


def test_remote_receipt_requires_hash_shaped_root() -> None:
    with pytest.raises(GuardClientError, match="root hash"):
        GuardRemoteReceipt.from_mapping({"root_hash": "untrusted", "status": "ARMED"})


def test_preflight_after_arm_returns_contract_attestation() -> None:
    transport = Transport()
    identity = RunIdentity("run-1", LABEL, NOW)
    client = GuardClient(transport, nonce=NONCE)
    client.arm(identity, NOW + timedelta(minutes=10))

    attestation = client.preflight()

    validate_attestation(identity, attestation)
    assert attestation.nonce == NONCE
    assert attestation.hard_deadline == NOW + timedelta(minutes=10)


def test_guard_service_templates_harden_the_worker_and_keep_secrets_out_of_argv() -> None:
    root = Path(__file__).parents[2]
    service = (root / "guard/srecon26-guard.service.template").read_text(encoding="utf-8")
    timer = (root / "guard/srecon26-guard.timer.template").read_text(encoding="utf-8")
    worker = (root / "guard/guard_worker.py").read_text(encoding="utf-8")
    installer = (root / "guard/install_guard.sh").read_text(encoding="utf-8")

    assert "ExecStart=/usr/local/libexec/srecon26-guard/guard_worker.py" in service
    assert "NoNewPrivileges=true" in service
    assert "TimeoutStartSec=" in service
    assert "VAST_API_KEY" not in service
    assert "Persistent=true" in timer
    assert "OnUnitActiveSec=" in timer
    assert worker.startswith("#!/usr/bin/env python3\n")
    assert installer.index("install -d -o root -g root -m 0755 /usr/local/libexec/srecon26-guard") < installer.index("guard_worker.py\" /usr/local/libexec")
    assert "command -v vastai" in installer
    assert "--vast-bin /usr/local/libexec/srecon26-guard/vastai" in service


def test_client_refuses_a_label_without_the_provider_nonce_protocol() -> None:
    client = GuardClient(Transport(), nonce=NONCE)

    with pytest.raises(GuardClientError, match="nonce-bound"):
        client.arm(RunIdentity("run-1", "srecon26-run-1", NOW), NOW + timedelta(minutes=10))


def test_independent_preflight_fails_closed_without_deployed_root_and_credential(tmp_path) -> None:
    root = Path(__file__).parents[2]
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/preflight_guard.py",
            "--require-independent",
            "--host",
            "guard.example",
            "--root",
            str(tmp_path / "missing-root"),
            "--secret-file",
            str(tmp_path / "missing-secret"),
            "--json",
            str(tmp_path / "attestation.json"),
        ],
        cwd=root,
        env={"PYTHONPATH": "src:."},
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "not deployed" in completed.stderr
