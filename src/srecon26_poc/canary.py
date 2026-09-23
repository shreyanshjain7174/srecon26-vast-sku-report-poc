from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal


VLLM_IMAGE_DIGEST = "sha256:df2607b26bdda2875de4832f4d08da0055b4b6e3570347f3a849bcc652771dd6"
MAX_DIGEST_AGE = timedelta(minutes=5)
MAX_METRIC_AGE = timedelta(seconds=60)


@dataclass(frozen=True, slots=True)
class KvmFacts:
    """Facts probed from the current candidate host, never marketplace metadata."""

    systemd: bool
    cgroup_v2: bool
    privileged: bool
    containerd: bool
    nvidia_runtime: bool
    nvidia_smi: bool
    cuda: bool
    resolved_image_digest: str | None
    digest_resolved_at: datetime | None


@dataclass(frozen=True, slots=True)
class CapabilityDecision:
    allowed: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MetricSample:
    value: Decimal
    collected_at: datetime


@dataclass(frozen=True, slots=True)
class CanarySnapshot:
    node_allocatable_gpus: int
    device_plugin_ready: bool
    vllm_pod_ready: bool
    vllm_gpu_allocated: int
    prometheus_target_healthy: bool
    cpu: MetricSample | None
    queue: MetricSample | None
    kv: MetricSample | None
    warmup_request_succeeded: bool
    measured_request_succeeded: bool


@dataclass(frozen=True, slots=True)
class ReadinessDecision:
    """Readiness to issue measured traffic, distinct from measured-run completion."""

    allowed: bool
    blockers: tuple[str, ...]
    measured_request_succeeded: bool

    @property
    def measurement_complete(self) -> bool:
        return self.allowed and self.measured_request_succeeded


def evaluate_kvm_capabilities(
    facts: KvmFacts,
    *,
    expected_image_digest: str = VLLM_IMAGE_DIGEST,
    now: datetime | None = None,
    max_digest_age: timedelta = MAX_DIGEST_AGE,
) -> CapabilityDecision:
    """Fail closed unless fresh, directly-probed host and image facts are complete."""

    current_time = _normalise_now(now)
    blockers: list[str] = []
    for name, present in (
        ("systemd", facts.systemd),
        ("cgroup v2", facts.cgroup_v2),
        ("privileged container support", facts.privileged),
        ("containerd", facts.containerd),
        ("NVIDIA container runtime", facts.nvidia_runtime),
        ("nvidia-smi", facts.nvidia_smi),
        ("CUDA", facts.cuda),
    ):
        if not present:
            blockers.append(f"missing {name}")

    if not _is_sha256_digest(expected_image_digest):
        blockers.append("expected image digest is not an immutable sha256 digest")
    if facts.resolved_image_digest is None:
        blockers.append("current image digest resolution is missing")
    elif facts.resolved_image_digest != expected_image_digest:
        blockers.append("current image digest does not match the frozen run contract")
    if facts.digest_resolved_at is None:
        blockers.append("image digest resolution timestamp is missing")
    elif not _is_fresh(facts.digest_resolved_at, current_time, max_digest_age):
        blockers.append("image digest resolution is stale or has an invalid timestamp")

    return CapabilityDecision(allowed=not blockers, blockers=tuple(blockers))


def evaluate_canary_readiness(
    snapshot: CanarySnapshot,
    *,
    now: datetime | None = None,
    max_metric_age: timedelta = MAX_METRIC_AGE,
) -> ReadinessDecision:
    """Require every observation hop before measured workload traffic may begin."""

    current_time = _normalise_now(now)
    blockers: list[str] = []
    if snapshot.node_allocatable_gpus < 1:
        blockers.append("node has no allocatable GPU")
    if not snapshot.device_plugin_ready:
        blockers.append("NVIDIA device plugin is not ready")
    if not snapshot.vllm_pod_ready:
        blockers.append("vLLM pod is not ready")
    if snapshot.vllm_gpu_allocated != 1:
        blockers.append("vLLM pod does not hold exactly one allocated GPU")
    if not snapshot.prometheus_target_healthy:
        blockers.append("Prometheus vLLM target is unhealthy")
    for name, sample in (("CPU", snapshot.cpu), ("queue", snapshot.queue), ("KV", snapshot.kv)):
        if sample is None:
            blockers.append(f"{name} metric sample is absent")
        elif not sample.value.is_finite():
            blockers.append(f"{name} metric sample is not finite")
        elif not _is_fresh(sample.collected_at, current_time, max_metric_age):
            blockers.append(f"{name} metric sample is stale or has an invalid timestamp")
    if not snapshot.warmup_request_succeeded:
        blockers.append("vLLM warm-up request has not succeeded")

    return ReadinessDecision(
        allowed=not blockers,
        blockers=tuple(blockers),
        measured_request_succeeded=snapshot.measured_request_succeeded,
    )


def _normalise_now(now: datetime | None) -> datetime:
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return current_time


def _is_fresh(observed_at: datetime, now: datetime, maximum_age: timedelta) -> bool:
    if maximum_age < timedelta(0) or observed_at.tzinfo is None:
        return False
    age = now - observed_at
    return timedelta(0) <= age <= maximum_age


def _is_sha256_digest(value: str) -> bool:
    return len(value) == 71 and value.startswith("sha256:") and all(char in "0123456789abcdef" for char in value[7:])
