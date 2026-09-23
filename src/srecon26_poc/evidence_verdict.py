"""Small, fail-closed evidence verdicts for the published PoC artifacts.

The local HPA evidence and the paid canary answer different questions.  A
correctly sealed ``FAILED_SAFE`` canary is useful lifecycle evidence, but is
never evidence for a GPU, latency, or A/B performance claim.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


VALID = "VALID"
INVALID = "INVALID"
EXPLORATORY = "EXPLORATORY"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LOCAL_ARMS = frozenset({"cpu", "queue", "kv"})
_METRIC = re.compile(r"^(?P<name>[^#\s]+) (?P<value>[0-9.]+)$", re.MULTILINE)
_PINNED_LOCAL_ANCHOR_SHA256 = "544d28e48f1e75b05a5b71603c157fdc51bdcf1b5b1f9c86808dd18eaad484f9"
_PINNED_LOCAL_RECEIPT_SHA256 = "1ea0d0a5d1064d92b4fc2e7bf3ac0382015c14b6d114d0c0775d6854b0f5a134"
_PINNED_LOCAL_GUARD_ROOT = "76624912f5f1a397ebfad23fc782d29e43ab6c98d4f32433c6840dace8d42aa7"
_PINNED_LOCAL_ROOTS = {
    "cpu": "1ae7f36ade32cf257e89edadbe3330be1f0584701f93c7607dad777b941834d2",
    "queue": "f2d9faaba8f9b440843ed7f14f7cbbd4d536c5ba9a2ad7111d7c3deb125fceeb",
    "kv": "0b61b3eb05eff58c8912ba0d3032dc1d60e495fe4c4af0b3d67e4bcbadf7be10",
}
_PINNED_LIVE_MANIFEST_SHA256 = "3ba23dbcc316461aad9e894154976102451b597dded00bd3db133cd632105ecd"
_PINNED_LIVE_GUARD_SHA256 = "bdb5e7e9a9d0c606ca99bb7b8d25c40ad03e7d22739fb31a8b1a41ec75d4c8c0"
_PINNED_LIVE_FINAL_ROOT = "d8b36066acc2d616e315730d411923b41769b0845192f4138b1cd20b299f4803"
_PINNED_LIVE_PRE_ANCHOR_ROOT = "e41a6683538ef846f7bb579acece9751ae6339c8e2a3ec65057ccaa3b5ec5f18"


@dataclass
class CheckResult:
    """A serializable result whose errors never become a positive claim."""

    verdict: str = VALID
    failing_checks: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def fail(self, check: str) -> None:
        self.verdict = INVALID
        self.failing_checks.append(check)

    def exploratory(self, check: str) -> None:
        if self.verdict != INVALID:
            self.verdict = EXPLORATORY
        self.failing_checks.append(check)

    def to_json(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "failing_checks": self.failing_checks,
            **self.details,
        }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path, result: CheckResult, check: str) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        result.fail(check)
        return None
    if not isinstance(data, dict):
        result.fail(check)
        return None
    return data


def _safe_child(root: Path, name: str) -> Path | None:
    candidate = (root / name).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _prometheus_metric(path: Path, name: str) -> float:
    for match in _METRIC.finditer(path.read_text(encoding="utf-8")):
        if match.group("name") == name:
            return float(match.group("value"))
    raise ValueError(f"metric {name!r} missing")


def _cpu_millicores(path: Path) -> float:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = next(item["usage"]["cpu"] for item in payload["containers"] if item["name"] == "server")
    if raw.endswith("n"):
        return float(raw[:-1]) / 1_000_000
    if raw.endswith("m"):
        return float(raw[:-1])
    return float(raw) * 1_000


def _desired_replicas(path: Path) -> int:
    return int(json.loads(path.read_text(encoding="utf-8"))["status"]["desiredReplicas"])


def _ready_replicas(path: Path) -> int:
    return int(json.loads(path.read_text(encoding="utf-8"))["status"]["readyReplicas"])


def _timestamp(path: Path) -> datetime:
    return datetime.fromisoformat(path.read_text(encoding="utf-8").strip().replace("Z", "+00:00")).astimezone(UTC)


def _verify_signal_evidence(bundle: Path, arm: str, result: CheckResult) -> None:
    """Recompute independence controls from the checksummed raw captures."""
    try:
        negative_sources = [bundle / f"negative-{index}.source.prom" for index in range(7)]
        negative_cpus = [bundle / f"negative-{index}.cpu.json" for index in range(7)]
        negative_hpas = [bundle / f"negative-{index}.hpa.json" for index in range(7)]
        negative_times = [_timestamp(bundle / f"negative-{index}.timestamp") for index in range(7)]
        if negative_times != sorted(negative_times) or (negative_times[-1] - negative_times[0]).total_seconds() < 90:
            result.fail(f"local.{arm}.negative_control_timestamps_invalid")
        if any(_desired_replicas(path) != 1 for path in negative_hpas):
            result.fail(f"local.{arm}.negative_control_scaled")

        waiting = [_prometheus_metric(path, "vllm:num_requests_waiting") for path in negative_sources]
        kv = [_prometheus_metric(path, "vllm:kv_cache_usage_perc") for path in negative_sources]
        cpu = [_cpu_millicores(path) for path in negative_cpus]
        scaled_source = bundle / "scaled.source.prom"
        scaled_cpu = _cpu_millicores(bundle / "scaled.cpu.json")
        scaled_waiting = _prometheus_metric(scaled_source, "vllm:num_requests_waiting")
        scaled_kv = _prometheus_metric(scaled_source, "vllm:kv_cache_usage_perc")
        if _desired_replicas(bundle / "scaled.hpa.json") != 2 or _ready_replicas(bundle / "scaled.deployment.json") != 2:
            result.fail(f"local.{arm}.scale_transition_missing")

        controls = {
            "cpu": max(waiting) <= 0.8 and max(kv) <= 0.64 and scaled_cpu >= 72 and scaled_waiting <= 0.8 and scaled_kv <= 0.64,
            "queue": max(cpu) <= 48 and max(kv) <= 0.64 and scaled_waiting >= 1.2 and scaled_cpu <= 48 and scaled_kv <= 0.64,
            "kv": max(cpu) <= 48 and max(waiting) <= 0.8 and scaled_kv >= 0.96 and scaled_cpu <= 48 and scaled_waiting <= 0.8,
        }
        if not controls[arm]:
            result.fail(f"local.{arm}.independence_margin_failed")
        result.details["observations"] = {
            "negative_sample_count": 7,
            "negative_duration_seconds": int((negative_times[-1] - negative_times[0]).total_seconds()),
            "negative_cpu_max_millicores": max(cpu),
            "negative_queue_max": max(waiting),
            "negative_kv_max": max(kv),
            "scaled_cpu_millicores": scaled_cpu,
            "scaled_queue": scaled_waiting,
            "scaled_kv": scaled_kv,
            "desired_replicas": 2,
            "ready_replicas": 2,
        }
    except (KeyError, OSError, StopIteration, TypeError, ValueError, json.JSONDecodeError):
        result.fail(f"local.{arm}.raw_signal_evidence_invalid")


def _verify_checksum_bundle(
    bundle: Path,
    result: CheckResult,
    *,
    prefix: str,
    sums_path: Path | None = None,
    root_path: Path | None = None,
) -> None:
    """Verify every listed checksum plus the root derived from exact sums bytes."""
    sums_path = sums_path or bundle / "SHA256SUMS"
    root_path = root_path or bundle / "ROOT-HASH.txt"
    try:
        sums = sums_path.read_bytes()
        root_hash = root_path.read_text(encoding="utf-8").strip()
    except OSError:
        result.fail(f"{prefix}.integrity_bundle_missing")
        return
    if not _SHA256.fullmatch(root_hash) or hashlib.sha256(sums).hexdigest() != root_hash:
        result.fail(f"{prefix}.root_hash_mismatch")
    seen: set[str] = set()
    try:
        lines = sums.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        result.fail(f"{prefix}.checksums_not_utf8")
        return
    if not lines:
        result.fail(f"{prefix}.checksums_empty")
    for line in lines:
        try:
            digest, name = line.split("  ", 1)
        except ValueError:
            result.fail(f"{prefix}.checksum_format")
            continue
        path = _safe_child(bundle, name)
        if not _SHA256.fullmatch(digest) or not name or name in seen or path is None or not path.is_file():
            result.fail(f"{prefix}.checksum_entry_invalid")
            continue
        seen.add(name)
        if _sha256(path) != digest:
            result.fail(f"{prefix}.checksum_mismatch:{name}")


def _verify_local_arm(repo_root: Path, arm_record: dict[str, Any], expected_roots: dict[str, Any]) -> CheckResult:
    result = CheckResult()
    arm = arm_record.get("arm")
    bundle_name = arm_record.get("bundle")
    if arm not in _LOCAL_ARMS or not isinstance(bundle_name, str):
        result.fail("local.arm_record_invalid")
        return result
    bundle = _safe_child(repo_root, bundle_name)
    if bundle is None or not bundle.is_dir():
        result.fail(f"local.{arm}.bundle_missing")
        return result
    result.details.update({"arm": arm, "bundle": bundle_name, "root_hash": arm_record.get("root_hash")})
    _verify_checksum_bundle(bundle, result, prefix=f"local.{arm}")
    root_path = bundle / "ROOT-HASH.txt"
    try:
        actual_root = root_path.read_text(encoding="utf-8").strip()
    except OSError:
        actual_root = None
    anchored_root = expected_roots.get(arm)
    if not isinstance(anchored_root, str) or actual_root != anchored_root or arm_record.get("root_hash") != anchored_root:
        result.fail(f"local.{arm}.anchor_root_mismatch")

    manifest_path = bundle / "EVIDENCE-MANIFEST.json"
    manifest = _read_json(manifest_path, result, f"local.{arm}.manifest_invalid")
    if manifest is not None:
        if manifest.get("arm") != arm or manifest.get("run_id") != arm_record.get("run_id"):
            result.fail(f"local.{arm}.manifest_identity_mismatch")
        if manifest.get("provenance") != "local-synthetic":
            result.fail(f"local.{arm}.manifest_provenance_invalid")
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts or any(not isinstance(item, str) for item in artifacts):
            result.fail(f"local.{arm}.manifest_artifacts_invalid")
        else:
            sums_names = set()
            try:
                for line in (bundle / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
                    _digest, name = line.split("  ", 1)
                    sums_names.add(name)
            except (OSError, ValueError):
                sums_names = set()
            if set(artifacts) != sums_names:
                result.fail(f"local.{arm}.manifest_checksum_coverage_mismatch")
        expected_manifest_hash = arm_record.get("manifest_sha256")
        if not isinstance(expected_manifest_hash, str) or _sha256(manifest_path) != expected_manifest_hash:
            result.fail(f"local.{arm}.manifest_hash_mismatch")

    summary = _read_json(bundle / "summary.json", result, f"local.{arm}.summary_invalid")
    if summary is not None:
        if summary.get("arm") != arm or summary.get("provenance") != "local-synthetic":
            result.fail(f"local.{arm}.summary_provenance_invalid")
        if summary.get("negative_control_seconds", 0) < 90:
            result.fail(f"local.{arm}.negative_control_too_short")
        if summary.get("desired_replicas") != 2 or summary.get("ready_replicas") != 2:
            result.fail(f"local.{arm}.scale_transition_missing")
    _verify_signal_evidence(bundle, arm, result)
    return result


def analyze_selected_evidence(
    repo_root: Path,
    *,
    local_anchor: Path | None = None,
    live_manifest: Path | None = None,
) -> dict[str, Any]:
    """Analyze the explicitly selected local arms and the final limitation run.

    ``VALID`` describes evidence integrity and its narrow lifecycle/plumbing
    claim.  GPU and performance claims remain ``EXPLORATORY`` until their
    direct observations exist; the deck must only render claims marked both
    ``allowed`` and ``VALID``.
    """
    root = repo_root.resolve()
    anchor_path = local_anchor or root / ".planning/phases/01-safety-foundation-and-local-evidence/01-LOCAL-EVIDENCE-ANCHOR.json"
    manifest_path = live_manifest or root / "artifacts/live-runs/gpu-smoke-distinct-20260923101734/run-manifest.json"
    local = CheckResult(details={"anchor_path": _display_path(root, anchor_path), "arms": []})
    if not anchor_path.is_file() or _sha256(anchor_path) != _PINNED_LOCAL_ANCHOR_SHA256:
        local.fail("local.anchor_not_pinned")
    anchor = _read_json(anchor_path, local, "local.anchor_invalid")
    arm_results: list[CheckResult] = []
    if anchor is not None:
        receipt = anchor.get("external_guard_receipt")
        selected = anchor.get("selected_arms")
        if anchor.get("provenance") != "local-synthetic" or not isinstance(receipt, dict) or receipt.get("status") != "ACKNOWLEDGED":
            local.fail("local.anchor_not_acknowledged")
        roots = receipt.get("required_root_hashes") if isinstance(receipt, dict) else None
        if roots != _PINNED_LOCAL_ROOTS:
            local.fail("local.anchor_roots_invalid")
            roots = {}
        receipt_name = receipt.get("receipt_path") if isinstance(receipt, dict) else None
        receipt_path = _safe_child(root, receipt_name) if isinstance(receipt_name, str) else None
        if receipt_path is None or not receipt_path.is_file() or _sha256(receipt_path) != _PINNED_LOCAL_RECEIPT_SHA256:
            local.fail("local.external_receipt_not_pinned")
        else:
            receipt_payload = _read_json(receipt_path, local, "local.external_receipt_invalid")
            if (
                receipt_payload is None
                or receipt_payload.get("run_id") != "35820524825"
                or receipt_payload.get("phase1_roots") != _PINNED_LOCAL_ROOTS
                or not isinstance(receipt_payload.get("guard_receipt"), dict)
                or receipt_payload["guard_receipt"].get("root_hash") != _PINNED_LOCAL_GUARD_ROOT
            ):
                local.fail("local.external_receipt_binding_mismatch")
        if not isinstance(selected, list) or len(selected) != 3:
            local.fail("local.selected_arms_invalid")
            selected = []
        arm_results = [_verify_local_arm(root, record, roots) for record in selected if isinstance(record, dict)]
        arms = {record.details.get("arm") for record in arm_results}
        if arms != _LOCAL_ARMS:
            local.fail("local.selected_arms_incomplete")
        for record in arm_results:
            local.details["arms"].append(record.to_json())
            if record.verdict != VALID:
                for failure in record.failing_checks:
                    local.fail(failure)

    live = CheckResult(details={"manifest_path": _display_path(root, manifest_path)})
    manifest = _read_json(manifest_path, live, "live.manifest_invalid")
    live_bundle = manifest_path.parent
    if not manifest_path.is_file() or _sha256(manifest_path) != _PINNED_LIVE_MANIFEST_SHA256:
        live.fail("live.manifest_not_pinned")
    _verify_checksum_bundle(live_bundle, live, prefix="live")
    final_root_path = live_bundle / "ROOT-HASH.txt"
    try:
        if final_root_path.read_text(encoding="utf-8").strip() != _PINNED_LIVE_FINAL_ROOT:
            live.fail("live.final_root_not_pinned")
    except OSError:
        live.fail("live.final_root_not_pinned")
    guard_path = live_bundle / "guard-anchor.json"
    if not guard_path.is_file() or _sha256(guard_path) != _PINNED_LIVE_GUARD_SHA256:
        live.fail("live.guard_anchor_not_pinned")
    guard = _read_json(guard_path, live, "live.guard_anchor_invalid")
    if guard is not None:
        pre_anchor_root = guard.get("root_hash")
        if pre_anchor_root != guard.get("acknowledged") or pre_anchor_root != _PINNED_LIVE_PRE_ANCHOR_ROOT:
            live.fail("live.guard_anchor_mismatch")
        else:
            pre_result = CheckResult()
            pre_anchor = live_bundle / "pre-anchor"
            _verify_checksum_bundle(
                live_bundle,
                pre_result,
                prefix="live.pre_anchor",
                sums_path=pre_anchor / "SHA256SUMS",
                root_path=pre_anchor / "ROOT-HASH.txt",
            )
            try:
                actual_pre_root = (pre_anchor / "ROOT-HASH.txt").read_text(encoding="utf-8").strip()
            except OSError:
                actual_pre_root = None
            if actual_pre_root != pre_anchor_root:
                live.fail("live.pre_anchor_root_mismatch")
            for failure in pre_result.failing_checks:
                live.fail(failure)
    if manifest is not None:
        live.details.update({key: manifest.get(key) for key in ("run_id", "status", "provenance", "real_gpu_claim")})
        if manifest.get("status") != "FAILED_SAFE":
            live.fail("live.status_not_failed_safe")
        if manifest.get("provenance") != "live-limitation" or manifest.get("real_gpu_claim") is not False:
            live.fail("live.provenance_or_claim_invalid")
        blockers = manifest.get("evidence_blockers")
        if not isinstance(blockers, list) or not blockers:
            live.fail("live.evidence_blockers_missing")

    local_valid = local.verdict == VALID
    live_valid = live.verdict == VALID
    gpu_blockers = [
        "missing directly observed GPU identity",
        "missing directly observed CUDA version",
        "missing KVM capability probe",
        "no real vLLM request, metrics, or latency evidence",
    ]
    claims = {
        "local_hpa_signal_plumbing": {
            "verdict": VALID if local_valid else INVALID,
            "allowed": local_valid,
            "failing_checks": [] if local_valid else list(local.failing_checks),
            "provenance": "local-synthetic",
        },
        "failed_safe_lifecycle": {
            "verdict": VALID if live_valid else INVALID,
            "allowed": live_valid,
            "failing_checks": [] if live_valid else list(live.failing_checks),
            "provenance": "live-limitation",
        },
        "real_gpu_metric_path": {
            "verdict": EXPLORATORY if live_valid else INVALID,
            "allowed": False,
            "failing_checks": gpu_blockers if live_valid else list(live.failing_checks),
            "provenance": "live-limitation",
        },
        "paired_hpa_performance": {
            "verdict": EXPLORATORY if live_valid else INVALID,
            "allowed": False,
            "failing_checks": ["paired A/B comparison not run"] if live_valid else list(live.failing_checks),
            "provenance": "live-limitation",
        },
    }
    verdict = INVALID if not local_valid or not live_valid else VALID
    return {
        "schema_version": 2,
        "verdict": verdict,
        "failing_checks": [*local.failing_checks, *live.failing_checks],
        "local_evidence": local.to_json(),
        "live_evidence": live.to_json(),
        "claims": claims,
        "input_bindings": {
            "local_anchor_sha256": _PINNED_LOCAL_ANCHOR_SHA256,
            "local_receipt_sha256": _PINNED_LOCAL_RECEIPT_SHA256,
            "local_roots": _PINNED_LOCAL_ROOTS,
            "live_manifest_sha256": _PINNED_LIVE_MANIFEST_SHA256,
            "live_guard_anchor_sha256": _PINNED_LIVE_GUARD_SHA256,
            "live_final_root": _PINNED_LIVE_FINAL_ROOT,
            "live_pre_anchor_root": _PINNED_LIVE_PRE_ANCHOR_ROOT,
        },
    }


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return str(path)


def write_verdict(path: Path, verdict: dict[str, Any]) -> None:
    """Write deterministic machine-readable output for chart/deck consumers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(verdict, indent=2, sort_keys=True) + "\n", encoding="utf-8")
