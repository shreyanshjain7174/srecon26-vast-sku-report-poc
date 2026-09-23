from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "remote_host_canary.sh"


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_remote_host_script_is_syntax_valid_and_inert_without_an_explicit_subcommand() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"]},
    )

    assert result.returncode == 64
    assert "Usage:" in result.stderr


def test_only_explicit_lifecycle_subcommands_are_exposed() -> None:
    script = _script()

    assert "<probe|install|deploy|collect|cleanup|inference-smoke>" in script
    for command in ("probe", "install", "deploy", "collect", "cleanup", "inference-smoke"):
        assert f"{command})" in script
    assert "*) usage >&2; exit 64" in script


def test_host_probe_fails_closed_on_required_kvm_gpu_and_runtime_capabilities() -> None:
    script = _script()

    for required in (
        "Ubuntu 22.04 is required",
        "systemd is unavailable",
        "/sys/fs/cgroup/cgroup.controllers",
        "systemd-detect-virt --vm",
        "nvidia-smi -L",
        "CUDA Version:",
        "docker info",
    ):
        assert required in script
    assert "nested_kvm_not_required=true" in script
    assert "-c /dev/kvm" not in script
    assert "probe-status.json" in script


def test_k3s_install_is_a_pinned_local_binary_install_not_a_network_pipe() -> None:
    script = _script()

    assert 'readonly K3S_VERSION="v1.36.4+k3s1"' in script
    assert "835873f37245fc615f547a2fe2af9402a347875f13fa64a1f136de644955ea3f" in script
    assert "v1.36.4%2Bk3s1/k3s" in script
    assert "K3S_BINARY_PATH" in script
    assert "sha256sum" in script
    assert not re.search(r"\bcurl\s*\|\s*(?:ba)?sh\b", script, flags=re.IGNORECASE)
    assert "K3S_BINARY_PATH must name a regular, pre-staged local file" in script


def test_deploy_rejects_tag_only_images_and_requires_a_checksumbound_bundle() -> None:
    script = _script()

    assert "CANARY_MANIFEST_SHA256" in script
    assert "manifest bundle checksum mismatch" in script
    assert "CANARY_VLLM_IMAGE must be an image pinned by immutable SHA256 digest" in script
    assert "tag-only container image found in manifest bundle" in script
    assert "CANARY_MODEL_REVISION must be frozen" in script
    assert "NVIDIA_RUNTIME_TEMPLATE" in script
    assert "runtimes.nvidia" in script


def test_all_waits_are_bounded_and_services_stay_cluster_or_local_only() -> None:
    script = _script()

    assert "CANARY_WAIT_SECONDS must be no greater than 900" in script
    assert "timeout --foreground" in script
    assert "rollout status" in script
    assert "--timeout=\"${wait_seconds}s\"" in script
    assert "--address 127.0.0.1" in script
    assert "a non-SSH wildcard listener is present; refusing deploy" in script
    assert "ufw allow" not in script
    assert "iptables -A" not in script


def test_evidence_contract_captures_every_required_observation_layer_without_secret_dump() -> None:
    script = _script()

    required_evidence = (
        "nodes.txt",
        "node-describe.txt",
        "device-plugin.txt",
        "vllm.txt",
        "vllm-logs.txt",
        "prometheus-targets.json",
        "prometheus-metrics.json",
        "resource-metrics.txt",
        "custom-metrics.txt",
        "hpa.txt",
        "events.txt",
        "warmup-timing.json",
        "measured-timing.json",
        "request-timing.json",
        "collection-status.json",
    )
    for filename in required_evidence:
        assert filename in script

    assert "kubectl get secrets" not in script
    assert "printenv" not in script
    assert "env >" not in script
    assert "redact_stream" in script


def test_deploy_runs_a_bounded_concurrent_pressure_phase_before_cleanup() -> None:
    script = _script()

    assert "run_pressure_load \"$out\"" in script
    assert "CANARY_HARD_DEADLINE" in script
    assert "CANARY_HARD_DEADLINE does not leave enough time for the bounded load phase and teardown margin" in script
    assert "MIN_HARD_DEADLINE_MARGIN_SECONDS=420" in script
    assert "CANARY_LOAD_SECONDS" in script
    assert "CANARY_LOAD_CONCURRENCY" in script
    assert "CANARY_LOAD_PROMPT_REPETITIONS" in script
    assert "CANARY_LOAD_MAX_TOKENS" in script
    assert "run_pressure_request \"$out\" during" in script
    assert "cleanup_pressure_processes" in script
    assert 'trap \'cleanup_pressure_processes' in script
    assert "' EXIT" in script


def test_pressure_phase_preserves_before_during_after_cpu_queue_kv_ttft_and_hpa_evidence() -> None:
    script = _script()

    for filename in (
        "pressure-${phase}-cpu.txt",
        "pressure-${phase}-hpa.json",
        "pressure-${phase}-ready.json",
        "pressure-${phase}-pods.json",
        "pressure-${phase}-queue-kv-ttft-prometheus.json",
    ):
        assert filename in script
    for phase in ("before", "during", "after"):
        assert f'capture_pressure_snapshot "$out" {phase}' in script

    assert "pressure-load-status.json" in script
    assert "ttft_seconds" in script
    assert "latency_seconds" in script
    assert "vllm:num_requests_waiting" in script
    assert "vllm:kv_cache_usage_perc" in script
    assert "vllm:time_to_first_token_seconds" in script


