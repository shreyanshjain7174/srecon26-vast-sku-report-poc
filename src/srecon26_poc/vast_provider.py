"""Bounded Vast CLI adapter; read-only calls are the default integration surface."""
from __future__ import annotations

import json
import hashlib
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .contracts import InstanceContract, OfferContract
from .provider import AccountSnapshot, AmbiguousCreate


class VastProviderError(RuntimeError):
    """The Vast CLI did not produce a usable, safe provider response."""


class VastPreflightError(VastProviderError):
    """A read-only snapshot shows that paid work is not eligible."""


Runner = Callable[[list[str], int], str]

OFFICIAL_UBUNTU_2204_TEMPLATE_HASH = "b7942f6bbc4374893ff66eb78145bbac"
OFFICIAL_KVM_IMAGE = "docker.io/vastai/kvm:ubuntu_cli_22.04-2025-05-16"
OFFICIAL_UBUNTU_DESKTOP_TEMPLATE_HASH = "10d921fdff3c0d2a794897d81ae870c5"
OFFICIAL_UBUNTU_DESKTOP_IMAGE = "docker.io/vastai/kvm:ubuntu_desktop_22.04-2025-11-21"
APPROVED_VM_TEMPLATES = {
    OFFICIAL_UBUNTU_2204_TEMPLATE_HASH: OFFICIAL_KVM_IMAGE,
    OFFICIAL_UBUNTU_DESKTOP_TEMPLATE_HASH: OFFICIAL_UBUNTU_DESKTOP_IMAGE,
}
OFFICIAL_UBUNTU_DESKTOP_TEMPLATE_EVIDENCE = Path(__file__).resolve().parents[2] / "evidence" / "vast-template-ubuntu-desktop-vm-20260923.json"
OFFICIAL_UBUNTU_DESKTOP_TEMPLATE_EVIDENCE_SHA256 = "771fd7af957052ea991a29c95f8f61d59124a23d7fa8f4a444c17dd4985b48ca"


@dataclass(frozen=True, slots=True)
class VastPreflight:
    balance_threshold_enabled: bool | None
    instance_count: int
    offer_count: int
    offers: tuple[Mapping[str, object], ...]

    def to_json(self) -> dict[str, object]:
        return {
            "balance_threshold_enabled": self.balance_threshold_enabled,
            "instance_count": self.instance_count,
            "offer_count": self.offer_count,
            "offers": [dict(item) for item in self.offers],
        }


@dataclass(frozen=True, slots=True)
class VastInvoiceEvidence:
    amount: Decimal
    artifact: Path
    sha256: str


@dataclass(frozen=True, slots=True)
class VastAbsenceEvidence:
    artifact: Path
    sha256: str


