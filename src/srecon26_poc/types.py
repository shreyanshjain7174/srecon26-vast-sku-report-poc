from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Mapping, TypeAlias


JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]


class RunState(StrEnum):
    NEW = "NEW"
    OFFLINE_VALIDATED = "OFFLINE_VALIDATED"
    BUDGET_RESERVED = "BUDGET_RESERVED"
    OFFER_PINNED = "OFFER_PINNED"
    REPORT_ADAPTER_READY = "REPORT_ADAPTER_READY"
    GUARD_ARMED = "GUARD_ARMED"
    CREATE_REQUESTED = "CREATE_REQUESTED"
    CREATED_VERIFYING = "CREATED_VERIFYING"
    RUNNING_CANARY = "RUNNING_CANARY"
    CAPTURING_FAULT = "CAPTURING_FAULT"
    REPORTING_FAULT = "REPORTING_FAULT"
    COLLECTING = "COLLECTING"
    DESTROYING = "DESTROYING"
    ABSENCE_VERIFYING = "ABSENCE_VERIFYING"
    TERMINAL = "TERMINAL"


class TerminalStatus(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED_SAFE = "FAILED_SAFE"
    REPORT_UNCONFIRMED = "REPORT_UNCONFIRMED"
    HALTED = "HALTED"


class FaultClass(StrEnum):
    PROVIDER_FAULT_CONFIRMED = "PROVIDER_FAULT_CONFIRMED"
    DIAGNOSIS_UNRESOLVED = "DIAGNOSIS_UNRESOLVED"


@dataclass(frozen=True, slots=True)
class RunIdentity:
    run_id: str
    label: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TransitionEvent:
    sequence: int
    run_id: str
    label: str
    from_state: RunState
    to_state: RunState
    event_type: str
    payload: Mapping[str, JSONValue]
    wall_time: datetime
    monotonic_ns: int
    previous_hash: str
    event_hash: str

