from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
from pathlib import Path

from guard.guard_worker import GuardSafetyError, GuardWorker, VastCliGuardProvider, load_provider_secret


ROOT = Path(__file__).resolve().parents[1]


def _template_attestation() -> dict[str, bool]:
    service = (ROOT / "guard/srecon26-guard.service.template").read_text(encoding="utf-8")
    timer = (ROOT / "guard/srecon26-guard.timer.template").read_text(encoding="utf-8")
    return {
        "service_absolute_worker_path": "ExecStart=/usr/local/libexec/srecon26-guard/guard_worker.py" in service,
        "service_no_new_privileges": "NoNewPrivileges=true" in service,
        "service_bounded_timeout": "TimeoutStartSec=" in service,
        "service_keeps_secret_out_of_environment": "VAST_API_KEY" not in service,
        "timer_persistent": "Persistent=true" in timer,
        "timer_bounded_interval": "OnUnitActiveSec=" in timer,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only preflight for the independent deadline guard")
    parser.add_argument("--require-independent", action="store_true")
    parser.add_argument("--host", default=os.environ.get("SRECON26_GUARD_HOST"))
    parser.add_argument("--root", type=Path, default=Path("/var/lib/srecon26-guard"))
    parser.add_argument("--secret-file", type=Path, default=Path("/etc/srecon26-guard/vast-api-key"))
    parser.add_argument("--vast-bin", default="/usr/local/libexec/srecon26-guard/vastai")
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()

    local_identity = socket.getfqdn()
    result: dict[str, object] = {"local_identity": local_identity, "template_attestation": _template_attestation(), "mutations": []}
    if not all(result["template_attestation"].values()):  # type: ignore[union-attr]
        raise GuardSafetyError("guard unit templates fail static hardening checks")
    if args.require_independent:
        if not args.host:
            raise GuardSafetyError("independent guard host is not configured; refusing paid run")
        if args.host in {"localhost", "127.0.0.1", "::1", local_identity}:
            raise GuardSafetyError("guard host is not independent from the laptop")
        result["configured_guard_host"] = args.host
        result["independent_identity_configured"] = True
    if args.root.exists():
        worker = GuardWorker(args.root, VastCliGuardProvider(args.secret_file, vast_bin=args.vast_bin), require_root_owner=True)
        result["root_private_and_root_owned"] = True
        result["vastai_binary"] = args.vast_bin
        result["guard_script_hash"] = worker.preflight()["script_hash"]
    else:
        result["root_private_and_root_owned"] = False
    if args.secret_file.exists():
        load_provider_secret(args.secret_file, require_root_owner=True)
        result["credential_root_owned_0600"] = True
    else:
        result["credential_root_owned_0600"] = False
    if args.require_independent and not (result["root_private_and_root_owned"] and result["credential_root_owned_0600"]):
        raise GuardSafetyError("independent guard is not deployed with a root-owned state root and 0600 credential")
    result["preflight_hash"] = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GuardSafetyError, OSError) as exc:
        print(f"guard preflight refused: {exc}", file=sys.stderr)
        raise SystemExit(2)
