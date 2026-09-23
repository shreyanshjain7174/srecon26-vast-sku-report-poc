# Domain Pitfalls

**Domain:** GPU-rental experiment controller with evidence integrity chain + Kubernetes HPA canary + conference presentation
**Researched:** 2026-09-23
**Confidence:** HIGH (design constraints in PROJECT.md directly encode past failure modes)

---

## Critical Pitfalls

### Pitfall 1: Float Arithmetic in Budget Enforcement

**What goes wrong:**
Using `float` for the $5.00 cap lets accumulated rounding error allow overspend. At Python `float` precision, `0.1 + 0.2 != 0.3`. A series of small reservations can silently breach the cap by a fraction of a cent — enough for an extra paid create to slip through.

**Why it happens:**
It looks like overkill to import `Decimal` for a five-dollar ledger. The instinct is to use `float`, which works correctly for individual values but drifts on summation across multiple reservation events.

**How to avoid:**
All budget values live as `Decimal` from the moment they are read from JSON. No intermediate conversion to float anywhere on the budget-path. Parse at the boundary: `Decimal(str(raw_value))` — never `Decimal(float_value)`, which re-introduces rounding.

**Warning signs:**
Any `float` appearing in the budget module, any arithmetic using `/`, `*`, or `+` on raw numeric types, any JSON deserialization that uses `json.loads` without a `parse_float=Decimal` hook on budget fields.

**Phase to address:** Phase 1 — PoC Safety Foundation

---

### Pitfall 2: Race Condition in Budget Reservation

**What goes wrong:**
Two concurrent controller paths both read the ledger, both see remaining headroom, both proceed to reserve — resulting in double-billing that breaches the $5.00 cap. Even a single process can hit this if the create loop is re-entered on crash-resume without checking whether the reservation already exists.

**Why it happens:**
The ledger is append-only flat-file, not a database with row-level locking. The natural "check then act" pattern has a TOCTOU gap.

**How to avoid:**
The reservation write must be atomic at the OS level (use `O_EXCL` lock file or `fcntl` advisory lock before every ledger append). On crash-resume, reconcile by label *before* issuing any new reservation — if a label matches an existing open reservation, resume that reservation rather than creating a new one.

**Warning signs:**
Any code path that calls `create` without first holding the ledger lock. Any resume path that does not grep existing journal events for the instance label before issuing a new reservation.

**Phase to address:** Phase 1 — PoC Safety Foundation

---

### Pitfall 3: Ambiguous Create → Second Create → Double-Billing

**What goes wrong:**
A `create` API call to Vast.ai times out or returns an ambiguous error. The controller retries, creating a second instance. Both instances start billing. The controller has now spent money on an instance it cannot track.

**Why it happens:**
Network timeouts look identical to "create failed" errors. The retry is well-intentioned but unsafe because the provider API is not idempotent unless a client-side idempotency key is used.

**How to avoid:**
Never retry a create. On ambiguous response, immediately reconcile by querying the provider for instances matching the launch label. If one exists, adopt it. If none exists, the create failed cleanly and a new attempt is safe. The idempotency key is the label — always unique per run, never reused.

**Warning signs:**
Any `for _ in range(retries)` or `try/except … retry` wrapping a provider `create` call. Any create call issued without a unique label generated *before* the call.

**Phase to address:** Phase 1 — PoC Safety Foundation

---

### Pitfall 4: Teardown Guard Not Independently Reachable When Armed

**What goes wrong:**
The teardown guard is deployed on a host that is reachable only through the same laptop that runs the controller. When the laptop sleeps or loses network, the guard loses its activation channel — the hard deadline passes, the instance keeps billing.

**Why it happens:**
Developers co-locate the guard on the local dev machine or behind a home NAT for convenience. "It's just a PoC" suppresses the urge to pay for an always-on host.