def test_cleanup_is_idempotent_and_scoped_to_the_checksumbound_bundle() -> None:
    script = _script()

    assert "kubectl_cmd delete --ignore-not-found --wait=true" in script
    assert "-f \"$CANARY_MANIFEST_DIR\"" in script
    assert "namespace remains after cleanup" in script
    assert "cleanup-status.json" in script


def test_direct_inference_smoke_is_a_pinned_localhost_only_non_k3s_path() -> None:
    script = _script()
    start = script.index("run_direct_inference_smoke()")
    end = script.index("\ndeploy_canary()", start)
    direct_smoke = script[start:end]

    assert "require_frozen_vllm_contract" in direct_smoke
    assert "docker image inspect" in direct_smoke
    assert "docker run --detach --rm" in direct_smoke
    assert '--publish "127.0.0.1:${local_port}:8000"' in direct_smoke
    assert "--revision \"$CANARY_MODEL_REVISION\"" in direct_smoke
    assert "--served-model-name \"$CANARY_MODEL\"" in direct_smoke
    assert "kubectl" not in direct_smoke
    assert "--network host" not in direct_smoke
    assert "direct inference container name is already in use" in direct_smoke
    assert "CANARY_INFERENCE_LOCAL_PORT is already listening" in direct_smoke
    assert 'capture_inference_public_listeners "$out" "$phase_deadline" before' in direct_smoke
    assert 'capture_inference_public_listeners "$out" "$phase_deadline" after' in direct_smoke
    assert "assert_no_new_inference_public_listener" in direct_smoke
    assert "direct inference introduced a new wildcard listener" in script


def test_direct_inference_evidence_contract_retains_raw_stream_timing_tokens_queue_and_gpu_samples() -> None:
    script = _script()

    for filename in (
        "inference-contract.json",
        "inference-image-inspect.json",
        "inference-gpu-identity.txt",
        "inference-cuda.txt",
        "inference-nvidia-smi-${phase}.csv",
        "inference-nvidia-compute-during.csv",
        "inference-vllm-models.json",
        "inference-${label}-body.ndjson",
        "inference-${label}-curl-timing.json",
        "inference-${label}-timing.json",
        "inference-metrics-${phase}.txt",
        "inference-queue-${phase}.txt",
        "inference-queue-${phase}-status.json",
        "inference-container-inspect.json",
        "inference-container-logs.txt",
        "inference-status.json",
    ):
        assert filename in script

    for required_measurement in (
        "stream_options: {include_usage: true}",
        "curl_time_starttransfer_first_stream_response_byte",
        ".prompt_tokens",
        ".completion_tokens",
        ".tpot_seconds",
        ".completion_tokens_per_second",
        ".generation_tokens_per_second",
        "capture_inference_gpu_snapshot \"$out\" before",
        "capture_inference_gpu_snapshot \"$out\" during",
        "capture_inference_gpu_snapshot \"$out\" after",
        "--query-compute-apps=pid,process_name,used_gpu_memory,gpu_uuid",
        "capture_inference_queue_metrics \"$out\" during",
    ):
        assert required_measurement in script


def test_direct_inference_fails_closed_before_the_immutable_teardown_margin() -> None:
    script = _script()

    assert "MIN_HARD_DEADLINE_MARGIN_SECONDS=420" in script
    assert "INFERENCE_CONTAINER_CLEANUP_RESERVE_SECONDS=15" in script
    assert "CANARY_HARD_DEADLINE does not leave enough time for the bounded direct inference phase and teardown margin" in script
    assert "CANARY_INFERENCE_PHASE_SECONDS must leave time to stop the direct inference container" in script
    assert "capture_inference_until_deadline" in script
    assert 'trap \'cleanup_inference_container "$out" "$container" "$phase_deadline" || true\' EXIT' in script


def test_local_tcp_ports_are_strictly_numeric_and_unprivileged() -> None:
    script = _script()

    assert '[[ "$value" =~ ^[0-9]+$ ]] && (( 10#$value >= 1024 && 10#$value <= 65535 ))' in script
    for variable in (
        "CANARY_LOCAL_PORT",
        "CANARY_PROMETHEUS_LOCAL_PORT",
        "CANARY_INFERENCE_LOCAL_PORT",
    ):
        assert f"require_local_tcp_port {variable}" in script


def test_direct_stream_request_fails_closed_without_an_http_200_matching_model_and_usage() -> None:
    script = _script()

    assert 'select(.http_code == 200 and .model == .response_model' in script
    assert 'response_model: $response_model' in script
    assert 'MISSING_STREAM_USAGE' in script
    assert 'MISSING_STREAM_MODEL' in script
