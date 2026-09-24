from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

from srecon26_poc.azure_run_command_transport import (
    AzureRunCommandGuardConfig,
    AzureRunCommandGuardTransport,
    AzureRunCommandTransportError,
)


NONCE = "nonce_12345678"
LABEL = f"srecon26-run-1--nonce-{NONCE}"
ROOT = "a" * 64


def _arm() -> dict[str, object]:
    return {
        "run_id": "run-1", "label": LABEL, "nonce": NONCE,
        "hard_deadline": "2026-09-24T04:00:00Z", "heartbeat_timeout_seconds": 120,
    }


def _receipt() -> dict[str, object]:
    return {
        "status": "ARMED", "root_hash": ROOT, "nonce": NONCE, "label": LABEL,
        "hard_deadline": "2026-09-24T04:00:00Z", "heartbeat_timeout_seconds": 120,
    }


def _view(output: str, *, state: str = "Succeeded", exit_code: int = 0) -> str:
    return json.dumps({"properties": {"instanceView": {
        "executionState": state, "exitCode": exit_code, "output": output,
    }}})


def _transport(runner) -> AzureRunCommandGuardTransport:
    return AzureRunCommandGuardTransport(
        AzureRunCommandGuardConfig(
            subscription_id="11111111-1111-1111-1111-111111111111",
            resource_group="guard-rg", vm_name="guard-vm", az_path=Path(sys.executable),
        ),
        runner=runner,
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"subscription_id": "not-a-uuid"},
        {"resource_group": "bad/group"},
        {"vm_name": "bad/vm"},
        {"az_path": Path("/missing/az")},
        {"timeout_seconds": 0},
        {"timeout_seconds": 61},
    ],
)
def test_config_rejects_untrusted_azure_endpoint_values(updates: dict[str, object]) -> None:
    values: dict[str, object] = {
        "subscription_id": "11111111-1111-1111-1111-111111111111",
        "resource_group": "guard-rg", "vm_name": "guard-vm", "az_path": Path(sys.executable),
        "timeout_seconds": 20,
    }
    values.update(updates)
    with pytest.raises(AzureRunCommandTransportError):
        AzureRunCommandGuardConfig(**values).validated()  # type: ignore[arg-type]


def test_uses_fixed_run_command_and_base64_stdin_gateway() -> None:
    calls: list[tuple[list[str], str, int]] = []

    def runner(arguments, stdin: str, timeout: int) -> subprocess.CompletedProcess[str]:
        calls.append((list(arguments), stdin, timeout))
        stdout = "{}" if "create" in arguments else _view(json.dumps(_receipt()) + "\n")
        return subprocess.CompletedProcess(arguments, 0, stdout=stdout, stderr="")

    response = _transport(runner).call("arm", _arm())

    assert response["status"] == "ARMED"
    create, status, delete = calls
    assert create[1] == status[1] == delete[1] == ""
    assert create[2] == status[2] == delete[2] == 20
    assert create[0][1:4] == ["vm", "run-command", "create"]
    assert "--command-id" not in create[0]
    assert "RunShellScript" not in create[0]
    assert "--script" in create[0]
    assert "--scripts" not in create[0]
    script = create[0][create[0].index("--script") + 1]
    assert "SSH_ORIGINAL_COMMAND=guardctl" in script
    assert "/usr/local/libexec/srecon26-guard/ssh_rpc.py" in script
    encoded = script.split("'")[3]
    assert json.loads(base64.b64decode(encoded)) == {
        "command": "arm", "payload": _arm(), "protocol": "srecon26-guard-v1",
    }
    assert status[0][1:4] == ["rest", "--method", "get"]
    url = status[0][status[0].index("--url") + 1]
    assert "$expand=instanceView" in url
    assert "guard-rg" in url and "guard-vm" in url
    assert delete[0][1:4] == ["vm", "run-command", "delete"]
    assert delete[0][delete[0].index("--run-command-name") + 1].startswith("srecon26-rpc-")
    assert "--yes" in delete[0]


@pytest.mark.parametrize(
    "output",
    ["not-json\n", json.dumps({"status": "ARMED", "root_hash": ROOT}) + "\n{}\n"],
)
def test_rejects_malformed_guard_response(output: str) -> None:
    calls: list[list[str]] = []

    def runner(arguments, stdin: str, timeout: int) -> subprocess.CompletedProcess[str]:
        calls.append(list(arguments))
        stdout = "{}" if "create" in arguments else _view(output)
        return subprocess.CompletedProcess(arguments, 0, stdout=stdout, stderr="")

    with pytest.raises(AzureRunCommandTransportError, match="invalid response"):
        _transport(runner).call("arm", _arm())
    assert [call[3] for call in calls] == ["create", "get", "delete"]


def test_rejects_nonzero_cli_and_run_command_exit() -> None:
    def failed_cli(arguments, stdin: str, timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 1, stdout="sensitive detail", stderr="sensitive detail")

    with pytest.raises(AzureRunCommandTransportError, match="failed safely"):
        _transport(failed_cli).call("arm", _arm())

    def failed_guest(arguments, stdin: str, timeout: int) -> subprocess.CompletedProcess[str]:
        stdout = "{}" if "create" in arguments else _view("secret guest output", state="Failed", exit_code=1)
        return subprocess.CompletedProcess(arguments, 0, stdout=stdout, stderr="")

    with pytest.raises(AzureRunCommandTransportError, match="refused"):
        _transport(failed_guest).call("arm", _arm())


def test_rejects_oversized_azure_or_guard_response() -> None:
    def huge_azure(arguments, stdin: str, timeout: int) -> subprocess.CompletedProcess[str]:
        stdout = "{}" if "delete" in arguments else "x" * (64 * 1024 + 1)
        return subprocess.CompletedProcess(arguments, 0, stdout=stdout, stderr="")

    with pytest.raises(AzureRunCommandTransportError, match="size limit"):
        _transport(huge_azure).call("arm", _arm())

    def huge_guard(arguments, stdin: str, timeout: int) -> subprocess.CompletedProcess[str]:
        stdout = "{}" if "create" in arguments or "delete" in arguments else _view("x" * (64 * 1024 + 1))
        return subprocess.CompletedProcess(arguments, 0, stdout=stdout, stderr="")

    with pytest.raises(AzureRunCommandTransportError, match="size limit"):
        _transport(huge_guard).call("arm", _arm())


def test_cleanup_failure_never_returns_a_guard_receipt() -> None:
    calls: list[list[str]] = []

    def runner(arguments, stdin: str, timeout: int) -> subprocess.CompletedProcess[str]:
        calls.append(list(arguments))
        if "delete" in arguments:
            return subprocess.CompletedProcess(arguments, 1, stdout="", stderr="")
        stdout = "{}" if "create" in arguments else _view(json.dumps(_receipt()) + "\n")
        return subprocess.CompletedProcess(arguments, 0, stdout=stdout, stderr="")

    with pytest.raises(AzureRunCommandTransportError, match="cleanup failed safely and could not be confirmed"):
        _transport(runner).call("arm", _arm())
    assert [call[3] for call in calls] == ["create", "get", "delete"]
