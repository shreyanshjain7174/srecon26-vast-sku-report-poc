from __future__ import annotations

import json
import sys
import types
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from srecon26_poc.contracts import InstanceContract
from srecon26_poc.canary import VLLM_IMAGE_DIGEST
from srecon26_poc.guard_client import GuardClient
from srecon26_poc.live_factory import (
    GitHubGuardConfig,
    GitHubGuardTransport,
    LiveFactoryError,
    ProviderStartupFault,
    SshRemoteWorkload,
    StartupStatusObservation,
    VastSshResolver,
    _load_report_gate,
)
from srecon26_poc.live_dispatch import FROZEN_MODEL_ID, FROZEN_MODEL_REVISION, REPORT_MARGIN, WorkloadContract
from srecon26_poc.reporting import ReportGate
from srecon26_poc.types import RunIdentity


NOW = datetime(2026, 9, 23, 4, 0, tzinfo=UTC)
NONCE = "nonce_12345678"
LABEL = f"srecon26-smoke--nonce-{NONCE}"


def test_live_factory_rejects_report_adapter_without_authenticated_session_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    class OldAdapter:
        def preflight_exact_instance(self, instance_id, label): pass
        def capture_before(self, fault): pass
        def submit(self, fault): return False
        def capture_after(self, receipt): pass

    module = types.SimpleNamespace(create=lambda: OldAdapter())
    monkeypatch.setitem(sys.modules, "old_report_adapter", module)

    with pytest.raises(LiveFactoryError, match="exact-target adapter"):
        _load_report_gate("old_report_adapter:create")

    module.create = lambda: ReportGate(OldAdapter())
    with pytest.raises(LiveFactoryError, match="authenticated-session preflight"):
        _load_report_gate("old_report_adapter:create")


