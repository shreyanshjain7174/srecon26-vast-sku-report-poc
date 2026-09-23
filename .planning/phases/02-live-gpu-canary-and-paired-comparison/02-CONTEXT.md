# Phase 2 Context: Live GPU Canary and Paired Comparison

## Phase Goal

Prove the real GPU/vLLM/Kubernetes observation path on Vast.ai hardware and run three complete paired HPA blocks only when the evidence, topology, ownership, and cost gates make that safe.

## Blocking Preconditions

The current Phase 1 verification at `01-VERIFICATION.md` has status `gaps_found`, not pass. Its concrete guard, queue independence, and integrity-anchor gaps are Phase 1 work and must not be reclassified as Phase 2 evidence. Every paid-mutation plan must first read a newer Phase 1 verification with `status: passed`; otherwise it writes a precise blocked/limitation record and exits before provider creation. Implementing offline Phase 2 code and fixtures remains allowed.

## Decisions

### D-01: Phase 1 pass is a runtime prerequisite for every paid mutation

Before any paid create, require a fresh passing Phase 1 verification, a successful current no-spend gate, and no prior `SAFETY_BREACH`. Existing Phase 1 plans, tests, or summaries do not substitute for this verification status.

### D-02: Treat all Vast account, inventory, pricing, and offer observations as ephemeral

Every run starts with a read-only account/inventory/offer snapshot. Autobilling must be disabled, inventory must be empty or same-run-owned, and the exact offer contract is fetched and frozen immediately before the one allowed create. Prior offer IDs, template IDs, hosts, prices, SKUs, and capacity reports are examples only and are never encoded as runnable defaults.

### D-03: Guard authority is independent, durable, and immutable

The guard runs on a separately reachable always-on Linux host, never the laptop or rented VM. It is armed before create with fresh nonce, unique label, exact instance expectation, and immutable hard deadline; it persists its journal, receives controller heartbeats, and performs break-glass exact teardown only after heartbeat loss or deadline. Provider credentials remain in a root-owned `0600` secret file or platform store outside the repository.

### D-04: Report qualifying provider faults before teardown without extending exposure

Only a `PROVIDER_FAULT_CONFIRMED` with exact instance ID, nonce-bound label, and evidence bundle may invoke the authenticated desktop report driver. The immutable report-start deadline subtracts 60 seconds reporting, 60 seconds exact teardown, and 45 seconds three-read absence margin. `DIAGNOSIS_UNRESOLVED` never reports and always proceeds to exact teardown.

### D-05: Prove the full KVM GPU metric path, not a container-only approximation

The canary runs on a full KVM GPU VM with systemd, cgroup v2, privileged/container runtime support, NVIDIA runtime, device plugin, k3s, and `autoscaling/v2`. vLLM uses the approved image only by current resolvable digest; the run captures GPU identity/CUDA, node allocatable GPU, vLLM request timing, Prometheus, custom-metrics API, HPA state/events, and cost/lifecycle evidence. Public exposure is SSH only, restricted to the controller; all other access is through an SSH tunnel or localhost.

### D-06: Separate free/read-only preparation from paid stages

Provider adapter, guard, report fixture, KVM capability gate, manifests, canary runner, and comparison validator are implemented and tested offline before any provider mutation. Paid GPU smoke, metric-path canary, and paired comparison are separate plans with complete gate checks and a safe blocked result when a prerequisite is absent.

### D-07: Bound canary spend and require absence proof between stages

GPU/CUDA smoke reserves at most $1.00, metric-path canary at most $1.00 after smoke finalization, and each paid stage finalizes its evidence, exact teardown, and three separately timestamped absence reads before another stage. The project-wide Decimal ledger remains capped at $5.00.

### D-08: Permit comparison only with matched controls and three complete paired blocks

Comparison requires a validated real-GPU canary, two compatible ready GPU capacities, remaining Decimal budget of at least $3.00, same model revision/image digest/GPU class/trace/token lengths/schedule/seed/warm-up/window/replica limits/readiness policy, and fixed order A/B, B/A, A/B. Fewer than three complete blocks are `EXPLORATORY` operational evidence and cannot emit a performance conclusion or paired chart claim.

## Deferred Ideas

- Fixing Phase 1 queue evidence and Phase 1 integrity anchors is delegated to Phase 1 gap closure, not Phase 2.
- Multi-region, production KServe/llm-d, model-family benchmarking, and any claim beyond the captured evidence are out of scope.
- Evidence analysis verdicts, chart generation, and the deck remain Phase 3 work.

## Source Coverage Audit

| Source | Item group | Coverage |
|---|---|---|
| GOAL | Real GPU metric path | Plans 02-01 through 02-06 |
| GOAL | Conditional three-block paired comparison | Plans 02-07 and 02-08 |
| REQ | GPU-01 through GPU-07 | Plans 02-01 through 02-06 |
| REQ | PAIR-01 through PAIR-04 | Plans 02-07 and 02-08 |
| CONTEXT | D-01 through D-08 | Every plan cites its applicable decision IDs |
| RESEARCH | Read-only-first provider operation, independent guard, no retry, current KVM probe, `autoscaling/v2`, evidence provenance | Plans 02-01 through 02-08 |

All Phase 2 scope is planned. No prior Vast finding is a fixed offer or authority to create.
