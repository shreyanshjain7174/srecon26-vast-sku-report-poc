from __future__ import annotations

import hashlib
import importlib.util
import json
import shlex
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from srecon26_poc.provider import AmbiguousCreate
from srecon26_poc.single_vm_hpa import (
    EXCLUDED_MACHINE_IDS,
    K3S_AMD64_SHA256,
    VM_DISK_GIB,
    VM_TEMPLATE_HASH,
    VmLaunchContract,
    VmOffer,
    VmSdkProvider,
    vm_offer_rejection,
)
from srecon26_poc.vast_provider import OFFICIAL_UBUNTU_DESKTOP_TEMPLATE_HASH
from srecon26_poc.vast_sdk_adapter import SdkInstance, SdkOffer, VastSdkError

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "run_single_vm_hpa.py"


def _load_module() -> Any:
    name = "run_single_vm_hpa"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


hpa = _load_module()

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
NONCE = "0123456789abcdef"
LABEL = f"srecon26-vm2-{NOW.strftime('%Y%m%dt%H%M%Sz').lower()}-{NONCE[:8]}--nonce-{NONCE}"
PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyMaterialForTests0123456789abcdefghij test@example"


def _offer(**changes: object) -> VmOffer:
    sdk_changes = {key: value for key, value in changes.items() if key in SdkOffer.__dataclass_fields__}
    vm_changes = {key: value for key, value in changes.items() if key not in SdkOffer.__dataclass_fields__}
    sdk = SdkOffer(
        offer_id=101,
        machine_id=202,
        gpu_name="RTX 4090",
        num_gpus=2,
        gpu_ram_mib=24564,
        compute_capability="8.9",
        dph_total=Decimal("0.80"),
        reliability=Decimal("0.995"),
        direct_port_count=8,
        inet_down_mbps=Decimal("900"),
        label=LABEL,
        inet_down_cost=Decimal("0.004"),
        inet_up_cost=Decimal("0.004"),
    )
    base = VmOffer(replace(sdk, **sdk_changes), cpu_ram_mib=64390, cpu_cores=Decimal("16"), vms_enabled=True)
    return replace(base, **vm_changes)


def _instance(**changes: object) -> SdkInstance:
    base = SdkInstance(
        instance_id=9001,
        machine_id=202,
        gpu_name="RTX 4090",
        num_gpus=2,
        gpu_ram_mib=24564,
        compute_capability="8.9",
        dph_total=Decimal("0.80"),
        label=LABEL,
        actual_status="running",
        ssh_host=None,
        ssh_port=None,
        public_ipaddr="203.0.113.10",
        ports=(("22/tcp", 40022),),
    )
    return replace(base, **changes)


def _relabel(offer: VmOffer, label: str) -> VmOffer:
    return replace(offer, sdk=replace(offer.sdk, label=label))


class FakeVmProvider:
    def __init__(self, offers: list[VmOffer] | None = None, *, current: VmOffer | None = None, live: list[SdkInstance] | None = None) -> None:
        self.offers = [_offer()] if offers is None else offers
        self.current = self.offers[0] if current is None else current
        self.live = [] if live is None else list(live)
        self.search_queries: list[str] = []
        self.create_kwargs: list[dict[str, object]] = []
        self.attached: list[tuple[int, str, str]] = []
        self.destroy_calls = 0
        self.absence_reads = 0
        self.arm_calls = 0
        self.heartbeat_calls = 0

    def search_vm_offers(self, query: str, *, limit: int = 25, label: str = "") -> tuple[VmOffer, ...]:
        self.search_queries.append(query)
        return tuple(_relabel(offer, label) for offer in self.offers)

    def get_vm_offer(self, offer_id: int, machine_id: int, label: str) -> VmOffer:
        assert (offer_id, machine_id) == (self.current.sdk.offer_id, self.current.sdk.machine_id)
        return _relabel(self.current, label)

    def create_once(self, offer: SdkOffer, contract: VmLaunchContract) -> SdkInstance:
        assert isinstance(contract, VmLaunchContract)
        self.create_kwargs.append(contract.create_kwargs(offer_id=offer.offer_id, label=offer.label))
        instance = _instance(label=offer.label)
        self.live = [instance]
        return instance

    def attach_ssh_key(self, instance_id: int, expected_label: str, public_key: str) -> None:
        self.attached.append((instance_id, expected_label, public_key))

    def reconcile_label(self, label: str) -> SdkInstance | None:  # pragma: no cover - create is unambiguous here
        matches = [item for item in self.live if item.label == label]
        return matches[0] if matches else None

    def get_instance(self, instance_id: int) -> SdkInstance:
        for item in self.live:
            if item.instance_id == instance_id:
                return item
        raise VastSdkError("instance missing")

    def list_instances(self) -> tuple[SdkInstance, ...]:
        if self.destroy_calls:
            self.absence_reads += 1
        return tuple(self.live)

    def destroy_exact(self, instance_id: int, expected_label: str) -> None:
        self.destroy_calls += 1
        self.live = [item for item in self.live if not (item.instance_id == instance_id and item.label == expected_label)]