def test_direct_inference_evidence_is_bound_to_exact_instance_and_raw_measurement(tmp_path: Path) -> None:
    evidence = tmp_path / "remote-evidence"
    transport = tmp_path / "remote-transport"
    manifests = tmp_path / "manifests"
    evidence.mkdir()
    transport.mkdir()
    manifests.mkdir()
    (manifests / "vllm.yaml").write_text("REQUIRED_AT_RUN_TIME_MODEL REQUIRED_AT_RUN_TIME_REVISION", encoding="utf-8")
    workload = WorkloadContract(FROZEN_MODEL_ID, FROZEN_MODEL_REVISION, VLLM_IMAGE_DIGEST)
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 145338, Decimal("0.2975"), LABEL)
    (evidence / "probe-status.json").write_text('{"status":"PASSED"}', encoding="utf-8")
    (evidence / "inference-status.json").write_text(
        json.dumps(
            {
                "status": "PASSED",
                "image": f"docker.io/vllm/vllm-openai@{VLLM_IMAGE_DIGEST}",
                "model": FROZEN_MODEL_ID,
                "model_revision": FROZEN_MODEL_REVISION,
                "failed_requests": 0,
            }
        ),
        encoding="utf-8",
    )
    (evidence / "inference-image-inspect.json").write_text(json.dumps([f"vllm/vllm-openai@{VLLM_IMAGE_DIGEST}"]), encoding="utf-8")
    (evidence / "inference-container-inspect.json").write_text(json.dumps({"image": f"docker.io/vllm/vllm-openai@{VLLM_IMAGE_DIGEST}", "command": ["--model", FROZEN_MODEL_ID, "--revision", FROZEN_MODEL_REVISION]}), encoding="utf-8")
    (evidence / "inference-vllm-models.json").write_text(json.dumps({"data": [{"id": FROZEN_MODEL_ID}]}), encoding="utf-8")
    (evidence / "inference-gpu-identity.txt").write_text("GPU 0: NVIDIA GeForce RTX 3090", encoding="utf-8")
    (evidence / "inference-cuda.txt").write_text("CUDA Version: 12.8", encoding="utf-8")
    usage = {"prompt_tokens": 32, "completion_tokens": 64, "total_tokens": 96}
    (evidence / "inference-request-1-timing.json").write_text(
        json.dumps(
            {
                "status": "PASSED",
                "request": "request-1",
                "http_code": 200,
                "model": FROZEN_MODEL_ID,
                "response_model": FROZEN_MODEL_ID,
                "usage": usage,
                **usage,
                "ttft_seconds": 0.12,
                "e2e_seconds": 1.4,
                "tpot_seconds": 0.02,
                "generation_tokens_per_second": 49.2,
            }
        ),
        encoding="utf-8",
    )
    (evidence / "inference-request-1-body.ndjson").write_text(
        f"data: {json.dumps({'model': FROZEN_MODEL_ID, 'usage': usage})}\n"
        "data: [DONE]\n",
        encoding="utf-8",
    )
    (evidence / "inference-nvidia-smi-before.csv").write_text(
        "2026/09/23 04:00:00, NVIDIA GeForce RTX 3090, GPU-123, 535.1, 0, 0, 100, 24576, 80\n",
        encoding="utf-8",
    )
    (evidence / "inference-nvidia-smi-during.csv").write_text(
        "2026/09/23 04:00:02, NVIDIA GeForce RTX 3090, GPU-123, 535.1, 87, 20, 2048, 24576, 250\n",
        encoding="utf-8",
    )
    (evidence / "inference-nvidia-smi-after.csv").write_text(
        "2026/09/23 04:00:04, NVIDIA GeForce RTX 3090, GPU-123, 535.1, 0, 0, 2048, 24576, 90\n",
        encoding="utf-8",
    )
    (evidence / "inference-nvidia-compute-during.csv").write_text(
        "1234, python, 2048, GPU-123\n",
        encoding="utf-8",
    )
    files = tuple(path for path in evidence.rglob("*") if path.is_file())
    remote = object.__new__(SshRemoteWorkload)
    remote.config = types.SimpleNamespace(local_manifest_dir=manifests)

    observed = remote._evidence("inference-smoke", evidence, transport, workload, files, run_id="inference-run-1", instance=instance)

    complete, blockers = observed.complete_for("inference-smoke", now=datetime.now(UTC), workload=workload, run_id="inference-run-1", instance=instance)
    assert complete is True
    assert blockers == ()
    assert observed.inference_measurement is not None
    measurement = json.loads(observed.inference_measurement.artifact.read_text(encoding="utf-8"))
    assert (measurement["run_id"], measurement["instance_id"], measurement["label"]) == ("inference-run-1", 417, LABEL)
    assert measurement["request"]["output_tokens"] == 64
    assert measurement["hardware_attribution"]["gpu_uuid"] == "GPU-123"
    assert measurement["hardware_attribution"]["active_compute_processes"] == 1


@pytest.mark.parametrize(
    ("artifact", "replacement"),
    (
        ("inference-nvidia-compute-during.csv", ""),
        ("inference-nvidia-compute-during.csv", "1234, python, 2048, GPU-other\n"),
        ("inference-request-1-timing.json", json.dumps({"status": "PASSED", "request": "request-1", "http_code": 201})),
        (
            "inference-request-1-timing.json",
            json.dumps(
                {
                    "status": "PASSED",
                    "request": "request-1",
                    "http_code": 200,
                    "model": FROZEN_MODEL_ID,
                    "response_model": "other-model",
                    "usage": {"prompt_tokens": 32, "completion_tokens": 64, "total_tokens": 96},
                    "prompt_tokens": 32,
                    "completion_tokens": 64,
                    "total_tokens": 96,
                    "ttft_seconds": 0.12,
                    "e2e_seconds": 1.4,
                }
            ),
        ),
        ("inference-nvidia-smi-during.csv", "2026/09/23 04:00:02, NVIDIA GeForce RTX 3090, GPU-123, 535.1, 0, 0, 100, 24576, 80\n"),
    ),
)
def test_direct_inference_measurement_is_not_emitted_without_request_or_hardware_attribution(
    tmp_path: Path, artifact: str, replacement: str
) -> None:
    # Build the fully attested shape first, then invalidate exactly one link in
    # the request-to-GPU evidence chain.
    test_direct_inference_evidence_is_bound_to_exact_instance_and_raw_measurement(tmp_path)
    evidence = tmp_path / "remote-evidence"
    (evidence / artifact).write_text(replacement, encoding="utf-8")
    (evidence / "direct-inference-measurement.json").unlink()
    workload = WorkloadContract(FROZEN_MODEL_ID, FROZEN_MODEL_REVISION, VLLM_IMAGE_DIGEST)
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 145338, Decimal("0.2975"), LABEL)
    remote = object.__new__(SshRemoteWorkload)
    remote.config = types.SimpleNamespace(local_manifest_dir=tmp_path / "manifests")

    observed = remote._evidence(
        "inference-smoke",
        evidence,
        tmp_path / "remote-transport",
        workload,
        tuple(path for path in evidence.rglob("*") if path.is_file()),
        run_id="inference-run-1",
        instance=instance,
    )

    assert observed.inference_measurement is None

class GitHubRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.ack_anchors = True
        self.comments: list[dict[str, object]] = [
            {
                "body": f"SRECON26_GUARD_V1 ARMED nonce={NONCE} label={LABEL} deadline=2026-09-23T04:20:00Z host=github-runner-1 script={'a' * 64} root={'b' * 64}",
                "created_at": "2026-09-23T04:00:01Z",
                "user": {"login": "github-actions[bot]"},
            },
            {
                "body": f"SRECON26_GUARD_V1 BACKSTOP_ARMED nonce={NONCE} label={LABEL} deadline=2026-09-23T04:20:00Z host=github-runner-2 script={'c' * 64} root={'d' * 64}",
                "created_at": "2026-09-23T04:00:02Z",
                "user": {"login": "github-actions[bot]"},
            },
        ]

    def __call__(self, arguments, *, timeout: int) -> str:
        del timeout
        command = list(arguments)
        self.calls.append(command)
        endpoint = command[4]
        if "/comments?per_page=100&since=" in endpoint:
            return json.dumps(self.comments)
        if endpoint.endswith("/comments"):
            body = next((item.split("=", 1)[1] for item in command if item.startswith("body=")), "")
            anchor = body.replace("SRECON26_GUARD_V1 ANCHOR", "SRECON26_GUARD_V1 ANCHORED", 1)
            if anchor != body and self.ack_anchors:
                self.comments.append(
                    {
                        "body": anchor,
                        "created_at": "2026-09-23T04:00:03Z",
                        "user": {"login": "github-actions[bot]"},
                    }
                )
            return json.dumps({"id": 99})
        return ""


def test_github_guard_dispatch_verifies_bound_action_attestation_then_comments_heartbeats() -> None:
    runner = GitHubRunner()
    transport = GitHubGuardTransport(
        GitHubGuardConfig("owner/private-repo", "main", 17, "sunny"),
        runner=runner,
        sleep=lambda _seconds: None,
        now=lambda: NOW,
    )
    client = GuardClient(transport, nonce=NONCE)
    identity = RunIdentity("live-run", LABEL, NOW)

    armed = client.arm(identity, NOW + timedelta(minutes=20))
    client.record_heartbeat(identity, 7)
    root = client.anchor("c" * 64)

    assert armed.host_identity == "github-runner-1+github-runner-2"
    assert root == "c" * 64
    dispatch = runner.calls[0]
    assert "/actions/workflows/independent-guard.yml/dispatches" in dispatch[4]
    assert f"inputs[nonce]={NONCE}" in dispatch
    assert any(
        item == f"body=SRECON26_GUARD_V1 HEARTBEAT nonce={NONCE} root={'b' * 64}"
        for call in runner.calls for item in call
    )
    assert any(
        item == f"body=SRECON26_GUARD_V1 ANCHOR nonce={NONCE} root={'c' * 64}"
        for call in runner.calls for item in call
    )
    assert any("comments?per_page=100&since=2026-09-23T04:00:00Z" in argument for call in runner.calls for argument in call)


