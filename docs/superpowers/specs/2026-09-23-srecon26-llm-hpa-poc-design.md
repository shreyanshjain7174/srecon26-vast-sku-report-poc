# SRECon26 LLM HPA PoC Design

**Date:** 2026-09-23  
**Status:** Proposed for review  
**Talk:** “Why Your Kubernetes Autoscaler Fails LLM Inference (And What vLLM/KServe/llm-d Do Instead)”

## 1. Purpose

Build a presentation-grade proof of concept for a four-minute SRECon lightning talk. The PoC tests one bounded claim:

> CPU-only Kubernetes HPA can miss LLM demand because queue depth, KV-cache pressure, and time to first token can worsen before CPU utilization creates a useful scaling signal.

The work is an evidence pipeline, not a production serving platform. Every slide claim must trace to a captured, checksummed artifact or be labeled as background, hypothesis, or future work.

## 2. Success Criteria

The project succeeds when it produces:

1. Local evidence showing CPU, request-queue, and synthetic KV-cache signals independently drive Kubernetes HPA decisions.
2. A real GPU canary showing the provider, GPU, CUDA, vLLM, Kubernetes, Prometheus, custom-metrics, and HPA observation path works end to end.
3. If compatible two-replica GPU capacity is available within the remaining budget, a paired comparison using the same model, workload, hardware class, and timing windows for CPU-only versus queue/KV-aware HPA.
4. A checksummed integrity bundle containing raw metrics, workload records, lifecycle events, cost records, environment identity, and an externally anchored root hash.
5. Publication artifacts: CSV and JSON summaries, reusable PNG/SVG graphs, and a validated 16-slide PowerPoint deck with speaker notes that imports into Google Slides.
6. Zero active Vast.ai instances after every run, proved by repeated inventory checks.

The PoC does not need a successful real A/B result to remain useful. If capacity, topology, compatibility, or budget blocks the comparison, the deck must say so and limit its conclusion to the local signal proof and real metric-path canary.

## 3. Non-Goals

- Multi-region or production clusters.
- Sharding, distributed inference, or frontier-model benchmarking.
- Production-grade KServe, llm-d, or KServe/llm-d integration.
- General claims that CPU HPA always fails.
- Treating a local mock or synthetic KV metric as real GPU/KV evidence.
- Spending provider credit without an independently reachable teardown guard.
- Retrying an ambiguous provider create request.
- Allowing report-button automation to defeat the cost deadline.

## 4. Current Evidence Boundary

The reference workspace already proves that three local signals can scale a mock workload from one replica to two. That evidence is local and synthetic.

Existing real rentals prove only that Vast.ai returned RTX 3090 instances, the pinned vLLM container reached provider-running state, and SSH became reachable. They do not prove CUDA execution, a successful vLLM request, real vLLM metrics, Kubernetes custom metrics, HPA behavior, or the CPU-versus-queue/KV comparison.

The new project will selectively port tested safety and fixture logic from the reference workspace. It will not copy the prior kit wholesale, and it will not describe the prior KVM template as known-good. The only currently evidenced runnable image path is:

```text
docker.io/vllm/vllm-openai:v0.26.0
sha256:df2607b26bdda2875de4832f4d08da0055b4b6e3570347f3a849bcc652771dd6
```

This image must still be revalidated before every paid run.

## 5. System Architecture

The project contains six isolated components.

### 5.1 Experiment Controller

Owns the state machine, run identity, budget ledger, deadlines, exact instance ownership, evidence paths, and terminal status. It is the only component allowed to request creation. It performs normal teardown after exact-ID and exact-label validation.

### 5.2 Provider Adapter

Wraps read-only account and offer inspection, one create attempt, exact-instance inspection, and exact-instance destruction. Provider output is normalized into versioned JSON records without exposing credentials.

### 5.3 Independent Teardown Guard

Runs on a separately reachable, always-on controller host outside both the rented machine and the laptop process that performs the experiment. Laptop sleep or loss must not affect it. It is armed before creation, bound to a fresh nonce and expected label, reconciles delayed or ambiguous creation, and has break-glass teardown authority only after the immutable hard deadline or loss of the controller heartbeat. Controller and guard share an append-only journal persisted on the independent host and the same ownership contract; either may record teardown, but neither may target an instance that fails the nonce, label, and exact-ID checks. The guard reads its provider credential from a root-owned `0600` secret file or platform secret store. The credential is never copied into the repository, run artifacts, process arguments, or logs; provider-side least privilege is used when available, and wrapper-level ownership checks remain mandatory.

