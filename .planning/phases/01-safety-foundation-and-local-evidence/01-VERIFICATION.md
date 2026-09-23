---
phase: 01-safety-foundation-and-local-evidence
verified: 2026-09-23T04:32:43Z
status: gaps_found
score: 3/5 roadmap success criteria verified
overrides_applied: 0
gaps:
  - truth: "A separately reachable guard is armed before paid create and autonomously tears down on heartbeat loss without controller involvement."
    status: failed
    reason: "The code defines only a Guard protocol and attestation validator. There is no concrete independently running guard, heartbeat-loss loop, or autonomous teardown implementation; tests use an in-process fake."
    artifacts:
      - path: src/srecon26_poc/guard.py
        issue: "Protocol and validation only; no guard service or teardown executor."
      - path: src/srecon26_poc/controller.py
        issue: "create_or_reconcile calls preflight but neither arms the guard nor records heartbeats."
    missing:
      - "Implement and exercise a separately reachable guard with immutable deadline and heartbeat-loss exact teardown before allowing a paid create."
  - truth: "Every run produces SHA256SUMS and ROOT-HASH integrity evidence, anchored in a signed commit and copied to the guard journal."
    status: failed
    reason: "Neither selected live rehearsal directory contains SHA256SUMS, ROOT-HASH.txt, or an external guard-journal acknowledgement. local-evidence/ is gitignored, and commit 51dc2f3 records only planning metadata, so no signed commit anchors the raw selected-run artifacts."
    artifacts:
      - path: local-evidence/phase1-live-202609d03531790135589z
        issue: "No SHA256SUMS, ROOT-HASH.txt, or guard anchor receipt."
      - path: local-evidence/phase1-live-202609d04031790136227z
        issue: "No SHA256SUMS, ROOT-HASH.txt, or guard anchor receipt."
      - path: .gitignore
        issue: "local-evidence/ is excluded from the claimed evidence commit."
    missing:
      - "Finalize both selected successful runs with explicit artifact allowlists, SHA256SUMS, ROOT-HASH.txt, and an external guard-journal receipt, then anchor the evidence as specified using a signed-off commit."
---

# Phase 1: Safety Foundation and Local Evidence — Verification Report

**Phase Goal:** Safety envelope is complete and verified before any paid run; all three local HPA arms independently drive 1-to-2 replica transitions with independence proofs.

**Verified:** 2026-09-23T04:32:43Z

**Status:** gaps_found

**Re-verification:** No — the prior report had no structured gaps and its PASS narrative was independently rechecked.

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
| --- | --- | --- | --- |
| 1 | Budget reservations over $5.00 are rejected and persisted open reservations retain Decimal headroom after restart. | ✓ VERIFIED | `tests/unit/test_budget.py` passes in the 32-test suite; `ExposureLedger` parses JSON with `parse_float=Decimal`, persists reservations, and rejects the `$3.01` excess after `$2.00`. |
| 2 | A separately reachable guard is armed before paid create and autonomously tears down on heartbeat loss without the controller. | ✗ FAILED | `guard.py` contains only a `Protocol`, dataclass, and attestation validation; no process/service executes teardown. `controller.py` preflights an already-armed fake but does not arm it or record heartbeats. |
| 3 | Crash recovery never reissues an already-recorded create call. | ✓ VERIFIED | `tests/unit/test_controller.py::test_create_intent_is_not_reissued_after_reopen` passes; recovery lists by label after `CREATE_REQUESTED` rather than calling `create_once` again. |
| 4 | CPU, queue, and synthetic-KV each prove an independent 1→2 transition with non-target maxima below 20%-below margins. | ✓ VERIFIED FOR LOCAL ARMS | The fresh owned queue run `d04271790137679z` retained seven negative CPU captures of 2.062901–16.456944m (all ≤48m), then reached desired/ready=2. The validator now checks every negative sample; the earlier crosstalk and partial-metrics runs remain retained failures and are not selected. |
| 5 | Every selected run has a SHA256SUMS + ROOT-HASH integrity bundle, guard-journal copy, and signed external anchor. | ✗ FAILED | Neither selected raw run has `SHA256SUMS`, `ROOT-HASH.txt`, or a guard receipt. `local-evidence/` is gitignored; the signed `51dc2f3` commit contains only metadata. |

