import json

from scripts.prepaid_gate import evaluate


def test_gate_reports_every_missing_condition_and_is_read_only():
    verdict = evaluate({"tests": True, "semgrep": False, "secret_checks": True, "inventory": "empty", "budget_headroom": "5.00", "guard_attestation": False, "report_fixture": False})
    assert not verdict["eligible"]
    assert verdict["missing"] == ["semgrep", "guard_attestation", "report_fixture"]


def test_gate_accepts_only_complete_prior_evidence():
    verdict = evaluate({"tests": True, "semgrep": True, "secret_checks": True, "inventory": "empty", "budget_headroom": "0.01", "guard_attestation": True, "report_fixture": True})
    assert verdict == {"eligible": False, "missing": ["authorization_not_granted"]}