class FakeGuard:
    def __init__(self, provider: FakeVmProvider) -> None:
        self.provider = provider

    def arm(self, identity: Any, hard_deadline: datetime) -> Any:
        self.provider.arm_calls += 1
        deadline = hard_deadline

        class _Attestation:
            host_identity = "guard.example.net"
            script_hash = "a" * 64
            nonce = NONCE
            label = identity.label
            last_heartbeat = None
            azure_resource_id = "/subscriptions/1/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/guard-vm"
            azure_vm_id = "/subscriptions/1/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/guard-vm"
            host_key_fingerprint = "sha256:deadbeef"
            heartbeat_timeout_seconds = 600
            hard_deadline = deadline

        return _Attestation()

    def record_heartbeat(self, identity: Any, monotonic_ns: int) -> None:
        del identity, monotonic_ns
        self.provider.heartbeat_calls += 1


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def deterministic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hpa, "NOW_FACTORY", lambda: NOW)
    monkeypatch.setattr(hpa, "NONCE_FACTORY", lambda: NONCE)
    monkeypatch.setattr(hpa, "SLEEP", lambda seconds: None)
    monkeypatch.setattr(hpa, "SDK_CLIENT_FACTORY", lambda api_key=None: object())
    # The real k3s release binary is a local-only download; bind a stand-in to the pinned checks.
    tools = tmp_path / "tools"
    tools.mkdir()
    k3s = tools / "k3s"
    k3s.write_bytes(b"fake-k3s-binary")
    k3s.chmod(0o755)
    fake_pin = _sha(k3s)
    canary = tools / "remote_host_canary.sh"
    canary.write_text(f'#!/usr/bin/env bash\nreadonly K3S_AMD64_SHA256="{fake_pin}"\n', encoding="utf-8")
    monkeypatch.setattr(hpa, "K3S_BINARY", k3s)
    monkeypatch.setattr(hpa, "CANARY_SCRIPT", canary)
    monkeypatch.setattr(hpa, "K3S_AMD64_SHA256", fake_pin)


def _plan_args(path: Path, *extra: str) -> list[str]:
    return [
        "--plan", str(path),
        "--azure-subscription-id", "11111111-1111-1111-1111-111111111111",
        "--azure-resource-group", "rg",
        "--azure-vm-name", "guard-vm",
        "--az-path", sys.executable,
        *extra,
    ]


def _plan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], provider: FakeVmProvider) -> tuple[Path, str]:
    monkeypatch.setattr(hpa, "VM_PROVIDER_FACTORY", lambda client: provider)
    plan_path = tmp_path / "run" / "plan.json"
    assert hpa.main(_plan_args(plan_path)) == 0
    return plan_path, json.loads(capsys.readouterr().out.strip())["confirm_plan_sha256"]


def _identity(tmp_path: Path) -> Path:
    identity = tmp_path / "id_ed25519"
    identity.write_text("fake-private-key\n", encoding="utf-8")
    Path(f"{identity}.pub").write_text(PUBLIC_KEY + "\n", encoding="utf-8")
    return identity


def _execute_args(plan_path: Path, confirm: str, identity: Path, tmp_path: Path) -> list[str]:
    return [
        "--execute", "--plan", str(plan_path), "--confirm-plan-sha256", confirm,
        "--ssh-identity-file", str(identity), "--az-path", sys.executable,
        "--known-hosts-file", str(tmp_path / "known_hosts"),
    ]


