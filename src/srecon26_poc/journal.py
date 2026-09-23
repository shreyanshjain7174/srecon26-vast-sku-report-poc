from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping

from .types import JSONValue, RunIdentity, RunState, TransitionEvent


class InvalidJournal(ValueError):
    """Raised when persisted journal evidence is corrupt or unsafe to replay."""


class InvalidTransition(ValueError):
    """Raised when a lifecycle transition is not part of the approved state machine."""


_GENESIS_HASH = "0" * 64
_ALLOWED: dict[RunState, frozenset[RunState]] = {
    RunState.NEW: frozenset({RunState.OFFLINE_VALIDATED, RunState.TERMINAL}),
    RunState.OFFLINE_VALIDATED: frozenset({RunState.BUDGET_RESERVED, RunState.TERMINAL}),
    RunState.BUDGET_RESERVED: frozenset({RunState.OFFER_PINNED, RunState.TERMINAL}),
    RunState.OFFER_PINNED: frozenset({RunState.REPORT_ADAPTER_READY, RunState.TERMINAL}),
    RunState.REPORT_ADAPTER_READY: frozenset({RunState.GUARD_ARMED, RunState.TERMINAL}),
    RunState.GUARD_ARMED: frozenset({RunState.CREATE_REQUESTED, RunState.TERMINAL}),
    RunState.CREATE_REQUESTED: frozenset({RunState.CREATED_VERIFYING, RunState.TERMINAL}),
    RunState.CREATED_VERIFYING: frozenset({RunState.RUNNING_CANARY, RunState.CAPTURING_FAULT, RunState.DESTROYING, RunState.TERMINAL}),
    RunState.RUNNING_CANARY: frozenset({RunState.CAPTURING_FAULT, RunState.COLLECTING, RunState.DESTROYING, RunState.TERMINAL}),
    RunState.CAPTURING_FAULT: frozenset({RunState.REPORTING_FAULT, RunState.DESTROYING, RunState.TERMINAL}),
    RunState.REPORTING_FAULT: frozenset({RunState.DESTROYING, RunState.TERMINAL}),
    RunState.COLLECTING: frozenset({RunState.DESTROYING, RunState.TERMINAL}),
    RunState.DESTROYING: frozenset({RunState.ABSENCE_VERIFYING, RunState.TERMINAL}),
    RunState.ABSENCE_VERIFYING: frozenset({RunState.TERMINAL}),
    RunState.TERMINAL: frozenset(),
}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("wall_time must be timezone-aware UTC")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def _digest(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


class RunJournal:
    """Durable append-only, hash-chained lifecycle journal."""

    def __init__(self, directory: Path, identity: RunIdentity, events: tuple[TransitionEvent, ...]) -> None:
        self.directory = directory
        self.path = directory / "journal.ndjson"
        self._identity = identity
        self._events = events

    @classmethod
    def create(cls, path: Path, identity: RunIdentity) -> "RunJournal":
        if not identity.run_id or not identity.label:
            raise ValueError("run_id and label are required")
        _utc(identity.created_at)
        directory = Path(path)
        directory.mkdir(parents=True, exist_ok=True)
        identity_path = directory / "identity.json"
        if identity_path.exists() or (directory / "journal.ndjson").exists():
            raise FileExistsError("journal directory already contains a journal")
        cls._durable_write(identity_path, _canonical({"run_id": identity.run_id, "label": identity.label, "created_at": _timestamp(identity.created_at)}) + b"\n")
        cls._durable_write(directory / "journal.ndjson", b"")
        return cls(directory, identity, ())

    @classmethod
    def open(cls, path: Path) -> "RunJournal":
        directory = Path(path)
        try:
            raw_identity = json.loads((directory / "identity.json").read_text(encoding="utf-8"))
            identity = RunIdentity(
                run_id=raw_identity["run_id"],
                label=raw_identity["label"],
                created_at=datetime.fromisoformat(raw_identity["created_at"].replace("Z", "+00:00")),
            )
            _utc(identity.created_at)
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise InvalidJournal("invalid run identity") from exc
        journal_path = directory / "journal.ndjson"
        if not journal_path.exists():
            raise InvalidJournal("journal file is missing")
        raw = journal_path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            last_newline = raw.rfind(b"\n")
            raw = raw[: last_newline + 1] if last_newline >= 0 else b""
            cls._durable_write(journal_path, raw)
        events: list[TransitionEvent] = []
        previous_state = RunState.NEW
        previous_hash = _GENESIS_HASH
        previous_monotonic = -1
        for index, line in enumerate(raw.splitlines(), start=1):
            try:
                record = json.loads(line)
                event = cls._event_from_record(record)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise InvalidJournal(f"invalid journal record {index}") from exc
            cls._validate_event(event, identity, index, previous_state, previous_hash, previous_monotonic)
            events.append(event)
            previous_state = event.to_state
            previous_hash = event.event_hash
            previous_monotonic = event.monotonic_ns
        return cls(directory, identity, tuple(events))

    @staticmethod
    def _durable_write(path: Path, data: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _event_from_record(record: Mapping[str, object]) -> TransitionEvent:
        return TransitionEvent(
            sequence=int(record["sequence"]),
            run_id=str(record["run_id"]),
            label=str(record["label"]),
            from_state=RunState(str(record["from_state"])),
            to_state=RunState(str(record["to_state"])),
            event_type=str(record["event_type"]),
            payload=record["payload"],  # type: ignore[arg-type]
            wall_time=datetime.fromisoformat(str(record["wall_time"]).replace("Z", "+00:00")),
            monotonic_ns=int(record["monotonic_ns"]),
            previous_hash=str(record["previous_hash"]),
            event_hash=str(record["event_hash"]),
        )

    @staticmethod
    def _record(event: TransitionEvent) -> dict[str, object]:
        return {
            "sequence": event.sequence,
            "run_id": event.run_id,
            "label": event.label,
            "from_state": event.from_state.value,
            "to_state": event.to_state.value,
            "event_type": event.event_type,
            "payload": event.payload,
            "wall_time": _timestamp(event.wall_time),
            "monotonic_ns": event.monotonic_ns,
            "previous_hash": event.previous_hash,
            "event_hash": event.event_hash,
        }

    @classmethod
    def _validate_event(cls, event: TransitionEvent, identity: RunIdentity, sequence: int, current: RunState, previous_hash: str, previous_monotonic: int) -> None:
        if event.sequence != sequence or event.run_id != identity.run_id or event.label != identity.label:
            raise InvalidJournal("sequence or run identity mismatch")
        if event.from_state is not current or event.to_state not in _ALLOWED[current]:
            raise InvalidJournal("invalid state transition")
        if event.monotonic_ns <= previous_monotonic:
            raise InvalidJournal("monotonic time did not advance")
        _utc(event.wall_time)
        if event.previous_hash != previous_hash:
            raise InvalidJournal("previous hash mismatch")
        record = cls._record(event)
        expected = _digest({key: value for key, value in record.items() if key != "event_hash"})
        if event.event_hash != expected:
            raise InvalidJournal("event hash mismatch")

    def append(self, to_state: RunState, event_type: str, payload: Mapping[str, JSONValue], wall_time: datetime, monotonic_ns: int) -> TransitionEvent:
        current = self.state()
        if to_state not in _ALLOWED[current]:
            raise InvalidTransition(f"{current.value} cannot transition to {to_state.value}")
        event = TransitionEvent(
            sequence=len(self._events) + 1,
            run_id=self._identity.run_id,
            label=self._identity.label,
            from_state=current,
            to_state=to_state,
            event_type=event_type,
            payload=dict(payload),
            wall_time=_utc(wall_time),
            monotonic_ns=monotonic_ns,
            previous_hash=self._events[-1].event_hash if self._events else _GENESIS_HASH,
            event_hash="",
        )
        unsigned = self._record(event)
        digest = _digest({key: value for key, value in unsigned.items() if key != "event_hash"})
        event = TransitionEvent(**{**asdict(event), "event_hash": digest})
        line = _canonical(self._record(event)) + b"\n"
        with self.path.open("ab", buffering=0) as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        self._events = (*self._events, event)
        return event

    def record_halt(self, exception_class: str, message: str, traceback_hash: str, wall_time: datetime, monotonic_ns: int) -> TransitionEvent:
        return self.append(
            RunState.TERMINAL,
            "HALT",
            {"exception_class": exception_class, "message": message, "traceback_hash": traceback_hash},
            wall_time,
            monotonic_ns,
        )

    def state(self) -> RunState:
        return self._events[-1].to_state if self._events else RunState.NEW

    def events(self) -> tuple[TransitionEvent, ...]:
        return self._events
