#!/usr/bin/env python3
"""Create reproducible, claim-bounded visuals for the lightning-talk design handoff."""
from __future__ import annotations

import argparse
import csv
import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping, Sequence
from uuid import UUID
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "artifacts" / "runs" / "inference-infer20260923184725" / "manual-benchmark"
OUT = ROOT / "artifacts" / "evidence-pack"
VERDICT = Path("/var/tmp/srecon26-verdict.json")
STANDALONE_RUN_ID = "inference-infer20260923184725"
PROVENANCE_LIMITATION_KEYS = frozenset({"pre_anchor", "benchmark", "model_revision", "absence"})
LOCAL_HPA_ARMS = {"cpu": "scaled_cpu_millicores", "kv": "scaled_kv", "queue": "scaled_queue"}
METRIC_PATH_ARTIFACT = "two-node-vllm-metric-path.svg"
CONTRAST_ARTIFACT = "gpu-ttft-tpot-contrast.svg"
PRESSURE_ARTIFACT = "gpu-observed-pressure.svg"
CAPACITY_ARTIFACT = "gpu-capacity-envelope.svg"
# Enforced per request by build_post_run_gpu_verdict.py's completion usage check.
CAPACITY_OUTPUT_TOKENS = 128
CAPACITY_CONCURRENCY = 32
CAPACITY_HEADROOM = 0.7
CAPACITY_TARGET_RPS = (5, 10, 20, 30)
CHART_ARMS = (("c4", 4), ("c32", 32))
CHART_MODEL = "Qwen/Qwen2.5-1.5B-Instruct"
# Enforced by build_post_run_gpu_verdict.py's nvidia-smi probe before any verdict is emitted.
CHART_GPU = "NVIDIA GeForce RTX 4090"
FONT = "Arial, Helvetica, sans-serif"
SUMMARY_ARTIFACT = "evidence-summary.json"
CATALOG_ARTIFACT = "artifact-catalog.json"
LOCAL_HPA_VERDICT_ARTIFACT = "local-hpa-verdict.json"
CANONICAL_PACK = "artifacts/evidence-pack"
CATALOG_CHARTS = (
    "gpu-latency-by-concurrency.svg", "local-hpa-signal-plumbing.svg", CONTRAST_ARTIFACT, PRESSURE_ARTIFACT,
    CAPACITY_ARTIFACT, "two-node-canary-startup.svg", METRIC_PATH_ARTIFACT,
)
CHART_PRESENTATION_ROLES = {
    "gpu-latency-by-concurrency.svg": "excluded",
    "local-hpa-signal-plumbing.svg": "excluded",
    CONTRAST_ARTIFACT: "appendix-only",
    PRESSURE_ARTIFACT: "appendix-only",
    CAPACITY_ARTIFACT: "appendix-only",
    "two-node-canary-startup.svg": "excluded",
    METRIC_PATH_ARTIFACT: "excluded",
}
# (number, headline, pill, charts, source_ids); production slides use native redraws and registered sources.
SLIDES = (
    (1, "Inference has more than one knob.", "SYNTHESIS - talk thesis", (), ()),
    (2, "Inference is a stack of layers.", "SYNTHESIS - talk thesis", (), ()),
    (3, "Start with workload math.", "SYNTHESIS - workload contract", (), ()),
    (4, "Prefill sets TTFT. Decode sets TPOT.", "PATTERN - request anatomy", (), ()),
    (5, "Capacity is a token budget.", "SYNTHESIS - worked example", (), ()),
    (6, "FP8 frees memory for more requests.", "PUBLISHED - vLLM docs and blog", (), ("vllm-fp8", "vllm-turboquant")),
    (7, "Three engine levers.", "PATTERN - serving techniques", (), ("vllm-x-omni", "vllm-x-v030")),
    (8, "Route to the cache.", "PUBLISHED - arXiv preprint, 8xA100", (), ("cache-routing-preprint",)),
    (9, "GPU utilization is a weak scaling signal.", "PUBLISHED - Google Cloud blog, L4 GPU", (), ("google-gke-hpa",)),
    (10, "Scale on engine signals.", "PATTERN - metrics pipeline", (), ()),
    (11, "Qwen3.8-27B on 8x RTX 4090.", "MEASURED - 8x RTX 4090, one host", (), ("tp8-bf16",)),
    (12, "At 8 concurrent, TTFT rose 7x.", "MEASURED - 8x RTX 4090, one host", (), ("tp8-bf16",)),
    (13, "Queue stayed at zero. TTFT still rose.", "MEASURED - 8x RTX 4090, one host", (), ("tp8-bf16",)),
    (14, "Scale against the SLO.", "PUBLISHED - KServe and llm-d docs", (), ("kserve-wva", "llmd-slo-aware")),
    (15, "FP8: less memory, mixed throughput.", "MEASURED - 8x RTX 4090, one host", (), ("tp8-precision",)),
    (16, "Instrument every layer.", "SYNTHESIS - bottom-up rule", (), ()),
)
# Sealed single-host runs; each directory holds run.json and SHA256SUMS (one subdirectory per precision arm when present).
MEASURED_RUNS: dict[str, tuple[str, ...]] = {
    "tp8-bf16": ("artifacts/live-runs/single-host-multigpu-20260927T132309Z-3a1e631f/remote-artifacts",),
    "tp8-precision": (
        "artifacts/live-runs/single-host-multigpu-20260928T074427Z-4050fa59/remote-artifacts",
        "artifacts/live-runs/single-host-multigpu-20260928T083252Z-04cb07e6/remote-artifacts",
    ),
}
_WEIGHT_LOG = re.compile(r"Model loading took ([0-9.]+) ?GiB")
_KV_TOKENS_LOG = re.compile(r"GPU KV cache size: ([0-9,]+) tokens")
_KV_MEMORY_LOG = re.compile(r"Available KV cache memory: ([0-9.]+) GiB")
PUBLISHED_ACCESSED = "2026-09-25"
PUBLISHED_SOURCES = {
    "vllm-fp8": {
        "title": "FP8 W8A8",
        "publisher": "vLLM documentation (v0.21.0)",
        "url": "https://docs.vllm.ai/en/v0.21.0/features/quantization/fp8/",
        "accessed": PUBLISHED_ACCESSED,
        "claim": "FP8 quantization allows a 2x reduction in model memory requirements and up to a 1.6x throughput improvement with minimal impact on accuracy.",
        "context": "Vendor documentation. W8A8 officially supported on Hopper and Ada Lovelace (compute capability >= 8.9); Turing/Ampere run weight-only W8A16 via Marlin. No benchmark setup is stated for the headline figures.",
    },
    "vllm-turboquant": {
        "title": "A First Comprehensive Study of TurboQuant: Accuracy and Performance",
        "publisher": "vLLM blog (Red Hat AI), 2026-05-11",
        "url": "https://vllm.ai/blog/2026-05-11-turboquant",
        "accessed": PUBLISHED_ACCESSED,
        "claim": "FP8 KV cache (--kv-cache-dtype fp8) provides 2x KV-cache capacity with negligible accuracy loss; Qwen3-30B on 2xH100 retained the same throughput at 2x capacity; under burst load on Llama-3.3-70B-Instruct, BF16 P99 TTFT reached ~17s while FP8 reached ~1.3s.",
        "context": "vLLM 0.20.2; Llama-3.3-70B-Instruct on 4xH100 and Qwen3-30B-A3B-Instruct-2507 on 2xH100; vllm bench serve with 1024 input / 512 output tokens, 300 prompts, request rates 2, 8, and inf. The burst TTFT gap is the memory-constrained Llama-70B case; on Qwen3-30B FP8 matched BF16.",
    },
    "cache-routing-preprint": {
        "title": "Evaluating Kubernetes Performance for GenAI Inference: From Automatic Speech Recognition to LLM Summarization",
        "publisher": "arXiv 2602.04900v3 preprint",
        "url": "https://arxiv.org/html/2602.04900",
        "accessed": PUBLISHED_ACCESSED,
        "claim": "Precise prefix-cache-aware scheduling (Gateway API Inference Extension with llm-d) cut average TTFT from ~500 ms with random scheduling to ~80 ms and reduced P99 TTFT by up to 90%.",
        "context": "arXiv v3 preprint. 8 vLLM replicas of Qwen3-8B, each on one NVIDIA A100 40GB (AWS p4d.24xlarge) on OpenShift 4.19; 256 summarization requests with ~8,500-token prompts generated by GuideLLM.",
    },
    "google-gke-hpa": {
        "title": "Save on GPUs: Smarter autoscaling for your GKE inferencing workloads",
        "publisher": "Google Cloud blog, 2024-10-24",
        "url": "https://cloud.google.com/blog/products/containers-kubernetes/tuning-the-gke-hpa-to-run-inference-on-gpus",
        "accessed": PUBLISHED_ACCESSED,
        "claim": "GPU utilization is not an effective HPA metric for LLM inference and tends to overprovision; under 150% traffic spikes with an HPA range of 1-16 replicas, queue target 25 kept TGI mean time per token below ~0.4 s and batch target 50 kept it almost below ~0.3 s.",
        "context": "TGI serving Llama 2 7B on single-L4 GPU nodes (g2-standard-16 and g2-standard-96) with HPA via the custom metrics Stackdriver adapter; thresholds identified experimentally. Stated as applicable to servers with similar metrics such as vLLM.",
    },
    "kserve-wva": {
        "title": "Autoscaling LLMInferenceService with WVA",
        "publisher": "KServe documentation (v0.20)",
        "url": "https://kserve.github.io/website/docs/model-serving/generative-inference/llmisvc/autoscaling/llmisvc-autoscaling",
        "accessed": PUBLISHED_ACCESSED,
        "claim": "The Workload Variant Autoscaler scales LLMInferenceService on inference-specific signals such as KV cache utilization, queue depth, and saturation instead of CPU or memory, actuated through HPA or KEDA.",
        "context": "Design and configuration documentation, not a benchmark.",
    },
    "llmd-slo-aware": {
        "title": "SLO-Aware Autoscaling with KEDA - the control law",
        "publisher": "llm-d documentation (dev, unreleased)",
        "url": "https://llm-d.ai/docs/dev/architecture/advanced/autoscaling/slo-aware-keda",
        "accessed": PUBLISHED_ACCESSED,
        "claim": "SLO-aware autoscaling derives a saturation signal from pool P90 TTFT and P90 TPOT relative to their SLO targets and scales with a 0.40 to 0.55 hysteresis band.",
        "context": "Control-law design documentation; its reported settle point is ~1.4 to 1.5 rps per H100 TP=2 replica for a 4000-token-prefill workload at the guide's SLOs.",
    },
    "vllm-x-omni": {
        "title": "Under the hood, vLLM-Omni",
        "publisher": "vLLM official X account, 2026-09-20",
        "url": "https://x.com/vllm_project/status/2101665925137379695",
        "accessed": PUBLISHED_ACCESSED,
        "claim": "vLLM-Omni includes cross-step prefix KV reuse, dedicated CUDA Graphs, request-level and step-level continuous batching, phase-aware prefill/decode scheduling, and FP8 prefix KV storage.",
        "context": "Official project post describing available techniques, not a benchmark. No performance number from the post is used.",
    },
    "vllm-x-v030": {
        "title": "vLLM v0.30.0 release highlights",
        "publisher": "vLLM official X account, 2026-09-23",
        "url": "https://x.com/vllm_project/status/2102593516740411733",
        "accessed": PUBLISHED_ACCESSED,
        "claim": "vLLM v0.30.0 Model Runner V2 brings EAGLE3-style drafts to pipeline parallelism and adaptive verification through online acceptance estimation; release also adds MXFP8 KV and Fast Start weight reuse.",
        "context": "Official project release post describing available features, not a benchmark. No performance number from the post is used.",
    },
}