class ScriptedRunner:
    """Fake subprocess.run for ssh/scp that classifies every call in order."""

    def __init__(self, *, fail: str | None = None, allocatable: str = "2", deploy_delay: float = 0.0) -> None:
        self.fail, self.allocatable, self.deploy_delay = fail, allocatable, deploy_delay
        self.calls: list[str] = []
        self.remote_env: dict[str, list[str]] = {}

    def _staged_hashes(self, paths: list[str]) -> str:
        local = {
            "remote_host_canary.sh": hpa.CANARY_SCRIPT,
            "k3s": hpa.K3S_BINARY,
            "nvidia-runtime.toml": hpa.NVIDIA_RUNTIME_TEMPLATE,
            "device-plugin.yaml": hpa.MANIFEST_DIR / "device-plugin.yaml",
        }
        return "".join(f"{_sha(local[path.rsplit('/', 1)[1]])}  {path}\n" for path in paths)

    def _classify_ssh(self, remote: list[str]) -> tuple[str, str]:
        if remote == ["true"]:
            return "wait-ssh", ""
        if remote[:2] == ["install", "-d"]:
            return "mkdir", ""
        if remote[0] == "curl":
            assert remote[-1] == hpa.K3S_RELEASE_URL and remote[-2].endswith("/k3s")
            return "fetch-k3s", ""
        if remote[:2] == ["chmod", "0755"]:
            return "chmod-k3s", ""
        if remote[0] == "sha256sum":
            return "verify-staged", self._staged_hashes(remote[1:])
        if remote[0] == "env" and remote[-3] == "bash":
            self.remote_env[remote[-1]] = remote[1:-3]
            return remote[-1], ""
        if remote[:2] == ["sh", "-ceu"]:
            return "runtime-dropin", ""
        if remote[0] == "nvidia-smi":
            return "gpu-smi", "0, NVIDIA GeForce RTX 4090, 24564 MiB\n1, NVIDIA GeForce RTX 4090, 24564 MiB\n"
        if remote[:2] == ["sh", "-c"]:
            if "containerd" in remote[2]:
                return "gpu-containerd", "12:default_runtime_name = \"nvidia\"\n"
            return "gpu-listeners", "tcp LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\ntcp LISTEN 0 4096 *:10250 *:*\n"
        if remote[:2] == ["/usr/local/bin/k3s", "kubectl"]:
            rest = remote[2:]
            if "apply" in rest:
                return "gpu-apply", ""
            if "rollout" in rest:
                return "gpu-rollout", ""
            if rest[:2] == ["get", "nodes"]:
                node = {"items": [{
                    "metadata": {"name": "vm-node", "labels": {"srecon26.io/vllm-gpu": "true"}},
                    "status": {"conditions": [{"type": "Ready", "status": "True"}], "allocatable": {"nvidia.com/gpu": self.allocatable}},
                }]}
                return "gpu-nodes", json.dumps(node)
            if "deployment" in rest:
                return "scale-deployment", json.dumps({"spec": {"replicas": 2}, "status": {"readyReplicas": 2}})
            if "hpa" in rest:
                return "scale-hpa", json.dumps({"status": {"desiredReplicas": 2, "currentReplicas": 2}})
        raise AssertionError(remote)

    def __call__(self, arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        argv = list(arguments)
        assert kwargs["shell"] is False and kwargs["timeout"] > 0
        if argv[0] == "ssh":
            assert "-p" in argv and "-P" not in argv and argv[-2] == "root@203.0.113.10"
            assert argv[argv.index("-p") + 1] == "40022"
            name, stdout = self._classify_ssh(shlex.split(argv[-1]))
            if name == "deploy" and self.deploy_delay:
                time.sleep(self.deploy_delay)
        elif argv[0] == "scp":
            assert "-P" in argv and "-p" not in argv
            if ":" in argv[-2]:
                name, stdout = "fetch", ""
                destination = Path(argv[-1])
                destination.mkdir(parents=True)
                (destination / "collection-status.json").write_text("{}\n", encoding="utf-8")
            else:
                name, stdout = f"up:{argv[-1].rsplit('/', 1)[1]}", ""
        else:
            raise AssertionError(argv)
        self.calls.append(name)
        code = 1 if name == self.fail else 0
        return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr=f"{name} failed" if code else "")


