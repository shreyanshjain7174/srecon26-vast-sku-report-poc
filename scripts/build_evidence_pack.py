#!/usr/bin/env python3
"""Create reproducible, claim-bounded visuals for the lightning-talk design handoff."""
from __future__ import annotations

import json
import math
from pathlib import Path
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "artifacts" / "runs" / "inference-infer20260923184725" / "manual-benchmark"
OUT = ROOT / "artifacts" / "evidence-pack"
VERDICT = Path("/var/tmp/srecon26-verdict.json")

COLORS = {"ink": "#152238", "blue": "#155EEF", "orange": "#D97600", "green": "#0C8B6B", "muted": "#52616B", "grid": "#D8DEE4"}


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


def write_summary(requests: dict[str, list[dict[str, object]]], verdict: dict[str, object]) -> None:
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
        "boundaries": [
            "GPU measurements are standalone vLLM serving data from one rented VM, not a Kubernetes HPA experiment.",
            "Local HPA proof validates independent signal plumbing; the KV source is synthetic.",
            "No CPU-only versus queue/KV-aware HPA A/B result exists yet.",
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
    write_summary(requests, verdict)


if __name__ == "__main__":
    main()
