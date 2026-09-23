from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

from srecon26_poc.external_report import (
    ExternalDesktopReportAdapter,
    ExternalReportError,
    ExternalReportTimeout,
    RESPONSE_SCHEMA,
    create_adapter,
)
from srecon26_poc.reporting import FaultRecord, ReportReceipt


RUN_ID = "inference-run-01"
NONCE = "nonce_12345678"
LABEL = "srecon26-inference--nonce-nonce_12345678"
FAULT = FaultRecord(417, LABEL, NONCE, "confirmed provider contract mismatch")


def _atomic_json(path: Path, payload: object) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(temporary, path)


class Responder:
    def __init__(self, evidence_dir: Path, *, mutate=None, stale: bool = False) -> None:
        self.evidence_dir = evidence_dir
        self.mutate = mutate
        self.stale = stale
        self.requests: list[dict[str, object]] = []

    def __call__(self, _delay: float) -> None:
        request_paths = sorted(self.evidence_dir.glob(".external-report-*/*.request.json"))
        if not request_paths:
            return
        request_path = request_paths[-1]
        response_path = request_path.with_suffix(".response.json")
        if response_path.exists():
            return
        request = json.loads(request_path.read_text())
        self.requests.append(request)
        response: dict[str, object] = {
            "schema": RESPONSE_SCHEMA,
            "operation": request["operation"],
            "request_id": request["request_id"],
            "run_id": request["run_id"],
            "nonce": request["nonce"],
        }
        if request["operation"] == "preflight-authenticated-session":
            response["authenticated"] = True
        elif request["operation"] == "preflight-exact-instance":
            response.update({"instance_id": request["instance_id"], "label": request["label"], "exact_target": True})
        elif request["operation"] in {"capture-before", "capture-after"}:
            image = self.evidence_dir / "screenshots" / f"{request['operation']}.png"
            image.parent.mkdir(exist_ok=True)
            image.write_bytes(b"redacted-image")
            response["image_path"] = str(image.relative_to(self.evidence_dir))
            if request["operation"] == "capture-after":
                response["confirmed"] = request["confirmed"]
        elif request["operation"] == "submit":
            response.update({"fault": request["fault"], "confirmed": True})
        else:
            raise AssertionError(f"unexpected operation {request['operation']}")
        if self.mutate:
            self.mutate(response, request)
        _atomic_json(response_path, response)
        if self.stale:
            old = time.time() - 120
            os.utime(response_path, (old, old))


def _adapter(tmp_path: Path, responder: Responder, **kwargs) -> ExternalDesktopReportAdapter:
    return ExternalDesktopReportAdapter(tmp_path / "evidence", RUN_ID, NONCE, timeout_seconds=2, poll_interval_seconds=0.001, sleeper=responder, **kwargs)


def test_handshake_sequences_exact_nonce_bound_requests_without_browser_automation(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    responder = Responder(evidence_dir)
    adapter = _adapter(tmp_path, responder)

    adapter.preflight_authenticated_session()
    adapter.preflight_exact_instance(FAULT.instance_id, FAULT.label)
    before = adapter.capture_before(FAULT)
    assert adapter.submit(FAULT) is True
    after = adapter.capture_after(ReportReceipt(True, before, None))

    assert before.read_bytes() == b"redacted-image"
    assert after.read_bytes() == b"redacted-image"
    assert [request["operation"] for request in responder.requests] == [
        "preflight-authenticated-session",
        "preflight-exact-instance",
        "capture-before",
        "submit",
        "capture-after",
    ]
    assert all(request["run_id"] == RUN_ID and request["nonce"] == NONCE for request in responder.requests)
    assert len({request["request_id"] for request in responder.requests}) == 5
    assert stat.S_IMODE(adapter.handshake_dir.stat().st_mode) == 0o700
    assert not list(adapter.handshake_dir.glob("*.tmp"))


def test_handshake_rejects_response_for_another_nonce_before_target_is_accepted(tmp_path: Path) -> None:
    responder = Responder(tmp_path / "evidence", mutate=lambda response, _request: response.update({"nonce": "nonce_other_123456"}))
    adapter = _adapter(tmp_path, responder)

    with pytest.raises(ExternalReportError, match="exact run, nonce, and request"):
        adapter.preflight_authenticated_session()


def test_capture_rejects_existing_image_path_outside_evidence_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")

    def escape(response: dict[str, object], request: dict[str, object]) -> None:
        if request["operation"] == "capture-before":
            response["image_path"] = str(outside)

    responder = Responder(tmp_path / "evidence", mutate=escape)
    adapter = _adapter(tmp_path, responder)
    adapter.preflight_authenticated_session()
    adapter.preflight_exact_instance(FAULT.instance_id, FAULT.label)

    with pytest.raises(ExternalReportError, match="escapes evidence directory"):
        adapter.capture_before(FAULT)


def test_submit_rejects_confirmation_for_a_different_fault(tmp_path: Path) -> None:
    def wrong_fault(response: dict[str, object], request: dict[str, object]) -> None:
        if request["operation"] == "submit":
            response["fault"] = {**response["fault"], "instance_id": 999}  # type: ignore[arg-type]

    responder = Responder(tmp_path / "evidence", mutate=wrong_fault)
    adapter = _adapter(tmp_path, responder)
    adapter.preflight_authenticated_session()
    adapter.preflight_exact_instance(FAULT.instance_id, FAULT.label)
    adapter.capture_before(FAULT)

    with pytest.raises(ExternalReportError, match="exact fault"):
        adapter.submit(FAULT)


def test_after_capture_requires_receipt_from_completed_submit(tmp_path: Path) -> None:
    responder = Responder(tmp_path / "evidence")
    adapter = _adapter(tmp_path, responder)
    adapter.preflight_authenticated_session()
    adapter.preflight_exact_instance(FAULT.instance_id, FAULT.label)
    before = adapter.capture_before(FAULT)

    with pytest.raises(ExternalReportError, match="submission"):
        adapter.capture_after(ReportReceipt(True, before, None))

    assert adapter.submit(FAULT) is True
    with pytest.raises(ExternalReportError, match="does not match"):
        adapter.capture_after(ReportReceipt(False, before, None))


def test_handshake_rejects_stale_response(tmp_path: Path) -> None:
    responder = Responder(tmp_path / "evidence", stale=True)
    adapter = _adapter(tmp_path, responder)

    with pytest.raises(ExternalReportError, match="stale"):
        adapter.preflight_authenticated_session()


def test_handshake_timeout_is_bounded(tmp_path: Path) -> None:
    ticks = iter((0.0, 0.0, 1.0))
    adapter = ExternalDesktopReportAdapter(
        tmp_path / "evidence",
        RUN_ID,
        NONCE,
        timeout_seconds=1,
        poll_interval_seconds=0.001,
        sleeper=lambda _delay: None,
        monotonic=lambda: next(ticks),
    )

    with pytest.raises(ExternalReportTimeout, match="within 1 seconds"):
        adapter.preflight_authenticated_session()


def test_factory_reads_only_explicit_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SRECON26_REPORT_EVIDENCE_DIR", str(tmp_path / "evidence"))
    monkeypatch.setenv("SRECON26_EXTERNAL_REPORT_RUN_ID", RUN_ID)
    monkeypatch.setenv("SRECON26_EXTERNAL_REPORT_NONCE", NONCE)
    monkeypatch.setenv("SRECON26_EXTERNAL_REPORT_TIMEOUT_SECONDS", "7")

    adapter = create_adapter()

    assert adapter.timeout_seconds == 7
    assert adapter.run_id == RUN_ID
    assert adapter.nonce == NONCE
