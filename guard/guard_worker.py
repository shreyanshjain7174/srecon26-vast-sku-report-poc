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
_TERMINAL = frozenset({"TEARDOWN_CONFIRMED", "ABSENT_OBSERVED", "OWNERSHIP_MISMATCH", "TEARDOWN_UNCONFIRMED", "DISARMED"})
_MAX_POST_DEADLINE_WINDOW = timedelta(minutes=10)


class GuardSafetyError(RuntimeError):
    """A request violated an ownership or durability invariant."""


@dataclass(frozen=True, slots=True)
class GuardedInstance:
    instance_id: int
    label: str


class GuardProvider(Protocol):
    def instance_inventory_count(self) -> int: ...
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

    def __init__(self, secret_file: Path, *, vast_bin: str = "vastai", timeout_seconds: int = 10) -> None:
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be between 1 and 60")
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
    def _instance_listing(raw: object) -> list[object]:
        """Return a verified provider inventory payload.

        Treat an unknown response shape as unsafe: an authorization error or
        API change must never be mistaken for an empty account.
        """
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict) and isinstance(raw.get("instances"), list):
            return raw["instances"]
        raise GuardSafetyError("provider instance listing has an unexpected response")

    def instance_inventory_count(self) -> int:
        """Authenticate through Vast's read-only inventory endpoint."""
        return len(self._instance_listing(self._run(["show", "instances", "--raw"])))

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
        items = self._instance_listing(self._run(["show", "instances", "--raw"]))
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
    """Durable, nonce-bound worker with bounded, exact-target teardown retries.

    A provider destroy can time out after accepting the request.  The journal
    therefore never treats a destroy request as proof of teardown: every retry
    first re-reads the exact numeric ID and nonce-bound label, and only a
    provider read which reports that ID absent is confirmation.  Retrying ends
    at a fixed, bounded interval after the immutable deadline.
    """

    def __init__(
        self,
        root: Path,
        provider: GuardProvider,
        *,
        heartbeat_timeout: timedelta = timedelta(minutes=2),
        post_deadline_window: timedelta = timedelta(minutes=5),
        require_root_owner: bool = True,
    ) -> None:
        if heartbeat_timeout <= timedelta(0):
            raise ValueError("heartbeat_timeout must be positive")
        if not timedelta(seconds=1) <= post_deadline_window <= _MAX_POST_DEADLINE_WINDOW:
            raise ValueError("post_deadline_window must be between 1 second and 10 minutes")
        self.root = Path(root).resolve()
        self.provider = provider
        self.heartbeat_timeout = heartbeat_timeout
        self.post_deadline_window = post_deadline_window
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
        elif "teardown_unconfirmed" in events:
            state["status"] = "TEARDOWN_UNCONFIRMED"
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

    def provider_preflight(self) -> dict[str, object]:
        """Fail closed unless the credential can list a completely empty account.

        This deliberately performs no guard state or provider mutation. It is
        the mandatory last check before a workflow may arm a paid-run watcher.
        """
        inventory_count = self.provider.instance_inventory_count()
        if inventory_count != 0:
            raise GuardSafetyError("provider account has existing instances; refusing to arm")
        return {"instance_count": inventory_count, "status": "PROVIDER_PREFLIGHT_READY"}

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

    def _teardown_error(self, directory: Path, state: dict[str, object], now: datetime, reason: str) -> GuardReceipt:
        """Record a retryable failure without treating it as teardown proof."""
        state["status"] = "TEARDOWN_ERROR"
        self._append_event(directory, state, "teardown_error", now, {"reason": reason})
        return self._persist(directory, state)

    def _teardown_unconfirmed(self, directory: Path, state: dict[str, object], now: datetime, reason: str) -> GuardReceipt:
        """Stop destructive retries only after the fixed post-deadline bound."""
        state["status"] = "TEARDOWN_UNCONFIRMED"
        self._append_event(directory, state, "teardown_unconfirmed", now, {"reason": reason})
        return self._persist(directory, state)

    def _absence_observed(self, directory: Path, state: dict[str, object], now: datetime, *, target: str) -> GuardReceipt:
        state["status"] = "ABSENT_OBSERVED"
        self._append_event(directory, state, "absence_observed", now, {"target": target})
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
            state["status"] = "ARMED"
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
            if str(state["status"]) in _TERMINAL:
                return self._receipt(state)
            deadline = _parse_stamp(state["hard_deadline"])
            last_heartbeat = _parse_stamp(state["last_heartbeat"])
            # A pre-create arm intentionally has no instance ID. Reconcile it
            # even while healthy so normal lifecycle operations bind the exact
            # nonce-bearing label before a later deadline/absence decision.
            if state.get("instance_id") is None:
                instance = self._reconcile(directory, state, now)
                if instance is None:
                    if now >= deadline:
                        # A label query that returns no exact match is the only
                        # provider-confirmed absence available before an ID was
                        # ever bound.  It is terminal only at the deadline:
                        # before then a delayed create remains possible.
                        state = self._load(directory)
                        if str(state["status"]) == "AWAITING_INSTANCE":
                            return self._absence_observed(directory, state, now, target="nonce-bound label")
                    return self._receipt(self._load(directory))
            if now < deadline and now - last_heartbeat <= self.heartbeat_timeout:
                return self._receipt(state)
            # Once the immutable deadline has passed, provider confirmation is
            # the only successful terminal state.  Do one final read at the
            # bound, but never issue a fresh destroy outside it.
            if now > deadline + self.post_deadline_window:
                try:
                    instance = self._reconcile(directory, state, now)
                except Exception as exc:
                    return self._teardown_unconfirmed(directory, state, now, f"final_reconcile_{type(exc).__name__}")
                if instance is None:
                    return self._receipt(self._load(directory))
                return self._teardown_unconfirmed(directory, state, now, "provider still reports exact target after post-deadline window")
            try:
                instance = self._reconcile(directory, state, now)
            except Exception as exc:
                return self._teardown_error(directory, state, now, f"reconcile_{type(exc).__name__}")
            if instance is None:
                return self._receipt(self._load(directory))
            self._append_event(directory, state, "teardown_requested", now, {"instance_id": instance.instance_id, "reason": "deadline" if now >= deadline else "heartbeat_loss"})
            state["status"] = "TEARDOWN_REQUESTED"
            self._persist(directory, state)
            try:
                self.provider.destroy_exact(instance.instance_id, instance.label)
            except Exception as exc:
                return self._teardown_error(directory, state, now, f"destroy_{type(exc).__name__}")
            # A successful CLI exit is not sufficient: the provider must
            # independently confirm that this exact ID has disappeared.
            try:
                absent = self.provider.get_instance(instance.instance_id)
            except Exception as exc:
                return self._teardown_error(directory, state, now, f"absence_reconcile_{type(exc).__name__}")
            if absent is not None:
                if absent.label != instance.label:
                    return self._ownership_mismatch(directory, state, now, "exact ID changed label after destroy request")
                return self._teardown_error(directory, state, now, "provider still reports exact target after destroy request")
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
    parser.add_argument("command", choices=("preflight", "provider-preflight", "arm", "heartbeat", "status", "tick", "anchor", "disarm-after-absence"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--secret-file", type=Path, required=True)
    parser.add_argument("--vast-bin", default=os.environ.get("SRECON26_GUARD_VAST_BIN", "vastai"))
    parser.add_argument("--heartbeat-timeout-seconds", type=int, default=120)
    parser.add_argument("--post-deadline-window-seconds", type=int, default=300)
    parser.add_argument("--nonce")
    parser.add_argument("--label")
    parser.add_argument("--instance-id", type=int)
    parser.add_argument("--hard-deadline")
    parser.add_argument("--root-hash")
    args = parser.parse_args()
    if not 1 <= args.heartbeat_timeout_seconds <= 600:
        parser.error("--heartbeat-timeout-seconds must be between 1 and 600")
    if not 1 <= args.post_deadline_window_seconds <= 600:
        parser.error("--post-deadline-window-seconds must be between 1 and 600")
    now = datetime.now(UTC)
    provider = VastCliGuardProvider(args.secret_file, vast_bin=args.vast_bin)
    worker = GuardWorker(
        args.root,
        provider,
        heartbeat_timeout=timedelta(seconds=args.heartbeat_timeout_seconds),
        post_deadline_window=timedelta(seconds=args.post_deadline_window_seconds),
    )
    if args.command == "preflight":
        payload: object = worker.preflight()
    elif args.command == "provider-preflight":
        payload = worker.provider_preflight()
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
