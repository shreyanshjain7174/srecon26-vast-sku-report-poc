# Paired comparison evidence contract

`scripts/run_comparison.py` is an offline fixture replayer. It has no provider command, credential input, or paid-dispatch option. Its output validates records; it does not acquire capacity, start a workload, create a chart, or make a performance claim.

Run an offline replay with:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python scripts/run_comparison.py \
  --fixture tests/fixtures/comparison/complete-real-gpu.json \
  --json artifacts/comparison-fixture-replay.json
```

The checked-in fixture is deliberately incomplete: it proves the parser and classifier only. It is not real-GPU evidence, despite exercising the required `real-gpu` label. Fixture replay must remain labelled `mode=fixture-replay` and `comparative_claim_emitted=false`.

## Eligibility gate

Do not dispatch a paired run unless all of these are fresh current facts:

- a validated canary reference with `provenance=real-gpu`;
- two distinct, compatible, current, ready GPU capacities;
- at least `Decimal("3.00")` remaining budget; and
- a pinned model revision, image digest, GPU class, trace hash, prompt and token schedules, seed, warm-up, measurement window, replica limit, readiness policy, and block order `AB, BA, AB`.

Capacity observations are not reservations. Historical offers, previous capacity snapshots, local CPU rehearsal, and fixture data never satisfy this gate.

## Per-run evidence

For each A and B half-block, preserve raw queue depth, TTFT, TPOT, end-to-end latency, GPU/KV/CPU utilization, desired and ready replicas, readiness delay, failures, trace identity, provenance, and the complete immutable controls. The trace hash and controls must match the plan on every run.

Exactly three complete blocks in `AB, BA, AB` order may be handed to the Phase 3 analysis consumer as `VALID`. Fewer than three complete blocks are `EXPLORATORY_ONLY`: preserve the raw operational evidence, reject comparative conclusions, and do not create a paired chart. Missing or mixed provenance, trace/control drift, bad ordering, or more than three blocks is `INVALID`.
