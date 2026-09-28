from __future__ import annotations

from dataclasses import replace
import importlib.util
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from srecon26_poc.vast_sdk_adapter import SdkInstance, SdkOffer, VastSdkError

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_single_host_multigpu_benchmark.py"


def _load_module() -> Any:
    name = "run_single_host_multigpu_benchmark"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bench = _load_module()

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
NONCE = "0123456789abcdef"
LABEL = f"srecon26-mgpu-{NOW.strftime('%Y%m%dt%H%M%Sz').lower()}-{NONCE[:8]}--nonce-{NONCE}"


def _offer(**changes: object) -> SdkOffer:
    base = SdkOffer(
        offer_id=101,
        machine_id=202,
        gpu_name="RTX 4090",
        num_gpus=8,
        gpu_ram_mib=24564,
        compute_capability="8.9",
        dph_total=Decimal("2.90"),
        reliability=Decimal("0.995"),
        direct_port_count=4,
        inet_down_mbps=Decimal("800"),
        label=LABEL,
    )
    return replace(base, **changes)


def _instance(**changes: object) -> SdkInstance:
    base = SdkInstance(
        instance_id=9001,
        machine_id=202,
        gpu_name="RTX 4090",
        num_gpus=8,
        gpu_ram_mib=24564,
        compute_capability="8.9",
        dph_total=Decimal("2.90"),
        label=LABEL,
        actual_status="running",
        ssh_host="proxy.example.net",
        ssh_port=2222,
        public_ipaddr=None,
        ports=(),
    )
    return replace(base, **changes)


class FakeProvider:
    def __init__(self, offer: SdkOffer | None = None, *, current_offer: SdkOffer | None = None, live: list[SdkInstance] | None = None) -> None:
        self.offer = _offer() if offer is None else offer
        self.current_offer = self.offer if current_offer is None else current_offer
        self.live = [] if live is None else list(live)
        self.search_calls = 0
        self.create_calls = 0
        self.destroy_calls = 0
        self.arm_calls = 0
        self.heartbeat_calls = 0
        self.absence_reads = 0
        self.reconcile_calls = 0
        self.fetch_calls = 0
        self.initial_inventory_reads = 0

    def search_offers(self, *, query: str, limit: int, label: str) -> tuple[SdkOffer, ...]:
        self.search_calls += 1
        assert query and limit >= 1
        return (SdkOffer(
            offer_id=self.offer.offer_id,
            machine_id=self.offer.machine_id,
            gpu_name=self.offer.gpu_name,
            num_gpus=self.offer.num_gpus,
            gpu_ram_mib=self.offer.gpu_ram_mib,
            compute_capability=self.offer.compute_capability,
            dph_total=self.offer.dph_total,
            reliability=self.offer.reliability,
            direct_port_count=self.offer.direct_port_count,
            inet_down_mbps=self.offer.inet_down_mbps,
            label=label,
            inet_down_cost=self.offer.inet_down_cost,
            inet_up_cost=self.offer.inet_up_cost,
        ),)

    def get_offer(self, offer_id: int, machine_id: int, label: str) -> SdkOffer:
        assert offer_id == self.current_offer.offer_id
        assert machine_id == self.current_offer.machine_id
        current = self.current_offer
        return SdkOffer(
            offer_id=current.offer_id,
            machine_id=current.machine_id,
            gpu_name=current.gpu_name,
            num_gpus=current.num_gpus,
            gpu_ram_mib=current.gpu_ram_mib,
            compute_capability=current.compute_capability,
            dph_total=current.dph_total,
            reliability=current.reliability,
            direct_port_count=current.direct_port_count,
            inet_down_mbps=current.inet_down_mbps,
            label=label,
            inet_down_cost=current.inet_down_cost,
            inet_up_cost=current.inet_up_cost,
        )

    def create_once(self, offer: SdkOffer, contract: Any) -> SdkInstance:
        del contract
        self.create_calls += 1
        instance = _instance(label=offer.label)
        self.live = [instance]
        return instance

    def reconcile_label(self, label: str) -> SdkInstance | None:
        self.reconcile_calls += 1
        matches = [item for item in self.live if item.label == label]
        return matches[0] if matches else None

    def get_instance(self, instance_id: int) -> SdkInstance:
        for item in self.live:
            if item.instance_id == instance_id:
                return item
        raise AssertionError("instance missing")

    def list_instances(self) -> tuple[SdkInstance, ...]:
        if self.destroy_calls:
            self.absence_reads += 1
        else:
            self.initial_inventory_reads += 1
        return tuple(self.live)

    def destroy_exact(self, instance_id: int, expected_label: str) -> None:
        self.destroy_calls += 1
        self.live = [item for item in self.live if not (item.instance_id == instance_id and item.label == expected_label)]