**How to avoid:**
The guard MUST run on a separately-reachable always-on host (a cheap VPS, a persistent cloud VM, or a service like fly.io/render). The controller arms the guard over an outbound HTTPS call. The guard's teardown trigger is a time-based or heartbeat-loss-based trip wire — it does not depend on any callback from the laptop.

**Warning signs:**
Guard runs as a local process. Guard reachability test is `ping localhost` or a LAN IP. Guard activation requires an inbound connection from Vast.ai (which would require NAT traversal).

**Phase to address:** Phase 1 — PoC Safety Foundation

---

### Pitfall 5: Hard TTL Miscalculation — Not Subtracting Report Window + Teardown Margin

**What goes wrong:**
The hard TTL is set equal to the maximum allowed billing duration. A provider fault occurs near the deadline. The report-before-destroy contract requires: capture evidence → click Report → teardown — in that order. If the report window + teardown margin hasn't been subtracted from the hard TTL, there is not enough time left to complete all three steps before the billing clock runs out.

**Why it happens:**
The report window feels like "bonus time" rather than "mandatory time subtracted from the budget." The controller author sets `hard_ttl = max_billing_duration` and discovers the error only when a fault occurs at `max_billing_duration - 30s`.

**How to avoid:**
`effective_ttl = max_billing_duration - report_window_seconds - teardown_margin_seconds`. The guard triggers at `effective_ttl`, never at `max_billing_duration`. The cost deadline is `effective_ttl`.

**Warning signs:**
Any code where `hard_ttl` or `deadline` equals the raw budget-derived maximum duration without subtraction. Any teardown path that starts before the report step completes.

**Phase to address:** Phase 1 — PoC Safety Foundation

---

### Pitfall 6: Provenance Mixing — Synthetic KV Metrics Used as Real GPU Evidence

**What goes wrong:**
Local synthetic KV metrics are produced to demonstrate the KV-aware HPA arm. If those metrics are included in the evidence bundle without mandatory provenance labels, the analysis pipeline (or worse, the slide generator) treats them as real GPU/KV evidence. The talk makes a stronger claim than the data supports.

**Why it happens:**
All three local arms (CPU, queue, KV) are stored in the same evidence directory. A glob-based collector that doesn't check provenance fields conflates them.

**How to avoid:**
Every artifact carries a `provenance` field: `local_synthetic | gpu_real | comparison_real`. The analyzer hard-rejects any chart or claim that mixes provenance categories. Slide generator enforces the same gate — slides 7–13 require `gpu_real` or `comparison_real` provenance; `local_synthetic` evidence can only populate slides in the "local independence proof" section.

**Warning signs:**
Any evidence collector that uses `glob("**/*.json")` without reading the `provenance` field. Any slide that shows both local and GPU data in the same chart without a clear visual separator.

**Phase to address:** Phase 1 (labeling), Phase 3 (enforcement)

---

### Pitfall 7: Hash Chain Break on Crash-Resume

**What goes wrong:**
The controller crashes mid-write, leaving a partial JSON event at the end of the journal. On resume, the chain validator reads the corrupt tail, finds the hash doesn't match, and either (a) refuses to resume or (b) silently truncates and re-sequences. Option (a) kills the run; option (b) creates a gap in the tamper-evidence chain.

**Why it happens:**
Append-only files are not atomic at the OS level unless you write to a temp file and `os.rename`. A crash between the `write` and `flush/sync` calls leaves partial data.

**How to avoid:**
Write each event as: write to `journal.tmp` → `fsync` → `os.rename("journal.tmp", "journal.jsonl")` — but this only works for single-event files. For an append-only log, write the event to a staging file, validate the hash chain extension, then append. On resume, the validator must detect and truncate partial tail events (those not terminated by `\n` or failing JSON parse) before resuming sequence numbering.

**Warning signs:**
Journal writes using `file.write(json.dumps(event))` without a staging + rename pattern. Resume logic that doesn't handle `json.JSONDecodeError` on the last line of the journal.

**Phase to address:** Phase 1 — PoC Safety Foundation

---

### Pitfall 8: CUDA Not Actually Executing on Rented GPU

