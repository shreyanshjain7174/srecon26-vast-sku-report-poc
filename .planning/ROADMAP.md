# Roadmap: SRECon26 LLM HPA PoC

## Overview

Three phases enforced by a hard dependency chain: build the safety envelope and validate all local HPA signal independence proofs before spending any cloud money; run the live GPU canary and optional A/B/A paired comparison only after local artifacts are anchored; then generate charts and assemble the deck from validated result files only. The deck generator is a pure consumer — it never drives experiment execution.

## Phases

- [ ] **Phase 1: Safety Foundation and Local Evidence** - Safety envelope (budget, guard, journal, semgrep gate) complete; all three local HPA arm independence proofs validated and anchored
- [ ] **Phase 2: Live GPU Canary and Paired Comparison** - Real GPU metric-path canary end-to-end on Vast.ai; conditional A/B/A paired comparison if budget and topology allow
- [ ] **Phase 3: Evidence, Charts, and Deck** - All artifacts analyzed, charts generated from validated results, 16-slide deck assembled and validated for Google Slides import

## Phase Details

### Phase 1: Safety Foundation and Local Evidence
**Goal**: Safety envelope is complete and verified before any paid run; all three local HPA arms independently drive 1→2 replica transitions with independence proofs
**Depends on**: Nothing (first phase)
**Requirements**: BUDG-01, BUDG-02, BUDG-03, BUDG-04, BUDG-05, GUARD-01, GUARD-02, GUARD-03, GUARD-04, GUARD-05, CTRL-01, CTRL-02, CTRL-03, CTRL-04, CTRL-05, CTRL-06, SEC-01, SEC-02, SEC-03, SEC-04, HPA-01, HPA-02, HPA-03, HPA-04, HPA-05, HPA-06, INT-01, INT-02, INT-03, INT-04, INT-05, ERR-01, ERR-02, ERR-03, ERR-04
**Success Criteria** (what must be TRUE):
  1. Budget ledger rejects any reservation that would exceed $5.00 and survives process restart with uncommitted reservations re-checked
  2. Teardown guard is armed on a separately-reachable always-on host before any paid create, and autonomously fires teardown on heartbeat loss without controller involvement
  3. Controller resumes idempotently after crash from last valid journal entry without re-issuing any already-recorded create call
  4. Each of the three local HPA arms (CPU, queue-depth, synthetic-KV) independently drives a 1→2 replica transition with non-target signal maxima documented below their configured margins
  5. Every run produces a SHA256SUMS + ROOT-HASH integrity bundle anchored in a signed git commit and copied to the guard journal
**Plans**: TBD

### Phase 2: Live GPU Canary and Paired Comparison
**Goal**: Real GPU metric-path is proven end-to-end on Vast.ai hardware; paired comparison either produces three complete A/B blocks or is correctly labeled exploratory
**Depends on**: Phase 1
**Requirements**: GPU-01, GPU-02, GPU-03, GPU-04, GPU-05, GPU-06, GPU-07, PAIR-01, PAIR-02, PAIR-03, PAIR-04
**Success Criteria** (what must be TRUE):
  1. vLLM on RTX 3090 accepts at least one inference request and the response is captured in a `provenance=real-gpu` artifact bundle
  2. Prometheus scrapes vLLM metrics and scraped time-series values appear in a captured artifact
  3. Custom-metrics API surfaces at least one vLLM-derived metric readable by the HPA controller
  4. The full observation path (vLLM → Prometheus → custom-metrics adapter → HPA) is documented in a single artifact with hop-level timestamps
  5. Paired comparison produces three complete A/B blocks with real-GPU artifacts, or is labeled exploratory with fewer blocks and cannot support a slide conclusion
**Plans**: TBD
**UI hint**: no

### Phase 3: Evidence, Charts, and Deck
**Goal**: All validated artifacts are analyzed, charts are generated only from VALID bundles, and the 16-slide deck is assembled and confirmed importable into Google Slides
**Depends on**: Phase 2
**Requirements**: ANLZ-01, ANLZ-02, ANLZ-03, ANLZ-04, ANLZ-05, ANLZ-06, CHART-01, CHART-02, CHART-03, DECK-01, DECK-02, DECK-03, DECK-04, DECK-05, DECK-06, ERR-05
**Success Criteria** (what must be TRUE):
  1. Analyzer returns a structured verdict (VALID/INVALID/EXPLORATORY) with failing checks listed, plus a machine-readable verdict.json consumed by the deck generator
  2. Charts are generated only from VALID-verdicted artifact bundles and carry provenance annotations (local-synthetic or real-gpu)
  3. 16-slide 16:9 .pptx is generated with speaker notes; slides 7–13 are produced from validated artifacts or substituted with limitation slides — never from placeholder data
  4. Deck imports cleanly into Google Slides with screenshot evidence covering all 16 slides
**Plans**: TBD

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Safety Foundation and Local Evidence | 0/? | Not started | - |
| 2. Live GPU Canary and Paired Comparison | 0/? | Not started | - |
| 3. Evidence, Charts, and Deck | 0/? | Not started | - |