### 5.4 Desktop Report Adapter

Uses the authenticated desktop website to report a qualifying provider fault before normal teardown. It verifies the exact instance and host identity, captures before/after screenshots, clicks the Report control, waits for a bounded confirmation, and writes a receipt record. It holds no provider API credential.

### 5.5 Workload and Metrics Harness

Creates repeatable local and real-GPU workloads, records request timing, scrapes Prometheus, captures HPA and pod events, and writes raw evidence before analysis. Local and real modes share a schema but use explicit provenance labels.

### 5.6 Analysis and Presentation Builder

Validates artifact completeness, computes summaries, generates graphs, and builds the deck. It refuses to render unsupported claims and labels local, real-canary, and real-comparison evidence separately.

## 6. Controller State Machine

```text
NEW
  -> OFFLINE_VALIDATED
  -> BUDGET_RESERVED
  -> OFFER_PINNED
  -> REPORT_ADAPTER_READY
  -> GUARD_ARMED
  -> CREATE_REQUESTED
  -> CREATED_VERIFYING
       -> RUNNING_CANARY
            -> COLLECTING
            -> REPORTING_RESULTS
            -> DESTROYING
       -> PROVIDER_FAULT
            -> CAPTURING_FAULT
            -> REPORTING_FAULT
            -> DESTROYING
  -> ABSENCE_VERIFYING
  -> TERMINAL
```

Every transition appends a timestamped event to an immutable run journal. Re-running a command resumes from the journal; it must not issue a second create or second destructive action.

Terminal states are:

- `SUCCEEDED`: required evidence captured and absence proved.
- `FAILED_REPORTED`: qualifying fault reported, then absence proved.
- `FAILED_REPORT_UNCONFIRMED`: report attempt failed or timed out, alert emitted, then absence proved.
- `FAILED_SAFE`: run stopped before creation or after confirmed teardown.
- `SAFETY_BREACH`: ownership or absence could not be proved. This blocks every later paid run.

## 7. Provider Fault and Report-First Contract

Qualifying provider faults include:

- Returned GPU model, count, memory, compute capability, host, price, or label differs from the pinned offer contract.
- GPU is absent or `nvidia-smi` fails after the bounded boot window.
- CUDA cannot initialize with the pinned container.
- Provider says running but SSH or required direct ports remain unusable past the bounded readiness window.
- Pinned image digest is not the image actually running.
- Host behavior makes the advertised SKU unusable for the canary.

Application defects, invalid project configuration, and model-download failures are recorded but are not automatically classified as provider SKU faults.

For a qualifying fault, the order is:

1. Freeze a fault bundle containing preflight contract, returned contract, probe output, timestamps, instance ID, offer ID, machine ID, label, and fault code.
2. Open the exact instance in the authenticated desktop site.
3. Capture a pre-report screenshot.
4. Click Report and submit the concise evidence-backed fault description.
5. Capture confirmation, report ID if present, and a post-report screenshot.
6. Request exact-ID teardown after verifying both instance ID and nonce-bound label.
7. Prove absence with three inventory reads separated by bounded intervals.

The report window is 60 seconds. Each run computes an immutable `report_start_by` time:

```text
report_start_by = hard_teardown_deadline
                  - 60-second report window
                  - 60-second exact teardown margin
                  - 45-second three-read absence margin
```

All SKU, SSH, CUDA, image, and host probes must finish before `report_start_by`. A confirmed contract mismatch or provider-attributable failure becomes `PROVIDER_FAULT_CONFIRMED` and starts reporting immediately. A probe that remains inconclusive because of client configuration, controller network, model download, or insufficient evidence becomes `DIAGNOSIS_UNRESOLVED`: it preserves the bundle, emits an immediate alert, skips provider reporting to avoid a false report, and proceeds to exact teardown. Normal teardown may begin after report confirmation, report-window expiry, or unresolved-diagnosis capture. The independent guard never extends the hard deadline and uses break-glass teardown when the controller misses it. Creation fails closed unless the reserved runtime funds the probes, report grace, teardown margin, and absence checks. Failure to confirm a justified report produces `REPORT_UNCONFIRMED` and an immediate visible alert, but never blocks the independent teardown deadline. This is the only safe interpretation of “report before destroy” under a strict cost boundary.

