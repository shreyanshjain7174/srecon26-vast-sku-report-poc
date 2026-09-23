# GPU Canary Lifecycle Runbook

## Status and scope

`scripts/run_canary.py` defaults to an **offline fixture simulator**.  A normal
invocation writes a `BLOCKED` limitation manifest and exits without a provider
create.  It is not a command to rent a GPU.

Plan 02-06 adds a separately injected live dispatcher.  It has no built-in
credential, SSH command, browser driver, offer, image, template, or remote
workload implementation.  The operator-owned dispatcher factory must supply
those privileged dependencies; this repository never discovers or defaults
them.  Until that factory exists, only the following local, in-memory command
is available:

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
SKUs, images, templates, or retry paths.  A create response that cannot be
reconciled to exactly one nonce-bound label is never retried.

## Live dispatcher contract

The `--live` branch rejects missing inputs before it loads the integration
factory.  It requires `--require-zero-instances`, `--nonce`, `--label`,
`--offer-id`, `--hard-deadline`, `--phase1-verification`,
`--semgrep-artifact`, `--provider-preflight`, `--guard-attestation`,
`--report-fixture`, and `--dispatcher-factory`.  Metric path additionally
requires `--smoke-manifest`.  All artifact paths must exist and be no more than
five minutes old.  The dispatcher hashes every accepted gate file into the run
manifest.

The preflight JSON must say `eligible: true`,
`balance_threshold_enabled: false`, and `instance_count: 0`.  It must contain
one current exact offer whose `vms_enabled` flag is true.  The dispatcher
re-reads that offer immediately before create and blocks if any contract field
changed.  The guard record must be `ARMED` or `ACTIVE` and bind the exact
nonce, label, immutable deadline, non-local host identity, and script hash.
The report fixture must prove a `SUBMITTED` exact-target receipt with zero
provider requests.

The approved VM launch contract is explicit and frozen: Ubuntu 22.04 template
hash `b7942f6bbc4374893ff66eb78145bbac`, with recorded image identity
`docker.io/vastai/kvm:ubuntu_cli_22.04-2025-05-16`.  The CLI selects the
template with `--template_hash`, never a default image, and always uses exactly
`130` GiB disk with `--ssh`, `--direct`, and `--cancel-unavail`. Vast documents
`--direct` as requesting both direct and proxy SSH routes; it does not prove
which route is usable. The resolver uses a direct endpoint only when the exact
instance record supplies `public_ipaddr` plus `ports["22/tcp"][0].HostPort`;
otherwise it records and uses the exact record's proxy pair. It never combines
fields across routes or guesses a port. The exact create argument order is
recorded in unit tests; provider CLI calls use an argument vector, never a
shell string. The earlier one-attempt scope is exhausted. On 2026-09-23 the
user explicitly authorized up to three new, distinct `inference-smoke`
attempts when needed for a credible conference PoC. Each attempt uses budget
category `gpu-inference-smoke`, reserves at most `$1.00`, and still requires a
fresh read-only CLI/schema preflight because the corrected direct route has not
yet been live-validated.

The workload contract is equally frozen and explicit: model
`Qwen/Qwen2.5-1.5B-Instruct`, revision
`989aa7980e4cf806f80c7fef2b1adb7bc71aa306`, and the revalidated amd64 vLLM
amd64 v0.10.2 digest
`sha256:df2607b26bdda2875de4832f4d08da0055b4b6e3570347f3a849bcc652771dd6`.
The remote executor must report all three values back from the running VM;
missing or different values produce a limitation bundle, never real-GPU
provenance.

## Lifecycle and deadline

For an authorized create, the orchestrator must first journal the Decimal
reservation, exact offer contract, immutable `hard_deadline`, and
`report_start_by`. The current inference PoC is authorized for as many as three
distinct guarded attempts; it begins with one machine and expands only when an
actionable failure or a materially useful comparison justifies another. The
report start cutoff is:

```
hard_deadline - 180s report - 180s exact teardown - 60s three-read absence proof
```

The independent guard remains authoritative at the hard deadline.  A confirmed
contract/GPU/CUDA/image/host mismatch may be reported only before that cutoff;
an unresolved diagnosis is never reported.  Every created instance is torn
down by exact numeric ID plus its nonce-bound label, then the runner records
three separately timestamped absence reads.  A KVM capability failure performs
the same cleanup but must not bootstrap k3s.

The injected remote workload receives a heartbeat callback and has no authority
to create or destroy instances.  It must return direct GPU identity, CUDA,
KVM, and evidence files.  Metric path also needs fresh node/device-plugin/vLLM
readiness, Prometheus, resource and custom metrics APIs, HPA state, events,
timing, warm-up, and measured request evidence.  `provenance: real-gpu` and
`real_gpu_claim: true` are emitted only after all of this evidence, exact
teardown, three absence reads, and guard root-hash acknowledgement succeed.
All other terminal paths are `live-limitation` bundles.

## Stage evidence requirements

`gpu-smoke` records the frozen offer/instance contract, GPU and CUDA facts,
KVM decision, lifecycle timestamps, cost reservation, diagnosis/report result,
exact teardown, and absence proof.

`inference-smoke` is the current proof path. It does not bootstrap k3s. On the
created VM it starts the immutable vLLM image on loopback only, loads the frozen
model revision, performs a warm-up and bounded concurrent streamed requests,
and captures raw stream bodies, TTFT transport timing, end-to-end latency,
token counts, derived TPOT/throughput, vLLM queue/KV metrics when exposed, and
GPU utilization/memory samples before, during, and after load. Real-GPU success
also requires a sealed measurement bound to the exact run ID, instance ID,
nonce label, model revision, and image digest. Missing measurements are a safe
failure, never a successful smoke.

`metric-path` remains an unexecuted protocol, not an authorized next action. If
a later paid dispatcher is explicitly authorized, it is permitted only after a
finalized successful smoke. It additionally requires current node allocatable GPU, ready
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
