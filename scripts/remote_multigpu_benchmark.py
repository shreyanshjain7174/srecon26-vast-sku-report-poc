#!/usr/bin/env python3
"""On-host Qwen3.8-27B TP8 vLLM benchmark driver.

Load generation uses the Python standard library only; vLLM, nvidia-smi and
timeout run as argv subprocesses (never via a shell). Everything binds and
talks to 127.0.0.1 only. The vLLM API key is generated per run and redacted
from every output file.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Protocol, Sequence

MODEL_ID = "Qwen/Qwen3.8-27B"
MODEL_REVISION = "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0"
VLLM_IMAGE = "docker.io/vllm/vllm-openai@sha256:5f5e535216848d0c52159c8c13a0af04be5f6fe1a84e79914300610796f76d40"
SERVED_MODEL_NAME = "qwen38-27b"
HOST = "127.0.0.1"
PORT = 8000
BASE_URL = f"http://{HOST}:{PORT}"
TENSOR_PARALLEL = 8
MAX_MODEL_LEN = 4096
MAX_NUM_SEQS = 64
GPU_MEMORY_UTILIZATION = "0.90"
READINESS_TIMEOUT_S = 1800
PRECISION_FLAGS: dict[str, tuple[str, ...]] = {
    "bf16": (),
    "fp8": ("--quantization", "fp8"),
    "fp8-kv": ("--quantization", "fp8", "--kv-cache-dtype", "fp8"),
}
SCHEMA = "remote-multigpu-benchmark/v1"
REDACTED = "<redacted>"
PROMPT_SEED_TEXT = (
    "The quick brown fox jumps over the lazy dog while the site reliability engineer "
    "reviews the latency histogram for the inference fleet. "
)
GPU_QUERY = (
    "timestamp,index,name,utilization.gpu,utilization.memory,memory.used,memory.total,"
    "power.draw,temperature.gpu,clocks.sm"
)
OUTPUT_FILES = ("run.json", "requests.ndjson", "metrics.ndjson", "gpu.csv", "nvidia-smi-q.txt", "topology.txt", "vllm.log")


@dataclasses.dataclass(frozen=True)
class Cell:
    name: str
    input_tokens: int
    output_tokens: int
    concurrency: int
    num_requests: int


_SMOKE = (
    Cell("short-c1", 1024, 256, 1, 32),
    Cell("short-c8", 1024, 256, 8, 64),
)
_FALLBACK_MATRIX: dict[str, tuple[Cell, ...]] = {
    "smoke": _SMOKE,
    "full": _SMOKE + (Cell("short-c32", 1024, 256, 32, 128), Cell("long-c8", 3072, 512, 8, 64)),
}


def benchmark_cells(matrix: str) -> tuple[Cell, ...]:
    try:
        from srecon26_poc.multigpu_contracts import BenchmarkMatrix
    except ImportError:
        if matrix not in _FALLBACK_MATRIX:
            raise ValueError(f"unknown matrix name {matrix!r}") from None
        return _FALLBACK_MATRIX[matrix]
    cells = BenchmarkMatrix.for_name(matrix).cells
    return tuple(Cell(c.name, c.input_tokens, c.output_tokens, c.concurrency, c.num_requests) for c in cells)


def serve_command(api_key: str, precision: str = "bf16") -> list[str]:
    if not api_key or any(ch.isspace() for ch in api_key):
        raise ValueError("api_key must be a non-empty token without whitespace")
    if precision not in PRECISION_FLAGS:
        raise ValueError(f"unknown precision {precision!r}")
    return [
        "vllm", "serve", MODEL_ID,
        "--revision", MODEL_REVISION,
        "--tokenizer-revision", MODEL_REVISION,
        "--tensor-parallel-size", str(TENSOR_PARALLEL),
        "--dtype", "bfloat16",
        *PRECISION_FLAGS[precision],
        "--max-model-len", str(MAX_MODEL_LEN),
        "--max-num-seqs", str(MAX_NUM_SEQS),
        "--gpu-memory-utilization", GPU_MEMORY_UTILIZATION,
        "--seed", "0",
        "--host", HOST,
        "--port", str(PORT),
        "--served-model-name", SERVED_MODEL_NAME,
        "--api-key", api_key,
    ]


def redact(text: str, secret: Optional[str]) -> str:
    return text.replace(secret, REDACTED) if secret else text


def redact_argv(argv: Sequence[str], secret: str) -> list[str]:
    return [REDACTED if arg == secret else redact(arg, secret) for arg in argv]


def percentile(values: Sequence[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100.0
    low, high = math.floor(rank), math.ceil(rank)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


# --- HTTP -------------------------------------------------------------------


class HttpResponse(Protocol):
    status: int

    def lines(self) -> Iterator[bytes]: ...

    def read(self) -> bytes: ...

    def close(self) -> None: ...


Transport = Callable[[str, str, "dict[str, str]", Optional[bytes], float], HttpResponse]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


class _UrllibResponse:
    def __init__(self, raw: Any, status: int) -> None:
        self._raw = raw
        self.status = status

    def lines(self) -> Iterator[bytes]:
        yield from self._raw

    def read(self) -> bytes:
        return self._raw.read()

    def close(self) -> None:
        self._raw.close()


def _require_loopback(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or parts.hostname != HOST:
        raise ValueError(f"refusing non-loopback URL {url!r}")


def urllib_transport(method: str, url: str, headers: dict[str, str], body: Optional[bytes], timeout: float) -> HttpResponse:
    _require_loopback(url)
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        raw = _OPENER.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return _UrllibResponse(exc, exc.code)
    return _UrllibResponse(raw, raw.status)


@dataclasses.dataclass(frozen=True)
class Client:
    api_key: str = dataclasses.field(repr=False)
    transport: Transport = urllib_transport
    base_url: str = BASE_URL

    def __post_init__(self) -> None:
        _require_loopback(self.base_url)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def get(self, path: str, timeout: float) -> tuple[int, bytes]:
        response = self.transport("GET", self.base_url + path, self._headers(), None, timeout)
        try:
            return response.status, response.read()
        finally:
            response.close()

    def post_json(self, path: str, payload: dict[str, Any], timeout: float) -> tuple[int, bytes]:
        response = self.stream(path, payload, timeout)
        try:
            return response.status, response.read()
        finally:
            response.close()

    def stream(self, path: str, payload: dict[str, Any], timeout: float) -> HttpResponse:
        body = json.dumps(payload).encode()
        return self.transport("POST", self.base_url + path, self._headers(), body, timeout)


def tokenize(client: Client, text: str) -> list[int]:
    payload = {"model": SERVED_MODEL_NAME, "prompt": text, "add_special_tokens": False}
    status, body = client.post_json("/tokenize", payload, 60.0)
    if status != 200:
        raise RuntimeError(f"tokenize failed with http {status}")
    tokens = json.loads(body)["tokens"]
    if not all(isinstance(t, int) and not isinstance(t, bool) for t in tokens):
        raise RuntimeError("tokenize returned non-integer tokens")
    return tokens


def build_prompt_tokens(tokenize_fn: Callable[[str], list[int]], n_tokens: int) -> list[int]:
    # Seed text is repeated past n_tokens assuming >= 1 token per 8 chars, then cycled if short.
    reps = max(1, math.ceil(n_tokens * 8 / len(PROMPT_SEED_TEXT)))
    tokens = list(tokenize_fn(PROMPT_SEED_TEXT * reps))
    if not tokens:
        raise RuntimeError("tokenizer returned no tokens for seed text")
    return (tokens * math.ceil(n_tokens / len(tokens)))[:n_tokens]


# --- load generation --------------------------------------------------------


def run_request(
    client: Client,
    *,
    cell_name: str,
    index: int,
    prompt_tokens: Sequence[int],
    max_tokens: int,
    clock: Callable[[], float] = time.monotonic,
    timeout: float = 600.0,
) -> dict[str, Any]:
    payload = {
        "model": SERVED_MODEL_NAME,
        "prompt": list(prompt_tokens),
        "max_tokens": max_tokens,
        "temperature": 0,
        "ignore_eos": True,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    record: dict[str, Any] = {
        "cell": cell_name,
        "index": index,
        "input_tokens": len(prompt_tokens),
        "max_tokens": max_tokens,
        "status": "error",
        "http_status": None,
        "ttft_s": None,
        "e2e_s": None,
        "tpot_s": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "error": None,
    }
    response: Optional[HttpResponse] = None
    start = clock()
    try:
        response = client.stream("/v1/completions", payload, timeout)
        record["http_status"] = response.status
        if response.status != 200:
            detail = response.read()[:300].decode("utf-8", errors="replace")
            record["error"] = f"http {response.status}: {detail}"
            return record
        ttft: Optional[float] = None
        usage: Optional[dict[str, Any]] = None
        done = False
        for raw in response.lines():
            line = raw.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                done = True
                break
            chunk = json.loads(data)
            if ttft is None and any(choice.get("text") for choice in chunk.get("choices") or ()):
                ttft = clock() - start
            if chunk.get("usage"):
                usage = chunk["usage"]
        e2e = clock() - start
        record["e2e_s"] = e2e
        record["ttft_s"] = ttft
        if usage:
            record["prompt_tokens"] = usage.get("prompt_tokens")
            record["completion_tokens"] = usage.get("completion_tokens")
        if not done:
            record["error"] = "stream ended without [DONE]"
        elif usage is None:
            record["error"] = "stream missing usage"
        elif ttft is None:
            record["error"] = "stream produced no tokens"
        else:
            record["status"] = "ok"
            completion = record["completion_tokens"] or 0
            if completion > 1:
                record["tpot_s"] = (e2e - ttft) / (completion - 1)
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if response is not None:
            response.close()
        if record["error"]:
            record["error"] = redact(record["error"], client.api_key)[:500]
    return record


def summarize(cell: Cell, records: Sequence[dict[str, Any]], duration_s: float) -> dict[str, Any]:
    ok = [r for r in records if r.get("status") == "ok"]
    output_tokens = sum(r.get("completion_tokens") or 0 for r in ok)
    summary: dict[str, Any] = {
        "cell": cell.name,
        "input_tokens": cell.input_tokens,
        "output_tokens": cell.output_tokens,
        "concurrency": cell.concurrency,
        "num_requests": cell.num_requests,
        "requests": len(records),
        "successes": len(ok),
        "failures": len(records) - len(ok),
        "output_token_mismatches": sum(1 for r in ok if r.get("completion_tokens") != cell.output_tokens),
        "duration_s": duration_s,
        "req_per_s": len(ok) / duration_s if duration_s > 0 else None,
        "output_tok_per_s": output_tokens / duration_s if duration_s > 0 else None,
    }
    for metric in ("ttft", "e2e", "tpot"):
        values = [r[f"{metric}_s"] for r in ok if r.get(f"{metric}_s") is not None]
        for pct in (50, 90, 99):
            summary[f"{metric}_p{pct}_s"] = percentile(values, pct)
    return summary


def run_cell(
    cell: Cell,
    request_fn: Callable[[int], dict[str, Any]],
    sink: Callable[[dict[str, Any]], None],
    *,
    clock: Callable[[], float] = time.monotonic,
    secret: Optional[str] = None,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    start = clock()
    with ThreadPoolExecutor(max_workers=cell.concurrency, thread_name_prefix=f"bench-{cell.name}") as pool:
        futures = {pool.submit(request_fn, index): index for index in range(cell.num_requests)}
        for future in as_completed(futures):
            try:
                record = future.result()
            except Exception as exc:
                record = {
                    "cell": cell.name,
                    "index": futures[future],
                    "status": "error",
                    "error": redact(f"{type(exc).__name__}: {exc}", secret)[:500],
                }
            sink(record)
            records.append(record)
    return summarize(cell, records, clock() - start)


# --- metrics ----------------------------------------------------------------


_PROM_LINE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{.*\})?\s+(\S+)(?:\s+-?\d+)?$")


def parse_prometheus(text: str) -> dict[str, list[float]]:
    parsed: dict[str, list[float]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _PROM_LINE.match(line)
        if not match:
            continue
        try:
            value = float(match.group(3))
        except ValueError:
            continue
        parsed.setdefault(match.group(1), []).append(value)
    return parsed


def sample_metrics(text: str) -> dict[str, Optional[float]]:
    parsed = parse_prometheus(text)
    kv = parsed.get("vllm:kv_cache_usage_perc") or parsed.get("vllm:gpu_cache_usage_perc")
    waiting = parsed.get("vllm:num_requests_waiting")
    running = parsed.get("vllm:num_requests_running")
    return {
        "waiting": sum(waiting) if waiting else None,
        "running": sum(running) if running else None,
        "kv_cache_usage": max(kv) if kv else None,
    }


def sample_once(client: Client, *, clock: Callable[[], float], wall: Callable[[], float]) -> dict[str, Any]:
    record: dict[str, Any] = {"t_mono": clock(), "t_wall": wall()}
    try:
        status, body = client.get("/metrics", 10.0)
        if status == 200:
            record.update(sample_metrics(body.decode("utf-8", errors="replace")))
        else:
            record["error"] = f"http {status}"
    except Exception as exc:
        record["error"] = redact(f"{type(exc).__name__}: {exc}", client.api_key)[:300]
    return record


class MetricsSampler(threading.Thread):
    def __init__(
        self,
        client: Client,
        path: Path,
        *,
        interval_s: float,
        deadline: float,
        clock: Callable[[], float],
        wall: Callable[[], float],
    ) -> None:
        super().__init__(name="metrics-sampler", daemon=True)
        self._client = client
        self._path = path
        self._interval_s = interval_s
        self._deadline = deadline
        self._clock = clock
        self._wall = wall
        self.stop_event = threading.Event()

    def run(self) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            while not self.stop_event.is_set() and self._clock() < self._deadline:
                record = sample_once(self._client, clock=self._clock, wall=self._wall)
                fh.write(json.dumps(record, sort_keys=True) + "\n")
                fh.flush()
                self.stop_event.wait(self._interval_s)


# --- processes --------------------------------------------------------------


def terminate_process(proc: Any, *, grace_s: float = 30.0, killpg: Callable[[int, int], None] = os.killpg) -> None:
    if proc is None or proc.poll() is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=grace_s)
            return
        except subprocess.TimeoutExpired:
            continue


def wait_for_ready(
    client: Client,
    proc: Any,
    *,
    timeout_s: float = READINESS_TIMEOUT_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_s: float = 5.0,
) -> float:
    start = clock()
    while True:
        if proc.poll() is not None:
            raise RuntimeError(f"vllm exited with code {proc.returncode} before becoming ready")
        try:
            status, _ = client.get("/health", 5.0)
            if status == 200:
                status, body = client.get("/v1/models", 10.0)
                ids = {m.get("id") for m in json.loads(body).get("data", [])} if status == 200 else set()
                if SERVED_MODEL_NAME in ids:
                    return clock() - start
        except (OSError, ValueError):
            pass
        elapsed = clock() - start
        if elapsed >= timeout_s:
            raise TimeoutError(f"vllm not ready after {elapsed:.0f}s")
        sleep(min(poll_s, max(timeout_s - elapsed, 0.0)))


# --- files ------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _files(out_dir: Path) -> list[Path]:
    return sorted((p for p in out_dir.rglob("*") if p.is_file()), key=lambda p: p.relative_to(out_dir).as_posix())


def write_sha256sums(out_dir: Path) -> Path:
    target = out_dir / "SHA256SUMS"
    lines = [f"{_sha256(p)}  {p.relative_to(out_dir).as_posix()}\n" for p in _files(out_dir) if p != target]
    target.write_text("".join(lines), encoding="utf-8")
    return target


def redact_tree(out_dir: Path, secret: str) -> None:
    needle = secret.encode()
    for path in _files(out_dir):
        data = path.read_bytes()
        if needle in data:
            path.write_bytes(data.replace(needle, REDACTED.encode()))


def find_secret(out_dir: Path, secret: str) -> list[str]:
    needle = secret.encode()
    return [p.relative_to(out_dir).as_posix() for p in _files(out_dir) if needle in p.read_bytes()]


def _snapshot(run: Callable[..., Any], argv: list[str], path: Path) -> dict[str, Any]:
    with path.open("wb") as fh:
        try:
            result = run(argv, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=120, check=False)
            return {"argv": argv, "returncode": result.returncode}
        except (OSError, subprocess.TimeoutExpired) as exc:
            fh.write(f"{type(exc).__name__}: {exc}\n".encode())
            return {"argv": argv, "error": type(exc).__name__}


# --- orchestration ----------------------------------------------------------


@dataclasses.dataclass
class Runtime:
    popen: Callable[..., Any] = subprocess.Popen
    run: Callable[..., Any] = subprocess.run
    transport: Transport = urllib_transport
    clock: Callable[[], float] = time.monotonic
    wall: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    killpg: Callable[[int, int], None] = os.killpg
    token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32)


def run_benchmark(args: argparse.Namespace, rt: Optional[Runtime] = None) -> int:
    rt = rt or Runtime()
    out = Path(args.output_dir)
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    api_key = rt.token_factory()
    client = Client(api_key=api_key, transport=rt.transport)
    cells = benchmark_cells(args.matrix)
    run_start = rt.clock()
    deadline = run_start + args.readiness_timeout_s + args.max_run_seconds
    meta: dict[str, Any] = {
        "schema": SCHEMA,
        "model_id": MODEL_ID,
        "model_revision": args.model_revision,
        "tokenizer_revision": args.model_revision,
        "vllm_image": args.vllm_image,
        "served_model_name": SERVED_MODEL_NAME,
        "matrix": args.matrix,
        "precision": args.precision,
        "tensor_parallel": TENSOR_PARALLEL,
        "host": HOST,
        "port": PORT,
        "api_key": "ephemeral-per-run, redacted",
        "serve_argv": redact_argv(serve_command(api_key, args.precision), api_key),
        "started_wall": rt.wall(),
        "status": "running",
        "error": None,
        "readiness_s": None,
        "snapshots": [],
        "warmups": [],
        "cells": [],
    }
    vllm_proc = gpu_proc = None
    sampler: Optional[MetricsSampler] = None
    handles: list[Any] = []
    try:
        meta["snapshots"].append(_snapshot(rt.run, ["nvidia-smi", "-q"], out / "nvidia-smi-q.txt"))
        meta["snapshots"].append(_snapshot(rt.run, ["nvidia-smi", "topo", "-m"], out / "topology.txt"))
        (out / "metrics.ndjson").touch()

        gpu_fh = (out / "gpu.csv").open("wb")
        handles.append(gpu_fh)
        bound_s = int(math.ceil(deadline - rt.clock())) + 60
        gpu_proc = rt.popen(
            ["timeout", f"{bound_s}s", "nvidia-smi", f"--query-gpu={GPU_QUERY}", "--format=csv", "--loop=1"],
            stdout=gpu_fh, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True,
        )

        log_fh = (out / "vllm.log").open("wb")
        handles.append(log_fh)
        vllm_proc = rt.popen(
            serve_command(api_key, args.precision),
            stdout=log_fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
        )
        meta["readiness_s"] = wait_for_ready(
            client, vllm_proc, timeout_s=args.readiness_timeout_s, clock=rt.clock, sleep=rt.sleep
        )

        sampler = MetricsSampler(
            client, out / "metrics.ndjson", interval_s=args.metrics_interval_s, deadline=deadline, clock=rt.clock, wall=rt.wall
        )
        sampler.start()

        req_fh = (out / "requests.ndjson").open("w", encoding="utf-8")
        handles.append(req_fh)

        def sink(record: dict[str, Any]) -> None:
            req_fh.write(json.dumps(record, sort_keys=True) + "\n")
            req_fh.flush()

        for cell in cells:
            if rt.clock() >= deadline:
                raise TimeoutError(f"run budget exhausted before cell {cell.name}")
            prompt = build_prompt_tokens(lambda text: tokenize(client, text), cell.input_tokens)

            def request_fn(index: int, cell: Cell = cell, prompt: list[int] = prompt) -> dict[str, Any]:
                return run_request(
                    client, cell_name=cell.name, index=index, prompt_tokens=prompt,
                    max_tokens=cell.output_tokens, clock=rt.clock, timeout=args.request_timeout_s,
                )

            warmup = request_fn(-1)
            meta["warmups"].append({"cell": cell.name, "status": warmup["status"], "error": warmup["error"]})
            meta["cells"].append(run_cell(cell, request_fn, sink, clock=rt.clock, secret=api_key))
        meta["status"] = "completed"
    except Exception as exc:
        meta["status"] = "failed"
        meta["error"] = redact(f"{type(exc).__name__}: {exc}", api_key)[:1000]
    except BaseException as exc:
        meta["status"] = "interrupted"
        meta["error"] = type(exc).__name__
        raise
    finally:
        if sampler is not None:
            sampler.stop_event.set()
            sampler.join(timeout=15)
        terminate_process(vllm_proc, killpg=rt.killpg)
        terminate_process(gpu_proc, grace_s=5.0, killpg=rt.killpg)
        for fh in handles:
            fh.close()
        meta["finished_wall"] = rt.wall()
        meta["elapsed_s"] = rt.clock() - run_start
        (out / "run.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        redact_tree(out, api_key)
        leaked = find_secret(out, api_key)
        write_sha256sums(out)
    if leaked:
        print(f"secret still present in {leaked}", file=sys.stderr)
        return 2
    return 0 if meta["status"] == "completed" else 1


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output-dir", required=True, help="new directory; must not exist")
    parser.add_argument("--matrix", required=True, choices=sorted(_FALLBACK_MATRIX))
    parser.add_argument("--precision", default="bf16", choices=sorted(PRECISION_FLAGS))
    parser.add_argument("--model-revision", required=True)
    parser.add_argument("--vllm-image", required=True)
    parser.add_argument("--readiness-timeout-s", type=float, default=float(READINESS_TIMEOUT_S))
    parser.add_argument("--max-run-seconds", type=float, default=5400.0)
    parser.add_argument("--request-timeout-s", type=float, default=600.0)
    parser.add_argument("--metrics-interval-s", type=float, default=1.0)
    args = parser.parse_args(argv)
    if args.model_revision != MODEL_REVISION:
        parser.error(f"--model-revision must be {MODEL_REVISION}")
    if args.vllm_image != VLLM_IMAGE:
        parser.error(f"--vllm-image must be {VLLM_IMAGE}")
    if not 0 <= args.readiness_timeout_s <= READINESS_TIMEOUT_S:
        parser.error(f"--readiness-timeout-s must be within [0, {READINESS_TIMEOUT_S}]")
    for name in ("max_run_seconds", "request_timeout_s", "metrics_interval_s"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    return args


def _raise_on_sigterm(signum: int, _frame: Any) -> None:
    raise SystemExit(128 + signum)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    signal.signal(signal.SIGTERM, _raise_on_sigterm)
    return run_benchmark(args)


if __name__ == "__main__":
    sys.exit(main())