COLORS = {"ink": "#152238", "blue": "#155EEF", "orange": "#D97600", "green": "#0C8B6B", "muted": "#52616B", "grid": "#D8DEE4"}
SHA256 = re.compile(r"^[0-9a-f]{64}$")
NONCE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
HOST_KEY_FINGERPRINT = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
AZURE_RESOURCE_ID = re.compile(
    r"^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\.Compute/virtualMachines/[^/]+$",
    re.IGNORECASE,
)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _remote_host(value: object) -> bool:
    if not isinstance(value, str) or not value or value.casefold() in {"localhost", "localhost.localdomain"}:
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return True
    return not (address.is_loopback or address.is_unspecified or address.is_multicast)


def _uuid(value: object) -> bool:
    try:
        UUID(str(value))
    except ValueError:
        return False
    return isinstance(value, str)


def _safe_artifact(run_dir: Path, record: Mapping[str, object]) -> tuple[Path, bytes] | None:
    name, digest = record.get("artifact"), record.get("sha256")
    if not isinstance(name, str) or Path(name).name != name or not isinstance(digest, str) or not SHA256.fullmatch(digest):
        return None
    path = run_dir / name
    if not path.is_file() or path.is_symlink():
        return None
    raw = path.read_bytes()
    return (path, raw) if hashlib.sha256(raw).hexdigest() == digest else None


def _json_artifact(run_dir: Path, record: Mapping[str, object]) -> Mapping[str, object] | None:
    artifact = _safe_artifact(run_dir, record)
    if artifact is None:
        return None
    try:
        payload = json.loads(artifact[1])
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def validate_bound_guard_receipts(manifest: Mapping[str, object]) -> tuple[bool, dict[str, str]]:
    """Validate both exact Azure arm bindings, never just their status text."""

    errors: dict[str, str] = {}
    guards = manifest.get("guards")
    deadline = _timestamp(manifest.get("hard_deadline"))
    if manifest.get("guard_backend") != "azure" or not isinstance(guards, Mapping) or deadline is None:
        return False, {"manifest": "missing exact Azure guard binding context"}
    independent: dict[str, list[str]] = {
        "host_identity": [], "azure_resource_id": [], "azure_vm_id": [], "host_key_fingerprint": [],
    }
    heartbeat_timeout = manifest.get("guard_heartbeat_timeout_seconds")
    for role in ("server", "worker"):
        target = manifest.get(role)
        receipt = guards.get(role)
        offer = target.get("offer") if isinstance(target, Mapping) else None
        nonce = target.get("nonce") if isinstance(target, Mapping) else None
        label = offer.get("label") if isinstance(offer, Mapping) else None
        valid = (
            isinstance(receipt, Mapping)
            and receipt.get("backend") == "azure"
            and receipt.get("role") == role
            and receipt.get("status") == "ARMED"
            and isinstance(nonce, str)
            and NONCE.fullmatch(nonce) is not None
            and receipt.get("nonce") == nonce
            and isinstance(label, str)
            and label == f"srecon26-two-node-{role}--nonce-{nonce}"
            and receipt.get("label") == label
            and _timestamp(receipt.get("hard_deadline")) == deadline
            and isinstance(receipt.get("root_hash"), str)
            and SHA256.fullmatch(str(receipt.get("root_hash"))) is not None
            and isinstance(receipt.get("script_hash"), str)
            and SHA256.fullmatch(str(receipt.get("script_hash"))) is not None
            and _remote_host(receipt.get("host_identity"))
            and _timestamp(receipt.get("last_heartbeat")) is not None
            and isinstance(receipt.get("azure_resource_id"), str)
            and AZURE_RESOURCE_ID.fullmatch(str(receipt.get("azure_resource_id"))) is not None
            and isinstance(receipt.get("azure_vm_id"), str)
            and _uuid(receipt.get("azure_vm_id"))
            and isinstance(receipt.get("host_key_fingerprint"), str)
            and HOST_KEY_FINGERPRINT.fullmatch(str(receipt.get("host_key_fingerprint"))) is not None
            and isinstance(heartbeat_timeout, int)
            and receipt.get("heartbeat_timeout_seconds") == heartbeat_timeout
        )
        if not valid:
            errors[role] = "receipt is not bound to the exact Azure role, nonce, label, deadline, host, and script"
        else:
            assert isinstance(receipt, Mapping)
            for field in independent:
                independent[field].append(str(receipt[field]).casefold())
    for field, values in independent.items():
        if len(values) == 2 and values[0] == values[1]:
            errors[f"independence_{field}"] = f"guard receipts use the same attested {field}"
    return not errors, errors


def validate_guard_journal(
    run_dir: Path,
    *,
    role: str,
    receipt: Mapping[str, object],
    finalization: Mapping[str, object],
) -> bool:
    """Verify the exported guard journal's file hash and event hash chain."""

    record = {
        "artifact": finalization.get("journal_artifact"),
        "sha256": finalization.get("journal_sha256"),
    }
    artifact = _safe_artifact(run_dir, record)
    nonce, label = receipt.get("nonce"), receipt.get("label")
    final_root = finalization.get("root_hash")
    if (
        artifact is None
        or not isinstance(nonce, str)
        or not isinstance(label, str)
        or not isinstance(final_root, str)
        or not SHA256.fullmatch(final_root)
    ):
        return False
    root = "0" * 64
    records: list[Mapping[str, object]] = []
    try:
        lines = artifact[1].decode("utf-8").splitlines()
        for sequence, line in enumerate(lines, start=1):
            event = json.loads(line)
            if not isinstance(event, Mapping):
                return False
            unsigned = {key: value for key, value in event.items() if key != "event_hash"}
            encoded = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
            event_hash = hashlib.sha256(encoded).hexdigest()
            if event.get("sequence") != sequence or event.get("previous_hash") != root or event.get("event_hash") != event_hash or event.get("nonce") != nonce:
                return False
            root = event_hash
            records.append(event)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return False
    if not records or root != final_root or records[0].get("event") != "armed" or records[0].get("event_hash") != receipt.get("root_hash"):
        return False
    first_payload = records[0].get("payload")
    return role in {"server", "worker"} and isinstance(first_payload, Mapping) and first_payload.get("label") == label


def post_deadline_absence(status: Mapping[str, object], deadline: datetime, current: datetime) -> bool:
    """Require three later reads, even if heartbeat loss authorized teardown early."""

    authority = _timestamp(status.get("teardown_authority_at"))
    observations = status.get("absence_observations")
    if (
        status.get("status") != "ABSENCE_CONFIRMED"
        or current < deadline
        or authority is None
        or not isinstance(observations, list)
        or len(observations) != 3
    ):
        return False
    stamps = [_timestamp(value) for value in observations]
    return (
        all(stamp is not None and max(deadline, authority) < stamp <= current for stamp in stamps)
        and stamps == sorted(set(stamps))
    )


