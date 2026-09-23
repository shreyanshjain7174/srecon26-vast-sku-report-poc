from datetime import UTC, datetime
from pathlib import Path

import pytest

from srecon26_poc.reporting import FaultRecord, ReportGate, ReportRejected


class FakeAdapter:
    def __init__(self, submitted=True): self.submitted = submitted; self.events = []
    def preflight_exact_instance(self, instance_id, label): self.events.append("preflight"); assert (instance_id, label) == (1, "label")
    def capture_before(self, fault): self.events.append("before"); return Path("before.json")
    def submit(self, fault): self.events.append("submit"); return self.submitted
    def capture_after(self, receipt): self.events.append("after"); return Path("after.json")


def test_report_gate_confirms_or_times_out_without_widening_ownership():
    adapter = FakeAdapter()
    receipt = ReportGate(adapter).handle(FaultRecord(1, "label", "nonce", "gpu mismatch"), datetime(2026, 9, 23, tzinfo=UTC))
    assert receipt.confirmed and adapter.events == ["preflight", "before", "submit", "after"]
    with pytest.raises(ReportRejected):
        ReportGate(adapter).handle(FaultRecord(2, "wrong", "nonce", "x"), datetime(2026, 9, 23, tzinfo=UTC))
