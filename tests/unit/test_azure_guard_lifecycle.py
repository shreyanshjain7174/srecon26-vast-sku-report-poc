from __future__ import annotations

import json

import pytest

from scripts.azure_guard_lifecycle import AzureGuardLifecycleConfig, AzureLifecycleError, preflight


SUBSCRIPTION = "11111111-1111-4111-8111-111111111111"
PRINCIPAL = "22222222-2222-4222-8222-222222222222"
CLIENT = "33333333-3333-4333-8333-333333333333"
GROUP = "srecon26-independent-guard-rg"
IDENTITY_NAME = "srecon26-guard-id"
VM_NAME = "srecon26-guard-vm"
LOCATION = "centralindia"
TAGS = {"srecon26-purpose": "independent-vast-guard", "srecon26-dedicated": "true"}
IDENTITY_ID = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{IDENTITY_NAME}"
VM_ID = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}/providers/Microsoft.Compute/virtualMachines/{VM_NAME}"


def _config() -> AzureGuardLifecycleConfig:
    return AzureGuardLifecycleConfig(SUBSCRIPTION, GROUP, IDENTITY_NAME, VM_NAME, LOCATION)


def _responses() -> dict[tuple[str, str], object]:
    return {
        ("group", "show"): {"name": GROUP, "location": LOCATION, "tags": TAGS},
        ("identity", "show"): {
            "id": IDENTITY_ID,
            "type": "Microsoft.ManagedIdentity/userAssignedIdentities",
            "location": LOCATION,
            "principalId": PRINCIPAL,
            "clientId": CLIENT,
            "tags": TAGS,
        },
        ("vm", "show"): {
            "id": VM_ID,
            "location": LOCATION,
            "tags": TAGS,
            "identity": {"type": "UserAssigned", "userAssignedIdentities": {IDENTITY_ID: {}}},
        },
        ("resource", "list"): [
            {"id": IDENTITY_ID, "name": IDENTITY_NAME, "tags": TAGS},
            {"id": VM_ID, "name": VM_NAME, "tags": TAGS},
        ],
    }


def test_preflight_uses_read_only_secret_free_azure_arguments_and_validates_identity_binding() -> None:
    responses = _responses()
    calls: list[list[str]] = []

    def runner(arguments, timeout: int) -> str:
        args = list(arguments)
        calls.append(args)
        return json.dumps(responses[(args[1], args[2])])

    result = preflight(_config(), runner=runner)

    assert result["status"] == "AZURE_GUARD_PREFLIGHT_READY"
    assert result["controller_identity_id"] == IDENTITY_ID
    assert result["controller_principal_id"] == PRINCIPAL
    assert result["dedicated_resource_count"] == 2
    flattened = " ".join(item for call in calls for item in call)
    assert [call[1:3] for call in calls] == [["group", "show"], ["identity", "show"], ["vm", "show"], ["resource", "list"]]
    assert all("--subscription" in call and "--only-show-errors" in call and call[-2:] == ["--output", "json"] for call in calls)
    assert "VAST" not in flattened.upper()
    assert "credential" not in flattened.lower()
    assert "secret" not in flattened.lower()
    assert "--parameters" not in flattened
    assert "deployment" not in flattened


def test_preflight_rejects_an_unowned_resource_in_the_claimed_dedicated_group() -> None:
    responses = _responses()
    resources = list(responses[("resource", "list")])
    resources.append(
        {
            "id": f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}/providers/Microsoft.Storage/storageAccounts/unrelated",
            "name": "unrelated",
            "tags": {"owner": "other"},
        }
    )
    responses[("resource", "list")] = resources

    def runner(arguments, timeout: int) -> str:
        args = list(arguments)
        return json.dumps(responses[(args[1], args[2])])

    with pytest.raises(AzureLifecycleError, match="not dedicated"):
        preflight(_config(), runner=runner)


def test_preflight_rejects_a_controller_without_the_exact_identity() -> None:
    responses = _responses()
    vm = dict(responses[("vm", "show")])
    vm["identity"] = {"type": "UserAssigned", "userAssignedIdentities": {"/subscriptions/other": {}}}
    responses[("vm", "show")] = vm

    def runner(arguments, timeout: int) -> str:
        args = list(arguments)
        return json.dumps(responses[(args[1], args[2])])

    with pytest.raises(AzureLifecycleError, match="different user-assigned identity"):
        preflight(_config(), runner=runner)


@pytest.mark.parametrize(
    "identity",
    [
        {"type": "SystemAssigned", "userAssignedIdentities": {IDENTITY_ID: {}}},
        {
            "type": "UserAssigned",
            "userAssignedIdentities": {
                IDENTITY_ID: {},
                "/subscriptions/other/resourceGroups/other/providers/Microsoft.ManagedIdentity/userAssignedIdentities/extra": {},
            },
        },
    ],
)
def test_preflight_requires_exact_user_assigned_type_and_singleton_identity(identity: dict[str, object]) -> None:
    responses = _responses()
    vm = dict(responses[("vm", "show")])
    vm["identity"] = identity
    responses[("vm", "show")] = vm

    def runner(arguments, timeout: int) -> str:
        args = list(arguments)
        return json.dumps(responses[(args[1], args[2])])

    with pytest.raises(AzureLifecycleError):
        preflight(_config(), runner=runner)


def test_preflight_rejects_a_different_identity_resource_type() -> None:
    responses = _responses()
    identity = dict(responses[("identity", "show")])
    identity["type"] = "Microsoft.Compute/virtualMachines"
    responses[("identity", "show")] = identity

    def runner(arguments, timeout: int) -> str:
        args = list(arguments)
        return json.dumps(responses[(args[1], args[2])])

    with pytest.raises(AzureLifecycleError, match="identity differs"):
        preflight(_config(), runner=runner)