HAPPY_CALLS = [
    "wait-ssh", "mkdir",
    "up:remote_host_canary.sh", "fetch-k3s", "chmod-k3s", "up:nvidia-runtime.toml", "up:device-plugin.yaml",
    "verify-staged", "probe", "runtime-dropin", "install",
    "gpu-smi", "gpu-containerd", "gpu-listeners", "gpu-apply", "gpu-rollout", "gpu-nodes",
    "up:manifests", "deploy", "scale-deployment", "scale-hpa", "collect", "fetch", "cleanup",
]
HAPPY_STEPS = [
    "wait-ssh", "mkdir", "stage-script", "fetch-k3s", "chmod-k3s", "stage-runtime", "stage-device-plugin",
    "verify-staged", "probe", "runtime-dropin", "install", "gpu-check", "stage-manifests",
    "deploy", "scale-observe", "collect", "fetch-evidence", "cleanup",
]


def test_repo_canary_script_pins_the_module_k3s_checksum() -> None:
    assert f'readonly K3S_AMD64_SHA256="{K3S_AMD64_SHA256}"' in (REPO / "scripts" / "remote_host_canary.sh").read_text(encoding="utf-8")


def test_plan_is_signed_with_vm_template_and_prints_confirmation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = FakeVmProvider()
    plan_path, confirm = _plan(monkeypatch, tmp_path, capsys, provider)

    envelope = json.loads(plan_path.read_text(encoding="utf-8"))
    document = envelope["document"]
    assert envelope["document_sha256"] == confirm == hpa._sha256_text(hpa._canonical_json(document))
    assert provider.search_queries == [hpa.DEFAULT_QUERY]
    assert provider.create_kwargs == [] and provider.destroy_calls == 0
    assert document["run_identity"]["run_id"] == f"single-vm-hpa-20260928T120000Z-{NONCE[:8]}"
    assert document["run_identity"]["label"] == LABEL and len(LABEL) <= 64
    assert document["launch_contract"] == {
        "template_hash": VM_TEMPLATE_HASH, "image_contract": "docker.io/vastai/kvm:ubuntu_cli_22.04-2025-05-16",
        "disk_gib": 130, "ssh": True, "direct": True, "cancel_unavail": True,
    }
    assert document["offer"]["gpu_count"] == 2 and document["offer"]["vms_enabled"] is True
    workload = document["remote_workload"]
    assert workload["remote_root"].startswith("/var/tmp/srecon26-vm-")
    assert workload["files"]["k3s_binary"]["sha256"] == hpa.K3S_AMD64_SHA256
    assert workload["files"]["manifest_bundle"]["sha256"] == hpa.SshRemoteWorkload._manifest_hash(hpa.MANIFEST_DIR)
    assert workload["load_env"] == {"CANARY_LOAD_CONCURRENCY": 32, "CANARY_LOAD_MAX_TOKENS": 512, "CANARY_LOAD_SECONDS": 120}
    assert document["guard_binding"]["rpc_timeout_seconds"] == 150
    assert document["guard_binding"]["cli_timeout_seconds"] == 30
    assert document["guard_binding"]["heartbeat_timeout_seconds"] == 600
    assert document["spend_limits"]["max_allowed_spend_usd"] == "2.064"  # 1.20 * 1.5h + 0.004 * (64 + 2) GB


def test_execute_rejects_tampered_plan_or_wrong_confirmation_before_clients(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan_path, confirm = _plan(monkeypatch, tmp_path, capsys, FakeVmProvider())
    identity = _identity(tmp_path)
    calls = {"client": 0}
    monkeypatch.setattr(hpa, "SDK_CLIENT_FACTORY", lambda api_key=None: calls.__setitem__("client", calls["client"] + 1) or object())

    assert hpa.main(_execute_args(plan_path, "0" * 64, identity, tmp_path)) == 2
    envelope = json.loads(plan_path.read_text(encoding="utf-8"))
    envelope["document"]["offer"]["dph"] = "0.01"
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(envelope), encoding="utf-8")
    assert hpa.main(_execute_args(tampered, confirm, identity, tmp_path)) == 2
    assert "does not match its recorded sha256" in capsys.readouterr().err
    assert calls["client"] == 0


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"machine_id": 15326}, "excluded"),
        ({"num_gpus": 1}, "exactly 2 GPUs"),
        ({"gpu_name": "RTX 4060 Ti"}, "approved"),
        ({"compute_capability": "7.5"}, "compute capability"),
        ({"gpu_ram_mib": 16380}, "VRAM"),
        ({"reliability": Decimal("0.99")}, "reliability"),
        ({"inet_down_mbps": Decimal("100")}, "inet_down"),
        ({"direct_port_count": 0}, "direct ports"),
        ({"vms_enabled": False}, "vms_enabled"),
        ({"cpu_ram_mib": 32000}, "system RAM"),
        ({"cpu_cores": Decimal("4")}, "CPU cores"),
    ],
)
def test_offer_eligibility_rejects_each_filter(changes: dict[str, object], reason: str) -> None:
    assert vm_offer_rejection(_offer()) is None
    rejection = vm_offer_rejection(_offer(**changes))
    assert rejection is not None and reason in rejection