def validate_deferred_guard_evidence(
    run_dir: Path, role: str, receipt: Mapping[str, object], finalization: Mapping[str, object],
    *, current: datetime | None = None,
) -> bool:
    """Bind deferred absence to its original arm and durable journal events."""

    deadline = _timestamp(receipt.get("hard_deadline"))
    if (
        deadline is None
        or not post_deadline_absence(finalization, deadline, current or datetime.now(UTC))
        or any(finalization.get(field) != receipt.get(field) for field in ("nonce", "label"))
        or _timestamp(finalization.get("hard_deadline")) != deadline
        or not validate_guard_journal(run_dir, role=role, receipt=receipt, finalization=finalization)
    ):
        return False
    artifact = _safe_artifact(run_dir, {
        "artifact": finalization.get("journal_artifact"), "sha256": finalization.get("journal_sha256"),
    })
    assert artifact is not None
    records = [json.loads(line) for line in artifact[1].decode("utf-8").splitlines()]
    authority = None
    observations: list[datetime] = []
    for record in records:
        event, payload = record.get("event"), record.get("payload")
        if not isinstance(payload, Mapping):
            return False
        if event == "teardown_authority_activated":
            if authority is not None:
                return False
            authority = _timestamp(payload.get("authority_at"))
            if authority is None:
                return False
        elif event == "absence_quorum_reset":
            observations = []
        elif event == "absence_observation":
            stamp = _timestamp(payload.get("observed_at"))
            if (
                authority is None or stamp is None or stamp <= authority
                or (observations and stamp <= observations[-1])
                or payload.get("label") != receipt.get("label")
                or payload.get("observation_number") != len(observations) + 1
            ):
                return False
            observations.append(stamp)
    return (
        records[-1].get("event") in {"absence_observation", "absence_confirmed"}
        and authority == _timestamp(finalization.get("teardown_authority_at"))
        and observations == [_timestamp(value) for value in finalization["absence_observations"]]
    )


def validate_absence_artifact(run_dir: Path, role: str, target: Mapping[str, object], record: Mapping[str, object]) -> bool:
    instance = target.get("instance")
    if not isinstance(instance, Mapping) or record.get("status") != "THREE_READS_CONFIRMED":
        return False
    instance_id, label, nonce = instance.get("instance_id"), instance.get("label"), target.get("nonce")
    payload = _json_artifact(run_dir, record)
    reads = payload.get("reads") if isinstance(payload, Mapping) else None
    stamps = [_timestamp(read.get("observed_at")) for read in reads] if isinstance(reads, list) and all(isinstance(read, Mapping) for read in reads) else []
    return (
        isinstance(payload, Mapping)
        and payload.get("schema") == "srecon26-vast-absence-evidence/v1"
        and payload.get("source") == "VastCliProvider.capture_absence_evidence/v1"
        and payload.get("run_id") == f"two-node-{role}-{nonce}"
        and payload.get("instance_id") == instance_id
        and payload.get("label") == label
        and isinstance(reads, list)
        and len(reads) == 3
        and all(read.get("matching_instances") == 0 for read in reads)
        and all(stamp is not None for stamp in stamps)
        and stamps == sorted(set(stamps))
    )


def _money(value: object) -> Decimal | None:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def validate_billing_artifact(run_dir: Path, role: str, target: Mapping[str, object], record: Mapping[str, object]) -> bool:
    instance = target.get("instance")
    if not isinstance(instance, Mapping) or record.get("status") != "AUTHORITATIVE_INVOICE_CAPTURED":
        return False
    instance_id, label, nonce = instance.get("instance_id"), instance.get("label"), target.get("nonce")
    payload = _json_artifact(run_dir, record)
    charge = payload.get("provider_charge") if isinstance(payload, Mapping) else None
    metadata = charge.get("metadata") if isinstance(charge, Mapping) else None
    amount = _money(payload.get("amount_usd")) if isinstance(payload, Mapping) else None
    return (
        isinstance(payload, Mapping)
        and payload.get("schema") == "srecon26-vast-invoice-evidence/v1"
        and payload.get("source") == "VastCliProvider.capture_invoice_charge/v1"
        and payload.get("run_id") == f"two-node-{role}-{nonce}"
        and payload.get("instance_id") == instance_id
        and payload.get("label") == label
        and amount is not None
        and _money(record.get("amount_usd")) == amount
        and isinstance(charge, Mapping)
        and charge.get("type") == "instance"
        and charge.get("source") == f"instance-{instance_id}"
        and _money(charge.get("amount")) == amount
        and isinstance(metadata, Mapping)
        and metadata.get("label") == label
        and _timestamp(payload.get("observed_at")) is not None
    )


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def request_data() -> dict[str, list[dict[str, object]]]:
    requests: dict[str, list[dict[str, object]]] = {"c4": [], "c32": []}
    for path in RUN.glob("*-request-*.json"):
        if path.name.endswith("-curl.json"):
            continue
        payload = json.loads(path.read_text())
        arm = payload.get("arm")
        if arm in requests:
            requests[arm].append(payload)
    for values in requests.values():
        values.sort(key=lambda item: int(item["request_number"]))
    return requests


def svg_chart(title: str, subtitle: str, panels: list[tuple[str, list[str], list[float], str, str]]) -> str:
    width, height = 1600, 760
    margin, panel_gap = 78, 48
    panel_width = (width - margin * 2 - panel_gap * (len(panels) - 1)) / len(panels)
    chunks = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">', '<rect width="100%" height="100%" fill="#ffffff"/>', f'<text x="{margin}" y="62" font-family="Arial, sans-serif" font-size="34" font-weight="700" fill="{COLORS["ink"]}">{escape(title)}</text>', f'<text x="{margin}" y="100" font-family="Arial, sans-serif" font-size="19" fill="{COLORS["muted"]}">{escape(subtitle)}</text>']
    for index, (panel_title, labels, values, color, unit) in enumerate(panels):
        left = margin + index * (panel_width + panel_gap)
        top, bottom = 180, 620
        maximum = max(values) * 1.2 if max(values) else 1
        chunks.append(f'<text x="{left}" y="150" font-family="Arial, sans-serif" font-size="24" font-weight="700" fill="{COLORS["ink"]}">{escape(panel_title)}</text>')
        for tick in range(5):
            value = maximum * tick / 4
            y = bottom - (bottom - top) * tick / 4
            chunks.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + panel_width}" y2="{y:.1f}" stroke="{COLORS["grid"]}" stroke-width="1"/>')
            chunks.append(f'<text x="{left - 8}" y="{y + 5:.1f}" text-anchor="end" font-family="Arial, sans-serif" font-size="14" fill="{COLORS["muted"]}">{value:.0f}</text>')
        slot = panel_width / len(values)
        bar_width = slot * 0.58
        for item, (label, value) in enumerate(zip(labels, values, strict=True)):
            x = left + slot * item + (slot - bar_width) / 2
            bar_height = (value / maximum) * (bottom - top)
            y = bottom - bar_height
            chunks.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" rx="3" fill="{color}"/>')
            chunks.append(f'<text x="{x + bar_width / 2:.1f}" y="{y - 10:.1f}" text-anchor="middle" font-family="Arial, sans-serif" font-size="16" font-weight="700" fill="{COLORS["ink"]}">{value:.1f}</text>')
            chunks.append(f'<text x="{x + bar_width / 2:.1f}" y="{bottom + 30}" text-anchor="middle" font-family="Arial, sans-serif" font-size="15" fill="{COLORS["muted"]}">{escape(label)}</text>')
        chunks.append(f'<text x="{left}" y="{bottom + 72}" font-family="Arial, sans-serif" font-size="16" fill="{COLORS["muted"]}">{escape(unit)}</text>')
    chunks.append('</svg>')
    return "\n".join(chunks)


def verified_standalone_gpu_verdict(root: Path = ROOT) -> Mapping[str, object]:
    """Verify raw standalone evidence before exposing any GPU latency output."""

    run_id = STANDALONE_RUN_ID
    verdict_path = root / "artifacts" / "runs" / run_id / "post-run" / "verdict.json"
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "build_post_run_gpu_verdict.py"), "--verify", "--print-verified", "--root", str(root)],
        cwd=root,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join((str(root), str(root / "src"))),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        suffix = f": {detail}" if detail else ""
        raise SystemExit(f"post-run standalone GPU verification failed{suffix}")

    # Only the verifier's recomputed output is trusted; disk snapshots can be swapped around the call (ABA).
    try:
        verdict = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"verified post-run GPU verdict is unreadable: {exc}") from exc
    if not isinstance(verdict, dict):
        raise SystemExit("verified post-run GPU verdict has invalid shape")
    try:
        canonical = json.dumps(verdict, indent=2, sort_keys=True, allow_nan=False) + "\n"
    except ValueError as exc:
        raise SystemExit(f"verified post-run GPU verdict is not canonical: {exc}") from exc
    if result.stdout != canonical:
        raise SystemExit("verified post-run GPU verdict output is not canonical")
    try:
        persisted = json.loads(verdict_path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        persisted = None
    if persisted != verdict:
        raise SystemExit("post-run GPU verdict changed during verification: persisted artifact does not match verified canonical data")

    latency = verdict.get("latency_ms")
    standalone = verdict.get("standalone_gpu_latency")
    observed = verdict.get("prometheus_observed")
    limitations = verdict.get("provenance_limitations")
    basis = standalone.get("basis") if isinstance(standalone, Mapping) else None
    valid = (
        verdict.get("schema") == "srecon26-standalone-gpu-post-run/v1"
        and verdict.get("run_id") == run_id
        and verdict.get("raw_file_count") == 125
        and isinstance(standalone, Mapping)
        and standalone.get("allowed") is True
        and isinstance(basis, str)
        and "not independently anchored" in basis
        and isinstance(verdict.get("kubernetes_hpa"), Mapping)
        and verdict["kubernetes_hpa"].get("allowed") is False
        and isinstance(verdict.get("paired_hpa_performance"), Mapping)
        and verdict["paired_hpa_performance"].get("allowed") is False
        and isinstance(latency, Mapping)
        and isinstance(observed, Mapping)
        and isinstance(limitations, Mapping)
        and set(limitations) == PROVENANCE_LIMITATION_KEYS
        and all(isinstance(value, str) and value.strip() for value in limitations.values())
        and "not independently anchor" in str(limitations.get("benchmark"))
    )
    if not valid:
        raise SystemExit("verified post-run GPU verdict is missing required schema, claims, metrics, or limitations")
    for arm, count in (("c4", 4), ("c32", 32)):
        arm_latency = latency.get(arm)
        arm_observed = observed.get(arm)
        if not isinstance(arm_latency, Mapping) or arm_latency.get("requests") != count or not isinstance(arm_observed, Mapping):
            raise SystemExit(f"verified post-run GPU verdict has invalid {arm} samples")
        for field in ("p50_ttft_ms", "p95_ttft_ms", "p50_e2e_ms", "p95_e2e_ms", "p50_tpot_ms", "p95_tpot_ms"):
            value = arm_latency.get(field)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise SystemExit(f"verified post-run GPU verdict has invalid {arm} metric: {field}")
    return verdict


def _finite_number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)  # type: ignore[arg-type]


