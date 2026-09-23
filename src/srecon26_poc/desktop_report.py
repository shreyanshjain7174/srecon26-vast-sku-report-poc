from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from .reporting import FaultRecord
from .types import RunState


class ReportTargetMismatch(ValueError):
    """The browser fixture does not prove the exact owned report target."""


# The implementation name keeps report-target failures distinguishable while
# preserving the plan's concise public exception spelling.
TargetMismatch = ReportTargetMismatch


class UnconfirmedFault(ValueError):
    """Only a frozen, provider-confirmed fault is reportable."""


class ReportStartExpired(ValueError):
    """The immutable report-start deadline has already elapsed."""


class ReportConfirmationTimeout(TimeoutError):
    """The fixture click did not receive a bounded confirmation."""


class LiveSubmissionBlocked(ValueError):
    """Live browser submission requires the later lifecycle integration."""


@dataclass(frozen=True, slots=True)
class ConfirmedFault:
    record: FaultRecord
    classification: str = "PROVIDER_FAULT_CONFIRMED"

    @classmethod
    def from_record(cls, record: FaultRecord) -> "ConfirmedFault":
        return cls(record)

    def require_confirmed(self) -> None:
        if self.classification != "PROVIDER_FAULT_CONFIRMED":
            raise UnconfirmedFault("fixture reporting requires PROVIDER_FAULT_CONFIRMED")
        if self.record.instance_id <= 0 or not self.record.label or not self.record.nonce or not self.record.description:
            raise UnconfirmedFault("confirmed fault lacks exact ownership or evidence")


@dataclass(frozen=True, slots=True)
class FixtureReportReceipt:
    status: str
    instance_id: int
    label: str
    nonce: str
    before_path: Path
    after_path: Path
    provider_request_count: int = 0


class _Locator(Protocol):
    def get_attribute(self, name: str) -> str | None: ...
    def text_content(self) -> str | None: ...
    def click(self) -> None: ...
    def wait_for(self, *, state: str, timeout: int) -> None: ...


class FixturePage(Protocol):
    def locator(self, selector: str) -> _Locator: ...
    def screenshot(self, *, path: str) -> None: ...


class FixtureReportDriver:
    """Exact-target fixture driver; it has no provider credential or network path."""

    confirmation_timeout_ms = 60_000

    def __init__(self, evidence_dir: Path) -> None:
        self.evidence_dir = Path(evidence_dir)

    @staticmethod
    def _exact(page: FixturePage, instance_id: int, label: str, fault: ConfirmedFault) -> None:
        fault.require_confirmed()
        if fault.record.instance_id != instance_id or fault.record.label != label:
            raise ReportTargetMismatch("requested target differs from frozen fault record")
        target = page.locator("[data-instance]")
        values = (
            target.get_attribute("data-instance-id"),
            target.get_attribute("data-label"),
            target.get_attribute("data-nonce"),
        )
        expected = (str(instance_id), label, fault.record.nonce)
        if values != expected:
            raise ReportTargetMismatch("fixture target instance, label, or nonce does not match frozen fault")
        visible = (
            page.locator("[data-field=instance-id]").text_content(),
            page.locator("[data-field=label]").text_content(),
            page.locator("[data-field=nonce]").text_content(),
        )
        if visible != expected:
            raise ReportTargetMismatch("fixture visible target text does not match frozen fault")

    def submit_fixture(
        self,
        page: FixturePage,
        instance_id: int,
        label: str,
        fault: ConfirmedFault,
        *,
        report_start_by: datetime,
        now: datetime,
    ) -> FixtureReportReceipt:
        if report_start_by.tzinfo is None or now.tzinfo is None:
            raise ReportStartExpired("report timestamps must be timezone-aware")
        if now.astimezone(UTC) > report_start_by.astimezone(UTC):
            raise ReportStartExpired("report start deadline has elapsed")
        self._exact(page, instance_id, label, fault)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        before = self.evidence_dir / f"before-instance-{instance_id}.png"
        after = self.evidence_dir / f"after-instance-{instance_id}.png"
        page.screenshot(path=str(before))
        page.locator("[data-action=report]").click()
        try:
            page.locator("[data-report-submitted=true]").wait_for(state="visible", timeout=self.confirmation_timeout_ms)
        except TimeoutError as exc:
            raise ReportConfirmationTimeout("fixture report confirmation timed out") from exc
        page.screenshot(path=str(after))
        return FixtureReportReceipt("SUBMITTED", instance_id, label, fault.record.nonce, before, after)


class LiveReportDriver:
    """Fail-closed gate for future real-browser wiring; it never accepts fixture URLs."""

    def __init__(self, provider_url: str) -> None:
        parsed = urlparse(provider_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
            raise LiveSubmissionBlocked("live reporting requires a genuine https provider URL")
        self.provider_url = provider_url

    def submit_live(self, fault: ConfirmedFault, state: RunState) -> None:
        fault.require_confirmed()
        if state is not RunState.REPORTING_FAULT:
            raise LiveSubmissionBlocked("live reporting is allowed only from REPORTING_FAULT")
        raise LiveSubmissionBlocked("live submission is intentionally unavailable to fixture-only code")