**Score:** 3/5 roadmap success criteria verified.

### Required Artifacts

| Artifact | Expected | Status | Details |
| --- | --- | --- | --- |
| `src/srecon26_poc/budget.py` | Decimal, persistent $5 cap ledger | ✓ VERIFIED | Unit tests pass and implementation uses `Decimal` / `parse_float=Decimal`. |
| `src/srecon26_poc/journal.py` + `controller.py` | Hash-chain replay and single create after crash | ✓ VERIFIED | Journal and controller unit/integration tests pass; replay path reconciles by label. |
| `src/srecon26_poc/guard.py` | Independently executing guard and exact teardown on controller loss | ✗ STUB FOR RUNTIME CLAIM | Interface/validator only; no concrete external guard implementation exists. |
| `local-evidence/.../cpu` | Owned CPU arm with 90-second control and 1→2 | ✓ VERIFIED FOR LOCAL ARM | Seven desired=1 samples span 98 seconds; scaled HPA desired=2 and deployment ready=2. Queue=0.0 and KV=0.2 in source snapshots. |
| `local-evidence/phase1-live-202609d04271790137679z/queue` | Owned queue arm with isolated non-target signals | ✓ VERIFIED FOR LOCAL ARM | Seven negative samples retain CPU values ≤16.456944m (required ≤48m), queue=0, KV=0.2; the trigger/scaled captures record desired/ready=2. |
| `local-evidence/.../kv` (rerun `d040...`) | Corrected owned KV arm with 90-second control and 1→2 | ✓ VERIFIED FOR LOCAL ARM | Seven desired=1 samples span 96 seconds; KV reaches 1.0 (>=0.96 required trigger), CPU max is 18.621092m and queue max 0.0; scaled desired/ready are 2. |
| Selected live evidence bundles | Checksums, root hash, guard anchor, signed external anchor | ✗ MISSING | Required integrity artifacts and receipt are absent from both rehearsal directories. |

### Key Link Verification

| From | To | Via | Status | Details |
| --- | --- | --- | --- | --- |
| `controller.py` | guard | preflight before `create_once` | PARTIAL | Preflight is present, but the controller neither arms nor heartbeats a concrete guard. |
| `run_live_local_rehearsal.sh` | Kind HPA/custom-metrics APIs | owned per-arm rehearsal | VERIFIED FOR LOCAL ARMS | CPU, fresh queue, and corrected KV each reach ready replicas=2 with source, custom/resource metrics, HPA, deployment, event, and timestamp captures. |
| `scripts/anchor_local_evidence.py` | guard receipt / signed evidence commit | integrity validation | NOT WIRED | Script exists but no selected run contains generated checksums, root, receipt, or anchor record. |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
| --- | --- | --- | --- | --- |
| CPU local arm | HPA CPU utilization / source queue+KV | Owned Kind metrics APIs and mock server | Yes, captured raw snapshots | ✓ FLOWING |
| Queue local arm | queue custom metric and CPU non-target | Owned Kind custom/resource metrics APIs | Yes; each negative CPU capture is ≤16.456944m, and the queue trigger reaches desired/ready=2 | ✓ FLOWING |
| Corrected KV local arm | KV custom metric and CPU/queue non-targets | Owned Kind custom/resource metrics APIs | Yes, captured raw snapshots | ✓ FLOWING |
| Integrity anchor | declared artifacts → sums → root → guard journal | No selected-run output exists | No | ✗ DISCONNECTED |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
| --- | --- | --- | --- |
| Automated safety/control behavior | `make test` | 32 passed | ✓ PASS |
| Static security policy | `semgrep --config .semgrep.yml --error src tests scripts` | 0 findings | ✓ PASS |
| Local manifests | `kubectl apply --dry-run=client -f k8s/local/` | All seven resources client-validated | ✓ PASS |
| Three-arm validator | `python3 scripts/validate_live_evidence.py local-evidence/phase1-live-202609d03531790135589z/cpu local-evidence/phase1-live-202609d04271790137679z/queue local-evidence/phase1-live-202609d04031790136227z/kv` | `{"cpu": true, "kv": true, "queue": true}` | ✓ PASS — validates all negative-sample non-target margins, including queue CPU ≤48m. |
| Kind cleanup | `kind get clusters` | Only `clawdlinux-demo`, `desktop`, `ninevigil-demo`; neither owned rehearsal cluster remains | ✓ PASS |
| Vast inventory | `vastai show instances` | `Total: 0 instances`; `No instances found.` | ✓ PASS (read-only; no provider mutation) |

