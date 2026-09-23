from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Protocol


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
    def preflight_authenticated_session(self) -> None: ...
    def preflight_exact_instance(self, instance_id: int, label: str) -> None: ...
    def capture_before(self, fault: FaultRecord) -> Path: ...
    def submit(self, fault: FaultRecord) -> bool: ...
    def capture_after(self, receipt: ReportReceipt) -> Path: ...


class ReportGate:
    """Run the report sequence only while its immutable teardown window remains.

    ``report_start_by`` is a hard cutoff, not merely the time at which report
    work should begin.  Once it is reached, the caller must retain the window
    for exact provider teardown and absence verification instead of starting
    (or continuing) a browser operation.
    """

    def __init__(self, adapter: DesktopReportAdapter, *, now: Callable[[], datetime] | None = None) -> None:
        self.adapter = adapter
        self._now = now or (lambda: datetime.now(UTC))

    def _require_before_cutoff(self, report_start_by: datetime) -> None:
        if self._now() >= report_start_by:
            raise ReportRejected("report cutoff reached; preserving teardown window")

    def preflight_authenticated_session(self) -> None:
        try:
            self.adapter.preflight_authenticated_session()
        except (AssertionError, ValueError) as exc:
            raise ReportRejected("report adapter lacks an authenticated provider session") from exc

    def handle(self, fault: FaultRecord, report_start_by: datetime) -> ReportReceipt:
        if fault.instance_id <= 0 or not fault.label or not fault.nonce or report_start_by.tzinfo is None:
            raise ReportRejected("report target lacks exact ownership evidence")
        try:
            self._require_before_cutoff(report_start_by)
            self.adapter.preflight_exact_instance(fault.instance_id, fault.label)
            self._require_before_cutoff(report_start_by)
            before = self.adapter.capture_before(fault)
            self._require_before_cutoff(report_start_by)
            confirmed = self.adapter.submit(fault)
        except ReportRejected:
            raise
        except (AssertionError, ValueError) as exc:
            raise ReportRejected("report adapter rejected exact target") from exc
        receipt = ReportReceipt(confirmed=confirmed, before_path=before, after_path=None)
        self._require_before_cutoff(report_start_by)
        after = self.adapter.capture_after(receipt)
        return ReportReceipt(confirmed=confirmed, before_path=before, after_path=after)
