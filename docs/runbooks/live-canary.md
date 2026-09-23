# GPU Canary Lifecycle Runbook

## Status and scope

`scripts/run_canary.py` is an **offline fixture simulator**.  It has no
provider adapter, browser driver, network client, or credential source.  A
normal invocation writes a `BLOCKED` limitation manifest and exits without a
provider create.  It is not a command to rent a GPU.

The separately reviewed Plan 02-06 paid dispatcher must consume a fresh set of
gate evidence and use the lifecycle contract proven by these fixtures.  Until
then, only the following local, in-memory command is available:

```sh
python3 scripts/run_canary.py --fixture --stage gpu-smoke --scenario success \
  --output-root /tmp/srecon26-canary-fixtures --run-id smoke-success
```

Other deterministic fixture scenarios are `sku-fault`,
`unresolved-diagnosis`, `report-timeout`, `create-ambiguity`,
`deadline-teardown`, `kvm-failure`, and `readiness-failure`.  They make no
provider request and report `provenance: offline-fixture`.

## Required evidence before any future paid dispatch

Every current input below must pass in the same run.  Missing, stale, false,
or ambiguous evidence is a `BLOCKED` terminal result before provider create.

| Gate | Required current evidence |
|---|---|
| Phase 1 | `01-VERIFICATION.md` has `status: passed`; summaries and earlier test runs do not substitute. |
| Static safety | Current Semgrep and no-spend preflight pass. |
| Independent guard | A live independent-host attestation binds nonce, label, and immutable deadline. |
| Reporting | The exact-target, non-submitting report fixture is verified. |
| Offer and inventory | Read-only exact offer contract is frozen and inventory is empty or owned by this exact run label. |
| Budget | Decimal reservation is available: no more than `$1.00` for smoke or metric path, within the `$5.00` project ledger. |
| Security | Current secret, exposure, and ownership checks pass, and no `SAFETY_BREACH` is present. |
| Metric path ordering | A finalized smoke result is present before the metric-path stage may begin. |

There are deliberately no fallback credentials, remembered offer IDs, default
SKUs, or retry paths.  A create response that cannot be reconciled to exactly
one nonce-bound label is never retried.

## Lifecycle and deadline

Before the sole permitted future create, the orchestrator journals the Decimal
reservation, exact offer contract, immutable `hard_deadline`, and
`report_start_by`.  The report start cutoff is:

```
hard_deadline - 60s report - 60s exact teardown - 45s three-read absence proof
```

The independent guard remains authoritative at the hard deadline.  A confirmed
contract/GPU/CUDA/image/host mismatch may be reported only before that cutoff;
an unresolved diagnosis is never reported.  Every created instance is torn
down by exact numeric ID plus its nonce-bound label, then the runner records
three separately timestamped absence reads.  A KVM capability failure performs
the same cleanup but must not bootstrap k3s.

## Stage evidence requirements

`gpu-smoke` records the frozen offer/instance contract, GPU and CUDA facts,
KVM decision, lifecycle timestamps, cost reservation, diagnosis/report result,
exact teardown, and absence proof.

`metric-path` is permitted only after finalized smoke in a future paid
dispatcher.  It additionally requires current node allocatable GPU, ready
NVIDIA device plugin, ready vLLM pod holding exactly one GPU, healthy
Prometheus target, fresh CPU/queue/KV metrics, one successful warm-up, one
measured request, resource/custom metrics APIs, HPA state, events, and timing
fields.

## Manifest schema and provenance

Each run emits one `artifacts/runs/<run-id>/run-manifest.json` (or the supplied
output root) containing:

```text
run_id, stage, status, limitation, scenario, provenance, real_gpu_claim,
required_gates, gate_failures, hard_deadline, report_start_by,
offer_contract, contract_and_gpu_facts, lifecycle_timestamps, cost,
report, provider_create_calls, provider_destroy_calls, absence_reads,
journal_state
```

Fixture and blocked manifests always set `provenance` to `offline-fixture` and
`real_gpu_claim` to `false`.  They cannot be promoted into GPU-performance,
CUDA, KVM, or paid-run evidence.  Only a future independently reviewed live
dispatcher may produce `real-gpu` provenance after it has captured the full
live evidence bundle.
