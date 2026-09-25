# Copilot handoff: SRECon26 Vast GPU autoscaling PoC

Last verified: **2026-09-25 06:05 UTC**

This file is the continuation brief. Treat the repository and live provider state as authoritative. Do not launch another paid instance until the active-controller check below is terminal and Vast inventory is still empty.

## Objective

Build evidence for a four-minute SRECon lightning talk titled:

> Why Your Kubernetes Autoscaler Fails LLM Inference (And What vLLM/KServe/llm-d Do Instead)

The sharp claim is:

> CPU-only Kubernetes HPA can miss LLM demand because queue/KV pressure and TTFT worsen before CPU signals enough load.

Completion requires:

1. local Kubernetes evidence that CPU, queue, and KV signals can trigger independently;
2. a real Vast GPU/vLLM/Kubernetes metric-path canary;
3. a controlled CPU-HPA versus queue/KV-aware HPA comparison if budget remains;
4. graphs and evidence that support only measured claims;
5. a copyright-safe, 16:9, 16-slide deck or design handoff for 15 seconds per slide.

This is a conference PoC, not a production platform. Do not expand into multi-region, sharding, KServe/llm-d production deployment, or frontier-model benchmarking.

## Repository state

- Repository: `/Users/sunny/Documents/Codex/2026-09-23/srecon26-vast-sku-report-poc`
- Branch: `fix/two-node-metric-contract`
- Remote branch is current through commit `ad492a9`.
- Pull request: <https://github.com/shreyanshjain7174/srecon26-vast-sku-report-poc/pull/4>
- Recent commits:
  - `ad492a9 fix: tolerate volatile Vast offer ids`
  - `f13e194 fix: coalesce paired Azure guard heartbeats`
  - `aca33b4 fix: wait for Azure guard command stdout`
  - `f14f199 docs: add copyright-safe conference design brief`
- Commit rules: use a separate branch, `git commit -s -m`, signed commit, no Codex coauthor.
- Preserve existing user-owned worktree state:
  - modified `.gitignore`
  - untracked `docs/superpowers/plans/2026-09-24-independent-azure-guard.md`
  - untracked `node_modules/`
- Latest verification: **538 passed, 1 skipped**.
- Latest Semgrep scan: **0 findings** across tracked `src`, `tests`, and `scripts`.

## Verified result 1: local HPA signal independence

Authoritative verdict: `artifacts/presentation/verdict.json`.

All three local arms are `VALID`. Each independently changed desired and ready replicas from 1 to 2 while its non-target controls remained low:

| Arm | Trigger observation | Important controls |
|---|---:|---:|
| CPU | 862.800 millicores | queue 0, synthetic KV 0.2 |
| Queue | 4 waiting requests | CPU 21.988 millicores, synthetic KV 0.2 |
| Synthetic KV | 1.0 occupancy | CPU 14.796 millicores, queue 0 |

Boundaries:

- This proves signal plumbing and independent HPA triggers.
- KV is synthetic in this local experiment.
- This does not prove real vLLM/Kubernetes performance.

Useful files:

- `artifacts/presentation/verdict.json`
- `artifacts/presentation/local-signal-independence.png`
- `artifacts/presentation/queue-cpu-control.png`
- `.planning/phases/01-safety-foundation-and-local-evidence/01-LOCAL-EVIDENCE-ANCHOR.json`

## Verified result 2: real Vast RTX 4090 vLLM measurements

The recovered standalone GPU run is real hardware evidence, but it is **not** a Kubernetes HPA experiment.