def test_github_guard_rejects_anchor_without_remote_workflow_ack() -> None:
    runner = GitHubRunner()
    transport = GitHubGuardTransport(
        GitHubGuardConfig("owner/private-repo", "main", 17, "sunny", arm_timeout_seconds=30),
        runner=runner,
        sleep=lambda _seconds: None,
        now=lambda: NOW,
    )
    client = GuardClient(transport, nonce=NONCE)
    identity = RunIdentity("live-run", LABEL, NOW)
    client.arm(identity, NOW + timedelta(minutes=20))
    runner.ack_anchors = False

    with pytest.raises(LiveFactoryError, match="did not acknowledge"):
        client.anchor("e" * 64)


def test_github_guard_bounds_heartbeat_comment_volume() -> None:
    runner = GitHubRunner()
    transport = GitHubGuardTransport(
        GitHubGuardConfig("owner/private-repo", "main", 17, "sunny", heartbeat_seconds=120),
        runner=runner,
        sleep=lambda _seconds: None,
        now=lambda: NOW,
    )
    client = GuardClient(transport, nonce=NONCE)
    identity = RunIdentity("live-run", LABEL, NOW)
    client.arm(identity, NOW + timedelta(minutes=20))

    client.record_heartbeat(identity, 1_000_000_000)
    client.record_heartbeat(identity, 2_000_000_000)
    client.record_heartbeat(identity, 61_000_000_000)

    heartbeat_calls = [
        call for call in runner.calls
        if any("SRECON26_GUARD_V1 HEARTBEAT" in argument for argument in call)
    ]
    assert len(heartbeat_calls) == 2


def test_github_guard_rejects_an_attestation_from_any_non_workflow_actor() -> None:
    runner = GitHubRunner()
    runner.comments[0]["user"] = {"login": "attacker"}
    ticks = iter((NOW, NOW + timedelta(minutes=3)))
    transport = GitHubGuardTransport(
        GitHubGuardConfig("owner/private-repo", "main", 17, "sunny", arm_timeout_seconds=30),
        runner=runner,
        sleep=lambda _seconds: None,
        now=lambda: next(ticks),
    )

    with pytest.raises(LiveFactoryError, match="did not publish"):
        transport.call("arm", {"nonce": NONCE, "label": LABEL, "hard_deadline": "2026-09-23T04:20:00Z"})


def test_github_guard_can_adopt_exact_prearmed_dual_receipts_without_redispatch() -> None:
    runner = GitHubRunner()
    transport = GitHubGuardTransport(
        GitHubGuardConfig("owner/private-repo", "main", 17, "sunny", dispatch_on_arm=False),
        runner=runner,
        sleep=lambda _seconds: None,
        now=lambda: NOW,
    )
    client = GuardClient(transport, nonce=NONCE)

    receipt = client.arm(RunIdentity("live-run", LABEL, NOW), NOW + timedelta(minutes=20))

    assert receipt.host_identity == "github-runner-1+github-runner-2"
    assert not any("/dispatches" in argument for call in runner.calls for argument in call)


def test_ssh_resolver_rejects_an_instance_record_that_does_not_preserve_exact_label() -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)

    def runner(_arguments, *, timeout: int) -> str:
        del timeout
        return json.dumps({"id": 417, "label": "other--nonce-nonce_12345678", "actual_status": "running", "ssh_host": "203.0.113.8", "ssh_port": 22})

    with pytest.raises(LiveFactoryError, match="changed ID or nonce-bound label"):
        VastSshResolver("vastai", runner=runner).resolve(instance)