def validated_local_hpa_verdict(verdict: object) -> Mapping[str, object]:
    """Accept only an analyzer VALID verdict whose three isolated arms each reached 2 desired and ready."""

    claims = verdict.get("claims") if isinstance(verdict, Mapping) else None
    claim = claims.get("local_hpa_signal_plumbing") if isinstance(claims, Mapping) else None
    local = verdict.get("local_evidence") if isinstance(verdict, Mapping) else None
    arms = local.get("arms") if isinstance(local, Mapping) else None
    if not (
        isinstance(verdict, Mapping)
        and verdict.get("verdict") == "VALID"
        and verdict.get("failing_checks") == []
        and isinstance(claim, Mapping)
        and claim.get("allowed") is True
        and claim.get("verdict") == "VALID"
        and claim.get("failing_checks") == []
        and isinstance(local, Mapping)
        and local.get("verdict") == "VALID"
        and local.get("failing_checks") == []
        and isinstance(arms, list)
        and len(arms) == len(LOCAL_HPA_ARMS)
        and all(isinstance(item, Mapping) for item in arms)
        and {item.get("arm") for item in arms} == set(LOCAL_HPA_ARMS)
    ):
        raise SystemExit("local HPA verdict is not an allowed VALID signal-plumbing claim")
    for item in arms:
        observations = item.get("observations")
        if not (
            item.get("verdict") == "VALID"
            and item.get("failing_checks") == []
            and isinstance(observations, Mapping)
            and observations.get("desired_replicas") == 2
            and observations.get("ready_replicas") == 2
            and _finite_number(observations.get(LOCAL_HPA_ARMS[str(item["arm"])]))
        ):
            raise SystemExit(f"local HPA verdict arm is not VALID: {item.get('arm')}")
    return verdict


def _gpu_latency_svg(latency: Mapping[str, object]) -> str:
    labels = ["c4 p50", "c4 p95", "c32 p50", "c32 p95"]
    ttft = []
    e2e = []
    for arm in ("c4", "c32"):
        arm_latency = latency[arm]
        assert isinstance(arm_latency, Mapping)
        ttft.extend((float(arm_latency["p50_ttft_ms"]), float(arm_latency["p95_ttft_ms"])))
        e2e.extend((float(arm_latency["p50_e2e_ms"]), float(arm_latency["p95_e2e_ms"])))
    return svg_chart("Standalone vLLM latency, locally checksum-verified", "Qwen2.5-1.5B, 128 completion tokens; c4 n=4, c32 n=32; benchmark capture not independently anchored", [("TTFT", labels, ttft, COLORS["orange"], "milliseconds"), ("End-to-end latency", labels, e2e, COLORS["blue"], "milliseconds")])


def gpu_latency_chart(latency: Mapping[str, object]) -> None:
    (OUT / "gpu-latency-by-concurrency.svg").write_text(_gpu_latency_svg(latency))


def _capacity_projection(standalone: Mapping[str, object]) -> tuple[dict[str, object], str]:
    """Project a token budget from the verified c32 p95 E2E; target-RPS values are never measured."""

    latency = standalone.get("latency_ms")
    c32 = latency.get("c32") if isinstance(latency, Mapping) else None
    if not isinstance(c32, Mapping) or c32.get("requests") != CAPACITY_CONCURRENCY:
        raise SystemExit("verified post-run GPU verdict has invalid c32 samples for capacity projection")
    p95_e2e_ms = c32.get("p95_e2e_ms")
    if not _finite_number(p95_e2e_ms) or p95_e2e_ms <= 0:  # type: ignore[operator]
        raise SystemExit("verified post-run GPU verdict has invalid c32 p95_e2e_ms for capacity projection")
    rps = CAPACITY_CONCURRENCY / (float(p95_e2e_ms) / 1000)  # type: ignore[arg-type]
    tps = rps * CAPACITY_OUTPUT_TOKENS
    budget = tps * CAPACITY_HEADROOM
    projected = {f"{target}_rps": math.floor(budget / target) for target in CAPACITY_TARGET_RPS}
    if not all(math.isfinite(value) and value > 0 for value in (rps, tps, budget)) or not all(value > 0 for value in projected.values()):
        raise SystemExit("capacity projection produced a non-finite or non-positive value")
    projection: dict[str, object] = {
        "status": "projected",
        "claim": "PROJECTED, not measured at target RPS",
        "basis": "Little's Law proxy from c32 concurrency and p95 E2E",
        "run_id": standalone.get("run_id"),
        "measured": {"concurrency": CAPACITY_CONCURRENCY, "output_tokens": CAPACITY_OUTPUT_TOKENS, "p95_e2e_ms": p95_e2e_ms},
        "burst_completion_rate_proxy_rps": rps,
        "output_token_rate_proxy_tps": tps,
        "operating_headroom_fraction": CAPACITY_HEADROOM,
        "projected_max_output_tokens": projected,
        "artifact": CAPACITY_ARTIFACT,
    }
    svg = svg_chart(
        "GPU capacity envelope: token budget at 70% headroom",
        f"PROJECTED, not measured at target RPS. Proxy = 32 / c32 p95 E2E ({float(p95_e2e_ms):.2f} ms), 128 output tokens; 1x {CHART_GPU}, {CHART_MODEL}",  # type: ignore[arg-type]
        [
            ("c32 completion-rate proxy", ["c32 n=32"], [rps], COLORS["blue"], "requests/s (Little's Law proxy)"),
            ("Output-token rate proxy", ["c32 n=32"], [tps], COLORS["green"], "output tokens/s"),
            (
                "Max output tokens per request", [f"{target} RPS" for target in CAPACITY_TARGET_RPS],
                [float(value) for value in projected.values()], COLORS["orange"], "tokens - PROJECTED, not measured at target RPS",
            ),
        ],
    )
    return projection, svg


def _nonnegative(value: object, name: str) -> float:
    if not _finite_number(value) or value < 0:  # type: ignore[operator]
        raise SystemExit(f"verified post-run GPU verdict has invalid chart metric: {name}")
    return float(value)  # type: ignore[arg-type]


def _series_max(observed: Mapping[str, object], arm: str, metric: str) -> float:
    series = observed.get(metric)
    samples = series.get("samples") if isinstance(series, Mapping) else None
    if type(samples) is not int or samples <= 0:
        raise SystemExit(f"verified post-run GPU verdict has no {arm} samples for {metric}")
    assert isinstance(series, Mapping)
    return _nonnegative(series.get("max"), f"{arm} {metric}")


def gpu_chart_metrics(verdict: Mapping[str, object]) -> dict[str, object]:
    """Extract chart values only from the in-memory, freshly verified standalone verdict."""

    latency, observed = verdict.get("latency_ms"), verdict.get("prometheus_observed")
    if not isinstance(latency, Mapping) or not isinstance(observed, Mapping):
        raise SystemExit("verified post-run GPU verdict is missing chart metrics")
    if verdict.get("model") != CHART_MODEL:
        raise SystemExit("verified post-run GPU verdict has unexpected chart model")
    arms: dict[str, dict[str, float | int]] = {}
    for arm, count in CHART_ARMS:
        arm_latency, arm_observed = latency.get(arm), observed.get(arm)
        if not isinstance(arm_latency, Mapping) or arm_latency.get("requests") != count or not isinstance(arm_observed, Mapping):
            raise SystemExit(f"verified post-run GPU verdict has invalid {arm} chart samples")
        kv_fraction = _series_max(arm_observed, arm, "vllm:kv_cache_usage_perc")
        if kv_fraction > 1:
            raise SystemExit(f"verified post-run GPU verdict has invalid chart metric: {arm} KV fraction above 1")
        arms[arm] = {
            "requests": count,
            "p50_ttft_ms": _nonnegative(arm_latency.get("p50_ttft_ms"), f"{arm} p50_ttft_ms"),
            "p50_tpot_ms": _nonnegative(arm_latency.get("p50_tpot_ms"), f"{arm} p50_tpot_ms"),
            "max_waiting_requests": _series_max(arm_observed, arm, "vllm:num_requests_waiting"),
            "max_running_requests": _series_max(arm_observed, arm, "vllm:num_requests_running"),
            "max_kv_cache_percent": kv_fraction * 100,
        }
    ratios: dict[str, float] = {}
    for field in ("p50_ttft_ms", "p50_tpot_ms"):
        base = float(arms["c4"][field])
        if base <= 0:
            raise SystemExit(f"verified post-run GPU verdict has zero c4 {field}; ratio is undefined")
        ratios[field] = float(arms["c32"][field]) / base
    return {
        "run_id": verdict.get("run_id"),
        "model": CHART_MODEL,
        "gpu": CHART_GPU,
        "source": "in-memory output of build_post_run_gpu_verdict.py --verify --print-verified",
        "arms": arms,
        "c32_over_c4": ratios,
        "artifacts": [CONTRAST_ARTIFACT, PRESSURE_ARTIFACT],
    }


