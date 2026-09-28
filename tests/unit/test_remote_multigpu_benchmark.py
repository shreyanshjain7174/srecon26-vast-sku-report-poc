from __future__ import annotations

import hashlib
import importlib.util
import json
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from srecon26_poc import multigpu_contracts as contracts

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "remote_multigpu_benchmark.py"
SECRET = "sk-test-secret-DO-NOT-LEAK-0123456789"


def _load_module() -> Any:
    name = "remote_multigpu_benchmark"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bench = _load_module()


# --- fakes ------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status: int, lines: list[bytes] | None = None, body: bytes = b"", on_line: Any = None) -> None:
        self.status = status
        self._lines = lines or []
        self._body = body
        self._on_line = on_line
        self.closed = False

    def lines(self):
        for line in self._lines:
            if self._on_line is not None:
                self._on_line(line)
            yield line

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        self.closed = True


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _sse(payload: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(payload).encode() + b"\n"


def _stream_lines(n_tokens: int, prompt_tokens: int) -> list[bytes]:
    lines = [_sse({"choices": [{"text": f"t{i}", "index": 0}]}) for i in range(n_tokens)]
    lines.append(b"\n")
    lines.append(
        _sse({"choices": [], "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": n_tokens, "total_tokens": prompt_tokens + n_tokens}})
    )
    lines.append(b"data: [DONE]\n")
    return lines


# --- constants and argv -----------------------------------------------------


def test_pins_match_contracts() -> None:
    assert bench.MODEL_ID == contracts.MODEL_ID
    assert bench.MODEL_REVISION == contracts.MODEL_REVISION
    assert bench.VLLM_IMAGE == contracts.VLLM_IMAGE
    assert bench.MAX_MODEL_LEN == contracts.MAX_MODEL_LEN
    assert bench.MAX_NUM_SEQS == contracts.MAX_NUM_SEQS
    assert bench.TENSOR_PARALLEL == contracts.REQUIRED_TENSOR_PARALLEL


def test_serve_command_exact_argv() -> None:
    rev = contracts.MODEL_REVISION
    assert bench.serve_command(SECRET) == [
        "vllm", "serve", "Qwen/Qwen3.8-27B",
        "--revision", rev,
        "--tokenizer-revision", rev,
        "--tensor-parallel-size", "8",
        "--dtype", "bfloat16",
        "--max-model-len", "4096",
        "--max-num-seqs", "64",
        "--gpu-memory-utilization", "0.90",
        "--seed", "0",
        "--host", "127.0.0.1",
        "--port", "8000",
        "--served-model-name", "qwen38-27b",
        "--api-key", SECRET,
    ]



@pytest.mark.parametrize(
    ("precision", "flags"),
    [("fp8", ["--quantization", "fp8"]), ("fp8-kv", ["--quantization", "fp8", "--kv-cache-dtype", "fp8"])],
)
def test_serve_command_precision_arms_only_add_quantization_flags(precision: str, flags: list[str]) -> None:
    base = bench.serve_command(SECRET)
    argv = bench.serve_command(SECRET, precision)
    at = base.index("--max-model-len")
    assert argv == base[:at] + flags + base[at:]


def test_serve_command_rejects_unknown_precision() -> None:
    with pytest.raises(ValueError, match="unknown precision"):
        bench.serve_command(SECRET, "int4")

@pytest.mark.parametrize("bad", ["", "has space", "tab\tkey", "new\nline", "-leading-dash"])
def test_serve_command_rejects_bad_api_key(bad: str) -> None:
    with pytest.raises(ValueError):
        bench.serve_command(bad)


def test_redact_argv_hides_api_key() -> None:
    argv = bench.redact_argv(bench.serve_command(SECRET), SECRET)
    assert SECRET not in " ".join(argv)
    assert argv[-2:] == ["--api-key", bench.REDACTED]


# --- matrix -----------------------------------------------------------------


def _as_tuples(cells: Any) -> list[tuple[Any, ...]]:
    return [(c.name, c.input_tokens, c.output_tokens, c.concurrency, c.num_requests) for c in cells]


@pytest.mark.parametrize("matrix", ["smoke", "full"])
def test_benchmark_cells_match_contracts(matrix: str) -> None:
    expected = _as_tuples(contracts.BenchmarkMatrix.for_name(matrix).cells)
    assert _as_tuples(bench.benchmark_cells(matrix)) == expected
    assert _as_tuples(bench._FALLBACK_MATRIX[matrix]) == expected


