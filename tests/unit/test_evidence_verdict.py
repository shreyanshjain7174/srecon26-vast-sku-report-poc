from __future__ import annotations

import json
import shutil
from pathlib import Path

from srecon26_poc.evidence_verdict import EXPLORATORY, INVALID, VALID, analyze_selected_evidence, write_verdict


REPOSITORY_ROOT = Path(__file__).parents[2]


def test_selected_evidence_keeps_local_proof_and_denies_gpu_claims() -> None:
    verdict = analyze_selected_evidence(REPOSITORY_ROOT)

    assert verdict["verdict"] == VALID
    assert verdict["claims"]["local_hpa_signal_plumbing"] == {
        "verdict": VALID,
        "allowed": True,
        "failing_checks": [],
        "provenance": "local-synthetic",
    }
    assert verdict["claims"]["failed_safe_lifecycle"]["allowed"] is True
    assert verdict["claims"]["real_gpu_metric_path"]["verdict"] == EXPLORATORY
    assert verdict["claims"]["real_gpu_metric_path"]["allowed"] is False
    assert verdict["claims"]["paired_hpa_performance"]["allowed"] is False
    arms = {item["arm"]: item for item in verdict["local_evidence"]["arms"]}
    assert arms["queue"]["observations"]["negative_sample_count"] == 7
    assert arms["queue"]["observations"]["negative_duration_seconds"] >= 90
    assert arms["queue"]["observations"]["negative_cpu_max_millicores"] == 16.456944


def test_rewritten_local_root_is_invalid(tmp_path: Path) -> None:
    evidence_root = tmp_path / "local-evidence"
    for run_id in (
        "phase1-live-202609d03531790135589z",
        "phase1-live-202609d04271790137679z",
        "phase1-live-202609d04031790136227z",
    ):
        shutil.copytree(REPOSITORY_ROOT / "local-evidence" / run_id, evidence_root / run_id)
    anchor_path = tmp_path / ".planning/phases/01-safety-foundation-and-local-evidence/01-LOCAL-EVIDENCE-ANCHOR.json"
    anchor_path.parent.mkdir(parents=True)
    shutil.copy2(REPOSITORY_ROOT / ".planning/phases/01-safety-foundation-and-local-evidence/01-LOCAL-EVIDENCE-ANCHOR.json", anchor_path)
    target = evidence_root / "phase1-live-202609d03531790135589z/cpu"
    (target / "ROOT-HASH.txt").write_text("0" * 64 + "\n", encoding="utf-8")

    verdict = analyze_selected_evidence(tmp_path, local_anchor=anchor_path)

    assert verdict["verdict"] == INVALID
    assert "local.cpu.anchor_root_mismatch" in verdict["failing_checks"]


def test_verdict_output_is_machine_readable(tmp_path: Path) -> None:
    output = tmp_path / "verdict.json"
    verdict = analyze_selected_evidence(REPOSITORY_ROOT)
    write_verdict(output, verdict)

    assert json.loads(output.read_text()) == verdict


def test_mutable_anchor_cannot_replace_pinned_external_receipt(tmp_path: Path) -> None:
    anchor = json.loads(
        (REPOSITORY_ROOT / ".planning/phases/01-safety-foundation-and-local-evidence/01-LOCAL-EVIDENCE-ANCHOR.json").read_text()
    )
    anchor["external_guard_receipt"]["required_root_hashes"]["cpu"] = "0" * 64
    replacement = tmp_path / "replacement-anchor.json"
    replacement.write_text(json.dumps(anchor), encoding="utf-8")

    verdict = analyze_selected_evidence(REPOSITORY_ROOT, local_anchor=replacement)

    assert verdict["verdict"] == INVALID
    assert "local.anchor_not_pinned" in verdict["failing_checks"]