def _nice_ceiling(value: float) -> float:
    if value <= 0:
        return 1.0
    exponent = math.floor(math.log10(value))
    for step in (1, 2, 2.5, 5, 10):
        if step * 10 ** exponent >= value:
            return float(step * 10 ** exponent)
    return float(10 ** (exponent + 1))


def _text(
    x: float, y: float, text: str, size: int, *, fill: str = COLORS["ink"], weight: str = "400", anchor: str = "start", halo: bool = False,
) -> str:
    stroke = ' stroke="#ffffff" stroke-width="8" stroke-linejoin="round" paint-order="stroke"' if halo else ""
    return f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" font-family="{FONT}" font-size="{size}" font-weight="{weight}" fill="{fill}"{stroke}>{escape(text)}</text>'


def _bar_panel(
    left: float, width: float, top: float, bottom: float, title: str, caption: str,
    bars: Sequence[tuple[str, float, str, str]], axis_max: float, tick_label: str,
) -> list[str]:
    """One bordered panel; the caption must describe the axis_max actually drawn."""

    plot_left = left + 96
    plot_width = width - 96
    chunks = [
        _text(left + 20, top - 58, title, 30, weight="700"),
        _text(left + 20, top - 24, caption, 24, fill=COLORS["muted"]),
        f'<rect x="{left:.1f}" y="{top - 100:.1f}" width="{width:.1f}" height="{bottom - top + 200:.1f}" rx="10" fill="none" stroke="{COLORS["grid"]}" stroke-width="2"/>',
    ]
    for tick in range(6):
        value = round(axis_max * tick / 5, 6)
        y = bottom - (bottom - top) * tick / 5
        chunks.append(f'<line x1="{plot_left:.1f}" y1="{y:.1f}" x2="{plot_left + plot_width - 16:.1f}" y2="{y:.1f}" stroke="{COLORS["grid"]}" stroke-width="1.5"/>')
        chunks.append(_text(plot_left - 12, y + 9, tick_label.format(value), 26, fill=COLORS["muted"], anchor="end"))
    chunks.append(f'<line x1="{plot_left:.1f}" y1="{top:.1f}" x2="{plot_left:.1f}" y2="{bottom:.1f}" stroke="{COLORS["muted"]}" stroke-width="2"/>')
    slot = (plot_width - 16) / len(bars)
    bar_width = slot * 0.56
    for index, (label, value, display, color) in enumerate(bars):
        x = plot_left + slot * index + (slot - bar_width) / 2
        height = max(value / axis_max * (bottom - top), 0.0)
        if height > 0:
            chunks.append(f'<rect x="{x:.1f}" y="{bottom - height:.1f}" width="{bar_width:.1f}" height="{height:.1f}" rx="4" fill="{color}"/>')
        else:
            chunks.append(f'<rect x="{x:.1f}" y="{bottom - 3:.1f}" width="{bar_width:.1f}" height="6" fill="{color}"/>')
        chunks.append(_text(x + bar_width / 2, bottom - max(height, 3) - 16, display, 28, weight="700", anchor="middle", halo=True))
        chunks.append(_text(x + bar_width / 2, bottom + 40, label, 24, weight="700", fill=color, anchor="middle"))
    return chunks


def _svg_document(width: int, height: int, title: str, body: Sequence[str]) -> str:
    return "\n".join([
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">',
        f"<title>{escape(title)}</title>",
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        *body,
        "</svg>",
    ]) + "\n"


def _arm_labels(arms: Mapping[str, Mapping[str, float | int]]) -> list[tuple[str, str]]:
    return [(f"c4 (n={arms['c4']['requests']})", COLORS["blue"]), (f"c32 (n={arms['c32']['requests']})", COLORS["orange"])]


def _provenance(metrics: Mapping[str, object], top: float) -> list[str]:
    return [
        _text(80, top, f"Source: post-run verifier output, run {metrics['run_id']}.", 24, fill=COLORS["muted"]),
        _text(80, top + 32, "Locally checksum-verified; benchmark capture not independently anchored.", 24, fill=COLORS["muted"]),
    ]


def _context(metrics: Mapping[str, object]) -> str:
    return f"{metrics['model']} \u00b7 1\u00d7 {metrics['gpu']}"


def _contrast_svg(metrics: Mapping[str, object]) -> str:
    arms = metrics["arms"]
    ratios = metrics["c32_over_c4"]
    assert isinstance(arms, Mapping) and isinstance(ratios, Mapping)
    labels = _arm_labels(arms)
    ttft, tpot = float(ratios["p50_ttft_ms"]), float(ratios["p50_tpot_ms"])
    title = f"c4 \u2192 c32: p50 TTFT {ttft:.2f}\u00d7, p50 TPOT {tpot:.2f}\u00d7"
    fields = (("p50_ttft_ms", "p50 time to first token (TTFT)"), ("p50_tpot_ms", "p50 time per output token (TPOT)"))
    peak = max(float(arms[arm][field]) for field, _ in fields for arm in ("c4", "c32"))
    axis_max = 25.0 if peak <= 25 else _nice_ceiling(peak * 1.15)
    body = [
        _text(80, 70, title, 44, weight="700"),
        _text(80, 112, "Median per-request latency in milliseconds, both metrics on one shared axis.", 26, fill=COLORS["muted"]),
        _text(80, 150, f"{_context(metrics)} \u00b7 standalone vLLM on one rented VM", 24, fill=COLORS["muted"]),
    ]
    plot_left, plot_right, top, bottom = 170, 1520, 260, 660
    body.append(_text(plot_left, top - 30, f"Shared axis: 0 to {axis_max:g} ms", 24, fill=COLORS["muted"]))
    for tick in range(6):
        y = bottom - (bottom - top) * tick / 5
        body.append(f'<line x1="{plot_left}" y1="{y:.1f}" x2="{plot_right}" y2="{y:.1f}" stroke="{COLORS["grid"]}" stroke-width="1.5"/>')
        body.append(_text(plot_left - 14, y + 9, f"{round(axis_max * tick / 5, 6):g} ms", 26, fill=COLORS["muted"], anchor="end"))
    body.append(f'<line x1="{plot_left}" y1="{top}" x2="{plot_left}" y2="{bottom}" stroke="{COLORS["muted"]}" stroke-width="2"/>')
    group = (plot_right - plot_left) / 2
    bar_width = 170.0
    for index, (field, name) in enumerate(fields):
        center = plot_left + group * index + group / 2
        for offset, ((label, color), arm) in zip((-110, 110), zip(labels, ("c4", "c32"), strict=True), strict=True):
            value = float(arms[arm][field])
            height = value / axis_max * (bottom - top)
            x = center + offset - bar_width / 2
            body.append(f'<rect x="{x:.1f}" y="{bottom - height:.1f}" width="{bar_width:.1f}" height="{height:.1f}" rx="4" fill="{color}"/>')
            body.append(_text(x + bar_width / 2, bottom - height - 16, f"{value:.3f} ms", 30, weight="700", anchor="middle", halo=True))
            body.append(_text(x + bar_width / 2, bottom + 36, label, 24, weight="700", fill=color, anchor="middle"))
        body.append(_text(center, bottom + 82, name, 28, weight="700", anchor="middle"))
        body.append(_text(center, bottom + 124, f"c32 / c4 = {float(ratios[field]):.2f}\u00d7", 30, weight="700", anchor="middle"))
    body += [
        _text(80, 842, "Descriptive only: not Kubernetes HPA, KServe, or llm-d; no causal attribution.", 24, fill=COLORS["muted"]),
        *_provenance(metrics, 878),
    ]
    return _svg_document(1600, 950, title, body)


def _pressure_svg(metrics: Mapping[str, object]) -> str:
    arms = metrics["arms"]
    assert isinstance(arms, Mapping)
    labels = _arm_labels(arms)
    waiting = [float(arms[arm]["max_waiting_requests"]) for arm in ("c4", "c32")]
    kv_peak = max(float(arms[arm]["max_kv_cache_percent"]) for arm in ("c4", "c32"))
    if all(value == 0 for value in waiting):
        title = f"No queue buildup: waiting 0 in both arms; KV peak {kv_peak:.3f}%"
    else:
        title = f"Queue observed: waiting max c4 {waiting[0]:g}, c32 {waiting[1]:g}; KV peak {kv_peak:.3f}%"
    body = [
        _text(80, 70, title, 44, weight="700"),
        _text(80, 112, "Maximum of vLLM Prometheus snapshots captured during each load arm.", 26, fill=COLORS["muted"]),
        f'<rect x="80" y="134" width="620" height="48" rx="24" fill="{COLORS["ink"]}"/>',
        _text(390, 167, "STANDALONE vLLM \u2014 NOT Kubernetes HPA", 24, fill="#ffffff", weight="700", anchor="middle"),
        _text(724, 167, _context(metrics), 24, fill=COLORS["muted"]),
    ]
    top, bottom = 330, 660
    request_axis = _nice_ceiling(max(max(float(arms[arm][field]) for arm in ("c4", "c32") for field in ("max_waiting_requests", "max_running_requests")) * 1.15, 5.0))
    panels = (
        ("Max waiting requests", f"Axis: 0 to {request_axis:g} requests", "max_waiting_requests", "waiting {:g}", "{:g}", request_axis),
        ("Max running requests", f"Axis: 0 to {request_axis:g} requests", "max_running_requests", "running {:g}", "{:g}", request_axis),
        ("Peak KV cache usage", "Axis: 0 to 100% of cache capacity", "max_kv_cache_percent", "{:.3f}%", "{:g}%", 100.0),
    )
    for index, (name, caption, field, display, tick, axis_max) in enumerate(panels):
        values = [float(arms[arm][field]) for arm in ("c4", "c32")]
        bars = [(label, value, display.format(value), color) for (label, color), value in zip(labels, values, strict=True)]
        body += _bar_panel(80 + index * 490, 460, top, bottom, name, caption, bars, axis_max, tick)
    body += [
        _text(80, 842, "Descriptive only: no optimization win or causal attribution.", 24, fill=COLORS["muted"]),
        *_provenance(metrics, 878),
    ]
    return _svg_document(1600, 950, title, body)


