# Project Research Summary

**Project:** SRECon26 LLM HPA PoC — Evidence Pipeline
**Domain:** GPU-rental experiment controller + Kubernetes HPA canary + conference presentation
**Researched:** 2026-09-23
**Confidence:** HIGH

## Executive Summary

This project is a presentation-grade evidence pipeline for a SRECon26 lightning talk that proves three independent HPA scaling signals (CPU, queue-depth, KV-cache) each drive 1→2 replica transitions on their own, culminating in a real GPU metric-path canary on Vast.ai. The defining architectural constraint is a hard $5.00 cost cap enforced with `decimal.Decimal` arithmetic, an independent teardown guard on a separate always-on host, and a hash-chained append-only journal that ensures crash-safe idempotent resume. Every claim in the 16-slide deck is gated against validated artifacts — fabricated data is structurally impossible, not just policy.

The recommended approach follows a strict three-phase dependency chain: first build the safety envelope (budget ledger, guard, journal, semgrep gate) and validate all local HPA arm independence proofs; second run the live GPU canary and optional A/B/A paired comparison only after local artifacts are fully anchored; third generate charts and assemble the deck from validated result files only. This ordering is non-negotiable because each phase's paid cloud runs depend on safety infrastructure validated in the previous phase, and the deck generator is strictly downstream — it never drives experiment execution.

The primary risks are float arithmetic in budget enforcement (silent overspend), ambiguous provider create responses triggering double-billing, and a teardown guard that is co-located with the controller (defeating its purpose). All three are architecture-level risks that must be addressed in Phase 1 before any paid run. Secondary risks (CUDA not executing on the rented GPU, HPA signal crosstalk in independence tests, Prometheus adapter RBAC gaps) are Phase 2 pre-flight items with defined smoke tests that block canary execution.

## Key Findings

### Recommended Stack

The stack splits into two explicit layers enforced by project constraint: a **safety core** (stdlib only — `decimal`, `hashlib`, `dataclasses`, `json`, `pathlib`, `subprocess`, `typing`, `asyncio`) and a **presentation/infra layer** (external packages allowed: `python-pptx≥1.0.2`, `matplotlib≥3.9`, `kubernetes≥31.0`, `prometheus_client≥0.21`). Any library that touches the budget path, journal, or checksum logic is prohibited regardless of convenience. The runtime is Python 3.14.

**Core technologies:**
- `decimal.Decimal` (stdlib): budget arithmetic — only safe representation for $5.00 hard cap; `float` cannot represent 0.01 exactly
- `hashlib` (stdlib): SHA-256 hash chain for journal tamper-evidence and ROOT-HASH integrity bundle
- `dataclasses` (stdlib): typed value objects for journal events and artifact metadata — no Pydantic on the safety path
- `json` + NDJSON (stdlib): append-only crash-safe journal serialization — no external DB, no binary format
- `asyncio` (stdlib): single-process concurrent heartbeat + experiment polling — no threading races
- `pytest 9` + `pytest-asyncio≥0.24`: test suite; `asyncio_mode = "auto"` required in pyproject.toml
- `python-pptx≥1.0.2`: Phase 3 deck assembly only — never imported by safety core
- `kubernetes≥31.0`: Phase 2 HPA status reads — preferred over kubectl JSON parsing
- `semgrep`: pre-flight security gate before every paid run — blocks on unresolved high-severity findings

### Expected Features

**Must have (table stakes):**
- Crash-safe controller with hash-chained append-only journal — idempotent resume after any failure
- Strict $5.00 budget ledger (Decimal) — blocks reservation if it would breach the cap
- Independent teardown guard on a separate always-on host, armed before every paid create
- Report-before-destroy protocol — `PROVIDER_FAULT_CONFIRMED` only triggers report; `DIAGNOSIS_UNRESOLVED` never does
- Local CPU, queue-depth, and synthetic-KV HPA arm independence proofs (three separate runs)
- Real GPU metric-path canary on Vast.ai RTX 3090 with vLLM v0.26.0 (pinned digest)
- SHA256 + ROOT-HASH integrity bundle anchored in signed git commit + guard journal copy
- 16-slide 16:9 .pptx with speaker notes, claim-gated (slides 7–13 from validated results only)
- Semgrep pre-flight gate — hard block before any paid run