def test_ssh_resolver_selects_explicit_direct_route_and_records_normalized_evidence(tmp_path: Path) -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)

    def runner(_arguments, *, timeout: int) -> str:
        del timeout
        return json.dumps({
            "id": 417,
            "label": LABEL,
            "actual_status": "running",
            "public_ipaddr": "203.0.113.8",
            "ports": {"22/tcp": [{"HostPort": "2222"}]},
            "ssh_host": "proxy.example.test",
            "ssh_port": "12345",
        })

    log = tmp_path / "status.ndjson"
    endpoint = VastSshResolver("vastai", runner=runner).resolve(instance, status_log=log)
    assert (endpoint.host, endpoint.port, endpoint.destination("root")) == ("203.0.113.8", 2222, "root@203.0.113.8")
    evidence = json.loads(log.read_text())
    assert (evidence["direct_endpoint_host"], evidence["direct_endpoint_port"]) == ("203.0.113.8", 2222)
    assert (evidence["proxy_endpoint_host"], evidence["proxy_endpoint_port"]) == ("proxy.example.test", 12345)
    assert evidence["direct_endpoint_candidate"] == {"host": "203.0.113.8", "port": 2222}
    assert evidence["proxy_endpoint_candidate"] == {"host": "proxy.example.test", "port": 12345}
    assert evidence["selected_ssh_route"] == "direct"
    assert evidence["ssh_route_selection_reason"] == "direct_public_ipaddr_22_tcp_hostport"


def test_ssh_resolver_safely_falls_back_to_proxy_when_direct_mapping_is_unavailable(tmp_path: Path) -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)

    def runner(_arguments, *, timeout: int) -> str:
        del timeout
        return json.dumps({
            "id": 417,
            "label": LABEL,
            "actual_status": "running",
            "public_ipaddr": "203.0.113.8",
            "ports": {"22/tcp": []},
            "ssh_host": "proxy.example.test",
            "ssh_port": "12345",
        })

    log = tmp_path / "status.ndjson"
    endpoint = VastSshResolver("vastai", runner=runner).resolve(instance, status_log=log)

    assert (endpoint.host, endpoint.port) == ("proxy.example.test", 12345)
    evidence = json.loads(log.read_text())
    assert (evidence["direct_endpoint_host"], evidence["direct_endpoint_port"]) == ("203.0.113.8", None)
    assert evidence["direct_endpoint_candidate"] is None
    assert evidence["selected_ssh_route"] == "proxy"
    assert evidence["ssh_route_selection_reason"] == "proxy_ssh_host_ssh_port"


def test_ssh_resolver_never_pairs_public_ip_with_malformed_or_proxy_port(tmp_path: Path) -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)

    def runner(_arguments, *, timeout: int) -> str:
        del timeout
        return json.dumps({
            "id": 417,
            "label": LABEL,
            "actual_status": "running",
            "public_ipaddr": "203.0.113.8",
            "ports": {"22/tcp": [{"HostPort": "not-a-port"}]},
            "ssh_port": "2222",
        })

    log = tmp_path / "status.ndjson"
    with pytest.raises(LiveFactoryError, match="did not reach running with a safe SSH endpoint"):
        VastSshResolver("vastai", runner=runner, attempts=1).resolve(instance, status_log=log)

    evidence = json.loads(log.read_text())
    assert (evidence["direct_endpoint_host"], evidence["direct_endpoint_port"]) == ("203.0.113.8", None)
    assert evidence["direct_endpoint_candidate"] is None
    assert evidence["proxy_endpoint_candidate"] is None
    assert evidence["selected_ssh_route"] is None
    assert evidence["ssh_route_selection_reason"] == "no_safe_endpoint_candidate"


def test_ssh_resolver_waits_for_running_even_when_endpoint_is_published(tmp_path: Path) -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)
    statuses = iter(("loading", "running"))

    def runner(_arguments, *, timeout: int) -> str:
        del timeout
        return json.dumps({"id": 417, "label": LABEL, "actual_status": next(statuses), "ssh_host": "203.0.113.8", "ssh_port": 2222})

    log = tmp_path / "status.ndjson"
    endpoint = VastSshResolver("vastai", runner=runner, interval_seconds=0).resolve(instance, status_log=log)

    assert endpoint.host == "203.0.113.8"
    assert [json.loads(line)["actual_status"] for line in log.read_text().splitlines()] == ["loading", "running"]


