from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace
from typing import Callable

import pytest

import scripts.build_evidence_pack as evidence_pack
from scripts.build_evidence_pack import (
    validate_absence_artifact,
    validate_billing_artifact,
    validate_bound_guard_receipts,
    validate_guard_journal,
)
from scripts.build_post_run_gpu_verdict import build_verdict


DEADLINE = "2026-09-24T12:00:00Z"


@pytest.fixture
def standalone_root() -> Path:
    root = Path(__file__).resolve().parents[2]
    if not (root / "artifacts/runs/inference-infer20260923184725/manual-benchmark/SHA256SUMS").is_file():
        pytest.skip("local standalone GPU evidence is not present")
    return root


def standalone_verdict_fixture() -> dict[str, object]:
    return {
        "schema": "srecon26-standalone-gpu-post-run/v1",
        "run_id": "inference-infer20260923184725",
        "model": "Qwen/Qwen2.5-1.5B-Instruct",
        "raw_file_count": 125,
        "latency_ms": {
            "c4": {
                "requests": 4,
                "p50_ttft_ms": 11, "p95_ttft_ms": 21,
                "p50_e2e_ms": 111, "p95_e2e_ms": 121,
                "p50_tpot_ms": 1, "p95_tpot_ms": 2,
            },
            "c32": {
                "requests": 32,
                "p50_ttft_ms": 33, "p95_ttft_ms": 43,
                "p50_e2e_ms": 133, "p95_e2e_ms": 143,
                "p50_tpot_ms": 3, "p95_tpot_ms": 4,
            },
        },
        "prometheus_observed": {
            arm: {
                "vllm:num_requests_waiting": {"samples": 3, "max": 0},
                "vllm:num_requests_running": {"samples": 3, "max": running},
                "vllm:kv_cache_usage_perc": {"samples": 3, "max": 0.005},
            }
            for arm, running in (("c4", 4), ("c32", 32))
        },
        "provenance_limitations": {
            "pre_anchor": "Pre-anchor sums establish retained local consistency.",
            "benchmark": "Post-run checksums do not independently anchor benchmark capture.",
            "model_revision": "Model revision comes from config/CLI.",
            "absence": "Absence evidence is post-hoc.",
        },
        "standalone_gpu_latency": {
            "allowed": True,
            "basis": "Locally checksum-verified post-run evidence; benchmark capture not independently anchored.",
        },
        "kubernetes_hpa": {"allowed": False},
        "paired_hpa_performance": {"allowed": False},
    }


def canonical_stdout(verdict: dict[str, object]) -> str:
    return json.dumps(verdict, indent=2, sort_keys=True, allow_nan=False) + "\n"


def verified_run(verdict: dict[str, object]) -> Callable[..., SimpleNamespace]:
    return lambda *a, **k: SimpleNamespace(returncode=0, stdout=canonical_stdout(verdict), stderr="")


def test_standalone_verdict_runs_fresh_verifier_for_requested_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    verdict = standalone_verdict_fixture()
    output = tmp_path / "artifacts/runs/inference-infer20260923184725/post-run/verdict.json"
    output.parent.mkdir(parents=True)
    output.write_text(json.dumps(verdict), encoding="utf-8")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=canonical_stdout(verdict), stderr="")

    monkeypatch.setattr(evidence_pack.subprocess, "run", run)

    result = evidence_pack.verified_standalone_gpu_verdict(tmp_path)

    command, options = calls[0]
    assert "--verify" in command
    assert "--print-verified" in command
    assert command[-1] == str(tmp_path)
    assert options["cwd"] == tmp_path
    assert result["latency_ms"] == verdict["latency_ms"]


def write_standalone_verdict(root: Path, verdict: dict[str, object]) -> Path:
    output = root / "artifacts/runs/inference-infer20260923184725/post-run/verdict.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(verdict), encoding="utf-8")
    return output


def test_standalone_verdict_rejects_forged_file_written_during_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = write_standalone_verdict(tmp_path, standalone_verdict_fixture())
    forged = standalone_verdict_fixture()
    forged["latency_ms"]["c32"]["p95_ttft_ms"] = 1  # type: ignore[index]
    forged["provenance_limitations"] = {}

    def run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        output.write_text(json.dumps(forged), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=canonical_stdout(standalone_verdict_fixture()), stderr="")

    monkeypatch.setattr(evidence_pack.subprocess, "run", run)

    with pytest.raises(SystemExit, match="changed during verification"):
        evidence_pack.verified_standalone_gpu_verdict(tmp_path)


def test_standalone_verdict_rejects_forge_then_restore_aba(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    forged = standalone_verdict_fixture()
    forged["latency_ms"]["c32"]["p95_ttft_ms"] = 1.0  # type: ignore[index]
    output = write_standalone_verdict(tmp_path, forged)
    forged_bytes = output.read_bytes()

    def run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        output.write_text(json.dumps(standalone_verdict_fixture()), encoding="utf-8")
        verified = canonical_stdout(standalone_verdict_fixture())
        output.write_bytes(forged_bytes)
        return SimpleNamespace(returncode=0, stdout=verified, stderr="")

    monkeypatch.setattr(evidence_pack.subprocess, "run", run)

    with pytest.raises(SystemExit, match="changed during verification"):
        evidence_pack.verified_standalone_gpu_verdict(tmp_path)


@pytest.mark.parametrize(
    "stdout",
    ["", "not json", json.dumps(standalone_verdict_fixture()), canonical_stdout(standalone_verdict_fixture()) + "extra\n", "[]\n"],
    ids=["empty", "garbage", "non-canonical", "trailing", "wrong-shape"],
)
def test_standalone_verdict_rejects_noncanonical_verifier_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str,
) -> None:
    write_standalone_verdict(tmp_path, standalone_verdict_fixture())
    monkeypatch.setattr(evidence_pack.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=stdout, stderr=""))

    with pytest.raises(SystemExit, match="verified post-run GPU verdict"):
        evidence_pack.verified_standalone_gpu_verdict(tmp_path)


def copy_standalone_tree(source: Path, target: Path) -> Path:
    run_name = "inference-infer20260923184725"
    runs = target / "artifacts/runs"
    runs.mkdir(parents=True)
    shutil.copytree(source / "artifacts/runs" / run_name, runs / run_name)
    for prefix in ("invoice", "absence"):
        shutil.copy2(source / "artifacts/runs" / f"{prefix}-{run_name}.json", runs)
    for folder in ("scripts", "src"):
        shutil.copytree(source / folder, target / folder, ignore=shutil.ignore_patterns("__pycache__"))
    return runs / run_name / "post-run/verdict.json"


