# Vast GPU PoC learnings

This log records operational findings from the real Vast.ai path. It is not a
substitute for raw evidence, and a failed attempt is never promoted into a
performance claim.

## Before the new inference scope

- Marketplace allocation and provider `running` state did not prove usable GPU
  inference. Earlier attempts reached provider lifecycle states but did not
  capture CUDA execution, model responses, or latency measurements.
- The last distinct-machine attempt used offer `52180811`, machine `147086`,
  and instance `52212017`. SSH remained closed across the bounded readiness
  window. Exact teardown, three absence reads, and a `$0.025` invoice were
  preserved, but the result was correctly marked `FAILED_SAFE`.
- Reusing one route field with another is unsafe. Direct SSH is valid only when
  the exact instance record supplies both `public_ipaddr` and the published
  `22/tcp` host port; otherwise the resolver may use the exact provider proxy
  host/port pair. The two pairs must never be mixed.
- A hardware-only smoke is not enough for the talk. The accepted success
  condition now includes a real non-empty response plus raw timing/token and
  GPU telemetry tied to the exact rented instance.

## Current experiment design

- Start with one distinct, verified VM-enabled 24 GB Ada/Ampere offer selected
  from live availability for the frozen model. The current accepted set is RTX
  3090, RTX 4000 Ada, or RTX 4090; retain the exact SKU in every claim.
  Use a second or third GPU only if a prior run produces an actionable failure
  or a second arm materially strengthens the conference result.
- Run the frozen Qwen 1.5B revision in an immutable vLLM image directly under
  Docker. Avoid k3s for the first proof so image/model startup and inference,
  rather than cluster bootstrap, consume the bounded run window.
- Bind vLLM only to `127.0.0.1`. Capture the runtime command, port mapping,
  image digest, model endpoint, raw streamed responses, token usage, TTFT/E2E,
  derived TPOT/throughput, queue/KV metrics when present, and `nvidia-smi`
  samples before/during/after concurrent load.
- Keep the independent guard authoritative. Provider reporting is permitted
  only for a directly proven contract/SKU fault and never delays exact-ID
  teardown.

## Claims allowed only after a successful run

- The exact Vast machine, GPU, driver/CUDA report, model revision, and vLLM
  image executed a measured inference workload.
- The captured concurrency, TTFT, end-to-end latency, token throughput, GPU
  utilization/memory, and exposed queue/KV observations describe that run.

The run alone does not prove Kubernetes HPA behavior, KServe/llm-d behavior,
or a general causal claim about CPU autoscaling. Those require a later,
separately measured comparison.
