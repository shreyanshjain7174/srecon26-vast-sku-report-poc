#!/usr/bin/env python3
"""A local label-only Vast CLI double for the GitHub guard rehearsal.

Its state and call log live beside the executable name supplied to the real
``VastCliGuardProvider``. It has no network code and never reads credentials.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Mapping


class FakeVastError(ValueError):
    pass


def _paths() -> tuple[Path, Path]:
    directory = Path(sys.argv[0]).absolute().parent
    return directory / "state.json", directory / "calls.ndjson"


def _load(path: Path) -> dict[str, object]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise FakeVastError("fake provider state is invalid") from exc
    if not isinstance(state, dict):
        raise FakeVastError("fake provider state is invalid")
    return state


def _instance(state: Mapping[str, object]) -> dict[str, object] | None:
    item = state.get("instance")
    if item is None:
        return None
    if not isinstance(item, dict) or not isinstance(item.get("id"), int) or not isinstance(item.get("label"), str):
        raise FakeVastError("fake provider only permits an integer ID and label")
    return {"id": item["id"], "label": item["label"]}


def _write(path: Path, state: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _append(path: Path, event: Mapping[str, object]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def main() -> int:
    state_path, calls_path = _paths()
    state = _load(state_path)
    instance = _instance(state)
    args = sys.argv[1:]
    if args == ["show", "instances", "--raw"]:
        _append(calls_path, {"operation": "show_instances"})
        print(json.dumps([instance] if instance else []))
        return 0
    if len(args) == 4 and args[:2] == ["show", "instance"] and args[3] == "--raw":
        instance_id = int(args[2])
        _append(calls_path, {"operation": "show_instance", "instance_id": instance_id})
        print(json.dumps(instance if instance and instance["id"] == instance_id else {}))
        return 0
    if len(args) == 4 and args[:2] == ["destroy", "instance"] and args[3] == "--yes":
        instance_id = int(args[2])
        if instance is None or instance["id"] != instance_id:
            raise FakeVastError("fake destroy requires the exact live instance ID")
        _append(calls_path, {"operation": "destroy", "instance_id": instance_id, "label": instance["label"]})
        state["instance"] = None
        _write(state_path, state)
        print(json.dumps({"destroyed": instance_id}))
        return 0
    raise FakeVastError("unsupported fake vastai command")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FakeVastError, ValueError, OSError) as exc:
        print(f"fake vastai refused request: {exc}", file=sys.stderr)
        raise SystemExit(2)
