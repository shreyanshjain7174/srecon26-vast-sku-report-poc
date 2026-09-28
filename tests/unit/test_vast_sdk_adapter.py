from __future__ import annotations

import importlib.util
import json
import sys
import types
from decimal import Decimal

import pytest

from srecon26_poc import vast_sdk_adapter as adapter
from srecon26_poc.provider import AmbiguousCreate
from srecon26_poc.vast_sdk_adapter import (
    SdkInstance,
    SdkLaunchContract,
    SdkOffer,
    VastSdkError,
    VastSdkProvider,
    create_vast_sdk_client,
    normalize_payload,
    normalize_records,
)

SECRET = "sk-vast-SUPERSECRET-0123456789"
IMAGE = "docker.io/vllm/vllm-openai@sha256:" + "a" * 64
LABEL = "srecon26-mg-abc123"

OFFER = {
    "id": 101, "machine_id": 55, "gpu_name": "RTX 4090", "num_gpus": 2,
    "gpu_ram": 24564, "compute_cap": 890, "dph_total": 0.812,
    "reliability2": 0.995, "direct_port_count": 256, "inet_down": 1200.5,
    "inet_down_cost": 0, "inet_up_cost": 0,
}
INSTANCE = {
    "id": 9001,
    "machine_id": 55,
    "gpu_name": "RTX 4090",
    "num_gpus": 2,
    "gpu_ram": 24564,
    "compute_cap": 890,
    "dph_total": "0.812",
    "label": LABEL,
    "actual_status": "running",
    "ssh_host": "ssh5.vast.ai",
    "ssh_port": 12345,
    "public_ipaddr": "203.0.113.7 ",
    "ports": {"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "40800"}], "22/tcp": [{"HostPort": "40022"}]},
}


class FakeClient:
    api_key = SECRET

    def __init__(self, **handlers: object) -> None:
        self.handlers = handlers
        self.calls: list[tuple[str, dict[str, object]]] = []

    def _dispatch(self, name: str, kwargs: dict[str, object]) -> object:
        self.calls.append((name, kwargs))
        handler = self.handlers[name]
        if isinstance(handler, BaseException):
            raise handler
        if callable(handler):
            return handler()
        return handler

    def search_offers(self, **kwargs: object) -> object:
        return self._dispatch("search_offers", kwargs)

    def create_instance(self, **kwargs: object) -> object:
        return self._dispatch("create_instance", kwargs)

    def show_instances(self, **kwargs: object) -> object:
        return self._dispatch("show_instances", kwargs)

    def show_instance(self, **kwargs: object) -> object:
        return self._dispatch("show_instance", kwargs)

    def destroy_instance(self, **kwargs: object) -> object:
        return self._dispatch("destroy_instance", kwargs)

    def show_invoices(self, **kwargs: object) -> object:
        return self._dispatch("show_invoices", kwargs)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def offer(label: str = LABEL) -> SdkOffer:
    return SdkOffer(
        101, 55, "RTX 4090", 2, 24564, "8.9", Decimal("0.812"),
        Decimal("0.995"), 256, Decimal("1200.5"), label,
    )


def contract(**overrides: object) -> SdkLaunchContract:
    return SdkLaunchContract(**{"image": IMAGE, "disk_gib": 250, **overrides})


def assert_no_secret(error: BaseException) -> None:
    assert SECRET not in str(error)
    assert SECRET not in repr(error)
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


# --- import and factory -----------------------------------------------------


def test_module_imports_without_vast_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "vastai", None)
    name = "srecon26_poc._isolated_vast_sdk_adapter"
    spec = importlib.util.spec_from_file_location(name, adapter.__file__)
    assert spec is not None and spec.loader is not None
    isolated = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, isolated)
    spec.loader.exec_module(isolated)
    assert isolated.VastSdkProvider is not None


def test_factory_constructs_official_client_with_fail_closed_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class VastAI:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setitem(sys.modules, "vastai", types.SimpleNamespace(VastAI=VastAI))
    monkeypatch.setattr(adapter, "_installed_sdk_version", lambda: "1.8.2")
    client = create_vast_sdk_client(SECRET)
    assert isinstance(client, VastAI)
    assert captured == {"api_key": SECRET, "retry": 1, "raw": True, "quiet": True}
    create_vast_sdk_client()
    assert captured["api_key"] is None