**What goes wrong:**
The Vast.ai RTX 3090 instance reaches `provider-running` state, SSH is reachable, the vLLM container reports "running" — but CUDA is not available to the container. Inference calls succeed on CPU fallback (slowly) or fail silently. Prometheus reports zero GPU utilization throughout the run. The evidence captures a metric path that never touched the GPU.

**Why it happens:**
`docker pull` and container start do not require GPU access. The NVIDIA Container Toolkit, the correct driver version, and `--gpus all` (or the k3s equivalent device plugin) must all be present and correctly configured. Vast.ai instances are not always configured identically even for the same SKU.

**How to avoid:**
Before any other canary step: run `nvidia-smi` via SSH, then run `python3 -c "import torch; print(torch.cuda.is_available())"` inside the container. Both must pass. Add a GPU smoke-test as Phase 2 step 0 — if it fails, tear down and report; do not proceed to vLLM canary.

**Warning signs:**
Skipping the smoke test and going straight to vLLM startup. Container log shows "CPU fallback" or very slow token generation for Llama-class models. `nvidia-smi` not in PATH inside the container.

**Phase to address:** Phase 2 — Live GPU Canary

---

### Pitfall 9: vLLM Image Digest Drift

**What goes wrong:**
`docker.io/vllm/vllm-openai:v0.26.0` is re-tagged to a different commit between PoC design and the paid run. The pinned digest `sha256:770fe65...` is no longer the image pulled by the tag. The environment behavior changes unpredictably.

**Why it happens:**
OCI registries allow tags to be moved. The `v0.26.0` tag is mutable — the image authors can repush. The digest is immutable, but if you pull by tag (not by digest), you get the new image.

**How to avoid:**
Always pull by digest: `docker pull docker.io/vllm/vllm-openai@sha256:770fe65b2c73ee74a5c42165cf3433de4048cc2cd9c57a937ca4e35aba5aa87b`. Re-validate the digest is still resolvable before every paid run with `docker manifest inspect`. If the digest is unreachable, halt and investigate before spending.

**Warning signs:**
Any `docker pull vllm/vllm-openai:v0.26.0` without the `@sha256:` suffix in automation scripts. Any skip of the pre-run digest validation step.

**Phase to address:** Phase 2 — Live GPU Canary

---

### Pitfall 10: HPA Signal Crosstalk in Independence Tests

**What goes wrong:**
The CPU HPA arm test is supposed to drive CPU above the HPA threshold while keeping queue depth and KV pressure below their margins. But the load generator that increases CPU also happens to enqueue requests, incidentally pushing queue depth above the noise floor. The test is not independent — the "CPU-only" arm is actually a combined CPU+queue arm.

**Why it happens:**
It's difficult to increase CPU utilization in a containerized workload without generating requests, and requests consume queue slots. The test design has to be careful to drain the queue before measuring independence.

**How to avoid:**
Design each arm with a dedicated synthetic load path:
- CPU arm: CPU-burn goroutine/thread, no request queue involvement
- Queue arm: request injector that holds requests in queue without processing (sleep in handler), low CPU
- KV arm: synthetic metric injection via Prometheus pushgateway, no real requests

Verify independence by recording all three signal values throughout each arm test. The analyzer must assert that non-target signals stayed below their margins for the entire duration of the transition.

**Warning signs:**
Any arm test that uses a real inference request to drive load. Any independence report that only shows the signal at the moment of transition rather than throughout the window.

**Phase to address:** Phase 1 — PoC Safety Foundation

---

### Pitfall 11: Custom Metrics API RBAC Not Configured for HPA

**What goes wrong:**
The HPA object queries the `custom.metrics.k8s.io` API for queue depth or KV cache metrics. The API adapter (Prometheus adapter) is installed but the RBAC binding is missing or incorrect. HPA reports `unable to fetch metrics` and never scales.

