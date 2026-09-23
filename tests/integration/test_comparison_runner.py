import json
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
RUNNER = REPO / "scripts" / "run_comparison.py"
FIXTURE = REPO / "tests" / "fixtures" / "comparison" / "complete-real-gpu.json"


def test_fixture_runner_has_no_provider_dispatch_and_records_provenance_requirement(tmp_path):
    output = tmp_path / "comparison.json"
    completed = subprocess.run(
        [sys.executable, str(RUNNER), "--fixture", str(FIXTURE), "--json", str(output)],
        check=False,
        cwd=REPO,
        text=True,
        capture_output=True,
    )

    assert completed.returncode == 0
    payload = json.loads(output.read_text())
    assert payload["mode"] == "fixture-replay"
    assert payload["provider_dispatch"] == "disabled"
    assert payload["real_gpu_provenance_required"] is True
    assert payload["comparative_claim_emitted"] is False
    assert payload["eligibility"]["eligible"] is True
    assert payload["analysis"]["classification"] == "INVALID"


def test_runner_requires_explicit_fixture_and_has_no_live_default(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(RUNNER), "--json", str(tmp_path / "unused.json")],
        check=False,
        cwd=REPO,
        text=True,
        capture_output=True,
    )

    assert completed.returncode == 2
    assert "--fixture" in completed.stderr
