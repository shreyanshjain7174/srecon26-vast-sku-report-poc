#!/usr/bin/env python3
"""Read current Vast account, inventory, and offer capability facts without provider mutation."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from srecon26_poc.vast_provider import VastCliProvider, VastPreflightError, VastProviderError


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--read-only", action="store_true", required=True)
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--query", default="external=false rentable=true verified=true")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--vast-cli", default="vastai")
    args = parser.parse_args()

    if args.limit < 1 or args.limit > 25:
        parser.error("--limit must be between 1 and 25")

    try:
        snapshot = VastCliProvider(args.vast_cli).read_only_preflight(args.query, limit=args.limit, require_ready=True)
        payload: dict[str, object] = {"eligible": True, **snapshot.to_json()}
        exit_code = 0
    except (VastPreflightError, VastProviderError) as error:
        payload = {"eligible": False, "error": str(error)}
        exit_code = 1

    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