- Run: `inference-infer20260923184725`
- Vast instance: `52280205`
- Machine: `15326`
- GPU: NVIDIA GeForce RTX 4090, 24,564 MiB
- Driver: 575.51.03
- CUDA: 12.9
- KVM probe: `kvm`
- Model: `Qwen/Qwen2.5-1.5B-Instruct`
- Model revision: `989aa7980e4cf806f80c7fef2b1adb7bc71aa306`
- vLLM image: `docker.io/vllm/vllm-openai@sha256:df2607b26bdda2875de4832f4d08da0055b4b6e3570347f3a849bcc652771dd6`
- Endpoint exposure: loopback only (`127.0.0.1:28000`)
- HTTP 200 completions retained: 4 at concurrency 4 and 32 at concurrency 32
- Authoritative invoice: **$0.116**
- Exact teardown and three fresh zero-match reads are retained.

Measured values:

| Metric | Concurrency 4 | Concurrency 32 | Change |
|---|---:|---:|---:|
| p50 TTFT | 6.278 ms | 20.991 ms | 3.34x |
| p95 TTFT | 8.860 ms | 51.497 ms | 5.81x |
| p50 end-to-end | 763.096 ms | 821.221 ms | 1.08x |
| p95 end-to-end | 766.955 ms | 851.370 ms | 1.11x |
| p50 TPOT | 5.959 ms | 6.297 ms | 1.06x |
| p95 TPOT | 5.969 ms | 6.324 ms | 1.06x |

Interpretation allowed by evidence: TTFT degraded much faster than per-output-token latency as concurrency increased on the same GPU/model. Do not call this CPU-HPA versus queue-HPA proof.

Evidence:

- `artifacts/runs/inference-infer20260923184725/manual-benchmark/`
- `artifacts/runs/inference-infer20260923184725/remote-evidence/`
- `artifacts/runs/inference-infer20260923184725/journal/journal.ndjson`
- `artifacts/runs/invoice-inference-infer20260923184725.json`
- `artifacts/runs/absence-inference-infer20260923184725.json`
- `artifacts/evidence-pack/evidence-summary.json`
- `artifacts/evidence-pack/gpu-latency-by-concurrency.svg`

Integrity note: `manual-benchmark/SHA256SUMS` contains valid hashes for all 125 retained files, but paths point to the original remote `/var/tmp/srecon26-measured-benchmark/` location. A basename-by-basename verification found **125 present, 0 missing, 0 mismatched**. Normalize only the paths before using standard `shasum -c`; do not change any digest.

The old `pre-anchor/run-manifest.json` remains `FAILED_SAFE` because it was written before the manual benchmark was collected. Do not overwrite history. Create a separate post-run evidence verdict if promoting these standalone GPU measurements.

## Two-node attempts and findings

### Attempt `artifacts/live-two-node-20260925T052000Z`

- Both Vast RTX 4090 VMs reached provider running state.
- Azure Run Command heartbeats consumed minutes before SSH/Kubernetes work.
- No Kubernetes/vLLM output was produced.
- Exact IDs were destroyed.
- Three-read absence and authoritative invoices were retained.
- Combined invoice: **$0.212**.
- Classification: controller/heartbeat latency, not a provider contract fault; no Report click.
- Resulting fix: `f13e194` coalesces heartbeats and sends the two guard updates concurrently.

### Attempt `artifacts/live-two-node-20260925T052709Z`

- Server `52538655`, machine `57783`, RTX 4090: reached `running`, then `exited`.
- Worker `52538659`, machine `41599`, RTX 5090: remained `created` across 17 bounded reads.
- No Kubernetes, CUDA, vLLM, or inference evidence was produced.
- Controller was ended and exact teardown completed.
- Three-read absence exists for both instances.
- Authoritative invoice total: **$0.013**, disk only.
- Vast inventory became empty.
- Report was not confirmed. A later exact-target preflight could not run because teardown had already removed both rows.

### Current attempt `artifacts/live-two-node-20260925T055500Z`

At the last handoff timestamp, the local controller was still finalizing evidence:

