# Presentation visual audit - production-only handoff

**Scope:** every pre-rendered image that could have been attached to the Claude Design handoff
for the 16-slide SRECon26 talk. **Outcome:** the production story uses **zero pre-rendered
evidence images**; all 16 slides are native shapes and text built from
`artifact-catalog.json#/slides` and `#/published_sources`. Canonical prompt:
`artifacts/evidence-pack/CLAUDE-DESIGN-PROMPT.md`; companion: `docs/claude-design-brief.md`.

## Criteria

| Criterion | Question asked |
| --- | --- |
| Production relevance | Does the image teach something an SRE running production inference can act on? |
| 15 s readability | Can the audience read the point in one 15-second auto-advance slide from the back row? |
| Provenance burden | How much on-slide scoping (sample size, projection, synthetic source) does honest use require? |
| Local framing | Does the image frame the talk around the speaker's own local or single-VM rig instead of production patterns? |

## Verdicts (10 candidates)

| # | Candidate | Verdict | Reasons |
| --- | --- | --- | --- |
| 1 | `artifacts/evidence-pack/gpu-capacity-envelope.svg` | **appendix-only** | Relevance: method is useful, but numbers come from a 1.5B model on one consumer GPU. Readability: three panels exceed a 15 s read. Provenance: needs a `PROJECTED` caveat plus Little's Law proxy explanation. Framing: local single-VM. Replaced on-slide by the slide 5 native worked example. Kept for Q&A. |
| 2 | `artifacts/evidence-pack/gpu-latency-by-concurrency.svg` | **drop** | Relevance: p50/p95 at n = 4 and n = 32 does not generalize. Readability: dense multi-series chart. Provenance: small samples need heavy caveats. Framing: local single-VM. Redundant with candidate 4. |
| 3 | `artifacts/evidence-pack/gpu-observed-pressure.svg` | **appendix-only** | Relevance: shows an unsaturated engine (waiting 0, KV under 1 %), so it teaches no production failure mode. Readability: three snapshot panels. Provenance: sampled snapshots, no causal reading. Framing: local single-VM. Kept only to answer "was the run saturated?" in Q&A. |
| 4 | `artifacts/evidence-pack/gpu-ttft-tpot-contrast.svg` | **appendix-only** | Relevance: TTFT/TPOT split is the right idea, but a toy-scale ratio distracts from it. Readability: acceptable, yet the point lands better as the slide 4 native request-anatomy diagram. Provenance: n = 4 / n = 32 and cause-free wording required. Framing: local single-VM. Kept for Q&A. |
| 5 | `artifacts/evidence-pack/local-hpa-signal-plumbing.svg` | **drop** | Relevance: mock workload with a synthetic KV source shows plumbing, not production behavior. Readability: three arm panels plus a control window. Provenance: synthetic source and negative controls need explanation. Framing: local cluster. Replaced by the slide 14 native engine-metrics pipeline. |
| 6 | `artifacts/evidence-pack/two-node-canary-startup.svg` | **excluded / drop** | Relevance: historical rental-startup attempt counts, not inference behavior. Readability: requires run history to interpret. Provenance: attempt counts, not instances. Framing: speaker's rental infrastructure. Catalog role already `excluded`. |
| 7 | `artifacts/presentation/local-signal-independence.png` | **drop** | Relevance: older-storyboard local HPA claim. Readability: raster with small embedded text. Provenance: bound to the older deck manifest, not the current catalog. Framing: local cluster. |
| 8 | `artifacts/presentation/queue-cpu-control.png` | **drop** | Relevance: local queue-vs-CPU control, not production. Readability: raster, embedded labels. Provenance: older deck manifest. Framing: local cluster. |
| 9 | `artifacts/presentation/evidence-boundary.png` | **drop** | Relevance: describes the speaker's evidence boundaries, not inference engineering. Readability: text-heavy raster. Provenance: older storyboard. Framing: centers the talk on the rig instead of the stack. |
| 10 | `artifacts/presentation/qa-contact-sheet.jpg` and `qa-slide-*.jpg` set | **drop** | QA screenshots of the older deck, not content. No production relevance; unreadable as a slide; provenance is the older PPTX; local framing throughout. |