## 8. Cost and Teardown Safety

The controller maintains a project ledger independent of available account credit. Maximum new exposure is **$5.00** across all paid runs in this project.

Before creation, it calculates a conservative reservation:

```text
hourly instance price × hard TTL
+ storage reservation
+ bandwidth reservation
+ safety margin
```

Creation is blocked if the reservation exceeds the unspent project budget. A provider marketplace cannot guarantee an exact final bill, so the deck describes this as a bounded exposure policy, not a billing guarantee.

Budget stages:

- Offline pipeline and failure fixtures: $0.00.
- Real GPU/CUDA smoke: reserve at most $1.00.
- Real vLLM request and metric-path canary: reserve at most $1.00.
- Paired HPA comparison: reserve at most the remaining $3.00 and run only when two already-ready compatible GPU capacities are available.

Required safeguards:

- Autobilling remains disabled.
- No existing instance may be present at run start unless explicitly owned by the same run journal.
- Fresh 128-bit nonce and unique label per run.
- Exactly one create request; ambiguous create is reconciled, never retried.
- Guard armed and independently reachable before create.
- Local fallback watchdog also armed, but never treated as the primary safety mechanism.
- Immutable hard deadline includes probe time, report grace, teardown margin, and absence-check margin.
- Controller writes a heartbeat to the shared journal; guard authority activates only after heartbeat loss or the hard deadline.
- Exact-ID and exact-label checks before teardown.
- Three consecutive absence reads before terminal success.
- Any safety breach blocks all later paid stages.

## 9. Experiment Design

### 9.1 Local Signal Independence

Run three isolated arms against the same local mock service:

- CPU arm: increase CPU utilization while queue and synthetic KV remain below their thresholds.
- Queue arm: increase waiting requests while CPU and synthetic KV remain below their thresholds.
- KV arm: increase synthetic KV pressure while CPU and queue remain below their thresholds.

All arms use `autoscaling/v2`, a 15-second Prometheus scrape, and a 15-second HPA reconciliation target. CPU comes from the Kubernetes resource metrics API as average utilization. Queue depth and synthetic KV pressure come from Prometheus Adapter through the custom metrics API as per-pod average values. Each target is set from a measured idle baseline; the injected signal must exceed its target by at least 20%, while both non-target signals remain at least 20% below their targets. Scale-up stabilization is zero, scale-down stabilization is 300 seconds, and the experiment allows four reconciliation intervals before judging a transition.

Each arm must capture the source metric, metrics API value, HPA desired replicas, ready replicas, pod events, and timestamps. Success requires each target signal to cause an independent one-to-two replica transition while non-target signals remain below their margins. A 90-second negative-control window precedes injection and must show no desired-replica change.

This experiment proves signal plumbing and independence only. It does not prove real vLLM latency or real KV behavior.

### 9.2 Real GPU Metric-Path Canary

The real canary uses a single-node k3s cluster installed on a full KVM Vast.ai GPU VM. The VM must expose the pinned GPU through the NVIDIA container runtime and Kubernetes device plugin. vLLM runs as a Kubernetes pod from the pinned image digest. Prometheus scrapes vLLM and node metrics; metrics-server supplies CPU resource metrics; Prometheus Adapter publishes queue and KV metrics to `custom.metrics.k8s.io`; an `autoscaling/v2` HPA reads those APIs. Only SSH is exposed publicly, restricted to the controller source; Prometheus, vLLM, and Kubernetes API access use the SSH tunnel or localhost. A native Vast container may validate GPU and vLLM separately, but cannot satisfy this end-to-end canary unless its privilege and Kubernetes topology are first proved equivalent.

The canary must capture:

- Provider contract and price.
- GPU UUID, model, memory, driver, and compute capability.
- CUDA health.
- Kubernetes node and allocatable GPU state.
- vLLM version, model revision, and image digest.
- One warm-up request and one measured request.
- TTFT, TPOT, end-to-end latency, queue time, and request status.
- vLLM queue and KV-cache metrics.
- Prometheus samples and custom-metrics API values.
- HPA desired/ready replicas and Kubernetes events.
- Cost timestamps and teardown proof.

Readiness gates require the node to report the expected allocatable GPU, the device plugin to be Ready, the vLLM pod to hold the GPU resource, Prometheus targets to be healthy, and all three metrics APIs to return bounded fresh samples before workload starts.