def _hpa_signal_svg(verdict: Mapping[str, object]) -> str:
    validated_local_hpa_verdict(verdict)
    evidence = verdict["local_evidence"]["arms"]
    arms = {item["arm"]: item["observations"] for item in evidence}
    labels = ["CPU", "Queue", "Synthetic KV"]
    values = [arms["cpu"]["scaled_cpu_millicores"], arms["queue"]["scaled_queue"], arms["kv"]["scaled_kv"]]
    units = ["millicores", "waiting requests", "occupancy"]
    panels = [(label, [label], [value], color, unit) for label, value, unit, color in zip(labels, values, units, (COLORS["blue"], COLORS["orange"], COLORS["green"]), strict=True)]
    return svg_chart("Local Kubernetes HPA signal plumbing", "Each isolated signal produced a 1 to 2 desired-and-ready replica transition. KV source was synthetic.", panels)


def hpa_signal_chart(verdict: dict[str, object]) -> None:
    (OUT / "local-hpa-signal-plumbing.svg").write_text(_hpa_signal_svg(verdict))


def two_node_startup_chart(*, current: datetime | None = None) -> dict[str, object]:
    """Summarize actual two-node provider attempts without inflating them to K8s results."""
    data, svg = _two_node_startup(current=current)
    (OUT / "two-node-canary-startup.svg").write_text(svg)
    return data


def _two_node_startup(*, current: datetime | None = None) -> tuple[dict[str, object], str]:
    runs = sorted((ROOT / "artifacts" / "runs").glob("two-node-*/run-manifest.json"))
    attempts: list[dict[str, object]] = []
    counts = {"created": 0, "running": 0, "completed": 0}
    for manifest_path in runs:
        manifest = json.loads(manifest_path.read_text())
        run_dir = manifest_path.parent
        statuses = []
        for status_file in run_dir.glob("*-provider-status.ndjson"):
            for line in status_file.read_text(encoding="utf-8").splitlines():
                try:
                    status = json.loads(line).get("actual_status")
                except json.JSONDecodeError:
                    status = None
                if isinstance(status, str):
                    statuses.append(status)
        created = bool(manifest.get("server", {}).get("instance") or manifest.get("worker", {}).get("instance"))
        running = "running" in statuses
        guards = manifest.get("guards") if isinstance(manifest.get("guards"), dict) else {}
        guards_armed, guard_errors = validate_bound_guard_receipts(manifest)
        finalization = manifest.get("provider_finalization") if isinstance(manifest.get("provider_finalization"), dict) else {}
        azure_finalization = manifest.get("azure_guard_finalization") if isinstance(manifest.get("azure_guard_finalization"), dict) else {}
        guard_journals_verified = guards_armed and all(
            isinstance(guards.get(role), Mapping)
            and isinstance(azure_finalization.get(role), Mapping)
            and validate_guard_journal(
                run_dir,
                role=role,
                receipt=guards[role],
                finalization=azure_finalization[role],
            )
            for role in ("server", "worker")
        )
        absence_proved = all(
            isinstance(manifest.get(role), Mapping)
            and isinstance(finalization.get(role), Mapping)
            and isinstance(finalization[role].get("absence"), Mapping)
            and validate_absence_artifact(run_dir, role, manifest[role], finalization[role]["absence"])
            for role in ("server", "worker")
        )
        billing_captured = all(
            isinstance(manifest.get(role), Mapping)
            and isinstance(finalization.get(role), Mapping)
            and isinstance(finalization[role].get("billing"), Mapping)
            and validate_billing_artifact(run_dir, role, manifest[role], finalization[role]["billing"])
            for role in ("server", "worker")
        )
        completed = (
            manifest.get("status") == "completed"
            and guards_armed
            and guard_journals_verified
            and absence_proved
            and billing_captured
        )
        report = manifest.get("report") if isinstance(manifest.get("report"), dict) else {}
        counts["created"] += int(created)
        counts["running"] += int(running)
        counts["completed"] += int(completed)
        attempts.append({
            "run": run_dir.name,
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "template": manifest.get("vm_template", "ubuntu-cli"),
            "created": created,
            "guard_backend": manifest.get("guard_backend", "github-legacy"),
            "both_bound_arm_receipts": guards_armed,
            "guard_receipt_validation_errors": guard_errors,
            "both_guard_journals_hash_chain_verified": guard_journals_verified,
            "deferred_guard_evidence_validated": {
                role: isinstance(guards.get(role), Mapping)
                and isinstance(azure_finalization.get(role), Mapping)
                and validate_deferred_guard_evidence(run_dir, role, guards[role], azure_finalization[role], current=current)
                for role in ("server", "worker")
            },
            "provider_running_observed": running,
            "kubernetes_completed": completed,
            "three_read_absence_proved_for_both": absence_proved,
            "authoritative_billing_captured_for_both": billing_captured,
            "report": {
                "attempted": report.get("attempted") is True,
                "confirmed": report.get("confirmed") is True,
                "instance_id": report.get("instance_id"),
            },
        })
    svg = svg_chart(
        f"Two-node canary: {len(attempts)} selected historical attempts (subset)",
        "Each bar counts attempts, not instances. Only artifacts/runs/two-node-*; later live attempts excluded. ‘Running’ is provider state only.",
        [(
            "Historical attempt outcomes",
            ["Attempts with ≥1 instance", "Attempts provider-running", "Attempts K8s completed"],
            [float(counts["created"]), float(counts["running"]), float(counts["completed"])],
            COLORS["blue"],
            "attempts (an attempt with two instances counts once)",
        )],
    )
    return {
        "scope": "selected historical attempts: artifacts/runs/two-node-*/run-manifest.json only; later live-two-node-* attempts excluded",
        "count_unit": "attempts",
        "count_meaning": {
            "created": "attempts with at least one created instance",
            "running": "attempts with any provider 'running' status observation",
            "completed": "attempts meeting the full Kubernetes completion contract",
        },
        "attempts": attempts,
        "counts": counts,
    }, svg