def test_real_verifier_aba_swap_cannot_publish_forged_verdict(
    tmp_path: Path, standalone_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = copy_standalone_tree(standalone_root, tmp_path)
    genuine = output.read_bytes()
    forged = json.loads(genuine)
    forged["latency_ms"]["c32"]["p95_ttft_ms"] = 1.0
    forged_bytes = canonical_stdout(forged).encode()
    output.write_bytes(forged_bytes)
    real_run = subprocess.run

    def swapping_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        output.write_bytes(genuine)
        try:
            return real_run(*args, **kwargs)  # type: ignore[arg-type]
        finally:
            output.write_bytes(forged_bytes)

    monkeypatch.setattr(evidence_pack.subprocess, "run", swapping_run)

    with pytest.raises(SystemExit, match="changed during verification"):
        evidence_pack.verified_standalone_gpu_verdict(tmp_path)

    monkeypatch.setattr(evidence_pack.subprocess, "run", real_run)
    output.write_bytes(genuine)
    result = evidence_pack.verified_standalone_gpu_verdict(tmp_path)
    assert result == json.loads(genuine) == build_verdict(tmp_path)


@pytest.mark.parametrize(
    "change",
    [
        lambda verdict: verdict.update(run_id="inference-other"),
        lambda verdict: verdict.update(provenance_limitations={}),
        lambda verdict: verdict["provenance_limitations"].pop("model_revision"),
        lambda verdict: verdict["provenance_limitations"].update(extra="unreviewed"),
        lambda verdict: verdict["provenance_limitations"].update(absence=" "),
        lambda verdict: verdict["provenance_limitations"].update(benchmark="Capture anchored."),
        lambda verdict: verdict["standalone_gpu_latency"].pop("basis"),
        lambda verdict: verdict["standalone_gpu_latency"].update(basis="Independently anchored capture."),
    ],
    ids=["run-id", "empty-limitations", "missing-key", "extra-key", "blank-value", "benchmark-anchored", "missing-basis", "anchored-basis"],
)
def test_standalone_verdict_requires_exact_provenance_and_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: Callable[[dict], object],
) -> None:
    verdict = standalone_verdict_fixture()
    change(verdict)
    write_standalone_verdict(tmp_path, verdict)
    monkeypatch.setattr(evidence_pack.subprocess, "run", verified_run(verdict))

    with pytest.raises(SystemExit, match="missing required schema"):
        evidence_pack.verified_standalone_gpu_verdict(tmp_path)


def test_standalone_verdict_preserves_positive_waiting_as_descriptive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    verdict = standalone_verdict_fixture()
    verdict["prometheus_observed"]["c32"]["vllm:num_requests_waiting"]["max"] = 5  # type: ignore[index]
    write_standalone_verdict(tmp_path, verdict)
    monkeypatch.setattr(evidence_pack.subprocess, "run", verified_run(verdict))

    result = evidence_pack.verified_standalone_gpu_verdict(tmp_path)

    assert result["prometheus_observed"]["c32"]["vllm:num_requests_waiting"]["max"] == 5  # type: ignore[index]


def local_hpa_verdict_fixture() -> dict[str, object]:
    scaled = {"cpu": "scaled_cpu_millicores", "kv": "scaled_kv", "queue": "scaled_queue"}
    return {
        "schema_version": 2,
        "verdict": "VALID",
        "failing_checks": [],
        "claims": {
            "local_hpa_signal_plumbing": {"allowed": True, "failing_checks": [], "provenance": "local-synthetic", "verdict": "VALID"},
        },
        "local_evidence": {
            "verdict": "VALID",
            "failing_checks": [],
            "arms": [
                {
                    "arm": arm,
                    "verdict": "VALID",
                    "failing_checks": [],
                    "observations": {
                        "desired_replicas": 2, "ready_replicas": 2,
                        "scaled_cpu_millicores": 20.0, "scaled_kv": 0.2, "scaled_queue": 0.0, field: 9.0,
                    },
                }
                for arm, field in scaled.items()
            ],
        },
    }


@pytest.mark.parametrize(
    "change",
    [
        lambda verdict: verdict.update(verdict="INVALID"),
        lambda verdict: verdict["claims"]["local_hpa_signal_plumbing"].update(allowed=False),
        lambda verdict: verdict["claims"]["local_hpa_signal_plumbing"].update(verdict="INVALID"),
        lambda verdict: verdict["claims"]["local_hpa_signal_plumbing"].update(failing_checks=["x"]),
        lambda verdict: verdict["local_evidence"].update(verdict="INVALID"),
        lambda verdict: verdict["local_evidence"]["arms"].pop(),
        lambda verdict: verdict["local_evidence"]["arms"][0].update(verdict="INVALID"),
        lambda verdict: verdict["local_evidence"]["arms"][1]["observations"].update(ready_replicas=1),
    ],
    ids=["top", "claim-disallowed", "claim-invalid", "claim-failing", "local-invalid", "missing-arm", "arm-invalid", "not-ready"],
)
def test_invalid_local_hpa_verdict_blocks_publication_and_chart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: Callable[[dict], object],
) -> None:
    verdict = local_hpa_verdict_fixture()
    change(verdict)
    output = tmp_path / "published"
    local = tmp_path / "local-verdict.json"
    local.write_text(json.dumps(verdict), encoding="utf-8")
    monkeypatch.setattr(evidence_pack, "OUT", output)
    monkeypatch.setattr(evidence_pack, "VERDICT", local)
    monkeypatch.setattr(evidence_pack, "verified_standalone_gpu_verdict", lambda _root: standalone_verdict_fixture())

    with pytest.raises(SystemExit, match="local HPA verdict"):
        evidence_pack.main([])
    with pytest.raises(SystemExit, match="local HPA verdict"):
        evidence_pack.hpa_signal_chart(verdict)

    assert not output.exists()


def test_failed_optional_metric_chart_leaves_previous_outputs_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "published"
    output.mkdir()
    previous = {
        name: f"previous {name}\n"
        for name in (
            "gpu-latency-by-concurrency.svg", "local-hpa-signal-plumbing.svg", "two-node-canary-startup.svg",
            "two-node-vllm-metric-path.svg", "evidence-summary.json",
        )
    }
    for name, text in previous.items():
        (output / name).write_text(text, encoding="utf-8")
    local = tmp_path / "local-verdict.json"
    local.write_text(json.dumps(local_hpa_verdict_fixture()), encoding="utf-8")
    run_dir = tmp_path / "two-node-incomplete"
    (run_dir / "server-evidence").mkdir(parents=True)
    (run_dir / "run-manifest.json").write_text(json.dumps({"workload": {"model_id": "fixture"}}), encoding="utf-8")
    monkeypatch.setattr(evidence_pack, "ROOT", tmp_path)
    monkeypatch.setattr(evidence_pack, "OUT", output)
    monkeypatch.setattr(evidence_pack, "VERDICT", local)
    monkeypatch.setattr(evidence_pack, "verified_standalone_gpu_verdict", lambda _root: standalone_verdict_fixture())
    monkeypatch.setattr(evidence_pack, "_completed_two_node_metric_run", lambda _run_dir, _manifest: True)

    with pytest.raises(SystemExit, match="timing evidence is incomplete"):
        evidence_pack.main(["--two-node-run", str(run_dir)])

    assert {path.name: path.read_text(encoding="utf-8") for path in output.iterdir()} == previous
    assert sorted(path.name for path in tmp_path.iterdir()) == ["local-verdict.json", "published", "two-node-incomplete"]


