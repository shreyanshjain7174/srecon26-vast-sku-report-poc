---
phase: 01-safety-foundation-and-local-evidence
verified: 2026-09-23T05:15:36Z
status: passed
score: 5/5 must-haves verified
overrides_applied: 0
re_verification:
  previous_status: gaps_found
  previous_score: 3/5
  gaps_closed:
    - "A concrete independently hosted guard has a remote heartbeat-loss teardown rehearsal with exact ownership and idempotent second tick evidence."
    - "The CPU, queue, and synthetic-KV selected local evidence roots have an independently hosted guard-journal anchor receipt."
  gaps_remaining: []
  regressions: []
---

# Phase 1: Safety Foundation and Local Evidence Verification Report

**Phase Goal:** Safety envelope is complete and verified before any paid run; all three local HPA arms independently drive 1→2 replica transitions with independence proofs.

**Verified:** 2026-09-23T05:15:36Z

**Status:** passed

**Re-verification:** Yes — after gap closure

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
| --- | --- | --- | --- |
| 1 | Budget ledger rejects exposure above $5.00 and rechecks persistent reservations after restart. | ✓ VERIFIED | The isolated suite reports `83 passed`; budget unit tests cover Decimal-only caps, category caps, persistence, and restart reconciliation. `scripts/prepaid_gate.py` always returns `eligible: false` and cannot authorize spending. |
| 2 | A separately reachable guard is armed before paid create and autonomously tears down on heartbeat loss without the controller. | ✓ VERIFIED | GitHub Actions run `35821274670` in the separate `shreyanshjain7174/srecon26-independent-guard` repository completed successfully. Downloaded artifact and tracked receipt match exactly: it armed the remote worker, lost heartbeat, issued exactly one exact-label fake-provider destroy for ID 417, then made no second destroy (`TEARDOWN_CONFIRMED`). The live workflow requires a root-only `VAST_API_KEY` secret before arming; no live credential or paid create was used in this phase. |
| 3 | Crash recovery does not reissue an already-recorded create. | ✓ VERIFIED | `tests/unit/test_controller.py::test_create_intent_is_not_reissued_after_reopen` passes; `ExperimentController.create_or_reconcile()` reconciles recorded `CREATE_REQUESTED` state by label rather than calling `create_once` again. |
| 4 | CPU, queue-depth, and synthetic-KV each independently drive 1→2 with non-target maxima below margins. | ✓ VERIFIED | `scripts/validate_live_evidence.py` returns `{"cpu": true, "kv": true, "queue": true}` for selected CPU `d035`, queue `d042`, and KV `d040` bundles. Queue's seven negative CPU captures peak at 16.456944m (≤48m margin); the selected bundles retain the raw HPA, deployment, metric, event, and timestamp captures. |
| 5 | Every selected run has SHA256SUMS + ROOT-HASH integrity evidence anchored in a signed commit and copied to the guard journal. | ✓ VERIFIED | All three selected bundles pass `shasum -a 256 -c SHA256SUMS`; their roots match `01-LOCAL-EVIDENCE-ANCHOR.json`. GitHub Actions run `35820524825` completed successfully on the independent guard repository; its downloaded journal records one `armed` plus three `controller_root_anchored` events for exactly CPU, queue, and KV roots. Downloaded receipt equals the tracked copy. Commit `927f99b` anchors the declared roots and is signed off. |

**Score:** 5/5 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
| --- | --- | --- | --- |
| `src/srecon26_poc/budget.py` | Decimal-only persistent exposure ledger | ✓ VERIFIED | Exercised by the full unit suite; no float budget path is accepted. |
| `src/srecon26_poc/journal.py`, `controller.py` | Hash-chained lifecycle and idempotent create recovery | ✓ VERIFIED | Typed journal state gates creation; recorded intents reconcile rather than retry. |
| `guard/guard_worker.py` and `.github/workflows/independent-guard.yml` | Separate, nonce-bound deadline guard | ✓ VERIFIED | Root-only secret path, immutable deadline, exact label/ID checks, heartbeat-loss tick, and durable journal are implemented; external runner rehearsal executed the teardown path. |
| `local-evidence/.../cpu`, `.../queue`, `.../kv` | Three complete selected local proof bundles | ✓ VERIFIED | Explicit 73-file allowlists, manifests, `SHA256SUMS`, and roots validate for each selected arm. |
| `receipts/phase1-anchor-35820524825.json` | Independent guard journal anchor receipt | ✓ VERIFIED | Remote action and downloaded journal independently confirm the three local roots. |
| `receipts/remote-guard-rehearsal-35821274670.json` | Remote guard teardown rehearsal receipt | ✓ VERIFIED | Remote action and artifact show one exact fake-provider destroy, idempotent second tick, and zero real provider calls. |
| `src/srecon26_poc/vast_provider.py`, `scripts/preflight_vast.py` | Read-only provider safety preflight | ✓ VERIFIED | Current read-only preflight returned `eligible: true`, `instance_count: 0`, `balance_threshold_enabled: false`; it is not paid-run authorization. |
| `.semgrep.yml`, `.githooks/pre-commit` | Branch-controlled security gate | ✓ VERIFIED | Semgrep found 0 findings; `make setup-hooks` sets `core.hooksPath` to `.githooks`. |

### Key Link Verification

