"""Contracts and a fail-closed lease for one 2-GPU Vast KVM VM running k3s + HPA.

The lease reuses ``SingleHostLease``'s lifecycle unchanged (empty inventory,
one armed guard, one paid create reconciled by label only, exact verification,
workload, one exact destroy, three-read absence proof); only plan and launch
validation differ because a VM is created from an approved template hash.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Mapping

from .provider import AmbiguousCreate
from .single_host_multigpu import SingleHostError, SingleHostLease
from .vast_provider import APPROVED_VM_TEMPLATES, OFFICIAL_KVM_IMAGE, OFFICIAL_UBUNTU_2204_TEMPLATE_HASH
from .vast_sdk_adapter import (
    SdkInstance,
    SdkOffer,
    VastSdkClient,
    VastSdkError,
    VastSdkProvider,
    _decimal,
    _validate_label,
    normalize_payload,
    normalize_records,
)

VM_TEMPLATE_HASH = OFFICIAL_UBUNTU_2204_TEMPLATE_HASH
VM_TEMPLATE_IMAGE = OFFICIAL_KVM_IMAGE
VM_DISK_GIB = 130
REQUIRED_GPU_COUNT = 2
MIN_GPU_RAM_MIB = 24000
ALLOWED_GPU_NAMES = frozenset({"RTX 4090", "RTX 3090", "RTX 3090 Ti", "RTX A5000", "RTX A6000", "RTX 6000Ada"})
ALLOWED_COMPUTE_CAPABILITIES = frozenset({"8.6", "8.9"})
MIN_RELIABILITY_EXCLUSIVE = Decimal("0.99")
MIN_INET_DOWN_MBPS = Decimal("200")
MIN_DIRECT_PORTS = 1
# Two vLLM pods request 2 CPU / 12 GiB each, plus Prometheus, adapter, metrics-server.
# Vast reports cpu_ram in MB and the SDK query multiplies GB by 1000, so 48 GB == 48000.
MIN_CPU_RAM_MIB = 48000
MIN_CPU_CORES = Decimal("8")
MIN_DEADLINE_MINUTES = 60
MAX_DEADLINE_MINUTES = 120
# COPILOT-HANDOFF.md "Do not use known failed or excluded machines".
EXCLUDED_MACHINE_IDS = frozenset({99239, 17545, 150513, 15881, 44906, 57783, 41599, 67809, 15326, 20126, 29929})
K3S_AMD64_SHA256 = "835873f37245fc615f547a2fe2af9402a347875f13fa64a1f136de644955ea3f"
_SSH_PUBLIC_KEY = re.compile(r"ssh-(?:rsa|ed25519) [A-Za-z0-9+/=]+(?: [^\r\n]+)?")


@dataclass(frozen=True, slots=True)
class VmOffer:
    """An SDK offer plus the VM-only facts the SDK dataclass drops."""

    sdk: SdkOffer
    cpu_ram_mib: int
    cpu_cores: Decimal
    vms_enabled: bool

    @classmethod
    def from_record(cls, record: Mapping[str, object], label: str) -> VmOffer:
        vms_enabled = record.get("vms_enabled")
        if not isinstance(vms_enabled, bool):
            raise VastSdkError("missing or invalid vms_enabled")
        return cls(
            SdkOffer.from_record(record, label),
            int(_decimal(record.get("cpu_ram"), "cpu_ram")),
            _decimal(record.get("cpu_cores_effective"), "cpu_cores_effective"),
            vms_enabled,
        )


def vm_offer_rejection(offer: VmOffer) -> str | None:
    """Return why an offer is ineligible for the single-VM HPA run, else None."""
    sdk = offer.sdk
    if offer.vms_enabled is not True:
        return "offer is not vms_enabled"
    if sdk.machine_id in EXCLUDED_MACHINE_IDS:
        return "machine is on the excluded list"
    if sdk.num_gpus != REQUIRED_GPU_COUNT:
        return "offer must have exactly 2 GPUs"
    if sdk.gpu_name not in ALLOWED_GPU_NAMES:
        return "GPU model is not an approved 24 GB+ Ada/Ampere part"
    if sdk.compute_capability not in ALLOWED_COMPUTE_CAPABILITIES:
        return "GPU compute capability is not Ampere 8.6 or Ada 8.9"
    if sdk.gpu_ram_mib < MIN_GPU_RAM_MIB:
        return "GPU VRAM is below 24 GB"
    if sdk.reliability <= MIN_RELIABILITY_EXCLUSIVE:
        return "reliability must exceed 0.99"
    if sdk.inet_down_mbps < MIN_INET_DOWN_MBPS:
        return "inet_down is below 200 Mbps"
    if sdk.direct_port_count < MIN_DIRECT_PORTS:
        return "offer has no direct ports"
    if offer.cpu_ram_mib < MIN_CPU_RAM_MIB:
        return "system RAM is below 48 GB"
    if offer.cpu_cores < MIN_CPU_CORES:
        return "effective CPU cores are below 8"
    return None


@dataclass(frozen=True, slots=True)
class VmPlan:
    offer_id: int
    machine_id: int
    gpu_name: str
    gpu_count: int
    gpu_ram_mib: int
    compute_capability: str
    dph: Decimal
    max_dph: Decimal
    reliability: Decimal
    direct_port_count: int
    inet_down_mbps: Decimal
    cpu_ram_mib: int
    cpu_cores: Decimal
    disk_gib: int
    deadline_minutes: int
    template_hash: str

    def __post_init__(self) -> None:
        candidate = VmOffer(
            SdkOffer(
                self.offer_id, self.machine_id, self.gpu_name, self.gpu_count, self.gpu_ram_mib,
                self.compute_capability, self.dph, self.reliability, self.direct_port_count,
                self.inet_down_mbps, "plan",
            ),
            self.cpu_ram_mib,
            self.cpu_cores,
            True,
        )
        reason = vm_offer_rejection(candidate)
        if reason is not None:
            raise ValueError(reason)
        if not self.max_dph.is_finite() or self.max_dph <= 0 or self.dph > self.max_dph:
            raise ValueError("offer dph exceeds max_dph")
        if self.disk_gib != VM_DISK_GIB:
            raise ValueError(f"disk must be exactly {VM_DISK_GIB} GiB")
        if type(self.deadline_minutes) is not int or not MIN_DEADLINE_MINUTES <= self.deadline_minutes <= MAX_DEADLINE_MINUTES:
            raise ValueError(f"deadline_minutes must be in [{MIN_DEADLINE_MINUTES}, {MAX_DEADLINE_MINUTES}]")
        if self.template_hash != VM_TEMPLATE_HASH:
            raise ValueError("template hash must be the approved Ubuntu 22.04 CLI KVM template")

    @classmethod
    def for_offer(cls, offer: VmOffer, *, max_dph: Decimal, deadline_minutes: int) -> VmPlan:
        sdk = offer.sdk
        return cls(
            sdk.offer_id, sdk.machine_id, sdk.gpu_name, sdk.num_gpus, sdk.gpu_ram_mib, sdk.compute_capability,
            sdk.dph_total, max_dph, sdk.reliability, sdk.direct_port_count, sdk.inet_down_mbps,
            offer.cpu_ram_mib, offer.cpu_cores, VM_DISK_GIB, deadline_minutes, VM_TEMPLATE_HASH,
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> VmPlan:
        values: dict[str, Any] = dict(payload)
        for name in ("dph", "max_dph", "reliability", "inet_down_mbps", "cpu_cores"):
            raw = values.get(name)
            if not isinstance(raw, str):
                raise ValueError(f"vm_plan.{name} must be a decimal string")
            values[name] = Decimal(raw)
        return cls(**values)

    def canonical_dict(self) -> dict[str, object]:
        return {key: (format(value, "f") if isinstance(value, Decimal) else value) for key, value in asdict(self).items()}

    def sha256(self) -> str:
        encoded = json.dumps(self.canonical_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class VmLaunchContract:
    """Exact VM create settings: approved template hash, 130 GiB, no env/onstart payloads."""

    template_hash: str = VM_TEMPLATE_HASH
    image_contract: str = VM_TEMPLATE_IMAGE
    disk_gib: int = VM_DISK_GIB
    ssh: bool = True
    direct: bool = True
    cancel_unavail: bool = True
    env: Mapping[str, str] | None = field(default=None, repr=False)
    onstart: str | None = field(default=None, repr=False)

    def validate(self) -> None:
        if self.template_hash != VM_TEMPLATE_HASH or APPROVED_VM_TEMPLATES.get(self.template_hash) != self.image_contract:
            raise VastSdkError("VM launch must use the approved Ubuntu 22.04 CLI KVM template hash")
        if type(self.disk_gib) is not int or self.disk_gib != VM_DISK_GIB:
            raise VastSdkError(f"VM launch disk must be exactly {VM_DISK_GIB} GiB")
        if self.ssh is not True or self.direct is not True or self.cancel_unavail is not True:
            raise VastSdkError("VM launch requires ssh, direct, and cancel_unavail")
        if self.env or self.onstart:
            raise VastSdkError("VM launch forbids env and onstart payloads because they can carry secrets")

    def create_kwargs(self, *, offer_id: int, label: str) -> dict[str, object]:
        self.validate()
        # With template_hash the SDK omits runtype; the template supplies image and ssh_direct runtype.
        return {
            "id": offer_id,
            "template_hash": self.template_hash,
            "disk": self.disk_gib,
            "label": label,
            "ssh": True,
            "direct": True,
            "cancel_unavail": True,
        }

    def payload(self) -> dict[str, object]:
        return {
            "template_hash": self.template_hash,
            "image_contract": self.image_contract,
            "disk_gib": self.disk_gib,
            "ssh": self.ssh,
            "direct": self.direct,
            "cancel_unavail": self.cancel_unavail,
        }


class VmSdkProvider:
    """VM search/create/attach on the official SDK; inventory and destroy delegate to ``VastSdkProvider``."""

    def __init__(self, client: VastSdkClient, *, base: VastSdkProvider | None = None) -> None:
        self._client = client
        self._base = VastSdkProvider(client) if base is None else base

    def __repr__(self) -> str:
        return "VmSdkProvider(client=<redacted>)"

    def search_vm_offers(self, query: str, *, limit: int = 25, label: str = "") -> tuple[VmOffer, ...]:
        if not isinstance(query, str) or not query.strip():
            raise VastSdkError("offer query must be a non-empty string")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise VastSdkError("offer limit must be an integer 1-100")
        if label:
            _validate_label(label)
        try:
            raw = self._client.search_offers(query=query, limit=limit, storage=VM_DISK_GIB)
        except Exception as error:
            raise VastSdkError(f"Vast SDK search_offers failed ({type(error).__name__})") from None
        offers: list[VmOffer] = []
        for record in normalize_records(raw):
            try:
                offers.append(VmOffer.from_record(record, label))
            except VastSdkError:
                # An offer missing a required field can never be selected or re-matched.
                continue
        return tuple(offers)

    def get_vm_offer(self, offer_id: int, machine_id: int, label: str) -> VmOffer:
        _validate_label(label)
        query = f"machine_id={int(machine_id)} vms_enabled=true num_gpus={REQUIRED_GPU_COUNT}"
        matches = [offer for offer in self.search_vm_offers(query, label=label) if offer.sdk.offer_id == offer_id]
        if len(matches) != 1 or matches[0].sdk.machine_id != machine_id:
            raise VastSdkError("current VM offer is not uniquely available on the expected machine")
        return matches[0]

    def create_once(self, offer: SdkOffer, contract: VmLaunchContract) -> SdkInstance:
        if not isinstance(offer, SdkOffer) or not isinstance(contract, VmLaunchContract):
            raise VastSdkError("VM create requires an SdkOffer and VmLaunchContract")
        label = _validate_label(offer.label)
        kwargs = contract.create_kwargs(offer_id=offer.offer_id, label=label)
        try:
            response = self._client.create_instance(**kwargs)
        except Exception as error:
            raise AmbiguousCreate(f"Vast SDK create_instance outcome unknown ({type(error).__name__}); reconcile by label, never retry") from None
        try:
            payload = normalize_payload(response)
        except VastSdkError:
            raise AmbiguousCreate("Vast SDK create_instance returned no JSON; reconcile by label, never retry") from None
        contract_id = payload.get("new_contract") if isinstance(payload, Mapping) and payload.get("success") is True else None
        if isinstance(contract_id, bool) or not isinstance(contract_id, int) or contract_id <= 0:
            raise AmbiguousCreate("Vast SDK create_instance did not confirm a new contract; reconcile by label, never retry")
        try:
            instance = self._base.get_instance(contract_id)
        except VastSdkError:
            raise AmbiguousCreate("new VM contract was not readable; reconcile by label, never retry") from None
        if instance.label != label or instance.machine_id != offer.machine_id:
            raise AmbiguousCreate("new VM contract does not match the requested offer; reconcile by label, never retry")
        return instance

    def attach_ssh_key(self, instance_id: int, expected_label: str, public_key: str) -> None:
        if not isinstance(public_key, str) or not _SSH_PUBLIC_KEY.fullmatch(public_key.strip()):
            raise VastSdkError("SSH public key is invalid")
        current = self._base.get_instance(instance_id)
        if current.label != expected_label:
            raise VastSdkError("instance label changed; refusing SSH key attach")
        if self._account_has_key(public_key):
            # Vast injects account-level keys at create; attach then reports success=false.
            return
        try:
            response = self._client.attach_ssh(instance_id=instance_id, ssh_key=public_key.strip())
        except Exception as error:
            raise VastSdkError(f"Vast SDK attach_ssh failed ({type(error).__name__})") from None
        try:
            payload = normalize_payload(response)
        except VastSdkError:
            raise VastSdkError("Vast SDK attach_ssh was not confirmed") from None
        if isinstance(payload, Mapping) and payload.get("success") is False:
            raise VastSdkError("Vast SDK attach_ssh was rejected")

    def reconcile_label(self, label: str) -> SdkInstance | None:
        return self._base.reconcile_label(label)

    def _account_has_key(self, public_key: str) -> bool:
        wanted = public_key.strip().split()[:2]
        try:
            payload = normalize_payload(self._client.show_ssh_keys())
        except Exception:
            return False
        records = payload if isinstance(payload, list) else (payload.get("keys") or []) if isinstance(payload, Mapping) else []
        for record in records:
            if isinstance(record, Mapping):
                key = record.get("public_key") or record.get("key") or record.get("ssh_key")
                if isinstance(key, str) and key.strip().split()[:2] == wanted:
                    return True
        return False

    def get_instance(self, instance_id: int) -> SdkInstance:
        return self._base.get_instance(instance_id)

    def list_instances(self) -> tuple[SdkInstance, ...]:
        return self._base.list_instances()

    def destroy_exact(self, instance_id: int, expected_label: str) -> None:
        self._base.destroy_exact(instance_id, expected_label)


_OFFER_PLAN_FIELDS = (
    ("offer_id", "offer_id"),
    ("machine_id", "machine_id"),
    ("gpu_name", "gpu_name"),
    ("num_gpus", "gpu_count"),
    ("gpu_ram_mib", "gpu_ram_mib"),
    ("compute_capability", "compute_capability"),
    ("dph_total", "dph"),
    ("reliability", "reliability"),
    ("direct_port_count", "direct_port_count"),
    ("inet_down_mbps", "inet_down_mbps"),
)


class VmHpaLease(SingleHostLease):
    """``SingleHostLease`` lifecycle bound to a ``VmPlan`` and ``VmLaunchContract``."""

    def _validate(self) -> None:
        plan, launch = self.plan, self.launch
        if not isinstance(self.offer, SdkOffer) or not isinstance(plan, VmPlan) or not isinstance(launch, VmLaunchContract):
            raise SingleHostError("VM lease requires SdkOffer, VmPlan, and VmLaunchContract")
        if not isinstance(self.run_id, str) or not self.run_id.strip() or not isinstance(self.nonce, str) or not self.nonce.strip():
            raise SingleHostError("VM lease requires a run id and nonce")
        label = self.offer.label
        prefix = label.removesuffix(f"--nonce-{self.nonce}") if isinstance(label, str) else ""
        if not prefix or prefix == label or "--nonce-" in prefix:
            raise SingleHostError("offer label is not bound to this exact nonce")
        for offer_field, plan_field in _OFFER_PLAN_FIELDS:
            if getattr(self.offer, offer_field) != getattr(plan, plan_field):
                raise SingleHostError(f"offer {offer_field} does not match plan {plan_field}")
        deadline = self.hard_deadline
        if deadline.tzinfo is None or deadline.utcoffset() is None or deadline <= self.now():
            raise SingleHostError("VM lease deadline must be future and timezone-aware")
        try:
            launch.validate()
        except VastSdkError as error:
            raise SingleHostError("VM launch contract is invalid") from error
        if launch.template_hash != plan.template_hash or launch.disk_gib != plan.disk_gib:
            raise SingleHostError("VM launch template and disk must match the plan")
