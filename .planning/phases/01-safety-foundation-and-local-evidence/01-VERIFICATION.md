---
phase: 01-safety-foundation-and-local-evidence
verified: 2026-09-23T04:13:16Z
status: gaps_found
score: 2/5 roadmap success criteria verified
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
  - truth: "CPU, queue-depth, and synthetic-KV each independently cause 1-to-2 desired and ready replica transitions while both non-target signals remain at least 20% below their thresholds."
    status: failed
    reason: "The queue proof's captured negative-control CPU samples are 970.926429m, 996.108985m, 934.611385m, and 996.257369m. With the live deployment's 100m CPU request, these are 970-996% utilization, not <=48% (20% below the 60% CPU HPA target). The existing validator inspects only scaled.cpu.json and misses this crosstalk."
    artifacts:
      - path: local-evidence/phase1-live-202609d03531790135589z/queue/negative-0.cpu.json
        issue: "970.926429m CPU during queue negative control."
      - path: local-evidence/phase1-live-202609d03531790135589z/queue/negative-1.cpu.json
        issue: "996.108985m CPU during queue negative control."
      - path: scripts/validate_live_evidence.py
        issue: "Checks only scaled CPU/Prometheus files, not all captured samples."
    missing:
      - "Re-run the owned queue arm with actual non-target CPU samples at or below 48% utilization (or the documented equivalent 20%-below margin) throughout the 90-second control and scaling window."
      - "Make validation calculate maxima from every captured source/resource sample, not only the scaled snapshot."
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

**Verified:** 2026-09-23T04:13:16Z

**Status:** gaps_found

**Re-verification:** No — the prior report had no structured gaps and its PASS narrative was independently rechecked.

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
| --- | --- | --- | --- |
| 1 | Budget reservations over $5.00 are rejected and persisted open reservations retain Decimal headroom after restart. | ✓ VERIFIED | `tests/unit/test_budget.py` passes in the 32-test suite; `ExposureLedger` parses JSON with `parse_float=Decimal`, persists reservations, and rejects the `$3.01` excess after `$2.00`. |
| 2 | A separately reachable guard is armed before paid create and autonomously tears down on heartbeat loss without the controller. | ✗ FAILED | `guard.py` contains only a `Protocol`, dataclass, and attestation validation; no process/service executes teardown. `controller.py` preflights an already-armed fake but does not arm it or record heartbeats. |
| 3 | Crash recovery never reissues an already-recorded create call. | ✓ VERIFIED | `tests/unit/test_controller.py::test_create_intent_is_not_reissued_after_reopen` passes; recovery lists by label after `CREATE_REQUESTED` rather than calling `create_once` again. |
| 4 | CPU, queue, and synthetic-KV each prove an independent 1→2 transition with non-target maxima below 20%-below margins. | ✗ FAILED | CPU and corrected KV support their individual transitions; queue's captured negative-control CPU is 970-996% utilization against a 60% target and required <=48% margin. `validate_live_evidence.py` incorrectly reports true because it reads only scaled CPU. |
| 5 | Every selected run has a SHA256SUMS + ROOT-HASH integrity bundle, guard-journal copy, and signed external anchor. | ✗ FAILED | Neither selected raw run has `SHA256SUMS`, `ROOT-HASH.txt`, or a guard receipt. `local-evidence/` is gitignored; the signed `51dc2f3` commit contains only metadata. |

**Score:** 2/5 roadmap success criteria verified.

### Required Artifacts

| Artifact | Expected | Status | Details |
| --- | --- | --- | --- |
| `src/srecon26_poc/budget.py` | Decimal, persistent $5 cap ledger | ✓ VERIFIED | Unit tests pass and implementation uses `Decimal` / `parse_float=Decimal`. |
| `src/srecon26_poc/journal.py` + `controller.py` | Hash-chain replay and single create after crash | ✓ VERIFIED | Journal and controller unit/integration tests pass; replay path reconciles by label. |
| `src/srecon26_poc/guard.py` | Independently executing guard and exact teardown on controller loss | ✗ STUB FOR RUNTIME CLAIM | Interface/validator only; no concrete external guard implementation exists. |
| `local-evidence/.../cpu` | Owned CPU arm with 90-second control and 1→2 | ✓ VERIFIED FOR LOCAL ARM | Seven desired=1 samples span 98 seconds; scaled HPA desired=2 and deployment ready=2. Queue=0.0 and KV=0.2 in source snapshots. |
| `local-evidence/.../queue` | Owned queue arm with isolated non-target signals | ✗ FAILED | HPA queue metric scaled 1→2, but raw negative CPU samples violate the non-target CPU constraint. |
| `local-evidence/.../kv` (rerun `d040...`) | Corrected owned KV arm with 90-second control and 1→2 | ✓ VERIFIED FOR LOCAL ARM | Seven desired=1 samples span 96 seconds; KV reaches 1.0 (>=0.96 required trigger), CPU max is 18.621092m and queue max 0.0; scaled desired/ready are 2. |
| Selected live evidence bundles | Checksums, root hash, guard anchor, signed external anchor | ✗ MISSING | Required integrity artifacts and receipt are absent from both rehearsal directories. |

### Key Link Verification

