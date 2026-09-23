# Feature Research

**Domain:** Presentation-grade evidence pipeline for SRE/platform engineering lightning talk
**Researched:** 2026-09-23
**Confidence:** HIGH — requirements are fully specified in PROJECT.md; no market research ambiguity

## Feature Landscape

### Table Stakes (Users Expect These)

These features are non-negotiable. Missing any one makes the whole PoC invalid for conference presentation.

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Crash-safe experiment controller | Any multi-hour GPU run will encounter failures; idempotent resume is mandatory | HIGH | Hash-chained append-only journal; each event carries sequence + previous_hash |
| Strict budget ledger ($5.00 cap) | Paid cloud runs with hard cost ceiling; floating-point rounding must not drift | MEDIUM | Python Decimal only; blocks reservation if it would breach the cap |
| Independent teardown guard | Laptop sleep or network loss cannot extend billable time; guard must be on a separate always-on host | HIGH | Armed before every paid create; never waits for controller heartbeat to arm |
| Report-before-destroy protocol | Vast.ai fault reporting requires the instance to still exist; ordering error is irrecoverable | MEDIUM | Only `PROVIDER_FAULT_CONFIRMED` fires the report adapter; `DIAGNOSIS_UNRESOLVED` never does |
| Local CPU HPA independence proof | Must show CPU signal alone drives 1→2 replica scale with queue/KV signals held below margin | MEDIUM | Non-target signals held below thresholds during the CPU arm run |
| Local queue-depth HPA independence proof | Must show queue signal alone drives 1→2 replica scale with CPU/KV signals held below margin | MEDIUM | Same isolation requirement as CPU arm |
| Local synthetic-KV HPA independence proof | Must show synthetic KV signal alone drives 1→2 replica scale with CPU/queue signals held below margin | MEDIUM | Provenance label mandatory: this is synthetic, not real GPU KV pressure |
| Real GPU metric-path canary | Only thing that proves the Prometheus → custom-metrics API → HPA path works with a real GPU | HIGH | k3s on Vast.ai RTX 3090, vLLM v0.26.0, full end-to-end observation path captured |
| SHA256 + ROOT-HASH integrity bundle | Every artifact must be checksummed and externally anchored | MEDIUM | Signed Git commit + copy to guard journal per run |
| 16-slide .pptx with speaker notes | Final deliverable for the talk; must import cleanly into Google Slides | HIGH | Slides 7–13 only generated from validated result files; claim gating enforced |

### Differentiators (What Makes This Evidence Pipeline Trustworthy)

These are not "competitive" features in a market sense — they are what separates a credible conference talk from a demo that can't be reproduced.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Claim-gated deck generation | Refuses to render unsupported slides — a fabricated chart is impossible by construction | HIGH | Analyzer validates provenance, timestamps, checksums before any slide 7–13 is rendered |
| Provenance labels on all artifacts | Distinguishes local-synthetic evidence from real GPU evidence so the audience can assess each claim | MEDIUM | Analyzer enforces; missing or mixed provenance is a hard rejection |
| External anchor for integrity | Root hash copied to independent guard journal proves artifacts weren't created after the fact | MEDIUM | Signed Git commit is the anchor; guard journal is a second copy |
| Conditional paired comparison (A/B/A pattern) | If budget and topology allow, three complete A/B blocks produce a statistically defensible comparison chart | HIGH | Labeled "exploratory" if fewer than three blocks; never used for a performance conclusion |
| Semgrep pre-flight gate | No high-severity security findings before any paid run — prevents credential leaks and unsafe code reaching the GPU host | LOW | Run on every commit; blocks paid runs if unresolved findings remain |
| Idempotent reconcile-by-label, never retry | Prevents double-billing on ambiguous create responses — a known failure mode for Vast.ai and similar providers | MEDIUM | Controller reconciles by label; a second create is never issued |

### Anti-Features (Commonly Requested, Often Problematic)

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| Floating-point budget arithmetic | Simpler code, fewer imports | Rounding drift can allow a $5.01 reservation to pass the $5.00 cap check | Use `decimal.Decimal` at every JSON boundary; no floats on the budget path |
| Auto-retry on ambiguous create | Seems like resilience | Creates duplicate paid instances; billing ambiguity is irrecoverable | Reconcile by label, report the ambiguity, and halt until resolved |
| Report-button automation bypassing deadline | Seems like full automation | Could extend the hard deadline window indefinitely, blowing the cost cap | Report window is bounded; guard never extends the hard deadline regardless of report status |
| Treating synthetic KV as real GPU evidence | Saves $2–3 on the vLLM canary run | Misleads the audience; a conference talk with fabricated GPU data is career-damaging | Run the real canary or substitute a limitation slide — no middle ground |
| Auto-merge/squash without signed-off commit | Convenience | Violates project commit policy (`git commit -s`); breaks auditability chain | Always sign off; CI should reject unsigned commits |
| Multi-region or production cluster scope | More impressive demo | Out of scope; adds cost, complexity, and latency that obscures the local PoC signal | Keep to single rented GPU VM + local k3s; footnote production path in the talk |
| Example/placeholder data in deck slides 7–13 | Allows slide authoring before experiments run | Fabricated data is explicitly prohibited; audiences assume slide data is real | If results are absent, substitute a "limitation: experiment not yet run" slide |

## Feature Dependencies

