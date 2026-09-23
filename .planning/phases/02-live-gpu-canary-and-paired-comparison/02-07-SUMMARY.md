# Plan 02-07 summary: offline paired comparator

Implemented a pure offline comparison eligibility validator and block-evidence classifier.

- `validate_comparison_plan` requires a validated `real-gpu` canary reference, two distinct current ready compatible capacities, at least `Decimal("3.00")` headroom, all pinned controls, and fixed `AB,BA,AB` order.
- `analyze_comparison` retains block-level operational evidence while classifying incomplete runs as `EXPLORATORY_ONLY`, control/provenance/trace/order defects as `INVALID`, and only exactly three complete matched blocks as `VALID` for a later analysis consumer.
- `scripts/run_comparison.py` requires an explicit offline fixture and has no provider dispatch path. Its output is always `mode=fixture-replay` and never emits a comparative claim.

Verification:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q tests/unit/test_comparison.py tests/integration/test_comparison_runner.py
semgrep --config .semgrep.yml --error src/srecon26_poc/comparison.py scripts/run_comparison.py tests/unit/test_comparison.py tests/integration/test_comparison_runner.py
```

Both commands passed on 2026-09-23. The fixture is replay-only and is not evidence of live capacity, a validated canary, actual GPU behavior, or a performance conclusion.