def test_factory_fails_closed_when_sdk_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "vastai", None)
    with pytest.raises(VastSdkError, match="not installed") as caught:
        create_vast_sdk_client(SECRET)
    assert_no_secret(caught.value)


def test_factory_rejects_unsupported_sdk_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "vastai", types.SimpleNamespace(VastAI=lambda **_: object()))
    monkeypatch.setattr(adapter, "_installed_sdk_version", lambda: "1.9.0")
    with pytest.raises(VastSdkError, match="1.8.2"):
        create_vast_sdk_client(SECRET)


def test_factory_construction_failure_hides_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**kwargs: object) -> object:
        raise RuntimeError(f"bad key {kwargs['api_key']}")

    monkeypatch.setitem(sys.modules, "vastai", types.SimpleNamespace(VastAI=boom))
    monkeypatch.setattr(adapter, "_installed_sdk_version", lambda: "1.8.2")
    with pytest.raises(VastSdkError) as caught:
        create_vast_sdk_client(SECRET)
    assert_no_secret(caught.value)


@pytest.mark.parametrize("bad", ["", "   ", 123, True])
def test_factory_rejects_invalid_api_key(bad: object) -> None:
    with pytest.raises(VastSdkError):
        create_vast_sdk_client(bad)  # type: ignore[arg-type]


# --- payload normalization --------------------------------------------------


def test_normalize_accepts_dict_list_json_str_and_bytes() -> None:
    assert normalize_payload({"a": 1}) == {"a": 1}
    assert normalize_payload([{"a": 1}]) == [{"a": 1}]
    assert normalize_payload('{"dph": 0.5}') == {"dph": Decimal("0.5")}
    assert normalize_payload(b'[{"id": 1}]') == [{"id": 1}]


@pytest.mark.parametrize("bad", [True, False, None, "not json", b"\xff\xfe", "true", '"text"', "null", 42, '{"dph": NaN}', 1.5])
def test_normalize_rejects_malformed_payloads(bad: object) -> None:
    with pytest.raises(VastSdkError):
        normalize_payload(bad)


@pytest.mark.parametrize(
    "payload",
    [
        {"offers": [OFFER]},
        {"instances": [INSTANCE]},
        {"results": [OFFER]},
        {"instances": INSTANCE},
        json.dumps([OFFER]),
        INSTANCE,
    ],
)
def test_normalize_records_unwraps_known_envelopes(payload: object) -> None:
    records = normalize_records(payload)
    assert len(records) == 1
    assert records[0]["machine_id"] == 55


@pytest.mark.parametrize("bad", [{"success": True, "new_contract": 5}, [OFFER, 3], {"offers": "x"}, {"instances": None}, 7])
def test_normalize_records_rejects_non_record_shapes(bad: object) -> None:
    with pytest.raises(VastSdkError):
        normalize_records(bad)


def test_normalize_records_allows_empty_list() -> None:
    assert normalize_records({"instances": []}) == ()


# --- offers -----------------------------------------------------------------


def test_search_offers_parses_offer_contracts() -> None:
    client = FakeClient(search_offers={"offers": [OFFER]})
    offers = VastSdkProvider(client).search_offers("gpu_name=RTX_4090 num_gpus=2", limit=10)
    assert offers == (SdkOffer(
        101, 55, "RTX 4090", 2, 24564, "8.9", Decimal("0.812"),
        Decimal("0.995"), 256, Decimal("1200.5"), "",
    ),)
    assert client.calls == [("search_offers", {"query": "gpu_name=RTX_4090 num_gpus=2", "limit": 10, "storage": 250})]