def test_successful_publish_removes_stale_optional_metric_chart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "published"
    output.mkdir()
    (output / "two-node-vllm-metric-path.svg").write_text("stale\n", encoding="utf-8")
    local = tmp_path / "local-verdict.json"
    local.write_text(json.dumps(local_hpa_verdict_fixture()), encoding="utf-8")
    monkeypatch.setattr(evidence_pack, "ROOT", tmp_path)
    monkeypatch.setattr(evidence_pack, "OUT", output)
    monkeypatch.setattr(evidence_pack, "VERDICT", local)
    monkeypatch.setattr(evidence_pack, "verified_standalone_gpu_verdict", lambda _root: standalone_verdict_fixture())

    evidence_pack.main([])

    assert sorted(path.name for path in output.iterdir()) == [
        "artifact-catalog.json", "evidence-summary.json", "gpu-capacity-envelope.svg", "gpu-latency-by-concurrency.svg",
        "gpu-observed-pressure.svg", "gpu-ttft-tpot-contrast.svg", "local-hpa-signal-plumbing.svg", "local-hpa-verdict.json",
        "two-node-canary-startup.svg",
    ]
    summary = json.loads((output / "evidence-summary.json").read_text(encoding="utf-8"))
    assert summary["gpu_chart_metrics"]["c32_over_c4"]["p50_ttft_ms"] == pytest.approx(3.0)
    assert summary["gpu_chart_metrics"]["rendered_sha256"] == {
        name: hashlib.sha256((output / name).read_bytes()).hexdigest()
        for name in ("gpu-ttft-tpot-contrast.svg", "gpu-observed-pressure.svg")
    }
    assert summary["two_node_metric_path"] is None
    assert summary["standalone_gpu_verification"]["run_id"] == "inference-infer20260923184725"
    assert "not independently anchored" in summary["standalone_gpu_verification"]["standalone_gpu_latency"]["basis"]
    assert any("locally checksum-verified" in item and "not independently anchored" in item for item in summary["boundaries"])
    assert "not independently anchored" in (output / "gpu-latency-by-concurrency.svg").read_text(encoding="utf-8")
    assert not [path for path in tmp_path.iterdir() if path.name.startswith(".evidence-pack-staging-")]


def test_publication_emits_slide_ready_artifact_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "published"
    local = tmp_path / "local-verdict.json"
    local.write_text(json.dumps(local_hpa_verdict_fixture()), encoding="utf-8")
    latest = tmp_path / "artifacts/live-two-node-20260925T115115Z/run-manifest.json"
    latest.parent.mkdir(parents=True)
    latest_payload = {
        "status": "failed",
        "evidence_status": "incomplete",
        "failure": {"error_type": "TwoNodeError"},
        "report": {"attempted": False, "confirmed": False},
        "finalization_errors": ["server guard export failed"],
    }
    latest.write_text(json.dumps(latest_payload), encoding="utf-8")
    monkeypatch.setattr(evidence_pack, "ROOT", tmp_path)
    monkeypatch.setattr(evidence_pack, "OUT", output)
    monkeypatch.setattr(evidence_pack, "VERDICT", local)
    monkeypatch.setattr(evidence_pack, "verified_standalone_gpu_verdict", lambda _root: standalone_verdict_fixture())

    evidence_pack.main([])

    summary = json.loads((output / "evidence-summary.json").read_text(encoding="utf-8"))
    catalog_path = output / "artifact-catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert summary["artifact_catalog"] == {
        "path": "artifact-catalog.json",
        "sha256": hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
    }
    assert catalog["schema"] == "srecon26-presentation-input/v1"
    assert catalog["metrics"]["local_hpa_verdict"]["path"] == "artifacts/evidence-pack/local-hpa-verdict.json"
    assert (output / "local-hpa-verdict.json").is_file()
    assert [slide["number"] for slide in catalog["slides"]] == list(range(1, 17))
    assert catalog["results"]["two_node_metric_path"]["status"] == "unavailable"
    assert catalog["results"]["latest_live_attempt"] == {
        "run": "live-two-node-20260925T115115Z",
        "manifest_path": "artifacts/live-two-node-20260925T115115Z/run-manifest.json",
        "manifest_sha256": hashlib.sha256(latest.read_bytes()).hexdigest(),
        "status": "failed",
        "evidence_status": "incomplete",
        "failure_type": "TwoNodeError",
        "report": {"attempted": False, "confirmed": False},
        "finalization_errors": ["server guard export failed"],
    }
    paired = catalog["results"]["paired_hpa_comparison"]
    assert paired["status"] == "unavailable"
    assert paired["comparative_claim_allowed"] is False
    assert paired["required_block_order"] == ["AB", "BA", "AB"]
    assert paired["primary_outcome"]["metric"] == "pressure_window_p95_ttft_ms"
    assert paired["planned_outputs"] == [
        "artifacts/comparisons/<comparison-id>/analysis/summary.json",
        "artifacts/comparisons/<comparison-id>/analysis/paired-blocks.csv",
        "artifacts/comparisons/<comparison-id>/charts/paired-p95-ttft.svg",
        "artifacts/comparisons/<comparison-id>/charts/queue-readiness-overlay.svg",
    ]
    for chart in catalog["charts"]:
        chart_path = tmp_path / chart["path"]
        if chart["status"] == "available":
            assert chart_path.is_file()
            assert chart["sha256"] == hashlib.sha256(chart_path.read_bytes()).hexdigest()
        else:
            assert chart["sha256"] is None


