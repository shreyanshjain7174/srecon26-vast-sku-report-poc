# Live Vast results

## Qwen3.8-27B on 8x RTX 4090 (tensor parallel 8)

### Setup

| | |
| --- | --- |
| Hardware | 8x NVIDIA RTX 4090 (24 GB), one host, PCIe interconnect (no NVLink) |
| Model | Qwen/Qwen3.8-27B, BF16 |
| Engine | vLLM, tensor parallel 8, `max_model_len` 4096, prefix caching on |
| Workload | 1024 input tokens, 256 output tokens per request; fixed prompt; concurrency 1 and 8 |

### Results

| Concurrency | Output tok/s | TTFT p50 / p99 | TPOT p50 / p99 | E2E p50 |
| --- | --- | --- | --- | --- |
| 1 | 62.1 | 0.53 / 0.55 s | 14.2 / 14.2 ms | 4.14 s |
| 8 | 193.1 | 3.89 / 4.29 s | 26.1 / 39.8 ms | 10.61 s |

- Going from concurrency 1 to 8: throughput rises 3.1x, p50 TTFT 7.3x, and p50 TPOT 1.8x.
  TTFT grows far faster than TPOT, so the extra load mostly lands as queueing and prefill time.
- Each GPU holds about 21.2 GiB and runs at 100 % utilisation under load.
- Cold start to first ready request: 475 s.

Run `single-host-multigpu-20260927T132309Z-3a1e631f`; raw artifacts and `SHA256SUMS` are in
`artifacts/live-runs/single-host-multigpu-20260927T132309Z-3a1e631f/remote-artifacts/`.

## BF16 vs FP8 on the same 8x RTX 4090 host (machine 10216)

Same model, image, TP8 and `max_model_len` 4096. FP8 is dynamic W8A8 (`--quantization fp8`);
FP8 KV adds `--kv-cache-dtype fp8`. Short cells use 1024 input / 256 output tokens; `long-c8`
uses 3072 / 512.

| Per GPU | BF16 | FP8 | FP8 + FP8 KV |
| --- | --- | --- | --- |
| Model weights | 6.69 GiB | 3.69 GiB | 3.69 GiB |
| KV cache (whole engine) | 499,230 tokens | 611,508 tokens | 874,837 tokens |

| Cell | BF16 tok/s, TTFT p50, TPOT p50 | FP8 | FP8 + FP8 KV |
| --- | --- | --- | --- |
| c1 | 65.2, 0.34 s, 14.1 ms | 72.5, 0.53 s, 11.9 ms | 69.0, 0.56 s, 12.5 ms |
| c8 | 226.1, 2.62 s, 25.0 ms | 203.8, 3.92 s, 24.0 ms | 199.4, 4.05 s, 24.4 ms |
| c32 | 316.2, 5.76 s, 77.8 ms | 271.6, 8.71 s, 87.3 ms | 257.5, 8.40 s, 91.2 ms |
| long-c8 | 254.6, 3.22 s, 25.2 ms | 245.1, 4.58 s, 24.0 ms | 164.3, 6.28 s, 37.0 ms |

- FP8 weights cut model memory per GPU 0.55x; FP8 KV grows KV capacity 1.75x over BF16.
- One request at a time, FP8 decodes faster (TPOT 14.1 -> 11.9 ms, throughput +11 %).
- Under 8 and 32 concurrent requests, BF16 keeps higher throughput and lower TTFT on this
  PCIe-only host; KV capacity was never the limit at 4096-token contexts (KV usage peaked at
  10 % in BF16).

Runs `single-host-multigpu-20260928T074427Z-4050fa59` (fp8, fp8-kv) and
`single-host-multigpu-20260928T083252Z-04cb07e6` (bf16); artifacts under each run's
`remote-artifacts/<precision>/`.

## Earlier bounded canary (2026-09-23)

Run `gpu-smoke-distinct-20260923101734` was the earlier final paid distinct-machine attempt. It finished `FAILED_SAFE`; it is not GPU, vLLM, latency, or autoscaling evidence.

### Verified lifecycle facts

- Frozen offer `52180811`: provider contract `RTX 4000Ada`, 20,475 MiB, compute capability 8.9, VM enabled, machine `147086`.
- Created exact instance `52212017` with nonce-bound label `srecon26-gpu-smoke--nonce-distinct20260923095500`.
- Independent primary guard and deadline backstop armed before create. Guard workflow: <https://github.com/shreyanshjain7174/srecon26-independent-guard/actions/runs/35845737211>.
- SSH readiness failed after 36 bounded attempts. The controller classified this as unresolved access failure, not a confirmed provider/SKU contract fault. Website Report was therefore not attempted.
- Controller issued exact-ID teardown at `2026-09-23T10:24:34.166051Z`.
- Three fresh provider reads found zero matching instances at `10:24:38.905075Z`, `10:24:45.406835Z`, and `10:24:51.840973Z`.
- Final provider inventory was empty.
- Authoritative invoice charge: `$0.025` against a `$0.25` reservation. Remaining project-ledger headroom: `$3.971`.

### Integrity receipts

- Guard-acknowledged pre-anchor root: `e41a6683538ef846f7bb579acece9751ae6339c8e2a3ec65057ccaa3b5ec5f18`.
- Final sealed bundle root: `d8b36066acc2d616e315730d411923b41769b0845192f4138b1cd20b299f4803`.
- Journal SHA-256: `2f3d0b023935db0c2638895606ca6c63533270b4896c386ce52c616ab0258500`.
- Invoice evidence SHA-256: `21a633806656e078cf2bea9835b11390e54843b3c5c469c7f74199d606028ffb`.
- Reconciliation absence evidence SHA-256: `1dea1ce5db8c947d85a67f9839f33f8e118f1bd7c0b20861e61c8298f4c27dc0`.

### Honest presentation boundary

This run proves budget enforcement, pre-armed independent teardown, exact identity binding, conservative report gating, exact teardown, provider absence verification, invoice reconciliation, and sealed evidence. It does not prove direct GPU identity, CUDA, KVM capability, vLLM behavior, TTFT, or any CPU-only versus signal-aware A/B result.

### Postmortem hardening (not live-validated)

- Future creates now require Vast's `--direct` launch flag.
- SSH resolution prefers the exact instance record's `public_ipaddr` only when paired with its explicit `ports["22/tcp"][0].HostPort` mapping.
- If no valid direct pair exists, the resolver falls back only to the same record's `ssh_host` plus `ssh_port`; it never combines a public IP with a proxy port or guesses a port.
- Provider status evidence records both candidates, the selected route, and the selection reason.

These changes close the routing defect found by the failed canary, but they are code-and-test evidence only. They do not convert the failed canary into a GPU result and have not consumed another paid attempt.