def test_select_offer_skips_excluded_and_picks_cheapest_total(monkeypatch: pytest.MonkeyPatch) -> None:
    assert {99239, 17545, 150513, 15881, 44906, 57783, 41599, 67809, 15326, 20126, 29929} == EXCLUDED_MACHINE_IDS
    for token in ("verified=true", "rentable=true", "external=false", "vms_enabled=true", "num_gpus=2", "reliability>0.99"):
        assert token in hpa.DEFAULT_QUERY.split()
    excluded_cheapest = _offer(offer_id=1, machine_id=15326, dph_total=Decimal("0.20"))
    cheap_dph_expensive_network = _offer(offer_id=2, machine_id=2, dph_total=Decimal("0.60"), inet_down_cost=Decimal("0.05"))
    best = _offer(offer_id=3, machine_id=3, dph_total=Decimal("0.70"), inet_down_cost=Decimal("0.001"))
    over_max = _offer(offer_id=4, machine_id=4, dph_total=Decimal("1.30"))
    provider = FakeVmProvider([excluded_cheapest, cheap_dph_expensive_network, best, over_max])

    offer, plan = hpa.select_offer(provider, query="q", limit=25, label=LABEL, max_dph=Decimal("1.20"), deadline_minutes=90)

    assert offer.sdk.offer_id == 3 and plan.offer_id == 3 and plan.disk_gib == VM_DISK_GIB
    with pytest.raises(hpa.SingleVmHpaError, match="no eligible"):
        hpa.select_offer(FakeVmProvider([excluded_cheapest, over_max]), query="q", limit=25, label=LABEL, max_dph=Decimal("1.20"), deadline_minutes=90)


def test_spend_ceiling_rejects_plan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    provider = FakeVmProvider()
    monkeypatch.setattr(hpa, "VM_PROVIDER_FACTORY", lambda client: provider)
    assert hpa.main(_plan_args(tmp_path / "a.json", "--max-usd", "1.40")) == 2
    assert "exceeds --max-usd" in capsys.readouterr().err
    assert hpa.main(_plan_args(tmp_path / "b.json", "--max-usd", "2.00", "--max-dph", "1.20")) == 2
    assert "max_dph plus network allowance exceeds --max-usd" in capsys.readouterr().err
    assert hpa.main(_plan_args(tmp_path / "c.json", "--deadline-minutes", "45")) == 2
    assert hpa.main(_plan_args(tmp_path / "d.json", "--deadline-minutes", "60", "--deploy-timeout-seconds", "3000")) == 2
    assert "exceed the 60-minute deadline" in capsys.readouterr().err
    assert provider.create_kwargs == []


def test_vm_launch_contract_only_allows_the_approved_template() -> None:
    contract = VmLaunchContract()
    contract.validate()
    assert contract.create_kwargs(offer_id=7, label=LABEL) == {
        "id": 7, "template_hash": VM_TEMPLATE_HASH, "disk": 130, "label": LABEL,
        "ssh": True, "direct": True, "cancel_unavail": True,
    }
    for bad in (
        VmLaunchContract(template_hash=OFFICIAL_UBUNTU_DESKTOP_TEMPLATE_HASH, image_contract="docker.io/vastai/kvm:ubuntu_desktop_22.04-2025-11-21"),
        VmLaunchContract(template_hash="0" * 32),
        VmLaunchContract(image_contract="docker.io/vastai/kvm:latest"),
        VmLaunchContract(disk_gib=200),
        VmLaunchContract(ssh=False),
        VmLaunchContract(cancel_unavail=False),
        VmLaunchContract(env={"HF_TOKEN": "secret"}),
        VmLaunchContract(onstart="curl example.invalid | sh"),
    ):
        with pytest.raises(VastSdkError):
            bad.validate()


