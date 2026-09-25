"""Azure Run Command transport for the independent guard RPC.

This is deliberately a narrow escape hatch for environments where a public
SSH path to the guard VM is unavailable.  The controller invokes a fixed
program through Azure's managed Run Command channel; request bytes travel only
as base64 on the fixed shell-script argument and the VM runs the same bounded
``ssh_rpc.py`` gateway used by the SSH transport.
"""
from __future__ import annotations

import base64
import json
import re
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence
from uuid import UUID, uuid4

from .azure_guard_transport import (
    AzureGuardTransportError,
    GuardEvidenceExport,
    _MAX_RESPONSE_BYTES,
    _validate_request,
    _validate_response,
)
from .guard_client import GuardTransport


_RESOURCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.()-]{0,89}$")
_VM_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_COMMAND_NAME = re.compile(r"^srecon26-rpc-[0-9a-f]{24}$")
_API_VERSION = "2024-11-01"


class AzureRunCommandTransportError(AzureGuardTransportError):
    """Azure managed Run Command could not safely carry a guard RPC."""


Runner = Callable[[Sequence[str], str, int], subprocess.CompletedProcess[str]]
Clock = Callable[[], float]
Sleeper = Callable[[float], None]
_PENDING_EXECUTION_STATES = frozenset({"Creating", "Pending", "Running"})
_POLL_INTERVAL_SECONDS = 1.0


