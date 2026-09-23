# SRECon26 LLM HPA PoC

This repository contains the safety controller, independently anchored local HPA evidence, the bounded Vast live adapter, and the claim-gated SRECon26 presentation pipeline. The final paid canary ended `FAILED_SAFE`; no real GPU, vLLM, latency, or paired A/B result is claimed. Credentials are never stored in the repository.

Run the test suite with `make test`. The test target disables unrelated globally installed pytest plugins so the repository has a deterministic, dependency-free test environment.

Run `make setup-hooks` once per checkout. The tracked hook requires Semgrep and blocks staged provider-secret patterns on the project branch.

For a clean presentation build, install the pinned Node dependency and run the evidence gate before rendering:

```bash
pnpm install --frozen-lockfile
pnpm build:presentation
```

The deck builder first regenerates the evidence-gated charts and `artifacts/presentation/verdict.json` from the pinned evidence roots. It refuses unsupported live-GPU or A/B claims and emits `DECK-MANIFEST.json` with per-slide source hashes.