def _timing_payloads(evidence: Path, phase: str) -> list[Mapping[str, object]]:
    """Read only successful, raw curl timing records for one load phase."""

    values: list[Mapping[str, object]] = []
    for path in sorted(evidence.glob(f"pressure-{phase}-request-*-timing.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(payload, Mapping)
            and payload.get("http_code") == 200
            and isinstance(payload.get("ttft_seconds"), (int, float))
            and isinstance(payload.get("latency_seconds"), (int, float))
        ):
            values.append(payload)
    return values


def _prometheus_metric_maximum(path: Path, fragments: tuple[str, ...]) -> float | None:
    """Return a phase-local maximum only for the named raw Prometheus series."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    data = payload.get("data") if isinstance(payload, Mapping) else None
    results = data.get("result") if isinstance(data, Mapping) else None
    values: list[float] = []
    if not isinstance(results, list):
        return None
    for result in results:
        if not isinstance(result, Mapping):
            continue
        metric = result.get("metric")
        name = metric.get("__name__") if isinstance(metric, Mapping) else None
        sample = result.get("value")
        if not isinstance(name, str) or not any(fragment in name for fragment in fragments):
            continue
        if isinstance(sample, list) and len(sample) == 2:
            try:
                values.append(float(sample[1]))
            except (TypeError, ValueError):
                pass
    return max(values) if values else None


def _gpu_utilization_maximum(path: Path) -> float | None:
    """Read the vLLM pod's raw nvidia-smi utilization field (column five)."""

    try:
        rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
    except OSError:
        return None
    values: list[float] = []
    for row in rows:
        if len(row) != 5:
            continue
        try:
            values.append(float(row[4].strip().removesuffix(" %")))
        except ValueError:
            pass
    return max(values) if values else None


def two_node_metric_path_chart(run_dir: Path) -> dict[str, object]:
    """Create a chart only from a completed run's raw K8s/vLLM snapshots.

    This intentionally rejects absent queue/KV or timing series.  A rendered
    chart must be evidence of the metric path, never a visually plausible
    placeholder when a provider run merely created VMs.
    """
    data, svg = _two_node_metric_path(run_dir)
    (OUT / METRIC_PATH_ARTIFACT).write_text(svg, encoding="utf-8")
    return data


def _completed_two_node_metric_run(run_dir: Path, manifest: Mapping[str, object]) -> bool:
    if (
        manifest.get("status") != "completed"
        or manifest.get("evidence_status") != "complete"
        or manifest.get("workload_completed") is not True
    ):
        return False
    guards = manifest.get("guards")
    provider_finalization = manifest.get("provider_finalization")
    guard_finalization = manifest.get("azure_guard_finalization")
    guards_armed, _ = validate_bound_guard_receipts(manifest)
    if not (
        guards_armed
        and isinstance(guards, Mapping)
        and isinstance(provider_finalization, Mapping)
        and isinstance(guard_finalization, Mapping)
    ):
        return False
    return all(
        isinstance(manifest.get(role), Mapping)
        and isinstance(guards.get(role), Mapping)
        and isinstance(provider_finalization.get(role), Mapping)
        and isinstance(guard_finalization.get(role), Mapping)
        and isinstance(provider_finalization[role].get("absence"), Mapping)
        and isinstance(provider_finalization[role].get("billing"), Mapping)
        and validate_guard_journal(
            run_dir,
            role=role,
            receipt=guards[role],
            finalization=guard_finalization[role],
        )
        and validate_absence_artifact(run_dir, role, manifest[role], provider_finalization[role]["absence"])
        and validate_billing_artifact(run_dir, role, manifest[role], provider_finalization[role]["billing"])
        for role in ("server", "worker")
    )


def _two_node_metric_path(run_dir: Path) -> tuple[dict[str, object], str]:
    try:
        manifest = json.loads((run_dir / "run-manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"two-node run manifest is unreadable: {exc}") from exc
    if not isinstance(manifest, Mapping) or not _completed_two_node_metric_run(run_dir, manifest):
        raise SystemExit("two-node run does not satisfy the metric-path completion contract")
    evidence = run_dir / "server-evidence"
    if not evidence.is_dir():
        raise SystemExit(f"two-node evidence directory is missing: {evidence}")
    phases = ("before", "during", "after")
    labels = ["Baseline", "Pressure", "Recovery"]
    timings = {phase: _timing_payloads(evidence, phase) for phase in phases}
    if any(not timings[phase] for phase in phases):
        raise SystemExit("two-node timing evidence is incomplete; refusing to chart an unmeasured metric path")
    queues = {
        phase: _prometheus_metric_maximum(
            evidence / f"pressure-{phase}-queue-kv-ttft-prometheus.json",
            ("num_requests_waiting",),
        )
        for phase in phases
    }
    kv = {
        phase: _prometheus_metric_maximum(
            evidence / f"pressure-{phase}-queue-kv-ttft-prometheus.json",
            ("kv_cache_usage_perc", "gpu_cache_usage_perc"),
        )
        for phase in phases
    }
    gpu_utilization = {
        phase: _gpu_utilization_maximum(evidence / f"pressure-{phase}-gpu.csv")
        for phase in phases
    }
    if (
        any(queues[phase] is None for phase in phases)
        or any(kv[phase] is None for phase in phases)
        or any(gpu_utilization[phase] is None for phase in phases)
    ):
        raise SystemExit("two-node queue/KV/GPU evidence is incomplete; refusing to chart an unmeasured metric path")
    ttft = [percentile([float(record["ttft_seconds"]) * 1000 for record in timings[phase]], 0.5) for phase in phases]
    queue_values = [float(queues[phase]) for phase in phases]
    kv_values = [float(kv[phase]) * 100 for phase in phases]
    gpu_values = [float(gpu_utilization[phase]) for phase in phases]
    workload = manifest.get("workload") if isinstance(manifest, Mapping) else None
    model = workload.get("model_id") if isinstance(workload, Mapping) else "frozen model"
    svg = svg_chart(
        "Two-node Kubernetes vLLM metric path",
        f"Raw stream timing + Prometheus snapshots from {run_dir.name}; {model}",
        [
            ("p50 TTFT", labels, ttft, COLORS["orange"], "milliseconds; curl first response byte"),
            ("Max queued requests", labels, queue_values, COLORS["blue"], "vLLM waiting requests"),
            ("Max KV cache use", labels, kv_values, COLORS["green"], "percent"),
            ("GPU utilization", labels, gpu_values, COLORS["ink"], "percent; inside serving pod"),
        ],
    )
    return {
        "run": run_dir.name,
        "artifact": METRIC_PATH_ARTIFACT,
        "timing_samples": {phase: len(timings[phase]) for phase in phases},
        "p50_ttft_ms": dict(zip(phases, ttft, strict=True)),
        "max_queue_depth": dict(zip(phases, queue_values, strict=True)),
        "max_kv_cache_percent": dict(zip(phases, kv_values, strict=True)),
        "max_gpu_utilization_percent": dict(zip(phases, gpu_values, strict=True)),
    }, svg


def write_summary(standalone: Mapping[str, object], local_verdict: Mapping[str, object], two_node: dict[str, object], metric_path: dict[str, object] | None = None) -> None:
    (OUT / "evidence-summary.json").write_text(_summary_json(standalone, local_verdict, two_node, metric_path))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pack_path(name: str) -> str:
    try:
        return (OUT / name).relative_to(ROOT).as_posix()
    except ValueError:
        return f"{CANONICAL_PACK}/{name}"


def _latest_live_attempt() -> dict[str, object] | None:
    run_dirs = [path for path in ROOT.glob("artifacts/live-two-node-*") if path.is_dir()]
    if not run_dirs:
        return None
    def started_at(path: Path) -> tuple[datetime, str]:
        stamp = path.name.removeprefix("live-two-node-")
        for pattern in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%MZ"):
            try:
                return datetime.strptime(stamp, pattern).replace(tzinfo=UTC), path.name
            except ValueError:
                pass
        return datetime.min.replace(tzinfo=UTC), path.name

    run_dir = max(run_dirs, key=started_at)
    path = run_dir / "run-manifest.json"
    relative_path = path.relative_to(ROOT).as_posix()
    if not path.is_file():
        return {
            "run": run_dir.name,
            "manifest_path": relative_path,
            "manifest_sha256": None,
            "status": "manifest-missing",
            "evidence_status": "incomplete",
            "failure_type": "ManifestMissing",
            "report": {"attempted": None, "confirmed": None},
            "finalization_errors": ["run-manifest.json missing"],
        }
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"latest live two-node manifest is unreadable: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise SystemExit("latest live two-node manifest must be an object")
    failure = payload.get("failure")
    report = payload.get("report")
    finalization_errors = payload.get("finalization_errors")
    return {
        "run": path.parent.name,
        "manifest_path": relative_path,
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "status": payload.get("status"),
        "evidence_status": payload.get("evidence_status"),
        "failure_type": failure.get("error_type") if isinstance(failure, Mapping) else None,
        "report": {
            "attempted": report.get("attempted") if isinstance(report, Mapping) else None,
            "confirmed": report.get("confirmed") if isinstance(report, Mapping) else None,
        },
        "finalization_errors": (
            [value for value in finalization_errors if isinstance(value, str)]
            if isinstance(finalization_errors, list)
            else []
        ),
    }


def _measured_arm(arm_dir: Path) -> dict[str, object] | None:
    sums = arm_dir / "SHA256SUMS"
    for line in sums.read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        if hashlib.sha256((arm_dir / name).read_bytes()).hexdigest() != digest:
            raise SystemExit(f"measured run checksum mismatch: {arm_dir / name}")
    run = json.loads((arm_dir / "run.json").read_text(encoding="utf-8"))
    if run.get("status") != "completed":
        return None
    samples = [json.loads(line) for line in (arm_dir / "metrics.ndjson").read_text(encoding="utf-8").splitlines() if line.strip()]
    log_path = arm_dir / "vllm.log"
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    weights, kv_tokens, kv_memory = (pattern.search(log) for pattern in (_WEIGHT_LOG, _KV_TOKENS_LOG, _KV_MEMORY_LOG))
    keys = ("concurrency", "input_tokens", "output_tokens", "output_tok_per_s", "ttft_p50_s", "ttft_p99_s", "tpot_p50_s", "tpot_p99_s", "e2e_p50_s")
    return {
        "run_json_sha256": hashlib.sha256((arm_dir / "run.json").read_bytes()).hexdigest(),
        "precision": run.get("precision", "bf16"),
        "model_id": run["model_id"],
        "tensor_parallel": run["tensor_parallel"],
        "readiness_s": run["readiness_s"],
        "cells": [{"cell": cell["cell"], **{key: cell[key] for key in keys}} for cell in run["cells"]],
        "max_waiting": max((s["waiting"] for s in samples if s.get("waiting") is not None), default=None),
        "max_kv_cache_usage": max((s["kv_cache_usage"] for s in samples if s.get("kv_cache_usage") is not None), default=None),
        "weights_gib_per_gpu": float(weights.group(1)) if weights else None,
        "kv_cache_tokens": int(kv_tokens.group(1).replace(",", "")) if kv_tokens else None,
        "kv_cache_memory_gib_per_gpu": float(kv_memory.group(1)) if kv_memory else None,
    }


def _measured_results() -> dict[str, object]:
    results: dict[str, object] = {}
    for source_id, relatives in MEASURED_RUNS.items():
        arms: dict[str, object] = {}
        skipped: list[str] = []
        for relative in relatives:
            run_dir = ROOT / relative
            if not run_dir.is_dir():
                continue
            arm_dirs = [run_dir] if (run_dir / "run.json").exists() else sorted(p for p in run_dir.iterdir() if (p / "run.json").exists())
            for arm_dir in arm_dirs:
                arm = _measured_arm(arm_dir)
                if arm is None:
                    skipped.append(str(arm_dir.relative_to(ROOT)))
                elif arm["precision"] in arms:
                    raise SystemExit(f"duplicate measured precision arm {arm['precision']} for {source_id}")
                else:
                    arms[str(arm["precision"])] = arm
        results[source_id] = {"status": "available" if arms else "unavailable", "paths": list(relatives), "arms": arms, "skipped_incomplete_arms": skipped}
    return results


def _catalog_json(rendered: Mapping[str, str], metric_path: Mapping[str, object] | None) -> str:
    """Describe the already-rendered pack; the summary is excluded to avoid a circular hash."""

    charts = [
        {
            "name": name,
            "path": _pack_path(name),
            "status": "available" if name in rendered else "unavailable",
            "sha256": _sha256(rendered[name]) if name in rendered else None,
            "presentation_role": CHART_PRESENTATION_ROLES[name],
        }
        for name in CATALOG_CHARTS
    ]
    run_root = f"artifacts/runs/{STANDALONE_RUN_ID}"
    slides = [
        {"number": number, "headline": headline, "pill": pill, "charts": list(names), "source_ids": list(source_ids)}
        for number, headline, pill, names, source_ids in SLIDES
    ]
    catalog = {
        "schema": "srecon26-presentation-input/v1",
        "sources": {
            "evidence_summary": {
                "path": f"{CANONICAL_PACK}/{SUMMARY_ARTIFACT}",
                "sha256": None,
                "note": "summary embeds this catalog's SHA256; hash the summary file directly",
            },
            "standalone_gpu_verdict": f"{run_root}/post-run/verdict.json",
            "standalone_gpu_verdict_sums": f"{run_root}/post-run/SHA256SUMS",
            "manual_benchmark": f"{run_root}/manual-benchmark",
            "manual_benchmark_sums": f"{run_root}/manual-benchmark/SHA256SUMS",
            "claude_design_prompt": f"{CANONICAL_PACK}/CLAUDE-DESIGN-PROMPT.md",
            "claude_design_brief": "docs/claude-design-brief.md",
            "google_slides_import_qa": "docs/google-slides-import-qa.md",
            "paired_comparison_runbook": "docs/runbooks/paired-comparison.md",
        },
        "metrics": {
            "local_hpa_verdict": {
                "path": f"{CANONICAL_PACK}/{LOCAL_HPA_VERDICT_ARTIFACT}",
                "sha256": _sha256(rendered[LOCAL_HPA_VERDICT_ARTIFACT]),
                "claim": "local_hpa_signal_plumbing",
            },
        },
        "charts": charts,
        "slides": slides,
        "published_sources": PUBLISHED_SOURCES,
        "measured_results": _measured_results(),
        "results": {
            "latest_live_attempt": _latest_live_attempt(),
            "two_node_metric_path": {
                "status": "available" if metric_path is not None else "unavailable",
                "run": metric_path.get("run") if metric_path is not None else None,
                "chart": METRIC_PATH_ARTIFACT if metric_path is not None else None,
            },
            "paired_hpa_comparison": {
                "status": "unavailable",
                "comparative_claim_allowed": False,
                "reason": "No CPU-only versus queue/KV-aware HPA A/B result exists yet.",
                "arms": ["cpu-only-hpa", "queue-kv-aware-hpa"],
                "required_block_order": ["AB", "BA", "AB"],
                "primary_outcome": {"metric": "pressure_window_p95_ttft_ms", "measured": False},
                "planned_outputs": [
                    "artifacts/comparisons/<comparison-id>/analysis/summary.json",
                    "artifacts/comparisons/<comparison-id>/analysis/paired-blocks.csv",
                    "artifacts/comparisons/<comparison-id>/charts/paired-p95-ttft.svg",
                    "artifacts/comparisons/<comparison-id>/charts/queue-readiness-overlay.svg",
                ],
            },
        },
    }
    return json.dumps(catalog, indent=2, sort_keys=True, allow_nan=False) + "\n"


def _summary_json(
    standalone: Mapping[str, object], local_verdict: Mapping[str, object], two_node: dict[str, object],
    metric_path: dict[str, object] | None = None, chart_metrics: Mapping[str, object] | None = None,
    artifact_catalog: Mapping[str, str] | None = None, capacity_projection: Mapping[str, object] | None = None,
) -> str:
    latency = standalone["latency_ms"]
    assert isinstance(latency, Mapping)
    def stats(arm: str) -> dict[str, float | int]:
        values = latency[arm]
        assert isinstance(values, Mapping)
        return {
            "requests": values["requests"],
            "p50_ttft_ms": values["p50_ttft_ms"],
            "p95_ttft_ms": values["p95_ttft_ms"],
            "p50_e2e_ms": values["p50_e2e_ms"],
            "p95_e2e_ms": values["p95_e2e_ms"],
            "p50_tpot_ms": values["p50_tpot_ms"],
            "p95_tpot_ms": values["p95_tpot_ms"],
        }
    summary = {
        "schema": "srecon26-evidence-pack/v1",
        "gpu_measurements": {arm: stats(arm) for arm in ("c4", "c32")},
        "standalone_gpu_verification": {
            "schema": standalone["schema"],
            "run_id": standalone["run_id"],
            "raw_file_count": standalone["raw_file_count"],
            "raw_request_samples": {arm: stats(arm)["requests"] for arm in ("c4", "c32")},
            "prometheus_observed": standalone["prometheus_observed"],
            "provenance_limitations": standalone["provenance_limitations"],
            "standalone_gpu_latency": standalone["standalone_gpu_latency"],
            "kubernetes_hpa": standalone["kubernetes_hpa"],
            "paired_hpa_performance": standalone["paired_hpa_performance"],
        },
        "gpu_chart_metrics": chart_metrics,
        "capacity_projection": capacity_projection,
        "local_hpa_verdict": local_verdict["claims"]["local_hpa_signal_plumbing"],
        "two_node_canary": two_node,
        "two_node_metric_path": metric_path,
        "artifact_catalog": artifact_catalog,
        "boundaries": [
            "Standalone GPU benchmark evidence is locally checksum-verified post-run; the benchmark capture is not independently anchored.",
            "GPU measurements are standalone vLLM serving data from one rented VM, not a Kubernetes HPA experiment.",
            "Observed vLLM queue and KV series are descriptive evidence only; no optimization win is claimed.",
            "Local HPA proof validates independent signal plumbing; the KV source is synthetic.",
            "No CPU-only versus queue/KV-aware HPA A/B result exists yet.",
            "Provider ‘running’ does not establish SSH, GPU, Kubernetes, vLLM, metrics, or HPA readiness.",
            "A two-node result is complete only when both bound guard receipts, raw workload outputs, exact billing, and three-read absence artifacts are retained.",
            "The website Report action is attempted only for a frozen exact-instance provider fault before normal teardown.",
        ],
    }
    return json.dumps(summary, indent=2) + "\n"


def _publish(rendered: Mapping[str, str], *, keep_metric_path: bool) -> None:
    """Stage every file, then replace into OUT with the summary last."""

    OUT.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".evidence-pack-staging-", dir=OUT.parent))
    try:
        for name, text in rendered.items():
            (staging / name).write_text(text, encoding="utf-8")
        OUT.mkdir(parents=True, exist_ok=True)
        summary = "evidence-summary.json"
        for name in [name for name in rendered if name != summary] + [summary]:
            os.replace(staging / name, OUT / name)
        if not keep_metric_path:
            (OUT / METRIC_PATH_ARTIFACT).unlink(missing_ok=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--two-node-run", type=Path, help="completed two-node run directory whose raw K8s/vLLM evidence must be charted")
    args = parser.parse_args(argv)
    standalone = verified_standalone_gpu_verdict(ROOT)
    chart_metrics = gpu_chart_metrics(standalone)
    if not VERDICT.is_file():
        raise SystemExit("run scripts/analyze_evidence.py --output /var/tmp/srecon26-verdict.json first")
    try:
        verdict = json.loads(VERDICT.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"local HPA verdict is unreadable: {exc}") from exc
    validated_local_hpa_verdict(verdict)
    latency = standalone["latency_ms"]
    assert isinstance(latency, Mapping)
    rendered = {
        "gpu-latency-by-concurrency.svg": _gpu_latency_svg(latency),
        "local-hpa-signal-plumbing.svg": _hpa_signal_svg(verdict),
        CONTRAST_ARTIFACT: _contrast_svg(chart_metrics),
        PRESSURE_ARTIFACT: _pressure_svg(chart_metrics),
    }
    chart_metrics["rendered_sha256"] = {
        name: hashlib.sha256(rendered[name].encode("utf-8")).hexdigest() for name in (CONTRAST_ARTIFACT, PRESSURE_ARTIFACT)
    }
    capacity, rendered[CAPACITY_ARTIFACT] = _capacity_projection(standalone)
    capacity["rendered_sha256"] = _sha256(rendered[CAPACITY_ARTIFACT])
    two_node, rendered["two-node-canary-startup.svg"] = _two_node_startup()
    metric_path = None
    if args.two_node_run is not None:
        metric_path, rendered[METRIC_PATH_ARTIFACT] = _two_node_metric_path(args.two_node_run.resolve())
    rendered[LOCAL_HPA_VERDICT_ARTIFACT] = json.dumps(verdict, indent=2, sort_keys=True, allow_nan=False) + "\n"
    rendered[CATALOG_ARTIFACT] = _catalog_json(rendered, metric_path)
    catalog_ref = {"path": CATALOG_ARTIFACT, "sha256": _sha256(rendered[CATALOG_ARTIFACT])}
    rendered[SUMMARY_ARTIFACT] = _summary_json(standalone, verdict, two_node, metric_path, chart_metrics, catalog_ref, capacity)
    _publish(rendered, keep_metric_path=metric_path is not None)


if __name__ == "__main__":
    main()