This canary proves the observation path, not comparative autoscaling performance.

### 9.3 Conditional Real Comparison

Run only if two compatible ready GPU capacities and Kubernetes GPU topology are available within budget. Compare:

- Arm A: CPU-only HPA.
- Arm B: queue/KV-aware HPA with the same minimum and maximum replicas.

Controls:

- Same pinned model and revision.
- Same image digest and GPU class.
- Same prompt trace, token lengths, arrival schedule, seed, warm-up, and test duration.
- Same replica limits, readiness policy, and collection windows.
- Three complete paired blocks with fixed alternated order: A/B, B/A, A/B. Each block contains both arms and uses the same trace identity and collection duration.

Record queue depth, TTFT, TPOT, end-to-end latency, GPU utilization, KV-cache utilization, CPU, desired replicas, ready replicas, readiness delay, and failed requests.

Three complete paired blocks with required metrics, valid checksums, and no ownership or safety breach are the minimum comparative dataset. Any smaller, incomplete, or unbalanced run is labeled exploratory and may appear only as operational evidence; it cannot support a performance conclusion or a paired comparison chart. If the comparison cannot run, no chart may imply comparative results.

## 10. Evidence Schema and Integrity

Each run writes to `artifacts/runs/<run-id>/` and contains:

- `run-manifest.json`: schema version, provenance, git commit, commands, tool versions, environment, and experiment arm.
- `provider/`: account-safe preflight, offer, instance lifecycle, cost, and absence proof.
- `report/`: fault classification, description, screenshots, confirmation, and receipt status.
- `kubernetes/`: nodes, pods, HPAs, events, custom metrics, and manifests.
- `metrics/`: raw Prometheus exposition and normalized time series.
- `workload/`: prompt-trace identity, request-level records, and summary.
- `gpu/`: `nvidia-smi`, CUDA probe, and telemetry.
- `analysis/`: derived tables and statistical summaries.
- `SHA256SUMS`: digest for every immutable artifact.
- `ROOT-HASH.txt`: hash of the finalized manifest and checksum file, anchored in a signed-off Git commit and copied to the independently reachable controller journal before the run is considered complete.

All event records use UTC wall-clock timestamps plus monotonic nanoseconds from run start. The manifest records clock source, detected wall-clock skew, tool versions, and controller/guard identity. Browser screenshots must crop or redact account balance, email, IP addresses not needed for the report, API tokens, SSH material, and unrelated instances. Manifests and command logs must never contain provider credentials, cookies, tokens, private keys, or environment dumps.

The analyzer rejects missing required files, mixed provenance, timestamps outside the run window, non-monotonic event ordering, a changed workload identity, checksum mismatch, or a root hash that does not match its external anchor. The bundle is described as checksummed and externally anchored, not absolutely tamper-proof.

## 11. Graphs and Charts

Generate reusable high-resolution PNG and SVG outputs plus native PowerPoint charts where supported:

1. Local signal independence: small multiples for CPU, queue, synthetic KV, and desired replicas.
2. Canary path: provider-to-GPU-to-vLLM-to-Prometheus-to-HPA sequence with measured checkpoints.
3. Canary time series: queue, KV, CPU, GPU, TTFT, and HPA state over time.
4. Conditional comparison: paired CPU-only versus queue/KV-aware TTFT and queue charts with replica readiness overlay.
5. Budget and lifecycle: spend reservation, actual spend, report status, and teardown confirmation.

Graphs must show units, sample windows, provenance, and whether data is local synthetic, real canary, or real comparison. No interpolated or illustrative series may appear as measured data.

## 12. Presentation Deliverable

Produce a 16:9 `.pptx` that imports into Google Slides. Use a classic systems-conference style: high contrast, restrained typography, one dominant visual per slide, and minimal text. Use safe fonts, native charts, and speaker notes. Validate file structure, render every slide, inspect overflow and alignment, and verify content extraction. Then upload the final file to Google Slides, open every slide, compare it with the local reference render, and capture an import-validation screenshot. If Google Slides changes a native chart materially, replace that chart with the validated SVG or high-resolution PNG while keeping the underlying CSV/JSON beside the deck.

The 16-slide, 15-seconds-per-slide narrative is:

