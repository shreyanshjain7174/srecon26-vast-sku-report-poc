# Claude Design brief — SRECon26 LLM HPA evidence talk

## Deliverable

Design exactly **16 slides**, **16:9**, for a **four-minute lightning talk**.
Advance automatically or rehearse at **15 seconds per slide**. This is an
evidence-first conference talk, not a product pitch: every slide gets one
idea, one dominant visual, and at most a short headline plus a status label.
Make the final slide deck visually led and legible from the back of a large
room.

### Non-negotiable evidence boundary

The verified result is narrow:

- Local Kubernetes HPA plumbing independently scaled a mock workload from one
  ready replica to two for **CPU**, **queue depth**, and **synthetic KV
  pressure**.
- The current paid Vast GPU canary is **in progress**. Until its final
  evidence bundle exists, it contributes no claim, metric, or screenshot.
  It may end as a verified GPU path, a verified limitation, or a safe
  pre-payment failure.

Never visually imply that the GPU, vLLM, KServe, llm-d, user latency, or a
CPU-versus-aware-HPA comparison was measured. If a run asset or claim gate is
missing, show the limitation plainly; do not invent a proxy graphic that looks
like a result.

## USENIX and rights constraints

- Use a 16:9 layout. Keep all text, labels, and chart detail large enough for
  a large-room projection; do not overcrowd slides or use fine-detail charts.
- The deck may be publicly published with the talk. Use **only original,
  generated-by-the-presenter visual elements, native shapes, and verified run
  artifacts**. No stock photos, screenshots from other sites, third-party
  logos, icon packs, film/TV stills, GIFs, audio, or video.
- Do not use a Hollywood frame, character, costume, recognizable set, studio
  mark, dialogue, or quote. This avoids an unlicensed meme in a deck USENIX
  may publish. The opening joke must be an original visual metaphor.
- Preserve provenance on evidence slides with a small but readable footer:
  `LOCAL • synthetic`, `LIVE • limitation`, `BACKGROUND • unmeasured`, or
  `PLAN • future work`. Do not put hashes, raw IDs, or URLs on the projected
  slide; place them in speaker notes or a final references handout.
- Final preflight: speaker/title match the accepted program information;
  verify the then-current SRECon26 presenter instructions, consent workflow,
  delivery format, and deadline before submission.

The above applies the available USENIX presenter guidance: 16:9, large-room
legibility, no overcrowding, and permission/compliance for every copyrighted
asset (including Creative Commons material). The no-external-asset rule is a
deliberately stricter implementation for this talk.

## Visual system

**Mood:** incident-control-room clarity; technical, calm, slightly playful.
Not sci-fi, not a dark terminal theme, not a cloud-vendor demo.

| Token | Direction |
| --- | --- |
| Canvas | Near-white `#F7F8FA` with generous negative space |
| Ink | Near-black `#172033` for all essential text |
| Verified local | Teal `#007C78` |
| Live limitation / stop | Vermilion `#C9472C` |
| Background / plan | Slate `#5C6B7A` |
| Callout / measured number | Gold `#B7791F` (never alone; pair with label) |
| Type | Aptos/Arial or another installed sans serif; 48–60 pt headlines, 26–32 pt labels, 18 pt minimum chart/footer text |
| Shapes | Rounded rectangles, bold lines, dot/ring markers, simplified gauges; no gradients, shadows, 3D, decorative stripes, or dense tables |

Use the same tiny provenance pill at lower left and a slide number at lower
right. Keep a 0.5-inch safe margin. Build the title and closing slide with
native shapes so they survive PDF and Google Slides conversion. Do not rely on
animation to communicate meaning.

### Opening meme: copyright-safe resolution

Create an **original, two-panel “control-room” meme**. Left: a calm green CPU
dial labelled `CPU: fine`. Right: a long queue of simple dots presses against
a red `WAITING` line while a tiny replica button is still dim. Caption:
`The dashboard says “fine.” The queue disagrees.`

