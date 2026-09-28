# Results output specification

## Purpose

One reproducible handoff for a 16-slide SRECon lightning talk on layered production inference
engineering. Audience: SREs and inference-platform engineers. Build is offline and does not
create provider resources or a deck.

Canonical machine-readable entry point:

```text
artifacts/evidence-pack/artifact-catalog.json
```

Numeric source of truth:

```text
artifacts/evidence-pack/evidence-summary.json
```

## Populate current results

```bash
python3 scripts/analyze_evidence.py --output /var/tmp/srecon26-verdict.json
python3 scripts/build_evidence_pack.py
```

After a complete two-node metric-path run:

```bash
python3 scripts/build_evidence_pack.py \
  --two-node-run artifacts/live-two-node-<run-id>
```

Builder stages every output before replacement and publishes `evidence-summary.json` last.
A validation error leaves the previous pack unchanged. Catalog and summary bytes (and hashes)
change on every rebuild; handoff docs never pin them.

## Published tree

```text
artifacts/evidence-pack/
├── artifact-catalog.json
├── evidence-summary.json
├── local-hpa-verdict.json
├── gpu-latency-by-concurrency.svg
├── gpu-ttft-tpot-contrast.svg
├── gpu-observed-pressure.svg
├── gpu-capacity-envelope.svg
├── local-hpa-signal-plumbing.svg
├── two-node-canary-startup.svg
├── two-node-vllm-metric-path.svg       # only after complete measured run
└── CLAUDE-DESIGN-PROMPT.md
```

`evidence-summary.json#/artifact_catalog/sha256` binds exact catalog bytes. Catalog binds each
available chart and retained local verdict by SHA-256. Optional unmeasured charts remain listed
with `status: unavailable` and `sha256: null`; no placeholder is emitted.

## Graph paths (analytic appendix only)

Generated SVGs are an analytic appendix for Q&A and review. None is a slide input and none is
attached to Claude Design. Catalog `charts[].presentation_role` records each role; the stricter
presentation verdicts live in `docs/presentation-visual-audit.md`.

| Path | Evidence class | Catalog role | Audit verdict | Current meaning |
| --- | --- | --- | --- | --- |
| `artifacts/evidence-pack/gpu-capacity-envelope.svg` | Standalone GPU, projected | appendix-only | appendix-only | Little's Law proxy from c32 p95 and 70 % headroom token budget; PROJECTED, not measured at target RPS |
| `artifacts/evidence-pack/gpu-observed-pressure.svg` | Standalone GPU | appendix-only | appendix-only | Waiting/running/KV snapshots; no causal attribution |
| `artifacts/evidence-pack/gpu-ttft-tpot-contrast.svg` | Standalone GPU | appendix-only | appendix-only | TTFT versus TPOT at c4/c32; descriptive |
| `artifacts/evidence-pack/gpu-latency-by-concurrency.svg` | Standalone GPU | excluded | drop | p50/p95 latency; small samples |
| `artifacts/evidence-pack/local-hpa-signal-plumbing.svg` | Local synthetic KV | excluded | drop | Independent CPU, queue, synthetic-KV plumbing |
| `artifacts/evidence-pack/two-node-canary-startup.svg` | Historical subset | excluded | excluded / drop | Attempt counts, not instances |
| `artifacts/evidence-pack/two-node-vllm-metric-path.svg` | Real GPU Kubernetes | excluded | not rendered | Catalog `status: unavailable` |

Capacity appendix values live at `evidence-summary.json#/capacity_projection`; the chart is
bound by `capacity_projection.rendered_sha256` and catalog `charts[]` for the current build.

## Metric paths

| Data | Path or JSON pointer |
| --- | --- |
| Standalone latency | `artifacts/evidence-pack/evidence-summary.json#/gpu_measurements` |
| Standalone pressure | `artifacts/evidence-pack/evidence-summary.json#/standalone_gpu_verification/prometheus_observed` |
| GPU chart values and ratios | `artifacts/evidence-pack/evidence-summary.json#/gpu_chart_metrics` |
| Projected capacity budget | `artifacts/evidence-pack/evidence-summary.json#/capacity_projection` |
| Published external sources | `artifacts/evidence-pack/artifact-catalog.json#/published_sources` |
| Local HPA claim | `artifacts/evidence-pack/evidence-summary.json#/local_hpa_verdict` |
| Full retained local verdict | `artifacts/evidence-pack/local-hpa-verdict.json` |
| Historical two-node attempts | `artifacts/evidence-pack/evidence-summary.json#/two_node_canary` |
| Measured two-node path | `artifacts/evidence-pack/evidence-summary.json#/two_node_metric_path` |
| Latest live attempt | `artifacts/evidence-pack/artifact-catalog.json#/results/latest_live_attempt` |
| Claim boundaries | `artifacts/evidence-pack/evidence-summary.json#/boundaries` |
| Slide-to-chart mapping | `artifacts/evidence-pack/artifact-catalog.json#/slides` |

Raw standalone sources:

```text
artifacts/runs/inference-infer20260923184725/manual-benchmark/
artifacts/runs/inference-infer20260923184725/post-run/verdict.json
```