@pytest.mark.parametrize(
    "overrides",
    [{"num_gpus": True}, {"dph_total": True}, {"dph_total": "NaN"}, {"dph_total": -1}, {"gpu_name": ""}, {"machine_id": None}, {"compute_cap": None}, {"id": 1.5}, {"inet_down_cost": None}, {"inet_up_cost": "NaN"}],
)
def test_search_offers_rejects_malformed_fields(overrides: dict[str, object]) -> None:
    client = FakeClient(search_offers=[{**OFFER, **overrides}])
    with pytest.raises(VastSdkError):
        VastSdkProvider(client).search_offers("id=101")


@pytest.mark.parametrize("limit", [0, 101, True])
def test_search_offers_bounds_limit(limit: object) -> None:
    with pytest.raises(VastSdkError):
        VastSdkProvider(FakeClient(search_offers=[])).search_offers("id=1", limit=limit)  # type: ignore[arg-type]


def test_get_offer_returns_exact_current_match() -> None:
    other = {**OFFER, "id": 102, "machine_id": 77}
    client = FakeClient(search_offers=[other, OFFER])
    assert VastSdkProvider(client).get_offer(101, 55, LABEL) == offer()
    assert client.calls == [("search_offers", {"query": "machine_id=55", "limit": 25, "storage": 250})]


@pytest.mark.parametrize("records", [[], [OFFER, OFFER], [{**OFFER, "machine_id": 56}]])
def test_get_offer_rejects_missing_duplicate_or_moved_offer(records: list[object]) -> None:
    with pytest.raises(VastSdkError):
        VastSdkProvider(FakeClient(search_offers=records)).get_offer(101, 55, LABEL)


@pytest.mark.parametrize("label", ["", "has space", "x" * 65, "-lead"])
def test_get_offer_rejects_invalid_label(label: str) -> None:
    client = FakeClient(search_offers=[OFFER])
    with pytest.raises(VastSdkError):
        VastSdkProvider(client).get_offer(101, 55, label)
    assert client.calls == []


# --- instances --------------------------------------------------------------


def test_list_instances_parses_network_fields() -> None:
    client = FakeClient(show_instances={"instances": [INSTANCE]})
    (instance,) = VastSdkProvider(client).list_instances()
    assert instance == SdkInstance(
        9001, 55, "RTX 4090", 2, 24564, "8.9", Decimal("0.812"), LABEL,
        "running", "ssh5.vast.ai", 12345, "203.0.113.7", (("22/tcp", 40022), ("8000/tcp", 40800)),
    )


def test_get_instance_returns_exact_current_record() -> None:
    client = FakeClient(show_instance={"instances": INSTANCE})

    instance = VastSdkProvider(client).get_instance(9001)

    assert instance.instance_id == 9001
    assert client.calls == [("show_instance", {"id": 9001})]


@pytest.mark.parametrize("payload", [[], [{**INSTANCE, "id": 9002}], [INSTANCE, INSTANCE]])
def test_get_instance_rejects_missing_moved_or_duplicate_record(payload: object) -> None:
    with pytest.raises(VastSdkError, match="not uniquely available"):
        VastSdkProvider(FakeClient(show_instance=payload)).get_instance(9001)


def test_list_instances_tolerates_pending_network_fields() -> None:
    pending = {**INSTANCE, "actual_status": None, "ssh_host": None, "ssh_port": None, "public_ipaddr": None, "ports": None}
    (instance,) = VastSdkProvider(FakeClient(show_instances=[pending])).list_instances()
    assert (instance.actual_status, instance.ssh_host, instance.ssh_port, instance.public_ipaddr, instance.ports) == (None, None, None, None, ())


@pytest.mark.parametrize(
    "overrides",
    [{"label": None}, {"label": ""}, {"ssh_port": 70000}, {"ssh_port": True}, {"ports": ["22/tcp"]}, {"ports": {"22/tcp": [{"HostPort": "x"}]}}, {"ports": {"22/tcp": "40022"}}],
)
def test_list_instances_rejects_malformed_records(overrides: dict[str, object]) -> None:
    with pytest.raises(VastSdkError):
        VastSdkProvider(FakeClient(show_instances=[{**INSTANCE, **overrides}])).list_instances()


# --- launch contract --------------------------------------------------------


@pytest.mark.parametrize("disk", [200, 400])
def test_launch_contract_accepts_bounded_disk(disk: int) -> None:
    contract(disk_gib=disk).validate()