```
[Budget Ledger (Decimal)]
    └──required by──> [Independent Teardown Guard]
                          └──required by──> [Any Paid Cloud Run]
                                                └──required by──> [Real GPU Canary]
                                                └──required by──> [Paired Comparison (conditional)]

[Hash-Chained Journal]
    └──required by──> [Crash-Safe Controller]
                          └──required by──> [Any Paid Cloud Run]

[Report-Before-Destroy Protocol]
    └──required by──> [Any Paid Cloud Run]

[Local CPU HPA Arm] ──independence proof──> [Local Queue HPA Arm] ──independence proof──> [Local KV HPA Arm]
    All three ──required by──> [Deck Slides 7–9 (local evidence)]

[Real GPU Canary] ──required by──> [Deck Slides 10–11 (GPU metric path)]

[Paired Comparison]
    └──conditional on──> [Real GPU Canary complete] + [budget remaining ≥ $3.00] + [two compatible GPU SKUs available]
    └──required by──> [Deck Slides 12–13 (A/B comparison)]

[SHA256 + ROOT-HASH Bundle]
    └──required by──> [Claim-Gated Deck Generation]
    └──required by──> [External Anchor (signed Git commit)]

[Semgrep Pre-flight Gate]
    └──blocks──> [Any Paid Cloud Run] (if unresolved high-severity findings)

[All Validated Artifacts]
    └──required by──> [16-slide .pptx]
```

### Dependency Notes

- **Budget Ledger requires Decimal**: Budget check runs before every reservation; float rounding on a $5.00 cap is not acceptable.
- **Teardown Guard required before any paid create**: The guard must be armed and independently reachable *before* the create call is issued — not after.
- **Journal required for idempotent resume**: Without the hash chain, a crashed controller cannot safely resume without risk of re-issuing a create.
- **Three complete A/B blocks required for paired comparison**: Fewer blocks produce exploratory data only, which cannot be used for a slide conclusion.
- **Real GPU canary is a prerequisite for paired comparison**: The canary run validates the metric path; the comparison uses the same path at scale.
- **Provenance labels enforced at analysis time**: Analyzer rejects any artifact bundle with mixed or missing provenance before deck generation begins.

## MVP Definition

### Launch With (v1 — Milestone 1: PoC Safety Foundation and Local Evidence)

Minimum needed before any paid GPU run can begin.

- [x] Crash-safe experiment controller with hash-chained append-only journal
- [x] Strict $5.00 budget ledger using Decimal arithmetic
- [x] Independent teardown guard (separate always-on host, armed before every paid create)
- [x] Report-before-destroy protocol with `PROVIDER_FAULT_CONFIRMED` gate
- [x] Local CPU HPA arm: independent 1→2 replica transition, non-target signals below margin
- [x] Local queue-depth HPA arm: independent 1→2 replica transition
- [x] Local synthetic-KV HPA arm: independent 1→2 replica transition, provenance labeled
- [x] SHA256 + ROOT-HASH integrity bundle per run, anchored in signed Git commit
- [x] Semgrep pre-flight gate with no unresolved high-severity findings

### Add After Milestone 1 Validation (Milestone 2: Live GPU Canary and Comparison)

Run only once Milestone 1 artifacts are fully validated and guard is confirmed reachable.

- [ ] Real GPU metric-path canary: k3s on Vast.ai RTX 3090, vLLM v0.26.0, Prometheus → custom-metrics API → HPA, full observation path captured
- [ ] Conditional paired comparison: three complete A/B/A blocks, only when two compatible SKUs and ≥ $3.00 budget remain

### Final Phase (Milestone 3: Evidence, Charts, and Deck)

Run only after all planned experiments complete.

- [ ] Evidence analyzer: validates provenance, timestamps, checksums, root hash anchor
- [ ] Chart generation from validated result files only
- [ ] 16-slide 16:9 .pptx with speaker notes, claim-gated, slides 7–13 from validated results
- [ ] Google Slides import validation (screenshot evidence)

## Feature Prioritization Matrix

| Feature | User Value | Implementation Cost | Priority |
|---------|------------|---------------------|----------|
| Budget ledger (Decimal) | HIGH | LOW | P1 |
| Teardown guard | HIGH | HIGH | P1 |
| Crash-safe controller + journal | HIGH | HIGH | P1 |
| Report-before-destroy protocol | HIGH | MEDIUM | P1 |
| Local CPU/queue/KV HPA arms | HIGH | MEDIUM | P1 |
| SHA256 + ROOT-HASH bundle | HIGH | LOW | P1 |
| Semgrep pre-flight gate | HIGH | LOW | P1 |
| Real GPU metric-path canary | HIGH | HIGH | P1 |
| Evidence analyzer + claim gating | HIGH | MEDIUM | P1 |
| 16-slide .pptx generator | HIGH | MEDIUM | P1 |
| Paired comparison (A/B/A) | MEDIUM | HIGH | P2 |
| Google Slides import validation | MEDIUM | LOW | P2 |

**Priority key:**
- P1: Must have for a credible talk — missing any P1 breaks the integrity chain
- P2: Should have; adds slide depth and statistical credibility but talk can proceed without it

## Sources

- `PROJECT.md` — primary requirements source (HIGH confidence; project-specific spec)
- Kubernetes `autoscaling/v2` HPA documentation — governs custom-metrics API integration
- vLLM project (docker.io/vllm/vllm-openai:v0.26.0) — only evidenced runnable image path
- Vast.ai GPU rental — provider for real GPU canary and paired comparison runs
- SRECon lightning talk format — four minutes, 16 slides, evidence-driven claim structure

---
*Feature research for: SRECon26 LLM HPA evidence pipeline*
*Researched: 2026-09-23*