@dataclass(frozen=True, slots=True)
class VastLaunchContract:
    """Explicit KVM launch settings required for the one paid create.

    Vast otherwise selects a default image.  Defaults are unsafe here because
    the canary needs a known full VM topology, not a provider-chosen Docker
    image.  The current contract is the official Ubuntu 22.04 KVM template and
    its recorded KVM image identity.  The image is evidence metadata; the
    template hash is the only create selector, so the CLI never falls back to
    an implicit image. Approved hash/image pairs prevent mixing metadata from
    one template with another template's create selector.
    """

    ubuntu_template_hash: str | None = None
    image_contract: str | None = None
    disk_gib: int = 130
    ssh: bool = True
    cancel_unavail: bool = True
    request_direct_ssh: bool = True

    def validate(self) -> None:
        if (
            not isinstance(self.ubuntu_template_hash, str)
            or self.ubuntu_template_hash not in APPROVED_VM_TEMPLATES
            or APPROVED_VM_TEMPLATES[self.ubuntu_template_hash] != self.image_contract
        ):
            raise VastProviderError("launch contract must pin an approved exact Vast VM template and image pair")
        if self.ubuntu_template_hash == OFFICIAL_UBUNTU_DESKTOP_TEMPLATE_HASH:
            try:
                raw = OFFICIAL_UBUNTU_DESKTOP_TEMPLATE_EVIDENCE.read_bytes()
                snapshot = json.loads(raw)
                template = snapshot.get("template") if isinstance(snapshot, Mapping) else None
            except (OSError, json.JSONDecodeError):
                snapshot = {}
                template = None
                raw = b""
            if (
                hashlib.sha256(raw).hexdigest() != OFFICIAL_UBUNTU_DESKTOP_TEMPLATE_EVIDENCE_SHA256
                or snapshot.get("schema") != "srecon26-vast-template-snapshot/v1"
                or snapshot.get("source") != "VastCliProvider.search_templates/v1"
                or not isinstance(template, Mapping)
                or template.get("id") != 720360
                or template.get("hash_id") != self.ubuntu_template_hash
                or f"{template.get('image')}:{template.get('tag')}" != self.image_contract
                or template.get("recommended") is not True
                or template.get("ssh_direct") is not True
                or template.get("use_ssh") is not True
                or template.get("vm") is not True
            ):
                raise VastProviderError("alternate VM template lacks exact pinned provider snapshot evidence")
        if self.disk_gib != 130:
            raise VastProviderError("launch disk must be exactly 130 GiB")
        if not (self.ssh and self.request_direct_ssh and self.cancel_unavail):
            raise VastProviderError("launch contract requires SSH, a direct-route request, and cancel-unavail")

    def create_args(self, *, label: str) -> list[str]:
        self.validate()
        assert self.ubuntu_template_hash is not None
        return ["--template_hash", self.ubuntu_template_hash, "--disk", str(self.disk_gib), "--ssh", "--direct", "--cancel-unavail", "--label", label]

    def to_json(self) -> dict[str, object]:
        self.validate()
        return {
            "ubuntu_template_hash": self.ubuntu_template_hash,
            "image_contract": self.image_contract,
            "disk_gib": self.disk_gib,
            "ssh": self.ssh,
            "cancel_unavail": self.cancel_unavail,
            "direct_ssh_requested": self.request_direct_ssh,
        }


