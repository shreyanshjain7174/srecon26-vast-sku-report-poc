"""Opt-in production integrations for the bounded live canary.

Nothing in this module is used by the fixture path.  ``create_dispatcher`` is
only loaded by ``run_canary.py --live --dispatcher-factory`` and deliberately
requires every credential-adjacent or network endpoint setting to be supplied
by the operator.  It never creates an instance itself; the dispatcher remains
the sole owner of the one-create/exact-teardown lifecycle.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from .budget import ExposureLedger
from .canary import CanarySnapshot, KvmFacts, MetricSample
from .contracts import InstanceContract, ProbeOutcome
from .guard import GuardAttestation
from .guard_client import GuardClient, GuardClientError, GuardRemoteReceipt, GuardTransport
from .live_dispatch import (
    REPORT_MARGIN,
    InferenceMeasurementEvidence,
    LiveCanaryDispatcher,
    LiveDispatchError,
    LiveEvidence,
    WorkloadContract,
)
from .reporting import ReportGate
from .types import RunIdentity
from .vast_provider import VastCliProvider


_NONCE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ARMED = re.compile(
    r"^SRECON26_GUARD_V1 ARMED nonce=(?P<nonce>[A-Za-z0-9_-]{8,128}) "
    r"label=(?P<label>[A-Za-z0-9_.-]{1,128}) deadline=(?P<deadline>[^ ]+) "
    r"host=(?P<host>[A-Za-z0-9_.-]{1,128}) script=(?P<script>[0-9a-f]{64}) "
    r"root=(?P<root>[0-9a-f]{64})$"
)
_BACKSTOP_ARMED = re.compile(
    r"^SRECON26_GUARD_V1 BACKSTOP_ARMED nonce=(?P<nonce>[A-Za-z0-9_-]{8,128}) "
    r"label=(?P<label>[A-Za-z0-9_.-]{1,128}) deadline=(?P<deadline>[^ ]+) "
    r"host=(?P<host>[A-Za-z0-9_.-]{1,128}) script=(?P<script>[0-9a-f]{64}) "
    r"root=(?P<root>[0-9a-f]{64})$"
)
_ANCHORED = re.compile(
    r"^SRECON26_GUARD_V1 ANCHORED nonce=(?P<nonce>[A-Za-z0-9_-]{8,128}) "
    r"root=(?P<root>[0-9a-f]{64})$"
)


class LiveFactoryError(LiveDispatchError):
    """An operator-owned live integration is incomplete or unsafe."""


class CommandRunner(Protocol):
    def __call__(self, arguments: Sequence[str], *, timeout: int) -> str: ...


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise LiveFactoryError("timestamps must include a timezone")
    return value.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    try:
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError as error:
        raise LiveFactoryError("GitHub guard receipt contains an invalid deadline") from error


def _run(arguments: Sequence[str], *, timeout: int) -> str:
    try:
        return subprocess.run(list(arguments), check=True, text=True, capture_output=True, timeout=timeout).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise LiveFactoryError(f"command failed safely: {Path(arguments[0]).name}") from error


def _json(value: str, *, context: str) -> object:
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise LiveFactoryError(f"{context} did not return JSON") from error


@dataclass(frozen=True, slots=True)
class GitHubGuardConfig:
    repository: str
    ref: str
    issue_number: int
    trusted_author: str
    heartbeat_seconds: int = 120
    workflow: str = "independent-guard.yml"
    workflow_actor: str = "github-actions[bot]"
    arm_timeout_seconds: int = 120
    dispatch_on_arm: bool = True

    def validate(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise LiveFactoryError("GitHub repository must be owner/name")
        if not self.ref or any(character.isspace() for character in self.ref):
            raise LiveFactoryError("GitHub ref is required")
        if self.issue_number <= 0:
            raise LiveFactoryError("GitHub guard issue number must be positive")
        if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", self.trusted_author):
            raise LiveFactoryError("GitHub trusted author is invalid")
        if not 30 <= self.heartbeat_seconds <= 600:
            raise LiveFactoryError("GitHub heartbeat interval must be 30-600 seconds")
        if not 30 <= self.arm_timeout_seconds <= 300:
            raise LiveFactoryError("GitHub arm verification timeout must be 30-300 seconds")


class GitHubGuardTransport(GuardTransport):
    """Guard transport backed by an independently-run GitHub Action.

    The Action writes a strict attestation comment after its worker has armed.
    We dispatch once and refuse to continue unless that comment binds the
    nonce, label, immutable deadline, runner identity, worker hash, and guard
    journal root.  Heartbeats and bundle anchors are owner-authored issue
    comments consumed by the independent Action, not local guard calls.
    """

    def __init__(self, config: GitHubGuardConfig, *, runner: CommandRunner | None = None, sleep: Callable[[float], None] = time.sleep, now: Callable[[], datetime] | None = None) -> None:
        config.validate()
        self.config = config
        self.runner = runner or _run
        self.sleep = sleep
        self.now = now or (lambda: datetime.now(UTC))
        self._armed: GuardRemoteReceipt | None = None
        self._nonce: str | None = None
        self._last_heartbeat_ns: int | None = None

    def _api(self, method: str, endpoint: str, *, fields: Mapping[str, str] | None = None, paginate: bool = False) -> object:
        arguments = ["gh", "api", "--method", method, endpoint]
        if paginate:
            arguments.append("--paginate")
        for key, value in sorted((fields or {}).items()):
            arguments.extend(("-f", f"{key}={value}"))
        return _json(self.runner(arguments, timeout=20), context="GitHub API") if method != "POST" or endpoint.endswith("comments") else self.runner(arguments, timeout=20)

    def _comments(self, *, since: datetime) -> list[Mapping[str, object]]:
        # The arm receipt is necessarily one of the newest comments.  Avoid
        # ``gh --paginate`` here because its multiple JSON documents are not a
        # single trustworthy parse unit.
        raw = self._api(
            "GET",
            f"repos/{self.config.repository}/issues/{self.config.issue_number}/comments?per_page=100&since={_stamp(since)}",
        )
        if not isinstance(raw, list) or not all(isinstance(item, Mapping) for item in raw):
            raise LiveFactoryError("GitHub issue comments have an unexpected response")
        return list(raw)

    def _dispatch(self, *, nonce: str, label: str, deadline: datetime) -> None:
        # A workflow-dispatch response is intentionally empty (204), so its
        # only trusted completion signal is the Action's later signed-shaped
        # attestation comment.
        arguments = [
            "gh", "api", "--method", "POST",
            f"repos/{self.config.repository}/actions/workflows/{self.config.workflow}/dispatches",
            "-f", f"ref={self.config.ref}",
            "-f", f"inputs[nonce]={nonce}", "-f", f"inputs[label]={label}",
            "-f", f"inputs[issue_number]={self.config.issue_number}",
            "-f", f"inputs[hard_deadline]={_stamp(deadline)}",
            "-f", f"inputs[trusted_author]={self.config.trusted_author}",
            "-f", f"inputs[heartbeat_seconds]={self.config.heartbeat_seconds}",
            "-f", "inputs[preflight_only]=false",
        ]
        self.runner(arguments, timeout=20)

    def _find_attestation(self, *, nonce: str, label: str, deadline: datetime, dispatched_at: datetime) -> GuardRemoteReceipt | None:
        primary: tuple[re.Match[str], datetime] | None = None
        backstop: tuple[re.Match[str], datetime] | None = None
        for comment in self._comments(since=dispatched_at):
            body = comment.get("body")
            created = comment.get("created_at")
            user = comment.get("user")
            actor = user.get("login") if isinstance(user, Mapping) else None
            if actor != self.config.workflow_actor or not isinstance(body, str) or not isinstance(created, str):
                continue
            created_at = _parse_time(created)
            match = _ARMED.fullmatch(body)
            backstop_match = _BACKSTOP_ARMED.fullmatch(body)
            selected = match or backstop_match
            if selected is None or created_at < dispatched_at:
                continue
            if selected.group("nonce") != nonce or selected.group("label") != label or _parse_time(selected.group("deadline")) != deadline:
                continue
            if match is not None:
                primary = (match, created_at)
            else:
                assert backstop_match is not None
                backstop = (backstop_match, created_at)
        if primary is None or backstop is None:
            return None
        primary_match, primary_at = primary
        backstop_match, backstop_at = backstop
        combined_script = hashlib.sha256(f"{primary_match.group('script')}:{backstop_match.group('script')}".encode()).hexdigest()
        return GuardRemoteReceipt(
            status="ARMED",
            root_hash=primary_match.group("root"),
            nonce=nonce,
            label=label,
            hard_deadline=deadline,
            last_heartbeat=max(primary_at, backstop_at),
            host_identity=f"{primary_match.group('host')}+{backstop_match.group('host')}",
            script_hash=combined_script,
        )

    def _comment(self, body: str) -> None:
        self._api("POST", f"repos/{self.config.repository}/issues/{self.config.issue_number}/comments", fields={"body": body})

    def call(self, command: str, payload: dict[str, object]) -> dict[str, object]:
        if command == "arm":
            nonce, label = str(payload.get("nonce", "")), str(payload.get("label", ""))
            deadline = _parse_time(payload.get("hard_deadline", ""))
            if not _NONCE.fullmatch(nonce) or not label.endswith(f"--nonce-{nonce}"):
                raise LiveFactoryError("GitHub guard arm request has invalid nonce-bound ownership")
            started_at = _utc(self.now())
            dispatched_at = started_at if self.config.dispatch_on_arm else datetime(1970, 1, 1, tzinfo=UTC)
            if self.config.dispatch_on_arm:
                self._dispatch(nonce=nonce, label=label, deadline=deadline)
            until = min(deadline, started_at + timedelta(seconds=self.config.arm_timeout_seconds))
            while _utc(self.now()) < until:
                receipt = self._find_attestation(nonce=nonce, label=label, deadline=deadline, dispatched_at=dispatched_at)
                if receipt is not None:
                    self._armed, self._nonce = receipt, nonce
                    return _receipt_mapping(receipt)
                self.sleep(2)
            raise LiveFactoryError("independent GitHub guard did not publish a bound arm attestation")
        if self._armed is None or self._nonce is None:
            raise LiveFactoryError("GitHub guard is not armed")
        if command == "preflight":
            return _receipt_mapping(self._armed)
        if command == "heartbeat":
            if payload.get("nonce") != self._nonce or payload.get("label") != self._armed.label:
                raise LiveFactoryError("heartbeat ownership differs from armed GitHub guard")
            monotonic_ns = _integer(payload.get("monotonic_ns"), "monotonic heartbeat", minimum=0)
            if self._last_heartbeat_ns is not None:
                if monotonic_ns < self._last_heartbeat_ns:
                    raise LiveFactoryError("monotonic heartbeat moved backwards")
                minimum_interval_ns = self.config.heartbeat_seconds * 1_000_000_000 // 2
                if monotonic_ns - self._last_heartbeat_ns < minimum_interval_ns:
                    return _receipt_mapping(self._armed)
            self._comment(f"SRECON26_GUARD_V1 HEARTBEAT nonce={self._nonce} root={self._armed.root_hash}")
            self._last_heartbeat_ns = monotonic_ns
            return _receipt_mapping(self._armed)
        if command == "anchor":
            root = str(payload.get("root_hash", ""))
            if not _SHA256.fullmatch(root):
                raise LiveFactoryError("guard anchor must be a SHA-256 root")
            submitted_at = _utc(self.now())
            self._comment(f"SRECON26_GUARD_V1 ANCHOR nonce={self._nonce} root={root}")
            for _attempt in range(max(1, self.config.arm_timeout_seconds // 2)):
                for comment in self._comments(since=submitted_at):
                    body = comment.get("body")
                    created = comment.get("created_at")
                    user = comment.get("user")
                    actor = user.get("login") if isinstance(user, Mapping) else None
                    match = _ANCHORED.fullmatch(body) if isinstance(body, str) else None
                    if actor != self.config.workflow_actor or match is None or not isinstance(created, str):
                        continue
                    if _parse_time(created) < submitted_at:
                        continue
                    if match.group("nonce") == self._nonce and match.group("root") == root:
                        return {"status": "ANCHORED", "root_hash": root, "nonce": self._nonce, "label": self._armed.label}
                self.sleep(2)
            raise LiveFactoryError("independent GitHub guard did not acknowledge the bundle anchor")
        raise LiveFactoryError("unsupported GitHub guard command")


class DynamicGitHubGuard:
    """Bind a fresh GitHub transport to the request nonce at arm time.

    A factory is constructed before command-line request validation finishes,
    so keeping the nonce in process environment would be a needless second
    source of truth.  This wrapper makes the dispatcher request authoritative.
    """

    def __init__(self, config: GitHubGuardConfig) -> None:
        self.config = config
        self.client: GuardClient | None = None

    def arm(self, identity: RunIdentity, hard_deadline: datetime) -> GuardAttestation:
        suffix = "--nonce-"
        if suffix not in identity.label:
            raise LiveFactoryError("run label lacks a nonce-bound suffix")
        nonce = identity.label.rsplit(suffix, 1)[1]
        self.client = GuardClient(GitHubGuardTransport(self.config), nonce=nonce)
        self.client.arm(identity, hard_deadline)
        return self.client.preflight()

    def preflight(self) -> GuardAttestation:
        if self.client is None:
            raise LiveFactoryError("GitHub guard is not armed")
        return self.client.preflight()

    def record_heartbeat(self, identity: RunIdentity, monotonic_ns: int) -> None:
        if self.client is None:
            raise LiveFactoryError("GitHub guard is not armed")
        self.client.record_heartbeat(identity, monotonic_ns)

    def anchor(self, root_hash: str) -> str:
        if self.client is None:
            raise LiveFactoryError("GitHub guard is not armed")
        return self.client.anchor(root_hash)


def _receipt_mapping(receipt: GuardRemoteReceipt) -> dict[str, object]:
    return {
        "status": receipt.status,
        "root_hash": receipt.root_hash,
        "nonce": receipt.nonce,
        "label": receipt.label,
        "hard_deadline": _stamp(receipt.hard_deadline) if receipt.hard_deadline else None,
        "last_heartbeat": _stamp(receipt.last_heartbeat) if receipt.last_heartbeat else None,
        "host_identity": receipt.host_identity,
        "script_hash": receipt.script_hash,
    }


@dataclass(frozen=True, slots=True)
class SshEndpoint:
    host: str
    port: int

    def destination(self, user: str) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{user}@{host}"


class VastSshResolver:
    """Resolve SSH only from the exact created instance's current record."""

    _CLI_TIMEOUT_SECONDS = 20
    _CLI_DEADLINE_OVERHEAD_SECONDS = 2

    def __init__(self, cli_path: Path | str, *, runner: CommandRunner | None = None, attempts: int = 60, interval_seconds: float = 5.0, sleep: Callable[[float], None] = time.sleep) -> None:
        if not 1 <= attempts <= 90 or not 0 <= interval_seconds <= 15:
            raise ValueError("SSH resolution retry bounds are invalid")
        self.cli_path, self.runner = str(cli_path), runner or _run
        self.attempts, self.interval_seconds, self.sleep = attempts, interval_seconds, sleep

    @classmethod
    def _require_cli_window(cls, hard_deadline: datetime | None, operation: str) -> int:
        if hard_deadline is None:
            return cls._CLI_TIMEOUT_SECONDS
        remaining = (hard_deadline - REPORT_MARGIN - datetime.now(UTC)).total_seconds()
        required = cls._CLI_TIMEOUT_SECONDS + cls._CLI_DEADLINE_OVERHEAD_SECONDS
        if remaining < required:
            raise LiveFactoryError(f"{operation} cannot fit before immutable teardown margin")
        return cls._CLI_TIMEOUT_SECONDS

    @staticmethod
    def _require_remaining_margin(hard_deadline: datetime | None, operation: str) -> None:
        if hard_deadline is not None and datetime.now(UTC) >= hard_deadline - REPORT_MARGIN:
            raise LiveFactoryError(f"{operation} reached immutable teardown margin")

    @staticmethod
    def _endpoint_candidates(raw: Mapping[str, object]) -> tuple[tuple[str, int] | None, tuple[str, int] | None, str]:
        """Return the direct and proxy candidates without inferring either route.

        Vast exposes two materially different SSH paths.  A direct connection
        is usable only when the instance's public address is paired with the
        explicit Docker-style ``22/tcp`` host-port mapping.  ``ssh_host`` and
        ``ssh_port`` are instead the provider's proxy route.  In particular,
        a proxy port must never be paired with ``public_ipaddr``.
        """

        direct_host = _normalized_host(raw.get("public_ipaddr"))
        direct_port = _host_port(raw.get("ports"))
        proxy_host = _normalized_host(raw.get("ssh_host"))
        proxy_port = _normalized_port(raw.get("ssh_port"))

        direct = (direct_host, direct_port) if direct_host is not None and direct_port is not None else None
        proxy = (proxy_host, proxy_port) if proxy_host is not None and proxy_port is not None else None
        if direct is not None:
            return direct, proxy, "direct_public_ipaddr_22_tcp_hostport"
        if proxy is not None:
            return direct, proxy, "proxy_ssh_host_ssh_port"
        return direct, proxy, "no_safe_endpoint_candidate"

    @staticmethod
    def _endpoint_evidence(raw: Mapping[str, object], direct: tuple[str, int] | None, proxy: tuple[str, int] | None, *, selected_route: str | None, selection_reason: str) -> dict[str, object]:
        """Make route selection auditable without retaining raw provider fields."""

        direct_host = _normalized_host(raw.get("public_ipaddr"))
        direct_port = _host_port(raw.get("ports"))
        proxy_host = _normalized_host(raw.get("ssh_host"))
        proxy_port = _normalized_port(raw.get("ssh_port"))
        return {
            "direct_endpoint_host": direct_host,
            "direct_endpoint_port": direct_port,
            "proxy_endpoint_host": proxy_host,
            "proxy_endpoint_port": proxy_port,
            "direct_endpoint_candidate": (
                {"host": direct[0], "port": direct[1]} if direct is not None else None
            ),
            "proxy_endpoint_candidate": (
                {"host": proxy[0], "port": proxy[1]} if proxy is not None else None
            ),
            "selected_ssh_route": selected_route,
            "ssh_route_selection_reason": selection_reason,
        }

    def resolve(self, instance: InstanceContract, *, hard_deadline: datetime | None = None, heartbeat: Callable[[], None] | None = None, status_log: Path | None = None) -> SshEndpoint:
        terminal = {"error", "offline", "stopped", "exited"}
        for attempt in range(self.attempts):
            timeout = self._require_cli_window(hard_deadline, "provider status lookup")
            if heartbeat is not None:
                heartbeat()
            try:
                raw = _json(self.runner([self.cli_path, "--raw", "--no-color", "show", "instance", str(instance.instance_id)], timeout=timeout), context="Vast instance lookup")
            except LiveFactoryError:
                self._require_remaining_margin(hard_deadline, "provider status lookup")
                raw = None
            self._require_remaining_margin(hard_deadline, "provider status lookup")
            if isinstance(raw, Mapping):
                current_id = _integer(raw.get("id", raw.get("instance_id")), "instance id")
                label = raw.get("label")
                if current_id != instance.instance_id or label != instance.label:
                    raise LiveFactoryError("exact instance SSH lookup changed ID or nonce-bound label")
                direct, proxy, selection_reason = self._endpoint_candidates(raw)
                selected = direct or proxy
                selected_route = "direct" if direct is not None else "proxy" if proxy is not None else None
                status = str(raw.get("actual_status", "")).lower()
                if status_log is not None:
                    status_log.parent.mkdir(parents=True, exist_ok=True)
                    with status_log.open("a", encoding="utf-8") as handle:
                        evidence = {
                            "attempt": attempt + 1,
                            "instance_id": current_id,
                            "label": label,
                            "actual_status": status,
                            "endpoint_published": selected is not None,
                            "observed_at": _stamp(datetime.now(UTC)),
                        }
                        evidence.update(
                            self._endpoint_evidence(
                                raw,
                                direct,
                                proxy,
                                selected_route=selected_route,
                                selection_reason=selection_reason,
                            )
                        )
                        handle.write(json.dumps(evidence, sort_keys=True) + "\n")
                if status in terminal:
                    raise LiveFactoryError(f"exact instance entered terminal provider status: {status}")
                if status == "running" and selected is not None:
                    return SshEndpoint(*selected)
            if attempt + 1 < self.attempts:
                if heartbeat is not None:
                    heartbeat()
                if hard_deadline is not None and (hard_deadline - REPORT_MARGIN - datetime.now(UTC)).total_seconds() <= self.interval_seconds:
                    raise LiveFactoryError("provider status retry cannot fit before immutable teardown margin")
                self.sleep(self.interval_seconds)
        raise LiveFactoryError("exact instance did not reach running with a safe SSH endpoint within the bounded wait")

    def attach_public_key(self, instance: InstanceContract, public_key_file: Path, *, hard_deadline: datetime, heartbeat: Callable[[], None], status_log: Path) -> None:
        """Attach the approved public key only after rechecking exact ownership."""

        if not public_key_file.is_file() or public_key_file.is_symlink():
            raise LiveFactoryError("SSH public key must be a regular file")
        key = public_key_file.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"ssh-(?:rsa|ed25519) [A-Za-z0-9+/=]+(?: [^\r\n]+)?", key):
            raise LiveFactoryError("SSH public key file is invalid")
        terminal = {"error", "offline", "stopped", "exited"}
        for attempt in range(self.attempts):
            lookup_timeout = self._require_cli_window(hard_deadline, "SSH key ownership lookup")
            heartbeat()
            raw = _json(self.runner([self.cli_path, "--raw", "--no-color", "show", "instance", str(instance.instance_id)], timeout=lookup_timeout), context="Vast instance ownership lookup")
            self._require_remaining_margin(hard_deadline, "SSH key ownership lookup")
            if not isinstance(raw, Mapping) or _integer(raw.get("id", raw.get("instance_id")), "instance id") != instance.instance_id or raw.get("label") != instance.label:
                raise LiveFactoryError("refusing to attach SSH key after instance ownership changed")
            status = str(raw.get("actual_status", "")).lower()
            status_log.parent.mkdir(parents=True, exist_ok=True)
            with status_log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"event": "ssh_key_attach_wait", "attempt": attempt + 1, "instance_id": instance.instance_id, "label": instance.label, "actual_status": status, "observed_at": _stamp(datetime.now(UTC))}, sort_keys=True) + "\n")
            if status in terminal:
                raise LiveFactoryError(f"refusing to attach SSH key after terminal provider status: {status}")
            if status in {"loading", "running"}:
                break
            if attempt + 1 < self.attempts:
                heartbeat()
                if (hard_deadline - REPORT_MARGIN - datetime.now(UTC)).total_seconds() <= self.interval_seconds:
                    raise LiveFactoryError("SSH key attach retry cannot fit before immutable teardown margin")
                self.sleep(self.interval_seconds)
        else:
            raise LiveFactoryError("exact instance did not reach a safe SSH key attach status within the bounded wait")
        attach_timeout = self._require_cli_window(hard_deadline, "SSH key attach")
        self.runner([self.cli_path, "--raw", "--no-color", "attach", "ssh", str(instance.instance_id), str(public_key_file)], timeout=attach_timeout)
        self._require_remaining_margin(hard_deadline, "SSH key attach")
        heartbeat()