`two-node-vllm-metric-path.svg` is not a candidate: the catalog lists it `unavailable` and it
is not rendered.

### Catalog alignment

`scripts/build_evidence_pack.py` `CHART_PRESENTATION_ROLES` records candidates 1, 3, and 4 as
`appendix-only`; candidates 2, 5, and both two-node charts as `excluded`. This matches the audit.
The prompt tells Claude Design to ignore `charts[]` entirely, so every SVG stays off slides.

## UI Pro Max application

UI/UX Pro Max design-system search returned glassmorphism and Inter, but both conflict with
projector readability, existing no-gradient rule, and Google Slides font portability. Applied
project-specific synthesis instead:

- high-contrast technical editorial, white canvas, no blur/transparency/decorative depth;
- Arial Bold / Arial / Courier New, all Google Slides-safe;
- direct-labeled horizontal bars for comparisons; no radar charts or detached legends;
- minimum 4.5:1 text contrast, non-color cues, accessibility descriptions and data-table
  alternatives in speaker notes;
- one transform/opacity reveal, ease-out entry, plus reduced-motion/static fallback.

## Upload set

Upload to Claude Design exactly:

```text
artifacts/evidence-pack/CLAUDE-DESIGN-PROMPT.md
artifacts/evidence-pack/artifact-catalog.json
```

Excluded from upload:

- every `artifacts/evidence-pack/*.svg` (analytic appendix or excluded);
- `artifacts/evidence-pack/evidence-summary.json` and `local-hpa-verdict.json`;
- `artifacts/presentation/srecon26-llm-hpa-evidence-poc.pptx` and `.pdf` (older deck);
- `artifacts/presentation/*.png`, `qa-contact-sheet.jpg`, `qa-slide-*.jpg`, and every
  `artifacts/presentation/qa-*/` folder.

## Published sources

Catalog `published_sources` entries remain only external sources. Numeric figures are redrawn natively
with its full URL in speaker notes; no vendor chart, figure, or logo is copied:

- `vllm-fp8` - <https://docs.vllm.ai/en/v0.21.0/features/quantization/fp8/>
- `vllm-turboquant` - <https://vllm.ai/blog/2026-05-11-turboquant>
- `cache-routing-preprint` - <https://arxiv.org/html/2602.04900>
- `google-gke-hpa` - <https://cloud.google.com/blog/products/containers-kubernetes/tuning-the-gke-hpa-to-run-inference-on-gpus>
- `kserve-wva` - <https://kserve.github.io/website/docs/model-serving/generative-inference/llmisvc/autoscaling/llmisvc-autoscaling>
- `llmd-slo-aware` - <https://llm-d.ai/docs/dev/architecture/advanced/autoscaling/slo-aware-keda>
- `vllm-x-omni` - <https://x.com/vllm_project/status/2101665925137379695>
- `vllm-x-v030` - <https://x.com/vllm_project/status/2102593516740411733>

## X research

Streamable Chrome Bridge verified both direct posts from official `@vllm_project` account.
`vllm-x-omni` supports slides 9-10; `vllm-x-v030` supports slide 11. Posts prove feature
availability only. No X performance number is used.

## Operational note (not projected content)

- Projected slide text and spoken lines must never contain: failed, invalidated, limitation,
  limited resources, not yet. This list appears only here; the prompt and brief enforce it by
  neutral present-tense scope language instead of naming the words.
- Removed from the handoff: the OURS/local evidence sections, the chart SHA-256 gate, the
  local and two-node pair scope notes, and every local chart speaker line.
- The catalog and summary hashes change on every rebuild; the prompt instructs reading the
  catalog fresh and pins no catalog or summary hash.
- No PPTX or PDF is generated at this stage.