class VastCliProvider:
    """Normalizes Vast CLI JSON and never retries a paid create request."""

    def __init__(self, cli_path: Path | str = "vastai", *, timeout_seconds: int = 20, runner: Runner | None = None, reconcile_attempts: int = 1, reconcile_interval_seconds: float = 5.0, sleeper: Callable[[float], None] = time.sleep) -> None:
        if not 1 <= reconcile_attempts <= 30 or not 0 <= reconcile_interval_seconds <= 15:
            raise ValueError("reconcile retry bounds are invalid")
        self._cli_path = str(cli_path)
        self._timeout_seconds = timeout_seconds
        self._runner = runner or self._subprocess_runner
        self._reconcile_attempts = reconcile_attempts
        self._reconcile_interval_seconds = reconcile_interval_seconds
        self._sleeper = sleeper

    @staticmethod
    def _subprocess_runner(args: list[str], timeout: int) -> str:
        completed = subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout)
        return completed.stdout

    def _run_json(self, args: list[str]) -> object:
        command = [self._cli_path, "--raw", "--no-color", *args]
        try:
            stdout = self._runner(command, self._timeout_seconds)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
            raise VastProviderError(f"Vast CLI {args[:2]!r} failed") from error
        try:
            return json.loads(stdout, parse_float=Decimal)
        except json.JSONDecodeError as error:
            raise VastProviderError(f"Vast CLI {args[:2]!r} did not return JSON") from error

    def _run_mutation(self, args: list[str]) -> None:
        command = [self._cli_path, "--raw", "--no-color", *args]
        try:
            self._runner(command, self._timeout_seconds)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
            raise VastProviderError(f"Vast CLI {args[:2]!r} failed") from error

    @staticmethod
    def _records(value: object) -> list[Mapping[str, object]]:
        if isinstance(value, list) and all(isinstance(item, Mapping) for item in value):
            return list(value)
        if isinstance(value, Mapping):
            for key in ("instances", "offers", "results"):
                items = value.get(key)
                if isinstance(items, list) and all(isinstance(item, Mapping) for item in items):
                    return list(items)
            # Some Vast CLI versions return the result of a create as one
            # object rather than a one-element list.  It is still an untrusted
            # record; callers retain their exact label/identity checks.
            if "id" in value or "instance_id" in value or "offer_id" in value:
                return [value]
        raise VastProviderError("Vast CLI response did not contain records")

    @staticmethod
    def _decimal(value: object, field: str) -> Decimal:
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError) as error:
            raise VastProviderError(f"missing or invalid {field}") from error

    @staticmethod
    def _integer(value: object, field: str) -> int:
        try:
            if isinstance(value, bool):
                raise ValueError
            number = Decimal(str(value))
            if not number.is_finite() or number != number.to_integral_value():
                raise ValueError
            return int(number)
        except (InvalidOperation, TypeError, ValueError) as error:
            raise VastProviderError(f"missing or invalid {field}") from error

    @classmethod
    def _gpu_ram_mib(cls, record: Mapping[str, object]) -> int:
        value = record.get("gpu_ram_mib", record.get("gpu_ram"))
        ram = cls._decimal(value, "gpu_ram")
        return int(ram if ram >= Decimal("1024") else ram * Decimal("1024"))

    @classmethod
    def _compute_capability(cls, record: Mapping[str, object]) -> str:
        value = record.get("compute_capability", record.get("compute_cap"))
        if value is None:
            raise VastProviderError("missing or invalid compute capability")
        text = str(value)
        if "." in text:
            return text
        number = cls._integer(value, "compute_cap")
        return format(Decimal(number) / Decimal("100"), "f").rstrip("0").rstrip(".")

    @classmethod
    def _offer(cls, record: Mapping[str, object], label: str) -> OfferContract:
        return OfferContract(
            cls._integer(record.get("id", record.get("offer_id")), "offer id"),
            str(record.get("gpu_name")),
            cls._integer(record.get("num_gpus"), "num_gpus"),
            cls._gpu_ram_mib(record),
            cls._compute_capability(record),
            cls._integer(record.get("machine_id"), "machine_id"),
            cls._decimal(record.get("dph_total", record.get("dph")), "dph_total"),
            label,
        )

    @classmethod
    def _instance(cls, record: Mapping[str, object]) -> InstanceContract:
        label = record.get("label")
        if not isinstance(label, str) or not label:
            raise VastProviderError("instance record is missing a label")
        return InstanceContract(
            cls._integer(record.get("id", record.get("instance_id")), "instance id"),
            str(record.get("gpu_name")),
            cls._integer(record.get("num_gpus"), "num_gpus"),
            cls._gpu_ram_mib(record),
            cls._compute_capability(record),
            cls._integer(record.get("machine_id"), "machine_id"),
            cls._decimal(record.get("dph_total", record.get("dph")), "dph_total"),
            label,
        )

    def account_snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(instance_count=len(self.list_instances()))

    def list_instances(self) -> tuple[InstanceContract, ...]:
        return tuple(self._instance(record) for record in self._records(self._run_json(["show", "instances"])))

    def get_offer(self, offer_id: int, *, label: str) -> OfferContract:
        records = self._records(self._run_json(["search", "offers", f"id={offer_id}", "--limit", "25"]))
        matches = [record for record in records if self._integer(record.get("id", record.get("offer_id")), "offer id") == offer_id]
        if len(matches) != 1:
            raise VastProviderError("current offer contract is not uniquely available")
        return self._offer(matches[0], label)

    def get_vms_enabled_offer(self, offer_id: int, *, machine_id: int, label: str) -> OfferContract:
        """Read and freeze one current KVM-capable offer before a paid create."""

        # Vast's current offer search documents an ``id`` field, but its live
        # endpoint can return an empty result for an otherwise visible offer-ID
        # filter.  Machine ID is stable and supported; the exact offer ID is
        # still selected and required uniquely from that machine's records.
        records = self._records(self._run_json(["search", "offers", f"machine_id=={machine_id}", "--storage", "130", "--limit", "25"]))
        matches = [record for record in records if self._integer(record.get("id", record.get("offer_id")), "offer id") == offer_id]
        if len(matches) != 1:
            raise VastProviderError("current KVM offer contract is not uniquely available")
        if self._integer(matches[0].get("machine_id"), "machine_id") != machine_id:
            raise VastProviderError("current offer changed machine identity")
        if matches[0].get("vms_enabled") is not True:
            raise VastProviderError("current offer is not vms_enabled for required KVM topology")
        return self._offer(matches[0], label)

    def get_instance(self, instance_id: int) -> InstanceContract:
        matches = [item for item in self.list_instances() if item.instance_id == instance_id]
        if len(matches) != 1:
            raise VastProviderError("current instance contract is not uniquely available")
        return matches[0]

    def reconcile_label(self, label: str) -> InstanceContract:
        for attempt in range(self._reconcile_attempts):
            matches = [item for item in self.list_instances() if item.label == label]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise AmbiguousCreate("ambiguous create reconciliation found multiple label matches")
            if attempt + 1 < self._reconcile_attempts:
                self._sleeper(self._reconcile_interval_seconds)
        raise AmbiguousCreate("ambiguous create reconciliation found zero label matches")

    def create_once(self, contract: OfferContract, request_key: str, launch: VastLaunchContract) -> InstanceContract:
        del request_key
        launch.validate()
        try:
            response = self._run_json(["create", "instance", str(contract.offer_id), *launch.create_args(label=contract.label)])
        except VastProviderError as error:
            raise AmbiguousCreate("create outcome is ambiguous and must be reconciled by label") from error
        # Vast CLI 1.7/1.8 commonly acknowledges a successful create as
        # {"success": true, "new_contract": <id>} without returning the label
        # or frozen hardware contract.  Treat that as acknowledged-but-not-yet-
        # observable and reconcile only through the unique nonce-bound label;
        # never fabricate an InstanceContract from the numeric acknowledgement.
        if isinstance(response, Mapping) and response.get("success") is True and response.get("new_contract") is not None:
            raise AmbiguousCreate("create acknowledged; reconcile the nonce-bound label")
        records = self._records(response)
        if len(records) != 1:
            raise AmbiguousCreate("create outcome is ambiguous and must be reconciled by label")
        instance = self._instance(records[0])
        if instance.label != contract.label:
            raise AmbiguousCreate("create response label does not match the unique run label")
        return instance

    def destroy_exact(self, instance_id: int, expected_label: str) -> None:
        instance = self.get_instance(instance_id)
        if instance.label != expected_label:
            raise VastProviderError("refusing to destroy an instance with a mismatched label")
        self._run_mutation(["destroy", "instance", str(instance_id), "--yes"])

    def capture_invoice_charge(self, *, run_id: str, instance_id: int, label: str, start_date: str, end_date: str, artifact: Path) -> VastInvoiceEvidence:
        """Capture one authoritative Vast charge bound to the exact run target."""

        if not run_id or instance_id <= 0 or not label:
            raise VastProviderError("invoice capture requires an exact run, instance, and label")
        args = ["show", "invoices-v1", "--charges", "--charge-type", "instance", "--start-date", start_date, "--end-date", end_date, "--limit", "100", "--latest-first", "--format", "tree", "--verbose"]
        records = self._records(self._run_json(args))
        expected_source = f"instance-{instance_id}"
        matches = [
            record
            for record in records
            if record.get("type") == "instance"
            and record.get("source") == expected_source
            and isinstance(record.get("metadata"), Mapping)
            and record["metadata"].get("label") == label
        ]
        if len(matches) != 1:
            raise VastProviderError("authoritative invoice did not contain one exact instance-and-label charge")
        amount = self._decimal(matches[0].get("amount"), "invoice amount")
        if not amount.is_finite() or amount < 0:
            raise VastProviderError("invoice amount is invalid")
        payload = {
            "schema": "srecon26-vast-invoice-evidence/v1",
            "source": "VastCliProvider.capture_invoice_charge/v1",
            "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "run_id": run_id,
            "instance_id": instance_id,
            "label": label,
            "amount_usd": str(amount),
            "command": args,
            "provider_charge": matches[0],
        }
        encoded = json.dumps(payload, indent=2, sort_keys=True, default=str).encode() + b"\n"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        temporary = artifact.with_suffix(artifact.suffix + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, artifact)
        return VastInvoiceEvidence(amount, artifact, hashlib.sha256(encoded).hexdigest())

    def capture_absence_evidence(self, *, run_id: str, instance_id: int, label: str, artifact: Path, interval_seconds: float = 5.0) -> VastAbsenceEvidence:
        """Persist three fresh provider reads proving the exact target absent."""

        if not run_id or instance_id <= 0 or not label or not 0 <= interval_seconds <= 15:
            raise VastProviderError("absence capture requires an exact target and bounded interval")
        reads: list[dict[str, object]] = []
        for index in range(3):
            matches = [item for item in self.list_instances() if item.instance_id == instance_id or item.label == label]
            reads.append({"observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"), "matching_instances": len(matches)})
            if matches:
                raise VastProviderError("exact target remains present during absence reconciliation")
            if index < 2 and interval_seconds:
                self._sleeper(interval_seconds)
        payload = {
            "schema": "srecon26-vast-absence-evidence/v1",
            "source": "VastCliProvider.capture_absence_evidence/v1",
            "run_id": run_id,
            "instance_id": instance_id,
            "label": label,
            "reads": reads,
        }
        encoded = json.dumps(payload, indent=2, sort_keys=True, default=str).encode() + b"\n"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        temporary = artifact.with_suffix(artifact.suffix + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, artifact)
        return VastAbsenceEvidence(artifact, hashlib.sha256(encoded).hexdigest())

    @staticmethod
    def _auto_recharge_enabled(account: Mapping[str, object]) -> bool | None:
        """Normalize only Vast's current balance-threshold auto-recharge control.

        Older CLI/user payload fields are neither authoritative nor sufficient
        to permit a paid run.  A boolean disagreement is unsafe because it
        makes the account's recharge behavior ambiguous.
        """
        canonical = account.get("balance_threshold_enabled")
        if not isinstance(canonical, bool):
            return None
        for legacy_name in ("autobill_enabled", "autobill", "auto_billing"):
            legacy = account.get(legacy_name)
            if isinstance(legacy, bool) and legacy != canonical:
                raise VastPreflightError("legacy autobill field disagrees with balance-threshold setting")
        return canonical

    def read_only_preflight(self, search_query: str, *, limit: int, require_ready: bool = False, storage_gib: int = 5) -> VastPreflight:
        if not 1 <= limit <= 25:
            raise VastPreflightError("offer inspection limit must be between 1 and 25")
        if not 5 <= storage_gib <= 1000:
            raise VastPreflightError("offer storage pricing must be between 5 and 1000 GiB")
        account = self._run_json(["show", "user"])
        if not isinstance(account, Mapping):
            raise VastPreflightError("account snapshot is not an object")
        balance_threshold_enabled = self._auto_recharge_enabled(account)
        instances = self.list_instances()
        offers = self._records(self._run_json(["search", "offers", search_query, "--storage", str(storage_gib), "--limit", str(limit)]))
        summaries = tuple(
            {
                "offer_id": self._integer(record.get("id", record.get("offer_id")), "offer id"),
                "gpu_name": str(record.get("gpu_name")),
                "num_gpus": self._integer(record.get("num_gpus"), "num_gpus"),
                "gpu_ram_mib": self._gpu_ram_mib(record),
                "compute_capability": self._compute_capability(record),
                "machine_id": self._integer(record.get("machine_id"), "machine_id"),
                "dph_total": str(self._decimal(record.get("dph_total", record.get("dph")), "dph_total")),
                "rentable": record.get("rentable"),
                "verified": record.get("verified"),
                "vms_enabled": record.get("vms_enabled"),
            }
            for record in offers
        )
        result = VastPreflight(balance_threshold_enabled, len(instances), len(summaries), summaries)
        if require_ready and result.balance_threshold_enabled is not False:
            raise VastPreflightError("auto-recharge must be explicitly disabled")
        if require_ready and result.instance_count != 0:
            raise VastPreflightError("provider inventory must be empty before a new run")
        return result
