# SRECon26 LLM HPA PoC

## What This Is

A presentation-grade evidence pipeline for a four-minute SRECon lightning talk demonstrating that CPU-only Kubernetes HPA can miss LLM demand because queue depth, KV-cache pressure, and time-to-first-token can worsen before CPU utilization creates a useful scaling signal. The project produces checksummed, externally-anchored artifacts — local signal independence proofs, a real GPU metric-path canary, and optionally a paired CPU-vs-queue/KV comparison — then assembles them into a 16-slide PowerPoint deck with speaker notes. Every slide claim must trace to a captured artifact or be labeled as background, hypothesis, or future work.

## Core Value

Every slide claim traces to a checksummed, independently-anchored artifact — no fabricated data, no unsupported conclusions.

## Requirements

### Validated

(None yet — ship to validate)

### Active

- [ ] Crash-safe experiment controller with hash-chained append-only run journal and idempotent resume
- [ ] Strict $5.00 project exposure ledger using Decimal arithmetic; blocks creation if reservation would breach it
- [ ] Report-before-destroy contract: qualifying provider SKU faults capture evidence, click Report, then teardown — in that order
- [ ] Independent teardown guard running on a separately-reachable always-on host, armed before every paid create
- [ ] Local CPU, queue, and synthetic-KV HPA arms each independently drive a 1→2 replica transition with non-target signals held below their margins
- [ ] Real GPU metric-path canary: single-node k3s on Vast.ai RTX 3090, vLLM v0.26.0, Prometheus → custom-metrics API → autoscaling/v2 HPA, full end-to-end observation path captured
- [ ] Conditional paired comparison (CPU-only HPA vs queue/KV-aware HPA): runs only when two compatible ready GPU capacities and budget remain; three complete A/B/A–B/A/B blocks required
- [ ] Checksummed integrity bundle per run: SHA256SUMS + ROOT-HASH.txt anchored in a signed Git commit and copied to the independent guard journal
- [ ] Semgrep security scan with no unresolved high-severity findings before any paid run
- [ ] 16-slide 16:9 .pptx with speaker notes that imports cleanly into Google Slides, validated with screenshot evidence

### Out of Scope

- Multi-region or production clusters — PoC is bounded to a single rented GPU VM and local k3s
- Production KServe, llm-d, or sharded/distributed inference — talk covers what they expose, not deploying them
- General claim that CPU HPA always fails — conclusion is limited to the evidence captured
- Treating local synthetic KV metrics as real GPU/KV evidence — provenance labels are mandatory and the analyzer enforces them
- Spending provider credit without an independently reachable teardown guard — no exceptions
- Retrying an ambiguous create request — reconcile by label, never issue a second create
- Report-button automation defeating the cost deadline — report window is bounded; guard never extends the hard deadline
- Frontier model benchmarking or performance comparisons across model families

## Context

The reference workspace already has evidence that three local signals can scale a mock workload from one replica to two; that evidence is local and synthetic. Existing Vast.ai rentals proved only that RTX 3090 instances returned, the pinned vLLM container reached provider-running state, and SSH became reachable — they do not prove CUDA execution, real vLLM requests, Prometheus scraping, custom-metrics API values, HPA behavior, or the CPU-vs-queue/KV comparison.

The only currently evidenced runnable image path is `docker.io/vllm/vllm-openai:v0.26.0` at `sha256:770fe65b2c73ee74a5c42165cf3433de4048cc2cd9c57a937ca4e35aba5aa87b`. This digest must be revalidated before every paid run.

Implementation is in three milestone phases:
1. **PoC Safety Foundation and Local Evidence** — controller, budget, guard/report protocols, journal, and all three local HPA arms with validated independence proofs.
2. **Live GPU Canary and Comparison** — real GPU metric-path canary plus conditional paired comparison if topology and budget allow.
3. **Evidence, Charts, and Deck** — analysis, graph generation, and the 16-slide PowerPoint.

## Constraints

- **Tech Stack**: Python 3.14, standard library only for core safety logic (dataclasses, Decimal, protocols), pytest 9, `autoscaling/v2` — no shortcuts that hide Decimal precision or add external budget-path dependencies
- **Budget**: Maximum $5.00 new Vast.ai exposure across all paid runs in this project ledger; $1.00 reserved for GPU/CUDA smoke, $1.00 for vLLM canary, up to $3.00 remaining for paired comparison
- **Safety**: Independent teardown guard must be armed and independently reachable before any provider create; laptop sleep or loss must not affect it
- **Credential hygiene**: Provider credential read from root-owned `0600` secret file; never in repo, run artifacts, process arguments, or logs
- **Commits**: All commits signed off with `git commit -s`; no Codex coauthor trailer
- **Evidence integrity**: Analyzer rejects missing files, mixed provenance, out-of-window timestamps, non-monotonic ordering, checksum mismatch, or root hash that doesn't match external anchor
- **Claim gating**: Deck generation refuses to render unsupported claims; slides 7–13 generated from validated result files only
- **Timeline**: Talk is SRECon26 (2026-09-23); all implementation occurs before that date on branch `feat/vast-sku-report-poc`

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Python 3.14 + stdlib Decimal for all budget arithmetic | Floating-point rounding cannot be trusted for $5.00 cap enforcement; Decimal at JSON boundaries is mandatory | — Pending |
| Hash-chained append-only journal (not a database) | Crash safety, idempotent resume, and tamper-evidence without external dependencies; each event carries sequence + previous_hash | — Pending |
| Independent guard on a separate always-on host | Laptop sleep or network loss must not extend the hard deadline; guard authority activates only after heartbeat loss or hard deadline, never before | — Pending |
| Report before destroy, never the reverse | Provider fault reporting requires the instance to still exist; cost deadline is accounted for by subtracting report window + teardown margin from hard TTL | — Pending |
| `DIAGNOSIS_UNRESOLVED` never triggers provider report | Controller/network/model failures must not generate false reports; only `PROVIDER_FAULT_CONFIRMED` fires the report adapter | — Pending |
| Paired comparison requires three complete A/B blocks | Smaller or unbalanced runs are labeled exploratory and cannot support a performance conclusion or paired chart | — Pending |
| Deck slides 7–13 generated from validated result files | Fabricated example data is explicitly prohibited; if real results are absent, a limitation slide is substituted | — Pending |
| Single pinned image digest `sha256:770fe65...` | Only currently evidenced runnable path; revalidated before every paid run to prevent digest drift | — Pending |

---
*Last updated: 2026-09-23 after initial design and implementation plan approval*
