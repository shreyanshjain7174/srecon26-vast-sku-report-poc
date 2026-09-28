"""Fail-closed adapter for the official Vast SDK (vastai 1.8.2); paid creates get one HTTP attempt."""
from __future__ import annotations

import importlib.metadata
import json
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Protocol

from .multigpu_contracts import DISK_GIB
from .provider import AmbiguousCreate

SUPPORTED_SDK_VERSION = "1.8.2"
MIN_DISK_GIB = 200
MAX_DISK_GIB = 400
_ENVELOPE_KEYS = ("instances", "offers", "results", "invoices")
_RECORD_KEYS = ("id", "instance_id", "offer_id")
_DIGEST_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DOTTED_CAPABILITY = re.compile(r"^\d+\.\d+$")


class VastSdkError(RuntimeError):
    """The Vast SDK did not produce a usable, safe provider response."""


class VastSdkClient(Protocol):
    def search_offers(self, **kwargs: Any) -> object: ...
    def create_instance(self, **kwargs: Any) -> object: ...
    def show_instances(self, **kwargs: Any) -> object: ...
    def show_instance(self, **kwargs: Any) -> object: ...
    def destroy_instance(self, **kwargs: Any) -> object: ...
    def show_invoices(self, **kwargs: Any) -> object: ...


def _installed_sdk_version() -> str:
    return importlib.metadata.version("vastai")


def create_vast_sdk_client(api_key: str | None = None) -> VastSdkClient:
    """Build the official SDK client with one HTTP attempt and raw JSON output."""
    if api_key is not None and (not isinstance(api_key, str) or not api_key.strip()):
        raise VastSdkError("Vast API key must be a non-empty string when provided")
    try:
        from vastai import VastAI
    except ImportError:
        raise VastSdkError("official vastai SDK is not installed") from None
    try:
        version = _installed_sdk_version()
    except importlib.metadata.PackageNotFoundError:
        raise VastSdkError("official vastai SDK version is unknown") from None
    if version != SUPPORTED_SDK_VERSION:
        raise VastSdkError(f"vastai SDK {SUPPORTED_SDK_VERSION} is required")
    try:
        # vastai 1.8.2 loops over range(retry), so 1 means one attempt and no retry.
        return VastAI(api_key=api_key, retry=1, raw=True, quiet=True)
    except Exception as error:
        raise VastSdkError(f"Vast SDK client construction failed ({type(error).__name__})") from None


def _reject_constant(_: str) -> object:
    raise ValueError("non-finite JSON constant")


def normalize_payload(value: object) -> object:
    """Return a JSON object/array from SDK output; anything else is rejected."""
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            raise VastSdkError("Vast SDK response was not UTF-8") from None
    if isinstance(value, str):
        try:
            value = json.loads(value, parse_float=Decimal, parse_constant=_reject_constant)
        except ValueError:
            raise VastSdkError("Vast SDK response was not JSON") from None
    if isinstance(value, (Mapping, list)):
        return value
    raise VastSdkError("Vast SDK response was not a JSON object or array")


def normalize_records(value: object) -> tuple[Mapping[str, object], ...]:
    payload = normalize_payload(value)
    if isinstance(payload, Mapping):
        envelope = next((key for key in _ENVELOPE_KEYS if key in payload), None)
        if envelope is None:
            if any(key in payload for key in _RECORD_KEYS):
                return (payload,)
            raise VastSdkError("Vast SDK response did not contain records")
        payload = payload[envelope]
        if isinstance(payload, Mapping):
            return (payload,)
    if isinstance(payload, list) and all(isinstance(item, Mapping) for item in payload):
        return tuple(payload)
    raise VastSdkError("Vast SDK response did not contain records")