1. Title and sharp claim.
2. Request arrives: prefill, decode, and shared GPU resources.
3. Why CPU can look calm while users wait.
4. Three signals: CPU, queue, and KV pressure.
5. Experimental question and evidence boundary.
6. Local PoC architecture.
7. Local CPU-signal result.
8. Local queue-signal result.
9. Local synthetic-KV result.
10. What local evidence proves and does not prove.
11. Real GPU canary path.
12. Real canary result or explicit blocker.
13. CPU-only versus queue/KV comparison result, or clearly labeled planned experiment if not run.
14. What vLLM, KServe, and llm-d expose or automate, without claiming they remove measurement needs.
15. Operator rule: scale on demand pressure, gate on readiness and capacity.
16. Final claim, limitations, and repository/evidence pointer.

Slides 7–13 are generated from validated result files. If an expected real result is absent, the deck substitutes a limitation slide; it never substitutes fabricated example data.

## 13. Error Handling

- Offline test failure: stop before spend.
- Report adapter readiness failure: stop before spend.
- Guard readiness failure: stop before spend.
- Budget reservation failure: stop before spend.
- Create timeout or ambiguous response: do not retry; let guard reconcile by label.
- Ownership mismatch: do not destroy through the normal controller; alert and let the independent guard apply its exact ownership rules.
- Qualifying SKU fault: capture, report, then destroy.
- Workload or collection failure: capture application evidence, destroy safely, and classify separately from provider fault.
- Missing teardown proof: block all paid stages and mark the run `SAFETY_BREACH`.
- Missing evidence for a slide claim: omit or weaken the claim.

## 14. Testing Strategy

### Offline Unit Tests

- State transitions and crash-safe resume.
- Budget arithmetic and cumulative reservation.
- Offer, SKU, image, and label contract validation.
- Fault classification.
- Report receipt parsing and timeout behavior.
- Exact ownership checks and absence quorum.
- Evidence schema and checksum verification.
- Claim-to-evidence gating.

### Offline Integration Tests

- Delayed create discovered after client timeout without a second create.
- SKU mismatch follows capture, report, destroy order.
- Report confirmation failure still reaches deadline teardown.
- Report grace, teardown margin, and absence checks fit entirely before the hard deadline.
- Desktop adapter targets only the expected instance fixture.
- Controller and break-glass guard share idempotency without double-destroying or crossing ownership boundaries.
- Teardown never targets another label or instance.
- Three absence checks required.
- Deck substitutes limitation content when real results are absent.

### Live Gates

- Dry report-path check without submitting a false report.
- Independent guard reachability and deadline rehearsal.
- Account inventory and budget snapshot.
- One paid stage at a time, with terminal evidence review before the next.

New code must receive a Semgrep security scan before any paid run. Critical browser and report flows require Playwright validation against non-destructive fixtures before live use.

## 15. Repository and Delivery Workflow

Project root:

```text
/Users/sunny/Documents/Codex/2026-09-23/srecon26-vast-sku-report-poc
```

Development occurs on `feat/vast-sku-report-poc`. Commits are signed off using `git commit -s` and contain no Codex coauthor. Implementation follows milestone phases after this design and its implementation plan are approved. A pull request is created only after tests, security checks, evidence audit, deck validation, and branch review pass.

## 16. Acceptance Checklist

- [ ] Fresh repository and feature branch exist.
- [ ] Written implementation plan approved.
- [ ] Offline controller, budget, report-order, teardown, and evidence tests pass.
- [ ] Semgrep scan has no unresolved high-severity finding in new code.
- [ ] Local CPU, queue, and synthetic-KV arms produce validated evidence.
- [ ] Desktop report adapter passes non-destructive fixture validation.
- [ ] Independent guard is reachable and proven before any paid run.
- [ ] Total new provider exposure does not exceed the $5 project ledger.
- [ ] Real canary either produces complete required evidence or a precise limitation record.
- [ ] Real comparison runs only if topology, readiness, and budget gates pass.
- [ ] Every paid run ends with three absence confirmations.
- [ ] Results produce validated CSV/JSON, charts, graphs, and checksums.
- [ ] Sixteen-slide deck contains only evidence-supported claims.
- [ ] PowerPoint validation, content extraction, and rendered visual QA pass.
- [ ] Google Slides import opens all 16 slides without material visual or content loss, with screenshot evidence.
- [ ] Branch review passes and the pull request is attached to the task.
