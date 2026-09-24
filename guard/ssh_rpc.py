#!/usr/bin/python3
"""Root-only, bounded forced-command gateway for the independent Azure guard.

Only the bootstrap entry point consumes a credential. Provider subprocesses
receive it through the existing worker's private environment, never argv.
All failures use fixed public messages; exception text is deliberately absent.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, Mapping
from uuid import UUID


UTC = timezone.utc

if __package__:
    from .guard_worker import GuardSafetyError, GuardWorker, VastCliGuardProvider, label_binds_nonce
else:
    # The installer owns this directory and launcher uses Python isolated mode.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from guard_worker import GuardSafetyError, GuardWorker, VastCliGuardProvider, label_binds_nonce


PROTOCOL = "srecon26-guard-v1"
MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MAX_CREDENTIAL_BYTES = 4096
STATE_ROOT = Path("/var/lib/srecon26-guard")
CONFIG_FILE = Path("/etc/srecon26-guard/controller.json")
SECRET_FILE = Path("/etc/srecon26-guard/vast-api-key")
HOST_KEY_FILE = Path("/etc/ssh/ssh_host_ed25519_key.pub")
_NONCE = re.compile(r"[A-Za-z0-9_-]{8,128}")
_LABEL = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_RUN_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_HASH = re.compile(r"[0-9a-f]{64}")
_RESOURCE_ID = re.compile(
    r"/subscriptions/[A-Za-z0-9-]+/resourceGroups/[A-Za-z0-9_.()-]+/"
    r"providers/Microsoft\.Compute/virtualMachines/[A-Za-z0-9_.-]+", re.IGNORECASE,
)
_FIELDS = {
    "preflight": set(),
    "arm": {"run_id", "label", "nonce", "hard_deadline", "heartbeat_timeout_seconds"},
    "heartbeat": {"run_id", "label", "nonce", "monotonic_ns"},
    "status": {"nonce"},
    "anchor": {"nonce", "root_hash"},
    "export": {"nonce", "root_hash"},
}
_SAFE_TERMINAL = {"ABSENCE_CONFIRMED", "DISARMED"}


class GuardProcessDeadline(BaseException):
    """Unrecoverable process deadline; ordinary worker recovery cannot catch it."""


def _refuse() -> None:
    raise GuardSafetyError("guard request refused")


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        _refuse()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        _refuse()
    return parsed.astimezone(UTC)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _refuse()
        result[key] = value
    return result


def _json(raw: bytes) -> object:
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=lambda _: _refuse())


def _encode(payload: Mapping[str, object]) -> bytes:
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False, default=_stamp) + "\n").encode("utf-8")
    if len(raw) > MAX_RESPONSE_BYTES:
        _refuse()
    return raw


def read_bounded(stream: BinaryIO, limit: int) -> bytes:
    raw = stream.read(limit + 1)
    if not raw or len(raw) > limit:
        _refuse()
    return raw


def validate_request(raw: bytes) -> tuple[str, dict[str, object]]:
    if len(raw) > MAX_REQUEST_BYTES:
        _refuse()
    request = _json(raw)
    if not isinstance(request, dict) or set(request) != {"protocol", "command", "payload"}:
        _refuse()
    command, payload = request["command"], request["payload"]
    if request["protocol"] != PROTOCOL or not isinstance(command, str) or command not in _FIELDS:
        _refuse()
    if not isinstance(payload, dict) or set(payload) != _FIELDS[command]:
        _refuse()
    if command != "preflight" and (not isinstance(payload["nonce"], str) or not _NONCE.fullmatch(payload["nonce"])):
        _refuse()
    if command in {"arm", "heartbeat"}:
        if not isinstance(payload["run_id"], str) or not _RUN_ID.fullmatch(payload["run_id"]):
            _refuse()
        label = payload["label"]
        if not isinstance(label, str) or not _LABEL.fullmatch(label) or not label_binds_nonce(label, payload["nonce"]):
            _refuse()
    if command == "arm":
        _timestamp(payload["hard_deadline"])
        timeout = payload["heartbeat_timeout_seconds"]
        if type(timeout) is not int or not 1 <= timeout <= 600:
            _refuse()
    if command == "heartbeat":
        value = payload["monotonic_ns"]
        if type(value) is not int or not 0 <= value <= 2**63 - 1:
            _refuse()
    if command in {"anchor", "export"}:
        if not isinstance(payload["root_hash"], str) or not _HASH.fullmatch(payload["root_hash"]):
            _refuse()
    return command, payload


def check_path(path: Path, *, directory: bool = False, mode: int | None = None,
               require_root_owner: bool = True) -> None:
    info = path.lstat()
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not correct_type or (require_root_owner and info.st_uid != 0):
        _refuse()
    permissions = stat.S_IMODE(info.st_mode)
    if (mode is not None and permissions != mode) or permissions & 0o022:
        _refuse()


def read_private(path: Path, limit: int, *, require_root_owner: bool = True) -> bytes:
    check_path(path, mode=0o600, require_root_owner=require_root_owner)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        return read_bounded(stream, limit)


def validate_config(config: object) -> dict[str, str]:
    if not isinstance(config, dict) or set(config) != {"azure_resource_id", "azure_vm_id", "vast_bin"}:
        _refuse()
    if any(not isinstance(value, str) for value in config.values()):
        _refuse()
    if not _RESOURCE_ID.fullmatch(config["azure_resource_id"]):
        _refuse()
    UUID(config["azure_vm_id"])
    binary = Path(config["vast_bin"])
    if not binary.is_absolute() or binary.name != "vastai" or any(c.isspace() for c in str(binary)):
        _refuse()
    if ".." in binary.parts or not str(binary).startswith(("/opt/", "/usr/")):
        _refuse()
    return config


def install_credential(stream: BinaryIO, path: Path = SECRET_FILE, *, require_root_owner: bool = True) -> None:
    check_path(path.parent, directory=True, mode=0o700, require_root_owner=require_root_owner)
    raw = read_bounded(stream, MAX_CREDENTIAL_BYTES)
    # One printable ASCII token, with an optional single final newline.
    token = raw.removesuffix(b"\n")
    if not token or any(value < 33 or value > 126 for value in token):
        _refuse()
    # Exclusive creation prevents both symlink attacks and live key rotation.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=False) as output:
            output.write(token + b"\n")
            output.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    parent_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


class GuardGateway:
    def __init__(self, worker: GuardWorker, metadata: Mapping[str, str], *, require_root_owner: bool = True,
                 readiness_check: Callable[[], None] | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.worker = worker
        self.root = worker.root
        self.metadata = dict(metadata)
        self.require_root_owner = require_root_owner
        self.readiness_check = readiness_check
        self.clock = clock or (lambda: datetime.now(UTC))
        check_path(self.root, directory=True, mode=0o700, require_root_owner=require_root_owner)

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
            _refuse()
        return value.astimezone(UTC)

    def _authorize_receipt(self, receipt: Mapping[str, object]) -> datetime:
        # Call only after all potentially blocking receipt/readiness work.
        now = self._now()
        if receipt["status"] not in {"ARMED", "AWAITING_INSTANCE"} or receipt["teardown_authority_at"] is not None:
            _refuse()
        if now >= receipt["hard_deadline"] or now - receipt["last_heartbeat"] > timedelta(seconds=int(receipt["heartbeat_timeout_seconds"])):
            _refuse()
        return now

    @contextmanager
    def locked(self) -> Iterator[None]:
        path = self.root / "rpc.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            check_path(path, mode=0o600, require_root_owner=self.require_root_owner)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _runs(self) -> list[str]:
        nonces: list[str] = []
        for path in sorted(self.root.iterdir()):
            if _NONCE.fullmatch(path.name) and (path.is_dir() or path.is_symlink()):
                nonces.append(path.name)
        return nonces

    def _binding(self) -> dict[str, object]:
        value = _json(read_private(self.root / "rpc-binding.json", MAX_REQUEST_BYTES,
                                  require_root_owner=self.require_root_owner))
        _, binding = validate_request(_encode({"protocol": PROTOCOL, "command": "arm", "payload": value}))
        return binding

    def _receipt(self, nonce: str) -> dict[str, object]:
        check_path(self.root / nonce, directory=True, mode=0o700, require_root_owner=self.require_root_owner)
        check_path(self.root / nonce / "state.json", mode=0o600, require_root_owner=self.require_root_owner)
        receipt = asdict(self.worker.status(nonce))
        return {**receipt, **self.metadata, "script_hash": self.worker.preflight()["script_hash"]}

    def _bound_receipt(self, binding: Mapping[str, object]) -> dict[str, object]:
        receipt = self._receipt(str(binding["nonce"]))
        if any(receipt[key] != binding[key] for key in ("nonce", "label", "heartbeat_timeout_seconds")):
            _refuse()
        if receipt["hard_deadline"] != _timestamp(binding["hard_deadline"]):
            _refuse()
        return receipt

    def dispatch(self, command: str, payload: dict[str, object], *, now: datetime | None = None) -> dict[str, object]:
        # Retain the old request-time keyword for callers, but never use it as
        # authorization time. Tests inject a clock, just like production does.
        # Also validate callers that use this API directly (tests/local tooling).
        validate_request(_encode({"protocol": PROTOCOL, "command": command, "payload": payload}))
        with self.locked():
            if command in {"arm", "preflight"} and self.readiness_check is not None:
                self.readiness_check()
            if command == "arm":
                return self._arm(payload)
            binding = self._binding()
            if command == "preflight":
                receipt = self._bound_receipt(binding)
                self._authorize_receipt(receipt)
                # ARMED is the attestation status; the worker can still be
                # waiting for the provider create to become visible.
                return {**receipt, "status": "ARMED", "worker_status": receipt["status"]}
            nonce = str(payload["nonce"])
            if command in {"status", "export", "anchor"}:
                # Explicit nonce operations can collect an earlier completed run.
                self._receipt(nonce)
            elif nonce != binding["nonce"]:
                _refuse()
            if command == "heartbeat":
                if any(payload[key] != binding[key] for key in ("run_id", "label")):
                    _refuse()
                receipt = self._bound_receipt(binding)
                current = self._authorize_receipt(receipt)
                self.worker.heartbeat(nonce, now=current, clock=self._now)
                receipt = self._bound_receipt(binding)
                self._authorize_receipt(receipt)
                return receipt
            elif command == "anchor":
                self.worker.anchor(nonce, str(payload["root_hash"]), now=self._now())
                return {**self._receipt(nonce), "anchored_root_hash": payload["root_hash"]}
            elif command == "export":
                return self._export(nonce, str(payload["root_hash"]))
            return self._receipt(nonce)

    def _arm(self, payload: dict[str, object]) -> dict[str, object]:
        nonce = str(payload["nonce"])
        for existing in self._runs():
            receipt = self._receipt(existing)
            if existing != nonce and receipt["status"] not in _SAFE_TERMINAL:
                _refuse()
        binding_path = self.root / "rpc-binding.json"
        if binding_path.exists():
            binding = self._binding()
            if binding["nonce"] == nonce:
                if binding != payload:
                    _refuse()
                receipt = self._bound_receipt(binding)
                self._authorize_receipt(receipt)
                return {**receipt, "status": "ARMED", "worker_status": receipt["status"]}
        if (self.root / nonce).exists():
            # A crash between worker arm and binding commit leaves a watcher
            # active, but must not permit reassignment by a later RPC.
            _refuse()
        self.worker.provider_preflight()
        self.worker.heartbeat_timeout = timedelta(seconds=int(payload["heartbeat_timeout_seconds"]))
        self.worker.arm(None, str(payload["label"]), nonce, _timestamp(payload["hard_deadline"]), now=self._now())
        self.worker._durable_replace(binding_path, payload)
        receipt = self._bound_receipt(payload)
        self._authorize_receipt(receipt)
        return receipt

    def _export(self, nonce: str, expected_root: str) -> dict[str, object]:
        with self.worker._locked(nonce) as directory:
            state = self.worker._load(directory)
            if state["root_hash"] != expected_root:
                _refuse()
            journal = directory / "journal.ndjson"
            check_path(journal, mode=0o600, require_root_owner=self.require_root_owner)
            with journal.open("rb") as source:
                raw = read_bounded(source, MAX_RESPONSE_BYTES)
            if not raw.endswith(b"\n"):
                _refuse()
            response = {
                "status": "EVIDENCE_EXPORTED", "nonce": nonce, "root_hash": expected_root,
                "journal": raw.decode("utf-8"), "journal_encoding": "utf-8-jsonl",
                "journal_sha256": hashlib.sha256(raw).hexdigest(),
            }
            _encode(response)
            return response

    def tick_all(self, *, now: datetime | None = None) -> bool:
        failed = False
        with self.locked():
            for nonce in self._runs():
                try:
                    # Incomplete/crashed or unsafe arms fail this tick, but
                    # must not prevent an independent arm's teardown check.
                    self._receipt(nonce)
                    self.worker.tick(nonce, now=self._now())
                except Exception:
                    # One corrupt/provider-failed run must not stop other ticks.
                    failed = True
        return not failed


def check_timer_ready() -> None:
    result = subprocess.run(
        ["/usr/bin/systemctl", "show", "--property=LoadState,ActiveState,UnitFileState", "srecon26-azure-guard.timer"],
        check=True, capture_output=True, timeout=3, text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if properties != {"LoadState": "loaded", "ActiveState": "active", "UnitFileState": "enabled"}:
        _refuse()


def production_gateway() -> GuardGateway:
    check_path(CONFIG_FILE.parent, directory=True, mode=0o700)
    check_path(STATE_ROOT, directory=True, mode=0o700)
    config = validate_config(_json(read_private(CONFIG_FILE, MAX_REQUEST_BYTES)))
    check_path(SECRET_FILE, mode=0o600)
    check_path(Path(config["vast_bin"]))
    check_path(HOST_KEY_FILE)
    fields = HOST_KEY_FILE.read_text(encoding="ascii").split()
    if len(fields) < 2 or fields[0] != "ssh-ed25519":
        _refuse()
    digest = hashlib.sha256(base64.b64decode(fields[1], validate=True)).digest()
    metadata = {
        "azure_resource_id": config["azure_resource_id"], "azure_vm_id": config["azure_vm_id"],
        "host_identity": os.uname().nodename,
        "host_key_fingerprint": "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("="),
    }
    provider = VastCliGuardProvider(SECRET_FILE, vast_bin=config["vast_bin"])
    return GuardGateway(GuardWorker(STATE_ROOT, provider), metadata, readiness_check=check_timer_ready)


def serve(arguments: list[str], original_command: str | None, stream: BinaryIO, *,
          gateway_factory=production_gateway, credential_path: Path = SECRET_FILE) -> tuple[int, bytes]:
    """Return one bounded response; never include provider/exception output."""
    try:
        if os.geteuid() != 0:
            _refuse()
        if arguments == ["install-credential"]:
            if original_command != "guardctl install-credential":
                _refuse()
            install_credential(stream, credential_path)
            return 0, _encode({"status": "CREDENTIAL_INSTALLED"})
        if arguments == ["tick-all"]:
            if original_command is not None:
                _refuse()
            if not gateway_factory().tick_all():
                _refuse()
            return 0, _encode({"status": "TICK_COMPLETE"})
        if arguments or original_command != "guardctl":
            _refuse()
        command, payload = validate_request(read_bounded(stream, MAX_REQUEST_BYTES))
        result = gateway_factory().dispatch(command, payload)
        return 0, _encode(result)
    except Exception:
        return 2, _encode({"status": "REFUSED", "error": "guard request refused"})


def _deadline(_signal: int, _frame: object) -> None:
    raise GuardProcessDeadline()


def main() -> int:
    os.umask(0o077)
    signal.signal(signal.SIGALRM, _deadline)
    signal.alarm(120 if sys.argv[1:] == ["tick-all"] else 15)
    response_started = False
    try:
        # Suppress accidental diagnostic output from the worker/provider boundary.
        with open(os.devnull, "w", encoding="utf-8") as sink, redirect_stdout(sink), redirect_stderr(sink):
            code, response = serve(sys.argv[1:], os.environ.get("SSH_ORIGINAL_COMMAND"), sys.stdin.buffer)
        # Keep the alarm active through output, without a Python buffer that
        # could block again during interpreter shutdown after a timeout.
        response_started = True
        while response:
            response = response[os.write(sys.stdout.fileno(), response):]
        return code
    except GuardProcessDeadline:
        # Handle termination only here. Never let a stalled stdout consumer
        # extend the process deadline, nor append an error to a partial reply.
        if not response_started:
            try:
                descriptor = sys.stdout.fileno()
                os.set_blocking(descriptor, False)
                os.write(descriptor, _encode({"status": "REFUSED", "error": "guard request refused"}))
            except OSError:
                pass
        return 2
    except OSError:
        # A disconnected output channel must not produce a traceback.
        return 2
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    raise SystemExit(main())
