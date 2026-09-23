from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Sequence


class LocalArm(StrEnum):
    CPU = "cpu"
    QUEUE = "queue"
    KV = "kv"


@dataclass(frozen=True, slots=True)
class Sample:
    timestamp: datetime
    cpu: float
    queue: float
    kv: float
    desired_replicas: int
    ready_replicas: int
    provenance: str
    hpa_event: str


@dataclass(frozen=True, slots=True)
class ArmEvaluation:
    arm: LocalArm
    valid: bool
    negative_control_seconds: float
    target_maximum: float
    non_target_maxima: dict[str, float]
    failures: tuple[str, ...]


_THRESHOLDS = {"cpu": 80.0, "queue": 100.0, "kv": 80.0}


def evaluate_arm(arm: LocalArm, samples: Sequence[Sample]) -> ArmEvaluation:
    failures: list[str] = []
    if len(samples) < 2:
        failures.append("insufficient samples")
        return ArmEvaluation(arm, False, 0, 0, {}, tuple(failures))
    ordered = sorted(samples, key=lambda sample: sample.timestamp)
    if list(samples) != ordered or any((later.timestamp - earlier.timestamp).total_seconds() > 15 for earlier, later in zip(ordered, ordered[1:])):
        failures.append("samples are unordered or stale")
    if any(sample.provenance != "local-synthetic" or not sample.hpa_event for sample in ordered):
        failures.append("missing local-synthetic provenance or HPA event")
    signal = arm.value
    target_max = max(getattr(sample, signal) for sample in ordered)
    other = {name: max(getattr(sample, name) for sample in ordered) for name in _THRESHOLDS if name != signal}
    if target_max < _THRESHOLDS[signal] * 1.2:
        failures.append("target did not exceed 20 percent margin")
    if any(value > _THRESHOLDS[name] * 0.8 for name, value in other.items()):
        failures.append("non-target signal crossed its safety margin")
    transition_index = next((index for index, sample in enumerate(ordered) if sample.desired_replicas == 2 and sample.ready_replicas == 2), None)
    if transition_index is None or any(sample.desired_replicas != 1 or sample.ready_replicas != 1 for sample in ordered[:transition_index]):
        failures.append("missing clean 1-to-2 desired and ready transition")
        negative = 0.0
    else:
        negative = (ordered[transition_index - 1].timestamp - ordered[0].timestamp).total_seconds()
        if negative < 90:
            failures.append("negative control is shorter than 90 seconds")
        if (ordered[transition_index].timestamp - ordered[0].timestamp).total_seconds() > 150:
            failures.append("transition exceeded four reconciliation intervals")
    return ArmEvaluation(arm, not failures, negative, target_max, other, tuple(failures))


def build_local_bundle(output_directory: Path, arm: LocalArm, samples: Sequence[Sample]) -> Path:
    if any(sample.provenance != "local-synthetic" for sample in samples):
        raise ValueError("local evidence cannot claim non-synthetic provenance")
    bundle = Path(output_directory) / arm.value
    bundle.mkdir(parents=True, exist_ok=True)
    serialised = [{**asdict(sample), "timestamp": sample.timestamp.isoformat()} for sample in samples]
    (bundle / "samples.json").write_text(json.dumps(serialised, sort_keys=True) + "\n")
    evaluation = evaluate_arm(arm, samples)
    (bundle / "metadata.json").write_text(json.dumps({"arm": arm.value, "provenance": "local-synthetic", "valid": evaluation.valid, "failures": evaluation.failures}, sort_keys=True) + "\n")
    return bundle
