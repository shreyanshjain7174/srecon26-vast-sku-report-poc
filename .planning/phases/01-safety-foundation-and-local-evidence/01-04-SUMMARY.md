---
phase: 01-safety-foundation-and-local-evidence
plan: 04
subsystem: local-hpa-evidence
tags: [kubernetes, hpa, prometheus, local-synthetic]
---

# Phase 01 Plan 04: Local HPA evidence summary

CPU, queue, and synthetic-KV local arms validate 1-to-2 scaling independence while rejecting crosstalk and non-synthetic provenance.

## Verification

- Local evaluator and artifact suite: 5 passed.
- `kubectl apply --dry-run=client -f k8s/local/` passed for all manifests.
- No cluster was mutated; a live rehearsal was intentionally not run.

## Deviations from Plan

None - plan executed exactly as written.

## Self-Check: PASSED

- Found evaluator, manifests, scripts, and metric fixtures.
- Found commits `3448a41` and `0db6e32`.
