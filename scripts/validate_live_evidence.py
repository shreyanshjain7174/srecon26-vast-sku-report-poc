#!/usr/bin/env python3
"""Fail closed unless live local evidence satisfies every arm's 20% isolation margin."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def metric(path: Path, name: str) -> float:
    match = re.search(rf"^{re.escape(name)} ([0-9.]+)$", path.read_text(), re.M)
    if not match: raise ValueError(f"{name} missing from {path}")
    return float(match.group(1))


def cpu_millicores(path: Path) -> float:
    raw = next(item["usage"]["cpu"] for item in json.loads(path.read_text())["containers"] if item["name"] == "server")
    return float(raw[:-1]) / 1_000_000 if raw.endswith("n") else float(raw.rstrip("m"))


def negative_margins(arm: str, arm_path: Path) -> bool:
    sources = sorted(arm_path.glob("negative-*.source.prom"))
    cpus = sorted(arm_path.glob("negative-*.cpu.json"))
    if len(sources) < 7 or len(cpus) < 7:
        return False
    if arm == "cpu":
        return all(metric(path, "vllm:num_requests_waiting") <= 0.8 and metric(path, "vllm:kv_cache_usage_perc") <= 0.64 for path in sources)
    if arm == "queue":
        return all(cpu_millicores(path) <= 48 for path in cpus) and all(metric(path, "vllm:kv_cache_usage_perc") <= 0.64 for path in sources)
    return all(cpu_millicores(path) <= 48 for path in cpus) and all(metric(path, "vllm:num_requests_waiting") <= 0.8 for path in sources)


def main(arm_paths: dict[str, Path]) -> int:
    checks = {
        "cpu": lambda source, cpu: cpu >= 72 and metric(source, "vllm:num_requests_waiting") <= 0.8 and metric(source, "vllm:kv_cache_usage_perc") <= 0.64,
        "queue": lambda source, cpu: metric(source, "vllm:num_requests_waiting") >= 1.2 and cpu <= 48 and metric(source, "vllm:kv_cache_usage_perc") <= 0.64,
        "kv": lambda source, cpu: metric(source, "vllm:kv_cache_usage_perc") >= 0.96 and cpu <= 48 and metric(source, "vllm:num_requests_waiting") <= 0.8,
    }
    results = {}
    for arm, check in checks.items():
        arm_path = arm_paths[arm]
        source, cpu = arm_path / "scaled.source.prom", arm_path / "scaled.cpu.json"
        if not source.exists() or not cpu.exists(): results[arm] = False; continue
        negatives = sorted(arm_path.glob("negative-*.hpa.json"))
        desired_one = len(negatives) >= 7 and all((json.loads(path.read_text()).get("status", {}).get("desiredReplicas", 1) == 1) for path in negatives)
        scaled = json.loads((arm_path / "scaled.hpa.json").read_text()).get("status", {}).get("desiredReplicas") == 2
        ready = json.loads((arm_path / "scaled.deployment.json").read_text()).get("status", {}).get("readyReplicas") == 2
        results[arm] = desired_one and scaled and ready and negative_margins(arm, arm_path) and check(source, cpu_millicores(cpu))
    print(json.dumps(results, sort_keys=True))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    if len(sys.argv) != 4: raise SystemExit("usage: validate_live_evidence.py CPU_DIR QUEUE_DIR KV_DIR")
    raise SystemExit(main(dict(zip(("cpu", "queue", "kv"), map(Path, sys.argv[1:])))))
