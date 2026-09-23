# Final bounded Vast canary result

Run `gpu-smoke-distinct-20260923101734` is the single final paid distinct-machine attempt. It finished `FAILED_SAFE`; it is not GPU, vLLM, latency, or autoscaling evidence.

## Verified lifecycle facts

- Frozen offer `52180811`: provider contract `RTX 4000Ada`, 20,475 MiB, compute capability 8.9, VM enabled, machine `147086`.
- Created exact instance `52212017` with nonce-bound label `srecon26-gpu-smoke--nonce-distinct20260923095500`.
- Independent primary guard and deadline backstop armed before create. Guard workflow: <https://github.com/shreyanshjain7174/srecon26-independent-guard/actions/runs/35845737211>.
- SSH readiness failed after 36 bounded attempts. The controller classified this as unresolved access failure, not a confirmed provider/SKU contract fault. Website Report was therefore not attempted.
- Controller issued exact-ID teardown at `2026-09-23T10:24:34.166051Z`.
- Three fresh provider reads found zero matching instances at `10:24:38.905075Z`, `10:24:45.406835Z`, and `10:24:51.840973Z`.
- Final provider inventory was empty.
- Authoritative invoice charge: `$0.025` against a `$0.25` reservation. Remaining project-ledger headroom: `$3.971`.

## Integrity receipts

- Guard-acknowledged pre-anchor root: `e41a6683538ef846f7bb579acece9751ae6339c8e2a3ec65057ccaa3b5ec5f18`.
- Final sealed bundle root: `d8b36066acc2d616e315730d411923b41769b0845192f4138b1cd20b299f4803`.
- Journal SHA-256: `2f3d0b023935db0c2638895606ca6c63533270b4896c386ce52c616ab0258500`.
- Invoice evidence SHA-256: `21a633806656e078cf2bea9835b11390e54843b3c5c469c7f74199d606028ffb`.
- Reconciliation absence evidence SHA-256: `1dea1ce5db8c947d85a67f9839f33f8e118f1bd7c0b20861e61c8298f4c27dc0`.

## Honest presentation boundary

This run proves budget enforcement, pre-armed independent teardown, exact identity binding, conservative report gating, exact teardown, provider absence verification, invoice reconciliation, and sealed evidence. It does not prove direct GPU identity, CUDA, KVM capability, vLLM behavior, TTFT, or any CPU-only versus signal-aware A/B result.