def test_ssh_resolver_emits_typed_host_fault_only_after_all_exact_startup_reads(tmp_path: Path) -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)
    statuses = iter(("created", "loading", "starting"))

    def runner(_arguments, *, timeout: int) -> str:
        del timeout
        return json.dumps(
            {
                "id": 417,
                "label": LABEL,
                "actual_status": next(statuses),
                "ssh_host": "proxy.example.test",
                "ssh_port": 2222,
            }
        )

    with pytest.raises(ProviderStartupFault) as raised:
        VastSshResolver("vastai", runner=runner, attempts=3, interval_seconds=0).resolve(
            instance,
            status_log=tmp_path / "status.ndjson",
        )

    observations = raised.value.observations
    assert [item.actual_status for item in observations] == ["created", "loading", "starting"]
    assert all(item.endpoint_published for item in observations)
    assert all(item.selected_ssh_route == "proxy" for item in observations)


def test_ssh_key_attachment_preserves_reportable_created_startup_observations(tmp_path: Path) -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)
    public_key = tmp_path / "id_rsa.pub"
    public_key.write_text("ssh-rsa QUJDRA== test@example", encoding="utf-8")
    calls: list[list[str]] = []

    def runner(arguments, *, timeout: int) -> str:
        del timeout
        calls.append(list(arguments))
        return json.dumps(
            {
                "id": 417,
                "label": LABEL,
                "actual_status": "created",
                "ssh_host": "proxy.example.test",
                "ssh_port": 2222,
            }
        )

    with pytest.raises(ProviderStartupFault) as raised:
        VastSshResolver("vastai", runner=runner, attempts=2, interval_seconds=0).attach_public_key(
            instance,
            public_key,
            hard_deadline=datetime.now(UTC) + timedelta(minutes=20),
            heartbeat=lambda: None,
            status_log=tmp_path / "status.ndjson",
        )

    assert [item.actual_status for item in raised.value.observations] == ["created", "created"]
    assert not any("attach" in call for call in calls)


@pytest.mark.parametrize(
    "records",
    (
        (
            {"id": 417, "label": LABEL, "actual_status": "loading", "ssh_host": "proxy.example.test", "ssh_port": 2222},
            LiveFactoryError("provider lookup unavailable"),
        ),
        (
            {"id": 417, "label": LABEL, "actual_status": "loading"},
            {"id": 417, "label": LABEL, "actual_status": "loading"},
        ),
        (
            {"id": 417, "label": LABEL, "actual_status": "running"},
            {"id": 417, "label": LABEL, "actual_status": "loading", "ssh_host": "proxy.example.test", "ssh_port": 2222},
        ),
        (
            {"id": 417, "label": LABEL, "actual_status": "queued", "ssh_host": "proxy.example.test", "ssh_port": 2222},
            {"id": 417, "label": LABEL, "actual_status": "loading", "ssh_host": "proxy.example.test", "ssh_port": 2222},
        ),
    ),
)
def test_ssh_resolver_never_classifies_incomplete_or_nonstartup_observations_as_host_fault(records: tuple[object, ...]) -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)
    responses = iter(records)

    def runner(_arguments, *, timeout: int) -> str:
        del timeout
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return json.dumps(response)

    with pytest.raises(LiveFactoryError) as raised:
        VastSshResolver("vastai", runner=runner, attempts=2, interval_seconds=0).resolve(instance)
    assert not isinstance(raised.value, ProviderStartupFault)


def test_ssh_resolver_keeps_startup_failure_unresolved_when_report_reserve_cannot_fit() -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)
    calls: list[list[str]] = []

    def runner(arguments, *, timeout: int) -> str:
        del timeout
        calls.append(list(arguments))
        return json.dumps({"id": 417, "label": LABEL, "actual_status": "loading", "ssh_host": "proxy.example.test", "ssh_port": 2222})

    with pytest.raises(LiveFactoryError) as raised:
        VastSshResolver("vastai", runner=runner).resolve(
            instance,
            hard_deadline=datetime.now(UTC) + REPORT_MARGIN + timedelta(seconds=100),
        )
    assert not isinstance(raised.value, ProviderStartupFault)
    assert calls == []


