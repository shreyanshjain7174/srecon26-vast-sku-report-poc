# Requirements: SRECon26 LLM HPA PoC

**Defined:** 2026-09-23
**Core Value:** Every slide claim traces to a checksummed, independently-anchored artifact — no fabricated data, no unsupported conclusions.

## v1 Requirements

### Budget & Cost Safety

- [x] **BUDG-01**: Budget ledger rejects any reservation that would cause total project exposure to exceed $5.00 using `decimal.Decimal` arithmetic — no floating-point on the budget path
- [x] **BUDG-02**: Budget ledger persists across process restarts; uncommitted reservations are re-checked at resume
- [x] **BUDG-03**: Budget ledger exposes a read-only query interface that returns remaining headroom as a `Decimal` value
- [x] **BUDG-04**: Budget sub-allocations are enforced per run category: $1.00 for GPU/CUDA smoke, $1.00 for vLLM canary, up to $3.00 for paired comparison
- [x] **BUDG-05**: Any code path that converts budget values to or from JSON uses `Decimal`; `float` is prohibited on the budget path

### Teardown Guard

- [x] **GUARD-01**: An independent teardown guard process runs on a separately-reachable always-on host and is armed (acknowledgement received) before any paid provider `create` call is issued
- [x] **GUARD-02**: Guard activates teardown only after heartbeat loss from the controller or expiry of the hard TTL deadline — never before either condition
- [x] **GUARD-03**: Guard authority cannot be extended by the report window or any controller action; the hard deadline is immovable once armed
- [x] **GUARD-04**: Guard journal receives a copy of every run's SHA256SUMS + ROOT-HASH after each integrity bundle is produced
- [x] **GUARD-05**: Loss of the controller host (laptop sleep, network partition) does not prevent the guard from executing teardown

### Experiment Controller

- [x] **CTRL-01**: Controller uses a hash-chained append-only run journal; each event record carries a monotone sequence number and `previous_hash` field
- [x] **CTRL-02**: Controller resumes idempotently after a crash by replaying the journal from the last valid hash-chain entry without re-issuing any already-recorded create call
- [x] **CTRL-03**: Ambiguous provider `create` responses are never retried; controller reconciles instance state by label, records the ambiguity event, and halts until resolved
- [x] **CTRL-04**: Controller enforces the report-before-destroy ordering: qualifying `PROVIDER_FAULT_CONFIRMED` events trigger the report adapter before teardown; destroy is not issued until the report step completes or the hard deadline forces it
- [x] **CTRL-05**: `DIAGNOSIS_UNRESOLVED` status never triggers the provider report adapter; only `PROVIDER_FAULT_CONFIRMED` does
- [x] **CTRL-06**: Controller reads provider credentials from a root-owned `0600` secret file; credentials never appear in repo files, run artifacts, process arguments, environment exports, or logs

### Security & Pre-flight

- [x] **SEC-01**: Semgrep scan with no unresolved high-severity findings must pass before any paid provider `create` call is issued
- [x] **SEC-02**: Semgrep scan runs automatically on every commit on branch `feat/vast-sku-report-poc`
- [x] **SEC-03**: Provider credentials are never committed to the repository; CI rejects commits containing secrets patterns
- [x] **SEC-04**: All commits are signed off with `git commit -s`; no Codex co-author trailer is included

### Local HPA Arms (Independence Proofs)

- [x] **HPA-01**: CPU arm: a synthetic CPU load drives a 1→2 replica transition in local k3s; queue-depth and synthetic-KV metrics remain below their configured thresholds throughout the run, documented in the captured artifact
- [x] **HPA-02**: Queue-depth arm: synthetic queue depth drives a 1→2 replica transition; CPU utilization and synthetic-KV metrics remain below their configured thresholds throughout the run
- [x] **HPA-03**: Synthetic-KV arm: synthetic KV-cache-pressure metric drives a 1→2 replica transition; CPU and queue metrics remain below their configured thresholds throughout the run
- [x] **HPA-04**: Each local arm run produces a timestamped artifact bundle containing: raw metric samples, HPA event log, replica count timeline, and a provenance label identifying the signal type
- [x] **HPA-05**: The synthetic-KV artifact is labeled `provenance=local-synthetic`; the analyzer rejects any artifact bundle in which a KV metric is labeled `provenance=real-gpu` without a corresponding canary run
- [x] **HPA-06**: Each local arm independence proof declares the non-target signal maxima and asserts they stayed below margin; the analyzer verifies these values against the captured samples

