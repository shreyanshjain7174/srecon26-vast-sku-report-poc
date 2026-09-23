from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from srecon26_poc.desktop_report import ConfirmedFault, FixtureReportDriver, ReportConfirmationTimeout, ReportStartExpired, ReportTargetMismatch, TargetMismatch, UnconfirmedFault
from srecon26_poc.reporting import FaultRecord


INSTANCE_ID = 417
LABEL = "srecon26-run-report"
NONCE = "nonce-0123456789abcdef"


@dataclass
class Locator:
    page: "Page"
    selector: str

    def get_attribute(self, name: str) -> str | None:
        return self.page.attributes.get((self.selector, name))

    def text_content(self) -> str | None:
        return self.page.text.get(self.selector)

    def click(self) -> None:
        self.page.clicks.append(self.selector)
        if self.selector == "[data-action=report]":
            self.page.submitted = True

    def wait_for(self, *, state: str, timeout: int) -> None:
        assert state == "visible"
        assert timeout == 60_000
        if not self.page.submitted:
            raise TimeoutError("fixture never confirmed")


class Page:
    def __init__(self) -> None:
        self.attributes = {
            ("[data-instance]", "data-instance-id"): str(INSTANCE_ID),
            ("[data-instance]", "data-label"): LABEL,
            ("[data-instance]", "data-nonce"): NONCE,
        }
        self.text = {
            "[data-field=instance-id]": str(INSTANCE_ID),
            "[data-field=label]": LABEL,
            "[data-field=nonce]": NONCE,
        }
        self.clicks: list[str] = []
        self.submitted = False

    def locator(self, selector: str) -> Locator:
        return Locator(self, selector)

    def screenshot(self, *, path: str) -> None:
        with open(path, "wb") as handle:
            handle.write(b"fixture-redacted-image")


def confirmed_fault() -> ConfirmedFault:
    return ConfirmedFault.from_record(FaultRecord(INSTANCE_ID, LABEL, NONCE, "advertised GPU does not match"))


def report_start_by() -> datetime:
    return datetime(2026, 9, 23, 0, 1, tzinfo=UTC)


def now() -> datetime:
    return datetime(2026, 9, 23, tzinfo=UTC)


def test_fixture_submits_only_exact_instance(tmp_path) -> None:
    page = Page()
    receipt = FixtureReportDriver(tmp_path).submit_fixture(page, INSTANCE_ID, LABEL, confirmed_fault(), report_start_by=report_start_by(), now=now())

    assert receipt.status == "SUBMITTED"
    assert receipt.instance_id == INSTANCE_ID
    assert receipt.label == LABEL
    assert receipt.nonce == NONCE
    assert receipt.before_path.read_bytes() == b"fixture-redacted-image"
    assert receipt.after_path.read_bytes() == b"fixture-redacted-image"
    assert page.clicks == ["[data-action=report]"]


def test_wrong_target_blocks_click(tmp_path) -> None:
    page = Page()
    page.attributes[("[data-instance]", "data-instance-id")] = "999"

    with pytest.raises(TargetMismatch):
        FixtureReportDriver(tmp_path).submit_fixture(page, INSTANCE_ID, LABEL, confirmed_fault(), report_start_by=report_start_by(), now=now())

    assert page.clicks == []


def test_nonce_mismatch_blocks_click(tmp_path) -> None:
    page = Page()
    page.attributes[("[data-instance]", "data-nonce")] = "nonce-other-0000"

    with pytest.raises(ReportTargetMismatch, match="nonce"):
        FixtureReportDriver(tmp_path).submit_fixture(page, INSTANCE_ID, LABEL, confirmed_fault(), report_start_by=report_start_by(), now=now())

    assert page.clicks == []


def test_fixture_rejects_unconfirmed_fault_before_capture_or_click(tmp_path) -> None:
    page = Page()
    fault = ConfirmedFault(FaultRecord(INSTANCE_ID, LABEL, NONCE, "network timeout"), classification="DIAGNOSIS_UNRESOLVED")

    with pytest.raises(UnconfirmedFault):
        FixtureReportDriver(tmp_path).submit_fixture(page, INSTANCE_ID, LABEL, fault, report_start_by=report_start_by(), now=now())

    assert page.clicks == []


def test_stale_report_start_blocks_click_before_any_capture(tmp_path) -> None:
    page = Page()

    with pytest.raises(ReportStartExpired):
        FixtureReportDriver(tmp_path).submit_fixture(page, INSTANCE_ID, LABEL, confirmed_fault(), report_start_by=now() - timedelta(seconds=1), now=now())

    assert page.clicks == []
    assert not list(tmp_path.iterdir())


def test_confirmation_timeout_has_no_receipt_or_after_capture(tmp_path) -> None:
    class TimeoutLocator(Locator):
        def wait_for(self, *, state: str, timeout: int) -> None:
            raise TimeoutError("fixture confirmation timed out")

    class TimeoutPage(Page):
        def locator(self, selector: str) -> Locator:
            locator = super().locator(selector)
            return TimeoutLocator(locator.page, locator.selector) if selector == "[data-report-submitted=true]" else locator

    page = TimeoutPage()
    with pytest.raises(ReportConfirmationTimeout):
        FixtureReportDriver(tmp_path).submit_fixture(page, INSTANCE_ID, LABEL, confirmed_fault(), report_start_by=report_start_by(), now=now())

    assert page.clicks == ["[data-action=report]"]
    assert list(tmp_path.glob("after-*.png")) == []