class FakeSdkClient:
    def __init__(self, *, create_response: object = None, create_error: Exception | None = None) -> None:
        self.create_response, self.create_error = create_response, create_error
        self.calls: list[tuple[str, dict[str, object]]] = []

    def search_offers(self, **kwargs: object) -> object:
        self.calls.append(("search_offers", kwargs))
        record = {
            "id": 101, "machine_id": 202, "gpu_name": "RTX 4090", "num_gpus": 2, "gpu_ram": 24564,
            "compute_cap": 890, "dph_total": 0.8, "reliability2": 0.995, "direct_port_count": 8,
            "inet_down": 900.0, "inet_down_cost": 0.004, "inet_up_cost": 0.004,
            "cpu_ram": 64390, "cpu_cores_effective": 16.0, "vms_enabled": True,
        }
        return json.dumps([record, {**record, "id": 102, "vms_enabled": None}])

    def create_instance(self, **kwargs: object) -> object:
        self.calls.append(("create_instance", kwargs))
        if self.create_error is not None:
            raise self.create_error
        return self.create_response

    def show_instance(self, **kwargs: object) -> object:
        self.calls.append(("show_instance", kwargs))
        return json.dumps({
            "id": kwargs["id"], "machine_id": 202, "gpu_name": "RTX 4090", "num_gpus": 2, "gpu_ram": 24564,
            "compute_cap": 890, "dph_total": 0.8, "label": LABEL, "actual_status": "loading",
        })


def test_vm_provider_search_prices_130_gib_and_skips_incomplete_records() -> None:
    client = FakeSdkClient()
    offers = VmSdkProvider(client).search_vm_offers("vms_enabled=true num_gpus=2", limit=5, label=LABEL)  # type: ignore[arg-type]
    assert client.calls == [("search_offers", {"query": "vms_enabled=true num_gpus=2", "limit": 5, "storage": 130})]
    assert [offer.sdk.offer_id for offer in offers] == [101]
    assert offers[0].cpu_ram_mib == 64390 and offers[0].vms_enabled is True and offers[0].sdk.compute_capability == "8.9"


def test_vm_provider_create_uses_template_hash_once_and_is_ambiguous_on_error() -> None:
    offer = _offer().sdk
    client = FakeSdkClient(create_response={"success": True, "new_contract": 9001})
    instance = VmSdkProvider(client).create_once(offer, VmLaunchContract())  # type: ignore[arg-type]
    assert instance.instance_id == 9001 and instance.label == LABEL
    assert client.calls[0] == ("create_instance", {
        "id": 101, "template_hash": VM_TEMPLATE_HASH, "disk": 130, "label": LABEL,
        "ssh": True, "direct": True, "cancel_unavail": True,
    })
    assert "image" not in client.calls[0][1] and "env" not in client.calls[0][1] and "onstart_cmd" not in client.calls[0][1]

    for failing in (FakeSdkClient(create_error=RuntimeError("https://x?api_key=secret")), FakeSdkClient(create_response={"success": False})):
        with pytest.raises(AmbiguousCreate) as caught:
            VmSdkProvider(failing).create_once(offer, VmLaunchContract())  # type: ignore[arg-type]
        assert "secret" not in str(caught.value)
        assert [name for name, _ in failing.calls] == ["create_instance"]


def test_k3s_fetch_failure_falls_back_to_signed_upload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = FakeVmProvider()
    plan_path, confirm = _plan(monkeypatch, tmp_path, capsys, provider)
    monkeypatch.setattr(hpa, "RUN_COMMAND_GUARD_FACTORY", lambda config, nonce, script_hash: FakeGuard(provider))
    runner = ScriptedRunner(fail="fetch-k3s")
    monkeypatch.setattr(hpa, "SUBPROCESS_RUNNER", runner)

    assert hpa.main(_execute_args(plan_path, confirm, _identity(tmp_path), tmp_path)) == 0
    assert runner.calls[3:5] == ["fetch-k3s", "up:k3s"]
    assert "verify-staged" in runner.calls


