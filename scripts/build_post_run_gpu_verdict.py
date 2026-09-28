#!/usr/bin/env python3
"""Verify post-run standalone GPU evidence without changing pre-anchor history."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from scripts.build_evidence_pack import percentile
from srecon26_poc.journal import RunJournal
from srecon26_poc.types import RunState


ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "inference-infer20260923184725"
INSTANCE_ID = 52280205
MODEL = "Qwen/Qwen2.5-1.5B-Instruct"
REVISION = "989aa7980e4cf806f80c7fef2b1adb7bc71aa306"
IMAGE = "docker.io/vllm/vllm-openai@sha256:df2607b26bdda2875de4832f4d08da0055b4b6e3570347f3a849bcc652771dd6"
SERIES = ("num_requests_running", "num_requests_waiting", "gpu_cache_usage_perc", "kv_cache_usage_perc")
SUM = re.compile(r"([0-9a-f]{64})  (.+)")
REMOTE_PREFIX = "/var/tmp/srecon26-measured-benchmark/"
PRE_ANCHOR_ROOT = "788402210a9c58a09bbcd5ca9392f047f163b583f9ddaba972a5021d2985d5b0"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def timestamp(value: object) -> datetime:
    require(isinstance(value, str), "missing timestamp")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(stamp.tzinfo is not None, "timestamp lacks timezone")
    return stamp


def build_verdict(root: Path) -> dict[str, object]:
    run = Path("artifacts/runs") / RUN_ID
    bench = run / "manual-benchmark"
    remote = run / "remote-evidence"
    bindings: dict[str, str] = {}

    def raw(path: Path) -> bytes:
        require(not path.is_absolute() and all(part not in (".", "..") for part in path.parts), f"unsafe source path: {path}")
        source = root
        for part in path.parts:
            source /= part
            require(not source.is_symlink(), f"linked source: {path}")
        require(source.is_file(), f"missing source: {path}")
        data = source.read_bytes()
        bindings[path.as_posix()] = hashlib.sha256(data).hexdigest()
        return data

    def parsed(path: Path) -> object:
        return json.loads(raw(path))

    anchor = run / "pre-anchor"
    anchor_sums = raw(anchor / "SHA256SUMS")
    require(hashlib.sha256(anchor_sums).hexdigest() == raw(anchor / "ROOT-HASH.txt").decode("ascii").strip() == PRE_ANCHOR_ROOT, "pre-anchor root hash mismatch")
    anchored: dict[str, str] = {}
    for line in anchor_sums.decode("ascii").splitlines():
        match = SUM.fullmatch(line)
        require(match is not None, "invalid pre-anchor checksum line")
        digest, name = match.groups()
        require(re.fullmatch(r"(?:pre-anchor|remote-evidence|remote-transport)/[A-Za-z0-9][A-Za-z0-9._-]*", name) is not None and Path(name).name not in (".", ".."), "unsafe pre-anchor checksum path")
        require(name not in anchored, "duplicate pre-anchor checksum path")
        anchored[name] = digest
    actual_anchored: set[str] = set()
    for folder in ("pre-anchor", "remote-evidence", "remote-transport"):
        directory = root / run / folder
        require(directory.is_dir() and not directory.is_symlink(), f"missing or linked source directory: {folder}")
        for entry in directory.iterdir():
            require(entry.is_file() and not entry.is_symlink(), f"unexpected pre-anchor source: {entry.name}")
            if entry.name not in ("SHA256SUMS", "ROOT-HASH.txt") or folder != "pre-anchor":
                actual_anchored.add(f"{folder}/{entry.name}")
    require(len(anchored) == 42 and {"pre-anchor/limitation.json", "pre-anchor/run-manifest.json"} <= anchored.keys() and anchored.keys() == actual_anchored, "pre-anchor checksum inventory mismatch")
    for name, digest in anchored.items():
        require(hashlib.sha256(raw(run / name)).hexdigest() == digest, f"pre-anchor checksum mismatch: {name}")

    manifest = parsed(run / "pre-anchor/run-manifest.json")
    identity = parsed(run / "journal/identity.json")
    require(isinstance(manifest, dict) and isinstance(identity, dict), "invalid identity or manifest")
    label = identity.get("label")
    require(identity.get("run_id") == RUN_ID and isinstance(label, str) and label.startswith("srecon26-inference--nonce-"), "journal identity mismatch")
    require(manifest.get("run_id") == RUN_ID and manifest.get("label") == label and manifest.get("status") == "FAILED_SAFE", "pre-anchor history mismatch")
    frozen = manifest.get("workload_contract")
    require(isinstance(frozen, dict) and frozen == {"model_id": MODEL, "model_revision": REVISION, "vllm_image_digest": IMAGE.split("@", 1)[1]}, "frozen workload mismatch")
    journal_path = run / "journal/journal.ndjson"
    journal_bytes = raw(journal_path)
    require(bool(journal_bytes) and journal_bytes.endswith(b"\n") and all(journal_bytes.split(b"\n")[:-1]), "partial journal line")
    journal = RunJournal.open(root / run / "journal")
    events = journal.events()
    require(journal.state() is RunState.TERMINAL and bool(events) and events[-1].event_type == "terminal.safe" and events[-1].payload.get("status") == "FAILED_SAFE", "journal terminal state mismatch")
    require(all(event.run_id == RUN_ID and event.label == label for event in events), "journal chain identity mismatch")
    created = [event for event in events if event.event_type == "provider.create_observed"]
    teardown = [event for event in events if event.event_type == "teardown.exact"]
    absence_event = [event for event in events if event.event_type == "absence.proved"]
    require(len(created) == len(teardown) == len(absence_event) == 1, "incomplete journal lifecycle")
    require(created[0].payload.get("instance_id") == INSTANCE_ID and teardown[0].payload.get("instance_id") == INSTANCE_ID and teardown[0].payload.get("label") == label, "journal instance mismatch")
    require(absence_event[0].payload.get("reads") == 3 and len(absence_event[0].payload.get("timestamps", [])) == 3, "journal absence mismatch")
    start, end = created[0].wall_time, teardown[0].wall_time
    require(start < end, "invalid journal lifecycle")

    invoice = parsed(Path("artifacts/runs") / f"invoice-{RUN_ID}.json")
    absence = parsed(Path("artifacts/runs") / f"absence-{RUN_ID}.json")
    require(isinstance(invoice, dict) and isinstance(absence, dict), "invalid provider receipts")
    for receipt, schema, source in ((invoice, "srecon26-vast-invoice-evidence/v1", "VastCliProvider.capture_invoice_charge/v1"), (absence, "srecon26-vast-absence-evidence/v1", "VastCliProvider.capture_absence_evidence/v1")):
        require(receipt.get("schema") == schema and receipt.get("source") == source and receipt.get("run_id") == RUN_ID and receipt.get("instance_id") == INSTANCE_ID and receipt.get("label") == label, "provider receipt binding mismatch")
    charge = invoice.get("provider_charge")
    require(isinstance(charge, dict) and isinstance(charge.get("metadata"), dict) and charge["metadata"].get("label") == label and charge.get("source") == f"instance-{INSTANCE_ID}" and charge.get("type") == "instance", "invoice charge identity mismatch")
    amount = Decimal(str(invoice.get("amount_usd")))
    require(amount == Decimal("0.116") and amount == Decimal(str(charge.get("amount"))) and amount.is_finite(), "invoice amount mismatch")
    reads = absence.get("reads")
    require(isinstance(reads, list) and len(reads) == 3 and all(isinstance(item, dict) and item.get("matching_instances") == 0 for item in reads), "absence quorum mismatch")
    read_times = [timestamp(item["observed_at"]) for item in reads]
    require(read_times == sorted(set(read_times)) and read_times[0] > teardown[0].wall_time and timestamp(invoice.get("observed_at")) >= read_times[-1], "absence reads not fresh and ordered")

    contract = parsed(remote / "inference-contract.json")
    inspected = parsed(remote / "inference-container-inspect.json")
    command = parsed(remote / "inference-container-command.json")
    models = parsed(remote / "inference-vllm-models.json")
    image_inspect = parsed(remote / "inference-image-inspect.json")
    gpu = raw(remote / "inference-gpu-identity.txt").decode("utf-8")
    cuda = raw(remote / "inference-cuda.txt").decode("utf-8")
    kvm = raw(remote / "kvm-virt.txt").decode("utf-8")
    require("NVIDIA GeForce RTX 4090" in gpu and "NVIDIA GeForce RTX 4090" in cuda and "CUDA Version: 12.9" in cuda and kvm.strip() == "kvm", "GPU/CUDA/KVM probe mismatch")
    require(isinstance(contract, dict) and contract.get("model") == MODEL and contract.get("model_revision") == REVISION and contract.get("image") == IMAGE and contract.get("endpoint") == "http://127.0.0.1:28000", "remote contract mismatch")
    require(isinstance(inspected, dict) and inspected.get("image") == IMAGE and inspected.get("command") == command and inspected.get("port_bindings", {}).get("8000/tcp") == [{"HostIp": "127.0.0.1", "HostPort": "28000"}], "runtime image or endpoint mismatch")
    require(isinstance(command, list) and all(option in command and command[command.index(option) + 1] == value for option, value in (("--model", MODEL), ("--revision", REVISION), ("--served-model-name", MODEL))), "runtime model revision mismatch")
    require(image_inspect == [IMAGE.replace("docker.io/", "", 1)] and isinstance(models, dict) and any(item.get("id") == MODEL for item in models.get("data", []) if isinstance(item, dict)), "served model or image mismatch")

    sums_path = bench / "SHA256SUMS"
    original_sums = raw(sums_path).decode("utf-8")
    sums: dict[str, str] = {}
    for line in original_sums.splitlines():
        match = SUM.fullmatch(line)
        require(match is not None, "invalid checksum line")
        digest, name = match.groups()
        require(name == Path(name).name or (name.startswith(REMOTE_PREFIX) and name[len(REMOTE_PREFIX):] == Path(name).name), "unexpected checksum path")
        name = Path(name).name
        require(name not in sums and name != "SHA256SUMS", "duplicate checksum basename")
        sums[name] = digest
    actual = {path.name for path in (root / bench).iterdir() if path.is_file() and path.name != "SHA256SUMS"}
    require(len(sums) == 125 and sums.keys() == actual, "checksum inventory not exactly 125 raw files")
    for name, digest in sums.items():
        require(hashlib.sha256(raw(bench / name)).hexdigest() == digest, f"checksum mismatch: {name}")
    normalized = "".join(f"{sums[name]}  {name}\n" for name in sorted(sums)).encode()

    latency: dict[str, dict[str, float | int]] = {}
    observed: dict[str, dict[str, dict[str, float | int]]] = {}
    completion_ids: set[str] = set()
    for arm, count in (("c4", 4), ("c32", 32)):
        measurements: dict[str, list[float]] = {key: [] for key in ("ttft", "tpot", "e2e")}
        numbered = {f"{arm}-request-{number}.json" for number in range(1, count + 1)}
        require({name for name in sums if re.fullmatch(rf"{arm}-request-\d+\.json", name)} == numbered, "numbered request inventory mismatch")
        for number in range(1, count + 1):
            name = f"{arm}-request-{number}"
            request = parsed(bench / f"{name}.json")
            curl = parsed(bench / f"{name}-curl.json")
            require(isinstance(request, dict) and isinstance(curl, dict), "invalid raw request")
            require(request.get("arm") == arm and request.get("request_number") == number and request.get("http_code") == curl.get("http_code") == 200 and request.get("requested_model") == request.get("response_model") == MODEL, "request model or HTTP mismatch")
            require(request.get("usage", {}).get("completion_tokens") == request.get("completion_tokens") == 128 and request.get("usage", {}).get("total_tokens") == request.get("total_tokens"), "completion usage mismatch")
            require(all(request.get(field) == curl.get(field) for field in ("ttft_seconds", "e2e_seconds")), "curl timing mismatch")
            body = raw(bench / f"{name}-body.ndjson").decode("utf-8")
            lines = [line for line in body.splitlines() if line.strip()]
            require(len(lines) >= 3 and lines[-1] == "data: [DONE]" and all(line.startswith("data: {") for line in lines[:-1]), "streamed response mismatch")
            try:
                chunks = [json.loads(line[6:]) for line in lines[:-1]]
            except json.JSONDecodeError as exc:
                raise ValueError("streamed response mismatch") from exc
            require(all(isinstance(chunk, dict) and chunk.get("model") == MODEL for chunk in chunks), "streamed response mismatch")
            require(all(type(chunk.get("created")) is int and start.timestamp() <= chunk["created"] <= end.timestamp() for chunk in chunks), "stream created outside journal lifecycle")
            last_choices = chunks[-2].get("choices")
            require(chunks[-1].get("usage") == request["usage"] and chunks[-1].get("choices") == [] and isinstance(last_choices, list) and len(last_choices) == 1 and isinstance(last_choices[0], dict) and last_choices[0].get("finish_reason") == "length", "streamed response mismatch")
            completion_id = chunks[0].get("id")
            require(isinstance(completion_id, str) and bool(completion_id) and all(chunk.get("id") == completion_id for chunk in chunks), "streamed completion ID mismatch")
            require(completion_id not in completion_ids, "duplicate completion ID")
            completion_ids.add(completion_id)
            for key, field in (("ttft", "ttft_seconds"), ("e2e", "e2e_seconds")):
                value = request.get(field)
                require(type(value) in (int, float) and math.isfinite(value) and value > 0, f"invalid {key} timing")
                measurements[key].append(value * 1000)
            raw_tpot = (curl["e2e_seconds"] - curl["ttft_seconds"]) / (request["completion_tokens"] - 1)
            stored_tpot = request.get("tpot_seconds")
            require(type(stored_tpot) in (int, float) and math.isfinite(stored_tpot) and math.isfinite(raw_tpot) and raw_tpot > 0 and math.isclose(stored_tpot, raw_tpot, rel_tol=0, abs_tol=1e-9), "tpot timing mismatch")
            measurements["tpot"].append(raw_tpot * 1000)
        latency[arm] = {"requests": count, **{f"p{quantile}_{key}_ms": percentile(values, quantile / 100) for key, values in measurements.items() for quantile in (50, 95)}}

        expected_metrics = {f'vllm:{series}{{engine="0",model_name="{MODEL}"}}' for series in SERIES}
        by_series: dict[str, list[float]] = {series: [] for series in SERIES}
        samples: dict[int, set[str]] = {}
        sample_times: dict[int, datetime] = {}
        sampling = raw(bench / f"{arm}-metrics-during.ndjson").decode("utf-8")
        require(bool(sampling.strip()), "missing Prometheus sampling")
        for line in sampling.splitlines():
            prefix, metric_and_value = line.split(',"metric":"', 1)
            metric, value = metric_and_value.split('","value":', 1)
            header = json.loads(prefix + "}")
            measure = json.loads('{"value":' + value)
            require(metric in expected_metrics and set(header) == {"observed_at", "sample"} and set(measure) == {"value"}, "unexpected Prometheus sample")
            observed_at = timestamp(header["observed_at"])
            require(start <= observed_at <= end, "Prometheus sample outside journal lifecycle")
            sample = header["sample"]
            reading = measure["value"]
            require(type(sample) is int and sample > 0 and type(reading) in (int, float) and math.isfinite(reading) and reading >= 0, "invalid Prometheus value")
            require(sample not in sample_times or sample_times[sample] == observed_at, "inconsistent Prometheus sample time")
            sample_times[sample] = observed_at
            series = metric.split(":", 1)[1].split("{", 1)[0]
            require(series not in samples.setdefault(sample, set()), "duplicate Prometheus series")
            samples[sample].add(series)
            by_series[series].append(reading)
        require(bool(samples) and all(names == set(SERIES) for names in samples.values()), "incomplete Prometheus series")
        require(list(samples) == list(range(1, len(samples) + 1)), "noncontiguous Prometheus samples")
        require(all(earlier < later for earlier, later in zip(sample_times.values(), list(sample_times.values())[1:])), "nonincreasing Prometheus sample time")
        observed[arm] = {f"vllm:{series}": {"samples": len(values), "max": max(values)} for series, values in by_series.items()}

    require(len(completion_ids) == 36, "incomplete completion IDs")
    return {
        "schema": "srecon26-standalone-gpu-post-run/v1",
        "run_id": RUN_ID,
        "instance_id": INSTANCE_ID,
        "label": label,
        "historical_pre_anchor_status": "FAILED_SAFE",
        "journal_final_state": journal.state().value,
        "journal_root_hash": events[-1].event_hash,
        "model": MODEL,
        "model_revision": REVISION,
        "image": IMAGE,
        "invoice_usd": str(amount),
        "raw_file_count": len(sums),
        "normalized_sha256s_sha256": hashlib.sha256(normalized).hexdigest(),
        "latency_ms": latency,
        "prometheus_observed": observed,
        "sampling_format_note": "Captured during files have unescaped metric-label quotes; strict observed-series parser used, not JSON NDJSON parsing.",
        "provenance_limitations": {
            "pre_anchor": "Pre-anchor SHA256SUMS and ROOT-HASH.txt establish retained local consistency, not original capture anchoring.",
            "benchmark": "Original benchmark SHA256SUMS was normalized in place on 2026-09-25; original remote-path file unavailable. Post-run checksums do not independently anchor benchmark capture.",
            "model_revision": "Model revision comes from config/CLI, not independent snapshot hash.",
            "absence": "Later absence receipt is a post-hoc re-capture, not original teardown observation.",
        },
        "reverification_required": "PYTHONPATH=.:src python3 scripts/build_post_run_gpu_verdict.py --verify",
        "standalone_gpu_latency": {"allowed": True, "basis": "Locally checksum-verified post-run evidence; benchmark capture not independently anchored."},
        "kubernetes_hpa": {"allowed": False},
        "paired_hpa_performance": {"allowed": False},
        "source_sha256": dict(sorted(bindings.items())),
    }


def normalized_sums(root: Path) -> bytes:
    path = root / "artifacts/runs" / RUN_ID / "manual-benchmark/SHA256SUMS"
    entries = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = SUM.fullmatch(line)
        require(match is not None, "invalid checksum line")
        digest, name = match.groups()
        entries[Path(name).name] = digest
    return "".join(f"{entries[name]}  {name}\n" for name in sorted(entries)).encode()


def canonical_verdict_bytes(verdict: dict[str, object]) -> bytes:
    return (json.dumps(verdict, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def atomic_write(path: Path, data: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--verify", action="store_true", help="recheck every source binding against existing verdict")
    parser.add_argument("--print-verified", action="store_true", help="with --verify, print canonical recomputed verdict JSON")
    args = parser.parse_args()
    if args.print_verified and not args.verify:
        parser.error("--print-verified requires --verify")
    try:
        verdict = build_verdict(args.root)
        normalized = normalized_sums(args.root)
        require(hashlib.sha256(normalized).hexdigest() == verdict["normalized_sha256s_sha256"], "normalized checksum mismatch")
        output = args.root / "artifacts/runs" / RUN_ID / "post-run"
        canonical = canonical_verdict_bytes(verdict)
        if args.verify:
            require(json.loads((output / "verdict.json").read_bytes()) == verdict and (output / "SHA256SUMS").read_bytes() == normalized, "post-run verdict or bindings stale")
            if args.print_verified:
                sys.stdout.buffer.write(canonical)
                sys.stdout.buffer.flush()
        else:
            output.mkdir(parents=True, exist_ok=True)
            atomic_write(output / "SHA256SUMS", normalized)
            atomic_write(output / "verdict.json", canonical)
    except (OSError, ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        parser.exit(1, f"post-run GPU verdict rejected: {exc}\n")


if __name__ == "__main__":
    main()