def test_remote_workload_returns_reportable_host_fault_with_immutable_exact_target_artifact(tmp_path: Path) -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)
    fault = ProviderStartupFault(
        (
            StartupStatusObservation(1, "created", True, "proxy", "2026-09-23T04:00:00Z"),
            StartupStatusObservation(2, "loading", True, "proxy", "2026-09-23T04:00:01Z"),
        )
    )

    class Resolver:
        def attach_public_key(self, *_args, **_kwargs) -> None:
            return None

        def resolve(self, *_args, **_kwargs) -> SshEndpoint:
            raise fault

    remote = object.__new__(SshRemoteWorkload)
    remote.resolver = Resolver()
    remote.config = types.SimpleNamespace(public_key_file=tmp_path / "id_rsa.pub")
    run_directory = tmp_path / "run-1"
    observed = remote.run(
        stage="gpu-smoke",
        workload=WorkloadContract(FROZEN_MODEL_ID, FROZEN_MODEL_REVISION, VLLM_IMAGE_DIGEST),
        instance=instance,
        run_directory=run_directory,
        hard_deadline=datetime.now(UTC) + timedelta(minutes=20),
        heartbeat=lambda: None,
    )

    assert observed.provider_fault is not None
    assert observed.provider_fault.category == "host"
    artifact = observed.provider_fault.artifact
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert (payload["run_id"], payload["instance_id"], payload["label"]) == ("run-1", 417, LABEL)
    assert payload["confirmed"]["all_bounded_provider_reads_succeeded"] is True
    assert payload["confirmed"]["desktop_report_reserve_seconds"] == 90
    with pytest.raises(LiveFactoryError, match="already exists"):
        remote._write_startup_fault_evidence(
            artifact.parent,
            run_id="run-1",
            instance=instance,
            fault=fault,
        )


def test_remote_workload_does_not_report_generic_ssh_or_auth_errors(tmp_path: Path) -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)

    class Resolver:
        def attach_public_key(self, *_args, **_kwargs) -> None:
            return None

        def resolve(self, *_args, **_kwargs) -> SshEndpoint:
            raise LiveFactoryError("SSH authentication failed")

    remote = object.__new__(SshRemoteWorkload)
    remote.resolver = Resolver()
    remote.config = types.SimpleNamespace(public_key_file=tmp_path / "id_rsa.pub")
    observed = remote.run(
        stage="gpu-smoke",
        workload=WorkloadContract(FROZEN_MODEL_ID, FROZEN_MODEL_REVISION, VLLM_IMAGE_DIGEST),
        instance=instance,
        run_directory=tmp_path / "run-2",
        hard_deadline=datetime.now(UTC) + timedelta(minutes=20),
        heartbeat=lambda: None,
    )

    assert observed.provider_fault is None
    assert (tmp_path / "run-2" / "remote-transport" / "failure.txt").read_text(encoding="utf-8") == "SSH authentication failed\n"


def test_ssh_resolver_attaches_public_key_only_after_exact_ownership_check(tmp_path: Path) -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)
    public_key = tmp_path / "id_rsa.pub"
    public_key.write_text("ssh-rsa QUJDRA== test@example")
    calls: list[list[str]] = []

    def runner(arguments, *, timeout: int) -> str:
        del timeout
        calls.append(arguments)
        return json.dumps({"id": 417, "label": LABEL, "actual_status": "loading"}) if "show" in arguments else ""

    beats: list[int] = []
    VastSshResolver("vastai", runner=runner).attach_public_key(instance, public_key, hard_deadline=datetime.now(UTC) + timedelta(minutes=20), heartbeat=lambda: beats.append(1), status_log=tmp_path / "status.ndjson")

    assert calls[-1][-4:] == ["attach", "ssh", "417", str(public_key)]
    assert len(beats) == 2