class FakeGuard:
    def __init__(self, provider: FakeProvider) -> None:
        self.provider = provider

    def preflight(self) -> Any:  # pragma: no cover
        raise NotImplementedError

    def arm(self, identity: Any, hard_deadline: datetime) -> Any:
        self.provider.arm_calls += 1
        deadline = hard_deadline

        class _Attestation:
            host_identity = "guard.example.net"
            script_hash = "a" * 64
            nonce = NONCE
            label = identity.label
            last_heartbeat = None
            azure_resource_id = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/guard-vm"
            azure_vm_id = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/guard-vm"
            host_key_fingerprint = "sha256:deadbeef"
            heartbeat_timeout_seconds = 120
            hard_deadline = deadline

        return _Attestation()

    def record_heartbeat(self, identity: Any, monotonic_ns: int) -> None:  # pragma: no cover
        del identity, monotonic_ns
        self.provider.heartbeat_calls += 1

    def anchor(self, root_hash: str) -> str:  # pragma: no cover
        return root_hash


@pytest.fixture(autouse=True)
def deterministic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bench, "NOW_FACTORY", lambda: NOW)
    monkeypatch.setattr(bench, "NONCE_FACTORY", lambda: NONCE)
    monkeypatch.setattr(bench, "DEFAULT_KNOWN_HOSTS", tmp_path / "known_hosts")
    monkeypatch.setattr(bench, "SLEEP", lambda seconds: None)
    monkeypatch.setattr(bench, "DEFAULT_AZ_PATH", Path(sys.executable))


def _plan_args(path: Path) -> list[str]:
    return [
        "--plan",
        str(path),
        "--azure-subscription-id",
        "11111111-1111-1111-1111-111111111111",
        "--azure-resource-group",
        "rg",
        "--azure-vm-name",
        "guard-vm",
        "--az-path",
        sys.executable,
    ]


def test_default_plan_path_no_mutation_and_prints_confirmation_hash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = FakeProvider()
    monkeypatch.setattr(bench, "SDK_CLIENT_FACTORY", lambda api_key=None: object())
    monkeypatch.setattr(bench, "SDK_PROVIDER_FACTORY", lambda client: provider)
    plan_path = tmp_path / "plan.json"

    exit_code = bench.main(_plan_args(plan_path))

    assert exit_code == 0
    assert provider.search_calls == 1
    assert bench.DEFAULT_QUERY.endswith("gpu_name=RTX_4090 num_gpus=8")
    assert provider.create_calls == 0
    assert provider.destroy_calls == 0
    printed = json.loads(capsys.readouterr().out.strip())
    envelope = json.loads(plan_path.read_text(encoding="utf-8"))
    assert printed["status"] == "planned"
    assert printed["plan_path"] == str(plan_path)
    assert printed["confirm_plan_sha256"] == envelope["document_sha256"]
    assert envelope["document"]["run_identity"]["run_id"] == f"single-host-multigpu-20260927T120000Z-{NONCE[:8]}"
    assert envelope["document"]["offer"]["gpu_count"] == 8
    assert envelope["document"]["guard_binding"]["rpc_timeout_seconds"] == 150
    assert envelope["document"]["guard_binding"]["cli_timeout_seconds"] == 30
    assert envelope["document"]["guard_binding"]["heartbeat_timeout_seconds"] == 600
    assert envelope["document"]["remote_benchmark"]["script_sha256"] == bench._sha256_file(bench.REMOTE_SCRIPT)


