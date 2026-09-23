---
phase: 02-live-gpu-canary-and-paired-comparison
plan: 05
status: complete
---

# Offline Canary Lifecycle Simulation

Implemented `scripts/run_canary.py` as a fixture-only bounded lifecycle runner.
It journalizes fresh named gates, Decimal reservation, exact offer, immutable
deadline, report-start cutoff, fixture lifecycle, exact teardown, and three
absence reads.  Its non-fixture CLI path is an evidence-bearing `BLOCKED`
result: no paid dispatcher, provider adapter, network request, browser action,
or secret access is present in this plan.

The integration suite exercises successful smoke and metric path, confirmed SKU
fault reporting, unresolved diagnosis, report cutoff timeout, create ambiguity,
deadline cleanup, each named gate failure, foreign inventory, and provenance
separation.  Fixture outputs remain `offline-fixture` with
`real_gpu_claim: false`; they cannot become real GPU evidence.

`docs/runbooks/live-canary.md` documents the only available fixture invocation,
the complete fresh gate set needed by the later Plan 02-06 dispatcher, lifecycle
deadline formula, cleanup semantics, stage evidence, and manifest schema.