@pytest.mark.parametrize(
    "overrides",
    [
        {"image": "docker.io/vllm/vllm-openai:latest"},
        {"image": "docker.io/vllm/vllm-openai@sha256:abc"},
        {"image": "vllm@sha256:" + "A" * 64},
        {"disk_gib": 199},
        {"disk_gib": 401},
        {"disk_gib": True},
        {"disk_gib": 250.0},
        {"ssh": False},
        {"direct": False},
        {"cancel_unavail": False},
        {"ssh": 1},
        {"runtype": "jupyter"},
        {"env": {"HF_TOKEN": SECRET}},
        {"onstart": f"export TOKEN={SECRET}"},
    ],
)
def test_launch_contract_rejects_unsafe_settings(overrides: dict[str, object]) -> None:
    with pytest.raises(VastSdkError) as caught:
        contract(**overrides).validate()
    assert SECRET not in str(caught.value)


def test_launch_contract_repr_hides_env_and_onstart() -> None:
    text = repr(contract(env={"HF_TOKEN": SECRET}, onstart=SECRET))
    assert SECRET not in text


# --- create -----------------------------------------------------------------


def test_create_once_calls_sdk_exactly_once_with_exact_contract() -> None:
    client = FakeClient(create_instance=INSTANCE)
    instance = VastSdkProvider(client).create_once(offer(), contract())
    assert instance.instance_id == 9001
    assert client.calls == [
        (
            "create_instance",
            {"id": 101, "image": IMAGE, "disk": 250, "label": LABEL, "ssh": True, "direct": True, "cancel_unavail": True},
        )
    ]


@pytest.mark.parametrize("response", [{"success": True, "new_contract": 9001}, "", b"not json", True, None, {"instances": []}])
def test_create_once_ack_only_or_malformed_is_ambiguous(response: object) -> None:
    client = FakeClient(create_instance=response)
    with pytest.raises(AmbiguousCreate):
        VastSdkProvider(client).create_once(offer(), contract())
    assert client.names() == ["create_instance"]


@pytest.mark.parametrize("overrides", [{"label": "someone-else"}, {"machine_id": 56}])
def test_create_once_mismatched_record_is_ambiguous(overrides: dict[str, object]) -> None:
    client = FakeClient(create_instance={**INSTANCE, **overrides})
    with pytest.raises(AmbiguousCreate):
        VastSdkProvider(client).create_once(offer(), contract())


def test_create_once_exception_is_ambiguous_never_retried_and_redacted() -> None:
    client = FakeClient(create_instance=TimeoutError(f"https://console.vast.ai/api/v0/asks/101/?api_key={SECRET}"))
    with pytest.raises(AmbiguousCreate) as caught:
        VastSdkProvider(client).create_once(offer(), contract())
    assert client.names() == ["create_instance"]
    assert_no_secret(caught.value)


def test_create_once_invalid_contract_never_calls_sdk() -> None:
    client = FakeClient(create_instance=INSTANCE)
    with pytest.raises(VastSdkError) as caught:
        VastSdkProvider(client).create_once(offer(), contract(disk_gib=130))
    assert not isinstance(caught.value, AmbiguousCreate)
    with pytest.raises(VastSdkError):
        VastSdkProvider(client).create_once(offer(label=""), contract())
    assert client.calls == []


# --- reconcile --------------------------------------------------------------


def test_reconcile_label_polls_bounded_list_calls() -> None:
    responses = iter([[], [{**INSTANCE, "label": "other", "id": 1}], [INSTANCE]])
    sleeps: list[float] = []
    client = FakeClient(show_instances=lambda: next(responses))
    provider = VastSdkProvider(client, reconcile_attempts=3, reconcile_interval_seconds=2.0, sleeper=sleeps.append)
    assert provider.reconcile_label(LABEL).instance_id == 9001  # type: ignore[union-attr]
    assert client.names() == ["show_instances"] * 3
    assert sleeps == [2.0, 2.0]