| From | To | Via | Status | Details |
| --- | --- | --- | --- | --- |
| `controller.py` | guard | preflight before `create_once` | PARTIAL | Preflight is present, but the controller neither arms nor heartbeats a concrete guard. |
| `run_live_local_rehearsal.sh` | Kind HPA/custom-metrics APIs | owned per-arm rehearsal | PARTIAL | CPU, queue, and corrected KV each reach ready replicas=2, but queue evidence does not establish signal independence. |
| `scripts/anchor_local_evidence.py` | guard receipt / signed evidence commit | integrity validation | NOT WIRED | Script exists but no selected run contains generated checksums, root, receipt, or anchor record. |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
| --- | --- | --- | --- | --- |
| CPU local arm | HPA CPU utilization / source queue+KV | Owned Kind metrics APIs and mock server | Yes, captured raw snapshots | ✓ FLOWING |
| Queue local arm | queue custom metric and CPU non-target | Owned Kind custom/resource metrics APIs | Yes, but CPU crosstalk contradicts independence | ✗ HOLLOW AS INDEPENDENCE PROOF |
| Corrected KV local arm | KV custom metric and CPU/queue non-targets | Owned Kind custom/resource metrics APIs | Yes, captured raw snapshots | ✓ FLOWING |
| Integrity anchor | declared artifacts → sums → root → guard journal | No selected-run output exists | No | ✗ DISCONNECTED |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
| --- | --- | --- | --- |
| Automated safety/control behavior | `make test` | 32 passed | ✓ PASS |
| Static security policy | `semgrep --config .semgrep.yml --error src tests scripts` | 0 findings | ✓ PASS |
| Local manifests | `kubectl apply --dry-run=client -f k8s/local/` | All seven resources client-validated | ✓ PASS |
| Existing three-arm validator | `python3 scripts/validate_live_evidence.py ...` | `{"cpu": true, "kv": true, "queue": true}` | ✗ FALSE POSITIVE — it omits queue negative-control CPU samples |
| Kind cleanup | `kind get clusters` | Only `clawdlinux-demo`, `desktop`, `ninevigil-demo`; neither owned rehearsal cluster remains | ✓ PASS |
| Vast inventory | `vastai show instances` | `Total: 0 instances`; `No instances found.` | ✓ PASS (read-only; no provider mutation) |

### Requirements Coverage

| Requirement group | Status | Evidence |
| --- | --- | --- |
| BUDG-01 through BUDG-05 | ✓ SATISFIED | Decimal ledger tests and implementation pass. |
| CTRL-01 through CTRL-03, ERR-01 through ERR-03 | ✓ SATISFIED | Journal/controller tests pass, including no retry after restart. |
| GUARD-01 through GUARD-05, ERR-04 | ✗ BLOCKED | No concrete separately reachable guard, heartbeat-loss loop, or autonomous teardown exists. |
| HPA-01 | ✓ SATISFIED FOR LOCAL CPU ARM | CPU evidence has 90-second desired=1 control and desired/ready=2 with queue/KV source metrics below documented margins. |
| HPA-02, HPA-06 | ✗ BLOCKED | Queue evidence contains non-target CPU utilization far above the required margin; validator does not inspect all samples. |
| HPA-03 through HPA-05 | ✓ SATISFIED FOR CORRECTED LOCAL KV ARM | The `d040...` rerun, not the earlier 0.95 run, reaches KV 1.0 and records local-synthetic provenance and 1→2. |
| INT-01 through INT-05 | ✗ BLOCKED | No selected-run checksum/root/receipt/anchor evidence exists; code tests do not substitute for actual run integrity bundles. |
| SEC-01 through SEC-04 | ✓ SATISFIED FOR CURRENT CHECKOUT | Semgrep passes, tracked hook path is `.githooks`, and Phase commits carry signed-off trailers with no Codex co-author. |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
| --- | --- | --- | --- | --- |
| `scripts/validate_live_evidence.py` | 18-23 | Validates only `scaled.source.prom` and `scaled.cpu.json` | 🛑 Blocker | A queue run with 970-996% non-target CPU is reported valid. |
| `src/srecon26_poc/guard.py` | 19-24 | Protocol-only guard | 🛑 Blocker | Cannot autonomously tear down after controller loss. |
| selected `local-evidence` runs | n/a | Missing integrity bundle and external anchor | 🛑 Blocker | Raw evidence is mutable and cannot support the goal's integrity claim. |

### Human Verification Required

None before gap closure. Human review cannot turn the observed queue crosstalk, missing integrity outputs, or absent runtime guard into verified evidence.

### Gaps Summary

Phase 1 is incomplete. The corrected KV-only rerun may be used as the KV proof; the earlier 0.95-KV attempt is retained as failed evidence and is not accepted. CPU and corrected KV arms demonstrate real owned Kind transitions, and all owned rehearsal clusters are gone. However, queue independence is disproven by its own negative-control resource metrics, the runtime guard is unimplemented, and neither selected successful run has the mandatory checksum/root/guard-anchor chain. No Vast instance exists and this audit performed no create, destroy, report, credential, or provisioning action.

Required remediation without provider activity:

1. Re-run the owned queue arm with every actual resource/source sample demonstrating CPU at or below 48% utilization (or the documented equivalent 20%-below margin) across the 90-second negative control and scaling window; make the validator compute maxima across all samples.
2. Finalize the selected successful CPU, queue, and corrected-KV runs using explicit declared artifact lists, `SHA256SUMS`, `ROOT-HASH.txt`, and an external guard-journal receipt, then establish the specified signed anchor.
3. Implement and test the concrete independently reachable guard, including its heartbeat-loss autonomous exact-teardown path, before any code path can permit a paid create.

---

_Verified: 2026-09-23T04:13:16Z_

_Verifier: Codex (independent Phase 1 audit)_