def _integer(value: object, field: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise LiveFactoryError(f"invalid {field}") from error
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        raise LiveFactoryError(f"invalid {field}")
    number = int(parsed)
    if (minimum is not None and number < minimum) or (maximum is not None and number > maximum):
        raise LiveFactoryError(f"invalid {field}")
    return number


def _safe_host(host: str) -> bool:
    if not host or any(character.isspace() for character in host):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return bool(_HOST.fullmatch(host)) and host not in {"localhost", "localhost.localdomain"}
    return not (address.is_loopback or address.is_unspecified or address.is_multicast)


def _normalized_host(value: object) -> str | None:
    """Return a safe canonical host, or reject an untrusted provider field."""

    if not isinstance(value, str) or not _safe_host(value):
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return value.lower()


def _normalized_port(value: object) -> int | None:
    try:
        return _integer(value, "SSH port", minimum=1, maximum=65535)
    except LiveFactoryError:
        return None


def _host_port(value: object) -> int | None:
    """Extract only the explicit first published host port for container SSH."""

    if not isinstance(value, Mapping):
        return None
    mappings = value.get("22/tcp")
    if not isinstance(mappings, Sequence) or isinstance(mappings, (str, bytes)) or not mappings:
        return None
    first = mappings[0]
    if not isinstance(first, Mapping):
        return None
    return _normalized_port(first.get("HostPort"))


@dataclass(frozen=True, slots=True)
class SshWorkloadConfig:
    user: str
    identity_file: Path
    public_key_file: Path
    known_hosts_file: Path
    k3s_binary: Path
    nvidia_runtime_template: Path
    local_script: Path
    local_manifest_dir: Path
    heartbeat_seconds: int = 120

    def validate(self) -> None:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,31}", self.user):
            raise LiveFactoryError("SSH user is invalid")
        for label, path, executable in (
            ("SSH identity", self.identity_file, False), ("SSH public key", self.public_key_file, False), ("SSH known-hosts", self.known_hosts_file, False),
            ("k3s binary", self.k3s_binary, True), ("NVIDIA runtime template", self.nvidia_runtime_template, False),
            ("remote canary script", self.local_script, True),
        ):
            if not path.is_file() or path.is_symlink() or (executable and not os.access(path, os.X_OK)):
                raise LiveFactoryError(f"{label} must be a regular {'executable ' if executable else ''}file")
        if not self.local_manifest_dir.is_dir() or self.local_manifest_dir.is_symlink():
            raise LiveFactoryError("remote manifest directory must be a real directory")
        required = {"namespace.yaml", "vllm.yaml", "device-plugin.yaml"}
        if not required.issubset({item.name for item in self.local_manifest_dir.glob("*.yaml")}):
            raise LiveFactoryError("remote manifest directory is incomplete")
        if not 30 <= self.heartbeat_seconds <= 600:
            raise LiveFactoryError("SSH heartbeat interval must be 30-600 seconds")