def test_default_reconcile_waits_for_delayed_create_visibility() -> None:
    responses = iter([[], [INSTANCE]])
    sleeps: list[float] = []
    client = FakeClient(create_instance={"success": True, "new_contract": 9001}, show_instances=lambda: next(responses))
    provider = VastSdkProvider(client, sleeper=sleeps.append)

    with pytest.raises(AmbiguousCreate):
        provider.create_once(offer(), contract())
    assert provider.reconcile_label(LABEL).instance_id == 9001  # type: ignore[union-attr]
    assert client.names() == ["create_instance", "show_instances", "show_instances"]
    assert sleeps == [5.0]


def test_reconcile_label_returns_none_after_bound() -> None:
    sleeps: list[float] = []
    client = FakeClient(show_instances=[])
    provider = VastSdkProvider(client, reconcile_attempts=2, reconcile_interval_seconds=1.0, sleeper=sleeps.append)
    assert provider.reconcile_label(LABEL) is None
    assert client.names() == ["show_instances"] * 2
    assert sleeps == [1.0]


def test_reconcile_label_rejects_duplicate_labels() -> None:
    client = FakeClient(show_instances=[INSTANCE, {**INSTANCE, "id": 9002}])
    with pytest.raises(VastSdkError, match="duplicate"):
        VastSdkProvider(client).reconcile_label(LABEL)


@pytest.mark.parametrize("kwargs", [{"reconcile_attempts": 0}, {"reconcile_attempts": 31}, {"reconcile_attempts": True}, {"reconcile_interval_seconds": 16}, {"reconcile_interval_seconds": -1}])
def test_provider_rejects_unbounded_reconcile(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        VastSdkProvider(FakeClient(), **kwargs)  # type: ignore[arg-type]


# --- destroy ----------------------------------------------------------------


def test_destroy_exact_verifies_then_destroys_once() -> None:
    client = FakeClient(show_instance={"instances": INSTANCE}, destroy_instance={"success": True})
    VastSdkProvider(client).destroy_exact(9001, LABEL)
    assert client.calls == [("show_instance", {"id": 9001}), ("destroy_instance", {"id": 9001})]


@pytest.mark.parametrize("current", [{**INSTANCE, "label": "other"}, {**INSTANCE, "id": 9002}, [INSTANCE, {**INSTANCE, "id": 9002}], {"instances": []}])
def test_destroy_exact_refuses_mismatched_current_instance(current: object) -> None:
    client = FakeClient(show_instance=current, destroy_instance={"success": True})
    with pytest.raises(VastSdkError):
        VastSdkProvider(client).destroy_exact(9001, LABEL)
    assert "destroy_instance" not in client.names()


def test_destroy_exact_exception_is_not_retried_and_redacted() -> None:
    client = FakeClient(show_instance=[INSTANCE], destroy_instance=ConnectionError(f"api_key={SECRET}"))
    with pytest.raises(VastSdkError) as caught:
        VastSdkProvider(client).destroy_exact(9001, LABEL)
    assert client.names() == ["show_instance", "destroy_instance"]
    assert_no_secret(caught.value)


@pytest.mark.parametrize("response", [{"success": False}, {"msg": "ok"}, "not json", True])
def test_destroy_exact_unconfirmed_response_fails_closed(response: object) -> None:
    client = FakeClient(show_instance=[INSTANCE], destroy_instance=response)
    with pytest.raises(VastSdkError, match="absence"):
        VastSdkProvider(client).destroy_exact(9001, LABEL)
    assert client.names() == ["show_instance", "destroy_instance"]


# --- invoices and secrecy ---------------------------------------------------


def test_list_invoices_normalizes_envelope() -> None:
    client = FakeClient(show_invoices={"invoices": [{"amount": "0.05"}]})
    assert VastSdkProvider(client).list_invoices() == ({"amount": "0.05"},)


def test_provider_repr_and_read_errors_never_expose_api_key() -> None:
    client = FakeClient(show_instances=PermissionError(f"401 for api_key={SECRET}"))
    provider = VastSdkProvider(client)
    assert SECRET not in repr(provider)
    with pytest.raises(VastSdkError) as caught:
        provider.list_instances()
    assert_no_secret(caught.value)
