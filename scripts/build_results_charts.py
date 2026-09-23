#!/usr/bin/env python3
"""Render presentation-quality PNG charts from the selected, verified evidence.

This intentionally charts only values retained in the validation contract.  The
local KV signal is marked synthetic, and live GPU data is a limitation, not an
experimental outcome.
"""

from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


OUT = Path(__file__).resolve().parents[1] / "artifacts" / "presentation"
W, H = 1600, 900
BG, CARD, TEXT, MUTED = "171B22", "242A35", "F5F7FA", "B9C2CF"
TEAL, GOLD, CORAL, PURPLE, GRID = "24C4B8", "F6C85F", "FF6B6B", "A78BFA", "3B4554"


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


def local_signal_independence() -> None:
    image, draw = canvas(
        "Three independently verified local scaling signals",
        "Selected Kind/local runs — each arm produced a desired and ready 1→2 transition.",
    )
    data = [
        ("CPU", "d035", "CPU signal", TEAL, "Queue + synthetic KV\nheld below margins"),
        ("QUEUE", "d042", "Waiting requests", GOLD, "CPU + synthetic KV\nheld below margins"),
        ("SYNTHETIC KV", "d040", "KV pressure", PURPLE, "CPU + queue\nheld below margins"),
    ]
    for i, (name, run, signal, accent, control) in enumerate(data):
        x = 84 + i * 510
        rounded(draw, (x, 218, x + 440, 734), CARD)
        draw.ellipse((x + 42, 262, x + 116, 336), fill=color(accent))
        draw.text((x + 144, 264), name, font=font(30, True), fill=color(TEXT))
        draw.text((x + 144, 308), f"selected run {run}", font=font(18), fill=color(MUTED))
        draw.text((x + 42, 384), signal, font=font(24, True), fill=color(TEXT))
        draw.text((x + 42, 442), "HPA transition", font=font(18), fill=color(MUTED))
        write_centered(draw, "1", (x + 42, 500, x + 144, 612), 62, TEXT, True)
        draw.polygon([(x + 176, 550), (x + 234, 514), (x + 234, 538), (x + 300, 538), (x + 300, 562), (x + 234, 562), (x + 234, 586)], fill=color(accent))
        write_centered(draw, "2", (x + 326, 500, x + 424, 612), 62, TEXT, True)
        draw.text((x + 42, 650), control, font=font(18), fill=color(MUTED), spacing=6)
    draw.text((86, 794), "Provenance: local-synthetic • selected roots anchored independently • GPU performance not measured", font=font(18), fill=color(MUTED))
    save(image, "local-signal-independence.png")


def queue_cpu_control() -> None:
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
    values = [("peak", 16.456944, TEAL), ("ceiling", 48.0, GOLD)]
    for i, (label, value, accent) in enumerate(values):
        x = 540 + i * 410
        top = y1 - value * scale
        draw.rounded_rectangle((x, top, x + 230, y1), radius=18, fill=color(accent))
        write_centered(draw, f"{value:.6g}", (x, int(top) - 56, x + 230, int(top) - 8), 28, TEXT, True)
        write_centered(draw, label, (x, y1 + 26, x + 230, y1 + 66), 22, MUTED, True)
    draw.text((242, 662), "millicores", font=font(18), fill=color(MUTED))
    rounded(draw, (98, 764, 1502, 838), "1D2430", radius=18)
    draw.text((128, 784), "Conclusion: queue, not CPU, crossed the queue-arm target; this is a verified local plumbing result.", font=font(22, True), fill=color(TEXT))
    save(image, "queue-cpu-control.png")


def evidence_boundary() -> None:
    image, draw = canvas(
        "Evidence boundary: what is demonstrated versus what remains open",
        "The deck separates verified local signal plumbing from the incomplete live GPU path.",
    )
    columns = [
        (112, "VERIFIED", TEAL, ["Local CPU, queue, and", "synthetic-KV HPA arms", "independent 1→2 transitions", "anchored evidence roots"]),
        (844, "NOT DEMONSTRATED", CORAL, ["GPU identity / CUDA", "KVM capability", "real vLLM metrics", "TTFT or A/B performance"]),
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
    draw.text((144, 782), "Current live run status: FAILED_SAFE; exact teardown and absence evidence preserved.", font=font(23, True), fill=color(TEXT))
    save(image, "evidence-boundary.png")


if __name__ == "__main__":
    local_signal_independence()
    queue_cpu_control()
    evidence_boundary()
    print(f"Wrote charts to {OUT}")
