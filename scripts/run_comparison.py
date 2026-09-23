#!/usr/bin/env python3
"""Classify a recorded paired-comparison fixture without provider dispatch."""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from srecon26_poc.comparison import (
    CanaryReference,
    ComparisonControls,
    ComparisonPlan,
    ComparisonResult,
    PairedBlock,
    ReadyCapacity,
    RunEvidence,
    analyze_comparison,
    validate_comparison_plan,
)


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _sequence(value: object, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return value


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, float):
        raise ValueError(f"{name} must not be a float")
    if not isinstance(value, (Decimal, str, int)):
        raise ValueError(f"{name} must be a Decimal-compatible value")
    return Decimal(str(value))


def _controls(data: Mapping[str, Any]) -> ComparisonControls:
    return ComparisonControls(
        model_revision=str(data["model_revision"]),
        image_digest=str(data["image_digest"]),
        gpu_class=str(data["gpu_class"]),
        trace_hash=str(data["trace_hash"]),
        prompt_schedule=tuple(str(value) for value in _sequence(data["prompt_schedule"], "prompt_schedule")),
        token_schedule=tuple(int(value) for value in _sequence(data["token_schedule"], "token_schedule")),
        seed=int(data["seed"]),
        warmup_seconds=int(data["warmup_seconds"]),
        window_seconds=int(data["window_seconds"]),
        replica_limit=int(data["replica_limit"]),
        readiness_policy=str(data["readiness_policy"]),
    )


def load_fixture(payload: Mapping[str, Any]) -> tuple[ComparisonPlan, Decimal, ComparisonResult]:
    plan_data = _mapping(payload["plan"], "plan")
    canary_data = _mapping(plan_data["canary"], "plan.canary")
    controls = _controls(_mapping(plan_data["controls"], "plan.controls"))
    capacities = tuple(
        ReadyCapacity(str(item["capacity_id"]), str(item["gpu_class"]), bool(item["ready"]), bool(item["current"]))
        for item in _sequence(plan_data["capacities"], "plan.capacities")
    )
    plan = ComparisonPlan(
        CanaryReference(str(canary_data["reference"]), bool(canary_data["validated"]), str(canary_data["provenance"])),
        capacities,
        controls,
        tuple(str(item) for item in _sequence(plan_data.get("order", ["AB", "BA", "AB"]), "plan.order")),
    )
    blocks: list[PairedBlock] = []
    for block_data in _sequence(payload["blocks"], "blocks"):
        block = _mapping(block_data, "block")
        runs: list[RunEvidence] = []
        for run_data in _sequence(block["runs"], "block.runs"):
            run = _mapping(run_data, "run")
            runs.append(
                RunEvidence(
                    str(run["variant"]), bool(run["completed"]), _decimal(run["queue_depth"], "queue_depth"),
                    _decimal(run["ttft_ms"], "ttft_ms"), _decimal(run["tpot_ms"], "tpot_ms"),
                    _decimal(run["end_to_end_latency_ms"], "end_to_end_latency_ms"),
                    _decimal(run["gpu_utilization_pct"], "gpu_utilization_pct"),
                    _decimal(run["kv_cache_utilization_pct"], "kv_cache_utilization_pct"),
                    _decimal(run["cpu_utilization_pct"], "cpu_utilization_pct"), int(run["desired_replicas"]),
                    int(run["ready_replicas"]), _decimal(run["readiness_delay_seconds"], "readiness_delay_seconds"),
                    tuple(str(value) for value in _sequence(run["failures"], "run.failures")), str(run["trace_hash"]),
                    str(run["provenance"]), _controls(_mapping(run["controls"], "run.controls")),
                )
            )
        blocks.append(PairedBlock(str(block["order"]), tuple(runs)))
    result = ComparisonResult(plan, tuple(blocks))
    return plan, _decimal(payload["remaining"], "remaining"), result


def run_fixture(path: Path) -> dict[str, object]:
    """Replay only a local JSON fixture; provider dispatch is intentionally absent."""
    payload = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    if not isinstance(payload, Mapping):
        raise ValueError("fixture root must be an object")
    plan, remaining, result = load_fixture(payload)
    decision = validate_comparison_plan(plan, remaining)
    analysis = analyze_comparison(result)
    return {
        "mode": "fixture-replay",
        "provider_dispatch": "disabled",
        "real_gpu_provenance_required": True,
        "comparative_claim_emitted": False,
        "eligibility": {"eligible": decision.eligible, "reasons": list(decision.reasons), "minimum_headroom": str(decision.minimum_headroom)},
        "analysis": {
            "classification": analysis.classification.value,
            "reasons": list(analysis.reasons),
            "complete_blocks": analysis.complete_blocks,
            "raw_block_count": len(analysis.raw_blocks),
            "comparative_claim_allowed": analysis.comparative_claim_allowed,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True, help="offline comparison fixture JSON")
    parser.add_argument("--json", type=Path, required=True, help="output classification JSON")
    args = parser.parse_args()
    try:
        output = run_fixture(args.fixture)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        output = {"mode": "fixture-replay", "provider_dispatch": "disabled", "error": str(error)}
        exit_code = 1
    else:
        exit_code = 0 if output["eligibility"]["eligible"] else 1
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(output, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps(output, sort_keys=True, default=str))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