**Why it happens:**
Installing the Prometheus adapter creates the APIService registration but doesn't automatically grant the HPA controller read access to the custom metrics API. This is a separate RBAC step that docs sometimes omit.

**How to avoid:**
Verify the full HPA metric-fetch path end-to-end with `kubectl get --raw "/apis/custom.metrics.k8s.io/v1beta2/namespaces/default/pods/*/queue_depth"` before declaring the canary ready. This single command exercises: adapter running, APIService registered, RBAC correct, metric name matches config.

**Warning signs:**
HPA object shows `ScalingActive: False` with reason `FailedGetScale`. `kubectl describe hpa` shows metric fetch errors. Skipping the raw API verification step and only checking `kubectl get hpa`.

**Phase to address:** Phase 2 — Live GPU Canary

---

### Pitfall 12: autoscaling/v2 vs v2beta2 Object Version Confusion

**What goes wrong:**
The controller or helm chart creates HPA objects using `autoscaling/v2beta2` (deprecated in k8s 1.25, removed in 1.26). k3s ships a recent Kubernetes version where this API is gone. The HPA silently fails to be created, or the custom metrics field path differs between versions.

**Why it happens:**
Most online examples use `v2beta2`. The k3s default is recent enough that `v2` is the only stable external-metrics HPA API.

**How to avoid:**
All HPA manifests must use `apiVersion: autoscaling/v2`. Validate with `kubectl api-resources | grep horizontalpodautoscaler`. Test manifest apply before the paid run.

**Warning signs:**
Any HPA manifest with `v2beta1` or `v2beta2`. Any `kubectl apply` that returns `no matches for kind "HorizontalPodAutoscaler" in version "autoscaling/v2beta2"` without surfacing to the operator.

**Phase to address:** Phase 1 (local arms), Phase 2 (GPU canary)

---

### Pitfall 13: Prometheus Scraping Starts Before vLLM Emits Metrics

**What goes wrong:**
The canary timeline starts when vLLM container reports "running" but the model load takes 60-180 seconds. During model load, vLLM's `/metrics` endpoint either doesn't exist or returns empty counters. Prometheus scrapes return no data. The HPA has no metric signal to act on during the warm-up window. The first real load test starts while the metric pipeline is still empty.

**Why it happens:**
vLLM's container "ready" state is defined by its HTTP server accepting connections, not by the model being fully loaded into GPU VRAM. The `/metrics` endpoint opens before the first token can be generated.

**How to avoid:**
After container ready: poll `GET /health` until it returns 200. Then send one test completion request and wait for a non-empty response. Only after the first successful inference should the canary timeline begin. Record the warm-up duration in the evidence bundle.

**Warning signs:**
Timeline starts at `container_running_time`. Evidence shows the first 2 minutes of HPA metrics as flat zero. No warm-up step in the controller state machine.

**Phase to address:** Phase 2 — Live GPU Canary

---

### Pitfall 14: `DIAGNOSIS_UNRESOLVED` Triggering a Provider Report

**What goes wrong:**
The controller cannot determine if a failure is a provider fault or a controller/model/network fault. It reports the fault to Vast.ai anyway — generating a false report that could result in account action or wasted evidence-capture time. Meanwhile, the real issue is a misconfigured Prometheus adapter or a controller bug.

**Why it happens:**
The controller error handler catches all exceptions and routes them to the fault reporter for "safety." The developer conflates "we lost money on this run" with "the provider is at fault."

**How to avoid:**
The report adapter may only be called when `diagnosis == PROVIDER_FAULT_CONFIRMED`. `DIAGNOSIS_UNRESOLVED` must trigger: (1) capture diagnostic bundle, (2) log with `UNRESOLVED` label, (3) teardown without report. A human reviews the bundle and decides. False reports pollute provider reputation data and waste the report window.

**Warning signs:**
Any error handler that calls the report adapter on generic exception. Any state machine where `DIAGNOSIS_UNRESOLVED` transitions to `REPORT`.

**Phase to address:** Phase 1 — PoC Safety Foundation

---

