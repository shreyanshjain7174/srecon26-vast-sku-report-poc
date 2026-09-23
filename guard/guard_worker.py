#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator, Mapping, Protocol


_GENESIS_HASH = "0" * 64
_NONCE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_TERMINAL = frozenset({"TEARDOWN_CONFIRMED", "ABSENT_OBSERVED", "OWNERSHIP_MISMATCH", "TEARDOWN_ERROR", "DISARMED"})


class GuardSafetyError(RuntimeError):
    """A request violated an ownership or durability invariant."""


@dataclass(frozen=True, slots=True)
class GuardedInstance:
    instance_id: int
    label: str


class GuardProvider(Protocol):
    def find_instances(self, label: str) -> tuple[GuardedInstance, ...]: ...
    def get_instance(self, instance_id: int) -> GuardedInstance | None: ...
    def destroy_exact(self, instance_id: int, expected_label: str) -> None: ...


@dataclass(frozen=True, slots=True)
class GuardReceipt:
    nonce: str
    label: str
    instance_id: int | None
    hard_deadline: datetime
    last_heartbeat: datetime
    status: str
    root_hash: str
    event_count: int


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise GuardSafetyError("timestamps must be timezone-aware UTC")
    return value.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse_stamp(value: object) -> datetime:
    return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def _canonical(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def _hash(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def load_provider_secret(path: Path, *, require_root_owner: bool = True) -> str:
    """Read a root-owned 0600 credential without exposing it in argv or logs."""
    resolved = Path(path).resolve(strict=True)
    stat = resolved.stat()
    if not resolved.is_file() or stat.st_mode & 0o077:
        raise GuardSafetyError("provider secret must be a regular 0600 file")
    if require_root_owner and stat.st_uid != 0:
        raise GuardSafetyError("provider secret must be root-owned")
    secret = resolved.read_text(encoding="utf-8").strip()
    if not secret:
        raise GuardSafetyError("provider secret is empty")
    return secret


def nonce_bound_label(run_label: str, nonce: str) -> str:
    """Return the provider label protocol used as the remote nonce binding.

    Vast records expose their label but not arbitrary guard metadata. The
    complete label is therefore the provider-observable ownership token.
    """
    if not run_label or "--nonce-" in run_label:
        raise GuardSafetyError("run label must be non-empty and must not contain the nonce delimiter")
    if not _NONCE.fullmatch(nonce):
        raise GuardSafetyError("nonce must be 8-128 URL-safe characters")
    return f"{run_label}--nonce-{nonce}"


def label_binds_nonce(label: str, nonce: str) -> bool:
    """True only for a complete label produced by :func:`nonce_bound_label`."""
    try:
        prefix = label.removesuffix(f"--nonce-{nonce}")
        return bool(prefix) and prefix != label and nonce_bound_label(prefix, nonce) == label
    except GuardSafetyError:
        return False


class VastCliGuardProvider:
    """Minimal guarded Vast CLI adapter using Vast's observable label field."""

    def __init__(self, secret_file: Path, *, vast_bin: str = "vastai", timeout_seconds: int = 20) -> None:
        self.secret_file = Path(secret_file)
        self.vast_bin = self._resolve_vastai(vast_bin)
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _resolve_vastai(vast_bin: str) -> str:
        if Path(vast_bin).name != "vastai":
            raise GuardSafetyError("guard requires the actual vastai binary, not a compatibility alias")
        resolved = shutil.which(vast_bin) if Path(vast_bin).parent == Path(".") else str(Path(vast_bin))
        if not resolved or not Path(resolved).is_file() or not os.access(resolved, os.X_OK):
            raise GuardSafetyError("vastai binary is missing or not executable")
        return str(Path(resolved).resolve())

    def _run(self, arguments: list[str]) -> object:
        secret = load_provider_secret(self.secret_file)
        environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "VAST_API_KEY": secret}
        completed = subprocess.run(
            [self.vast_bin, *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            env=environment,
        )
        return json.loads(completed.stdout)

    @staticmethod
    def _instance(raw: Mapping[str, object]) -> GuardedInstance | None:
        raw_id = raw.get("id", raw.get("instance_id"))
        label = raw.get("label")
        if raw_id is None or not isinstance(label, str):
            return None
        try:
            return GuardedInstance(int(raw_id), label)
        except (TypeError, ValueError):
            return None

    def find_instances(self, label: str) -> tuple[GuardedInstance, ...]:
        raw = self._run(["show", "instances", "--raw"])
        items = raw if isinstance(raw, list) else raw.get("instances", []) if isinstance(raw, dict) else []
        return tuple(instance for item in items if isinstance(item, dict) if (instance := self._instance(item)) is not None and instance.label == label)

    def get_instance(self, instance_id: int) -> GuardedInstance | None:
        raw = self._run(["show", "instance", str(instance_id), "--raw"])
        if not isinstance(raw, dict):
            return None
        return self._instance(raw)

    def destroy_exact(self, instance_id: int, expected_label: str) -> None:
        current = self.get_instance(instance_id)
        if current is None or current.label != expected_label:
            raise GuardSafetyError("provider target changed before destroy")
        self._run(["destroy", "instance", str(instance_id)])


class GuardWorker:
    """Durable, nonce-bound worker that can issue at most one destroy per target."""

    def __init__(
        self,
        root: Path,
        provider: GuardProvider,
        *,
        heartbeat_timeout: timedelta = timedelta(minutes=2),
        require_root_owner: bool = True,
    ) -> None:
        if heartbeat_timeout <= timedelta(0):
            raise ValueError("heartbeat_timeout must be positive")
        self.root = Path(root).resolve()
        self.provider = provider
        self.heartbeat_timeout = heartbeat_timeout
        self.require_root_owner = require_root_owner
        self._ensure_root()

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        stat = self.root.stat()
        if not self.root.is_dir() or stat.st_mode & 0o077:
            raise GuardSafetyError("guard root must be a private directory")
        if self.require_root_owner and stat.st_uid != 0:
            raise GuardSafetyError("guard root must be root-owned")

    @staticmethod
    def _check_nonce(nonce: str) -> None:
        if not _NONCE.fullmatch(nonce):
            raise GuardSafetyError("nonce must be 8-128 URL-safe characters")

    def _run_dir(self, nonce: str) -> Path:
        self._check_nonce(nonce)
        return self.root / nonce

    @contextmanager
    def _locked(self, nonce: str) -> Iterator[Path]:
        directory = self._run_dir(nonce)
        directory.mkdir(mode=0o700, exist_ok=True)
        lock = directory / "guard.lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield directory
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @staticmethod
    def _durable_replace(path: Path, payload: Mapping[str, object]) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, _canonical(payload) + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @staticmethod
    def _journal_root(directory: Path) -> tuple[str, int, list[Mapping[str, object]]]:
        journal = directory / "journal.ndjson"
        if not journal.exists():
            return _GENESIS_HASH, 0, []
        root = _GENESIS_HASH
        records: list[Mapping[str, object]] = []
        for sequence, line in enumerate(journal.read_bytes().splitlines(), start=1):
            try:
                record = json.loads(line)
                previous = record["previous_hash"]
                event_hash = record["event_hash"]
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise GuardSafetyError("guard journal is corrupt") from exc
            unsigned = {key: value for key, value in record.items() if key != "event_hash"}
            if record.get("sequence") != sequence or previous != root or event_hash != _hash(unsigned):
                raise GuardSafetyError("guard journal hash chain is corrupt")
            root = str(event_hash)
            records.append(record)
        return root, len(records), records

    def _append_event(self, directory: Path, state: dict[str, object], event: str, now: datetime, payload: Mapping[str, object] | None = None) -> None:
        root, count, _ = self._journal_root(directory)
        record: dict[str, object] = {
            "sequence": count + 1,
            "event": event,
            "nonce": state["nonce"],
            "wall_time": _stamp(now),
            "previous_hash": root,
            "payload": dict(payload or {}),
        }
        record["event_hash"] = _hash(record)
        with (directory / "journal.ndjson").open("ab", buffering=0) as handle:
            handle.write(_canonical(record) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        state["root_hash"] = record["event_hash"]

    def _load(self, directory: Path) -> dict[str, object]:
        path = directory / "state.json"
        if not path.exists():
            raise GuardSafetyError("guard has not been armed for this nonce")
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                raise ValueError
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise GuardSafetyError("guard state is corrupt") from exc
        root, count, records = self._journal_root(directory)
        state["root_hash"] = root
        state["event_count"] = count
        # A crash after journal append but before the state snapshot must never
        # reissue a destructive call. Journal intent is the durable authority.
        events = {str(record["event"]) for record in records}
        if "disarmed" in events:
            state["status"] = "DISARMED"
        elif "ownership_mismatch" in events:
            state["status"] = "OWNERSHIP_MISMATCH"
        elif "teardown_confirmed" in events:
            state["status"] = "TEARDOWN_CONFIRMED"
        elif "teardown_error" in events:
            state["status"] = "TEARDOWN_ERROR"
        elif "teardown_requested" in events:
            state["status"] = "TEARDOWN_REQUESTED"
        return state

    @staticmethod
    def _receipt(state: Mapping[str, object]) -> GuardReceipt:
        return GuardReceipt(
            nonce=str(state["nonce"]),
            label=str(state["label"]),
            instance_id=int(state["instance_id"]) if state.get("instance_id") is not None else None,
            hard_deadline=_parse_stamp(state["hard_deadline"]),
            last_heartbeat=_parse_stamp(state["last_heartbeat"]),
            status=str(state["status"]),
            root_hash=str(state["root_hash"]),
            event_count=int(state["event_count"]),
        )

    def _persist(self, directory: Path, state: dict[str, object]) -> GuardReceipt:
        root, count, _ = self._journal_root(directory)
        state["root_hash"] = root
        state["event_count"] = count
        self._durable_replace(directory / "state.json", state)
        return self._receipt(state)

    def preflight(self) -> dict[str, str]:
        script_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        return {"host_identity": os.uname().nodename, "script_hash": script_hash, "root_hash": _GENESIS_HASH, "status": "READY"}

    def arm(self, instance_id: int | None, label: str, nonce: str, hard_deadline: datetime, *, now: datetime) -> GuardReceipt:
        self._check_nonce(nonce)
        if not label_binds_nonce(label, nonce):
            raise GuardSafetyError("label must use the exact nonce-bound provider label protocol")
        if instance_id is not None and instance_id <= 0:
            raise GuardSafetyError("instance id must be positive")
        now, deadline = _utc(now), _utc(hard_deadline)
        if deadline <= now:
            raise GuardSafetyError("hard deadline must be in the future")
        with self._locked(nonce) as directory:
            if (directory / "state.json").exists():
                state = self._load(directory)
                immutable = (state["instance_id"], state["label"], state["nonce"], state["hard_deadline"])
                supplied = (instance_id, label, nonce, _stamp(deadline))
                if immutable != supplied:
                    raise GuardSafetyError("guard target and deadline are immutable")
                return self._receipt(state)
            state: dict[str, object] = {
                "instance_id": instance_id,
                "label": label,
                "nonce": nonce,
                "hard_deadline": _stamp(deadline),
                "last_heartbeat": _stamp(now),
                "status": "ARMED",
                "root_hash": _GENESIS_HASH,
                "event_count": 0,
            }
            self._persist(directory, state)
            self._append_event(directory, state, "armed", now, {"instance_id": instance_id, "label": label})
            return self._persist(directory, state)

    def heartbeat(self, nonce: str, *, now: datetime) -> GuardReceipt:
        now = _utc(now)
        with self._locked(nonce) as directory:
            state = self._load(directory)
            if str(state["status"]) in _TERMINAL or str(state["status"]) == "TEARDOWN_REQUESTED":
                raise GuardSafetyError("heartbeat is forbidden after teardown authority activates")
            state["last_heartbeat"] = _stamp(now)
            self._append_event(directory, state, "heartbeat", now)
            return self._persist(directory, state)

    def status(self, nonce: str) -> GuardReceipt:
        with self._locked(nonce) as directory:
            return self._receipt(self._load(directory))

    def _ownership_mismatch(self, directory: Path, state: dict[str, object], now: datetime, reason: str) -> GuardReceipt:
        state["status"] = "OWNERSHIP_MISMATCH"
        self._append_event(directory, state, "ownership_mismatch", now, {"reason": reason})
        return self._persist(directory, state)

    def _reconcile(self, directory: Path, state: dict[str, object], now: datetime) -> GuardedInstance | None:
        instance_id = state.get("instance_id")
        if instance_id is None:
            matches = self.provider.find_instances(str(state["label"]))
            if len(matches) == 0:
                state["status"] = "AWAITING_INSTANCE"
                self._append_event(directory, state, "awaiting_instance", now)
                self._persist(directory, state)
                return None
            if len(matches) != 1:
                self._ownership_mismatch(directory, state, now, "multiple exact nonce-bound matches")
                return None
            instance = matches[0]
            state["instance_id"] = instance.instance_id
            self._append_event(directory, state, "instance_reconciled", now, {"instance_id": instance.instance_id})
            self._persist(directory, state)
            return instance
        instance = self.provider.get_instance(int(instance_id))
        if instance is None:
            state["status"] = "ABSENT_OBSERVED"
            self._append_event(directory, state, "absence_observed", now, {"instance_id": instance_id})
            self._persist(directory, state)
            return None
        if instance.label != state["label"]:
            self._ownership_mismatch(directory, state, now, "exact ID no longer has the nonce-bound label")
            return None
        return instance

    def tick(self, nonce: str, *, now: datetime) -> GuardReceipt:
        now = _utc(now)
        with self._locked(nonce) as directory:
            state = self._load(directory)
            if str(state["status"]) in _TERMINAL or str(state["status"]) == "TEARDOWN_REQUESTED":
                return self._receipt(state)
            deadline = _parse_stamp(state["hard_deadline"])
            last_heartbeat = _parse_stamp(state["last_heartbeat"])
            if now < deadline and now - last_heartbeat <= self.heartbeat_timeout:
                return self._receipt(state)
            instance = self._reconcile(directory, state, now)
            if instance is None:
                return self._receipt(self._load(directory))
            self._append_event(directory, state, "teardown_requested", now, {"instance_id": instance.instance_id, "reason": "deadline" if now >= deadline else "heartbeat_loss"})
            state["status"] = "TEARDOWN_REQUESTED"
            self._persist(directory, state)
            try:
                self.provider.destroy_exact(instance.instance_id, instance.label)
            except Exception as exc:
                state["status"] = "TEARDOWN_ERROR"
                self._append_event(directory, state, "teardown_error", now, {"exception": type(exc).__name__})
                return self._persist(directory, state)
            state["status"] = "TEARDOWN_CONFIRMED"
            self._append_event(directory, state, "teardown_confirmed", now, {"instance_id": instance.instance_id})
            return self._persist(directory, state)

    def anchor(self, nonce: str, root_hash: str, *, now: datetime) -> GuardReceipt:
        if not re.fullmatch(r"[0-9a-f]{64}", root_hash):
            raise GuardSafetyError("anchor must be a sha256 root hash")
        with self._locked(nonce) as directory:
            state = self._load(directory)
            self._append_event(directory, state, "controller_root_anchored", _utc(now), {"controller_root_hash": root_hash})
            return self._persist(directory, state)

    def disarm_after_absence(self, nonce: str, absence_root_hash: str, *, now: datetime) -> GuardReceipt:
        if not re.fullmatch(r"[0-9a-f]{64}", absence_root_hash):
            raise GuardSafetyError("absence proof must be a sha256 root hash")
        with self._locked(nonce) as directory:
            state = self._load(directory)
            if str(state["status"]) not in {"TEARDOWN_CONFIRMED", "ABSENT_OBSERVED"}:
                raise GuardSafetyError("cannot disarm before teardown or observed absence")
            state["status"] = "DISARMED"
            self._append_event(directory, state, "disarmed", _utc(now), {"absence_root_hash": absence_root_hash})
            return self._persist(directory, state)


def _main() -> int:
    parser = argparse.ArgumentParser(description="SRECon26 independent deadline guard")
    parser.add_argument("command", choices=("preflight", "arm", "heartbeat", "status", "tick", "anchor", "disarm-after-absence"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--secret-file", type=Path, required=True)
    parser.add_argument("--vast-bin", default="vastai")
    parser.add_argument("--nonce")
    parser.add_argument("--label")
    parser.add_argument("--instance-id", type=int)
    parser.add_argument("--hard-deadline")
    parser.add_argument("--root-hash")
    args = parser.parse_args()
    now = datetime.now(UTC)
    provider = VastCliGuardProvider(args.secret_file, vast_bin=args.vast_bin)
    worker = GuardWorker(args.root, provider)
    if args.command == "preflight":
        payload: object = worker.preflight()
    else:
        if not args.nonce:
            parser.error("--nonce is required for this command")
        if args.command == "arm":
            if not args.label or not args.hard_deadline:
                parser.error("arm requires --label and --hard-deadline")
            payload = worker.arm(args.instance_id, args.label, args.nonce, _parse_stamp(args.hard_deadline), now=now)
        elif args.command == "heartbeat":
            payload = worker.heartbeat(args.nonce, now=now)
        elif args.command == "status":
            payload = worker.status(args.nonce)
        elif args.command == "tick":
            payload = worker.tick(args.nonce, now=now)
        elif args.command == "anchor":
            if not args.root_hash:
                parser.error("anchor requires --root-hash")
            payload = worker.anchor(args.nonce, args.root_hash, now=now)
        else:
            if not args.root_hash:
                parser.error("disarm-after-absence requires --root-hash")
            payload = worker.disarm_after_absence(args.nonce, args.root_hash, now=now)
    print(json.dumps(asdict(payload) if hasattr(payload, "__dataclass_fields__") else payload, default=_stamp, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except (GuardSafetyError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(f"guard refused request: {exc}", file=sys.stderr)
        raise SystemExit(2)
