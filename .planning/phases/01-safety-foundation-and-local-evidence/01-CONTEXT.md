# Phase 1 Context: Safety Foundation and Local Evidence

## Phase Goal

Deliver the safety envelope and independently validated local CPU, queue-depth, and synthetic-KV HPA evidence before any paid provider activity is possible.

## Decisions

### D-01: Enforce the $5.00 project exposure cap with `Decimal`

All budget-path arithmetic, persistence, and JSON conversion use `decimal.Decimal`. The budget ledger is project-wide, survives restart, prevents both cumulative and category-cap overspend, and exposes read-only Decimal headroom. The run categories are GPU/CUDA smoke ($1.00), vLLM canary ($1.00), and paired comparison (up to $3.00). Float values are prohibited on the budget path.

### D-02: Make the run journal append-only, hash-chained, and crash-resumable

The controller state machine is the source of truth. Events are canonical JSON NDJSON with a monotone sequence, previous hash, event hash, ISO-8601 wall time, and monotonic time. Writes are durable. Resume may discard only a malformed partial final tail; it must validate every retained event and never issue a second create after a recorded create intent or ambiguous result.

### D-03: Arm an independent teardown guard before every paid create

The guard runs on a separately reachable always-on host outside both the controller laptop and the rented VM. Arming records a fresh 128-bit nonce, unique label, immutable hard deadline, expected ownership, host identity, and script hash. The guard acts only after heartbeat loss or the fixed deadline, never extends that deadline, and validates exact instance ID plus nonce-bound label before any teardown.

### D-04: Permit exactly one create attempt and reconcile ambiguity by label

The controller journals intent before every external side effect. It issues at most one provider create request for a run. An ambiguous create response is reconciled using the unique pre-created label and halts unresolved; it is never retried. Existing instances at run start are rejected unless owned by the same run journal.

### D-05: Report only a confirmed provider SKU fault, then destroy within the deadline

`PROVIDER_FAULT_CONFIRMED` freezes an evidence bundle, verifies exact instance ownership, performs the desktop report flow, records its receipt or timeout, then authorizes teardown. `DIAGNOSIS_UNRESOLVED` captures diagnostics and tears down without provider reporting. The immutable report-start deadline subtracts the 60-second report window, 60-second teardown margin, and 45-second three-read absence margin from the hard deadline; reporting never extends cost exposure.

### D-06: Keep provider credentials outside code, artifacts, arguments, environment, and logs

The controller reads the provider credential only from a root-owned `0600` secret file or an equivalent platform secret store. It never copies credentials into the repository, run artifacts, process arguments, environment exports, or logs. Provider-side least privilege is preferred, with the same ownership checks retained in wrappers.

### D-07: Prove each local HPA signal independently

CPU, queue, and synthetic-KV arms use `autoscaling/v2`, 15-second Prometheus scrape and HPA reconciliation targets, a 90-second negative control, and at most four reconciliation intervals. The target signal exceeds its threshold by at least 20%; both non-target signals remain at least 20% below their thresholds throughout. Each successful arm records a 1-to-2 desired and ready replica transition, samples, events, timeline, maxima, and `provenance=local-synthetic`.

### D-08: Anchor integrity before committing evidence

Every run bundle includes timestamped raw artifacts, `SHA256SUMS`, and `ROOT-HASH.txt`; all timestamps must fit the declared run window and event order must be monotonic. The guard journal acknowledges the checksum/root-hash anchor before the evidence is committed with `git commit -s` on `feat/vast-sku-report-poc`. Missing files, checksums, provenance, valid timestamps, monotonic ordering, or anchor agreement are rejection conditions.

### D-09: Make the no-spend gate automatic and fail closed

Every commit on `feat/vast-sku-report-poc` runs secret checks and Semgrep through repository-controlled hooks. Before any paid create, a preflight gate must have passing tests, no unresolved high-severity Semgrep findings, a zero-instance or same-run-owned inventory snapshot, sufficient Decimal budget, a live independent guard attestation, and a report-adapter fixture receipt. The gate is read-only and never creates or destroys provider resources.

## Codex's Discretion

- Select standard-library file-locking and atomic-write mechanics that work on the supported Python runtime, provided the ledger and journal invariants above are preserved.
- Name internal classes, fixtures, and artifact JSON schemas clearly while retaining the interfaces identified in the approved implementation plan.
- Use deterministic fakes and fixtures for all provider, guard, desktop-report, and local-cluster behavior in Phase 1. No credential, provider, or live-cluster access is needed to implement or test the safety path.

## Deferred Ideas

- Guard-host provisioning and any paid Vast.ai action are Phase 2 prerequisites, not Phase 1 execution activities.
- GPU canary, k3s GPU topology, vLLM inference, and paired A/B comparison are Phase 2.
- Full analyzer verdicts, charts, and the presentation deck are Phase 3.

## Source Coverage Audit

| Source | Item group | Coverage |
|---|---|---|
| GOAL | Safety envelope before paid activity; three local independent HPA arms | Plans 01-01 through 01-05 |
| REQ | BUDG-01..05, CTRL-01..03, ERR-01..03 | Plans 01-01 and 01-02 |
| REQ | GUARD-01..05, CTRL-04..06, ERR-04 | Plan 01-03 |
| REQ | HPA-01..06 | Plan 01-04 |
| REQ | INT-01..05, SEC-01..04 | Plan 01-05 |
| CONTEXT | D-01 through D-09 | Plans 01-01 through 01-05, each action cites its applicable decision IDs |
| RESEARCH | No float arithmetic, no ambiguous-create retry, independent guard, crosstalk checks, `autoscaling/v2`, provenance separation | Plans 01-01 through 01-05 |

All Phase 1 source items are planned. Deferred ideas are excluded from Phase 1 implementation.