Complete two-node input must retain:

```text
artifacts/live-two-node-<run-id>/run-manifest.json
artifacts/live-two-node-<run-id>/server-evidence/pressure-before-*
artifacts/live-two-node-<run-id>/server-evidence/pressure-during-*
artifacts/live-two-node-<run-id>/server-evidence/pressure-after-*
```

## Paired HPA comparison contract

Status remains `unavailable` until exactly three complete blocks in `AB, BA, AB` order pass
control, provenance, integrity, and safety gates. Fewer blocks are exploratory and cannot support
a comparative chart or conclusion.

Primary outcome, fixed before measurement:

```text
pressure-window p95 TTFT (ms)
contrast: queue/KV-aware HPA minus CPU-only HPA within each paired block
```

Secondary outcomes: p50 TTFT, queue depth, replica readiness delay, TPOT, end-to-end latency,
request errors, CPU, GPU utilization, and KV-cache utilization.

Reserved output paths after valid ingestion:

```text
artifacts/comparisons/<comparison-id>/analysis/summary.json
artifacts/comparisons/<comparison-id>/analysis/paired-blocks.csv
artifacts/comparisons/<comparison-id>/charts/paired-p95-ttft.svg
artifacts/comparisons/<comparison-id>/charts/queue-readiness-overlay.svg
```

Current code classifies paired evidence but does not ingest live paired runs or render these
charts. Therefore these paths are contract-only, not populated placeholders.

## Layered storyboard

`artifact-catalog.json#/slides` is the source registry: 16 production-only entries, each with
exact `headline`, `pill`, `charts`, and `source_ids`. Every `charts` list is empty. Design docs
must match headlines, pills, and sources exactly.

| Slides | Layer | Pill class | Sources |
| --- | --- | --- | --- |
| 1-2 | Thesis, layer stack | SYNTHESIS | none |
| 3 | Workload contract card | SYNTHESIS | none |
| 4 | Request anatomy, TTFT/TPOT, E2E formula | PATTERN | none |
| 5 | Capacity math plus tagged EXAMPLE (20 RPS, 400 output tokens, TTFT 0.5 s, TPOT 25 ms -> ~210 in flight, ~11.4k tok/s target; per-replica denominator left open) | SYNTHESIS | none |
| 6-8 | Precision and KV memory | PUBLISHED | `vllm-fp8`, `vllm-turboquant` |
| 9-10 | Phase-aware scheduling and prefix KV reuse | PATTERN | `vllm-x-omni`; feature availability, no numbers |
| 11 | EAGLE3-style drafts and adaptive verification | PATTERN | `vllm-x-v030`; feature availability, no numbers |
| 12 | Cache-aware routing | PUBLISHED | `cache-routing-preprint` |
| 13 | HPA metric choice; result labelled TGI mean time/token, not TPOT | PUBLISHED | `google-gke-hpa` |
| 14 | Engine-metrics pipeline: vLLM waiting/KV -> Prometheus -> adapter/KEDA -> HPA -> replicas -> readiness -> usable capacity; CPU off to the side | PATTERN | none |
| 15 | SLO-aware control | PUBLISHED | `kserve-wva`, `llmd-slo-aware` |
| 16 | Metric per layer; fix the lowest saturated layer first | SYNTHESIS | none |

On-slide pills use only PUBLISHED, PATTERN, or SYNTHESIS and equal the catalog `pill` string.
Slides carry zero pre-rendered evidence images. Published figures are redrawn natively with
full URLs in notes, no copied vendor image, and never share an axis with another study.
Streamable Chrome Bridge verified two direct posts from official `@vllm_project` account:
`vllm-x-omni` and `vllm-x-v030`. Catalog stores exact URLs, timestamps, claims, and scope.
No X performance number is used.

## Claude Design handoff

Attach exactly:

```text
artifacts/evidence-pack/CLAUDE-DESIGN-PROMPT.md
artifacts/evidence-pack/artifact-catalog.json
```

Tracked companion: `docs/claude-design-brief.md`. Visual audit and upload exclusions:
`docs/presentation-visual-audit.md`.

Not attached: every `artifacts/evidence-pack/*.svg` (analytic appendix or excluded),
`evidence-summary.json`, `local-hpa-verdict.json`, the older
`artifacts/presentation/*.pptx` / `*.pdf` deck, older presentation PNGs, and every QA
screenshot and `qa-*` folder.

Claude Design reads the catalog fresh (no pinned catalog or summary hash), uses only
`slides[]` and `published_sources`, and keeps source IDs plus full URLs in speaker notes.
No slide may imply paired A/B performance, GPU-Kubernetes metric-path completion, causality,
a reproduced published result, an ideal configuration, or a specific GPU recommendation.

Google Slides target: 16:9, exactly 16 slides, Arial-compatible text, basic native shapes, no
embedded images, no external relationships, and post-import inspection of all slides.
QA checklist: `docs/google-slides-import-qa.md`.

## Scope exclusions

- No PPTX or PDF generation in this output stage.
- No live provider action.
- No fabricated or illustrative series presented as measured.
- No performance conclusion from standalone GPU or local synthetic evidence.