### Pitfall 15: Evidence Bundle Missing Checksums at Anchor Time

**What goes wrong:**
The controller runs the experiment, collects artifacts, generates the SHA256SUMS file — but signs the git commit *before* copying the bundle to the independent guard journal. If the guard journal is the external anchor, the anchor doesn't contain the root hash. The external anchoring claim is false.

**Why it happens:**
The commit is the natural "done" signal. The developer commits first, then copies — but the copy step fails silently (network issue to the guard host) and the developer doesn't notice.

**How to avoid:**
The integrity bundle completion order is: (1) write all artifacts, (2) compute SHA256SUMS, (3) compute ROOT-HASH.txt, (4) **copy to guard journal and verify receipt**, (5) sign git commit. The receipt from step 4 must be present before step 5. The guard journal should return an acknowledgment hash that the controller records.

**Warning signs:**
Any sequence where `git commit -s` is called before the guard copy is confirmed. Any guard copy that ignores HTTP errors or timeouts. ROOT-HASH.txt present in repo but absent from guard journal.

**Phase to address:** Phases 1–2 (every bundle creation)

---

### Pitfall 16: Paired Comparison with Fewer Than Three Complete A/B Blocks

**What goes wrong:**
The comparison gets two A/B blocks before budget runs out. The developer labels it "paired comparison" in the deck and generates the paired chart. The statistical claim is unsupported — two blocks cannot establish the pattern required by the project spec. The conference audience accepts it as evidence; the claim is overclaimed.

**Why it happens:**
Two blocks looks "almost there." The budget is nearly exhausted. The developer rationalizes that two is enough for a lightning talk.

**How to avoid:**
The analyzer hard-refuses to generate a paired comparison chart from fewer than three complete blocks. If the run count is 1 or 2, the output is labeled `exploratory` and may only appear as a "preliminary results" slide with explicit caveat. The deck generator enforces this label at slide generation time.

**Warning signs:**
Any chart generation function that accepts `n_blocks < 3` without checking. Any slide that says "paired comparison" when the underlying data has fewer than 3 blocks. Any budget-remaining check that allows a partial third block to count as complete.

**Phase to address:** Phase 2 (data collection), Phase 3 (analysis gate)

---

### Pitfall 17: PowerPoint → Google Slides Import Corruption

**What goes wrong:**
The generated `.pptx` uses non-system fonts (e.g., Calibri, Source Code Pro). Google Slides substitutes them on import, breaking text layout. Slide aspect ratio is set to 4:3 instead of 16:9. Speaker notes import into the wrong field. Charts import as bitmaps rather than editable elements.

**Why it happens:**
`python-pptx` uses whatever fonts are set in slide masters. If the master references a font not available in Google Slides, the substitution is silent and layout-breaking.

**How to avoid:**
Use only fonts confirmed available in Google Slides: Arial, Roboto, Open Sans, Lato, PT Sans, Courier New. Set canvas to `Presentation(slide_width=Inches(13.33), slide_height=Inches(7.5))` (standard 16:9). Validate import by opening the .pptx in Google Slides and taking a screenshot of each slide — screenshot evidence is a hard requirement, not optional.

**Warning signs:**
Generated .pptx uses Calibri or any other MS-only font. Canvas dimensions don't match 16:9. No screenshot validation step in the pipeline.

**Phase to address:** Phase 3 — Evidence, Charts, and Deck

