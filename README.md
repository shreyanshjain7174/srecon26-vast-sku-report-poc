# SRECon26 LLM HPA PoC

This repository contains the safety controller, independently anchored local HPA evidence, the bounded Vast live adapter, and the claim-gated SRECon26 presentation pipeline. The final paid canary ended `FAILED_SAFE`; no real GPU, vLLM, latency, or paired A/B result is claimed. Credentials are never stored in the repository.

Run the test suite with `make test`. The test target disables unrelated globally installed pytest plugins so the repository has a deterministic, dependency-free test environment.

Run `make setup-hooks` once per checkout. The tracked hook requires Semgrep and blocks staged provider-secret patterns on the project branch.

The single-host multi-GPU collector uses the official Vast SDK and defaults to a read-only, locally signed plan. Install its optional dependency, generate and inspect a plan, then execute only with the exact printed hash:

```bash
uv pip install --python .venv/bin/python 'vastai==1.8.2'
.venv/bin/python scripts/run_single_host_multigpu_benchmark.py \
	--azure-subscription-id "$AZURE_SUBSCRIPTION_ID" \
	--azure-resource-group "$AZURE_GUARD_RESOURCE_GROUP" \
	--azure-vm-name "$AZURE_GUARD_VM"
# Review the generated plan before using --execute, --plan, and --confirm-plan-sha256.
```

For a clean presentation build, install the pinned Node dependency and run the evidence gate before rendering:

```bash
pnpm install --frozen-lockfile
pnpm build:presentation
```

The deck builder first regenerates the evidence-gated charts and `artifacts/presentation/verdict.json` from the pinned evidence roots. It refuses unsupported live-GPU or A/B claims and emits `DECK-MANIFEST.json` with per-slide source hashes.
