from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from scripts.run_canary import (
    FixtureScenario,
    _parse_inference_history,
    default_gate_evidence,
    run_blocked_paid_invocation,
    run_fixture,
)


NOW = datetime(2026, 9, 23, 12, tzinfo=UTC)


def test_parse_explicit_inference_history_binds_exact_prior_machine() -> None:
    history = _parse_inference_history("inference-infer20260923155935:8024")

    assert history.run_id == "inference-infer20260923155935"
    assert history.machine_id == 8024


@pytest.mark.parametrize("value", ["missing-separator", "run:zero", "run:not-an-int"])
def test_parse_explicit_inference_history_rejects_invalid_identity(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_inference_history(value)


def _manifest(result):
    return json.loads(result.manifest_path.read_text(encoding="utf-8"))


def _run(tmp_path, scenario=FixtureScenario.SUCCESS, *, stage="gpu-smoke", reserve=Decimal("1.00"), evidence=None):
    return run_fixture(
        output_root=tmp_path,
        stage=stage,
        scenario=scenario,
        reserve=reserve,
        evidence=evidence,
        now=NOW,
        run_id=f"{stage}-{scenario.value}",
    )


def test_success_records_bounded_exact_teardown_and_fixture_provenance(tmp_path) -> None:
    result = _run(tmp_path)
    manifest = _manifest(result)

    assert result.status == "COMPLETED"
    assert result.provider_create_calls == 1
    assert result.provider_destroy_calls == ((417, "srecon26-gpu-smoke--nonce-fixture-nonce-01234567"),)
    assert result.absence_reads == 3
    assert manifest["provenance"] == "offline-fixture"
    assert manifest["real_gpu_claim"] is False
    assert manifest["cost"] == {"actual": "0.00", "currency": "USD", "fixture_only": True, "reserved": "1.00"}
    assert manifest["lifecycle_timestamps"]["terminal"] <= manifest["hard_deadline"]


def test_metric_path_requires_and_records_complete_fixture_path(tmp_path) -> None:
    result = _run(tmp_path, stage="metric-path")
    manifest = _manifest(result)

    assert result.status == "COMPLETED"
    assert manifest["stage"] == "metric-path"
    assert manifest["contract_and_gpu_facts"]["fixture_only"] is True
    assert result.absence_reads == 3


def test_inference_smoke_fixture_accepts_the_distinct_half_dollar_stage(tmp_path) -> None:
    result = _run(tmp_path, stage="inference-smoke", reserve=Decimal("0.50"))
    manifest = _manifest(result)

    assert result.status == "COMPLETED"
    assert manifest["stage"] == "inference-smoke"
    assert manifest["cost"]["reserved"] == "0.50"


def test_inference_smoke_fixture_refuses_to_exceed_its_entitlement(tmp_path) -> None:
    with pytest.raises(ValueError, match="stage ceiling"):
        _run(tmp_path, stage="inference-smoke", reserve=Decimal("1.000001"))


def test_metric_path_blocks_until_smoke_is_finalized(tmp_path) -> None:
    label = "srecon26-metric-path--nonce-fixture-nonce-01234567"
    result = _run(tmp_path, stage="metric-path", evidence=replace(default_gate_evidence(label), smoke_finalized=False))

    assert result.status == "BLOCKED"
    assert "smoke_finalized_for_metric_path" in _manifest(result)["gate_failures"]
    assert result.provider_create_calls == 0


def test_confirmed_sku_fault_reports_before_exact_teardown(tmp_path) -> None:
    result = _run(tmp_path, FixtureScenario.SKU_FAULT)
    manifest = _manifest(result)

    assert result.status == "FAILED_SAFE"
    assert manifest["report"] == {"attempted": True, "confirmed": True, "events": ["preflight", "before", "submit", "after"]}
    assert result.provider_destroy_calls[0][0] == 417
    assert result.absence_reads == 3


def test_unresolved_diagnosis_never_reports_and_still_tears_down(tmp_path) -> None:
    result = _run(tmp_path, FixtureScenario.UNRESOLVED_DIAGNOSIS)
    manifest = _manifest(result)

    assert result.status == "FAILED_SAFE"
    assert result.limitation == "diagnosis unresolved; provider report forbidden"
    assert manifest["report"]["attempted"] is False
    assert manifest["report"]["events"] == []
    assert result.provider_destroy_calls and result.absence_reads == 3


def test_report_timeout_is_bounded_then_tears_down_before_deadline(tmp_path) -> None:
    result = _run(tmp_path, FixtureScenario.REPORT_TIMEOUT)
    manifest = _manifest(result)

    assert result.status == "REPORT_UNCONFIRMED"
    assert result.limitation == "report fixture timed out"
    assert manifest["report"]["events"] == ["preflight", "before", "submit"]
    assert manifest["lifecycle_timestamps"]["terminal"] <= manifest["hard_deadline"]
    assert result.provider_destroy_calls and result.absence_reads == 3


def test_ambiguous_create_is_never_retried_and_proves_absence(tmp_path) -> None:
    result = _run(tmp_path, FixtureScenario.CREATE_AMBIGUITY)
    manifest = _manifest(result)

    assert result.status == "HALTED"
    assert result.provider_create_calls == 1
    assert result.provider_destroy_calls == ()
    assert result.absence_reads == 3
    assert manifest["journal_state"] == "TERMINAL"


def test_deadline_takeover_leaves_time_for_teardown_and_absence(tmp_path) -> None:
    result = _run(tmp_path, FixtureScenario.DEADLINE_TEARDOWN)
    manifest = _manifest(result)

    assert result.status == "FAILED_SAFE"
    assert result.limitation == "hard deadline reached before canary completion"
    assert manifest["lifecycle_timestamps"]["terminal"] == manifest["hard_deadline"]
    assert result.provider_destroy_calls and result.absence_reads == 3


@pytest.mark.parametrize(
    ("mutate", "required_gate"),
    [
        (lambda evidence: replace(evidence, phase1_status="gaps_found"), "phase1_passed"),
        (lambda evidence: replace(evidence, semgrep_current=False), "semgrep_current"),
        (lambda evidence: replace(evidence, no_spend_preflight_current=False), "no_spend_preflight_current"),
        (lambda evidence: replace(evidence, independent_guard_attested=False), "independent_guard_attested"),
        (lambda evidence: replace(evidence, report_fixture_verified=False), "report_fixture_verified"),
        (lambda evidence: replace(evidence, offer=None), "exact_offer_contract"),
        (lambda evidence: replace(evidence, ledger_available=False), "ledger_available"),
        (lambda evidence: replace(evidence, security_current=False), "security_current"),
        (lambda evidence: replace(evidence, safety_breach=True), "no_safety_breach"),
    ],
)
def test_each_gate_failure_blocks_before_provider_create(tmp_path, mutate, required_gate) -> None:
    label = "srecon26-gpu-smoke--nonce-fixture-nonce-01234567"
    result = _run(tmp_path, evidence=mutate(default_gate_evidence(label)))
    manifest = _manifest(result)

    assert result.status == "BLOCKED"
    assert required_gate in manifest["gate_failures"]
    assert result.provider_create_calls == 0
    assert result.provider_destroy_calls == ()
    assert result.provenance == "offline-fixture"


def test_foreign_inventory_blocks_before_provider_create(tmp_path) -> None:
    label = "srecon26-gpu-smoke--nonce-fixture-nonce-01234567"
    evidence = default_gate_evidence(label)
    assert evidence.offer is not None
    foreign = replace(evidence.offer, label="unrelated-owner")
    result = _run(tmp_path, evidence=replace(evidence, inventory=(foreign,)))

    assert result.status == "BLOCKED"
    assert "inventory_safe" in _manifest(result)["gate_failures"]
    assert result.provider_create_calls == 0


def test_non_fixture_invocation_has_no_paid_dispatch_branch(tmp_path) -> None:
    result = run_blocked_paid_invocation(output_root=tmp_path, stage="gpu-smoke", reserve=Decimal("1.00"), now=NOW)
    manifest = _manifest(result)

    assert result.status == "BLOCKED"
    assert result.provider_create_calls == 0
    assert manifest["limitation"] == "paid dispatch is intentionally unavailable until Plan 02-06"
    assert manifest["provenance"] == "offline-fixture"