def _integer(value: object, name: str, *, low: int = 1, high: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str, Decimal, float)):
        raise VastSdkError(f"missing or invalid {name}")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise VastSdkError(f"missing or invalid {name}") from None
    if not number.is_finite() or number != number.to_integral_value() or number < low or (high is not None and number > high):
        raise VastSdkError(f"missing or invalid {name}")
    return int(number)


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, str, Decimal, float)):
        raise VastSdkError(f"missing or invalid {name}")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise VastSdkError(f"missing or invalid {name}") from None
    if not number.is_finite() or number < 0:
        raise VastSdkError(f"missing or invalid {name}")
    return number


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VastSdkError(f"missing or invalid {name}")
    return value.strip()


def _optional_text(value: object, name: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _text(value, name)


def _validate_label(label: object) -> str:
    if not isinstance(label, str) or not _LABEL.fullmatch(label):
        raise VastSdkError("label must be 1-64 characters of [A-Za-z0-9._-] starting alphanumeric")
    return label


def _gpu_ram_mib(record: Mapping[str, object]) -> int:
    ram = _decimal(record.get("gpu_ram_mib", record.get("gpu_ram")), "gpu_ram")
    mib = ram if ram >= 1024 else ram * 1024
    return _integer(mib, "gpu_ram")


def _compute_capability(record: Mapping[str, object]) -> str:
    value = record.get("compute_capability", record.get("compute_cap"))
    if isinstance(value, str) and "." in value:
        if not _DOTTED_CAPABILITY.fullmatch(value.strip()):
            raise VastSdkError("missing or invalid compute capability")
        return value.strip()
    number = _integer(value, "compute capability")
    return f"{number // 100}.{(number % 100) // 10}"


def _ports(value: object) -> tuple[tuple[str, int], ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise VastSdkError("invalid ports")
    ports: list[tuple[str, int]] = []
    for container_port, bindings in value.items():
        if not isinstance(container_port, str) or not container_port:
            raise VastSdkError("invalid ports")
        if bindings is None:
            continue
        if not isinstance(bindings, list) or not all(isinstance(item, Mapping) for item in bindings):
            raise VastSdkError("invalid ports")
        ports.extend((container_port, _integer(item.get("HostPort"), "host port", high=65535)) for item in bindings)
    return tuple(sorted(ports))


@dataclass(frozen=True, slots=True)
class SdkOffer:
    offer_id: int
    machine_id: int
    gpu_name: str
    num_gpus: int
    gpu_ram_mib: int
    compute_capability: str
    dph_total: Decimal
    reliability: Decimal
    direct_port_count: int
    inet_down_mbps: Decimal
    label: str
    inet_down_cost: Decimal = Decimal("0")
    inet_up_cost: Decimal = Decimal("0")

    @classmethod
    def from_record(cls, record: Mapping[str, object], label: str) -> SdkOffer:
        return cls(
            _integer(record.get("id", record.get("offer_id")), "offer id"),
            _integer(record.get("machine_id"), "machine_id"),
            _text(record.get("gpu_name"), "gpu_name"),
            _integer(record.get("num_gpus"), "num_gpus"),
            _gpu_ram_mib(record),
            _compute_capability(record),
            _decimal(record.get("dph_total", record.get("dph")), "dph_total"),
            _decimal(record.get("reliability2"), "reliability2"),
            _integer(record.get("direct_port_count"), "direct_port_count", low=0),
            _decimal(record.get("inet_down"), "inet_down"),
            label,
            _decimal(record.get("inet_down_cost"), "inet_down_cost"),
            _decimal(record.get("inet_up_cost"), "inet_up_cost"),
        )


@dataclass(frozen=True, slots=True)
class SdkInstance:
    instance_id: int
    machine_id: int
    gpu_name: str
    num_gpus: int
    gpu_ram_mib: int
    compute_capability: str
    dph_total: Decimal
    label: str
    actual_status: str | None
    ssh_host: str | None
    ssh_port: int | None
    public_ipaddr: str | None
    ports: tuple[tuple[str, int], ...]

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> SdkInstance:
        ssh_port = record.get("ssh_port")
        return cls(
            _integer(record.get("id", record.get("instance_id")), "instance id"),
            _integer(record.get("machine_id"), "machine_id"),
            _text(record.get("gpu_name"), "gpu_name"),
            _integer(record.get("num_gpus"), "num_gpus"),
            _gpu_ram_mib(record),
            _compute_capability(record),
            _decimal(record.get("dph_total", record.get("dph")), "dph_total"),
            _text(record.get("label"), "instance label"),
            _optional_text(record.get("actual_status"), "actual_status"),
            _optional_text(record.get("ssh_host"), "ssh_host"),
            None if ssh_port is None else _integer(ssh_port, "ssh_port", high=65535),
            _optional_text(record.get("public_ipaddr"), "public_ipaddr"),
            _ports(record.get("ports")),
        )


@dataclass(frozen=True, slots=True)
class SdkLaunchContract:
    """Exact create settings: digest-pinned image, bounded disk, direct SSH, no env/onstart payloads."""

    image: str
    disk_gib: int
    ssh: bool = True
    direct: bool = True
    cancel_unavail: bool = True
    runtype: str = "ssh"
    env: Mapping[str, str] | None = field(default=None, repr=False)
    onstart: str | None = field(default=None, repr=False)

    def validate(self) -> None:
        if not isinstance(self.image, str) or not _DIGEST_IMAGE.fullmatch(self.image):
            raise VastSdkError("launch image must be pinned by an immutable @sha256: digest")
        if type(self.disk_gib) is not int or not MIN_DISK_GIB <= self.disk_gib <= MAX_DISK_GIB:
            raise VastSdkError(f"launch disk must be an integer {MIN_DISK_GIB}-{MAX_DISK_GIB} GiB")
        if self.ssh is not True or self.direct is not True or self.cancel_unavail is not True:
            raise VastSdkError("launch contract requires ssh, direct, and cancel_unavail")
        if self.runtype != "ssh":
            raise VastSdkError("launch runtype must be 'ssh'")
        if self.env or self.onstart:
            raise VastSdkError("launch contract forbids env and onstart payloads because they can carry secrets")

    def create_kwargs(self, *, offer_id: int, label: str) -> dict[str, object]:
        self.validate()
        return {
            "id": offer_id,
            "image": self.image,
            "disk": self.disk_gib,
            "label": label,
            "ssh": True,
            "direct": True,
            "cancel_unavail": True,
        }


class VastSdkProvider:
    """Normalizes official SDK responses; creates are single-shot and destroys are identity-checked."""

    def __init__(
        self,
        client: VastSdkClient,
        *,
        reconcile_attempts: int = 24,
        reconcile_interval_seconds: float = 5.0,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if type(reconcile_attempts) is not int or not 1 <= reconcile_attempts <= 30:
            raise ValueError("reconcile_attempts must be an integer 1-30")
        if isinstance(reconcile_interval_seconds, bool) or not isinstance(reconcile_interval_seconds, (int, float)) or not 0 <= reconcile_interval_seconds <= 15:
            raise ValueError("reconcile_interval_seconds must be 0-15")
        self._client = client
        self._reconcile_attempts = reconcile_attempts
        self._reconcile_interval_seconds = float(reconcile_interval_seconds)
        self._sleeper = sleeper

    def __repr__(self) -> str:
        return f"VastSdkProvider(client=<redacted>, reconcile_attempts={self._reconcile_attempts})"

    def _call(self, method: str, **kwargs: object) -> object:
        try:
            return getattr(self._client, method)(**kwargs)
        except Exception as error:
            # SDK exception text can embed request URLs carrying the API key.
            raise VastSdkError(f"Vast SDK {method} failed ({type(error).__name__})") from None

    def search_offers(self, query: str, *, limit: int = 25, label: str = "") -> tuple[SdkOffer, ...]:
        if not isinstance(query, str) or not query.strip():
            raise VastSdkError("offer query must be a non-empty string")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise VastSdkError("offer limit must be an integer 1-100")
        if label:
            _validate_label(label)
        records = normalize_records(self._call("search_offers", query=query, limit=limit, storage=DISK_GIB))
        return tuple(SdkOffer.from_record(record, label) for record in records)

    def get_offer(self, offer_id: int, machine_id: int, label: str) -> SdkOffer:
        offer_id = _integer(offer_id, "offer id")
        machine_id = _integer(machine_id, "machine_id")
        _validate_label(label)
        matches = [offer for offer in self.search_offers(f"machine_id={machine_id}", label=label) if offer.offer_id == offer_id]
        if len(matches) != 1 or matches[0].machine_id != machine_id:
            raise VastSdkError("current offer is not uniquely available on the expected machine")
        return matches[0]

    def list_instances(self) -> tuple[SdkInstance, ...]:
        return tuple(SdkInstance.from_record(record) for record in normalize_records(self._call("show_instances")))

    def get_instance(self, instance_id: int) -> SdkInstance:
        instance_id = _integer(instance_id, "instance id")
        current = [SdkInstance.from_record(record) for record in normalize_records(self._call("show_instance", id=instance_id))]
        if len(current) != 1 or current[0].instance_id != instance_id:
            raise VastSdkError("current instance is not uniquely available")
        return current[0]

    def list_invoices(self) -> tuple[dict[str, object], ...]:
        return tuple(dict(record) for record in normalize_records(self._call("show_invoices")))

    def create_once(self, offer: SdkOffer, contract: SdkLaunchContract) -> SdkInstance:
        if not isinstance(offer, SdkOffer) or not isinstance(contract, SdkLaunchContract):
            raise VastSdkError("create requires an SdkOffer and SdkLaunchContract")
        label = _validate_label(offer.label)
        kwargs = contract.create_kwargs(offer_id=offer.offer_id, label=label)
        try:
            response = self._client.create_instance(**kwargs)
        except Exception as error:
            raise AmbiguousCreate(f"Vast SDK create_instance outcome unknown ({type(error).__name__}); reconcile by label, never retry") from None
        try:
            instances = [SdkInstance.from_record(record) for record in normalize_records(response)]
        except VastSdkError:
            raise AmbiguousCreate("Vast SDK create_instance returned no verifiable instance; reconcile by label, never retry") from None
        if len(instances) != 1 or instances[0].label != label or instances[0].machine_id != offer.machine_id:
            raise AmbiguousCreate("Vast SDK create_instance response does not match the requested offer; reconcile by label, never retry")
        return instances[0]

    def reconcile_label(self, label: str) -> SdkInstance | None:
        _validate_label(label)
        for attempt in range(self._reconcile_attempts):
            matches = [instance for instance in self.list_instances() if instance.label == label]
            if len(matches) > 1:
                raise VastSdkError("duplicate instances carry the reconcile label")
            if matches:
                return matches[0]
            if attempt + 1 < self._reconcile_attempts:
                self._sleeper(self._reconcile_interval_seconds)
        return None

    def destroy_exact(self, instance_id: int, expected_label: str) -> None:
        """Destroy once after exact ID+label verification; absence must be verified by the caller."""
        instance_id = _integer(instance_id, "instance id")
        _validate_label(expected_label)
        try:
            current = self.get_instance(instance_id)
        except VastSdkError:
            raise VastSdkError("current instance does not exactly match id and label; refusing destroy") from None
        if current.label != expected_label:
            raise VastSdkError("current instance does not exactly match id and label; refusing destroy")
        try:
            response = self._client.destroy_instance(id=instance_id)
        except Exception as error:
            raise VastSdkError(f"Vast SDK destroy_instance failed ({type(error).__name__}); caller must verify absence") from None
        try:
            payload = normalize_payload(response)
        except VastSdkError:
            payload = None
        if not isinstance(payload, Mapping) or payload.get("success") is not True:
            raise VastSdkError("Vast SDK destroy_instance was not confirmed; caller must verify absence")