| From | To | Via | Status | Details |
| --- | --- | --- | --- |
| `ExperimentController` | guard | guard preflight before `create_once` | WIRED | `create_or_reconcile()` admits a create only from `GUARD_ARMED`, validates an attestation, then records create intent. |
| GitHub guard channel | `guard_worker.py` | root-only secret, arm, heartbeat, tick | WIRED | Workflow writes the injected secret to `/etc/...` mode 0600, arms before channel watching, and ticks independently until a terminal receipt. |
| local bundles | independent guard journal | selected roots → run `35820524825` | WIRED | Downloaded action journal's three anchor-event payloads equal the three selected bundle roots. |
| queue/KV HPA manifests | Prometheus adapter | named custom metrics | WIRED | Deterministic evidence and local-arm integration tests pass for both custom-metric arms. |
| preflight | Vast CLI | read-only user, inventory, offer inspection | WIRED | Live read-only invocation observed zero instances and explicit auto-recharge disablement; no mutating CLI operation was run. |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
| --- | --- | --- | --- | --- |
| CPU local arm | CPU metric plus queue/KV controls | Owned Kind metric/HPA snapshots | Yes — selected raw capture files | ✓ FLOWING |
| Queue local arm | Queue metric plus CPU/KV controls | Owned custom/resource metric snapshots | Yes — selected raw capture files | ✓ FLOWING |
| KV local arm | Synthetic-KV metric plus CPU/queue controls | Owned custom/resource metric snapshots | Yes — selected raw capture files | ✓ FLOWING |
| Integrity anchor | Three SHA-256 roots | Downloaded independent-runner guard journal | Yes — three exact root-anchor events | ✓ FLOWING |
| Remote teardown | Heartbeat timeout → exact destroy | GitHub-hosted guard worker and network-free fake CLI | Yes for the rehearsal; deliberately not a live Vast call | ✓ FLOWING (rehearsal scope) |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
| --- | --- | --- | --- |
| Full deterministic suite | `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q` | `83 passed in 11.69s` | ✓ PASS |
| Static security policy | `semgrep --config .semgrep.yml --error src tests scripts` | 0 findings | ✓ PASS |
| Three selected local arms | `python3 scripts/validate_live_evidence.py .../cpu .../queue .../kv` | `{"cpu": true, "kv": true, "queue": true}` | ✓ PASS |
| Bundle checksums | `shasum -a 256 -c SHA256SUMS` in each selected bundle | CPU, queue, and KV all succeeded | ✓ PASS |
| Independent anchor | download run `35820524825` artifact and compare receipt/journal | Remote action success; tracked receipt byte-identical; all three roots present | ✓ PASS |
| Independent teardown rehearsal | download run `35821274670` artifact and compare receipt/call log | Remote action success; one fake destroy, idempotent second tick, zero real calls | ✓ PASS |
| Current provider no-spend safety | `scripts/preflight_vast.py --read-only` | `instance_count: 0`; `balance_threshold_enabled: false` | ✓ PASS |

### Probe Execution

| Probe | Command | Result | Status |
| --- | --- | --- | --- |
| Remote anchor workflow | GitHub Actions run `35820524825` | Completed `success` at commit `b1931d42…`; artifact journal downloaded and checked | PASS |
| Remote guard rehearsal workflow | GitHub Actions run `35821274670` | Completed `success` at commit `c28aaacb…`; artifact journal/call log downloaded and checked | PASS |

### Requirements Coverage

| Requirement | Status | Evidence |
| --- | --- | --- |
| BUDG-01 through BUDG-05; CTRL-01 through CTRL-06; ERR-01 through ERR-04 | ✓ SATISFIED | Full deterministic tests cover ledger, hash-chain/recovery, ambiguity halt, report ordering, credential path, and absence quorum. |
| GUARD-01 through GUARD-05 | ✓ SATISFIED | Concrete worker/channel implementation plus independently hosted heartbeat-loss rehearsal. The rehearsal is correctly credential-free; a real paid run remains contingent on the live secret and fresh preflight. |
| HPA-01 through HPA-06 | ✓ SATISFIED | Selected d035/d042/d040 captures validate their 1→2 transitions, provenance, windows, and non-target controls. |
| INT-01 through INT-05 | ✓ SATISFIED | Complete bundle checksums/roots, signed root metadata commit, and a separately hosted journal receipt that records each selected root. |
| SEC-01 through SEC-04 | ✓ SATISFIED | Semgrep is clean, branch hook is configured, and all first-parent commits through verification are signed off without a Codex co-author. |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
| --- | --- | --- | --- |
| selected local evidence | n/a | Retained earlier crosstalk/partial capture attempts | ℹ️ INFO | They are not selected as proof; the isolated d042 queue run and d040 KV rerun are selected instead. |
| remote guard rehearsal | n/a | Fake provider backend | ℹ️ INFO | Explicitly scoped to no-spend runtime-path proof. It must not be represented as a live Vast teardown. |

### Human Verification Required

None. The verifiable Phase 1 contract is covered by deterministic tests, downloaded independent-runner artifacts, source inspection, and read-only current provider facts. A future paid run still needs a fresh live-secret guard dispatch and preflight, but that is an authorization condition for Phase 2 rather than an unverified Phase 1 behavior.

### Gaps Summary

No Phase 1 blockers remain. The earlier integrity and concrete-guard gaps are closed by externally retrievable GitHub Actions artifacts, not by SUMMARY.md assertions. The remote rehearsal was deliberately credential-free and had zero real provider calls; it validates autonomous exact teardown mechanics only. The implementation continues to require a root-only live guard secret and a fresh read-only Vast preflight before any paid operation, and the no-spend gate itself never grants authorization.

---

_Verified: 2026-09-23T05:15:36Z_

_Verifier: Codex (gsd-verifier)_