**Should have (differentiators):**
- Provenance labels (`LOCAL_SYNTHETIC` / `GPU_REAL`) on every artifact, enforced by analyzer
- Claim-gated deck generator — missing dependencies substitute a limitation slide, never fabricated content
- External anchor: ROOT-HASH.txt in guard journal with acknowledgment receipt before git commit
- Conditional A/B/A paired comparison — three complete blocks required; fewer → exploratory label only

**Defer (v2+):**
- Multi-region or production cluster scope — adds cost and complexity that obscures the PoC signal
- Interactive HTML charts (plotly) — static PNGs are sufficient for PPTX and more reproducible
- Automated report button without human review — `DIAGNOSIS_UNRESOLVED` must always require human review

### Architecture Approach

The system is organized as a **safety envelope → experiment controller → signal/artifact layer → integrity system → deck generator** pipeline with strict one-way data flow. The safety envelope (budget ledger, teardown guard, credential manager) must be satisfied before any paid create. The experiment controller is a state machine backed by the append-only journal; it cannot reconstruct state from any source other than the journal on crash-resume. The integrity system validates all artifacts post-run and produces `ValidationResult` files; the deck generator is strictly downstream and never invokes the analyzer mid-render.

**Major components:**
1. **Budget Ledger** — `Decimal` arithmetic, reservation/commit/release cycle, OS-level lock before every append
2. **Teardown Guard** — independent always-on host; arms a deadline+heartbeat trip wire before every create; fires teardown autonomously on heartbeat loss or hard TTL
3. **Hash-Chained Journal** — `seq` + `previous_hash` (SHA256 of prior entry) per event; NDJSON; crash-resume truncates partial tail then re-sequences
4. **Experiment Controller** — state machine: reserve → arm guard → create (label-based, never retry) → observe → diagnose → report/destroy → commit cost → release guard → bundle
5. **Local HPA Arms (×3)** — CPU, queue-depth, synthetic-KV; each drives 1→2 replica independently with non-target signals held below margins throughout the transition window
6. **GPU Canary** — end-to-end: SSH + k3s config → vLLM (pinned digest) → Prometheus → custom-metrics API → autoscaling/v2 HPA; GPU smoke test (nvidia-smi + torch.cuda.is_available()) before any load
7. **Integrity Bundle** — SHA256SUMS + ROOT-HASH.txt; guard journal copy confirmed before git commit
8. **Analyzer** — validates provenance, timestamps, monotonicity, checksums, root hash anchor; hard rejects mixed provenance
9. **Deck Generator** — claim-gated; slides 7–13 require validated artifacts; missing → limitation slide

### Critical Pitfalls

1. **Float arithmetic in budget** — use `Decimal(str(raw_value))` at every JSON boundary; never `float` anywhere on the budget path; verify with unit test that accumulated sums match exactly
2. **Ambiguous create → double-billing** — never retry a create; reconcile by label (query instances matching the pre-generated unique label) before deciding whether to issue a new create
3. **Teardown guard co-located with controller** — guard MUST run on a separately-reachable always-on host; test reachability over a different network path before every paid run
4. **CUDA not executing on rented GPU** — run GPU smoke test (nvidia-smi + torch.cuda.is_available() inside the vLLM container) as step 0 of every canary; tear down and report as PROVIDER_FAULT_CONFIRMED if it fails
5. **Provenance mixing in evidence** — every artifact carries an explicit `provenance` field; analyzer hard-rejects bundles with missing or mixed labels; `LOCAL_SYNTHETIC` artifacts can never populate GPU claim slides

## Implications for Roadmap

Based on the combined research, the dependency graph dictates exactly three phases:

### Phase 1: PoC Safety Foundation and Local Evidence

**Rationale:** All paid cloud runs depend on budget ledger correctness, guard independence, and journal crash-safety. These cannot be validated concurrently with live runs — a float rounding bug discovered mid-canary is irrecoverable. Local HPA arm proofs are cheap (no cost exposure) and validate the full signal→HPA path before committing money to a GPU instance.