def test_execute_happy_path_exact_remote_order_destroy_and_absence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = FakeVmProvider()
    plan_path, confirm = _plan(monkeypatch, tmp_path, capsys, provider)

    def guard_factory(config: Any, nonce: str, script_hash: str) -> FakeGuard:
        assert (config.timeout_seconds, config.cli_timeout_seconds, config.heartbeat_timeout_seconds) == (150, 30, 600)
        assert nonce == NONCE and script_hash == _sha(hpa.DEFAULT_GUARD_WORKER)
        return FakeGuard(provider)

    monkeypatch.setattr(hpa, "RUN_COMMAND_GUARD_FACTORY", guard_factory)
    runner = ScriptedRunner()
    monkeypatch.setattr(hpa, "SUBPROCESS_RUNNER", runner)

    assert hpa.main(_execute_args(plan_path, confirm, _identity(tmp_path), tmp_path)) == 0

    out = plan_path.parent
    assert runner.calls == HAPPY_CALLS
    assert provider.arm_calls == 1 and provider.heartbeat_calls >= 4
    assert provider.create_kwargs == [{
        "id": 101, "template_hash": VM_TEMPLATE_HASH, "disk": 130, "label": LABEL,
        "ssh": True, "direct": True, "cancel_unavail": True,
    }]
    assert provider.attached == [(9001, LABEL, PUBLIC_KEY)]
    assert provider.destroy_calls == 1 and provider.absence_reads == 3
    execution = json.loads((out / "single-vm-hpa-execution.json").read_text())
    assert execution["steps"] == HAPPY_STEPS
    assert execution["absence_reads"] == 3 and execution["ssh_route"] == "direct_public_ipaddr_22_tcp_hostport"
    assert execution["gpu_check"]["node"]["allocatable_gpus"] == "2"
    assert execution["gpu_check"]["non_ssh_wildcard_listeners"] == ["*:10250"]
    assert execution["scale_observation"]["reached_two_ready_replicas"] is True
    assert (out / "server-evidence" / "collection-status.json").exists()
    for step in ("deploy", "gpu-check", "install", "cleanup", "fetch-k3s"):
        assert (out / f"{step}.log").exists()
    deploy_env = runner.remote_env["deploy"]
    assert f"CANARY_VLLM_IMAGE=docker.io/vllm/vllm-openai@{hpa.VLLM_IMAGE_DIGEST}" in deploy_env
    assert f"CANARY_MODEL={hpa.FROZEN_MODEL_ID}" in deploy_env and f"CANARY_MODEL_REVISION={hpa.FROZEN_MODEL_REVISION}" in deploy_env
    assert "CANARY_HARD_DEADLINE=2026-09-28T13:30:00Z" in deploy_env
    assert any(item.startswith("CANARY_MANIFEST_SHA256=") for item in deploy_env)
    assert "CANARY_LOAD_SECONDS=120" in deploy_env
    assert not any(item.startswith(("CANARY_K3S_", "CANARY_EXPECTED_NODE_NAMES")) for values in runner.remote_env.values() for item in values)
    assert hpa.main(_execute_args(plan_path, confirm, _identity(tmp_path), tmp_path)) == 2  # replay refused


@pytest.mark.parametrize(("fail", "stage"), [("deploy", "deploy"), ("gpu-nodes", "gpu-check")])
def test_failure_still_fetches_evidence_destroys_and_writes_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], fail: str, stage: str
) -> None:
    provider = FakeVmProvider()
    plan_path, confirm = _plan(monkeypatch, tmp_path, capsys, provider)
    monkeypatch.setattr(hpa, "RUN_COMMAND_GUARD_FACTORY", lambda config, nonce, script_hash: FakeGuard(provider))
    runner = ScriptedRunner(fail=fail)
    monkeypatch.setattr(hpa, "SUBPROCESS_RUNNER", runner)

    assert hpa.main(_execute_args(plan_path, confirm, _identity(tmp_path), tmp_path)) == 2

    out = plan_path.parent
    assert "fetch" in runner.calls
    assert runner.calls[-1] == ("cleanup" if fail == "deploy" else "fetch")
    assert "collect" not in runner.calls
    assert provider.destroy_calls == 1 and provider.absence_reads == 3
    failure = json.loads((out / "single-vm-hpa-failure.json").read_text())
    assert failure["stage"] == stage and failure["plan_sha256"] == confirm
    assert failure["teardown_and_absence_confirmed"] is True and failure["evidence_fetched"] is True
    assert failure["error_chain"][0].startswith("SingleHostError") and any(f"remote step {stage}" in item for item in failure["error_chain"])
    assert (out / "server-evidence" / "collection-status.json").exists()
    assert not (out / "single-vm-hpa-execution.json").exists()


