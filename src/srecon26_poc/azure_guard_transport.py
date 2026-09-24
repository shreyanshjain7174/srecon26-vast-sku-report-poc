"""Bounded SSH JSON transport for an independently hosted Azure guard.

The SSH key is expected to be restricted server-side to the ``guardctl``
forced command.  This client adds its own fail-closed boundary: a pinned host
key, no forwarding or PTY, one fixed remote command, and one JSON document in
each direction.
"""
from __future__ import annotations

import base64
import json
import hashlib
import os
import re
import selectors
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence
from uuid import UUID

from .guard_client import GuardClientError, GuardTransport


_COMMANDS = frozenset({"preflight", "arm", "heartbeat", "status", "anchor", "export"})
_HEX = re.compile(r"^[0-9a-f]{64}$")
_HOST = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9.-]+(?<!-)$")
_LABEL = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_FINGERPRINT = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
_AZURE_RESOURCE_ID = re.compile(
    r"^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\.Compute/virtualMachines/[^/]+$",
    re.IGNORECASE,
)
_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_STDERR_BYTES = 4 * 1024
_RECEIPT_STATUSES = frozenset(
    {
        "ARMED",
        "AWAITING_INSTANCE",
        "TEARDOWN_AUTHORIZED",
        "TEARDOWN_REQUESTED",
        "TEARDOWN_ERROR",
        "TEARDOWN_RETRIES_EXHAUSTED",
        "ABSENCE_PENDING",
        "ABSENCE_CONFIRMED",
        "OWNERSHIP_MISMATCH",
        "DISARMED",
    }
)


class AzureGuardTransportError(GuardClientError):
    """The bounded Azure guard channel failed closed."""