**Delivers:**
- Complete safety envelope (budget, guard, journal, credentials, semgrep gate)
- Three local HPA independence proofs with non-target signal margin enforcement
- SHA256 + ROOT-HASH bundle generation and external anchor workflow
- Full test suite (budget unit tests, journal crash-resume tests, arm independence tests, analyzer tests)

**Addresses:** All P1 table-stakes features except real GPU canary; all safety-path architecture components

**Avoids:**
- Float budget arithmetic (Pitfall 1) — enforced by unit tests before any paid work
- Ambiguous create retry (Pitfall 3) — label-based reconciliation implemented and tested
- Guard co-location (Pitfall 4) — guard_host/ deployed and reachability verified over separate network
- HPA signal crosstalk (Pitfall 10) — independence verified by recording all three signals throughout transition
- autoscaling/v2 version confusion (Pitfall 12) — API resource check in controller pre-flight
- DIAGNOSIS_UNRESOLVED false report (Pitfall 14) — state machine test enforced

**Research flag:** Standard patterns — well-documented stdlib usage, no external research needed

---

### Phase 2: Live GPU Canary and Conditional Paired Comparison

**Rationale:** Only begins after Phase 1 artifacts are fully validated and guard is confirmed reachable from a separate network. The real GPU canary is the only thing that proves the Prometheus → custom-metrics API → HPA path works with real GPU hardware. The paired comparison is conditional on topology and remaining budget (≥$3.00, two compatible SKUs).

**Delivers:**
- Real GPU metric-path canary (RTX 3090, vLLM v0.26.0 pinned digest, full observation path captured)
- GPU smoke test as step 0 (nvidia-smi + torch.cuda.is_available() inside container)
- Conditional A/B/A paired comparison (three complete blocks or exploratory-labeled partial)
- Canary artifacts with `GPU_REAL` provenance labels

**Uses:** `kubernetes≥31.0`, `subprocess + ssh`, vastai CLI (label-based, no retry), semgrep pre-flight

**Avoids:**
- CUDA not executing (Pitfall 8) — smoke test gates canary start
- vLLM image digest drift (Pitfall 9) — pull by @sha256 digest; pre-run manifest inspect
- Custom metrics RBAC gaps (Pitfall 11) — verify via `kubectl get --raw /apis/custom.metrics.k8s.io/...` before load test
- Prometheus warm-up gap (Pitfall 13) — wait for first successful inference before starting evidence timeline
- Hard TTL miscalculation (Pitfall 5) — effective_ttl = max_billing - report_window - teardown_margin
- Fewer than 3 A/B blocks presented as paired comparison (Pitfall 16) — analyzer enforces n_blocks≥3 gate

**Research flag:** Needs pre-run validation — GPU smoke test and RBAC verification are mandatory gates; Vast.ai SKU availability for two compatible GPU types is not guaranteed

---

### Phase 3: Evidence, Charts, and Deck

**Rationale:** Strictly downstream. Only begins after all planned experiments complete and artifacts are anchored. The analyzer runs once; the deck generator is invoked once. Any missing validated artifact substitutes a limitation slide — no re-running experiments from this phase.

**Delivers:**
- Evidence analyzer output (ValidationResult files for all artifacts)
- Chart PNGs from validated result files only (matplotlib)
- 16-slide 16:9 .pptx with speaker notes and claim-gating enforced
- Google Slides import validation (screenshot evidence for all 16 slides)

**Uses:** `python-pptx≥1.0.2`, `matplotlib≥3.9`, `mypy --strict` pass required before deck generation

**Avoids:**
- Provenance mixing in deck (Pitfall 6) — analyzer enforced; deck generator reads ValidationResult files only
- PPTX → Google Slides import corruption (Pitfall 17) — Google-available fonts only (Arial/Roboto/Open Sans); 16:9 canvas (13.33×7.5 inches); screenshot validation for all slides
- Paired comparison with <3 blocks presented as conclusion (Pitfall 16) — deck generator asserts n_blocks≥3 before rendering paired chart slide

**Research flag:** Standard patterns — python-pptx is well-documented; Google Slides font compatibility list is known

---

### Phase Ordering Rationale