def test_benchmark_cells_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        bench.benchmark_cells("nightly")


def test_benchmark_cells_fallback_without_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "srecon26_poc.multigpu_contracts", None)
    assert _as_tuples(bench.benchmark_cells("full")) == _as_tuples(bench._FALLBACK_MATRIX["full"])
    with pytest.raises(ValueError):
        bench.benchmark_cells("nightly")


# --- percentile math --------------------------------------------------------


def test_percentile_linear_interpolation() -> None:
    values = [float(v) for v in range(10, 0, -1)]
    assert bench.percentile(values, 50) == pytest.approx(5.5)
    assert bench.percentile(values, 90) == pytest.approx(9.1)
    assert bench.percentile(values, 99) == pytest.approx(9.91)
    assert bench.percentile([3.0], 99) == 3.0
    assert bench.percentile([], 50) is None


# --- prompt -----------------------------------------------------------------


def test_build_prompt_tokens_exact_and_deterministic() -> None:
    seen: list[str] = []

    def tokenize(text: str) -> list[int]:
        seen.append(text)
        return [7, 8, 9]

    first = bench.build_prompt_tokens(tokenize, 10)
    second = bench.build_prompt_tokens(tokenize, 10)
    assert first == second == [7, 8, 9, 7, 8, 9, 7, 8, 9, 7]
    assert seen[0] == seen[1]
    assert seen[0].startswith(bench.PROMPT_SEED_TEXT)


def test_build_prompt_tokens_rejects_empty_tokenizer() -> None:
    with pytest.raises(RuntimeError):
        bench.build_prompt_tokens(lambda _text: [], 4)