Runner = Callable[[Sequence[str], str, int], subprocess.CompletedProcess[str]]


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _run(arguments: Sequence[str], request: str, timeout: int) -> subprocess.CompletedProcess[str]:
    # The caller supplies only AzureSshGuardTransport.command(), whose binary,
    # options, destination grammar, and final remote command are allowlisted.
    # shell=False is the subprocess default and no payload enters argv.
    # nosemgrep: python.django.security.injection.command.subprocess-injection.subprocess-injection
    process = subprocess.Popen(
        list(arguments),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    deadline = time.monotonic() + timeout
    try:
        # ``request`` is a small canonical document produced only after the
        # per-verb schemas above validate every field; this writes to the
        # child's pipe, never to a filesystem path.
        # nosemgrep: python.django.security.injection.request-data-write.request-data-write
        process.stdin.write(request.encode("ascii"))
        process.stdin.close()
        selector.register(process.stdout, selectors.EVENT_READ, (stdout, _MAX_RESPONSE_BYTES, "response"))
        selector.register(process.stderr, selectors.EVENT_READ, (stderr, _MAX_STDERR_BYTES, "stderr"))
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(arguments, timeout)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired(arguments, timeout)
            for key, _mask in events:
                target, limit, description = key.data
                chunk = os.read(key.fd, 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target.extend(chunk)
                if len(target) > limit:
                    raise AzureGuardTransportError(f"Azure guard {description} exceeds the size limit")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(arguments, timeout)
        returncode = process.wait(timeout=remaining)
        try:
            decoded_stdout = stdout.decode("utf-8", errors="strict")
            decoded_stderr = stderr.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise AzureGuardTransportError("Azure guard returned non-UTF-8 output") from error
        return subprocess.CompletedProcess(arguments, returncode, decoded_stdout, decoded_stderr)
    except BaseException:
        _stop(process)
        raise
    finally:
        selector.close()


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


def _known_hosts_fingerprint(path: Path) -> str:
    fingerprints: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if fields[0].startswith("@"):
            fields = fields[1:]
        if len(fields) < 3 or not fields[1].startswith(("ssh-", "ecdsa-")):
            raise AzureGuardTransportError("Azure guard known-hosts entry is invalid")
        try:
            key_blob = base64.b64decode(fields[2], validate=True)
        except (ValueError, base64.binascii.Error) as error:
            raise AzureGuardTransportError("Azure guard known-hosts key is invalid") from error
        digest = base64.b64encode(hashlib.sha256(key_blob).digest()).decode("ascii").rstrip("=")
        fingerprints.add(f"SHA256:{digest}")
    if len(fingerprints) != 1:
        raise AzureGuardTransportError("Azure guard known-hosts file must pin exactly one host key")
    return fingerprints.pop()


@dataclass(frozen=True, slots=True)
class AzureGuardSshConfig:
    host: str
    user: str
    identity_file: Path
    known_hosts_file: Path
    port: int = 22
    timeout_seconds: int = 20
    heartbeat_timeout_seconds: int = 120

    def validated(self) -> "AzureGuardSshConfig":
        if not _HOST.fullmatch(self.host) or ".." in self.host:
            raise AzureGuardTransportError("Azure guard host is invalid")
        if not _USER.fullmatch(self.user):
            raise AzureGuardTransportError("Azure guard SSH user is invalid")
        if not 1 <= self.port <= 65535:
            raise AzureGuardTransportError("Azure guard SSH port is invalid")
        if not 1 <= self.timeout_seconds <= 60:
            raise AzureGuardTransportError("Azure guard timeout must be 1-60 seconds")
        if not 1 <= self.heartbeat_timeout_seconds <= 600:
            raise AzureGuardTransportError("Azure guard heartbeat timeout must be 1-600 seconds")
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
            heartbeat_timeout_seconds=self.heartbeat_timeout_seconds,
        )


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise AzureGuardTransportError(f"guard response {field} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise AzureGuardTransportError(f"guard response {field} is invalid") from error
    if parsed.tzinfo is None:
        raise AzureGuardTransportError(f"guard response {field} must include a timezone")
    return parsed.astimezone(UTC)


def _validate_request(command: str, payload: Mapping[str, object]) -> dict[str, object]:
    if command not in _COMMANDS:
        raise AzureGuardTransportError("unsupported Azure guard RPC verb")
    expected = {
        "preflight": frozenset(),
        "arm": frozenset({"run_id", "label", "nonce", "hard_deadline", "heartbeat_timeout_seconds"}),
        "heartbeat": frozenset({"run_id", "label", "nonce", "monotonic_ns"}),
        "status": frozenset({"nonce"}),
        "anchor": frozenset({"nonce", "root_hash"}),
        "export": frozenset({"nonce", "root_hash"}),
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
        timeout_seconds = request["heartbeat_timeout_seconds"]
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 600:
            raise AzureGuardTransportError("Azure guard heartbeat timeout is invalid")
    elif command == "heartbeat":
        monotonic_ns = request["monotonic_ns"]
        if isinstance(monotonic_ns, bool) or not isinstance(monotonic_ns, int) or monotonic_ns < 0:
            raise AzureGuardTransportError("Azure guard heartbeat is invalid")
    elif command in {"anchor", "export"}:
        root_hash = request["root_hash"]
        if not isinstance(root_hash, str) or not _HEX.fullmatch(root_hash):
            raise AzureGuardTransportError("Azure guard anchor root is invalid")
    return {"command": command, "payload": request, "protocol": "srecon26-guard-v1"}


def _required(response: Mapping[str, object], fields: frozenset[str], command: str) -> None:
    missing = fields - response.keys()
    if missing:
        raise AzureGuardTransportError(f"Azure guard {command} response lacks required fields")


def _same_timestamp(left: object, right: object, field: str) -> bool:
    return _timestamp(left, field) == _timestamp(right, field)


def _validate_absence(response: Mapping[str, object]) -> None:
    observations = response.get("absence_observations")
    if observations is None:
        if response["status"] == "ABSENCE_CONFIRMED":
            raise AzureGuardTransportError("absence confirmation lacks three observations")
        return
    if not isinstance(observations, list) or len(observations) > 3:
        raise AzureGuardTransportError("Azure guard absence observations are invalid")
    parsed = [_timestamp(value, "absence_observations") for value in observations]
    if len(set(parsed)) != len(parsed) or parsed != sorted(parsed):
        raise AzureGuardTransportError("Azure guard absence observations must be distinct and ordered")
    if response["status"] == "ABSENCE_CONFIRMED" and len(parsed) != 3:
        raise AzureGuardTransportError("absence confirmation requires three distinct observations")
    if response["status"] == "ABSENCE_CONFIRMED":
        authority = _timestamp(response.get("teardown_authority_at"), "teardown_authority_at")
        if any(observed <= authority for observed in parsed):
            raise AzureGuardTransportError("absence observations must follow teardown authority")


def _validate_response(
    command: str,
    payload: Mapping[str, object],
    raw: str,
    arm_binding: Mapping[str, object] | None,
) -> dict[str, object]:
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
    for field in (
        "nonce", "label", "host_identity", "script_hash", "azure_resource_id",
        "azure_vm_id", "host_key_fingerprint",
    ):
        if field in response and not isinstance(response[field], str):
            raise AzureGuardTransportError(f"Azure guard response {field} is invalid")
    if "nonce" in response and not _NONCE.fullmatch(str(response["nonce"])):
        raise AzureGuardTransportError("Azure guard response nonce is invalid")
    if "label" in response and not _LABEL.fullmatch(str(response["label"])):
        raise AzureGuardTransportError("Azure guard response label is invalid")
    if "label" in response and "nonce" in response and not str(response["label"]).endswith(f"--nonce-{response['nonce']}"):
        raise AzureGuardTransportError("Azure guard response label is not nonce-bound")
    if "script_hash" in response and not _HEX.fullmatch(str(response["script_hash"])):
        raise AzureGuardTransportError("Azure guard response script hash is invalid")
    if "heartbeat_timeout_seconds" in response:
        heartbeat_timeout = response["heartbeat_timeout_seconds"]
        if isinstance(heartbeat_timeout, bool) or not isinstance(heartbeat_timeout, int) or not 1 <= heartbeat_timeout <= 600:
            raise AzureGuardTransportError("Azure guard response heartbeat timeout is invalid")
    if "azure_resource_id" in response and not _AZURE_RESOURCE_ID.fullmatch(str(response["azure_resource_id"])):
        raise AzureGuardTransportError("Azure guard response Azure resource ID is invalid")
    if "azure_vm_id" in response:
        try:
            UUID(str(response["azure_vm_id"]))
        except ValueError as error:
            raise AzureGuardTransportError("Azure guard response Azure VM ID is invalid") from error
    if "host_key_fingerprint" in response and not _FINGERPRINT.fullmatch(str(response["host_key_fingerprint"])):
        raise AzureGuardTransportError("Azure guard response host-key fingerprint is invalid")
    for field in ("hard_deadline", "last_heartbeat", "teardown_authority_at"):
        if response.get(field) is not None:
            _timestamp(response[field], field)
    _validate_absence(response)

    binding = payload if command == "arm" else arm_binding
    if command == "arm":
        _required(response, frozenset({"status", "root_hash", "nonce", "label", "hard_deadline", "heartbeat_timeout_seconds"}), command)
        if status != "ARMED":
            raise AzureGuardTransportError("Azure guard arm did not return ARMED")
        if response["nonce"] != payload["nonce"] or response["label"] != payload["label"]:
            raise AzureGuardTransportError("Azure guard arm response changed ownership")
        if not _same_timestamp(response["hard_deadline"], payload["hard_deadline"], "hard_deadline"):
            raise AzureGuardTransportError("Azure guard arm response changed the deadline")
        if response["heartbeat_timeout_seconds"] != payload["heartbeat_timeout_seconds"]:
            raise AzureGuardTransportError("Azure guard arm response changed the heartbeat timeout")
    elif command == "preflight":
        if binding is None:
            raise AzureGuardTransportError("Azure guard preflight requires a validated arm receipt")
        _required(
            response,
            frozenset({
                "status", "root_hash", "nonce", "label", "hard_deadline", "host_identity", "script_hash",
                "azure_resource_id", "azure_vm_id", "host_key_fingerprint", "heartbeat_timeout_seconds",
            }),
            command,
        )
        if status != "ARMED" or response["nonce"] != binding["nonce"] or response["label"] != binding["label"]:
            raise AzureGuardTransportError("Azure guard preflight is not bound to the validated arm receipt")
        if not _same_timestamp(response["hard_deadline"], binding["hard_deadline"], "hard_deadline"):
            raise AzureGuardTransportError("Azure guard preflight changed the deadline")
        if not response["host_identity"]:
            raise AzureGuardTransportError("Azure guard preflight lacks the remote host identity")
        if response["heartbeat_timeout_seconds"] != binding["heartbeat_timeout_seconds"]:
            raise AzureGuardTransportError("Azure guard preflight changed the heartbeat timeout")
    elif command == "heartbeat":
        if binding is None:
            raise AzureGuardTransportError("Azure guard heartbeat requires a validated arm receipt")
        _required(response, frozenset({"status", "root_hash", "nonce", "label", "hard_deadline"}), command)
        if status not in {"ARMED", "AWAITING_INSTANCE"}:
            raise AzureGuardTransportError("Azure guard heartbeat returned a non-heartbeatable status")
        if payload["nonce"] != binding["nonce"] or payload["label"] != binding["label"]:
            raise AzureGuardTransportError("Azure guard heartbeat request differs from the validated arm receipt")
        if response["nonce"] != binding["nonce"] or response["label"] != binding["label"]:
            raise AzureGuardTransportError("Azure guard heartbeat response differs from the validated arm receipt")
        if not _same_timestamp(response["hard_deadline"], binding["hard_deadline"], "hard_deadline"):
            raise AzureGuardTransportError("Azure guard heartbeat changed the deadline")
    elif command == "status":
        if binding is None:
            raise AzureGuardTransportError("Azure guard status requires a validated arm receipt")
        _required(response, frozenset({"status", "root_hash", "nonce", "label", "hard_deadline"}), command)
        if payload["nonce"] != binding["nonce"]:
            raise AzureGuardTransportError("Azure guard status request differs from the validated arm receipt")
        if (
            status not in _RECEIPT_STATUSES
            or response["nonce"] != binding["nonce"]
            or response["label"] != binding["label"]
            or not _same_timestamp(response["hard_deadline"], binding["hard_deadline"], "hard_deadline")
        ):
            raise AzureGuardTransportError("Azure guard status response differs from the validated arm receipt")
    elif command == "anchor":
        if binding is None:
            raise AzureGuardTransportError("Azure guard anchor requires a validated arm receipt")
        _required(
            response,
            frozenset({"status", "root_hash", "nonce", "label", "hard_deadline", "anchored_root_hash"}),
            command,
        )
        if (
            status not in _RECEIPT_STATUSES
            or payload["nonce"] != binding["nonce"]
            or response["nonce"] != binding["nonce"]
            or response["label"] != binding["label"]
            or not _same_timestamp(response["hard_deadline"], binding["hard_deadline"], "hard_deadline")
            or response["anchored_root_hash"] != payload["root_hash"]
        ):
            raise AzureGuardTransportError("Azure guard anchor response differs from the validated arm receipt")
    else:
        _required(
            response,
            frozenset({"status", "root_hash", "nonce", "journal", "journal_sha256", "journal_encoding"}),
            command,
        )
        journal = response["journal"]
        if status != "EVIDENCE_EXPORTED" or response["nonce"] != payload["nonce"] or response["root_hash"] != payload["root_hash"]:
            raise AzureGuardTransportError("Azure guard evidence export is not bound to the request")
        if response["journal_encoding"] != "utf-8-jsonl" or not isinstance(journal, str) or not journal.endswith("\n"):
            raise AzureGuardTransportError("Azure guard evidence export has an invalid journal encoding")
        journal_hash = response["journal_sha256"]
        if not isinstance(journal_hash, str) or not _HEX.fullmatch(journal_hash):
            raise AzureGuardTransportError("Azure guard evidence export has an invalid file hash")
        if hashlib.sha256(journal.encode("utf-8")).hexdigest() != journal_hash:
            raise AzureGuardTransportError("Azure guard evidence export failed hash verification")
    return response


@dataclass(frozen=True, slots=True)
class GuardEvidenceExport:
    nonce: str
    root_hash: str
    journal: str
    journal_sha256: str


class AzureSshGuardTransport(GuardTransport):
    """Call the fixed ``guardctl`` forced command over hardened OpenSSH."""

    def __init__(self, config: AzureGuardSshConfig, *, runner: Runner | None = None) -> None:
        self.config = config.validated()
        self.host_key_fingerprint = _known_hosts_fingerprint(self.config.known_hosts_file)
        self.runner = runner or _run
        self._arm_binding: dict[str, object] | None = None

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
        if command == "arm" and payload.get("heartbeat_timeout_seconds") != self.config.heartbeat_timeout_seconds:
            raise AzureGuardTransportError("Azure guard arm request differs from the configured heartbeat timeout")
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
        response = _validate_response(command, payload, completed.stdout, self._arm_binding)
        if command == "arm":
            self._arm_binding = {
                "nonce": response["nonce"],
                "label": response["label"],
                "hard_deadline": response["hard_deadline"],
                "heartbeat_timeout_seconds": response["heartbeat_timeout_seconds"],
            }
        elif command == "preflight" and response["host_key_fingerprint"] != self.host_key_fingerprint:
            raise AzureGuardTransportError("Azure guard attested a different pinned host-key fingerprint")
        return response

    def export_evidence(self, *, nonce: str, root_hash: str) -> GuardEvidenceExport:
        response = self.call("export", {"nonce": nonce, "root_hash": root_hash})
        return GuardEvidenceExport(
            nonce=str(response["nonce"]),
            root_hash=str(response["root_hash"]),
            journal=str(response["journal"]),
            journal_sha256=str(response["journal_sha256"]),
        )