---

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| `float` for budget math | One less import | Silent overspend bypasses cap | Never |
| Skip GPU smoke test | Faster run | Collect fake GPU evidence | Never |
| Pull vLLM by tag not digest | Simpler command | Digest drift, non-reproducible run | Never |
| Guard on same laptop as controller | No extra VPS cost | Laptop sleep defeats teardown deadline | Never |
| Report on `DIAGNOSIS_UNRESOLVED` | "Safer" to always report | False reports, wasted run window | Never |
| Fewer than 3 A/B blocks | Under budget | Claim not statistically supported | Only if labeled "exploratory" |
| Hardcode `n_blocks=2` for deck | Ship faster | Slides make unsupported claim | Never without caveat slide |
| Skip external anchor copy check | Simpler code | Integrity claim is false | Never |
| Mix provenance in evidence bundle | Simpler collection | Talk makes overstated claim | Never |

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|-------------|----------------|------------------|
| Vast.ai create API | Retry on timeout | Reconcile by label, never retry create |
| Vast.ai report API | Call before evidence capture | Capture evidence first, then report, then teardown |
| Prometheus adapter | Install without RBAC binding | Verify via `kubectl get --raw /apis/custom.metrics.k8s.io/...` |
| k3s + NVIDIA device plugin | Assume GPU visible to containers | Confirm `nvidia-smi` and `torch.cuda.is_available()` inside container |
| python-pptx + Google Slides | Use Calibri/default fonts | Use only Google-available fonts, test import |
| append-only journal | Plain `file.write()` | Stage + fsync + rename for each event |
| Guard host | Call from controller thread | Guard is an independent process on a separate host |
| vLLM metrics endpoint | Scrape immediately on container ready | Wait for first successful inference before starting timeline |

## Performance Traps

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|----------------|
| HPA cooldown hiding signal | Scale-in happens too fast, 1→2 transition not cleanly captured | Set `scaleDown.stabilizationWindowSeconds` ≥ experiment window | Every run without explicit cooldown config |
| Prometheus scrape interval > HPA sync period | HPA sees stale metric values, scaling decision lags | Align scrape interval ≤ HPA `--horizontal-pod-autoscaler-sync-period` | Any run with default Prometheus 60s scrape |
| vLLM model load time > experiment timeout | Controller times out before first inference | Extend warm-up timeout; record in evidence | Large models on slow GPU memory transfer |
| SHA256 computation on large artifacts | Blocking the event loop during checksum | Use subprocess or thread for large file checksums | Artifact bundles > 100MB |

## Security Mistakes

| Mistake | Risk | Prevention |
|---------|------|------------|
| Vast.ai API key in process arguments | Exposed via `ps aux` on shared host | Read from root-owned `0600` file; never pass as argv |
| API key in run artifacts / logs | Leaked to evidence bundle, committed to repo | Scrub all artifact JSON and log lines for API key pattern before bundle seal |
| SSH private key in repo | Full access to any rented instance | Key files in `~/.ssh/` only; `.gitignore` enforced by pre-commit hook |
| Controller running as root | Broad blast radius if exploited | Run as unprivileged user; only credential file is root-owned |
| Semgrep skipped before paid run | Known high-severity pattern reaches live instance | Semgrep scan is a hard gate in the controller pre-flight checklist |

## "Looks Done But Isn't" Checklist

- [ ] **Budget ledger:** Decimal arithmetic used at *every* arithmetic site, not just the cap check — verify no `float` in budget module
- [ ] **Guard armed:** Guard host confirmed reachable from a *different* network than the controller before each create
- [ ] **GPU smoke test:** `nvidia-smi` AND `torch.cuda.is_available()` pass *inside the vLLM container*, not just on the host
- [ ] **Metric path:** `kubectl get --raw /apis/custom.metrics.k8s.io/v1beta2/...` returns a non-zero value before loading starts
- [ ] **Provenance labels:** Every artifact has an explicit `provenance` field; analyzer rejects bundles with missing labels
- [ ] **External anchor:** ROOT-HASH.txt is in the guard journal *and* the guard returned an acknowledgment hash before the git commit
- [ ] **Independence proofs:** Each local HPA arm test shows non-target signals stayed below margin for the *entire transition window*, not just at the peak
- [ ] **PPTX import:** Google Slides screenshot evidence taken for *every slide*, not just the title
- [ ] **Paired comparison gate:** Deck generation checked `n_blocks >= 3` before rendering paired chart
- [ ] **Credential hygiene:** `git log --all -p | grep -i "vast\|api.key\|token"` returns nothing

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| Float overspend detected | LOW | Rewrite budget module with Decimal; re-run ledger from journal to recompute balance |
| Double-create (two instances billing) | HIGH | Immediately tear down both instances; record in ledger; manually audit journal; contact Vast.ai support if needed |
| Guard unreachable at deadline | HIGH | Manual teardown via Vast.ai web console; document incident; redesign guard host selection |
| Hash chain corruption in journal | MEDIUM | Truncate at last valid event; re-derive sequence from events before break; mark run as "partial" in evidence bundle |
| CUDA unavailable on rented GPU | MEDIUM | Tear down, report as `PROVIDER_FAULT_CONFIRMED`, rent different instance |
| Prometheus adapter RBAC wrong | LOW | Apply corrected RBAC manifest; verify with raw API call; no evidence loss |
| PPTX font substitution on import | LOW | Rewrite slide template with Google-available fonts; re-export; re-screenshot |
| Paired comparison incomplete (2 blocks) | MEDIUM | Label data `exploratory`; substitute limitation slide in deck; do not re-run if budget exhausted |