def test_single_allocatable_gpu_fails_gpu_check_before_deploy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = FakeVmProvider()
    plan_path, confirm = _plan(monkeypatch, tmp_path, capsys, provider)
    monkeypatch.setattr(hpa, "RUN_COMMAND_GUARD_FACTORY", lambda config, nonce, script_hash: FakeGuard(provider))
    monkeypatch.setattr(hpa, "GPU_ALLOCATABLE_POLL_ATTEMPTS", 2)
    runner = ScriptedRunner(allocatable="1")
    monkeypatch.setattr(hpa, "SUBPROCESS_RUNNER", runner)

    assert hpa.main(_execute_args(plan_path, confirm, _identity(tmp_path), tmp_path)) == 2

    assert runner.calls.count("gpu-nodes") == 2 and "deploy" not in runner.calls and "up:manifests" not in runner.calls
    assert provider.destroy_calls == 1 and provider.absence_reads == 3
    failure = json.loads((plan_path.parent / "single-vm-hpa-failure.json").read_text())
    assert failure["stage"] == "gpu-check"
    assert any('nvidia.com/gpu == "2"' in item for item in failure["error_chain"])


def test_heartbeats_continue_during_long_remote_deploy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = FakeVmProvider()
    plan_path, confirm = _plan(monkeypatch, tmp_path, capsys, provider)
    monkeypatch.setattr(hpa, "RUN_COMMAND_GUARD_FACTORY", lambda config, nonce, script_hash: FakeGuard(provider))
    monkeypatch.setattr(hpa, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
    runner = ScriptedRunner(deploy_delay=0.3)
    monkeypatch.setattr(hpa, "SUBPROCESS_RUNNER", runner)

    assert hpa.main(_execute_args(plan_path, confirm, _identity(tmp_path), tmp_path)) == 0
    assert provider.heartbeat_calls >= 10


def test_offer_drift_or_non_empty_inventory_fail_before_create(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan_path, confirm = _plan(monkeypatch, tmp_path, capsys, FakeVmProvider())
    identity = _identity(tmp_path)
    drift = FakeVmProvider(current=_offer(dph_total=Decimal("0.81")))
    monkeypatch.setattr(hpa, "VM_PROVIDER_FACTORY", lambda client: drift)
    monkeypatch.setattr(hpa, "RUN_COMMAND_GUARD_FACTORY", lambda config, nonce, script_hash: FakeGuard(drift))
    assert hpa.main(_execute_args(plan_path, confirm, identity, tmp_path)) == 2
    assert "drifted" in capsys.readouterr().err
    assert drift.create_kwargs == [] and drift.arm_calls == 0

    busy = FakeVmProvider(live=[_instance(instance_id=1, label="other")])
    monkeypatch.setattr(hpa, "VM_PROVIDER_FACTORY", lambda client: busy)
    monkeypatch.setattr(hpa, "RUN_COMMAND_GUARD_FACTORY", lambda config, nonce, script_hash: FakeGuard(busy))
    assert hpa.main(_execute_args(plan_path, confirm, identity, tmp_path)) == 2
    assert busy.create_kwargs == [] and busy.arm_calls == 0 and busy.destroy_calls == 0


class AttachClient(FakeSdkClient):
    def __init__(self, *, account_keys: list[dict[str, object]], attach_response: object) -> None:
        super().__init__()
        self.account_keys, self.attach_response = account_keys, attach_response

    def show_ssh_keys(self) -> object:
        self.calls.append(("show_ssh_keys", {}))
        return json.dumps(self.account_keys)

    def attach_ssh(self, **kwargs: object) -> object:
        self.calls.append(("attach_ssh", kwargs))
        return self.attach_response


def test_attach_skips_keys_already_registered_on_the_account() -> None:
    client = AttachClient(account_keys=[{"id": 1, "public_key": PUBLIC_KEY.rsplit(" ", 1)[0] + " other@host"}], attach_response={"success": False})
    VmSdkProvider(client).attach_ssh_key(9001, LABEL, PUBLIC_KEY)  # type: ignore[arg-type]
    assert "attach_ssh" not in [name for name, _ in client.calls]

    client = AttachClient(account_keys=[], attach_response={"success": False})
    with pytest.raises(VastSdkError, match="rejected"):
        VmSdkProvider(client).attach_ssh_key(9001, LABEL, PUBLIC_KEY)  # type: ignore[arg-type]