def test_failed_two_node_run_cannot_publish_retained_metric_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "artifacts/live-two-node-failed"
    evidence = run_dir / "server-evidence"
    evidence.mkdir(parents=True)
    (run_dir / "run-manifest.json").write_text(
        json.dumps({"status": "failed", "evidence_status": "incomplete", "workload": {"model_id": "fixture"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        evidence_pack,
        "_timing_payloads",
        lambda _evidence, _phase: [{"ttft_seconds": 0.01}],
    )
    monkeypatch.setattr(evidence_pack, "_prometheus_metric_maximum", lambda _path, fragments: 0.1)
    monkeypatch.setattr(evidence_pack, "_gpu_utilization_maximum", lambda _path: 10.0)

    with pytest.raises(SystemExit, match="completion contract"):
        evidence_pack._two_node_metric_path(run_dir)


def test_measured_metric_path_stays_supplemental_to_layered_story(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(evidence_pack, "ROOT", tmp_path)
    monkeypatch.setattr(evidence_pack, "OUT", tmp_path / "artifacts/evidence-pack")
    rendered = {
        name: f"{name}\n"
        for name in evidence_pack.CATALOG_CHARTS
    }
    rendered[evidence_pack.LOCAL_HPA_VERDICT_ARTIFACT] = "{}\n"

    catalog = json.loads(evidence_pack._catalog_json(rendered, {"run": "live-two-node-complete"}))
    slide = next(item for item in catalog["slides"] if item["number"] == 10)

    assert catalog["results"]["two_node_metric_path"]["status"] == "available"
    assert slide["headline"] == "Scale on engine signals."
    assert slide["charts"] == []
    assert not any("two-node-vllm-metric-path.svg" in item["charts"] for item in catalog["slides"])


def test_capacity_projection_uses_verified_c32_p95_envelope() -> None:
    projection, svg = evidence_pack._capacity_projection(talk_verdict_fixture())

    assert projection["basis"] == "Little's Law proxy from c32 concurrency and p95 E2E"
    assert projection["measured"] == {"concurrency": 32, "output_tokens": 128, "p95_e2e_ms": 851.37}
    assert projection["burst_completion_rate_proxy_rps"] == pytest.approx(37.58648, abs=1e-5)
    assert projection["output_token_rate_proxy_tps"] == pytest.approx(4811.069, abs=1e-3)
    assert projection["operating_headroom_fraction"] == pytest.approx(0.7)
    assert projection["projected_max_output_tokens"] == {"5_rps": 673, "10_rps": 336, "20_rps": 168, "30_rps": 112}
    assert "PROJECTED, not measured at target RPS" in svg
    assert "gpu-capacity-envelope.svg" == evidence_pack.CAPACITY_ARTIFACT


def test_catalog_uses_layered_inference_story_and_registered_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(evidence_pack, "ROOT", tmp_path)
    monkeypatch.setattr(evidence_pack, "OUT", tmp_path / "artifacts/evidence-pack")
    rendered = {name: f"{name}\n" for name in evidence_pack.CATALOG_CHARTS}
    rendered[evidence_pack.LOCAL_HPA_VERDICT_ARTIFACT] = "{}\n"

    catalog = json.loads(evidence_pack._catalog_json(rendered, None))

    assert [slide["headline"] for slide in catalog["slides"]] == [
        "Most production inference sucks.",
        "Wrong layer. Wrong metric.",
        "Start with workload math.",
        "Prefill sets TTFT. Decode sets TPOT.",
        "Capacity is a token budget.",
        "FP8 frees memory for more requests.",
        "Three engine levers.",
        "Route to the cache.",
        "HPA watches the wrong thing.",
        "Scale on engine signals.",
        "Qwen3.8-27B on 8x RTX 4090.",
        "8 concurrent: first token 7x slower.",
        "Empty queue. Latency still rose.",
        "Control on the SLO.",
        "FP8 buys memory here, not speed.",
        "Instrument every layer.",
    ]
    banned = ("failed", "invalid", "limitation", "limited resources", "not yet", "smoke", "scale test", "not shown")
    assert not any(word in f"{slide['headline']} {slide['pill']}".casefold() for slide in catalog["slides"] for word in banned)
    registered = set(catalog["published_sources"])
    published = [slide for slide in catalog["slides"] if slide["pill"].startswith("PUBLISHED")]
    assert published
    assert all(slide["source_ids"] and set(slide["source_ids"]) <= registered for slide in published)
    google_claim = catalog["published_sources"]["google-gke-hpa"]["claim"]
    assert all(value in google_claim for value in ("150%", "queue target 25", "below ~0.4 s", "batch target 50", "almost below ~0.3 s", "1-16 replicas"))
    turboquant_claim = catalog["published_sources"]["vllm-turboquant"]["claim"]
    assert "Qwen3-30B" in turboquant_claim and "same throughput at 2x capacity" in turboquant_claim
    slide_sources = {slide["number"]: slide["source_ids"] for slide in catalog["slides"]}
    assert slide_sources[6] == ["vllm-fp8", "vllm-turboquant"]
    assert slide_sources[7] == ["vllm-x-omni", "vllm-x-v030"]
    measured = [slide for slide in catalog["slides"] if slide["pill"].startswith("MEASURED")]
    assert [slide["number"] for slide in measured] == [11, 12, 13, 15]
    assert all(set(slide["source_ids"]) <= set(catalog["measured_results"]) for slide in measured)
    assert all(value["status"] == "unavailable" for value in catalog["measured_results"].values())
    assert catalog["published_sources"]["vllm-x-omni"]["url"] == "https://x.com/vllm_project/status/2101665925137379695"
    assert catalog["published_sources"]["vllm-x-v030"]["url"] == "https://x.com/vllm_project/status/2102593516740411733"
    preprint = catalog["published_sources"]["cache-routing-preprint"]
    assert preprint["publisher"] == "arXiv 2602.04900v3 preprint"
    assert "peer-reviewed" not in preprint["context"] and "industry paper" not in preprint["context"]
    assert not [slide for slide in catalog["slides"] if slide["pill"].startswith("OURS")]
    assert not [slide for slide in catalog["slides"] if slide["charts"]]
    assert {slide["pill"].split(" - ", 1)[0] for slide in catalog["slides"]} <= {"PUBLISHED", "PATTERN", "SYNTHESIS", "MEASURED"}
    chart_roles = {chart["name"]: chart["presentation_role"] for chart in catalog["charts"]}
    assert chart_roles == {
        "gpu-latency-by-concurrency.svg": "excluded",
        "local-hpa-signal-plumbing.svg": "excluded",
        "gpu-ttft-tpot-contrast.svg": "appendix-only",
        "gpu-observed-pressure.svg": "appendix-only",
        "gpu-capacity-envelope.svg": "appendix-only",
        "two-node-canary-startup.svg": "excluded",
        "two-node-vllm-metric-path.svg": "excluded",
    }


def test_latest_live_attempt_reports_newer_manifestless_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    older = tmp_path / "artifacts/live-two-node-20260925T1200Z"
    newer = tmp_path / "artifacts/live-two-node-20260925T120030Z"
    older.mkdir(parents=True)
    newer.mkdir(parents=True)
    (older / "run-manifest.json").write_text(json.dumps({"status": "failed"}), encoding="utf-8")
    monkeypatch.setattr(evidence_pack, "ROOT", tmp_path)

    result = evidence_pack._latest_live_attempt()

    assert result is not None
    assert result["run"] == newer.name
    assert result["manifest_path"] == f"artifacts/{newer.name}/run-manifest.json"
    assert result["manifest_sha256"] is None
    assert result["status"] == "manifest-missing"
    assert result["evidence_status"] == "incomplete"
    assert result["failure_type"] == "ManifestMissing"


def test_main_blocks_evidence_publication_when_standalone_verification_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "published"
    monkeypatch.setattr(evidence_pack, "ROOT", tmp_path)
    monkeypatch.setattr(evidence_pack, "OUT", output)
    monkeypatch.setattr(
        evidence_pack.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="checksum mismatch"),
    )
    monkeypatch.setattr(evidence_pack, "gpu_latency_chart", lambda *_: pytest.fail("GPU chart published before verification"))

    with pytest.raises(SystemExit, match="post-run standalone GPU verification failed: checksum mismatch"):
        evidence_pack.main([])

    assert not output.exists()


def test_gpu_chart_and_summary_use_verified_latency_and_report_evidence_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    standalone = standalone_verdict_fixture()
    latency = standalone["latency_ms"]
    assert isinstance(latency, dict)
    monkeypatch.setattr(evidence_pack, "OUT", tmp_path)

    evidence_pack.gpu_latency_chart(latency)
    evidence_pack.write_summary(
        standalone,
        {"claims": {"local_hpa_signal_plumbing": {"allowed": True}}},
        {"attempts": [], "counts": {"created": 0, "running": 0, "completed": 0}},
    )

    chart = (tmp_path / "gpu-latency-by-concurrency.svg").read_text(encoding="utf-8")
    summary = json.loads((tmp_path / "evidence-summary.json").read_text(encoding="utf-8"))
    assert all(value in chart for value in ("11.0", "21.0", "33.0", "43.0", "111.0", "121.0", "133.0", "143.0"))
    assert summary["gpu_measurements"]["c4"]["requests"] == 4
    assert summary["gpu_measurements"]["c32"]["requests"] == 32
    published = summary["standalone_gpu_verification"]
    assert published["raw_request_samples"] == {"c4": 4, "c32": 32}
    assert published["prometheus_observed"]["c32"]["vllm:num_requests_waiting"]["max"] == 0
    assert published["prometheus_observed"]["c32"]["vllm:kv_cache_usage_perc"]["max"] < 0.01
    assert published["provenance_limitations"] == standalone["provenance_limitations"]
    assert published["kubernetes_hpa"]["allowed"] is False
    assert published["paired_hpa_performance"]["allowed"] is False


def talk_verdict_fixture() -> dict[str, object]:
    verdict = standalone_verdict_fixture()
    latency, observed = verdict["latency_ms"], verdict["prometheus_observed"]
    assert isinstance(latency, dict) and isinstance(observed, dict)
    latency["c4"].update(p50_ttft_ms=6.278, p50_tpot_ms=5.959196850393701)
    latency["c32"].update(p50_ttft_ms=20.991, p95_e2e_ms=851.37, p50_tpot_ms=6.297393700787402)
    observed["c4"]["vllm:kv_cache_usage_perc"]["max"] = 0.0011560136794952491
    observed["c32"]["vllm:kv_cache_usage_perc"]["max"] = 0.007321419970136356
    return verdict


def test_gpu_chart_metrics_and_svgs_use_verified_values() -> None:
    import xml.etree.ElementTree as ET

    metrics = evidence_pack.gpu_chart_metrics(talk_verdict_fixture())

    arms = metrics["arms"]
    assert metrics["c32_over_c4"]["p50_ttft_ms"] == pytest.approx(3.3436, abs=1e-4)
    assert metrics["c32_over_c4"]["p50_tpot_ms"] == pytest.approx(1.0568, abs=1e-4)
    assert (arms["c4"]["max_waiting_requests"], arms["c32"]["max_waiting_requests"]) == (0, 0)
    assert (arms["c4"]["max_running_requests"], arms["c32"]["max_running_requests"]) == (4, 32)
    assert arms["c4"]["max_kv_cache_percent"] == pytest.approx(0.1156, abs=1e-4)
    assert arms["c32"]["max_kv_cache_percent"] == pytest.approx(0.7321, abs=1e-4)
    contrast = evidence_pack._contrast_svg(metrics)
    pressure = evidence_pack._pressure_svg(metrics)
    for svg in (contrast, pressure):
        ET.fromstring(svg)
        assert "c4 (n=4)" in svg and "c32 (n=32)" in svg
        assert "not independently anchored" in svg and "<image" not in svg and "href" not in svg
    assert all(text in contrast for text in ("6.278 ms", "20.991 ms", "5.959 ms", "6.297 ms", "3.34\u00d7", "1.06\u00d7"))
    assert contrast.count("Shared axis: 0 to 25 ms") == 1 and "Own scale" not in contrast
    assert all(text in pressure for text in ("0.116%", "0.732%", "NOT Kubernetes HPA", "No queue buildup", "waiting 0"))
    assert "Axis: 0 to 100% of cache capacity" in pressure and "0 to 1%" not in pressure
    for svg in (contrast, pressure):
        assert "Qwen/Qwen2.5-1.5B-Instruct" in svg and "NVIDIA GeForce RTX 4090" in svg
        sizes = [int(size) for size in re.findall(r'font-size="(\d+)"', svg)]
        assert min(sizes) >= 24


def test_chart_captions_follow_drawn_axis_scale() -> None:
    verdict = talk_verdict_fixture()
    verdict["latency_ms"]["c32"].update(p50_ttft_ms=40.0)
    verdict["prometheus_observed"]["c32"]["vllm:num_requests_waiting"]["max"] = 3
    metrics = evidence_pack.gpu_chart_metrics(verdict)

    contrast = evidence_pack._contrast_svg(metrics)
    pressure = evidence_pack._pressure_svg(metrics)

    assert "Shared axis: 0 to 50 ms" in contrast and "50 ms</text>" in contrast
    assert "No queue buildup" not in pressure and "waiting 3" in pressure


def test_chart_metrics_reject_unexpected_model() -> None:
    verdict = talk_verdict_fixture()
    verdict["model"] = "other/model"

    with pytest.raises(SystemExit, match="unexpected chart model"):
        evidence_pack.gpu_chart_metrics(verdict)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda v: v["latency_ms"]["c32"].update(p50_ttft_ms=float("nan")), "invalid chart metric"),
        (lambda v: v["latency_ms"]["c4"].update(p50_tpot_ms=-1), "invalid chart metric"),
        (lambda v: v["latency_ms"]["c4"].update(p50_ttft_ms=0), "ratio is undefined"),
        (lambda v: v["latency_ms"]["c4"].update(requests=5), "invalid c4 chart samples"),
        (lambda v: v["prometheus_observed"]["c32"].pop("vllm:num_requests_running"), "no c32 samples"),
        (lambda v: v["prometheus_observed"]["c4"]["vllm:num_requests_waiting"].update(max=float("inf")), "invalid chart metric"),
        (lambda v: v["prometheus_observed"]["c4"]["vllm:kv_cache_usage_perc"].update(max=1.5), "above 1"),
        (lambda v: v["prometheus_observed"]["c4"]["vllm:kv_cache_usage_perc"].update(max=True), "invalid chart metric"),
    ],
    ids=["nan", "negative", "zero-base", "count", "missing-series", "inf", "kv-fraction", "bool"],
)
def test_invalid_chart_metrics_block_all_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: Callable[[dict], object], message: str,
) -> None:
    verdict = talk_verdict_fixture()
    change(verdict)
    output = tmp_path / "published"
    local = tmp_path / "local-verdict.json"
    local.write_text(json.dumps(local_hpa_verdict_fixture()), encoding="utf-8")
    monkeypatch.setattr(evidence_pack, "ROOT", tmp_path)
    monkeypatch.setattr(evidence_pack, "OUT", output)
    monkeypatch.setattr(evidence_pack, "VERDICT", local)
    monkeypatch.setattr(evidence_pack, "verified_standalone_gpu_verdict", lambda _root: verdict)

    with pytest.raises(SystemExit, match=message):
        evidence_pack.main([])

    assert not output.exists()

