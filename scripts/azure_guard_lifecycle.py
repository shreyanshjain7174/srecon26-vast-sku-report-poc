#!/usr/bin/env python3
"""Read-only Azure lifecycle preflight for the independent guard host."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence
from uuid import UUID


_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.()-]{0,89}$")
_LOCATION = re.compile(r"^[a-z0-9]+$")
_PURPOSE = "independent-vast-guard"


class AzureLifecycleError(RuntimeError):
    pass


Runner = Callable[[Sequence[str], int], str]


def _run(arguments: Sequence[str], timeout: int) -> str:
    try:
        return subprocess.run(list(arguments), check=True, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise AzureLifecycleError("Azure read-only preflight command failed") from error


def _uuid(value: str, field: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as error:
        raise AzureLifecycleError(f"{field} must be a UUID") from error


@dataclass(frozen=True, slots=True)
class AzureGuardLifecycleConfig:
    subscription_id: str
    resource_group: str
    identity_name: str
    controller_vm: str
    location: str
    timeout_seconds: int = 30

    def validate(self) -> None:
        _uuid(self.subscription_id, "subscription ID")
        for value, field in ((self.resource_group, "resource group"), (self.identity_name, "identity"), (self.controller_vm, "controller VM")):
            if not _NAME.fullmatch(value):
                raise AzureLifecycleError(f"Azure {field} name is invalid")
        if not _LOCATION.fullmatch(self.location):
            raise AzureLifecycleError("Azure location is invalid")
        if not 1 <= self.timeout_seconds <= 60:
            raise AzureLifecycleError("Azure CLI timeout must be 1-60 seconds")

    def commands(self) -> tuple[tuple[str, ...], ...]:
        self.validate()
        common = ("--subscription", self.subscription_id, "--only-show-errors", "--output", "json")
        return (
            ("az", "group", "show", "--name", self.resource_group, *common),
            ("az", "identity", "show", "--resource-group", self.resource_group, "--name", self.identity_name, *common),
            ("az", "vm", "show", "--resource-group", self.resource_group, "--name", self.controller_vm, *common),
            ("az", "resource", "list", "--resource-group", self.resource_group, *common),
        )


def _object(raw: str, context: str) -> Mapping[str, object]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise AzureLifecycleError(f"{context} did not return JSON") from error
    if not isinstance(value, dict):
        raise AzureLifecycleError(f"{context} returned an unexpected response")
    return value


def _tags(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise AzureLifecycleError(f"{context} lacks dedicated-purpose tags")
    if value.get("srecon26-purpose") != _PURPOSE or value.get("srecon26-dedicated") != "true":
        raise AzureLifecycleError(f"{context} is not dedicated to the independent guard")
    return value


def _resource_id(subscription: str, resource_group: str, provider_path: str) -> str:
    return f"/subscriptions/{subscription}/resourceGroups/{resource_group}/providers/{provider_path}"


def preflight(config: AzureGuardLifecycleConfig, *, runner: Runner = _run) -> dict[str, object]:
    commands = config.commands()
    group = _object(runner(commands[0], config.timeout_seconds), "Azure resource group")
    identity = _object(runner(commands[1], config.timeout_seconds), "Azure controller identity")
    vm = _object(runner(commands[2], config.timeout_seconds), "Azure controller VM")
    try:
        resources = json.loads(runner(commands[3], config.timeout_seconds))
    except json.JSONDecodeError as error:
        raise AzureLifecycleError("Azure resource inventory did not return JSON") from error
    if not isinstance(resources, list) or not resources:
        raise AzureLifecycleError("dedicated Azure resource group has no resources")

    if group.get("name") != config.resource_group or group.get("location") != config.location:
        raise AzureLifecycleError("Azure resource group identity or location differs from the approved plan")
    _tags(group.get("tags"), "Azure resource group")

    identity_id = _resource_id(
        config.subscription_id,
        config.resource_group,
        f"Microsoft.ManagedIdentity/userAssignedIdentities/{config.identity_name}",
    )
    if (
        str(identity.get("id", "")).lower() != identity_id.lower()
        or identity.get("location") != config.location
        or identity.get("type") != "Microsoft.ManagedIdentity/userAssignedIdentities"
    ):
        raise AzureLifecycleError("Azure controller identity differs from the approved plan")
    principal_id = _uuid(str(identity.get("principalId", "")), "controller principal ID")
    client_id = _uuid(str(identity.get("clientId", "")), "controller client ID")
    _tags(identity.get("tags"), "Azure controller identity")

    vm_id = _resource_id(config.subscription_id, config.resource_group, f"Microsoft.Compute/virtualMachines/{config.controller_vm}")
    if str(vm.get("id", "")).lower() != vm_id.lower() or vm.get("location") != config.location:
        raise AzureLifecycleError("Azure controller VM differs from the approved plan")
    _tags(vm.get("tags"), "Azure controller VM")
    vm_identity = vm.get("identity")
    if not isinstance(vm_identity, dict) or vm_identity.get("type") != "UserAssigned":
        raise AzureLifecycleError("Azure controller VM lacks the approved user-assigned identity")
    assigned = vm_identity.get("userAssignedIdentities")
    if not isinstance(assigned, dict) or {str(key).lower() for key in assigned} != {identity_id.lower()}:
        raise AzureLifecycleError("Azure controller VM has a different user-assigned identity")

    resource_ids: list[str] = []
    for resource in resources:
        if not isinstance(resource, dict) or not isinstance(resource.get("id"), str):
            raise AzureLifecycleError("Azure resource inventory contains an invalid record")
        resource_id = str(resource["id"])
        prefix = f"/subscriptions/{config.subscription_id}/resourceGroups/{config.resource_group}/providers/"
        if not resource_id.lower().startswith(prefix.lower()):
            raise AzureLifecycleError("Azure resource inventory crossed the dedicated group boundary")
        _tags(resource.get("tags"), f"Azure resource {resource.get('name', '')}")
        resource_ids.append(resource_id)
    if identity_id.lower() not in {value.lower() for value in resource_ids} or vm_id.lower() not in {value.lower() for value in resource_ids}:
        raise AzureLifecycleError("Azure dedicated resource inventory omits the controller or its identity")

    return {
        "status": "AZURE_GUARD_PREFLIGHT_READY",
        "subscription_id": str(UUID(config.subscription_id)),
        "resource_group": config.resource_group,
        "location": config.location,
        "controller_vm_id": vm_id,
        "controller_identity_id": identity_id,
        "controller_principal_id": principal_id,
        "controller_client_id": client_id,
        "dedicated_resource_count": len(resource_ids),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate an existing dedicated Azure guard controller without deploying or deleting it")
    parser.add_argument("preflight", nargs="?")
    parser.add_argument("--subscription-id", required=True)
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--identity-name", required=True)
    parser.add_argument("--controller-vm", required=True)
    parser.add_argument("--location", required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.preflight not in (None, "preflight"):
        raise AzureLifecycleError("only the read-only preflight lifecycle action is supported")
    result = preflight(
        AzureGuardLifecycleConfig(
            subscription_id=args.subscription_id,
            resource_group=args.resource_group,
            identity_name=args.identity_name,
            controller_vm=args.controller_vm,
            location=args.location,
        )
    )
    serialized = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(serialized, encoding="utf-8")
    else:
        sys.stdout.write(serialized)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AzureLifecycleError as error:
        print(f"Azure guard preflight refused: {error}", file=sys.stderr)
        raise SystemExit(2)