def _run(arguments: Sequence[str], request: str, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run one allowlisted Azure CLI invocation with no shell or inherited input."""

    # Every argv element is either fixed text or independently validated below.
    # The request is always empty for Azure CLI itself.  shell=False is explicit
    # to make the execution boundary evident to readers and static analysis.
    # nosemgrep: python.django.security.injection.command.subprocess-injection.subprocess-injection
    return subprocess.run(
        list(arguments),
        input=request,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        shell=False,
    )


def _executable(path: Path) -> Path:
    if not isinstance(path, Path):
        raise AzureRunCommandTransportError("Azure CLI path is invalid")
    try:
        resolved = path.expanduser().resolve(strict=True)
        info = resolved.stat()
    except OSError as error:
        raise AzureRunCommandTransportError("Azure CLI path is missing") from error
    if not resolved.is_file() or info.st_mode & 0o022 or not info.st_mode & stat.S_IXUSR:
        raise AzureRunCommandTransportError("Azure CLI path must be a safe executable file")
    return resolved


@dataclass(frozen=True, slots=True)
class AzureRunCommandGuardConfig:
    """The minimum immutable identity needed for a managed Run Command RPC."""

    subscription_id: str
    resource_group: str
    vm_name: str
    az_path: Path
    timeout_seconds: int = 20
    heartbeat_timeout_seconds: int = 120

    def validated(self) -> "AzureRunCommandGuardConfig":
        try:
            subscription = str(UUID(self.subscription_id))
        except (TypeError, ValueError, AttributeError) as error:
            raise AzureRunCommandTransportError("Azure subscription ID must be a UUID") from error
        if not isinstance(self.resource_group, str) or not _RESOURCE_NAME.fullmatch(self.resource_group):
            raise AzureRunCommandTransportError("Azure guard resource group is invalid")
        if not isinstance(self.vm_name, str) or not _VM_NAME.fullmatch(self.vm_name):
            raise AzureRunCommandTransportError("Azure guard VM name is invalid")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, int) or not 1 <= self.timeout_seconds <= 300:
            raise AzureRunCommandTransportError("Azure Run Command timeout must be 1-300 seconds")
        if (
            isinstance(self.heartbeat_timeout_seconds, bool)
            or not isinstance(self.heartbeat_timeout_seconds, int)
            or not 1 <= self.heartbeat_timeout_seconds <= 600
        ):
            raise AzureRunCommandTransportError("Azure guard heartbeat timeout must be 1-600 seconds")
        return AzureRunCommandGuardConfig(
            subscription_id=subscription,
            resource_group=self.resource_group,
            vm_name=self.vm_name,
            az_path=_executable(self.az_path),
            timeout_seconds=self.timeout_seconds,
            heartbeat_timeout_seconds=self.heartbeat_timeout_seconds,
        )


def _json_object(raw: str) -> Mapping[str, object]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_RESPONSE_BYTES:
        raise AzureRunCommandTransportError("Azure Run Command response exceeds the size limit")
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise AzureRunCommandTransportError("Azure Run Command returned malformed JSON") from error
    if not isinstance(value, dict):
        raise AzureRunCommandTransportError("Azure Run Command returned an invalid response")
    return value


def _instance_view_output(raw: str) -> str | None:
    """Extract successful stdout, or signal a still-pending managed command."""

    response = _json_object(raw)
    properties = response.get("properties")
    if not isinstance(properties, dict):
        raise AzureRunCommandTransportError("Azure Run Command lacks properties")
    view = properties.get("instanceView")
    if not isinstance(view, dict):
        raise AzureRunCommandTransportError("Azure Run Command lacks instance view")
    state, exit_code, output = view.get("executionState"), view.get("exitCode"), view.get("output")
    if state in _PENDING_EXECUTION_STATES:
        return None
    if state != "Succeeded" or type(exit_code) is not int or exit_code != 0:
        raise AzureRunCommandTransportError("Azure Run Command refused the guard request")
    if not isinstance(output, str) or not output:
        raise AzureRunCommandTransportError("Azure Run Command lacks guard output")
    if len(output.encode("utf-8")) > _MAX_RESPONSE_BYTES:
        raise AzureRunCommandTransportError("Azure guard response exceeds the size limit")
    return output


class AzureRunCommandGuardTransport(GuardTransport):
    """Use Azure-managed Run Command to execute one fixed guard RPC gateway."""

    def __init__(
        self,
        config: AzureRunCommandGuardConfig,
        *,
        runner: Runner | None = None,
        clock: Clock = time.monotonic,
        sleeper: Sleeper = time.sleep,
    ) -> None:
        self.config = config.validated()
        self.runner = runner or _run
        self.clock = clock
        self.sleeper = sleeper
        self._arm_binding: dict[str, object] | None = None

    def _command_name(self) -> str:
        name = f"srecon26-rpc-{uuid4().hex[:24]}"
        if not _COMMAND_NAME.fullmatch(name):  # Defensive even though UUID hex is fixed.
            raise AzureRunCommandTransportError("Azure Run Command name is invalid")
        return name

    @staticmethod
    def _script(encoded_request: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9+/=]+", encoded_request):
            raise AzureRunCommandTransportError("guard RPC payload encoding is invalid")
        # The only variable is standard base64, quoted as a literal.  The shell
        # receives no caller-provided command text or path.
        return (
            "set -eu\n"
            f"printf '%s' '{encoded_request}' | /usr/bin/base64 --decode | "
            "SSH_ORIGINAL_COMMAND=guardctl /usr/bin/python3 -I "
            "/usr/local/libexec/srecon26-guard/ssh_rpc.py"
        )

    def create_command(self, name: str, script: str) -> tuple[str, ...]:
        if not _COMMAND_NAME.fullmatch(name):
            raise AzureRunCommandTransportError("Azure Run Command name is invalid")
        return (
            str(self.config.az_path), "vm", "run-command", "create",
            "--subscription", self.config.subscription_id,
            "--resource-group", self.config.resource_group,
            "--vm-name", self.config.vm_name,
            "--name", name,
            "--script", script,
            "--async-execution", "true",
            "--no-wait",
            "--only-show-errors", "--output", "json",
        )

    def delete_command(self, name: str) -> tuple[str, ...]:
        """Build the synchronous deletion of one temporary managed command."""

        if not _COMMAND_NAME.fullmatch(name):
            raise AzureRunCommandTransportError("Azure Run Command name is invalid")
        return (
            str(self.config.az_path), "vm", "run-command", "delete",
            "--subscription", self.config.subscription_id,
            "--resource-group", self.config.resource_group,
            "--vm-name", self.config.vm_name,
            "--run-command-name", name,
            "--yes", "--only-show-errors", "--output", "json",
        )

    def instance_view_command(self, name: str) -> tuple[str, ...]:
        if not _COMMAND_NAME.fullmatch(name):
            raise AzureRunCommandTransportError("Azure Run Command name is invalid")
        path = (
            f"/subscriptions/{self.config.subscription_id}/resourceGroups/{self.config.resource_group}"
            f"/providers/Microsoft.Compute/virtualMachines/{self.config.vm_name}/runCommands/{name}"
        )
        url = f"https://management.azure.com{path}?api-version={_API_VERSION}&$expand=instanceView"
        return (
            str(self.config.az_path), "rest", "--method", "get", "--url", url,
            "--subscription", self.config.subscription_id,
            "--only-show-errors", "--output", "json",
        )

    def _expected_resource_id(self) -> str:
        return (
            f"/subscriptions/{self.config.subscription_id}/resourceGroups/{self.config.resource_group}"
            f"/providers/Microsoft.Compute/virtualMachines/{self.config.vm_name}"
        )

    def _invoke(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        try:
            completed = self.runner(arguments, "", self.config.timeout_seconds)
        except (OSError, subprocess.SubprocessError) as error:
            raise AzureRunCommandTransportError("Azure Run Command request failed safely") from error
        if completed.returncode != 0:
            raise AzureRunCommandTransportError("Azure Run Command request failed safely")
        if not isinstance(completed.stdout, str) or not isinstance(completed.stderr, str):
            raise AzureRunCommandTransportError("Azure Run Command returned an invalid response")
        if completed.stderr:
            raise AzureRunCommandTransportError("Azure Run Command returned unexpected stderr")
        if len(completed.stdout.encode("utf-8")) > _MAX_RESPONSE_BYTES:
            raise AzureRunCommandTransportError("Azure Run Command response exceeds the size limit")
        return completed

    def _wait_for_instance_view(self, name: str) -> str:
        """Poll the fixed Azure resource until success or the pinned deadline."""

        deadline = self.clock() + self.config.timeout_seconds
        while True:
            output = _instance_view_output(self._invoke(self.instance_view_command(name)).stdout)
            if output is not None:
                return output
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise AzureRunCommandTransportError("Azure Run Command did not complete before the pinned timeout")
            self.sleeper(min(_POLL_INTERVAL_SECONDS, remaining))

    def call(self, command: str, payload: dict[str, object]) -> dict[str, object]:
        if command == "arm" and payload.get("heartbeat_timeout_seconds") != self.config.heartbeat_timeout_seconds:
            raise AzureRunCommandTransportError("Azure guard arm request differs from the configured heartbeat timeout")
        request = _validate_request(command, payload)
        serialized = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n"
        encoded = base64.b64encode(serialized.encode("ascii")).decode("ascii")
        name = self._command_name()
        response: dict[str, object] | None = None
        binding: dict[str, object] | None = None
        failure: Exception | None = None
        try:
            # A create failure can happen after Azure accepts the request, so
            # always attempt cleanup once create has been invoked.
            self._invoke(self.create_command(name, self._script(encoded)))
            output = self._wait_for_instance_view(name)
            try:
                response = _validate_response(command, payload, output, self._arm_binding)
            except AzureGuardTransportError as error:
                failure = AzureRunCommandTransportError("Azure guard returned an invalid response")
                failure.__cause__ = error
        except AzureRunCommandTransportError as error:
            failure = error
        except AzureGuardTransportError as error:
            # Kept separate so a future helper from the SSH transport cannot
            # bypass cleanup by raising its shared base type.
            failure = AzureRunCommandTransportError("Azure guard returned an invalid response")
            failure.__cause__ = error
        except Exception as error:
            failure = error

        if failure is None and response is not None and command == "arm":
            binding = {
                "nonce": response["nonce"],
                "label": response["label"],
                "hard_deadline": response["hard_deadline"],
                "heartbeat_timeout_seconds": response["heartbeat_timeout_seconds"],
            }
        elif (
            failure is None
            and response is not None
            and command == "preflight"
            and str(response["azure_resource_id"]).casefold() != self._expected_resource_id().casefold()
        ):
            failure = AzureRunCommandTransportError("Azure guard attested a different managed VM")

        try:
            self._invoke(self.delete_command(name))
        except AzureRunCommandTransportError as error:
            # Do not return a receipt when Azure has not confirmed removal of
            # the per-RPC resource.  A failed RPC is already fail-closed; this
            # preserves that property while preventing silent quota exhaustion.
            raise AzureRunCommandTransportError(
                "Azure Run Command cleanup failed safely and could not be confirmed"
            ) from error

        if failure is not None:
            raise failure
        if response is None:  # Defensive: a successful path must validate a response.
            raise AzureRunCommandTransportError("Azure Run Command returned an invalid response")
        if binding is not None:
            self._arm_binding = binding
        return response

    def resume_arm_binding(self, receipt: Mapping[str, object]) -> None:
        """Restore a validated arm receipt without issuing an unsafe re-arm."""

        required = {"nonce", "label", "hard_deadline", "root_hash", "heartbeat_timeout_seconds"}
        if not required.issubset(receipt):
            raise AzureRunCommandTransportError("deferred finalizer arm receipt is incomplete")
        payload = {
            "run_id": str(receipt.get("run_id", "deferred-finalizer")),
            "nonce": receipt["nonce"],
            "label": receipt["label"],
            "hard_deadline": receipt["hard_deadline"],
            "heartbeat_timeout_seconds": receipt["heartbeat_timeout_seconds"],
        }
        response = {
            "status": "ARMED", "root_hash": receipt["root_hash"], "nonce": receipt["nonce"],
            "label": receipt["label"], "hard_deadline": receipt["hard_deadline"],
            "heartbeat_timeout_seconds": receipt["heartbeat_timeout_seconds"],
        }
        _validate_request("arm", payload)
        _validate_response("arm", payload, json.dumps(response), None)
        if payload["heartbeat_timeout_seconds"] != self.config.heartbeat_timeout_seconds:
            raise AzureRunCommandTransportError("deferred finalizer heartbeat timeout differs from the pinned channel")
        self._arm_binding = {
            "nonce": receipt["nonce"], "label": receipt["label"],
            "hard_deadline": receipt["hard_deadline"],
            "heartbeat_timeout_seconds": receipt["heartbeat_timeout_seconds"],
        }

    def export_evidence(self, *, nonce: str, root_hash: str) -> GuardEvidenceExport:
        response = self.call("export", {"nonce": nonce, "root_hash": root_hash})
        return GuardEvidenceExport(
            nonce=str(response["nonce"]), root_hash=str(response["root_hash"]),
            journal=str(response["journal"]), journal_sha256=str(response["journal_sha256"]),
        )
