"""Offline eligibility checks and evidence classification for paired comparisons.

This module deliberately has no provider, subprocess, or dispatch dependency.  A
positive decision says only that a *future* comparison may be considered after
the caller has independently satisfied every live-run safety gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum


MINIMUM_COMPARISON_HEADROOM = Decimal("3.00")
REQUIRED_BLOCK_ORDER = ("AB", "BA", "AB")
REAL_GPU_PROVENANCE = "real-gpu"


class ComparisonClassification(StrEnum):
    """The only classifications emitted from block-level evidence."""

    VALID = "VALID"
    EXPLORATORY_ONLY = "EXPLORATORY_ONLY"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class CanaryReference:
    reference: str
    validated: bool
    provenance: str


@dataclass(frozen=True, slots=True)
class ReadyCapacity:
    """A current, read-only capacity observation; it is not a reservation."""

    capacity_id: str
    gpu_class: str
    ready: bool
    current: bool


@dataclass(frozen=True, slots=True)
class ComparisonControls:
    model_revision: str
    image_digest: str
    gpu_class: str
    trace_hash: str
    prompt_schedule: tuple[str, ...]
    token_schedule: tuple[int, ...]
    seed: int
    warmup_seconds: int
    window_seconds: int
    replica_limit: int
    readiness_policy: str


@dataclass(frozen=True, slots=True)
class ComparisonPlan:
    canary: CanaryReference
    capacities: tuple[ReadyCapacity, ...]
    controls: ComparisonControls
    order: tuple[str, ...] = REQUIRED_BLOCK_ORDER


@dataclass(frozen=True, slots=True)
class ComparisonDecision:
    eligible: bool
    reasons: tuple[str, ...]
    minimum_headroom: Decimal = MINIMUM_COMPARISON_HEADROOM


@dataclass(frozen=True, slots=True)
class RunEvidence:
    """Raw operational evidence for one A or B half of a paired block."""

    variant: str
    completed: bool
    queue_depth: Decimal
    ttft_ms: Decimal
    tpot_ms: Decimal
    end_to_end_latency_ms: Decimal
    gpu_utilization_pct: Decimal
    kv_cache_utilization_pct: Decimal
    cpu_utilization_pct: Decimal
    desired_replicas: int
    ready_replicas: int
    readiness_delay_seconds: Decimal
    failures: tuple[str, ...]
    trace_hash: str
    provenance: str
    controls: ComparisonControls


@dataclass(frozen=True, slots=True)
class PairedBlock:
    order: str
    runs: tuple[RunEvidence, ...]

    @property
    def complete(self) -> bool:
        return (
            len(self.runs) == 2
            and tuple(run.variant for run in self.runs) == tuple(self.order)
            and all(run.completed and not run.failures for run in self.runs)
        )


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    plan: ComparisonPlan
    blocks: tuple[PairedBlock, ...]


@dataclass(frozen=True, slots=True)
class ComparisonAnalysis:
    classification: ComparisonClassification
    reasons: tuple[str, ...]
    complete_blocks: int
    raw_blocks: tuple[PairedBlock, ...]
    comparative_claim_allowed: bool

    @property
    def comparative_conclusion(self) -> None:
        """This phase never derives a performance conclusion or chart claim."""
        return None


def _as_decimal(value: Decimal | str | int) -> Decimal:
    if isinstance(value, float):
        raise TypeError("comparison budget must not use float")
    if not isinstance(value, (Decimal, str, int)):
        raise TypeError("comparison budget must be Decimal, string, or integer")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("comparison budget is not a Decimal amount") from exc


def _controls_reasons(controls: ComparisonControls) -> list[str]:
    reasons: list[str] = []
    for name in ("model_revision", "image_digest", "gpu_class", "trace_hash", "readiness_policy"):
        if not getattr(controls, name):
            reasons.append(f"missing control: {name}")
    if not controls.image_digest.startswith("sha256:"):
        reasons.append("image digest is not pinned")
    if not controls.prompt_schedule:
        reasons.append("missing control: prompt_schedule")
    if not controls.token_schedule or any(token <= 0 for token in controls.token_schedule):
        reasons.append("missing or invalid control: token_schedule")
    if controls.warmup_seconds < 0 or controls.window_seconds <= 0 or controls.replica_limit <= 0:
        reasons.append("invalid warm-up, window, or replica control")
    return reasons


def validate_comparison_plan(plan: ComparisonPlan, remaining: Decimal | str | int) -> ComparisonDecision:
    """Return a no-dispatch decision for the exact current comparison plan."""
    reasons: list[str] = []
    remaining_decimal = _as_decimal(remaining)

    if not plan.canary.reference or not plan.canary.validated or plan.canary.provenance != REAL_GPU_PROVENANCE:
        reasons.append("validated real-GPU canary reference is required")
    if remaining_decimal < MINIMUM_COMPARISON_HEADROOM:
        reasons.append("remaining Decimal budget is below $3.00")
    if len(plan.capacities) != 2:
        reasons.append("exactly two current compatible ready GPU capacities are required")
    else:
        capacity_ids = {capacity.capacity_id for capacity in plan.capacities}
        if len(capacity_ids) != 2:
            reasons.append("two distinct GPU capacities are required")
        for capacity in plan.capacities:
            if not capacity.current or not capacity.ready:
                reasons.append("each GPU capacity must be current and ready")
            if capacity.gpu_class != plan.controls.gpu_class:
                reasons.append("GPU class control mismatch")
    if plan.order != REQUIRED_BLOCK_ORDER:
        reasons.append("paired block order must be AB,BA,AB")
    reasons.extend(_controls_reasons(plan.controls))
    return ComparisonDecision(not reasons, tuple(dict.fromkeys(reasons)))


def analyze_comparison(result: ComparisonResult) -> ComparisonAnalysis:
    """Classify captured records while retaining all raw operational evidence."""
    reasons: list[str] = []
    complete_blocks = sum(block.complete for block in result.blocks)

    if len(result.blocks) > len(REQUIRED_BLOCK_ORDER):
        reasons.append("comparison has more than three blocks")
    for index, block in enumerate(result.blocks):
        expected_order = REQUIRED_BLOCK_ORDER[index] if index < len(REQUIRED_BLOCK_ORDER) else None
        if block.order != expected_order:
            reasons.append("paired block order does not match AB,BA,AB")
        if len(block.runs) != 2 or tuple(run.variant for run in block.runs) != tuple(block.order):
            reasons.append("paired block does not contain its declared A/B order")
        for run in block.runs:
            if run.provenance != REAL_GPU_PROVENANCE:
                reasons.append("comparison evidence must have real-gpu provenance")
            if run.trace_hash != result.plan.controls.trace_hash:
                reasons.append("trace identity mismatch")
            if run.controls != result.plan.controls:
                reasons.append("immutable comparison controls changed")

    if reasons:
        return ComparisonAnalysis(
            ComparisonClassification.INVALID,
            tuple(dict.fromkeys(reasons)),
            complete_blocks,
            result.blocks,
            False,
        )
    if complete_blocks < 3:
        return ComparisonAnalysis(
            ComparisonClassification.EXPLORATORY_ONLY,
            ("fewer than three complete paired blocks; comparative conclusion rejected",),
            complete_blocks,
            result.blocks,
            False,
        )
    if len(result.blocks) != 3 or complete_blocks != 3:
        return ComparisonAnalysis(
            ComparisonClassification.INVALID,
            ("exactly three complete paired blocks are required",),
            complete_blocks,
            result.blocks,
            False,
        )
    return ComparisonAnalysis(ComparisonClassification.VALID, (), complete_blocks, result.blocks, True)
