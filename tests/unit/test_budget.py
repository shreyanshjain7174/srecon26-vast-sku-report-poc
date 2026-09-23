import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from srecon26_poc.budget import BudgetExceeded, ExposureLedger
from srecon26_poc.journal import RunJournal
from srecon26_poc.types import RunIdentity, RunState


def _proof(tmp_path, run_id: str, amount: str = "0.01"):
    artifact = tmp_path / f"invoice-{run_id}.json"
    payload = {
        "schema": "srecon26-vast-invoice-evidence/v1",
        "source": "VastCliProvider.capture_invoice_charge/v1",
        "observed_at": "2026-09-23T06:30:25Z",
        "run_id": run_id,
        "instance_id": 417,
        "label": f"label-{run_id}",
        "amount_usd": amount,
        "command": ["show", "invoices-v1", "--charges", "--verbose"],
        "provider_charge": {"amount": amount, "source": "instance-417", "type": "instance", "metadata": {"label": f"label-{run_id}"}},
    }
    artifact.write_text(json.dumps(payload))
    journal = RunJournal.create(tmp_path / f"journal-{run_id}", RunIdentity(run_id, f"label-{run_id}", datetime.now(UTC)))
    now = datetime.now(UTC)
    states = [
        (RunState.OFFLINE_VALIDATED, "gates.passed", {}),
        (RunState.BUDGET_RESERVED, "budget.reserved", {}),
        (RunState.OFFER_PINNED, "offer.pinned", {}),
        (RunState.REPORT_ADAPTER_READY, "report.adapter_ready", {}),
        (RunState.GUARD_ARMED, "guard.armed", {}),
        (RunState.CREATE_REQUESTED, "provider.create_intent", {}),
        (RunState.CREATED_VERIFYING, "provider.create_observed", {"instance_id": 417}),
    ]
    for index, (state, event, event_payload) in enumerate(states, start=1):
        journal.append(state, event, event_payload, now + timedelta(microseconds=index), index)
    journal_path = journal.path
    journal.append(RunState.DESTROYING, "teardown.exact", {"instance_id": 417, "label": f"label-{run_id}"}, now + timedelta(microseconds=8), 8)
    journal.append(RunState.TERMINAL, "terminal.safe", {}, now + timedelta(microseconds=9), 9)
    absence = tmp_path / f"absence-{run_id}.json"
    absence.write_text(json.dumps({
        "schema": "srecon26-vast-absence-evidence/v1",
        "source": "VastCliProvider.capture_absence_evidence/v1",
        "run_id": run_id,
        "instance_id": 417,
        "label": f"label-{run_id}",
        "reads": [
            {"observed_at": "2026-09-23T06:23:25Z", "matching_instances": 0},
            {"observed_at": "2026-09-23T06:23:31Z", "matching_instances": 0},
            {"observed_at": "2026-09-23T06:23:38Z", "matching_instances": 0},
        ],
    }))
    return {
        "reads": 3,
        "instance_id": 417,
        "label": f"label-{run_id}",
        "billing": {
            "source": "provider_invoice",
            "artifact": artifact.name,
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "journal_artifact": str(journal_path.relative_to(tmp_path)),
            "journal_sha256": hashlib.sha256(journal_path.read_bytes()).hexdigest(),
            "absence_artifact": absence.name,
            "absence_sha256": hashlib.sha256(absence.read_bytes()).hexdigest(),
        },
    }


def test_cumulative_reservation_above_five_dollars_fails(tmp_path):
    ledger = ExposureLedger(tmp_path / "ledger.json")
    ledger.reserve("run-a", Decimal("2.00"), "paired-comparison")
    with pytest.raises(BudgetExceeded):
        ledger.reserve("run-b", Decimal("3.01"), "paired-comparison")