It may evoke the familiar Hollywood control-room *trope*, but must be drawn
from first principles: no movie title, actor, quote, frame, character, or
recognizable composition. This is the safe substitute for a famous Hollywood
meme, not a recreation of one.

## Data contract for the designer

Render a numeric visual only when its file is provided by the evidence build.
Use these placeholders, not guessed filenames:

```text
{{asset.local_signal_independence_chart}}
{{asset.cpu_arm_chart}}
{{asset.queue_cpu_control_chart}}
{{asset.kv_arm_chart}}
{{asset.live_lifecycle_timeline}}
{{asset.evidence_boundary_card}}
{{asset.claim_ledger}}
{{asset.integrity_chain_diagram}}
```

For every supplied chart, retain its source-provided title, units, time window,
and provenance. Do not smooth, interpolate, recolor semantic statuses, or
compare series from different provenance. If an asset is absent, replace it
with the stated limitation design—not an empty chart.

### Verified values allowed in copy or a native metric tile

| Evidence | Verified value | Allowed interpretation |
| --- | --- | --- |
| CPU local arm | 98-second negative control; CPU max 38.157475m; scaled CPU 862.799833m; desired/ready `2/2` | CPU signal independently drove a local 1→2 transition |
| Queue local arm | 96-second negative control; CPU max 16.456944m; queue reached 4.0; desired/ready `2/2` | Queue signal independently drove a local 1→2 transition |
| Synthetic-KV local arm | 96-second negative control; synthetic KV 0.2→1.0; CPU 14.795593m; desired/ready `2/2` | Synthetic KV signal independently drove a local 1→2 transition |
| Current live run | `PENDING` until final evidence pack is generated | No live GPU, vLLM, latency, or HPA claim is allowed yet |

Do not display the live offer/SKU as proof of GPU execution. Do not show a
latency, TTFT, throughput, GPU-utilization, KV-cache, or comparative-result
number unless a new verified run artifact permits it.

## 16-slide storyboard

Each `Talk line` is the entire spoken payload for its 15 seconds. Keep visible
copy to the stated headline, status pill, and any essential chart labels.