- launchd label: `org.srecon26.vast-two-node-20260925l`
- shell PID observed: `16874`
- hard deadline: `2026-09-25T06:35:22.811677Z`
- independent Azure guards were both `ARMED`
- authenticated website Report path had passed preflight
- server instance `52541210`, machine `15326`, RTX 4090
- worker instance `52541213`, machine `8024`, RTX 4090
- frozen server rate: `$0.6948148148/hour`
- created server contract rate: `$0.8148134815/hour`
- controller correctly classified an immediate provider contract mismatch
- website Report was attempted before teardown, but exact-target adapter returned `report adapter rejected exact target`
- report confirmation: **false**; never claim a support ticket was submitted
- both exact Vast instances were destroyed immediately
- current Vast inventory was `[]`
- server and worker three-read absence artifacts already exist
- provider invoices and Azure guard finalization were still pending at `06:01 UTC`

First Copilot action must be read-only:

```bash
cd /Users/sunny/Documents/Codex/2026-09-23/srecon26-vast-sku-report-poc
date -u +%Y-%m-%dT%H:%M:%SZ
launchctl print gui/$(id -u)/org.srecon26.vast-two-node-20260925l 2>&1 | rg 'state =|pid =|runs =|last exit code' || true
jq '{status,report,failure,finalization_errors,provider_finalization,azure_guard_finalization,finished_at,workload_completed}' artifacts/live-two-node-20260925T055500Z/run-manifest.json
/Users/sunny/.local/bin/vastai show instances --raw
```

Do not launch a duplicate while that controller exists. If it is gone and inventory remains empty, preserve the final manifest, invoices, guard evidence, and three-read absence proof.

## Budget state

- Project exposure cap: **$5.00**.
- Vast account credit observed: approximately `$8.49`; the PoC cap is still $5.00.
- Exposure-ledger headroom after reconciling the real GPU run: **$1.482**.
- Ledger: `artifacts/runs/exposure-ledger.json`.
- Reconciliation added for real GPU run:
  - `artifacts/runs/reconciliation-inference-infer20260923184725.json`
  - `artifacts/runs/invoice-inference-infer20260923184725.json`
  - `artifacts/runs/absence-inference-infer20260923184725.json`

Recompute before any paid action:

```bash
PYTHONPATH=src /opt/homebrew/bin/python3 - <<'PY'
from pathlib import Path
from srecon26_poc.budget import ExposureLedger
print(ExposureLedger(Path('artifacts/runs/exposure-ledger.json')).headroom())
PY
```

The two-node runner does not yet reserve a pair in `ExposureLedger`. Do not bypass the $5 aggregate cap. Bound any next pair by frozen rates and the immutable deadline, and retain exact invoices for both IDs.

## Provider and machine policy

Required for every fresh offer:

- `verified=true`
- `rentable=true`
- `vms_enabled=true`
- reliability above 99%
- exact 130 GiB storage query
- fresh offer read immediately before create
- distinct machine IDs
- independent teardown guards armed before create
- authenticated exact website Report path ready before create

Do not use known failed or excluded machines:

`99239, 17545, 150513, 15881, 44906, 57783, 41599, 67809, 15326`

Machine `15326` previously produced valid standalone GPU evidence but is now excluded from the next two-node attempt because its created contract price did not match the freshly frozen offer.

Machine `8024` previously produced a valid guarded standalone inference run and the latest two-node worker contract matched. It may be reused only after a fresh offer read and budget check.

A lower-cost candidate seen during the latest read-only query was verified machine `16592` (RTX 4060 Ti, 16,380 MiB, reliability 99.806%, Quebec). Offer IDs and prices are volatile; never reuse a stale offer ID from this document.

## Independent teardown safety

- Azure resource group: `SRECON26-GUARD-EASTUS-V2-RG`
- Current pair used by the runner:
  - `srecon26-guard-eastus-primary-b`
  - `srecon26-guard-eastus-worker-b`