def test_post_run_gpu_verdict_uses_raw_standalone_evidence(standalone_root: Path) -> None:
    root = standalone_root

    verdict = build_verdict(root)

    assert verdict["standalone_gpu_latency"]["allowed"] is True
    assert verdict["kubernetes_hpa"]["allowed"] is False
    assert verdict["paired_hpa_performance"]["allowed"] is False
    assert verdict["raw_file_count"] == 125
    assert verdict["latency_ms"]["c4"]["requests"] == 4
    assert verdict["latency_ms"]["c32"]["requests"] == 32
    assert verdict["prometheus_observed"]["c32"]["vllm:num_requests_waiting"]["max"] == 0
    assert verdict["prometheus_observed"]["c32"]["vllm:num_requests_running"]["max"] == 32
    assert verdict["reverification_required"].endswith("--verify")
    limitations = verdict["provenance_limitations"]
    assert "retained local consistency" in limitations["pre_anchor"]
    assert "not original capture anchoring" in limitations["pre_anchor"]
    assert "normalized in place on 2026-09-25" in limitations["benchmark"]
    assert "original remote-path file unavailable" in limitations["benchmark"]
    assert "not independent snapshot hash" in limitations["model_revision"]
    assert "post-hoc re-capture" in limitations["absence"]
    assert "not independently anchored" in verdict["standalone_gpu_latency"]["basis"]


def test_post_run_gpu_verdict_rejects_modified_preanchored_remote_file(
    monkeypatch: pytest.MonkeyPatch, standalone_root: Path,
) -> None:
    target = standalone_root / "artifacts/runs/inference-infer20260923184725/remote-evidence/inference-gpu-identity.txt"
    original = Path.read_bytes

    def tampered(path: Path) -> bytes:
        data = original(path)
        return data + b"\nmodified after capture\n" if path == target else data

    monkeypatch.setattr(Path, "read_bytes", tampered)

    with pytest.raises(ValueError, match="pre-anchor checksum mismatch"):
        build_verdict(standalone_root)