| # | Headline / visible copy | Dominant visual and build direction | Talk line | Status / data slot |
| --- | --- | --- | --- | --- |
| 1 | `The dashboard says “fine.” The queue disagrees.` | Original two-panel control-room meme above; title/subtitle only at bottom: `Why Kubernetes HPA can miss LLM demand` and speaker name | “CPU can look calm while waiting work is already telling us to scale.” | `SCOPE` |
| 2 | `CPU is one signal.` | Three large vertical signal cards: CPU, queue, KV pressure. CPU fades; queue/KV illuminate first. | “For inference, waiting work and cache pressure can matter before CPU becomes useful.” | `HYPOTHESIS` |
| 3 | `Conditional, not universal.` | One horizontal boundary line: `CPU can work` on one side; `can be late` on the other, with a small “depends on workload” hinge. | “This is not ‘CPU always fails’; it is a measurable, conditional question.” | `HYPOTHESIS` |
| 4 | `Two proof levels.` | Split-screen staircase: teal Local Signal Plumbing reaches a check; slate Live GPU Metric Path is intentionally unfinished. | “Local proof and live GPU proof have different bars. We do not merge them.” | `METHOD` |
| 5 | `Three signals. Three local transitions.` | Use `{{asset.local_signal_independence_chart}}` full-width. If absent, use three native 1→2 arrows labelled CPU, queue, synthetic KV—clearly marked as a schematic. | “All three isolated local arms reached the same recorded one-to-two transition.” | `LOCAL • synthetic` |
| 6 | `CPU: 1 → 2` | Use `{{asset.cpu_arm_chart}}`; call out `98 s control` and `2/2 ready` only if asset supports it. | “The CPU arm is the control: resource pressure alone scaled locally.” | `LOCAL • synthetic` |
| 7 | `Queue: 1 → 2 while CPU stayed low` | Use `{{asset.queue_cpu_control_chart}}`; one high-contrast callout: `16.456944m < 48m ceiling`. | “Waiting work triggered the transition while CPU remained below its independence ceiling.” | `LOCAL • synthetic` |
| 8 | `Synthetic KV: 1 → 2` | Use `{{asset.kv_arm_chart}}`; tag it prominently `synthetic ≠ GPU KV cache`. | “The custom metric path also worked for synthetic cache pressure—nothing more.” | `LOCAL • synthetic` |
| 9 | `Evidence, not vibes.` | Use `{{asset.integrity_chain_diagram}}`: capture → checksum → external anchor. Use three simple nodes, not a technical hash dump. | “Raw metrics, HPA events, and timestamps are tied to selected, anchored evidence bundles.” | `INTEGRITY` |
| 10 | `Paid work has a harder rule: fail safe.` | Five-step lifecycle ribbon: reserve → arm independent guard → create → verify → exact teardown. | “A paid run may fail, but it must not become an unbounded bill or unsupported claim.” | `LIVE • protocol` |
| 11 | `Live evidence: pending` | Use `{{asset.live_lifecycle_timeline}}` only after a final evidence bundle exists; otherwise render a slate waiting boundary with no numeric claim. | “The run is either measured to completion or presented as a limitation. There is no middle state.” | `LIVE • pending` |
| 12 | `What the GPU path must prove` | Use `{{asset.evidence_boundary_card}}`, five locked gates: GPU/CUDA, vLLM request, latency, queue/KV, paired A/B. | “A rentable GPU is not a result. These are the proof points that would make it one.” | `LIVE • proof gate` |
| 13 | `Claim ledger` | Use `{{asset.claim_ledger}}`: teal local rows; slate pending live rows until evidence arrives. | “This ledger is the talk’s discipline: speak only to evidence that exists.” | `CLAIM GATE` |
| 14 | `What must exist before a comparison` | Five locked gates in a row: observed GPU/CUDA → real request → metrics path → HPA events → three paired blocks. | “A comparison is a future experiment, not a conclusion waiting for a chart.” | `PLAN • future work` |
| 15 | `The operational takeaway` | Return to three signal cards; add a simple rule in center: `Measure the pressure users feel.` | “Choose scaling signals that represent demand, then earn the claim with evidence.” | `TAKEAWAY` |
| 16 | `Measure the signal. Earn the claim.` | Minimal closing: teal check for local plumbing; red boundary for live performance. Add speaker contact only. | “Today: a verified local signal path and a safe live boundary. Questions?” | `CLOSE` |

## Speaker-note and build requirements

- Put the 15-second talk line in the first sentence of each slide’s notes.
  Then add a one-sentence evidence boundary and the source artifact path/hash
  for the presenter—not the audience.
- Create an explicit accessibility description for every original diagram and
  every run chart. Never convey valid/invalid status by color alone; combine
  color with a check, boundary bar, or text label.
- If importing to Google Slides, use normal text boxes and basic shapes; avoid
  grouped shapes, SmartArt, external links/media, motion paths, or chart
  features that silently convert. Inspect all 16 slides after import.
- Export a local PDF before delivery. Confirm 16 pages, readable fonts, no
  clipping, no overlap, no missing chart, and no copyrighted/external asset.

## Sources for this brief

- `artifacts/presentation/verdict.json` — claim gates and local-arm values.
- `artifacts/live-two-node-*/run-manifest.json` — current live-run state; use
  only after terminal evidence is present.
- `docs/superpowers/specs/2026-09-23-srecon26-llm-hpa-poc-design.md` — scope,
  experiment contract, and limitations.
- USENIX presenter guidance: <https://www.usenix.org/conference/srecon26emea/instructions-presenters>.