- **Safety before spend**: Budget ledger, journal, and guard are correctness prerequisites for every paid run. A float rounding error or guard failure cannot be remediated retroactively.
- **Local before live**: Local HPA arms cost nothing, validate the full HPA signal path, and produce the majority of the deck slides (7–9). They are also the fastest path to a credible partial deck if the GPU budget runs out.
- **Artifacts before deck**: The deck generator is a pure consumer of validated artifacts. Running it before artifacts exist produces only limitation slides — no value. Running it after all artifacts are anchored produces the complete deck in one pass.
- **Conditional features gated by budget and topology**: The paired comparison and Google Slides import validation are P2 features — the talk can proceed without them if budget or compatible SKU availability is constrained.

### Research Flags

Phases needing deeper research during planning:
- **Phase 2:** Vast.ai SKU availability — two compatible GPU SKUs for A/B comparison are not guaranteed; the canary can proceed with any RTX 3090, but the paired comparison requires checking available inventory before committing budget
- **Phase 2:** Prometheus adapter RBAC configuration for k3s — k3s-specific RBAC for `custom.metrics.k8s.io` differs slightly from upstream; verify the exact ClusterRoleBinding needed

Phases with standard patterns (skip research):
- **Phase 1:** All stdlib patterns are well-documented; hash-chained journal, Decimal arithmetic, and append-only NDJSON are established patterns with no ambiguity
- **Phase 3:** python-pptx deck assembly and matplotlib static chart generation are well-documented; Google Slides compatibility font list is known

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | All decisions derived from explicit PROJECT.md constraints; no ambiguity in library selection |
| Features | HIGH | Requirements fully specified in PROJECT.md; no market research ambiguity |
| Architecture | HIGH | Component boundaries, data flow, and safety invariants stated explicitly in PROJECT.md key decisions |
| Pitfalls | HIGH | Critical pitfalls are directly encoded as project constraints; secondary pitfalls from k3s/vLLM/Vast.ai known behaviors |

**Overall confidence:** HIGH

### Gaps to Address

- **Guard host selection**: PROJECT.md requires a separately-reachable always-on host but does not specify one. A cheap VPS (fly.io, render, hetzner) must be provisioned before Phase 2 begins. Cost must be excluded from the $5.00 Vast.ai cap.
- **Vast.ai paired comparison SKU availability**: Two compatible GPU SKUs for the A/B/A comparison must be checked at canary time. If unavailable, the comparison is skipped and slides 12–13 become limitation slides.
- **vLLM pinned digest revalidation**: The digest `sha256:770fe65b...` in PROJECT.md must be revalidated against the registry before each paid run. If unreachable, halt and investigate.
- **Google Slides font test**: The exact font substitution behavior for the target slide templates should be verified once with a minimal test PPTX before the full deck is generated.

## Sources

### Primary (HIGH confidence)
- `PROJECT.md` — all safety constraints, key design decisions, cost cap, guard requirements, report-before-destroy protocol, provenance labels, claim gating
- Python 3.14 stdlib documentation — `decimal`, `hashlib`, `dataclasses`, `json`, `asyncio`, `pathlib`, `subprocess`, `typing`
- Kubernetes `autoscaling/v2` HPA API (stable since k8s 1.23) — custom metrics via `custom.metrics.k8s.io/v1beta2`
- pytest 9 + pytest-asyncio 0.24 release notes — `asyncio_mode = "auto"` requirement

### Secondary (MEDIUM confidence)
- python-pptx≥1.0.2 (python-pptx.readthedocs.io) — OOXML correctness, Google Slides import behavior
- prometheus_client Python library (github.com/prometheus/client_python) — metric exposition format
- kubernetes Python client v31 (github.com/kubernetes-client/python) — autoscaling/v2 object model
- vLLM project documentation — Prometheus metrics at `/metrics`; model load vs. HTTP server ready independence

### Tertiary (needs validation at run time)
- Vast.ai provider API — non-idempotent create confirmed; label-based reconciliation pattern derived from known failure mode
- k3s + NVIDIA Container Toolkit — device plugin + driver version dependencies; GPU smoke test is the validation gate
- Google Slides font compatibility — community-documented list; must be verified with a test import before full deck generation

---
*Research completed: 2026-09-23*
*Ready for roadmap: yes*