def test_post_run_gpu_verdict_rejects_linked_remote_file(monkeypatch: pytest.MonkeyPatch, standalone_root: Path) -> None:
    target = standalone_root / "artifacts/runs/inference-infer20260923184725/remote-evidence/inference-gpu-identity.txt"
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == target or original(path))

    with pytest.raises(ValueError, match="unexpected pre-anchor source"):
        build_verdict(standalone_root)


@pytest.mark.parametrize(
    ("source", "change", "error"),
    [
        ("journal/journal.ndjson", lambda raw: raw.rstrip(b"\n") + b"{", "partial journal line"),
        ("manual-benchmark/c32-request-1.json", lambda raw: raw.replace(b'"http_code": 200', b'"http_code": 500'), "checksum mismatch"),
        ("remote-evidence/inference-contract.json", lambda raw: raw.replace(b"Qwen/Qwen2.5-1.5B-Instruct", b"wrong-model"), "pre-anchor checksum mismatch"),
    ],
)
def test_post_run_gpu_verdict_rejects_tampered_sources(
    monkeypatch: pytest.MonkeyPatch, standalone_root: Path, source: str, change: Callable[[bytes], bytes], error: str,
) -> None:
    root = standalone_root
    target = root / "artifacts/runs/inference-infer20260923184725" / source
    original = Path.read_bytes

    def tampered(path: Path) -> bytes:
        data = original(path)
        return change(data) if path == target else data

    monkeypatch.setattr(Path, "read_bytes", tampered)

    with pytest.raises(ValueError, match=error):
        build_verdict(root)


def test_post_run_gpu_verdict_rejects_forged_sample_even_with_matching_checksum(
    monkeypatch: pytest.MonkeyPatch, standalone_root: Path,
) -> None:
    root = standalone_root
    bench = root / "artifacts/runs/inference-infer20260923184725/manual-benchmark"
    target = bench / "c32-metrics-during.ndjson"
    sums = bench / "SHA256SUMS"
    original = Path.read_bytes
    original_sample = original(target)
    changed_sample = original_sample.replace(b'"value":32.0}', b'"value":-1.0}', 1)
    assert changed_sample != original_sample
    changed_sums = original(sums).replace(
        hashlib.sha256(original_sample).hexdigest().encode(),
        hashlib.sha256(changed_sample).hexdigest().encode(),
    )

    def forged(path: Path) -> bytes:
        if path == target:
            return changed_sample
        if path == sums:
            return changed_sums
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", forged)

    with pytest.raises(ValueError, match="invalid Prometheus value"):
        build_verdict(root)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (lambda data: data.replace(b"2026-09-23T19:02:01.884Z", b"2026-09-23T18:49:00.000Z"), "outside journal lifecycle"),
        (lambda data: data.replace(b"2026-09-23T19:02:01.884Z", b"2026-09-23T19:05:00.000Z"), "outside journal lifecycle"),
        (lambda data: data.replace(b"2026-09-23T19:02:01.884Z", b"2026-09-23T19:02:01.885Z", 1), "inconsistent Prometheus sample time"),
        (lambda data: data.replace(b'"sample":2,', b'"sample":999,'), "noncontiguous Prometheus samples"),
        (lambda data: data.replace(b"2026-09-23T19:02:02.000Z", b"2026-09-23T19:02:01.884Z"), "nonincreasing Prometheus sample time"),
        (lambda data: data.replace(b'"value":0.0}', b'"value":}', 1), ""),
    ],
    ids=["before-create", "after-teardown", "within-sample", "sample-gap", "sample-order", "malformed-sample"],
)
def test_post_run_gpu_verdict_rejects_forged_metrics_with_matching_checksum(
    monkeypatch: pytest.MonkeyPatch, standalone_root: Path, change: Callable[[bytes], bytes], error: str,
) -> None:
    bench = standalone_root / "artifacts/runs/inference-infer20260923184725/manual-benchmark"
    target = bench / "c4-metrics-during.ndjson"
    sums = bench / "SHA256SUMS"
    original = Path.read_bytes
    original_sample = original(target)
    changed_sample = change(original_sample)
    assert changed_sample != original_sample
    changed_sums = original(sums).replace(
        hashlib.sha256(original_sample).hexdigest().encode(),
        hashlib.sha256(changed_sample).hexdigest().encode(),
    )

    def forged(path: Path) -> bytes:
        if path == target:
            return changed_sample
        if path == sums:
            return changed_sums
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", forged)

    with pytest.raises(ValueError, match=error or None):
        build_verdict(standalone_root)


@pytest.mark.parametrize("epoch", [0, 4102444800])
def test_post_run_gpu_verdict_rejects_forged_stream_epoch_with_matching_checksum(
    monkeypatch: pytest.MonkeyPatch, standalone_root: Path, epoch: int,
) -> None:
    bench = standalone_root / "artifacts/runs/inference-infer20260923184725/manual-benchmark"
    target = bench / "c4-request-1-body.ndjson"
    sums = bench / "SHA256SUMS"
    original = Path.read_bytes
    body = original(target)
    changed_body = re.sub(rb'"created":\d+', b'"created":' + str(epoch).encode(), body)
    assert changed_body != body
    changed_sums = original(sums).replace(
        hashlib.sha256(body).hexdigest().encode(),
        hashlib.sha256(changed_body).hexdigest().encode(),
    )

    def forged(path: Path) -> bytes:
        if path == target:
            return changed_body
        if path == sums:
            return changed_sums
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", forged)

    with pytest.raises(ValueError, match="stream created outside journal lifecycle"):
        build_verdict(standalone_root)


def test_post_run_gpu_verdict_rejects_duplicate_completion_body_with_matching_checksum(
    monkeypatch: pytest.MonkeyPatch, standalone_root: Path,
) -> None:
    bench = standalone_root / "artifacts/runs/inference-infer20260923184725/manual-benchmark"
    target = bench / "c4-request-2-body.ndjson"
    sums = bench / "SHA256SUMS"
    original = Path.read_bytes
    duplicate = original(bench / "c4-request-1-body.ndjson")
    assert duplicate != original(target)
    changed_sums = original(sums).replace(
        hashlib.sha256(original(target)).hexdigest().encode(),
        hashlib.sha256(duplicate).hexdigest().encode(),
    )

    def forged(path: Path) -> bytes:
        if path == target:
            return duplicate
        if path == sums:
            return changed_sums
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", forged)

    with pytest.raises(ValueError, match="duplicate completion ID"):
        build_verdict(standalone_root)


def test_post_run_gpu_verdict_rejects_forged_tpot_with_matching_checksum(
    monkeypatch: pytest.MonkeyPatch, standalone_root: Path,
) -> None:
    bench = standalone_root / "artifacts/runs/inference-infer20260923184725/manual-benchmark"
    target = bench / "c4-request-1.json"
    sums = bench / "SHA256SUMS"
    original = Path.read_bytes
    request = json.loads(original(target))
    request["tpot_seconds"] += 0.001
    changed_request = json.dumps(request).encode()
    changed_sums = original(sums).replace(
        hashlib.sha256(original(target)).hexdigest().encode(),
        hashlib.sha256(changed_request).hexdigest().encode(),
    )

    def forged(path: Path) -> bytes:
        if path == target:
            return changed_request
        if path == sums:
            return changed_sums
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", forged)

    with pytest.raises(ValueError, match="tpot timing mismatch"):
        build_verdict(standalone_root)


