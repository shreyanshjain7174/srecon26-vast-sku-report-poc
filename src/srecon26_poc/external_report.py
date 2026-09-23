"""Nonce-bound file handshake for an externally operated desktop report flow.

This adapter intentionally has no browser, subprocess, credential, or network
capability.  A separate desktop operator reads requests and atomically writes
the matching responses after visually checking the already-authenticated UI.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Callable, Mapping

from .reporting import FaultRecord, ReportReceipt


MAX_TIMEOUT_SECONDS = 60
DEFAULT_TIMEOUT_SECONDS = 20
RESPONSE_SCHEMA = "srecon26-external-report-response/v1"
REQUEST_SCHEMA = "srecon26-external-report-request/v1"


class ExternalReportError(ValueError):
    """The external desktop response cannot prove the exact requested action."""


class ExternalReportTimeout(TimeoutError):
    """The independently operated desktop did not answer within the cap."""


def _valid_token(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{8,128}", value))


class ExternalDesktopReportAdapter:
    """Wait for exact responses from an external, manually controlled desktop.

    The private handshake directory is scoped to one run and nonce.  Every
    response must echo a fresh, per-request token as well as the run/nonce,
    so a stale response from another operation cannot authorize a report.
    """

    def __init__(
        self,
        evidence_dir: Path,
        run_id: str,
        nonce: str,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        poll_interval_seconds: float = 0.1,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if not _valid_token(run_id) or not _valid_token(nonce):
            raise ExternalReportError("run id and nonce must be URL-safe tokens")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise ExternalReportError(f"timeout must be an integer from 1 to {MAX_TIMEOUT_SECONDS} seconds")
        if poll_interval_seconds <= 0 or poll_interval_seconds > 1:
            raise ExternalReportError("poll interval must be greater than zero and no more than one second")
        self.evidence_dir = Path(evidence_dir).expanduser().resolve()
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.nonce = nonce
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._sleep = sleeper
        self._monotonic = monotonic
        self._wall_time_ns = wall_time_ns
        digest = hashlib.sha256(f"{run_id}\0{nonce}".encode()).hexdigest()[:20]
        self.handshake_dir = self.evidence_dir / f".external-report-{digest}"
        self.handshake_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.handshake_dir, 0o700)
        if self.handshake_dir.resolve().parent != self.evidence_dir:
            raise ExternalReportError("private handshake directory escapes evidence directory")
        self._sequence = 0
        self._session_ready = False
        self._target: tuple[int, str] | None = None
        self._fault: FaultRecord | None = None
        self._before_path: Path | None = None
        self._submit_confirmed: bool | None = None

    def _atomic_write(self, path: Path, payload: Mapping[str, object]) -> None:
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")) .encode("utf-8") + b"\n"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise ExternalReportError("atomic request write made no progress")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
        directory_fd = os.open(self.handshake_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _path_from_response(self, response: Mapping[str, object], field: str) -> Path:
        value = response.get(field)
        if not isinstance(value, str) or not value:
            raise ExternalReportError(f"external response lacks {field}")
        candidate = Path(value)
        resolved = candidate.resolve(strict=True) if candidate.is_absolute() else (self.evidence_dir / candidate).resolve(strict=True)
        if self.evidence_dir not in resolved.parents:
            raise ExternalReportError("external image path is missing or escapes evidence directory")
        if resolved.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ExternalReportError("external image path is not an image")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(resolved, flags)
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or details.st_size <= 0:
                raise ExternalReportError("external image is not a non-empty regular file")
        finally:
            os.close(descriptor)
        return resolved

    def _response(self, operation: str, request: Mapping[str, object], request_path: Path) -> Mapping[str, object]:
        response_path = request_path.with_suffix(".response.json")
        deadline = self._monotonic() + self.timeout_seconds
        request_mtime_ns = request_path.stat().st_mtime_ns
        while self._monotonic() < deadline:
            try:
                response_mtime_ns = response_path.stat().st_mtime_ns
                age_ns = self._wall_time_ns() - response_mtime_ns
                if response_mtime_ns < request_mtime_ns or age_ns > self.timeout_seconds * 1_000_000_000 or age_ns < -1_000_000_000:
                    raise ExternalReportError("external response is stale or has an invalid timestamp")
                payload = json.loads(response_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                self._sleep(self.poll_interval_seconds)
                continue
            except json.JSONDecodeError:
                # An external operator must publish by atomic rename.  Ignore a
                # torn/in-progress file until the bounded deadline rather than
                # accepting an incomplete response.
                self._sleep(self.poll_interval_seconds)
                continue
            except OSError as error:
                raise ExternalReportError("external response cannot be read safely") from error
            if not isinstance(payload, Mapping):
                raise ExternalReportError("external response is not a JSON object")
            expected = {
                "schema": RESPONSE_SCHEMA,
                "operation": operation,
                "request_id": request["request_id"],
                "run_id": self.run_id,
                "nonce": self.nonce,
            }
            if any(payload.get(key) != value for key, value in expected.items()):
                raise ExternalReportError("external response is not bound to this exact run, nonce, and request")
            return payload
        raise ExternalReportTimeout(f"external desktop did not answer {operation} within {self.timeout_seconds} seconds")

    def _request(self, operation: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        self._sequence += 1
        request_id = secrets.token_urlsafe(24)
        request = {
            "schema": REQUEST_SCHEMA,
            "operation": operation,
            "request_id": request_id,
            "run_id": self.run_id,
            "nonce": self.nonce,
            **dict(payload),
        }
        request_path = self.handshake_dir / f"{self._sequence:02d}-{operation}.request.json"
        self._atomic_write(request_path, request)
        return self._response(operation, request, request_path)

    def _require_fault(self, fault: FaultRecord) -> None:
        if fault.nonce != self.nonce or fault.instance_id <= 0 or not fault.label or not fault.description:
            raise ExternalReportError("fault lacks exact nonce-bound report identity")
        if self._target != (fault.instance_id, fault.label):
            raise ExternalReportError("fault does not match the preflighted exact instance")

    def preflight_authenticated_session(self) -> None:
        response = self._request("preflight-authenticated-session", {})
        if response.get("authenticated") is not True:
            raise ExternalReportError("external desktop did not prove an authenticated session")
        self._session_ready = True

    def preflight_exact_instance(self, instance_id: int, label: str) -> None:
        if not self._session_ready:
            raise ExternalReportError("authenticated-session preflight is required before target preflight")
        if isinstance(instance_id, bool) or not isinstance(instance_id, int) or instance_id <= 0 or not label:
            raise ExternalReportError("exact report target is invalid")
        response = self._request("preflight-exact-instance", {"instance_id": instance_id, "label": label})
        if response.get("instance_id") != instance_id or response.get("label") != label or response.get("exact_target") is not True:
            raise ExternalReportError("external desktop did not prove the exact report target")
        self._target = (instance_id, label)

    def capture_before(self, fault: FaultRecord) -> Path:
        self._require_fault(fault)
        response = self._request("capture-before", {"fault": {"instance_id": fault.instance_id, "label": fault.label, "nonce": fault.nonce, "description": fault.description}})
        self._fault = fault
        self._before_path = self._path_from_response(response, "image_path")
        return self._before_path

    def submit(self, fault: FaultRecord) -> bool:
        self._require_fault(fault)
        if self._fault != fault:
            raise ExternalReportError("before-report capture is required before submission")
        response = self._request("submit", {"fault": {"instance_id": fault.instance_id, "label": fault.label, "nonce": fault.nonce, "description": fault.description}})
        returned_fault = response.get("fault")
        expected_fault = {"instance_id": fault.instance_id, "label": fault.label, "nonce": fault.nonce, "description": fault.description}
        if returned_fault != expected_fault or not isinstance(response.get("confirmed"), bool):
            raise ExternalReportError("external response lacks an exact fault and boolean report confirmation")
        self._submit_confirmed = response["confirmed"]
        return self._submit_confirmed

    def capture_after(self, receipt: ReportReceipt) -> Path:
        if self._fault is None or self._before_path is None or self._submit_confirmed is None:
            raise ExternalReportError("completed before-capture and submission are required before after-report capture")
        if receipt.before_path.resolve() != self._before_path.resolve() or receipt.confirmed is not self._submit_confirmed or receipt.after_path is not None:
            raise ExternalReportError("after-report receipt does not match the completed submission")
        response = self._request("capture-after", {"confirmed": receipt.confirmed})
        if response.get("confirmed") is not receipt.confirmed:
            raise ExternalReportError("after-report response confirmation differs from receipt")
        return self._path_from_response(response, "image_path")


def create_adapter() -> ExternalDesktopReportAdapter:
    """Construct the browser-free adapter from explicit operator environment."""

    def required(name: str) -> str:
        value = os.environ.get(name)
        if not value:
            raise ExternalReportError(f"{name} is required")
        return value

    raw_timeout = os.environ.get("SRECON26_EXTERNAL_REPORT_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
    try:
        timeout = int(raw_timeout)
    except ValueError as error:
        raise ExternalReportError("SRECON26_EXTERNAL_REPORT_TIMEOUT_SECONDS must be an integer") from error
    return ExternalDesktopReportAdapter(
        Path(required("SRECON26_REPORT_EVIDENCE_DIR")),
        required("SRECON26_EXTERNAL_REPORT_RUN_ID"),
        required("SRECON26_EXTERNAL_REPORT_NONCE"),
        timeout_seconds=timeout,
    )