### Integrity & Evidence Chain

- [x] **INT-01**: Every run produces a `SHA256SUMS` file covering all artifacts and a `ROOT-HASH.txt` derived from it; both are included in the integrity bundle
- [x] **INT-02**: The integrity bundle is anchored in a signed Git commit (`git commit -s`) on `feat/vast-sku-report-poc` immediately after the run completes
- [x] **INT-03**: The analyzer rejects any artifact bundle with: missing files, mixed or absent provenance labels, out-of-window timestamps, non-monotonic event ordering, checksum mismatch, or a root hash that does not match the external anchor
- [x] **INT-04**: Root hash is copied to the independent guard journal as a second external anchor within the same run lifecycle
- [x] **INT-05**: Artifact timestamps are validated to fall within the declared run window; artifacts created outside the window are rejected, not warned

### Real GPU Metric-Path Canary (Milestone 2)

- [ ] **GPU-01**: Single-node k3s cluster is provisioned on a Vast.ai RTX 3090 instance using `docker.io/vllm/vllm-openai:v0.26.0` at digest `sha256:770fe65b2c73ee74a5c42165cf3433de4048cc2cd9c57a937ca4e35aba5aa87b`; digest is revalidated immediately before each paid run
- [ ] **GPU-02**: vLLM process accepts at least one successful inference request over the OpenAI-compatible API; the response and latency are captured in the artifact bundle
- [ ] **GPU-03**: Prometheus scrapes vLLM metrics and the scraped values appear in a captured time-series artifact
- [ ] **GPU-04**: Custom-metrics API surfaces at least one vLLM-derived metric value readable by the HPA controller
- [ ] **GPU-05**: `autoscaling/v2` HPA object is configured against the custom metric; at least one HPA scale event (or a documented absence of scale event with explanation) is captured
- [ ] **GPU-06**: The full observation path — vLLM → Prometheus → custom-metrics adapter → HPA — is documented in a single artifact with timestamps linking each hop
- [ ] **GPU-07**: Canary run artifacts carry `provenance=real-gpu`; the analyzer enforces this label before allowing GPU-sourced evidence into deck generation

### Conditional Paired Comparison (Milestone 2)

- [ ] **PAIR-01**: Paired comparison runs only when: GPU canary is complete and validated, two compatible ready GPU capacities are available, and remaining budget is ≥ $3.00
- [ ] **PAIR-02**: Exactly three complete A/B blocks (A=CPU-only HPA, B=queue/KV-aware HPA) are required; a run with fewer blocks is labeled exploratory and cannot be used for a slide conclusion
- [ ] **PAIR-03**: Each A/B block uses the same workload replay trace; trace is captured as an artifact and checksummed
- [ ] **PAIR-04**: Comparison result artifacts carry `provenance=real-gpu`; mixed-provenance comparison data is rejected by the analyzer

### Evidence Analyzer

- [x] **ANLZ-01**: Analyzer accepts an artifact bundle path and returns a structured verdict: `VALID`, `INVALID`, or `EXPLORATORY`, with a list of failing checks for non-`VALID` outcomes
- [x] **ANLZ-02**: Analyzer verifies SHA256 checksums for every file listed in `SHA256SUMS` before any other check
- [x] **ANLZ-03**: Analyzer verifies the root hash matches the value recorded in the guard journal anchor
- [ ] **ANLZ-04**: Analyzer checks provenance labels on every artifact; missing or `MIXED` provenance is an `INVALID` verdict
- [ ] **ANLZ-05**: Analyzer checks event timestamps for monotonic ordering and containment within the declared run window
- [x] **ANLZ-06**: Analyzer verifies independence proof assertions: non-target signal maxima in the captured samples must be ≤ the declared margin for each local HPA arm

