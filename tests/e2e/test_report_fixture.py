from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_playwright_fixture_records_exact_target_receipt_and_redacted_screenshots(tmp_path) -> None:
    receipt_path = tmp_path / "receipt.json"
    command = [
        "node",
        "browser/report-fixture/fixture_runner.mjs",
        "--output-dir",
        str(tmp_path),
        "--instance-id",
        "417",
        "--label",
        "srecon26-run-report",
        "--nonce",
        "nonce-0123456789abcdef",
    ]
    completed = subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "SUBMITTED"
    assert receipt["instance_id"] == 417
    assert receipt["provider_request_count"] == 0
    assert (tmp_path / "before.png").is_file()
    assert (tmp_path / "after.png").is_file()


def test_playwright_fixture_refuses_wrong_exact_target_without_submitting(tmp_path) -> None:
    completed = subprocess.run(
        [
            "node",
            "browser/report-fixture/fixture_runner.mjs",
            "--output-dir",
            str(tmp_path),
            "--instance-id",
            "999",
            "--label",
            "srecon26-run-report",
            "--nonce",
            "nonce-0123456789abcdef",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "target mismatch" in completed.stderr
    assert not (tmp_path / "receipt.json").exists()
