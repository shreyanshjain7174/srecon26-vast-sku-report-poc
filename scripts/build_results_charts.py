#!/usr/bin/env python3
"""Render presentation-quality PNG charts from the selected, verified evidence.

This intentionally charts only values retained in the validation contract.  The
local KV signal is marked synthetic, and live GPU data is a limitation, not an
experimental outcome.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from srecon26_poc.evidence_verdict import analyze_selected_evidence, write_verdict


OUT = ROOT / "artifacts" / "presentation"
VERDICT = OUT / "verdict.json"
W, H = 1600, 900
BG, CARD, TEXT, MUTED = "171B22", "242A35", "F5F7FA", "B9C2CF"
TEAL, GOLD, CORAL, PURPLE, GRID = "24C4B8", "F6C85F", "FF6B6B", "A78BFA", "3B4554"


def load_gated_evidence() -> dict[str, object]:
    try:
        verdict = json.loads(VERDICT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"refusing charts without readable evidence verdict: {error}") from error
    claims = verdict.get("claims", {})
    required = {
        "local_hpa_signal_plumbing": ("VALID", True),
        "failed_safe_lifecycle": ("VALID", True),
        "real_gpu_metric_path": ("EXPLORATORY", False),
        "paired_hpa_performance": ("EXPLORATORY", False),
    }
    for name, (expected_verdict, expected_allowed) in required.items():
        claim = claims.get(name, {})
        if claim.get("verdict") != expected_verdict or claim.get("allowed") is not expected_allowed:
            raise SystemExit(f"refusing charts: claim gate {name} is not {expected_verdict}/{expected_allowed}")
    arms = verdict.get("local_evidence", {}).get("arms", [])
    by_arm = {item.get("arm"): item for item in arms if isinstance(item, dict)}
    if set(by_arm) != {"cpu", "queue", "kv"}:
        raise SystemExit("refusing charts: selected local arm observations are incomplete")
    return {"verdict": verdict, "arms": by_arm}


def color(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    )
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def canvas(title: str, subtitle: str) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (W, H), color(BG))
    draw = ImageDraw.Draw(image)
    draw.text((84, 58), title, font=font(44, True), fill=color(TEXT))
    draw.text((86, 122), subtitle, font=font(22), fill=color(MUTED))
    return image, draw


def rounded(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], fill: str, radius: int = 26) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=color(fill))


def write_centered(draw: ImageDraw.ImageDraw, text: str, box: tuple[int, int, int, int], size: int, fill: str, bold: bool = False) -> None:
    f = font(size, bold)
    left, top, right, bottom = box
    x0, y0, x1, y1 = draw.textbbox((0, 0), text, font=f)
    draw.text((left + (right - left - (x1 - x0)) / 2, top + (bottom - top - (y1 - y0)) / 2), text, font=f, fill=color(fill))


def save(image: Image.Image, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    image.save(OUT / name, optimize=True)


def local_signal_independence(arms: dict[str, object], provenance: str) -> None:
    image, draw = canvas(
        "Three independently verified local scaling signals",
        "Selected Kind/local runs — each arm produced a desired and ready 1→2 transition.",
    )
    data = [
        ("CPU", "d035", "CPU signal", TEAL, "Queue + synthetic KV\nheld below margins", arms["cpu"]),
        ("QUEUE", "d042", "Waiting requests", GOLD, "CPU + synthetic KV\nheld below margins", arms["queue"]),
        ("SYNTHETIC KV", "d040", "KV pressure", PURPLE, "CPU + queue\nheld below margins", arms["kv"]),
    ]
    for i, (name, run, signal, accent, control, arm) in enumerate(data):
        observations = arm["observations"]
        x = 84 + i * 510
        rounded(draw, (x, 218, x + 440, 734), CARD)
        draw.ellipse((x + 42, 262, x + 116, 336), fill=color(accent))
        draw.text((x + 144, 264), name, font=font(30, True), fill=color(TEXT))
        draw.text((x + 144, 308), f"selected run {run}", font=font(18), fill=color(MUTED))
        draw.text((x + 42, 384), signal, font=font(24, True), fill=color(TEXT))
        draw.text((x + 42, 442), "HPA transition", font=font(18), fill=color(MUTED))
        write_centered(draw, "1", (x + 42, 500, x + 144, 612), 62, TEXT, True)
        draw.polygon([(x + 300, 550), (x + 242, 514), (x + 242, 538), (x + 176, 538), (x + 176, 562), (x + 242, 562), (x + 242, 586)], fill=color(accent))
        write_centered(draw, str(observations["ready_replicas"]), (x + 326, 500, x + 424, 612), 62, TEXT, True)
        draw.text((x + 42, 650), control, font=font(18), fill=color(MUTED), spacing=6)
    draw.text((86, 794), f"Provenance: {provenance} • selected roots anchored independently • GPU performance not measured", font=font(18), fill=color(MUTED))
    save(image, "local-signal-independence.png")


def queue_cpu_control(queue: dict[str, object], provenance: str) -> None:
    peak = float(queue["observations"]["negative_cpu_max_millicores"])
    image, draw = canvas(
        "Queue-arm independence control: CPU stayed below the ceiling",
        "Run d042 — seven negative-control CPU captures; validator ceiling is 48 millicores.",
    )
    rounded(draw, (98, 218, 1502, 728), CARD)
    x0, y0, x1, y1 = 242, 304, 1420, 624
    for tick in range(0, 61, 12):
        y = y1 - (tick / 60) * (y1 - y0)
        draw.line((x0, y, x1, y), fill=color(GRID), width=2)
        draw.text((142, y - 12), str(tick), font=font(18), fill=color(MUTED))
    scale = (y1 - y0) / 60
    values = [("peak", peak, TEAL), ("ceiling", 48.0, GOLD)]
    for i, (label, value, accent) in enumerate(values):
        x = 540 + i * 410
        top = y1 - value * scale
        draw.rounded_rectangle((x, top, x + 230, y1), radius=18, fill=color(accent))
        write_centered(draw, f"{value:.6g}", (x, int(top) - 56, x + 230, int(top) - 8), 28, TEXT, True)
        write_centered(draw, label, (x, y1 + 26, x + 230, y1 + 66), 22, MUTED, True)
    draw.text((242, 662), "millicores", font=font(18), fill=color(MUTED))
    rounded(draw, (98, 764, 1502, 838), "1D2430", radius=18)
    draw.text((128, 776), "Conclusion: queue, not CPU, crossed the queue-arm target; this is a verified local plumbing result.", font=font(22, True), fill=color(TEXT))
    draw.text((128, 812), f"Provenance: {provenance}", font=font(15), fill=color(MUTED))
    save(image, "queue-cpu-control.png")


def evidence_boundary(provenance: str) -> None:
    image, draw = canvas(
        "Final paid canary: safe lifecycle proved, GPU path still open",
        "One $0.025 Vast attempt on distinct machine 147086; provider SSH never became usable.",
    )
    columns = [
        (112, "VERIFIED", TEAL, ["Provider contract: RTX 4000Ada", "machine 147086 / $0.025", "guarded exact-ID teardown", "three zero-match inventory reads"]),
        (844, "NOT DEMONSTRATED", CORAL, ["SSH never became ready", "36 bounded attempts", "direct GPU / CUDA probe", "vLLM or A/B metrics"]),
    ]
    for x, label, accent, lines in columns:
        rounded(draw, (x, 244, x + 644, 718), CARD)
        draw.ellipse((x + 48, 292, x + 122, 366), fill=color(accent))
        draw.text((x + 150, 302), label, font=font(31, True), fill=color(TEXT))
        for index, line in enumerate(lines):
            yy = 420 + index * 64
            draw.ellipse((x + 54, yy + 6, x + 70, yy + 22), fill=color(accent))
            draw.text((x + 94, yy), line, font=font(25), fill=color(TEXT))
    rounded(draw, (112, 760, 1488, 836), "1D2430", radius=18)
    draw.text((144, 774), "Instance 52212017 • report not attempted • sealed + guard-anchored FAILED_SAFE evidence", font=font(23, True), fill=color(TEXT))
    draw.text((144, 812), f"Provenance: {provenance}", font=font(15), fill=color(MUTED))
    save(image, "evidence-boundary.png")


if __name__ == "__main__":
    current_verdict = analyze_selected_evidence(ROOT)
    if current_verdict["verdict"] != "VALID":
        raise SystemExit(f"refusing charts: current evidence is {current_verdict['verdict']}")
    write_verdict(VERDICT, current_verdict)
    evidence = load_gated_evidence()
    local_provenance = evidence["verdict"]["claims"]["local_hpa_signal_plumbing"]["provenance"]
    live_provenance = evidence["verdict"]["claims"]["failed_safe_lifecycle"]["provenance"]
    local_signal_independence(evidence["arms"], local_provenance)
    queue_cpu_control(evidence["arms"]["queue"], local_provenance)
    evidence_boundary(live_provenance)
    print(f"Wrote charts to {OUT}")
