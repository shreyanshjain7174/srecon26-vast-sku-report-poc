#!/usr/bin/env python3
"""Create reproducible, claim-bounded visuals for the lightning-talk design handoff."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping
from uuid import UUID
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "artifacts" / "runs" / "inference-infer20260923184725" / "manual-benchmark"
OUT = ROOT / "artifacts" / "evidence-pack"
VERDICT = Path("/var/tmp/srecon26-verdict.json")

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


def gpu_latency_chart(requests: dict[str, list[dict[str, object]]]) -> None:
    labels = ["c4 p50", "c4 p95", "c32 p50", "c32 p95"]
    ttft = []
    e2e = []
    for arm in ("c4", "c32"):
        ttft_values = [float(item["ttft_seconds"]) * 1000 for item in requests[arm]]
        e2e_values = [float(item["e2e_seconds"]) * 1000 for item in requests[arm]]
        ttft.extend((percentile(ttft_values, 0.5), percentile(ttft_values, 0.95)))
        e2e.extend((percentile(e2e_values, 0.5), percentile(e2e_values, 0.95)))
    (OUT / "gpu-latency-by-concurrency.svg").write_text(svg_chart("Measured vLLM latency on one rented RTX 4090", "Qwen2.5-1.5B, 128 completion tokens, streamed requests; c4 n=4, c32 n=32", [("TTFT", labels, ttft, COLORS["orange"], "milliseconds"), ("End-to-end latency", labels, e2e, COLORS["blue"], "milliseconds")]))


def hpa_signal_chart(verdict: dict[str, object]) -> None:
    evidence = verdict["local_evidence"]["arms"]
    arms = {item["arm"]: item["observations"] for item in evidence}
    labels = ["CPU", "Queue", "Synthetic KV"]
    values = [arms["cpu"]["scaled_cpu_millicores"], arms["queue"]["scaled_queue"], arms["kv"]["scaled_kv"]]
    units = ["millicores", "waiting requests", "occupancy"]
    panels = [(label, [label], [value], color, unit) for label, value, unit, color in zip(labels, values, units, (COLORS["blue"], COLORS["orange"], COLORS["green"]), strict=True)]
    (OUT / "local-hpa-signal-plumbing.svg").write_text(svg_chart("Local Kubernetes HPA signal plumbing", "Each isolated signal produced a 1 to 2 desired-and-ready replica transition. KV source was synthetic.", panels))


def two_node_startup_chart() -> dict[str, object]:
    """Summarize actual two-node provider attempts without inflating them to K8s results."""
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
            "template": manifest.get("vm_template", "ubuntu-cli"),
            "created": created,
            "guard_backend": manifest.get("guard_backend", "github-legacy"),
            "both_bound_arm_receipts": guards_armed,
            "guard_receipt_validation_errors": guard_errors,
            "both_guard_journals_hash_chain_verified": guard_journals_verified,
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
    (OUT / "two-node-canary-startup.svg").write_text(svg_chart(
        "Two-node Vast canary: provider startup evidence",
        "Counts show only exact run manifests. ‘Running’ is a provider state, not Kubernetes readiness.",
        [("Run outcomes", ["Instances created", "Provider running", "K8s completed"], [float(counts["created"]), float(counts["running"]), float(counts["completed"])], COLORS["blue"], "attempts")],
    ))
    return {"attempts": attempts, "counts": counts}


def write_summary(requests: dict[str, list[dict[str, object]]], verdict: dict[str, object], two_node: dict[str, object]) -> None:
    def stats(arm: str) -> dict[str, float | int]:
        values = requests[arm]
        field = lambda name: [float(item[name]) for item in values]
        return {
            "requests": len(values),
            "p50_ttft_ms": percentile([value * 1000 for value in field("ttft_seconds")], 0.5),
            "p95_ttft_ms": percentile([value * 1000 for value in field("ttft_seconds")], 0.95),
            "p50_e2e_ms": percentile([value * 1000 for value in field("e2e_seconds")], 0.5),
            "p95_e2e_ms": percentile([value * 1000 for value in field("e2e_seconds")], 0.95),
            "p50_tpot_ms": percentile([value * 1000 for value in field("tpot_seconds")], 0.5),
            "p95_tpot_ms": percentile([value * 1000 for value in field("tpot_seconds")], 0.95),
        }
    summary = {
        "schema": "srecon26-evidence-pack/v1",
        "gpu_measurements": {arm: stats(arm) for arm in ("c4", "c32")},
        "local_hpa_verdict": verdict["claims"]["local_hpa_signal_plumbing"],
        "two_node_canary": two_node,
        "boundaries": [
            "GPU measurements are standalone vLLM serving data from one rented VM, not a Kubernetes HPA experiment.",
            "Local HPA proof validates independent signal plumbing; the KV source is synthetic.",
            "No CPU-only versus queue/KV-aware HPA A/B result exists yet.",
            "Provider ‘running’ does not establish SSH, GPU, Kubernetes, vLLM, metrics, or HPA readiness.",
            "A two-node result is complete only when both bound guard receipts, raw workload outputs, exact billing, and three-read absence artifacts are retained.",
            "The website Report action is attempted only for a frozen exact-instance provider fault before normal teardown.",
        ],
    }
    (OUT / "evidence-summary.json").write_text(json.dumps(summary, indent=2) + "\n")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    if not VERDICT.is_file():
        raise SystemExit("run scripts/analyze_evidence.py --output /var/tmp/srecon26-verdict.json first")
    verdict = json.loads(VERDICT.read_text())
    requests = request_data()
    if len(requests["c4"]) != 4 or len(requests["c32"]) != 32:
        raise SystemExit("raw request evidence is incomplete")
    gpu_latency_chart(requests)
    hpa_signal_chart(verdict)
    two_node = two_node_startup_chart()
    write_summary(requests, verdict, two_node)


if __name__ == "__main__":
    main()