def test_execute_rejects_missing_or_wrong_confirmation_before_clients(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan_path = tmp_path / "plan.json"
    provider = FakeProvider()
    monkeypatch.setattr(bench, "SDK_CLIENT_FACTORY", lambda api_key=None: object())
    monkeypatch.setattr(bench, "SDK_PROVIDER_FACTORY", lambda client: provider)
    assert bench.main(_plan_args(plan_path)) == 0

    called = {"client": 0}
    monkeypatch.setattr(bench, "SDK_CLIENT_FACTORY", lambda api_key=None: called.__setitem__("client", called["client"] + 1) or object())

    missing = bench.main(["--execute", "--plan", str(plan_path), "--ssh-identity-file", str(tmp_path / "id_rsa")])
    wrong = bench.main([
        "--execute",
        "--plan",
        str(plan_path),
        "--ssh-identity-file",
        str(tmp_path / "id_rsa"),
        "--confirm-plan-sha256",
        "0" * 64,
    ])
    assert missing == 2
    assert wrong == 2
    assert called["client"] == 0


def test_execute_rejects_unsigned_azure_cli_override_before_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan_path = tmp_path / "plan.json"
    provider = FakeProvider()
    monkeypatch.setattr(bench, "SDK_CLIENT_FACTORY", lambda api_key=None: object())
    monkeypatch.setattr(bench, "SDK_PROVIDER_FACTORY", lambda client: provider)
    assert bench.main(_plan_args(plan_path)) == 0
    confirm = json.loads(capsys.readouterr().out.strip())["confirm_plan_sha256"]
    identity = tmp_path / "id_rsa"
    identity.write_text("fake-key\n", encoding="utf-8")

    called = {"client": 0}
    monkeypatch.setattr(bench, "SDK_CLIENT_FACTORY", lambda api_key=None: called.__setitem__("client", 1) or object())
    exit_code = bench.main([
        "--execute",
        "--plan",
        str(plan_path),
        "--confirm-plan-sha256",
        confirm,
        "--ssh-identity-file",
        str(identity),
        "--az-path",
        "/bin/echo",
    ])

    assert exit_code == 2
    assert called["client"] == 0


def test_plan_binds_network_prices_and_rejects_excess_allowance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = FakeProvider(offer=_offer(inet_down_cost=Decimal("0.0065"), inet_up_cost=Decimal("0.0065")))
    monkeypatch.setattr(bench, "SDK_CLIENT_FACTORY", lambda api_key=None: object())
    monkeypatch.setattr(bench, "SDK_PROVIDER_FACTORY", lambda client: provider)

    assert bench.main([*_plan_args(tmp_path / "plan.json"), "--max-usd", "7.50", "--max-dph", "4.0", "--deadline-minutes", "90", "--readiness-timeout-s", "1500", "--max-run-seconds", "2100", "--remote-command-timeout-seconds", "3650"]) == 0
    capsys.readouterr()
    document = json.loads((tmp_path / "plan.json").read_text())["document"]
    assert document["offer"]["inet_down_cost"] == "0.0065"
    assert document["spend_limits"]["network_allowance_usd"] == "0.8450"
    assert Decimal(document["spend_limits"]["max_allowed_spend_usd"]) <= Decimal("7.50")

    assert bench.main([*_plan_args(tmp_path / "too-expensive.json"), "--max-usd", "6.00", "--max-dph", "4.0", "--deadline-minutes", "90", "--readiness-timeout-s", "1500", "--max-run-seconds", "2100", "--remote-command-timeout-seconds", "3650"]) == 2


def test_execute_with_fakes_exact_lifecycle_and_artifact_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = FakeProvider()
    monkeypatch.setattr(bench, "SDK_CLIENT_FACTORY", lambda api_key=None: object())
    monkeypatch.setattr(bench, "SDK_PROVIDER_FACTORY", lambda client: provider)
    def guard_factory(config: Any, nonce: str, script_hash: str) -> FakeGuard:
        assert (config.timeout_seconds, config.cli_timeout_seconds, config.heartbeat_timeout_seconds) == (150, 30, 600)
        return FakeGuard(provider)

    monkeypatch.setattr(bench, "RUN_COMMAND_GUARD_FACTORY", guard_factory)
    plan_path = tmp_path / "plan.json"
    assert bench.main(_plan_args(plan_path)) == 0
    printed = json.loads(capsys.readouterr().out.strip())
    confirm = printed["confirm_plan_sha256"]
    identity = tmp_path / "id_rsa"
    identity.write_text("fake-key\n", encoding="utf-8")

    def fake_run(arguments: list[str], **kwargs: Any) -> Any:
        argv = list(arguments)
        if argv[0] == "scp":
            assert "-P" in argv and "-p" not in argv
        if argv[0] == "ssh":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[0] == "scp" and "-r" in argv:
            destination = Path(argv[-1])
            destination.mkdir(parents=True)
            (destination / "run.json").write_text("{}\n", encoding="utf-8")
            provider.fetch_calls += 1
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[0] == "scp":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(argv)

    monkeypatch.setattr(bench, "SUBPROCESS_RUNNER", fake_run)
    exit_code = bench.main([
        "--execute",
        "--plan",
        str(plan_path),
        "--confirm-plan-sha256",
        confirm,
        "--ssh-identity-file",
        str(identity),
        "--artifacts-dir",
        str(tmp_path / "artifacts"),
    ])

    assert exit_code == 0
    assert provider.create_calls == 1
    assert provider.arm_calls == 1
    assert provider.heartbeat_calls >= 4
    assert provider.destroy_calls == 1
    assert provider.absence_reads == 3
    assert provider.fetch_calls == 1
    assert (tmp_path / "artifacts" / "run.json").exists()


@pytest.mark.parametrize("failure", ["benchmark", "fetch"])
def test_post_create_failure_still_destroys_and_proves_absence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    provider = FakeProvider()
    monkeypatch.setattr(bench, "SDK_CLIENT_FACTORY", lambda api_key=None: object())
    monkeypatch.setattr(bench, "SDK_PROVIDER_FACTORY", lambda client: provider)
    monkeypatch.setattr(bench, "RUN_COMMAND_GUARD_FACTORY", lambda config, nonce, script_hash: FakeGuard(provider))
    plan_path = tmp_path / "plan.json"
    assert bench.main(_plan_args(plan_path)) == 0
    confirm = json.loads(capsys.readouterr().out.strip())["confirm_plan_sha256"]
    identity = tmp_path / "id_rsa"
    identity.write_text("fake-key\n", encoding="utf-8")

    def fake_run(arguments: list[str], **kwargs: Any) -> Any:
        del kwargs
        argv = list(arguments)
        if argv[0] == "ssh" and any("remote_multigpu_benchmark.py" in item for item in argv):
            code = 1 if failure == "benchmark" else 0
            return subprocess.CompletedProcess(argv, code, stdout="", stderr="benchmark failed" if code else "")
        if argv[0] == "ssh":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[0] == "scp" and "-r" in argv:
            if failure == "fetch":
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="fetch failed")
            destination = Path(argv[-1])
            destination.mkdir(parents=True)
            (destination / "run.json").write_text("{}\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[0] == "scp":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(argv)

    monkeypatch.setattr(bench, "SUBPROCESS_RUNNER", fake_run)
    exit_code = bench.main([
        "--execute",
        "--plan",
        str(plan_path),
        "--confirm-plan-sha256",
        confirm,
        "--ssh-identity-file",
        str(identity),
        "--az-path",
        sys.executable,
        "--artifacts-dir",
        str(tmp_path / "artifacts"),
    ])

    assert exit_code == 2
    assert provider.create_calls == 1
    assert provider.destroy_calls == 1
    assert provider.absence_reads == 3
    failure_evidence = json.loads((tmp_path / "single-host-multigpu-failure.json").read_text())
    assert failure_evidence["stage"] == ("remote-benchmark" if failure == "benchmark" else "fetch-artifacts")
    assert failure_evidence["plan_sha256"] == confirm


@pytest.mark.parametrize("kind", ["drift", "inventory"])
def test_offer_drift_or_non_empty_inventory_fail_before_create(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    planning_provider = FakeProvider()
    monkeypatch.setattr(bench, "SDK_CLIENT_FACTORY", lambda api_key=None: object())
    monkeypatch.setattr(bench, "SDK_PROVIDER_FACTORY", lambda client: planning_provider)
    plan_path = tmp_path / "plan.json"
    assert bench.main(_plan_args(plan_path)) == 0
    confirm = json.loads(capsys.readouterr().out.strip())["confirm_plan_sha256"]
    identity = tmp_path / "id_rsa"
    identity.write_text("fake-key\n", encoding="utf-8")

    if kind == "drift":
        execution_provider = FakeProvider(current_offer=_offer(dph_total=Decimal("2.91")))
    else:
        execution_provider = FakeProvider(live=[_instance(instance_id=1, label="other")])
    monkeypatch.setattr(bench, "SDK_PROVIDER_FACTORY", lambda client: execution_provider)
    monkeypatch.setattr(bench, "RUN_COMMAND_GUARD_FACTORY", lambda config, nonce, script_hash: FakeGuard(execution_provider))

    exit_code = bench.main([
        "--execute",
        "--plan",
        str(plan_path),
        "--confirm-plan-sha256",
        confirm,
        "--ssh-identity-file",
        str(identity),
    ])

    assert exit_code == 2
    assert execution_provider.create_calls == 0
    if kind == "drift":
        assert execution_provider.arm_calls == 0
    else:
        assert execution_provider.arm_calls == 0


def test_endpoint_resolution_validates_exact_instance_and_safe_port() -> None:
    provider = FakeProvider(live=[_instance(ssh_host=None, ssh_port=None, public_ipaddr="203.0.113.10", ports=(("22/tcp", 2200),))])
    host, port, route, current = bench.resolve_ssh_endpoint(
        provider=provider,
        expected=_instance(),
        wait_seconds=0.0,
        poll_seconds=0.0,
        now=lambda: NOW,
        sleep=lambda seconds: None,
    )
    assert (host, port, route, current.instance_id) == ("203.0.113.10", 2200, "direct_public_ipaddr_22_tcp_hostport", 9001)

    bad_provider = FakeProvider(live=[_instance(label="foreign", ssh_host=None, ssh_port=None, public_ipaddr=None, ports=())])
    with pytest.raises(bench.BenchmarkOrchestratorError, match="label changed"):
        bench.resolve_ssh_endpoint(
            provider=bad_provider,
            expected=_instance(),
            wait_seconds=0.0,
            poll_seconds=0.0,
            now=lambda: NOW,
            sleep=lambda seconds: None,
        )


def test_endpoint_waits_for_running_even_when_port_is_published(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider()
    observed = iter([_instance(actual_status="loading"), _instance(actual_status="running")])
    reads: list[int] = []

    def get_instance(instance_id: int) -> SdkInstance:
        reads.append(instance_id)
        return next(observed)

    monkeypatch.setattr(provider, "get_instance", get_instance)
    host, port, _, current = bench.resolve_ssh_endpoint(
        provider=provider,
        expected=_instance(),
        wait_seconds=10.0,
        poll_seconds=1.0,
        now=lambda: NOW,
        sleep=lambda seconds: None,
    )

    assert (host, port, current.actual_status) == ("proxy.example.net", 2222, "running")
    assert reads == [9001, 9001]


def test_endpoint_retries_transient_instance_read_without_accepting_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider()
    reads: list[int] = []

    def transient_read(instance_id: int) -> SdkInstance:
        reads.append(instance_id)
        if len(reads) == 1:
            raise VastSdkError("temporary GET failure")
        return _instance()

    monkeypatch.setattr(provider, "get_instance", transient_read)
    host, port, _, _ = bench.resolve_ssh_endpoint(
        provider=provider, expected=_instance(), wait_seconds=10.0, poll_seconds=1.0,
        now=lambda: NOW, sleep=lambda seconds: None,
    )
    assert (host, port, reads) == ("proxy.example.net", 2222, [9001, 9001])


def test_plan_tampering_hash_mismatch_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = FakeProvider()
    monkeypatch.setattr(bench, "SDK_CLIENT_FACTORY", lambda api_key=None: object())
    monkeypatch.setattr(bench, "SDK_PROVIDER_FACTORY", lambda client: provider)
    plan_path = tmp_path / "plan.json"
    assert bench.main(_plan_args(plan_path)) == 0
    printed = json.loads(capsys.readouterr().out.strip())
    envelope = json.loads(plan_path.read_text(encoding="utf-8"))
    envelope["document"]["offer"]["gpu_count"] = 4
    plan_path.write_text(json.dumps(envelope, sort_keys=True) + "\n", encoding="utf-8")
    identity = tmp_path / "id_rsa"
    identity.write_text("fake-key\n", encoding="utf-8")

    exit_code = bench.main([
        "--execute",
        "--plan",
        str(plan_path),
        "--confirm-plan-sha256",
        printed["confirm_plan_sha256"],
        "--ssh-identity-file",
        str(identity),
    ])
    assert exit_code == 2


def test_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as raised:
        bench.main(["--help"])
    assert raised.value.code == 0


def test_guard_heartbeats_continue_during_workload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bench, "HEARTBEAT_INTERVAL_SECONDS", 0.005)
    heartbeats: list[float] = []

    def slow_workload() -> int:
        time.sleep(0.05)
        return 17

    assert bench._run_with_heartbeats(slow_workload, lambda: heartbeats.append(time.monotonic())) == 17
    assert len(heartbeats) >= 3

ARM_PLAN_TIMING = ["--deadline-minutes", "150", "--readiness-timeout-s", "1200", "--max-run-seconds", "1200", "--remote-command-timeout-seconds", "2460", "--max-usd", "15", "--max-dph", "4.0"]


def test_plan_signs_precision_arms_and_scales_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = FakeProvider()
    monkeypatch.setattr(bench, "SDK_CLIENT_FACTORY", lambda api_key=None: object())
    monkeypatch.setattr(bench, "SDK_PROVIDER_FACTORY", lambda client: provider)

    assert bench.main([*_plan_args(tmp_path / "plan.json"), *ARM_PLAN_TIMING, "--precisions", "bf16,fp8,fp8-kv"]) == 0
    capsys.readouterr()
    remote = json.loads((tmp_path / "plan.json").read_text())["document"]["remote_benchmark"]
    assert remote["precisions"] == ["bf16", "fp8", "fp8-kv"]
    assert remote["argv_template"][remote["argv_template"].index("--precision") + 1] == bench.REMOTE_PRECISION_PLACEHOLDER

    too_long = [*ARM_PLAN_TIMING[:-4], "--max-usd", "15", "--max-dph", "4.0"]
    too_long[too_long.index("--remote-command-timeout-seconds") + 1] = "2900"
    assert bench.main([*_plan_args(tmp_path / "long.json"), *too_long, "--precisions", "bf16,fp8,fp8-kv"]) == 2
    assert bench.main([*_plan_args(tmp_path / "dup.json"), *ARM_PLAN_TIMING, "--precisions", "fp8,fp8"]) == 2
    assert bench.main([*_plan_args(tmp_path / "bad.json"), *ARM_PLAN_TIMING, "--precisions", "int4"]) == 2


@pytest.mark.parametrize("failing_arm", [None, "fp8", "all"])
def test_execute_runs_one_remote_benchmark_per_precision_arm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], failing_arm: str | None
) -> None:
    provider = FakeProvider()
    monkeypatch.setattr(bench, "SDK_CLIENT_FACTORY", lambda api_key=None: object())
    monkeypatch.setattr(bench, "SDK_PROVIDER_FACTORY", lambda client: provider)
    monkeypatch.setattr(bench, "RUN_COMMAND_GUARD_FACTORY", lambda config, nonce, script_hash: FakeGuard(provider))
    plan_path = tmp_path / "plan.json"
    assert bench.main([*_plan_args(plan_path), *ARM_PLAN_TIMING, "--precisions", "bf16,fp8"]) == 0
    confirm = json.loads(capsys.readouterr().out.strip())["confirm_plan_sha256"]
    remote_output = json.loads(plan_path.read_text())["document"]["remote_benchmark"]["remote_output_dir"]
    identity = tmp_path / "id_rsa"
    identity.write_text("fake-key\n", encoding="utf-8")
    arm_calls: list[tuple[str, str]] = []

    def fake_run(arguments: list[str], **kwargs: Any) -> Any:
        del kwargs
        argv = list(arguments)
        if argv[0] == "ssh" and any("remote_multigpu_benchmark.py" in item for item in argv):
            precision = argv[argv.index("--precision") + 1]
            arm_calls.append((precision, argv[argv.index("--output-dir") + 1]))
            code = 1 if failing_arm in (precision, "all") else 0
            return subprocess.CompletedProcess(argv, code, stdout="", stderr="arm failed" if code else "")
        if argv[0] == "scp" and "-r" in argv:
            destination = Path(argv[-1])
            destination.mkdir(parents=True)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(bench, "SUBPROCESS_RUNNER", fake_run)
    exit_code = bench.main([
        "--execute", "--plan", str(plan_path), "--confirm-plan-sha256", confirm,
        "--ssh-identity-file", str(identity), "--artifacts-dir", str(tmp_path / "artifacts"),
    ])

    assert arm_calls == [("bf16", f"{remote_output}/bf16"), ("fp8", f"{remote_output}/fp8")]
    assert provider.destroy_calls == 1 and provider.absence_reads == 3
    if failing_arm == "all":
        assert exit_code == 2
        assert json.loads((tmp_path / "single-host-multigpu-failure.json").read_text())["stage"] == "remote-benchmark"
        return
    assert exit_code == 0
    evidence = json.loads((tmp_path / "single-host-multigpu-execution.json").read_text())
    assert evidence["arms"] == {"bf16": "completed", "fp8": "failed" if failing_arm == "fp8" else "completed"}