### Requirements Coverage

| Requirement group | Status | Evidence |
| --- | --- | --- |
| BUDG-01 through BUDG-05 | ✓ SATISFIED | Decimal ledger tests and implementation pass. |
| CTRL-01 through CTRL-03, ERR-01 through ERR-03 | ✓ SATISFIED | Journal/controller tests pass, including no retry after restart. |
| GUARD-01 through GUARD-05, ERR-04 | ✗ BLOCKED | No concrete separately reachable guard, heartbeat-loss loop, or autonomous teardown exists. |
| HPA-01 | ✓ SATISFIED FOR LOCAL CPU ARM | CPU evidence has 90-second desired=1 control and desired/ready=2 with queue/KV source metrics below documented margins. |
| HPA-02, HPA-06 | ✓ SATISFIED FOR FRESH LOCAL QUEUE ARM | The selected `d042...` queue run has seven negative CPU captures at or below 16.456944m, below the 48m margin, and validates desired/ready=2. |
| HPA-03 through HPA-05 | ✓ SATISFIED FOR CORRECTED LOCAL KV ARM | The `d040...` rerun, not the earlier 0.95 run, reaches KV 1.0 and records local-synthetic provenance and 1→2. |
| INT-01 through INT-05 | ✗ BLOCKED | No selected-run checksum/root/receipt/anchor evidence exists; code tests do not substitute for actual run integrity bundles. |
| SEC-01 through SEC-04 | ✓ SATISFIED FOR CURRENT CHECKOUT | Semgrep passes, tracked hook path is `.githooks`, and Phase commits carry signed-off trailers with no Codex co-author. |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
| --- | --- | --- | --- | --- |
| retained queue runs `d035...` and `d041...` | n/a | Failed crosstalk / partial resource-metrics capture | ℹ️ RETAINED | The crosstalk run and fail-closed partial-capture run are preserved as failures; neither is selected as proof. |
| `src/srecon26_poc/guard.py` | 19-24 | Protocol-only guard | 🛑 Blocker | Cannot autonomously tear down after controller loss. |
| selected `local-evidence` runs | n/a | Missing integrity bundle and external anchor | 🛑 Blocker | Raw evidence is mutable and cannot support the goal's integrity claim. |

### Human Verification Required

None before gap closure. Human review cannot turn the observed queue crosstalk, missing integrity outputs, or absent runtime guard into verified evidence.

### Gaps Summary

Phase 1 remains incomplete. CPU, the fresh isolated queue arm, and corrected KV arm demonstrate real owned Kind transitions, and all owned rehearsal clusters are gone. The queue crosstalk and partial-capture attempts are retained as failures; only the fresh `d042...` run is selected, with a negative-window CPU maximum of 16.456944m. The runtime guard and selected-run integrity chain remain unverified. No Vast instance exists and this audit performed no create, destroy, report, credential, or provisioning action.

Required remediation without provider activity:

1. Finalize the selected successful CPU, queue, and corrected-KV runs using explicit declared artifact lists, `SHA256SUMS`, `ROOT-HASH.txt`, and an external guard-journal receipt, then establish the specified signed anchor.
2. Implement and test the concrete independently reachable guard, including its heartbeat-loss autonomous exact-teardown path, before any code path can permit a paid create.

---

_Verified: 2026-09-23T04:32:43Z_

_Verifier: Codex (independent Phase 1 audit)_