### Chart Generation

- [x] **CHART-01**: Charts are generated only from artifact bundles that the analyzer has returned a `VALID` verdict for; `INVALID` or `EXPLORATORY` bundles produce no charts
- [x] **CHART-02**: Each chart carries a provenance annotation derived from the source artifact's label (`local-synthetic` or `real-gpu`)
- [x] **CHART-03**: CPU-vs-queue/KV comparison chart is generated only when the paired comparison bundle is `VALID` with three complete A/B blocks; otherwise it is replaced by a limitation notice

### 16-Slide Deck

- [x] **DECK-01**: Deck generator produces a 16-slide, 16:9 `.pptx` file with speaker notes on every slide
- [x] **DECK-02**: Slides 7–13 are generated exclusively from `VALID` artifact bundles; the generator refuses to render these slides from unvalidated or fabricated data
- [x] **DECK-03**: If a required `VALID` artifact is absent, slides 7–13 are replaced by a "limitation: experiment not yet run" slide — no placeholder or example data is substituted
- [x] **DECK-04**: Slides 1–6 and 14–16 (background, hypothesis, future work) are labeled with their claim category; no background slide contains a claim that requires artifact support
- [ ] **DECK-05**: Deck imports cleanly into Google Slides with no broken fonts, missing slides, or layout regressions; this is validated by screenshot evidence captured after import
- [x] **DECK-06**: Deck generation produces a `DECK-MANIFEST.json` listing each slide's source artifact paths and checksum references

### Error Handling & Observability

- [x] **ERR-01**: All experiment controller state transitions are logged to the journal with ISO-8601 timestamps and structured key-value fields; no unstructured log lines on the critical path
- [x] **ERR-02**: Any exception that causes the controller to halt produces a `HALT` journal event with the exception class, message, and traceback hash before process exit
- [x] **ERR-03**: Budget ledger write failures are fatal and produce a `BUDGET_WRITE_FAIL` halt event; the controller never continues past a failed budget write
- [x] **ERR-04**: Guard heartbeat failures are logged with timestamp and retry count; the guard treats three consecutive missed heartbeats as controller loss
- [x] **ERR-05**: Analyzer run produces a machine-readable verdict file (`verdict.json`) in addition to human-readable output; deck generator consumes `verdict.json`, not log output

## v2 Requirements

### Extended Observability

- **OBS-01**: Real-time dashboard (local Grafana or similar) streaming HPA metrics during live GPU runs
- **OBS-02**: Alerting on budget headroom dropping below $0.50 during an active run

### Reproducibility Package

- **REPR-01**: Hermetic experiment replay: given a captured artifact bundle, the controller can reproduce the workload replay trace against a new instance
- **REPR-02**: Public artifact archive (e.g. Zenodo DOI) for post-talk reference linking

### Deck Automation

- **DECK-V2-01**: CI job that regenerates the deck on every push to `feat/vast-sku-report-poc` when new `VALID` artifact bundles are detected
- **DECK-V2-02**: PDF export of the deck for upload to the SRECon26 speaker portal

## Out of Scope