def test_exact_cap_and_category_caps_are_decimal_and_durable(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = ExposureLedger(path)
    ledger.reserve("gpu", Decimal("1.00"), "gpu-smoke")
    ledger.reserve("canary", Decimal("1.00"), "canary")
    ledger.reserve("comparison", Decimal("3.00"), "paired-comparison")
    assert ledger.headroom() == Decimal("0.00")
    assert ExposureLedger(path).headroom() == Decimal("0.00")
    with pytest.raises(BudgetExceeded):
        ledger.reserve("another", Decimal("0.01"), "paired-comparison")


def test_actual_spend_does_not_release_reservation_without_absence_proof(tmp_path):
    ledger = ExposureLedger(tmp_path / "ledger.json")
    ledger.reserve("run-a", Decimal("1.00"), "gpu-smoke")
    with pytest.raises(ValueError, match="absence proof"):
        ledger.commit_actual("run-a", Decimal("0.01"), None)
    assert ledger.headroom() == Decimal("4.00")
    ledger.commit_actual("run-a", Decimal("0.01"), _proof(tmp_path, "run-a"))
    assert ledger.headroom() == Decimal("4.99")
    ledger.reserve("run-b", Decimal("0.99"), "gpu-smoke")
    assert ledger.headroom() == Decimal("4.00")


def test_actual_spend_cannot_exceed_reservation(tmp_path):
    ledger = ExposureLedger(tmp_path / "ledger.json")
    ledger.reserve("run-a", Decimal("1.00"), "gpu-smoke")
    with pytest.raises(BudgetExceeded, match="fit within"):
        ledger.commit_actual("run-a", Decimal("1.01"), _proof(tmp_path, "run-a", "1.01"))


def test_actual_spend_still_counts_toward_category_cap(tmp_path):
    ledger = ExposureLedger(tmp_path / "ledger.json")
    ledger.reserve("run-a", Decimal("1.00"), "gpu-smoke")
    ledger.commit_actual("run-a", Decimal("0.01"), _proof(tmp_path, "run-a"))
    with pytest.raises(BudgetExceeded):
        ledger.reserve("run-b", Decimal("2.00"), "gpu-smoke")


def test_audited_smoke_retry_envelope_keeps_pending_reservation_charged(tmp_path):
    ledger = ExposureLedger(tmp_path / "ledger.json")
    ledger.reserve("pending-invoice", Decimal("0.97"), "gpu-smoke")
    ledger.reserve("bounded-retry", Decimal("0.90"), "gpu-smoke-retry")
    assert ledger.headroom() == Decimal("3.13")
    with pytest.raises(BudgetExceeded):
        ledger.reserve("overflow", Decimal("0.01"), "gpu-smoke-retry")


def test_direct_inference_smoke_allows_three_bounded_reservations(tmp_path):
    ledger = ExposureLedger(tmp_path / "ledger.json")

    for run_id in ("inference-attempt-1", "inference-attempt-2", "inference-attempt-3"):
        ledger.reserve(run_id, Decimal("0.50"), "gpu-inference-smoke")
    assert ledger.headroom() == Decimal("3.50")
    assert ledger.run_ids_for_category("gpu-inference-smoke") == (
        "inference-attempt-1",
        "inference-attempt-2",
        "inference-attempt-3",
    )
    ledger.reserve("inference-attempt-4", Decimal("0.50"), "gpu-inference-smoke")
    with pytest.raises(BudgetExceeded, match="at most four reservation attempts"):
        ledger.reserve("inference-attempt-5", Decimal("0.01"), "gpu-inference-smoke")


def test_direct_inference_smoke_rejects_a_reservation_over_fifty_cents(tmp_path):
    ledger = ExposureLedger(tmp_path / "ledger.json")

    with pytest.raises(BudgetExceeded):
        ledger.reserve("inference-attempt", Decimal("1.000001"), "gpu-inference-smoke")


def test_direct_inference_machine_binding_is_atomic_and_distinct(tmp_path):
    ledger = ExposureLedger(tmp_path / "ledger.json")

    ledger.reserve("inference-attempt-1", Decimal("0.50"), "gpu-inference-smoke", machine_id=145338)
    with pytest.raises(BudgetExceeded, match="already reserved"):
        ledger.reserve("inference-attempt-2", Decimal("0.50"), "gpu-inference-smoke", machine_id=145338)
    ledger.reserve("inference-attempt-2", Decimal("0.50"), "gpu-inference-smoke", machine_id=145339)


def test_machine_binding_is_rejected_outside_inference(tmp_path):
    ledger = ExposureLedger(tmp_path / "ledger.json")

    with pytest.raises(BudgetExceeded, match="restricted"):
        ledger.reserve("canary-attempt", Decimal("0.50"), "canary", machine_id=145338)


def test_distinct_machine_smoke_is_single_use_after_settled_retry(tmp_path):
    ledger = ExposureLedger(tmp_path / "ledger.json")
    ledger.reserve("pending-original", Decimal("0.97"), "gpu-smoke")
    ledger.reserve("settled-retry", Decimal("0.90"), "gpu-smoke-retry")
    ledger.commit_actual("settled-retry", Decimal("0.005"), _proof(tmp_path, "settled-retry", "0.005"))

    ledger.reserve("distinct-machine", Decimal("0.25"), "gpu-smoke-distinct-machine")

    assert ledger.headroom() == Decimal("3.775")
    assert ledger.smoke_run_ids() == ("distinct-machine", "pending-original", "settled-retry")
    with pytest.raises(BudgetExceeded, match="unused entitlement"):
        ledger.reserve("second-distinct-machine", Decimal("0.01"), "gpu-smoke-distinct-machine")


def test_distinct_machine_smoke_requires_settled_retry(tmp_path):
    ledger = ExposureLedger(tmp_path / "ledger.json")
    ledger.reserve("pending-original", Decimal("0.97"), "gpu-smoke")
    ledger.reserve("pending-retry", Decimal("0.90"), "gpu-smoke-retry")

    with pytest.raises(BudgetExceeded, match="one settled retry"):
        ledger.reserve("distinct-machine", Decimal("0.25"), "gpu-smoke-distinct-machine")


def test_audited_smoke_retry_rejects_one_dollar_and_requires_pending_original(tmp_path):
    ledger = ExposureLedger(tmp_path / "ledger.json")
    with pytest.raises(BudgetExceeded, match="pending original"):
        ledger.reserve("retry-without-original", Decimal("0.90"), "gpu-smoke-retry")
    ledger.reserve("pending-invoice", Decimal("0.97"), "gpu-smoke")
    with pytest.raises(BudgetExceeded):
        ledger.reserve("oversized-retry", Decimal("1.00"), "gpu-smoke-retry")


def test_idempotent_reservation_returns_without_relocking(tmp_path):
    ledger = ExposureLedger(tmp_path / "ledger.json")
    ledger.reserve("run-a", Decimal("1.00"), "gpu-smoke")
    assert ledger.reserve("run-a", Decimal("1.00"), "gpu-smoke") == Decimal("4.00")


def test_malformed_entry_fails_closed(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text('{"reservations":{"bad":"not-a-mapping"}}')
    with pytest.raises(Exception, match="safely"):
        ExposureLedger(path).headroom()


def test_tampered_invoice_evidence_fails_closed(tmp_path):
    ledger = ExposureLedger(tmp_path / "ledger.json")
    ledger.reserve("run-a", Decimal("1.00"), "gpu-smoke")
    proof = _proof(tmp_path, "run-a")
    ledger.commit_actual("run-a", Decimal("0.01"), proof)
    (tmp_path / "invoice-run-a.json").write_text("{}")
    with pytest.raises(Exception, match="safely"):
        ledger.headroom()


def test_null_identity_cannot_release_reservation(tmp_path):
    ledger = ExposureLedger(tmp_path / "ledger.json")
    ledger.reserve("run-a", Decimal("1.00"), "gpu-smoke")
    proof = _proof(tmp_path, "run-a")
    proof["instance_id"] = None
    proof["label"] = None
    with pytest.raises(ValueError, match="exact instance"):
        ledger.commit_actual("run-a", Decimal("0.01"), proof)


def test_missing_teardown_cannot_release_reservation(tmp_path):
    ledger = ExposureLedger(tmp_path / "ledger.json")
    ledger.reserve("run-a", Decimal("1.00"), "gpu-smoke")
    proof = _proof(tmp_path, "run-a")
    journal_path = tmp_path / proof["billing"]["journal_artifact"]
    lines = journal_path.read_text().splitlines()
    journal_path.write_text("\n".join(lines[:7]) + "\n")
    proof["billing"]["journal_sha256"] = hashlib.sha256(journal_path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="teardown|journal"):
        ledger.commit_actual("run-a", Decimal("0.01"), proof)