@pytest.mark.parametrize(
    "change",
    [
        lambda raw: raw.replace(b'"prompt_tokens":272,"total_tokens":400', b'"prompt_tokens":273,"total_tokens":400'),
        lambda raw: raw.replace(b'"finish_reason":"length"', b'"finish_reason":"stop"'),
        lambda raw: raw.replace(b"data: [DONE]", b"data: not-json\n\ndata: [DONE]"),
    ],
    ids=["final-usage", "finish-reason", "non-json-data"],
)
def test_post_run_gpu_verdict_rejects_invalid_stream_with_matching_checksum(
    monkeypatch: pytest.MonkeyPatch, standalone_root: Path, change: Callable[[bytes], bytes],
) -> None:
    bench = standalone_root / "artifacts/runs/inference-infer20260923184725/manual-benchmark"
    target = bench / "c4-request-1-body.ndjson"
    sums = bench / "SHA256SUMS"
    original = Path.read_bytes
    changed_body = change(original(target))
    assert changed_body != original(target)
    changed_sums = original(sums).replace(
        hashlib.sha256(original(target)).hexdigest().encode(),
        hashlib.sha256(changed_body).hexdigest().encode(),
    )

    def forged(path: Path) -> bytes:
        if path == target:
            return changed_body
        if path == sums:
            return changed_sums
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", forged)

    with pytest.raises(ValueError, match="streamed response mismatch"):
        build_verdict(standalone_root)


@pytest.mark.parametrize("forgery", ["duplicate-body", "tpot"])
def test_post_run_gpu_verdict_cli_rejects_checksum_matched_forgery(
    tmp_path: Path, standalone_root: Path, forgery: str,
) -> None:
    run_name = "inference-infer20260923184725"
    source_runs = standalone_root / "artifacts/runs"
    copied_runs = tmp_path / "artifacts/runs"
    copied_runs.mkdir(parents=True)
    shutil.copytree(source_runs / run_name, copied_runs / run_name)
    for prefix in ("invoice", "absence"):
        shutil.copy2(source_runs / f"{prefix}-{run_name}.json", copied_runs)
    bench = copied_runs / run_name / "manual-benchmark"
    target = bench / ("c4-request-2-body.ndjson" if forgery == "duplicate-body" else "c4-request-1.json")
    original = target.read_bytes()
    if forgery == "duplicate-body":
        replacement = (bench / "c4-request-1-body.ndjson").read_bytes()
    else:
        request = json.loads(original)
        request["tpot_seconds"] += 0.001
        replacement = json.dumps(request).encode()
    assert replacement != original
    target.write_bytes(replacement)
    sums = bench / "SHA256SUMS"
    sums.write_bytes(sums.read_bytes().replace(
        hashlib.sha256(original).hexdigest().encode(),
        hashlib.sha256(replacement).hexdigest().encode(),
    ))

    result = subprocess.run(
        [sys.executable, "scripts/build_post_run_gpu_verdict.py", "--verify", "--root", str(tmp_path)],
        cwd=standalone_root,
        env={**os.environ, "PYTHONPATH": f"{standalone_root}:{standalone_root / 'src'}"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert ("duplicate completion ID" if forgery == "duplicate-body" else "tpot timing mismatch") in result.stderr


def test_post_run_gpu_verdict_rejects_missing_raw_request(monkeypatch: pytest.MonkeyPatch, standalone_root: Path) -> None:
    root = standalone_root
    target = root / "artifacts/runs/inference-infer20260923184725/manual-benchmark/c32-request-32.json"
    original = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda path: False if path == target else original(path))

    with pytest.raises(ValueError, match="checksum inventory not exactly 125 raw files"):
        build_verdict(root)


def manifest_fixture() -> dict[str, object]:
    manifest: dict[str, object] = {
        "guard_backend": "azure",
        "hard_deadline": DEADLINE,
        "guard_heartbeat_timeout_seconds": 120,
        "guards": {},
    }
    guards = manifest["guards"]
    assert isinstance(guards, dict)
    for role, host in (("server", "server-guard.example.test"), ("worker", "worker-guard.example.test")):
        nonce = f"{role}-nonce-12345678"
        label = f"srecon26-two-node-{role}--nonce-{nonce}"
        manifest[role] = {
            "nonce": nonce,
            "offer": {"label": label},
            "instance": {"instance_id": 41 if role == "server" else 42, "label": label},
        }
        guards[role] = {
            "backend": "azure",
            "role": role,
            "status": "ARMED",
            "root_hash": "a" * 64,
            "nonce": nonce,
            "label": label,
            "hard_deadline": DEADLINE,
            "last_heartbeat": "2026-09-24T11:00:00Z",
            "host_identity": host,
            "script_hash": "b" * 64,
            "azure_resource_id": f"/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/{role}-rg/providers/Microsoft.Compute/virtualMachines/{role}-guard",
            "azure_vm_id": "22222222-2222-2222-2222-222222222221" if role == "server" else "22222222-2222-2222-2222-222222222222",
            "host_key_fingerprint": "SHA256:" + ("A" if role == "server" else "B") * 43,
            "heartbeat_timeout_seconds": 120,
        }
    return manifest


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("backend", "github"),
        ("role", "worker"),
        ("nonce", "wrong-nonce"),
        ("label", "wrong-label"),
        ("hard_deadline", "2026-09-24T12:00:01Z"),
        ("root_hash", "not-a-hash"),
        ("host_identity", "localhost"),
        ("script_hash", "not-a-hash"),
    ],
)
def test_bound_guard_receipt_requires_every_exact_binding(field: str, value: str) -> None:
    manifest = manifest_fixture()
    manifest["guards"]["server"][field] = value  # type: ignore[index]

    valid, errors = validate_bound_guard_receipts(manifest)

    assert valid is False
    assert "server" in errors


def test_bound_guard_receipts_require_independent_host_identities() -> None:
    manifest = manifest_fixture()
    manifest["guards"]["worker"]["host_identity"] = "server-guard.example.test"  # type: ignore[index]

    valid, errors = validate_bound_guard_receipts(manifest)

    assert valid is False
    assert "independence_host_identity" in errors


def write_artifact(path: Path, payload: dict[str, object]) -> dict[str, object]:
    raw = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    path.write_bytes(raw)
    return {"artifact": path.name, "sha256": hashlib.sha256(raw).hexdigest()}


def test_absence_and_billing_require_hash_bound_exact_artifacts(tmp_path: Path) -> None:
    manifest = manifest_fixture()
    target = manifest["server"]
    assert isinstance(target, dict)
    instance = target["instance"]
    assert isinstance(instance, dict)
    nonce, instance_id, label = target["nonce"], instance["instance_id"], instance["label"]
    absence = write_artifact(
        tmp_path / "server-absence.json",
        {
            "schema": "srecon26-vast-absence-evidence/v1",
            "source": "VastCliProvider.capture_absence_evidence/v1",
            "run_id": f"two-node-server-{nonce}",
            "instance_id": instance_id,
            "label": label,
            "reads": [
                {"observed_at": f"2026-09-24T11:00:0{index}Z", "matching_instances": 0}
                for index in range(3)
            ],
        },
    )
    absence["status"] = "THREE_READS_CONFIRMED"
    billing = write_artifact(
        tmp_path / "server-invoice.json",
        {
            "schema": "srecon26-vast-invoice-evidence/v1",
            "source": "VastCliProvider.capture_invoice_charge/v1",
            "observed_at": "2026-09-24T11:30:00Z",
            "run_id": f"two-node-server-{nonce}",
            "instance_id": instance_id,
            "label": label,
            "amount_usd": "0.42",
            "provider_charge": {
                "type": "instance", "source": f"instance-{instance_id}", "amount": "0.42",
                "metadata": {"label": label},
            },
        },
    )
    billing.update({"status": "AUTHORITATIVE_INVOICE_CAPTURED", "amount_usd": "0.42"})

    assert validate_absence_artifact(tmp_path, "server", target, absence)
    assert validate_billing_artifact(tmp_path, "server", target, billing)

    (tmp_path / "server-absence.json").write_text("{}\n", encoding="utf-8")
    assert validate_absence_artifact(tmp_path, "server", target, absence) is False