## Pitfall-to-Phase Mapping

| Pitfall | Prevention Phase | Verification |
|---------|------------------|--------------|
| Float budget arithmetic | Phase 1 | Unit test: sum of known amounts equals exact Decimal |
| Race condition in reservation | Phase 1 | Integration test: concurrent reservation attempts; only one succeeds |
| Ambiguous create retry | Phase 1 | Test: mock timeout response → controller reconciles by label, no second create |
| Guard not independently reachable | Phase 1 | Network test: controller → guard over cellular/VPN separate from main connection |
| Hard TTL miscalculation | Phase 1 | Test: effective_ttl < max_billing_duration by at least report_window + teardown_margin |
| Provenance mixing | Phase 1 (labeling) + Phase 3 (gate) | Analyzer test: bundle with mixed provenance raises error |
| Hash chain break on resume | Phase 1 | Crash-injection test: partial write → resume → chain valid from truncation point |
| CUDA not executing | Phase 2 | Smoke test pass logged in evidence bundle before canary begins |
| vLLM digest drift | Phase 2 | Pre-run: `docker manifest inspect` confirms digest; logged in run metadata |
| HPA signal crosstalk | Phase 1 | Independence report: non-target signals below margin throughout transition window |
| Custom metrics RBAC | Phase 2 | `kubectl get --raw` call returns metric value before load test begins |
| autoscaling/v2 version | Phase 1 | k3s API resource check in controller pre-flight |
| Prometheus warm-up gap | Phase 2 | Controller waits for first successful inference before starting evidence timeline |
| DIAGNOSIS_UNRESOLVED false report | Phase 1 | Test: unresolved diagnosis → no report call; only PROVIDER_FAULT_CONFIRMED triggers report |
| Missing external anchor | Phases 1–2 | Post-bundle: verify ROOT-HASH.txt present in guard journal before commit |
| Fewer than 3 A/B blocks | Phase 3 | Deck generator: assert n_blocks >= 3 before paired chart; else substitute limitation slide |
| PPTX import corruption | Phase 3 | Screenshot validation of all 16 slides in Google Slides before accepting output |

## Sources

- PROJECT.md constraints directly encode prior failure modes (digest drift incident, float rounding, ambiguous create, guard co-location)
- Kubernetes autoscaling/v2 official docs (v2beta2 removed in k8s 1.26)
- vLLM container behavior: model load vs. HTTP server ready are independent events (known from vLLM issue tracker)
- python-pptx + Google Slides font compatibility: community-documented incompatibility list
- Vast.ai provider API: non-idempotent create confirmed in provider docs (no client idempotency key)
- NVIDIA Container Toolkit: device plugin + driver version dependency documented in k3s GPU guide

---
*Pitfalls research for: GPU-rental experiment controller with evidence integrity chain and Kubernetes HPA canary*
*Researched: 2026-09-23*
