from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from srecon26_poc.reporting import FaultRecord, ReportGate, ReportRejected


class FakeAdapter:
    def __init__(self, submitted=True): self.submitted = submitted; self.events = []
    def preflight_authenticated_session(self): self.events.append("session")
    def preflight_exact_instance(self, instance_id, label): self.events.append("preflight"); assert (instance_id, label) == (1, "label")
    def capture_before(self, fault): self.events.append("before"); return Path("before.json")
    def submit(self, fault): self.events.append("submit"); return self.submitted
    def capture_after(self, receipt): self.events.append("after"); return Path("after.json")


class MutableClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current

    def advance(self) -> None:
        self.current += timedelta(seconds=1)


class AdvancingAdapter(FakeAdapter):
    def __init__(self, clock: MutableClock, *, advance_after: set[str]) -> None:
        super().__init__()
        self.clock = clock
        self.advance_after = advance_after

    def _record(self, event: str) -> None:
        self.events.append(event)
        if event in self.advance_after:
            self.clock.advance()

    def preflight_exact_instance(self, instance_id, label):
        assert (instance_id, label) == (1, "label")
        self._record("preflight")

    def capture_before(self, fault):
        self._record("before")
        return Path("before.json")

    def submit(self, fault):
        self._record("submit")
        return True

    def capture_after(self, receipt):
        self._record("after")
        return Path("after.json")


def test_report_gate_confirms_or_times_out_without_widening_ownership():
    adapter = FakeAdapter()
    started = datetime(2026, 9, 23, tzinfo=UTC)
    receipt = ReportGate(adapter, now=lambda: started).handle(FaultRecord(1, "label", "nonce", "gpu mismatch"), started + timedelta(seconds=1))
    assert receipt.confirmed and adapter.events == ["preflight", "before", "submit", "after"]
    with pytest.raises(ReportRejected):
        ReportGate(adapter, now=lambda: started).handle(FaultRecord(2, "wrong", "nonce", "x"), started + timedelta(seconds=1))


@pytest.mark.parametrize(
    ("advance_after", "expected_events"),
    (
        ({"preflight"}, ["preflight"]),
        ({"before"}, ["preflight", "before"]),
        ({"submit"}, ["preflight", "before", "submit"]),
    ),
)
def test_report_gate_stops_before_each_remaining_action_when_cutoff_arrives(advance_after, expected_events):
    started = datetime(2026, 9, 23, tzinfo=UTC)
    clock = MutableClock(started)
    adapter = AdvancingAdapter(clock, advance_after=advance_after)

    with pytest.raises(ReportRejected, match="cutoff"):
        ReportGate(adapter, now=clock.now).handle(FaultRecord(1, "label", "nonce", "gpu mismatch"), started + timedelta(seconds=1))

    assert adapter.events == expected_events


def test_report_gate_rejects_before_preflight_when_cutoff_has_already_arrived():
    started = datetime(2026, 9, 23, tzinfo=UTC)
    adapter = FakeAdapter()

    with pytest.raises(ReportRejected, match="cutoff"):
        ReportGate(adapter, now=lambda: started).handle(FaultRecord(1, "label", "nonce", "gpu mismatch"), started)

    assert adapter.events == []