class SshRemoteWorkload:
    """Stages and drives the deliberately bounded remote host script over SSH."""

    def __init__(self, resolver: VastSshResolver, config: SshWorkloadConfig, *, popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen) -> None:
        config.validate()
        self.resolver, self.config, self.popen = resolver, config, popen

    def _ssh_prefix(self, endpoint: SshEndpoint) -> list[str]:
        return ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={self.config.known_hosts_file}", "-o", "ConnectTimeout=20", "-i", str(self.config.identity_file), "-p", str(endpoint.port), endpoint.destination(self.config.user)]

    def _deadline_seconds(self, hard_deadline: datetime) -> int:
        # Never let workload work consume the existing report+teardown margin.
        remaining = int((hard_deadline - REPORT_MARGIN - datetime.now(UTC)).total_seconds())
        if remaining <= 0:
            raise LiveFactoryError("remote workload reached immutable teardown margin")
        return min(remaining, 1800)

    def _stream(self, arguments: Sequence[str], *, hard_deadline: datetime, heartbeat: Callable[[], None], log: Path) -> None:
        heartbeat()
        process = self.popen(list(arguments), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        chunks: list[str] = []
        next_heartbeat = time.monotonic() + self.config.heartbeat_seconds / 2
        try:
            while True:
                if datetime.now(UTC) >= hard_deadline - REPORT_MARGIN:
                    process.kill()
                    raise LiveFactoryError("remote command exceeded teardown margin")
                try:
                    output, _ = process.communicate(timeout=1)
                    chunks.append(output or "")
                    if process.returncode:
                        raise LiveFactoryError("bounded remote command failed")
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= next_heartbeat:
                        heartbeat()
                        next_heartbeat = time.monotonic() + self.config.heartbeat_seconds / 2
        finally:
            if process.poll() is None:
                process.kill()
                output, _ = process.communicate()
                chunks.append(output or "")
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text("".join(chunks), encoding="utf-8")

    def _remote(self, endpoint: SshEndpoint, command: Sequence[str], *, hard_deadline: datetime, heartbeat: Callable[[], None], log: Path) -> None:
        seconds = self._deadline_seconds(hard_deadline)
        remote = shlex.join(["timeout", "--foreground", str(seconds), *command])
        self._stream([*self._ssh_prefix(endpoint), remote], hard_deadline=hard_deadline, heartbeat=heartbeat, log=log)

    def _wait_for_ssh(self, endpoint: SshEndpoint, *, hard_deadline: datetime, heartbeat: Callable[[], None], transport: Path) -> None:
        for attempt in range(1, 37):
            try:
                self._remote(endpoint, ["true"], hard_deadline=hard_deadline, heartbeat=heartbeat, log=transport / f"ssh-ready-{attempt:02d}.log")
                return
            except LiveFactoryError:
                if attempt == 36:
                    break
                heartbeat()
                time.sleep(5)
        raise LiveFactoryError("SSH listener did not become ready within the bounded wait")

    @staticmethod
    def _manifest_hash(directory: Path) -> str:
        records = []
        for path in sorted(directory.glob("*.yaml")):
            records.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  ./{path.name}")
        if not records:
            raise LiveFactoryError("manifest bundle has no YAML files")
        return hashlib.sha256(("\n".join(records) + "\n").encode()).hexdigest()

    def _copy(self, endpoint: SshEndpoint, local: Path, remote: str, *, recursive: bool, hard_deadline: datetime, heartbeat: Callable[[], None], log: Path) -> None:
        arguments = ["scp", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={self.config.known_hosts_file}", "-i", str(self.config.identity_file), "-P", str(endpoint.port)]
        if recursive:
            arguments.append("-r")
        arguments.extend((str(local), f"{endpoint.destination(self.config.user)}:{remote}"))
        self._stream(arguments, hard_deadline=hard_deadline, heartbeat=heartbeat, log=log)

    def _fetch(self, endpoint: SshEndpoint, remote: str, local: Path, *, hard_deadline: datetime, heartbeat: Callable[[], None], log: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        arguments = ["scp", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={self.config.known_hosts_file}", "-i", str(self.config.identity_file), "-P", str(endpoint.port), "-r", f"{endpoint.destination(self.config.user)}:{remote}", str(local)]
        self._stream(arguments, hard_deadline=hard_deadline, heartbeat=heartbeat, log=log)

    def run(self, *, stage: str, workload: WorkloadContract, instance: InstanceContract, run_directory: Path, hard_deadline: datetime, heartbeat: Callable[[], None]) -> LiveEvidence:
        root = f"/var/tmp/srecon26-canary-{hashlib.sha256(instance.label.encode()).hexdigest()[:20]}"
        remote_evidence, local_evidence = f"{root}/evidence", run_directory / "remote-evidence"
        transport = run_directory / "remote-transport"
        transport.mkdir(parents=True, exist_ok=True)
        cleanup_needed = False
        endpoint: SshEndpoint | None = None
        evidence_files: tuple[Path, ...] = ()
        try:
            self.resolver.attach_public_key(instance, self.config.public_key_file, hard_deadline=hard_deadline, heartbeat=heartbeat, status_log=transport / "provider-status.ndjson")
            (transport / "ssh-key-attached.txt").write_text(f"instance_id={instance.instance_id}\nlabel={instance.label}\n", encoding="utf-8")
            endpoint = self.resolver.resolve(instance, hard_deadline=hard_deadline, heartbeat=heartbeat, status_log=transport / "provider-status.ndjson")
            self._wait_for_ssh(endpoint, hard_deadline=hard_deadline, heartbeat=heartbeat, transport=transport)
            self._remote(endpoint, ["install", "-d", "-m", "0700", root], hard_deadline=hard_deadline, heartbeat=heartbeat, log=transport / "mkdir.log")
            self._copy(endpoint, self.config.local_script, f"{root}/remote_host_canary.sh", recursive=False, hard_deadline=hard_deadline, heartbeat=heartbeat, log=transport / "copy-script.log")
            base_env = [
                f"CANARY_EVIDENCE_DIR={remote_evidence}",
                f"CANARY_VLLM_IMAGE=docker.io/vllm/vllm-openai@{workload.vllm_image_digest}",
                f"CANARY_MODEL={workload.model_id}", f"CANARY_MODEL_REVISION={workload.model_revision}",
                f"CANARY_HARD_DEADLINE={_stamp(hard_deadline)}",
                f"CANARY_HARD_DEADLINE_MARGIN_SECONDS={int(REPORT_MARGIN.total_seconds())}",
                "CANARY_WAIT_SECONDS=600", "CANARY_COMMAND_TIMEOUT_SECONDS=120",
            ]
            script = f"{root}/remote_host_canary.sh"
            self._remote(endpoint, ["env", *base_env, "bash", script, "probe"], hard_deadline=hard_deadline, heartbeat=heartbeat, log=transport / "probe.log")
            if stage == "inference-smoke":
                self._remote(
                    endpoint,
                    ["env", *base_env, "CANARY_INFERENCE_PHASE_SECONDS=600", "CANARY_INFERENCE_CONCURRENCY=4", "CANARY_INFERENCE_MAX_TOKENS=128", "bash", script, "inference-smoke"],
                    hard_deadline=hard_deadline,
                    heartbeat=heartbeat,
                    log=transport / "inference-smoke.log",
                )
                self._fetch(endpoint, remote_evidence, local_evidence, hard_deadline=hard_deadline, heartbeat=heartbeat, log=transport / "copy-evidence.log")
                evidence_files = tuple(path for path in local_evidence.rglob("*") if path.is_file()) + tuple(path for path in transport.rglob("*") if path.is_file())
                return self._evidence(stage, local_evidence, transport, workload, evidence_files, run_id=run_directory.name, instance=instance)
            self._copy(endpoint, self.config.local_manifest_dir, f"{root}/manifests", recursive=True, hard_deadline=hard_deadline, heartbeat=heartbeat, log=transport / "copy-manifests.log")
            self._copy(endpoint, self.config.k3s_binary, f"{root}/k3s", recursive=False, hard_deadline=hard_deadline, heartbeat=heartbeat, log=transport / "copy-k3s.log")
            self._copy(endpoint, self.config.nvidia_runtime_template, f"{root}/nvidia-runtime.toml", recursive=False, hard_deadline=hard_deadline, heartbeat=heartbeat, log=transport / "copy-runtime.log")
            manifest_env = [
                f"CANARY_MANIFEST_DIR={root}/manifests",
                f"CANARY_MANIFEST_SHA256={self._manifest_hash(self.config.local_manifest_dir)}",
            ]
            if stage == "gpu-smoke":
                self._remote(endpoint, ["sha256sum", f"{root}/manifests/vllm.yaml", script], hard_deadline=hard_deadline, heartbeat=heartbeat, log=transport / "staged-contract-sha256.txt")
                self._fetch(endpoint, remote_evidence, local_evidence, hard_deadline=hard_deadline, heartbeat=heartbeat, log=transport / "copy-evidence.log")
                evidence_files = tuple(path for path in local_evidence.rglob("*") if path.is_file()) + tuple(path for path in transport.rglob("*") if path.is_file())
                return self._evidence(stage, local_evidence, transport, workload, evidence_files, run_id=run_directory.name, instance=instance)
            self._remote(endpoint, ["env", *base_env, *manifest_env, f"K3S_BINARY_PATH={root}/k3s", f"NVIDIA_RUNTIME_TEMPLATE={root}/nvidia-runtime.toml", "bash", script, "install"], hard_deadline=hard_deadline, heartbeat=heartbeat, log=transport / "install.log")
            cleanup_needed = True
            self._remote(endpoint, ["env", *base_env, *manifest_env, "bash", script, "deploy"], hard_deadline=hard_deadline, heartbeat=heartbeat, log=transport / "deploy.log")
            self._remote(endpoint, ["env", *base_env, *manifest_env, "bash", script, "collect"], hard_deadline=hard_deadline, heartbeat=heartbeat, log=transport / "collect.log")
            # The script captures operational evidence.  This direct, read-only
            # query additionally records the immutable image and GPU allocation
            # actually running, rather than inferring either from the template.
            self._remote(endpoint, ["/usr/local/bin/k3s", "kubectl", "-n", "srecon26-canary", "get", "deployment", "vllm", "-o", "json"], hard_deadline=hard_deadline, heartbeat=heartbeat, log=transport / "vllm-deployment.json")
            self._remote(endpoint, ["/usr/local/bin/k3s", "kubectl", "-n", "srecon26-canary", "get", "pods", "-l", "app.kubernetes.io/name=vllm", "-o", "json"], hard_deadline=hard_deadline, heartbeat=heartbeat, log=transport / "vllm-pods.json")
            self._fetch(endpoint, remote_evidence, local_evidence, hard_deadline=hard_deadline, heartbeat=heartbeat, log=transport / "copy-evidence.log")
            evidence_files = tuple(path for path in local_evidence.rglob("*") if path.is_file()) + tuple(path for path in transport.rglob("*") if path.is_file())
            return self._evidence(stage, local_evidence, transport, workload, evidence_files, run_id=run_directory.name, instance=instance)
        except Exception as error:
            # SSH readiness, controller networking, local staging, model pulls,
            # and Kubernetes bootstrap errors are unresolved diagnoses.  They
            # are never enough to justify clicking the provider report button.
            failure = transport / "failure.txt"
            failure.parent.mkdir(parents=True, exist_ok=True)
            failure.write_text(str(error) + "\n", encoding="utf-8")
            if endpoint is not None:
                try:
                    self._fetch(endpoint, remote_evidence, local_evidence, hard_deadline=hard_deadline, heartbeat=heartbeat, log=transport / "copy-failure-evidence.log")
                except Exception as fetch_error:
                    (transport / "failure-evidence-fetch.txt").write_text(str(fetch_error) + "\n", encoding="utf-8")
            evidence_files = tuple(path for path in local_evidence.rglob("*") if path.is_file()) + tuple(path for path in transport.rglob("*") if path.is_file())
            return LiveEvidence(None, None, None, provider_fault=None, probe_outcome=ProbeOutcome.CONTROLLER_FAILED, evidence_files=evidence_files)
        finally:
            if cleanup_needed and endpoint is not None:
                try:
                    self._remote(endpoint, ["env", f"CANARY_EVIDENCE_DIR={remote_evidence}", f"CANARY_MANIFEST_DIR={root}/manifests", f"CANARY_MANIFEST_SHA256={self._manifest_hash(self.config.local_manifest_dir)}", "bash", f"{root}/remote_host_canary.sh", "cleanup"], hard_deadline=hard_deadline, heartbeat=heartbeat, log=transport / "cleanup.log")
                except Exception:
                    # The dispatcher still owns exact provider teardown; a
                    # cleanup failure is retained in evidence and cannot claim success.
                    pass

    def _evidence(self, stage: str, evidence: Path, transport: Path, workload: WorkloadContract, files: tuple[Path, ...], *, run_id: str, instance: InstanceContract) -> LiveEvidence:
        now = datetime.now(UTC)
        def text(name: str) -> str:
            try:
                return (evidence / name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""
        def status(name: str) -> bool:
            try:
                return json.loads((evidence / name).read_text(encoding="utf-8")).get("status") == "PASSED"
            except (OSError, json.JSONDecodeError, AttributeError):
                return False
        def json_object(name: str) -> Mapping[str, object] | None:
            try:
                payload = json.loads((evidence / name).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            return payload if isinstance(payload, Mapping) else None
        def option_value(command: object, option: str) -> str | None:
            if not isinstance(command, list):
                return None
            for index, value in enumerate(command[:-1]):
                if value == option and isinstance(command[index + 1], str):
                    return command[index + 1]
            return None
        def finite_nonnegative(value: object) -> Decimal:
            number = Decimal(str(value))
            if not number.is_finite() or number < 0:
                raise ValueError("expected a finite non-negative decimal")
            return number
        def gpu_samples(name: str) -> dict[str, tuple[Decimal, Decimal]]:
            samples: dict[str, tuple[Decimal, Decimal]] = {}
            for line in text(name).splitlines():
                fields = [field.strip().strip('"') for field in line.split(",")]
                if len(fields) < 8:
                    continue
                uuid = fields[2]
                if not uuid:
                    continue
                samples[uuid] = (finite_nonnegative(fields[4]), finite_nonnegative(fields[6]))
            if not samples:
                raise ValueError("missing usable GPU sample")
            return samples
        def active_compute_processes(name: str, allowed_uuids: set[str]) -> list[tuple[str, Decimal]]:
            processes: list[tuple[str, Decimal]] = []
            for line in text(name).splitlines():
                fields = [field.strip().strip('"') for field in line.split(",")]
                if len(fields) < 4 or not fields[1] or fields[3] not in allowed_uuids:
                    continue
                used_memory = finite_nonnegative(fields[2])
                if used_memory > 0:
                    processes.append((fields[3], used_memory))
            if not processes:
                raise ValueError("missing active GPU compute process")
            return processes
        probe = status("probe-status.json")
        deployment = text_from(transport / "vllm-deployment.json")
        pods = text_from(transport / "vllm-pods.json")
        staged_manifest = text_from(self.config.local_manifest_dir / "vllm.yaml")
        inference_status = json_object("inference-status.json")
        inference_image_digests: object = None
        try:
            inference_image_digests = json.loads(text("inference-image-inspect.json"))
        except json.JSONDecodeError:
            pass
        inference_container = json_object("inference-container-inspect.json")
        inference_models = json_object("inference-vllm-models.json")
        image_ok = workload.vllm_image_digest in deployment or (stage == "gpu-smoke" and workload.vllm_image_digest in staged_manifest and bool(text_from(transport / "staged-contract-sha256.txt")))
        model_ok = (workload.model_id in deployment and workload.model_revision in deployment) or (stage == "gpu-smoke" and "REQUIRED_AT_RUN_TIME_MODEL" in staged_manifest and "REQUIRED_AT_RUN_TIME_REVISION" in staged_manifest)
        if stage == "inference-smoke":
            image_ok = (
                isinstance(inference_status, Mapping)
                and inference_status.get("status") == "PASSED"
                and inference_status.get("image") == f"docker.io/vllm/vllm-openai@{workload.vllm_image_digest}"
                and isinstance(inference_image_digests, list)
                and any(isinstance(value, str) and value.endswith(f"@{workload.vllm_image_digest}") for value in inference_image_digests)
                and isinstance(inference_container, Mapping)
                and inference_container.get("image") == f"docker.io/vllm/vllm-openai@{workload.vllm_image_digest}"
            )
            served_models = inference_models.get("data") if isinstance(inference_models, Mapping) else None
            model_ok = (
                isinstance(inference_status, Mapping)
                and inference_status.get("status") == "PASSED"
                and inference_status.get("model") == workload.model_id
                and inference_status.get("model_revision") == workload.model_revision
                and inference_status.get("failed_requests") == 0
                and isinstance(inference_container, Mapping)
                and option_value(inference_container.get("command"), "--model") == workload.model_id
                and option_value(inference_container.get("command"), "--revision") == workload.model_revision
                and isinstance(served_models, list)
                and any(isinstance(model, Mapping) and model.get("id") == workload.model_id for model in served_models)
            )
        gpu = (text("inference-gpu-identity.txt") if stage == "inference-smoke" else text("nvidia-smi.txt")).strip() or None
        cuda = (text("inference-cuda.txt") if stage == "inference-smoke" else text("cuda.txt")).strip() or None
        facts = KvmFacts(probe, probe, probe, probe, probe, bool(gpu), bool(cuda), workload.vllm_image_digest if image_ok else None, now if image_ok else None)
        metric = MetricSample(Decimal("1"), now)
        snapshot = CanarySnapshot(
            1 if "nvidia.com/gpu" in text("node-describe.txt") else 0,
            "numberReady: 1" in text("device-plugin.txt"),
            '"phase": "Running"' in pods and '"ready": true' in pods,
            1 if '"nvidia.com/gpu": "1"' in pods or '"nvidia.com/gpu": 1' in pods else 0,
            status("collection-status.json") and '"activeTargets"' in text("prometheus-targets.json"),
            metric if text("resource-metrics.txt") else None,
            metric if text("custom-metrics.txt") else None,
            metric if text("prometheus-metrics.json") else None,
            '"http_code":200' in text("warmup-timing.json"),
            '"http_code":200' in text("measured-timing.json"),
        )
        inference_measurement = None
        if stage == "inference-smoke" and image_ok and model_ok:
            try:
                timing = json_object("inference-request-1-timing.json")
                if timing is None:
                    raise ValueError("missing direct inference request timing")
                usage = timing.get("usage")
                output_tokens = timing.get("completion_tokens")
                prompt_tokens = timing.get("prompt_tokens")
                total_tokens = timing.get("total_tokens")
                if (
                    timing.get("status") != "PASSED"
                    or timing.get("request") != "request-1"
                    or timing.get("http_code") != 200
                    or timing.get("model") != workload.model_id
                    or timing.get("response_model") != workload.model_id
                    or not isinstance(usage, Mapping)
                    or any(not isinstance(value, int) or isinstance(value, bool) for value in (prompt_tokens, output_tokens, total_tokens))
                    or prompt_tokens < 0 or output_tokens <= 0 or total_tokens != prompt_tokens + output_tokens
                    or any(usage.get(key) != value for key, value in (("prompt_tokens", prompt_tokens), ("completion_tokens", output_tokens), ("total_tokens", total_tokens)))
                ):
                    raise ValueError("invalid direct inference timing")
                ttft = finite_nonnegative(timing["ttft_seconds"])
                latency = finite_nonnegative(timing["e2e_seconds"])
                if latency < ttft:
                    raise ValueError("direct inference end-to-end latency precedes TTFT")
                stream_chunks: list[Mapping[str, object]] = []
                for line in text("inference-request-1-body.ndjson").splitlines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    payload = json.loads(line.removeprefix("data: "))
                    if isinstance(payload, Mapping):
                        stream_chunks.append(payload)
                stream_usage = next((chunk.get("usage") for chunk in reversed(stream_chunks) if isinstance(chunk.get("usage"), Mapping)), None)
                if (
                    not stream_chunks
                    or not any(chunk.get("model") == workload.model_id for chunk in stream_chunks)
                    or not isinstance(stream_usage, Mapping)
                    or any(stream_usage.get(key) != value for key, value in (("prompt_tokens", prompt_tokens), ("completion_tokens", output_tokens), ("total_tokens", total_tokens)))
                ):
                    raise ValueError("stream body does not attest the timed direct model response")
                before = gpu_samples("inference-nvidia-smi-before.csv")
                during = gpu_samples("inference-nvidia-smi-during.csv")
                after = gpu_samples("inference-nvidia-smi-after.csv")
                shared_uuids = set(before) & set(during) & set(after)
                active_processes = active_compute_processes("inference-nvidia-compute-during.csv", shared_uuids)
                attributed_uuid = next(
                    (
                        uuid for uuid in sorted(shared_uuids)
                        if during[uuid][0] > 0 or during[uuid][1] > before[uuid][1]
                    ),
                    None,
                )
                if attributed_uuid is None or not any(uuid == attributed_uuid for uuid, _used_memory in active_processes):
                    raise ValueError("during GPU sample has neither utilization nor added memory attribution")
                measurement_path = evidence / "direct-inference-measurement.json"
                measurement_path.write_text(
                    json.dumps(
                        {
                            "schema": "srecon26-direct-inference-measurement/v1",
                            "source": "RemoteWorkload.direct_inference/v1",
                            "run_id": run_id,
                            "instance_id": instance.instance_id,
                            "label": instance.label,
                            "model_id": workload.model_id,
                            "model_revision": workload.model_revision,
                            "vllm_image_digest": workload.vllm_image_digest,
                            "request": {"succeeded": True, "model": workload.model_id, "http_code": 200, "prompt_tokens": prompt_tokens, "output_tokens": output_tokens, "total_tokens": total_tokens, "raw_timing_artifact": "inference-request-1-timing.json", "raw_stream_artifact": "inference-request-1-body.ndjson"},
                            "timing": {"ttft_seconds": str(ttft), "latency_seconds": str(latency), "tpot_seconds": timing.get("tpot_seconds"), "generation_tokens_per_second": timing.get("generation_tokens_per_second")},
                            "hardware_attribution": {"gpu_uuid": attributed_uuid, "before_gpu_csv": "inference-nvidia-smi-before.csv", "during_gpu_csv": "inference-nvidia-smi-during.csv", "after_gpu_csv": "inference-nvidia-smi-after.csv", "during_compute_process_csv": "inference-nvidia-compute-during.csv", "during_utilization_percent": str(during[attributed_uuid][0]), "before_memory_mib": str(before[attributed_uuid][1]), "during_memory_mib": str(during[attributed_uuid][1]), "active_compute_processes": sum(1 for uuid, _used_memory in active_processes if uuid == attributed_uuid)},
                            "observed_at": _stamp(now),
                        },
                        indent=2,
                        sort_keys=True,
                    ) + "\n",
                    encoding="utf-8",
                )
                files = (*files, measurement_path)
                inference_measurement = InferenceMeasurementEvidence(measurement_path)
            except (KeyError, TypeError, ValueError, InvalidOperation, json.JSONDecodeError):
                inference_measurement = None
        return LiveEvidence(
            gpu_identity=gpu, cuda_version=cuda, kvm_facts=facts, model_id=workload.model_id if model_ok else None,
            model_revision=workload.model_revision if model_ok else None, vllm_image_digest=workload.vllm_image_digest if image_ok else None,
            snapshot=snapshot, resource_metrics_api=bool(text("resource-metrics.txt")), custom_metrics_api=bool(text("custom-metrics.txt")),
            hpa_observed=bool(text("hpa.txt")), events_captured=bool(text("events.txt")), timing_captured=bool(text("request-timing.json")),
            probe_outcome=ProbeOutcome.PASS if probe and (stage != "inference-smoke" or status("inference-status.json")) else ProbeOutcome.CONTROLLER_FAILED, evidence_files=files,
            inference_measurement=inference_measurement,
        )


def text_from(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise LiveFactoryError(f"{name} must be explicitly configured for --live")
    return value


def _load_report_gate(spec: str) -> ReportGate:
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise LiveFactoryError("SRECON26_REPORT_ADAPTER_FACTORY must be module:callable")
    try:
        module = __import__(module_name, fromlist=[attribute])
        value = getattr(module, attribute)()
    except (ImportError, AttributeError, TypeError) as error:
        raise LiveFactoryError("cannot load exact-target report adapter") from error
    if isinstance(value, ReportGate):
        if not callable(getattr(value.adapter, "preflight_authenticated_session", None)):
            raise LiveFactoryError("report gate does not expose authenticated-session preflight")
        return value
    required = ("preflight_authenticated_session", "preflight_exact_instance", "capture_before", "submit", "capture_after")
    if not all(callable(getattr(value, name, None)) for name in required):
        raise LiveFactoryError("report adapter factory did not return an exact-target adapter")
    return ReportGate(value)  # type: ignore[arg-type]


def create_dispatcher() -> LiveCanaryDispatcher:
    """Build the production dispatcher from explicit, non-secret settings.

    Authentication remains in the user's existing ``gh`` and ``vastai`` CLI
    sessions; this factory neither accepts nor prints API keys.
    """
    root = Path(__file__).resolve().parents[2]
    output_root = Path(_required_env("SRECON26_LIVE_OUTPUT_ROOT")).expanduser().resolve()
    cli = _required_env("SRECON26_VAST_CLI")
    github = GitHubGuardConfig(
        repository=_required_env("SRECON26_GITHUB_REPOSITORY"), ref=_required_env("SRECON26_GITHUB_REF"),
        issue_number=_integer(_required_env("SRECON26_GUARD_ISSUE"), "GitHub guard issue", minimum=1),
        trusted_author=_required_env("SRECON26_GUARD_TRUSTED_AUTHOR"),
        heartbeat_seconds=_integer(os.environ.get("SRECON26_GUARD_HEARTBEAT_SECONDS", "120"), "GitHub heartbeat", minimum=30, maximum=600),
        dispatch_on_arm=os.environ.get("SRECON26_GUARD_ADOPT_EXISTING", "false").lower() != "true",
    )
    ssh = SshWorkloadConfig(
        user=_required_env("SRECON26_SSH_USER"), identity_file=Path(_required_env("SRECON26_SSH_IDENTITY_FILE")).expanduser(), public_key_file=Path(_required_env("SRECON26_SSH_PUBLIC_KEY_FILE")).expanduser(),
        known_hosts_file=Path(_required_env("SRECON26_SSH_KNOWN_HOSTS_FILE")).expanduser(),
        k3s_binary=Path(_required_env("SRECON26_K3S_BINARY")).expanduser(),
        nvidia_runtime_template=root / "infra/k3s/nvidia-runtime.toml", local_script=root / "scripts/remote_host_canary.sh",
        local_manifest_dir=root / "infra/k3s", heartbeat_seconds=github.heartbeat_seconds,
    )
    return LiveCanaryDispatcher(
        provider=VastCliProvider(cli, reconcile_attempts=24, reconcile_interval_seconds=5), guard=DynamicGitHubGuard(github),
        report_gate=_load_report_gate(_required_env("SRECON26_REPORT_ADAPTER_FACTORY")),
        workload=SshRemoteWorkload(VastSshResolver(cli), ssh), ledger=ExposureLedger(output_root / "exposure-ledger.json"), output_root=output_root,
    )
