from __future__ import annotations

import json
from pathlib import Path

import pytest

from srecon26_poc.live_report import LiveBrowserReportAdapter, LiveBrowserReportError, create_adapter
from srecon26_poc.reporting import FaultRecord


def _instance(label: str) -> str:
    return json.dumps({"id": 417, "gpu_name": "RTX 3090", "num_gpus": 1, "gpu_ram": 24, "compute_cap": 860, "machine_id": 99, "dph_total": 0.3, "label": label})


def test_live_report_preflight_requires_exact_provider_and_visible_desktop_target(tmp_path: Path) -> None:
    label = "run--nonce-nonce_12345678"
    browser = tmp_path / "browse"
    browser.write_text("#!/bin/sh\n", encoding="utf-8")
    browser.chmod(0o700)
    calls: list[list[str]] = []

    def runner(arguments, timeout: int) -> str:
        del timeout
        command = list(arguments)
        calls.append(command)
        if "show" in command and "instances" in command:
            return _instance(label)
        if command[1] == "text":
            return f"Instances 417 {label} Report"
        return ""

    adapter = LiveBrowserReportAdapter(browser, "vastai", tmp_path / "evidence", runner=runner)
    adapter.preflight_exact_instance(417, label)
    assert any(command[1:3] == ["goto", "https://cloud.vast.ai/instances/"] for command in calls)


def test_live_report_refuses_unauthenticated_page_before_click(tmp_path: Path) -> None:
    label = "run--nonce-nonce_12345678"
    browser = tmp_path / "browse"
    browser.write_text("#!/bin/sh\n", encoding="utf-8")
    browser.chmod(0o700)

    def runner(arguments, timeout: int) -> str:
        del timeout
        command = list(arguments)
        if "show" in command and "instances" in command:
            return _instance(label)
        return "Login"

    adapter = LiveBrowserReportAdapter(browser, "vastai", tmp_path / "evidence", runner=runner)
    with pytest.raises(LiveBrowserReportError, match="authenticated"):
        adapter.preflight_exact_instance(417, label)


def test_live_report_session_preflight_rejects_login_redirect_before_paid_create(tmp_path: Path) -> None:
    browser = tmp_path / "browse"
    browser.write_text("#!/bin/sh\n", encoding="utf-8")
    browser.chmod(0o700)

    def runner(arguments, timeout: int) -> str:
        del timeout
        command = list(arguments)
        if command[1] == "url":
            return "https://cloud.vast.ai/create/"
        if command[1] == "text":
            return "Login"
        return ""

    adapter = LiveBrowserReportAdapter(browser, "vastai", tmp_path / "evidence", runner=runner)
    with pytest.raises(LiveBrowserReportError, match="not authenticated"):
        adapter.preflight_authenticated_session()


@pytest.mark.parametrize(
    ("url", "text"),
    [
        ("https://cloud.vast.ai/instances-invalid", "Instances"),
        ("https://cloud.vast.ai/instances/", "Please sign in"),
        ("https://cloud.vast.ai/instances/", "LOG IN"),
    ],
)
def test_live_report_session_preflight_rejects_wrong_route_and_auth_markers(tmp_path: Path, url: str, text: str) -> None:
    browser = tmp_path / "browse"
    browser.write_text("#!/bin/sh\n", encoding="utf-8")
    browser.chmod(0o700)

    def runner(arguments, timeout: int) -> str:
        del timeout
        command = list(arguments)
        return url if command[1] == "url" else text if command[1] == "text" else ""

    adapter = LiveBrowserReportAdapter(browser, "vastai", tmp_path / "evidence", runner=runner)
    with pytest.raises(LiveBrowserReportError, match="not authenticated"):
        adapter.preflight_authenticated_session()


def test_live_report_session_preflight_accepts_exact_authenticated_instances_page(tmp_path: Path) -> None:
    browser = tmp_path / "browse"
    browser.write_text("#!/bin/sh\n", encoding="utf-8")
    browser.chmod(0o700)

    def runner(arguments, timeout: int) -> str:
        del timeout
        command = list(arguments)
        return "https://cloud.vast.ai/instances/" if command[1] == "url" else "Instances No running instances" if command[1] == "text" else ""

    LiveBrowserReportAdapter(browser, "vastai", tmp_path / "evidence", runner=runner).preflight_authenticated_session()


def test_live_report_factory_preflights_authenticated_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    browser = tmp_path / "browse"
    browser.write_text("#!/bin/sh\n", encoding="utf-8")
    browser.chmod(0o700)
    called: list[bool] = []
    monkeypatch.setenv("SRECON26_BROWSER_BIN", str(browser))
    monkeypatch.setenv("SRECON26_VAST_CLI", "vastai")
    monkeypatch.setenv("SRECON26_REPORT_EVIDENCE_DIR", str(tmp_path / "evidence"))
    monkeypatch.setattr(LiveBrowserReportAdapter, "preflight_authenticated_session", lambda self: called.append(True))

    adapter = create_adapter()

    assert isinstance(adapter, LiveBrowserReportAdapter)
    assert called == [True]


def test_live_report_click_script_binds_id_and_nonce_label(tmp_path: Path) -> None:
    browser = tmp_path / "browse"
    browser.write_text("#!/bin/sh\n", encoding="utf-8")
    browser.chmod(0o700)
    calls: list[list[str]] = []

    def runner(arguments, timeout: int) -> str:
        del timeout
        calls.append(list(arguments))
        return "report submitted"

    adapter = LiveBrowserReportAdapter(browser, "vastai", tmp_path / "evidence", runner=runner)
    fault = FaultRecord(417, "run--nonce-nonce_12345678", "nonce_12345678", "confirmed mismatch")
    assert adapter.submit(fault) is True
    script = next(command[2] for command in calls if command[1] == "js")
    assert "417" in script and fault.label in script and "controls.length !== 1" in script