def test_tokenize_posts_to_loopback_with_auth() -> None:
    calls: list[tuple[Any, ...]] = []

    def transport(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> FakeResponse:
        calls.append((method, url, headers, json.loads(body or b"{}")))
        return FakeResponse(200, body=json.dumps({"tokens": [1, 2, 3], "count": 3}).encode())

    client = bench.Client(api_key=SECRET, transport=transport)
    assert bench.tokenize(client, "hello") == [1, 2, 3]
    method, url, headers, payload = calls[0]
    assert (method, url) == ("POST", "http://127.0.0.1:8000/tokenize")
    assert headers["Authorization"] == f"Bearer {SECRET}"
    assert payload == {"model": "qwen38-27b", "prompt": "hello", "add_special_tokens": False}
    assert SECRET not in repr(client)


def test_client_rejects_non_loopback_base_url() -> None:
    with pytest.raises(ValueError):
        bench.Client(api_key=SECRET, base_url="http://0.0.0.0:8000")
    with pytest.raises(ValueError):
        bench.Client(api_key=SECRET, base_url="http://example.com:8000")


# --- run_request ------------------------------------------------------------


def test_run_request_success_records_timings_and_usage() -> None:
    clock = FakeClock()
    captured: dict[str, Any] = {}

    def on_line(_line: bytes) -> None:
        clock.now += 0.25

    def transport(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> FakeResponse:
        captured.update(method=method, url=url, headers=headers, payload=json.loads(body or b"{}"), timeout=timeout)
        return FakeResponse(200, lines=_stream_lines(4, 6), on_line=on_line)

    client = bench.Client(api_key=SECRET, transport=transport)
    record = bench.run_request(client, cell_name="short-c1", index=3, prompt_tokens=[5] * 6, max_tokens=4, clock=clock, timeout=12.0)

    assert captured["method"] == "POST"
    assert captured["url"] == "http://127.0.0.1:8000/v1/completions"
    assert captured["timeout"] == 12.0
    assert captured["payload"] == {
        "model": "qwen38-27b",
        "prompt": [5] * 6,
        "max_tokens": 4,
        "temperature": 0,
        "ignore_eos": True,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    assert record["status"] == "ok"
    assert record["http_status"] == 200
    assert record["cell"] == "short-c1" and record["index"] == 3
    assert record["ttft_s"] == pytest.approx(0.25)
    # 4 token chunks + blank + usage + [DONE] = 7 lines at 0.25s each.
    assert record["e2e_s"] == pytest.approx(0.25 * 7)
    assert record["tpot_s"] == pytest.approx((1.75 - 0.25) / 3)
    assert record["prompt_tokens"] == 6 and record["completion_tokens"] == 4
    assert record["error"] is None
    assert SECRET not in json.dumps(record)


def test_run_request_http_error_redacts_key() -> None:
    def transport(*_args: Any) -> FakeResponse:
        return FakeResponse(401, body=f"bad key {SECRET}".encode())

    client = bench.Client(api_key=SECRET, transport=transport)
    record = bench.run_request(client, cell_name="c", index=0, prompt_tokens=[1], max_tokens=2, clock=FakeClock())
    assert record["status"] == "error"
    assert record["http_status"] == 401
    assert "401" in record["error"]
    assert SECRET not in json.dumps(record)
    assert bench.REDACTED in record["error"]


def test_run_request_transport_exception_redacted() -> None:
    def transport(*_args: Any) -> FakeResponse:
        raise OSError(f"connection refused while sending {SECRET}")

    client = bench.Client(api_key=SECRET, transport=transport)
    record = bench.run_request(client, cell_name="c", index=0, prompt_tokens=[1], max_tokens=2, clock=FakeClock())
    assert record["status"] == "error"
    assert record["http_status"] is None
    assert "OSError" in record["error"]
    assert SECRET not in json.dumps(record)


@pytest.mark.parametrize(
    ("lines", "reason"),
    [
        (_stream_lines(2, 1)[:-1], "[DONE]"),
        ([_sse({"choices": [{"text": "a"}]}), b"data: [DONE]\n"], "usage"),
    ],
)
def test_run_request_incomplete_stream_is_error(lines: list[bytes], reason: str) -> None:
    client = bench.Client(api_key=SECRET, transport=lambda *_a: FakeResponse(200, lines=lines))
    record = bench.run_request(client, cell_name="c", index=0, prompt_tokens=[1], max_tokens=2, clock=FakeClock())
    assert record["status"] == "error"
    assert reason in record["error"]


# --- run_cell ---------------------------------------------------------------


def test_run_cell_fixed_concurrency_and_summary() -> None:
    cell = bench.Cell("short-c8", 1024, 256, 4, 12)
    lock = threading.Lock()
    in_flight = 0
    peak = 0

    def request_fn(index: int) -> dict[str, Any]:
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        time.sleep(0.02)
        with lock:
            in_flight -= 1
        if index == 5:
            return {"cell": cell.name, "index": index, "status": "error", "ttft_s": None, "e2e_s": None, "tpot_s": None, "completion_tokens": None}
        if index == 7:
            raise RuntimeError(f"boom {SECRET}")
        return {
            "cell": cell.name, "index": index, "status": "ok",
            "ttft_s": 0.1 * (index + 1), "e2e_s": 1.0 + index, "tpot_s": 0.01,
            "completion_tokens": 256 if index != 0 else 255,
        }

    sink: list[dict[str, Any]] = []
    ticks = iter([10.0, 14.0])
    summary = bench.run_cell(cell, request_fn, sink.append, clock=lambda: next(ticks), secret=SECRET)

    assert peak == 4
    assert sorted(r["index"] for r in sink) == list(range(12))
    assert SECRET not in json.dumps(sink)
    assert summary["cell"] == "short-c8"
    assert summary["requests"] == 12
    assert summary["successes"] == 10
    assert summary["failures"] == 2
    assert summary["output_token_mismatches"] == 1
    assert summary["duration_s"] == pytest.approx(4.0)
    assert summary["req_per_s"] == pytest.approx(10 / 4.0)
    assert summary["output_tok_per_s"] == pytest.approx((256 * 9 + 255) / 4.0)
    ok_e2e = [1.0 + i for i in range(12) if i not in (5, 7)]
    assert summary["e2e_p50_s"] == pytest.approx(bench.percentile(ok_e2e, 50))
    assert summary["e2e_p99_s"] == pytest.approx(bench.percentile(ok_e2e, 99))
    assert summary["tpot_p90_s"] == pytest.approx(0.01)
    assert set(summary) >= {f"{m}_p{p}_s" for m in ("ttft", "e2e", "tpot") for p in (50, 90, 99)}


# --- prometheus -------------------------------------------------------------


PROM_TEXT = """\
# HELP vllm:num_requests_waiting Number of requests waiting.
# TYPE vllm:num_requests_waiting gauge
vllm:num_requests_waiting{engine="0",model_name="qwen38-27b"} 3.0
vllm:num_requests_running{engine="0",model_name="qwen38-27b"} 8.0
vllm:num_requests_running{engine="1",model_name="qwen38-27b"} 2.0
vllm:kv_cache_usage_perc{engine="0",model_name="qwen38-27b"} 0.42
vllm:kv_cache_usage_perc{engine="1",model_name="qwen38-27b"} 0.30
vllm:prompt_tokens_total{model_name="a b"} 1234 1700000000000
garbage line here
"""


def test_parse_prometheus_groups_by_name() -> None:
    parsed = bench.parse_prometheus(PROM_TEXT)
    assert parsed["vllm:num_requests_running"] == [8.0, 2.0]
    assert parsed["vllm:prompt_tokens_total"] == [1234.0]
    assert "garbage" not in parsed


def test_sample_metrics_new_and_legacy_kv_names() -> None:
    assert bench.sample_metrics(PROM_TEXT) == {"waiting": 3.0, "running": 10.0, "kv_cache_usage": 0.42}
    legacy = "vllm:gpu_cache_usage_perc 0.75\n"
    assert bench.sample_metrics(legacy) == {"waiting": None, "running": None, "kv_cache_usage": 0.75}


def test_sample_once_records_error_without_key() -> None:
    def transport(*_args: Any) -> FakeResponse:
        raise OSError(f"refused {SECRET}")

    client = bench.Client(api_key=SECRET, transport=transport)
    record = bench.sample_once(client, clock=lambda: 1.0, wall=lambda: 2.0)
    assert record["t_mono"] == 1.0 and record["t_wall"] == 2.0
    assert SECRET not in json.dumps(record)
    assert "OSError" in record["error"]


# --- files ------------------------------------------------------------------


def test_write_sha256sums_excludes_itself(tmp_path: Path) -> None:
    (tmp_path / "run.json").write_text("{}\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_bytes(b"bee")
    (tmp_path / "SHA256SUMS").write_text("stale\n")
    target = bench.write_sha256sums(tmp_path)
    lines = target.read_text().splitlines()
    assert lines == [
        f"{hashlib.sha256(b'{}' + bytes([10])).hexdigest()}  run.json",
        f"{hashlib.sha256(b'bee').hexdigest()}  sub/b.txt",
    ]


def test_redact_tree_and_find_secret(tmp_path: Path) -> None:
    (tmp_path / "vllm.log").write_text(f"args: api_key=['{SECRET}']\n")
    (tmp_path / "clean.txt").write_text("nothing\n")
    assert bench.find_secret(tmp_path, SECRET) == ["vllm.log"]
    bench.redact_tree(tmp_path, SECRET)
    assert bench.find_secret(tmp_path, SECRET) == []
    assert bench.REDACTED in (tmp_path / "vllm.log").read_text()


# --- process termination ----------------------------------------------------


class FakeProc:
    def __init__(self, pid: int, ignores_term: bool = False) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.ignores_term = ignores_term
        self.signals: list[int] = []

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout or 0)
        return self.returncode

    def deliver(self, sig: int) -> None:
        self.signals.append(sig)
        if sig == signal.SIGKILL or not self.ignores_term:
            self.returncode = -sig


def test_terminate_process_escalates_to_sigkill() -> None:
    proc = FakeProc(4242, ignores_term=True)
    calls: list[tuple[int, int]] = []

    def killpg(pid: int, sig: int) -> None:
        calls.append((pid, sig))
        proc.deliver(sig)

    bench.terminate_process(proc, grace_s=0.01, killpg=killpg)
    assert calls == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert proc.returncode == -signal.SIGKILL


def test_terminate_process_noop_when_exited() -> None:
    proc = FakeProc(1)
    proc.returncode = 0
    calls: list[Any] = []
    bench.terminate_process(proc, killpg=lambda *a: calls.append(a))
    bench.terminate_process(None, killpg=lambda *a: calls.append(a))
    assert calls == []


# --- readiness --------------------------------------------------------------


def test_wait_for_ready_times_out() -> None:
    clock = FakeClock()

    def sleep(seconds: float) -> None:
        clock.now += seconds

    client = bench.Client(api_key=SECRET, transport=lambda *_a: FakeResponse(503))
    with pytest.raises(TimeoutError):
        bench.wait_for_ready(client, FakeProc(1), timeout_s=30, clock=clock, sleep=sleep, poll_s=5)
    assert clock.now - 100.0 == pytest.approx(30)


def test_wait_for_ready_fails_fast_when_vllm_exits() -> None:
    proc = FakeProc(1)
    proc.returncode = 3
    client = bench.Client(api_key=SECRET, transport=lambda *_a: FakeResponse(503))
    with pytest.raises(RuntimeError, match="exited"):
        bench.wait_for_ready(client, proc, timeout_s=30, clock=FakeClock(), sleep=lambda _s: None)


def test_parse_args_rejects_bad_pins_and_long_readiness(tmp_path: Path) -> None:
    base = ["--output-dir", str(tmp_path / "o"), "--matrix", "smoke", "--model-revision", bench.MODEL_REVISION, "--vllm-image", bench.VLLM_IMAGE]
    assert bench.parse_args(base).matrix == "smoke"
    with pytest.raises(SystemExit):
        bench.parse_args(base[:-1] + ["docker.io/vllm/vllm-openai:latest"])
    with pytest.raises(SystemExit):
        bench.parse_args(base[:5] + ["deadbeef"] + base[6:])
    with pytest.raises(SystemExit):
        bench.parse_args(base + ["--readiness-timeout-s", "1801"])
    assert not any("api" in a for a in vars(bench.parse_args(base)))


# --- end to end with fakes --------------------------------------------------


class Harness:
    def __init__(self, ready: bool = True) -> None:
        self.ready = ready
        self.procs: dict[int, FakeProc] = {}
        self.popen_calls: list[tuple[list[str], dict[str, Any]]] = []
        self.run_calls: list[list[str]] = []
        self.killpg_calls: list[tuple[int, int]] = []
        self.urls: set[str] = set()
        self.bad_auth = 0
        self._next_pid = 9000

    def popen(self, argv: list[str], **kwargs: Any) -> FakeProc:
        self.popen_calls.append((list(argv), kwargs))
        out = kwargs["stdout"]
        if argv[:2] == ["vllm", "serve"]:
            out.write(f"INFO non-default args: {{'api_key': ['{argv[-1]}']}}\n".encode())
        else:
            out.write(b"timestamp, index, name\n")
        out.flush()
        self._next_pid += 1
        proc = FakeProc(self._next_pid)
        self.procs[proc.pid] = proc
        return proc

    def run(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.run_calls.append(list(argv))
        kwargs["stdout"].write(f"fake {' '.join(argv)}\n".encode())
        return subprocess.CompletedProcess(argv, 0)

    def killpg(self, pid: int, sig: int) -> None:
        self.killpg_calls.append((pid, sig))
        self.procs[pid].deliver(sig)

    def transport(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> FakeResponse:
        assert url.startswith("http://127.0.0.1:8000/")
        path = url.removeprefix("http://127.0.0.1:8000")
        self.urls.add(path)
        if headers.get("Authorization") != f"Bearer {SECRET}":
            self.bad_auth += 1
        if path == "/health":
            return FakeResponse(200 if self.ready else 503)
        if path == "/v1/models":
            return FakeResponse(200, body=json.dumps({"data": [{"id": "qwen38-27b"}]}).encode())
        if path == "/tokenize":
            return FakeResponse(200, body=json.dumps({"tokens": [11, 12, 13, 14]}).encode())
        if path == "/metrics":
            return FakeResponse(200, body=PROM_TEXT.encode())
        if path == "/v1/completions":
            payload = json.loads(body or b"{}")
            return FakeResponse(200, lines=_stream_lines(payload["max_tokens"], len(payload["prompt"])))
        return FakeResponse(404)

    def runtime(self) -> Any:
        return bench.Runtime(
            popen=self.popen,
            run=self.run,
            transport=self.transport,
            sleep=lambda _s: None,
            killpg=self.killpg,
            token_factory=lambda: SECRET,
        )


def _args(out: Path, **extra: str) -> Any:
    argv = [
        "--output-dir", str(out), "--matrix", "smoke",
        "--model-revision", bench.MODEL_REVISION, "--vllm-image", bench.VLLM_IMAGE,
        "--metrics-interval-s", "0.01",
    ]
    for key, value in extra.items():
        argv += [f"--{key.replace('_', '-')}", value]
    return bench.parse_args(argv)


def test_run_benchmark_end_to_end_with_fakes(tmp_path: Path) -> None:
    out = tmp_path / "run"
    harness = Harness()
    assert bench.run_benchmark(_args(out), harness.runtime()) == 0

    names = sorted(p.name for p in out.iterdir())
    assert names == sorted(
        ["run.json", "requests.ndjson", "metrics.ndjson", "gpu.csv", "nvidia-smi-q.txt", "topology.txt", "vllm.log", "SHA256SUMS"]
    )
    assert bench.find_secret(out, SECRET) == []
    assert harness.bad_auth == 0
    assert {"/health", "/v1/models", "/tokenize", "/v1/completions", "/metrics"} <= harness.urls

    vllm_argv, vllm_kwargs = next(c for c in harness.popen_calls if c[0][:2] == ["vllm", "serve"])
    assert vllm_argv == bench.serve_command(SECRET)
    assert vllm_kwargs["start_new_session"] is True
    assert "shell" not in vllm_kwargs
    gpu_argv, _ = next(c for c in harness.popen_calls if "nvidia-smi" in c[0])
    assert gpu_argv[0] == "timeout" and "--format=csv" in gpu_argv
    assert ["nvidia-smi", "-q"] in harness.run_calls
    assert ["nvidia-smi", "topo", "-m"] in harness.run_calls
    assert all(p.returncode is not None for p in harness.procs.values())

    run = json.loads((out / "run.json").read_text())
    assert run["status"] == "completed"
    assert run["precision"] == "bf16"
    assert run["model_revision"] == bench.MODEL_REVISION
    assert run["vllm_image"] == bench.VLLM_IMAGE
    assert run["serve_argv"][-1] == bench.REDACTED
    assert [c["cell"] for c in run["cells"]] == ["short-c1", "short-c8"]
    assert all(c["failures"] == 0 and c["output_token_mismatches"] == 0 for c in run["cells"])

    requests = [json.loads(line) for line in (out / "requests.ndjson").read_text().splitlines()]
    assert len(requests) == 32 + 64
    assert {r["input_tokens"] for r in requests} == {1024}
    metrics = [json.loads(line) for line in (out / "metrics.ndjson").read_text().splitlines()]
    assert metrics and metrics[0]["running"] == 10.0

    sums = (out / "SHA256SUMS").read_text().splitlines()
    listed = {line.split("  ", 1)[1] for line in sums}
    assert listed == set(names) - {"SHA256SUMS"}
    for line in sums:
        digest, rel = line.split("  ", 1)
        assert hashlib.sha256((out / rel).read_bytes()).hexdigest() == digest


def test_run_benchmark_refuses_existing_output_dir(tmp_path: Path) -> None:
    out = tmp_path / "run"
    out.mkdir()
    harness = Harness()
    with pytest.raises(FileExistsError):
        bench.run_benchmark(_args(out), harness.runtime())
    assert harness.popen_calls == []


def test_run_benchmark_readiness_failure_cleans_up(tmp_path: Path) -> None:
    out = tmp_path / "run"
    harness = Harness(ready=False)
    assert bench.run_benchmark(_args(out, readiness_timeout_s="0"), harness.runtime()) == 1
    run = json.loads((out / "run.json").read_text())
    assert run["status"] == "failed"
    assert "TimeoutError" in run["error"]
    assert all(p.returncode is not None for p in harness.procs.values())
    assert {sig for _pid, sig in harness.killpg_calls} == {signal.SIGTERM}
    assert bench.find_secret(out, SECRET) == []
    assert (out / "SHA256SUMS").exists()


def test_run_benchmark_fp8_arm_serves_and_records_precision(tmp_path: Path) -> None:
    out = tmp_path / "fp8"
    harness = Harness()
    assert bench.run_benchmark(_args(out, precision="fp8"), harness.runtime()) == 0

    vllm_argv, _ = next(c for c in harness.popen_calls if c[0][:2] == ["vllm", "serve"])
    assert vllm_argv == bench.serve_command(SECRET, "fp8")
    run = json.loads((out / "run.json").read_text())
    assert run["precision"] == "fp8"
    assert "--quantization" in run["serve_argv"]


def test_parse_args_rejects_unknown_precision(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        _args(tmp_path / "o", precision="int4")
