from dataclasses import replace
from decimal import Decimal

import pytest

from srecon26_poc.comparison import (
    CanaryReference,
    ComparisonClassification,
    ComparisonControls,
    ComparisonPlan,
    ComparisonResult,
    PairedBlock,
    ReadyCapacity,
    RunEvidence,
    analyze_comparison,
    validate_comparison_plan,
)


def controls(**changes):
    base = ComparisonControls(
        "model-r1", "sha256:image", "RTX-3090", "sha256:trace", ("prompt-a", "prompt-b"), (32, 64),
        7, 30, 120, 2, "all-metrics-fresh",
    )
    return replace(base, **changes)


def plan(**changes):
    base = ComparisonPlan(
        CanaryReference("canary-sha256", True, "real-gpu"),
        (ReadyCapacity("capacity-a", "RTX-3090", True, True), ReadyCapacity("capacity-b", "RTX-3090", True, True)),
        controls(),
    )
    return replace(base, **changes)


def run(variant, *, completed=True, provenance="real-gpu", trace_hash="sha256:trace", run_controls=None, failures=()):
    return RunEvidence(
        variant, completed, Decimal("12"), Decimal("100"), Decimal("10"), Decimal("180"), Decimal("80"),
        Decimal("65"), Decimal("35"), 2, 2, Decimal("4"), failures, trace_hash, provenance, run_controls or controls(),
    )


def block(order, **run_changes):
    return PairedBlock(order, tuple(run(variant, **run_changes) for variant in order))


def test_eligibility_requires_a_validated_real_gpu_canary_two_ready_capacities_and_decimal_budget():
    decision = validate_comparison_plan(
        plan(
            canary=CanaryReference("", False, "local-synthetic"),
            capacities=(ReadyCapacity("capacity-a", "RTX-3090", False, False),),
        ),
        Decimal("2.99"),
    )

    assert not decision.eligible
    assert "validated real-GPU canary reference is required" in decision.reasons
    assert "exactly two current compatible ready GPU capacities are required" in decision.reasons
    assert "remaining Decimal budget is below $3.00" in decision.reasons


def test_eligibility_rejects_float_budget_and_control_or_order_mismatch():
    with pytest.raises(TypeError, match="float"):
        validate_comparison_plan(plan(), 3.0)

    decision = validate_comparison_plan(
        plan(
            capacities=(ReadyCapacity("same", "RTX-3090", True, True), ReadyCapacity("same", "different", True, True)),
            controls=controls(image_digest="not-pinned"),
            order=("AB", "AB", "BA"),
        ),
        "3.00",
    )

    assert not decision.eligible
    assert "two distinct GPU capacities are required" in decision.reasons
    assert "GPU class control mismatch" in decision.reasons
    assert "image digest is not pinned" in decision.reasons
    assert "paired block order must be AB,BA,AB" in decision.reasons


@pytest.mark.parametrize(
    "change",
    [
        {"model_revision": "model-r2"}, {"image_digest": "sha256:other"}, {"gpu_class": "A100"},
        {"trace_hash": "sha256:other-trace"}, {"prompt_schedule": ("other",)}, {"token_schedule": (16,)},
        {"seed": 8}, {"warmup_seconds": 31}, {"window_seconds": 121}, {"replica_limit": 3},
        {"readiness_policy": "other-policy"},
    ],
)
def test_any_per_run_control_change_invalidates_comparability(change):
    changed = controls(**change)
    result = ComparisonResult(plan(), (PairedBlock("AB", (run("A", run_controls=changed), run("B"))),))

    analysis = analyze_comparison(result)

    assert analysis.classification is ComparisonClassification.INVALID
    assert "immutable comparison controls changed" in analysis.reasons
    assert not analysis.comparative_claim_allowed


def test_fewer_than_three_complete_blocks_are_exploratory_and_keep_raw_evidence():
    incomplete = PairedBlock("BA", (run("B", completed=False), run("A", completed=False)))
    result = ComparisonResult(plan(), (block("AB"), incomplete))

    analysis = analyze_comparison(result)

    assert analysis.classification is ComparisonClassification.EXPLORATORY_ONLY
    assert analysis.complete_blocks == 1
    assert analysis.raw_blocks == result.blocks
    assert analysis.comparative_conclusion is None
    assert not analysis.comparative_claim_allowed


def test_mixed_provenance_trace_and_order_are_invalid_not_a_comparison_claim():
    result = ComparisonResult(
        plan(),
        (PairedBlock("BA", (run("B", provenance="local-synthetic"), run("A", trace_hash="sha256:other"))),),
    )

    analysis = analyze_comparison(result)

    assert analysis.classification is ComparisonClassification.INVALID
    assert "comparison evidence must have real-gpu provenance" in analysis.reasons
    assert "trace identity mismatch" in analysis.reasons
    assert "paired block order does not match AB,BA,AB" in analysis.reasons


def test_exactly_three_complete_matched_blocks_are_valid_for_later_analysis_only():
    result = ComparisonResult(plan(), (block("AB"), block("BA"), block("AB")))

    analysis = analyze_comparison(result)

    assert analysis.classification is ComparisonClassification.VALID
    assert analysis.complete_blocks == 3
    assert analysis.comparative_claim_allowed
    assert analysis.comparative_conclusion is None
