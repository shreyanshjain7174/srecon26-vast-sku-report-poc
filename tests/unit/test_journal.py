from __future__ import annotations

from datetime import UTC, datetime

import pytest

from srecon26_poc.journal import InvalidJournal, InvalidTransition, RunJournal
from srecon26_poc.types import RunIdentity, RunState


NOW = datetime(2026, 9, 23, tzinfo=UTC)


@pytest.fixture
def identity() -> RunIdentity:
    return RunIdentity(run_id="run-journal", label="srecon26-run-journal", created_at=NOW)


def advance_to_offer_pinned(journal: RunJournal) -> None:
    journal.append(RunState.OFFLINE_VALIDATED, "offline.validated", {}, NOW, 1)
    journal.append(RunState.BUDGET_RESERVED, "budget.reserved", {}, NOW, 2)
    journal.append(RunState.OFFER_PINNED, "offer.pinned", {}, NOW, 3)
    journal.append(RunState.REPORT_ADAPTER_READY, "report.adapter_ready", {}, NOW, 4)


def test_journal_rejects_skipped_transition(tmp_path, identity):
    journal = RunJournal.create(tmp_path, identity)
    with pytest.raises(InvalidTransition):
        journal.append(RunState.GUARD_ARMED, "guard.armed", {}, NOW, 1)
    assert journal.events() == ()


def test_create_intent_survives_reopen(tmp_path, identity):
    journal = RunJournal.create(tmp_path, identity)
    advance_to_offer_pinned(journal)
    journal.append(RunState.GUARD_ARMED, "guard.armed", {}, NOW, 10)
    journal.append(
        RunState.CREATE_REQUESTED,
        "provider.create_intent",
        {"key": "run-journal"},
        NOW,
        11,
    )
    reopened = RunJournal.open(tmp_path)
    assert reopened.events()[-1].payload["key"] == "run-journal"
    assert reopened.state() is RunState.CREATE_REQUESTED


def test_reopen_truncates_only_a_partial_final_tail(tmp_path, identity):
    journal = RunJournal.create(tmp_path, identity)
    journal.append(RunState.OFFLINE_VALIDATED, "offline.validated", {}, NOW, 1)
    journal.path.write_text(journal.path.read_text() + '{"partial":')
    reopened = RunJournal.open(tmp_path)
    assert len(reopened.events()) == 1
    assert reopened.path.read_bytes().endswith(b"\n")


def test_reopen_rejects_tampered_earlier_record(tmp_path, identity):
    journal = RunJournal.create(tmp_path, identity)
    journal.append(RunState.OFFLINE_VALIDATED, "offline.validated", {}, NOW, 1)
    journal.append(RunState.BUDGET_RESERVED, "budget.reserved", {}, NOW, 2)
    lines = journal.path.read_text().splitlines()
    journal.path.write_text(lines[0].replace("offline.validated", "tampered") + "\n" + lines[1] + "\n")
    with pytest.raises(InvalidJournal):
        RunJournal.open(tmp_path)


def test_halt_is_structured_terminal_event(tmp_path, identity):
    journal = RunJournal.create(tmp_path, identity)
    event = journal.record_halt("BudgetWriteFailure", "ledger write failed", "sha256:trace", NOW, 1)
    assert event.event_type == "HALT"
    assert event.payload == {
        "exception_class": "BudgetWriteFailure",
        "message": "ledger write failed",
        "traceback_hash": "sha256:trace",
    }
    assert journal.state() is RunState.TERMINAL