| Feature | Reason |
|---------|--------|
| Multi-region or production clusters | PoC is bounded to a single rented GPU VM and local k3s; adding regions obscures the signal |
| Production KServe, llm-d, or sharded/distributed inference | Talk describes what they expose; deploying them is out of scope |
| General claim that CPU HPA always fails | Conclusion is limited to the evidence captured in this PoC |
| Treating local synthetic KV metrics as real GPU/KV evidence | Misleads the audience; provenance labels are mandatory and the analyzer enforces them |
| Floating-point budget arithmetic | Float rounding cannot be trusted for a $5.00 hard cap; `Decimal` is mandatory |
| Auto-retry on ambiguous provider create | Creates duplicate paid instances; billing ambiguity is irrecoverable |
| Report-button automation bypassing the hard deadline | Could extend the cost window indefinitely; the guard never extends the hard deadline |
| Example or placeholder data in deck slides 7–13 | Fabricated data is explicitly prohibited; a limitation slide is substituted instead |
| Spending provider credit without an armed independent teardown guard | No exceptions; guard must be armed before any create call |
| Commits without `git commit -s` sign-off | Violates auditability chain |
| Frontier model benchmarking or cross-model performance comparison | Out of scope for a four-minute lightning talk on HPA scaling signals |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| BUDG-01 | Phase 1 | Complete |
| BUDG-02 | Phase 1 | Complete |
| BUDG-03 | Phase 1 | Complete |
| BUDG-04 | Phase 1 | Complete |
| BUDG-05 | Phase 1 | Complete |
| GUARD-01 | Phase 1 | Complete |
| GUARD-02 | Phase 1 | Complete |
| GUARD-03 | Phase 1 | Complete |
| GUARD-04 | Phase 1 | Complete |
| GUARD-05 | Phase 1 | Complete |
| CTRL-01 | Phase 1 | Complete |
| CTRL-02 | Phase 1 | Complete |
| CTRL-03 | Phase 1 | Complete |
| CTRL-04 | Phase 1 | Complete |
| CTRL-05 | Phase 1 | Complete |
| CTRL-06 | Phase 1 | Complete |
| SEC-01 | Phase 1 | Complete |
| SEC-02 | Phase 1 | Complete |
| SEC-03 | Phase 1 | Complete |
| SEC-04 | Phase 1 | Complete |
| HPA-01 | Phase 1 | Complete |
| HPA-02 | Phase 1 | Complete |
| HPA-03 | Phase 1 | Complete |
| HPA-04 | Phase 1 | Complete |
| HPA-05 | Phase 1 | Complete |
| HPA-06 | Phase 1 | Complete |
| INT-01 | Phase 1 | Complete |
| INT-02 | Phase 1 | Complete |
| INT-03 | Phase 1 | Complete |
| INT-04 | Phase 1 | Complete |
| INT-05 | Phase 1 | Complete |
| ERR-01 | Phase 1 | Complete |
| ERR-02 | Phase 1 | Complete |
| ERR-03 | Phase 1 | Complete |
| ERR-04 | Phase 1 | Complete |
| GPU-01 | Phase 2 | Pending |
| GPU-02 | Phase 2 | Pending |
| GPU-03 | Phase 2 | Pending |
| GPU-04 | Phase 2 | Pending |
| GPU-05 | Phase 2 | Pending |
| GPU-06 | Phase 2 | Pending |
| GPU-07 | Phase 2 | Pending |
| PAIR-01 | Phase 2 | Pending |
| PAIR-02 | Phase 2 | Pending |
| PAIR-03 | Phase 2 | Pending |
| PAIR-04 | Phase 2 | Pending |
| ANLZ-01 | Phase 3 | Complete |
| ANLZ-02 | Phase 3 | Complete |
| ANLZ-03 | Phase 3 | Complete |
| ANLZ-04 | Phase 3 | Pending |
| ANLZ-05 | Phase 3 | Pending |
| ANLZ-06 | Phase 3 | Complete |
| CHART-01 | Phase 3 | Complete |
| CHART-02 | Phase 3 | Complete |
| CHART-03 | Phase 3 | Complete |
| DECK-01 | Phase 3 | Complete |
| DECK-02 | Phase 3 | Complete |
| DECK-03 | Phase 3 | Complete |
| DECK-04 | Phase 3 | Complete |
| DECK-05 | Phase 3 | Pending |
| DECK-06 | Phase 3 | Complete |
| ERR-05 | Phase 3 | Complete |

**Coverage:**
- v1 requirements: 62 total
- Mapped to phases: 62
- Unmapped: 0 ✓
  - Phase 1: 35 requirements (BUDG×5, GUARD×5, CTRL×6, SEC×4, HPA×6, INT×5, ERR×4)
  - Phase 2: 11 requirements (GPU×7, PAIR×4)
  - Phase 3: 16 requirements (ANLZ×6, CHART×3, DECK×6, ERR-05×1)

---
*Requirements defined: 2026-09-23*
*Last updated: 2026-09-23 — phase traceability added (Phase 1/2/3 from ROADMAP.md)*
