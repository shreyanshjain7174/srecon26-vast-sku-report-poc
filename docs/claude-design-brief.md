# Claude Design brief - SRECon26 layered production inference talk

**Canonical prompt:** `artifacts/evidence-pack/CLAUDE-DESIGN-PROMPT.md` - paste it into Claude
Design and attach `artifacts/evidence-pack/artifact-catalog.json`. The prompt is a content
contract; `artifact-catalog.json#/slides` wins over it and over this brief for headlines, pills,
and sources. No catalog or summary hash is pinned.

**Claude Design owns all design decisions** - visual system, layout, typography, palette,
native diagrams, chart treatment, and transitions.

## Content contract

- Exactly **16 slides, 16:9, 15 s each = 4:00**; slides plus speaker notes.
- Audience: SREs and inference/platform engineers running production LLM inference.
- Thesis: inference is a stack (workload, precision, KV cache, engine, routing, autoscale,
  SLO); instrument every layer and fix the lowest saturated layer first.
- Numbers: only catalog `published_sources` figures, catalog `measured_results` figures
  (our 8x RTX 4090 TP8 run, `MEASURED` slides 11-13 and 15), and the slide 5 `EXAMPLE`
  arithmetic. PUBLISHED and MEASURED never share an axis or a slide; no shared axis across
  studies. MEASURED slides show results only: no "smoke test", "not a scale test",
  "does not show" lists, or request success counts.
- One content goal and one <= 15 s spoken line per slide; speaker notes carry setup, source
  IDs, and full URLs for published figures.
- Rights: zero copied vendor images or copyrighted media. Portability: imports cleanly into
  Google Slides.
- Required original comparisons: slide 6 (three pairs, each with a BF16 baseline: `1x` vs
  `0.5x` memory, `1x` vs `2x` KV, `~17 s` vs `~1.3 s` P99 TTFT); slide 8 (`500 ms` vs `80 ms`,
  to scale); slide 9 (`<~0.4 s` vs almost `<~0.3 s` TGI mean time/token); slide 12 (latency
  multiples, throughput on its own row); slide 15 (BF16 vs FP8 at concurrency 8).
- Brief and prompt are instructions only; no PPTX/PDF is generated.

## Slides (match `artifact-catalog.json#/slides`)

| # | Headline | Pill | `source_ids` | Content goal |
| --- | --- | --- | --- | --- |
| 1 | Most production inference sucks. | SYNTHESIS - talk thesis | - | Replica knob does not quiet queue/KV/token/SLO alarms: wrong layer |
| 2 | Wrong layer. Wrong metric. | SYNTHESIS - talk thesis | - | Seven-layer stack; CPU % misses KV and engine pressure |
| 3 | Start with workload math. | SYNTHESIS - workload contract | - | RPS, tokens in/out, TTFT and TPOT SLOs start every number |
| 4 | Prefill sets TTFT. Decode sets TPOT. | PATTERN - request anatomy | - | Queue, prefill, decode; `E2E ~= TTFT + (output tokens - 1) x TPOT` |
| 5 | Capacity is a token budget. | SYNTHESIS - worked example | - | Capacity identities plus the `EXAMPLE` arithmetic |
| 6 | FP8 frees memory for more requests. | PUBLISHED - vLLM docs and blog | `vllm-fp8`, `vllm-turboquant` | 0.5x memory, 2x KV, ~17 s -> ~1.3 s burst P99 TTFT, each with baseline |
| 7 | Three engine levers. | PATTERN - serving techniques | `vllm-x-omni`, `vllm-x-v030` | Phase-aware scheduling, prefix reuse, speculative decoding |
| 8 | Route to the cache. | PUBLISHED - arXiv preprint, 8xA100 | `cache-routing-preprint` | Avg TTFT ~500 ms random vs ~80 ms cache-aware |
| 9 | HPA watches the wrong thing. | PUBLISHED - Google Cloud blog, L4 GPU | `google-gke-hpa` | GPU util overprovisions; queue 25 / batch 50 targets; `TGI mean time/token` |
| 10 | Scale on engine signals. | PATTERN - metrics pipeline | - | vLLM metrics -> Prometheus -> adapter/KEDA -> HPA -> ready capacity |
| 11 | Qwen3.8-27B on 8x RTX 4090. | MEASURED - 8x RTX 4090, one host | `tp8-bf16` | Setup card plus c1/c8 results table |
| 12 | 8 concurrent: first token 7x slower. | MEASURED - 8x RTX 4090, one host | `tp8-bf16` | TTFT 7.3x, TPOT 1.8x; throughput 3.1x on its own row |
| 13 | Empty queue. Latency still rose. | MEASURED - 8x RTX 4090, one host | `tp8-bf16` | Queue 0, KV ~2 %, p50 TTFT 3.9 s at 8 concurrent |
| 14 | Control on the SLO. | PUBLISHED - KServe and llm-d docs | `kserve-wva`, `llmd-slo-aware` | Loop on queue, KV, saturation, P90 TTFT/TPOT vs SLO |
| 15 | BF16 vs FP8, same host. | MEASURED - 8x RTX 4090, one host | `tp8-precision` | BF16 vs FP8 (and FP8 KV) at concurrency 8 |
| 16 | Instrument every layer. | SYNTHESIS - bottom-up rule | - | One metric per layer; fix the lowest saturated layer first |

## Sources

- Published URLs (accessed 2026-09-25):
  - <https://docs.vllm.ai/en/v0.21.0/features/quantization/fp8/>
  - <https://vllm.ai/blog/2026-05-11-turboquant>
  - <https://arxiv.org/html/2602.04900>
  - <https://cloud.google.com/blog/products/containers-kubernetes/tuning-the-gke-hpa-to-run-inference-on-gpus>
  - <https://kserve.github.io/website/docs/model-serving/generative-inference/llmisvc/autoscaling/llmisvc-autoscaling>
  - <https://llm-d.ai/docs/dev/architecture/advanced/autoscaling/slo-aware-keda>
  - <https://x.com/vllm_project/status/2101665925137379695>
  - <https://x.com/vllm_project/status/2102593516740411733>
- X provenance: Streamable Chrome Bridge verified both direct posts from official
  `@vllm_project` account. Posts support feature availability only; no X performance number is
  used.
