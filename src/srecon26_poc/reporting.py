from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


class ReportRejected(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FaultRecord:
    instance_id: int
    label: str
    nonce: str
    description: str


@dataclass(frozen=True, slots=True)
class ReportReceipt:
    confirmed: bool
    before_path: Path
    after_path: Path | None


class DesktopReportAdapter(Protocol):
    def preflight_exact_instance(self, instance_id: int, label: str) -> None: ...
    def capture_before(self, fault: FaultRecord) -> Path: ...
    def submit(self, fault: FaultRecord) -> bool: ...
    def capture_after(self, receipt: ReportReceipt) -> Path: ...


class ReportGate:
    def __init__(self, adapter: DesktopReportAdapter) -> None:
        self.adapter = adapter

    def handle(self, fault: FaultRecord, report_start_by: datetime) -> ReportReceipt:
        if fault.instance_id <= 0 or not fault.label or not fault.nonce or report_start_by.tzinfo is None:
            raise ReportRejected("report target lacks exact ownership evidence")
        try:
            self.adapter.preflight_exact_instance(fault.instance_id, fault.label)
            before = self.adapter.capture_before(fault)
            confirmed = self.adapter.submit(fault)
        except (AssertionError, ValueError) as exc:
            raise ReportRejected("report adapter rejected exact target") from exc
        receipt = ReportReceipt(confirmed=confirmed, before_path=before, after_path=None)
        after = self.adapter.capture_after(receipt)
        return ReportReceipt(confirmed=confirmed, before_path=before, after_path=after)