def guard_event(sequence: int, event: str, nonce: str, previous: str, payload: dict[str, object]) -> dict[str, object]:
    record: dict[str, object] = {
        "sequence": sequence,
        "event": event,
        "nonce": nonce,
        "wall_time": f"2026-09-24T11:00:0{sequence}Z",
        "previous_hash": previous,
        "payload": payload,
    }
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    record["event_hash"] = hashlib.sha256(encoded).hexdigest()
    return record


def test_guard_journal_verifies_file_hash_chain_and_arm_root(tmp_path: Path) -> None:
    manifest = manifest_fixture()
    receipt = manifest["guards"]["server"]  # type: ignore[index]
    assert isinstance(receipt, dict)
    first = guard_event(1, "armed", str(receipt["nonce"]), "0" * 64, {"instance_id": None, "label": receipt["label"]})
    receipt["root_hash"] = first["event_hash"]
    second = guard_event(2, "heartbeat", str(receipt["nonce"]), str(first["event_hash"]), {})
    raw = "".join(json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n" for item in (first, second)).encode()
    path = tmp_path / "server-azure-guard-journal.ndjson"
    path.write_bytes(raw)
    finalization = {
        "root_hash": second["event_hash"],
        "journal_artifact": path.name,
        "journal_sha256": hashlib.sha256(raw).hexdigest(),
    }

    assert validate_guard_journal(tmp_path, role="server", receipt=receipt, finalization=finalization)

    tampered = copy.deepcopy(finalization)
    tampered["root_hash"] = "f" * 64
    assert validate_guard_journal(tmp_path, role="server", receipt=receipt, finalization=tampered) is False


def test_mere_finalization_statuses_are_not_artifact_proof(tmp_path: Path) -> None:
    target = manifest_fixture()["server"]
    assert isinstance(target, dict)

    assert validate_absence_artifact(tmp_path, "server", target, {"status": "THREE_READS_CONFIRMED"}) is False
    assert validate_billing_artifact(tmp_path, "server", target, {"status": "AUTHORITATIVE_INVOICE_CAPTURED"}) is False


def test_two_node_startup_counts_attempts_not_instances_and_labels_subset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    both = tmp_path / "artifacts/runs/two-node-a"
    none = tmp_path / "artifacts/runs/two-node-b"
    latest = tmp_path / "artifacts/live-two-node-latest"
    for run_dir in (both, none, latest):
        run_dir.mkdir(parents=True)
    instance = {"instance": {"instance_id": 1}}
    (both / "run-manifest.json").write_text(json.dumps({"server": instance, "worker": instance, "status": "failed"}))
    (both / "server-provider-status.ndjson").write_text(json.dumps({"actual_status": "running"}) + "\n")
    (none / "run-manifest.json").write_text(json.dumps({"status": "failed"}))
    (latest / "run-manifest.json").write_text(json.dumps({"server": instance, "worker": instance, "report": {"attempted": True}}))
    monkeypatch.setattr(evidence_pack, "ROOT", tmp_path)

    data, svg = evidence_pack._two_node_startup()

    assert data["counts"] == {"created": 1, "running": 1, "completed": 0}
    assert [attempt["run"] for attempt in data["attempts"]] == ["two-node-a", "two-node-b"]
    assert data["count_unit"] == "attempts"
    assert "live-two-node-* attempts excluded" in data["scope"]
    assert "2 selected historical attempts (subset)" in svg
    assert "counts attempts, not instances" in svg
    assert "Attempts with ≥1 instance" in svg
    assert "Instances created" not in svg


def _write_measured_arm(arm_dir: Path, precision: str, status: str = "completed") -> None:
    arm_dir.mkdir(parents=True)
    cell = {
        "cell": "short-c8", "concurrency": 8, "input_tokens": 1024, "output_tokens": 256, "output_tok_per_s": 193.1,
        "ttft_p50_s": 3.89, "ttft_p99_s": 4.29, "tpot_p50_s": 0.026, "tpot_p99_s": 0.040, "e2e_p50_s": 10.6,
        "successes": 64,
    }
    run = {"status": status, "precision": precision, "model_id": "Qwen/Qwen3.8-27B", "tensor_parallel": 8, "readiness_s": 475.0, "cells": [cell]}
    (arm_dir / "run.json").write_text(json.dumps(run), encoding="utf-8")
    (arm_dir / "metrics.ndjson").write_text('{"waiting": 0.0, "kv_cache_usage": 0.02}\n{"error": "http 503"}\n', encoding="utf-8")
    (arm_dir / "vllm.log").write_text("Model loading took 6.69 GiB memory\nAvailable KV cache memory: 12.65 GiB\nGPU KV cache size: 499,230 tokens\n", encoding="utf-8")
    lines = [f"{hashlib.sha256((arm_dir / name).read_bytes()).hexdigest()}  {name}\n" for name in ("metrics.ndjson", "run.json", "vllm.log")]
    (arm_dir / "SHA256SUMS").write_text("".join(lines), encoding="utf-8")


def test_measured_results_read_verified_precision_arms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evidence_pack, "ROOT", tmp_path)
    _write_measured_arm(tmp_path / "single", "bf16")
    _write_measured_arm(tmp_path / "arms/bf16", "bf16")
    _write_measured_arm(tmp_path / "arms/fp8", "fp8")
    _write_measured_arm(tmp_path / "arms/fp8-kv", "fp8-kv", status="failed")
    monkeypatch.setattr(evidence_pack, "MEASURED_RUNS", {"single": ("single",), "arms": ("arms",), "missing": ("nope",)})

    results = evidence_pack._measured_results()

    assert results["missing"] == {"status": "unavailable", "paths": ["nope"], "arms": {}, "skipped_incomplete_arms": []}
    assert results["arms"]["skipped_incomplete_arms"] == ["arms/fp8-kv"]
    assert list(results["single"]["arms"]) == ["bf16"]
    assert sorted(results["arms"]["arms"]) == ["bf16", "fp8"]
    arm = results["arms"]["arms"]["fp8"]
    assert arm["max_waiting"] == 0.0 and arm["max_kv_cache_usage"] == 0.02
    assert "successes" not in arm["cells"][0] and arm["cells"][0]["cell"] == "short-c8"
    assert (arm["weights_gib_per_gpu"], arm["kv_cache_tokens"], arm["kv_cache_memory_gib_per_gpu"]) == (6.69, 499230, 12.65)


def test_measured_results_reject_tampered_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evidence_pack, "ROOT", tmp_path)
    _write_measured_arm(tmp_path / "run", "bf16")
    (tmp_path / "run/run.json").write_text('{"status": "completed"}', encoding="utf-8")
    monkeypatch.setattr(evidence_pack, "MEASURED_RUNS", {"run": ("run",)})

    with pytest.raises(SystemExit, match="checksum mismatch"):
        evidence_pack._measured_results()