- Two older guard VMs also exist in the resource group. Do not delete them without confirming ownership; they may still contain historical guard evidence.
- Azure Run Command calls can take 60-150 seconds. The heartbeat fix coalesces calls and updates both guard channels concurrently.
- Never treat provider `running` as SSH, KVM, Kubernetes, vLLM, or metrics proof.
- Never delay exact teardown to obtain a Report confirmation.
- Report only a bounded, exact provider/SKU contract fault. Controller, SSH transport, bootstrap, Kubernetes, or workload failures are not automatically provider faults.

## Presentation outputs

- Existing editable 16-slide deck: `artifacts/presentation/srecon26-lightning-talk.pptx`
- PDF export: `artifacts/presentation/srecon26-lightning-talk.pdf`
- Deck manifest: `artifacts/presentation/DECK-MANIFEST.json`
- Claude Design prompt: `artifacts/evidence-pack/CLAUDE-DESIGN-PROMPT.md`
- Detailed design brief: `docs/claude-design-brief.md`
- Google Slides import QA: `docs/google-slides-import-qa.md`
- Evidence boundary visual: `artifacts/presentation/evidence-boundary.png`

The current deck predates recovery of the standalone GPU measurements and still says vLLM latency was not measured. Update it only after creating a separate verified post-run GPU verdict. Keep the central limitation visible: there is still no completed two-node Kubernetes/vLLM metric path and no CPU-HPA versus queue/KV-aware A/B.

Conference constraints:

- 16:9 aspect ratio
- 16 slides, 15 seconds each
- four minutes total
- minimal words, visual-first
- no copyrighted Hollywood stills or logos without permission
- use an original meme-like control-room visual instead of a copyrighted film frame

Official presenter guidance: <https://www.usenix.org/conference/srecon26emea/instructions-presenters>

## Next execution sequence

1. Re-read the current controller and Vast inventory. Do not duplicate a live run.
2. Wait for `20260925T055500Z` provider invoices and Azure guard absence evidence to finish. If the controller is gone but the manifest is incomplete, create an honest terminal-failure note; do not invent workload output.
3. Normalize the 125 `manual-benchmark/SHA256SUMS` paths and verify every digest locally.
4. Create a separate sealed post-run GPU verdict that binds:
   - exact run and instance identity;
   - journal hash chain;
   - GPU/CUDA/KVM evidence;
   - frozen model and image contract;
   - 36 request records and Prometheus samples;
   - invoice and three-read absence artifacts;
   - clear boundary: standalone vLLM, not Kubernetes HPA.
5. Regenerate the evidence pack and update the Claude Design prompt/deck with the recovered RTX 4090 metrics.
6. Only after both Azure guard channels are terminal and budget headroom is revalidated, select a fresh verified pair excluding the failed machines.
7. Run one final two-node path. Preserve raw SSH, K3s node join, vLLM Prometheus metrics, TTFT/TPOT, GPU/KV/queue observations, HPA desired/ready replicas, invoices, three-read absence, and sealed hashes.
8. If the two-node path succeeds, run the paired CPU-HPA versus queue/KV-aware blocks with same model, load, and hardware. If not, keep the talk claim scoped to local signal independence plus standalone GPU latency evidence.

## Claim ledger

Safe now:

- Local CPU, queue, and synthetic-KV HPA signals independently produced 1-to-2 replica transitions.
- A real Vast RTX 4090 served 36 measured vLLM completions for Qwen2.5-1.5B.
- On that one-GPU standalone run, p50 TTFT increased 3.34x from concurrency 4 to 32 while p50 TPOT increased 1.06x.
- Exact teardown, invoice binding, and three-read absence evidence exist for the standalone GPU run.

Not safe yet:

- “Two-node Kubernetes/vLLM metric path works on Vast.”
- “CPU-only HPA loses to queue/KV-aware HPA” as a measured performance result.
- Any two-node TTFT, TPOT, queue, KV, GPU, or replica comparison.
- Any claim that Vast accepted a support ticket for the latest provider mismatch.
- Any production-readiness claim.