def test_ssh_resolver_waits_for_loading_before_attaching_public_key(tmp_path: Path) -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)
    public_key = tmp_path / "id_rsa.pub"
    public_key.write_text("ssh-rsa QUJDRA== test@example")
    statuses = iter(("created", "loading"))
    calls: list[list[str]] = []

    def runner(arguments, *, timeout: int) -> str:
        calls.append(arguments)
        return json.dumps({"id": 417, "label": LABEL, "actual_status": next(statuses)}) if "show" in arguments else ""

    VastSshResolver("vastai", runner=runner, interval_seconds=0).attach_public_key(instance, public_key, hard_deadline=datetime.now(UTC) + timedelta(minutes=20), heartbeat=lambda: None, status_log=tmp_path / "status.ndjson")
    assert sum("show" in call for call in calls) == 2
    assert calls[-1][-4:] == ["attach", "ssh", "417", str(public_key)]


def test_ssh_resolver_does_not_start_cli_lookup_without_full_deadline_window() -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)
    calls: list[list[str]] = []

    def runner(arguments, *, timeout: int) -> str:
        calls.append(arguments)
        return ""

    with pytest.raises(LiveFactoryError, match="cannot fit before immutable teardown margin"):
        VastSshResolver("vastai", runner=runner).resolve(instance, hard_deadline=datetime.now(UTC) + REPORT_MARGIN + timedelta(seconds=21))
    assert calls == []


def test_ssh_resolver_does_not_start_key_attach_lookup_without_full_deadline_window(tmp_path: Path) -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)
    public_key = tmp_path / "id_rsa.pub"
    public_key.write_text("ssh-rsa QUJDRA== test@example")
    calls: list[list[str]] = []

    def runner(arguments, *, timeout: int) -> str:
        calls.append(arguments)
        return ""

    with pytest.raises(LiveFactoryError, match="cannot fit before immutable teardown margin"):
        VastSshResolver("vastai", runner=runner).attach_public_key(instance, public_key, hard_deadline=datetime.now(UTC) + REPORT_MARGIN + timedelta(seconds=21), heartbeat=lambda: None, status_log=tmp_path / "status.ndjson")
    assert calls == []


@pytest.mark.parametrize("status", ["error", "stopped"])
def test_ssh_resolver_refuses_key_attach_without_nonterminal_status(tmp_path: Path, status: str) -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)
    public_key = tmp_path / "id_rsa.pub"
    public_key.write_text("ssh-rsa QUJDRA== test@example")
    calls: list[list[str]] = []

    def runner(arguments, *, timeout: int) -> str:
        del timeout
        calls.append(arguments)
        return json.dumps({"id": 417, "label": LABEL, "actual_status": status})

    with pytest.raises(LiveFactoryError, match="terminal provider status"):
        VastSshResolver("vastai", runner=runner).attach_public_key(instance, public_key, hard_deadline=datetime.now(UTC) + timedelta(minutes=20), heartbeat=lambda: None, status_log=tmp_path / "status.ndjson")
    assert not any("attach" in call for call in calls)


def test_ssh_resolver_never_attaches_key_from_unknown_status(tmp_path: Path) -> None:
    instance = InstanceContract(417, "RTX 3090", 1, 24576, "8.6", 99, Decimal("0.30"), LABEL)
    public_key = tmp_path / "id_rsa.pub"
    public_key.write_text("ssh-rsa QUJDRA== test@example")
    calls: list[list[str]] = []

    def runner(arguments, *, timeout: int) -> str:
        calls.append(arguments)
        return json.dumps({"id": 417, "label": LABEL, "actual_status": ""})

    with pytest.raises(LiveFactoryError, match="did not reach a safe SSH key attach status"):
        VastSshResolver("vastai", runner=runner, attempts=2, interval_seconds=0).attach_public_key(instance, public_key, hard_deadline=datetime.now(UTC) + timedelta(minutes=20), heartbeat=lambda: None, status_log=tmp_path / "status.ndjson")
    assert not any("attach" in call for call in calls)


def test_guard_workflow_publishes_machine_parseable_bound_arm_receipt() -> None:
    workflow = Path(".github/workflows/independent-guard.yml").read_text(encoding="utf-8")

    assert "guard_worker.py status" in workflow
    assert "SRECON26_GUARD_V1 ARMED nonce=" in workflow
    assert "SRECON26_GUARD_V1 BACKSTOP_ARMED nonce=" in workflow
    assert "script=${script_hash} root=${root_hash}" in workflow
