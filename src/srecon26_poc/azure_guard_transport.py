"""Bounded SSH JSON transport for an independently hosted Azure guard.

The SSH key is expected to be restricted server-side to the ``guardctl``
forced command.  This client adds its own fail-closed boundary: a pinned host
key, no forwarding or PTY, one fixed remote command, and one JSON document in
each direction.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .guard_client import GuardClientError, GuardTransport


_COMMANDS = frozenset({"preflight", "arm", "heartbeat", "status", "anchor"})
_HEX = re.compile(r"^[0-9a-f]{64}$")
_HOST = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9.-]+(?<!-)$")
_LABEL = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_MAX_RESPONSE_BYTES = 64 * 1024


class AzureGuardTransportError(GuardClientError):
    """The bounded Azure guard channel failed closed."""


Runner = Callable[[Sequence[str], str, int], subprocess.CompletedProcess[str]]


def _run(arguments: Sequence[str], request: str, timeout: int) -> subprocess.CompletedProcess[str]:
    # The caller supplies only AzureSshGuardTransport.command(), whose binary,
    # options, destination grammar, and final remote command are allowlisted.
    # shell=False is the subprocess default and no payload enters argv.
    # nosemgrep: python.django.security.injection.command.subprocess-injection.subprocess-injection
    return subprocess.run(
        list(arguments),
        input=request,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _regular_private_file(path: Path, description: str, *, private: bool = False) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as error:
        raise AzureGuardTransportError(f"{description} is missing") from error
    stat = resolved.stat()
    if not resolved.is_file():
        raise AzureGuardTransportError(f"{description} must be a regular file")
    if stat.st_mode & 0o022:
        raise AzureGuardTransportError(f"{description} must not be group- or world-writable")
    if private and stat.st_mode & 0o077:
        raise AzureGuardTransportError(f"{description} must be private to its owner")
    return resolved


@dataclass(frozen=True, slots=True)
class AzureGuardSshConfig:
    host: str
    user: str
    identity_file: Path
    known_hosts_file: Path
    port: int = 22
    timeout_seconds: int = 20

    def validated(self) -> "AzureGuardSshConfig":
        if not _HOST.fullmatch(self.host) or ".." in self.host:
            raise AzureGuardTransportError("Azure guard host is invalid")
        if not _USER.fullmatch(self.user):
            raise AzureGuardTransportError("Azure guard SSH user is invalid")
        if not 1 <= self.port <= 65535:
            raise AzureGuardTransportError("Azure guard SSH port is invalid")
        if not 1 <= self.timeout_seconds <= 60:
            raise AzureGuardTransportError("Azure guard timeout must be 1-60 seconds")
        identity = _regular_private_file(self.identity_file, "Azure guard SSH identity", private=True)
        known_hosts = _regular_private_file(self.known_hosts_file, "Azure guard known-hosts file")
        if not known_hosts.read_bytes().strip():
            raise AzureGuardTransportError("Azure guard known-hosts file is empty")
        return AzureGuardSshConfig(
            host=self.host,
            user=self.user,
            identity_file=identity,
            known_hosts_file=known_hosts,
            port=self.port,
            timeout_seconds=self.timeout_seconds,
        )


def _timestamp(value: object, field: str) -> None:
    if not isinstance(value, str):
        raise AzureGuardTransportError(f"guard response {field} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise AzureGuardTransportError(f"guard response {field} is invalid") from error
    if parsed.tzinfo is None:
        raise AzureGuardTransportError(f"guard response {field} must include a timezone")


def _validate_request(command: str, payload: Mapping[str, object]) -> dict[str, object]:
    if command not in _COMMANDS:
        raise AzureGuardTransportError("unsupported Azure guard RPC verb")
    expected = {
        "preflight": frozenset(),
        "arm": frozenset({"run_id", "label", "nonce", "hard_deadline"}),
        "heartbeat": frozenset({"run_id", "label", "nonce", "monotonic_ns"}),
        "status": frozenset({"nonce"}),
        "anchor": frozenset({"nonce", "root_hash"}),
    }[command]
    if frozenset(payload) != expected:
        raise AzureGuardTransportError(f"Azure guard {command} request has unexpected fields")
    request = dict(payload)
    if command != "preflight":
        nonce = request.get("nonce")
        if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
            raise AzureGuardTransportError("Azure guard request nonce is invalid")
    if command in {"arm", "heartbeat"}:
        run_id, label = request.get("run_id"), request.get("label")
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise AzureGuardTransportError("Azure guard request run ID is invalid")
        if not isinstance(label, str) or not _LABEL.fullmatch(label) or not label.endswith(f"--nonce-{request['nonce']}"):
            raise AzureGuardTransportError("Azure guard request label is not nonce-bound")
    if command == "arm":
        _timestamp(request["hard_deadline"], "hard_deadline")
    elif command == "heartbeat":
        monotonic_ns = request["monotonic_ns"]
        if isinstance(monotonic_ns, bool) or not isinstance(monotonic_ns, int) or monotonic_ns < 0:
            raise AzureGuardTransportError("Azure guard heartbeat is invalid")
    elif command == "anchor":
        root_hash = request["root_hash"]
        if not isinstance(root_hash, str) or not _HEX.fullmatch(root_hash):
            raise AzureGuardTransportError("Azure guard anchor root is invalid")
    return {"command": command, "payload": request, "protocol": "srecon26-guard-v1"}


def _validate_response(raw: str) -> dict[str, object]:
    if len(raw.encode("utf-8")) > _MAX_RESPONSE_BYTES:
        raise AzureGuardTransportError("Azure guard response exceeds the size limit")
    try:
        response = json.loads(raw)
    except json.JSONDecodeError as error:
        raise AzureGuardTransportError("Azure guard did not return one JSON response") from error
    if not isinstance(response, dict) or not all(isinstance(key, str) for key in response):
        raise AzureGuardTransportError("Azure guard response must be a JSON object")
    status, root_hash = response.get("status"), response.get("root_hash")
    if not isinstance(status, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", status):
        raise AzureGuardTransportError("Azure guard response status is invalid")
    if not isinstance(root_hash, str) or not _HEX.fullmatch(root_hash):
        raise AzureGuardTransportError("Azure guard response root hash is invalid")
    for field in ("nonce", "label", "host_identity", "script_hash"):
        if field in response and not isinstance(response[field], str):
            raise AzureGuardTransportError(f"Azure guard response {field} is invalid")
    if "nonce" in response and not _NONCE.fullmatch(str(response["nonce"])):
        raise AzureGuardTransportError("Azure guard response nonce is invalid")
    if "label" in response and not _LABEL.fullmatch(str(response["label"])):
        raise AzureGuardTransportError("Azure guard response label is invalid")
    if "script_hash" in response and not _HEX.fullmatch(str(response["script_hash"])):
        raise AzureGuardTransportError("Azure guard response script hash is invalid")
    for field in ("hard_deadline", "last_heartbeat", "teardown_authority_at"):
        if response.get(field) is not None:
            _timestamp(response[field], field)
    observations = response.get("absence_observations")
    if observations is not None:
        if not isinstance(observations, list) or len(observations) > 3:
            raise AzureGuardTransportError("Azure guard absence observations are invalid")
        for value in observations:
            _timestamp(value, "absence_observations")
    return response


class AzureSshGuardTransport(GuardTransport):
    """Call the fixed ``guardctl`` forced command over hardened OpenSSH."""

    def __init__(self, config: AzureGuardSshConfig, *, runner: Runner | None = None) -> None:
        self.config = config.validated()
        self.runner = runner or _run

    def command(self) -> tuple[str, ...]:
        config = self.config
        return (
            "ssh",
            "-F", "/dev/null",
            "-T",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={config.known_hosts_file}",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "IdentitiesOnly=yes",
            "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no",
            "-o", "ForwardAgent=no",
            "-o", "ClearAllForwardings=yes",
            "-o", "PermitLocalCommand=no",
            "-o", "ControlMaster=no",
            "-o", f"ConnectTimeout={config.timeout_seconds}",
            "-i", str(config.identity_file),
            "-p", str(config.port),
            f"{config.user}@{config.host}",
            "guardctl",
        )

    def call(self, command: str, payload: dict[str, object]) -> dict[str, object]:
        request = _validate_request(command, payload)
        serialized = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n"
        try:
            completed = self.runner(self.command(), serialized, self.config.timeout_seconds)
        except (OSError, subprocess.SubprocessError) as error:
            raise AzureGuardTransportError("Azure guard SSH request failed safely") from error
        if completed.returncode != 0:
            raise AzureGuardTransportError("Azure guard forced command refused the request")
        if completed.stderr:
            raise AzureGuardTransportError("Azure guard returned unexpected stderr")
        return _validate_response(completed.stdout)
