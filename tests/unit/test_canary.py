from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from srecon26_poc.canary import (
    VLLM_IMAGE_DIGEST,
    CanarySnapshot,
    KvmFacts,
    MetricSample,
    evaluate_canary_readiness,
    evaluate_kvm_capabilities,
)


NOW = datetime(2026, 9, 23, 12, tzinfo=UTC)


@pytest.fixture
def kvm_facts() -> KvmFacts:
    return KvmFacts(True, True, True, True, True, True, True, VLLM_IMAGE_DIGEST, NOW)


@pytest.fixture
def snapshot() -> CanarySnapshot:
    sample = MetricSample(Decimal("1"), NOW - timedelta(seconds=15))
    return CanarySnapshot(1, True, True, 1, True, sample, sample, sample, True, False)


@pytest.mark.parametrize(
    "field",
    ["systemd", "cgroup_v2", "privileged", "containerd", "nvidia_runtime", "nvidia_smi", "cuda"],
)
def test_kvm_gate_rejects_each_missing_host_capability(kvm_facts: KvmFacts, field: str) -> None:
    decision = evaluate_kvm_capabilities(replace(kvm_facts, **{field: False}), now=NOW)
    assert not decision.allowed
    assert any("missing" in blocker for blocker in decision.blockers)


def test_kvm_gate_requires_current_matching_digest(kvm_facts: KvmFacts) -> None:
    assert evaluate_kvm_capabilities(kvm_facts, now=NOW).allowed
    assert not evaluate_kvm_capabilities(replace(kvm_facts, resolved_image_digest=None), now=NOW).allowed
    assert not evaluate_kvm_capabilities(replace(kvm_facts, resolved_image_digest="sha256:" + "0" * 64), now=NOW).allowed
    assert not evaluate_kvm_capabilities(replace(kvm_facts, digest_resolved_at=NOW - timedelta(minutes=6)), now=NOW).allowed


@pytest.mark.parametrize(
    "field,value",
    [
        ("node_allocatable_gpus", 0),
        ("device_plugin_ready", False),
        ("vllm_pod_ready", False),
        ("vllm_gpu_allocated", 0),
        ("prometheus_target_healthy", False),
        ("cpu", None),
        ("queue", None),
        ("kv", None),
        ("warmup_request_succeeded", False),
    ],
)
def test_readiness_rejects_each_missing_observation_layer(snapshot: CanarySnapshot, field: str, value: object) -> None:
    decision = evaluate_canary_readiness(replace(snapshot, **{field: value}), now=NOW)
    assert not decision.allowed


def test_readiness_rejects_stale_metric_and_distinguishes_measurement_completion(snapshot: CanarySnapshot) -> None:
    stale = MetricSample(Decimal("1"), NOW - timedelta(seconds=61))
    assert not evaluate_canary_readiness(replace(snapshot, queue=stale), now=NOW).allowed

    ready = evaluate_canary_readiness(snapshot, now=NOW)
    assert ready.allowed
    assert not ready.measurement_complete
    complete = evaluate_canary_readiness(replace(snapshot, measured_request_succeeded=True), now=NOW)
    assert complete.measurement_complete


def test_evaluator_rejects_naive_clock() -> None:
    facts = KvmFacts(True, True, True, True, True, True, True, VLLM_IMAGE_DIGEST, NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_kvm_capabilities(facts, now=datetime(2026, 9, 23))
