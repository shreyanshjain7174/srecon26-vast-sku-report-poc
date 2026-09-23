# Guarded Vast GPU Canary and Conditional HPA Comparison Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce defensible real GPU/vLLM/Kubernetes metric-path evidence and, only when feasibility and remaining budget permit, three complete paired HPA comparison blocks.

**Architecture:** Extend foundation protocols with a read-only-first Vast CLI adapter, an independently hosted deadline guard, a desktop report driver, and a KVM single-node k3s canary. Every paid stage is journaled, separately reserved, run once, finalized, torn down, and absence-verified before the next stage.

**Tech Stack:** Python 3.14, Vast CLI JSON, SSH, systemd timer/service on independent Linux controller, k3s, containerd NVIDIA runtime, NVIDIA device plugin, vLLM 0.26.0 pinned by digest, Prometheus, metrics-server, Prometheus Adapter, Playwright fixture tests.

**Spec:** `docs/superpowers/specs/2026-09-23-srecon26-llm-hpa-poc-design.md`

## Global Constraints

- Foundation plan and no-spend gate must pass first.
- Native Vast Docker instances do not satisfy Kubernetes metric-path proof unless equivalent privilege/topology is independently proved.
- Paid KVM use requires a fresh capability probe; prior templates and hosts are not known-good.
- Independent guard cannot run on the laptop or rented instance.
- Live report submission occurs only for a genuine `PROVIDER_FAULT_CONFIRMED` tied to exact instance ID and label.
- Stage reservations are at most `$1.00` for GPU/CUDA smoke, `$1.00` for metric-path canary, and remaining `$3.00` for comparison.
- Comparison requires three complete paired blocks; partial data is exploratory only.
- All commits use `git commit -s`; no Codex coauthor trailer.

## Review Focus

- Delayed instance visibility after create timeout must not trigger a second create; Task 1 pins this.
- Guard heartbeat loss during report window must preserve immutable hard deadline and exact ownership; Task 2 pins this.
- Desktop fixture matching another instance must block click and teardown authorization; Task 3 pins this.
- KVM offer that lacks systemd, cgroup v2, container runtime, or GPU device support must stop before k3s bootstrap; Task 4 pins this.
- A/B runner must reject changed model/image/GPU/trace/window and any reservation above remaining budget; Task 6 pins this.

---

## File Structure

```text
src/srecon26_poc/
  vast_provider.py
  guard_client.py
  desktop_report.py
  canary.py
  comparison.py
guard/
  guard_worker.py
  srecon26-guard.service.template
  srecon26-guard.timer.template
  install_guard.sh
providers/vast/fixtures/
browser/report-fixture/
infra/k3s/
scripts/
  preflight_vast.py
  preflight_guard.py
  preflight_report.py
  run_canary.py
  run_comparison.py
docs/runbooks/
tests/unit/
tests/integration/
tests/e2e/
```

### Task 1: Vast CLI Adapter and Exactly-Once Reconciliation

**Files:**
- Create: `src/srecon26_poc/vast_provider.py`
- Create: `scripts/preflight_vast.py`
- Create: `providers/vast/fixtures/account.json`
- Create: `providers/vast/fixtures/offers.json`
- Create: `providers/vast/fixtures/instances.json`
- Create: `tests/unit/test_vast_provider.py`
- Create: `tests/integration/test_vast_delayed_create.py`

**Interfaces:**
- Consumes: `Provider`, contracts, journal, ledger from foundation.
- Produces: `VastCliProvider` with bounded subprocess calls and redacted normalized records.

- [ ] **Step 1: Write failing parsing and delayed-create tests**

```python
def test_account_snapshot_exposes_no_secret(vast_fixture):
    snapshot = VastCliProvider(vast_fixture.cli).account_snapshot()
    assert snapshot.autobill_enabled is False
    assert "api_key" not in snapshot.to_json()

def test_ambiguous_create_reconciles_unique_nonce_label_without_retry(vast_fixture, controller):
    vast_fixture.timeout_after_create = True
    controller.run_once_expect_interruption()
    controller.run_or_resume()
    assert vast_fixture.create_calls == 1
    assert controller.instance_id == vast_fixture.created_instance_id
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python -m pytest -q tests/unit/test_vast_provider.py tests/integration/test_vast_delayed_create.py`  
Expected: missing module failures.

- [ ] **Step 3: Implement bounded CLI adapter**

Invoke CLI with argument arrays, never shell strings. Apply per-command timeouts. Parse JSON before returning typed contracts. On ambiguous create, search only for one exact nonce-bound label; zero matches remains pending, one match reconciles, multiple matches becomes `SAFETY_BREACH`.

- [ ] **Step 4: Run tests and read-only live snapshot**

Run: `python -m pytest -q tests/unit/test_vast_provider.py tests/integration/test_vast_delayed_create.py`  
Expected: PASS.  
Run: `python scripts/preflight_vast.py --read-only --json artifacts/vast-account-preflight.json`  
Expected: zero creates/destroys; account, autobill, inventory count, and redacted offer capability summary recorded.

- [ ] **Step 5: Commit**

```bash
git add src/srecon26_poc/vast_provider.py scripts/preflight_vast.py providers tests
git commit -s -m "feat: add read-only-first Vast provider"
```

### Task 2: Independently Hosted Deadline Guard

**Files:**
- Create: `src/srecon26_poc/guard_client.py`
- Create: `guard/guard_worker.py`
- Create: `guard/srecon26-guard.service.template`
- Create: `guard/srecon26-guard.timer.template`
- Create: `guard/install_guard.sh`
- Create: `scripts/preflight_guard.py`
- Create: `tests/unit/test_guard_client.py`
- Create: `tests/integration/test_guard_deadline.py`
- Create: `tests/integration/test_guard_idempotency.py`

**Interfaces:**
- Consumes: `Guard` protocol and journal.
- Produces: remote `GuardWorker` commands `preflight`, `arm`, `heartbeat`, `status`, and `disarm-after-absence`.

- [ ] **Step 1: Write failing deadline and ownership tests**

```python
def test_guard_destroys_exact_owned_instance_after_heartbeat_loss(clock, guard_harness):
    guard_harness.arm(INSTANCE_ID, LABEL, NONCE, clock.now() + timedelta(minutes=10))
    clock.advance(guard_harness.heartbeat_timeout + timedelta(seconds=1))
    guard_harness.tick()
    assert guard_harness.provider.destroy_calls == [(INSTANCE_ID, LABEL)]

def test_guard_refuses_changed_label_or_nonce(guard_harness):
    guard_harness.provider.instance.label = "unowned"
    guard_harness.tick_at_deadline()
    assert guard_harness.provider.destroy_calls == []
    assert guard_harness.status == "OWNERSHIP_MISMATCH"
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python -m pytest -q tests/unit/test_guard_client.py tests/integration/test_guard_deadline.py tests/integration/test_guard_idempotency.py`  
Expected: missing module failures.

- [ ] **Step 3: Implement worker, durable journal, and templates**

Worker stores state below an explicit root-owned directory, locks per nonce, fsyncs state, reads provider secret from a `0600` file, and destroys only after exact instance ID, label, and nonce reconciliation. Timer and service use absolute paths, `NoNewPrivileges=true`, and bounded command timeouts. No secret enters process arguments.

- [ ] **Step 4: Run offline and remote preflight**

Run: `python -m pytest -q tests/unit/test_guard_client.py tests/integration/test_guard_deadline.py tests/integration/test_guard_idempotency.py`  
Expected: PASS.  
Run: `python scripts/preflight_guard.py --require-independent --json artifacts/guard-attestation.json`  
Expected: remote host identity differs from laptop/rental, service/timer test passes, journal survives reconnect, credential permissions are `0600`, and no paid resource is created.

- [ ] **Step 5: Commit**

```bash
git add src/srecon26_poc/guard_client.py guard scripts/preflight_guard.py tests
git commit -s -m "feat: add independent deadline guard"
```

### Task 3: Desktop Report Driver and Non-Submitting Fixture

**Files:**
- Create: `src/srecon26_poc/desktop_report.py`
- Create: `browser/report-fixture/index.html`
- Create: `browser/report-fixture/app.js`
- Create: `scripts/preflight_report.py`
- Create: `tests/unit/test_desktop_report.py`
- Create: `tests/e2e/test_report_fixture.py`
- Create: `docs/runbooks/provider-reporting.md`

**Interfaces:**
- Consumes: `DesktopReportAdapter`, `FaultRecord`, `ReportReceipt`.
- Produces: fixture driver by default and live driver activated only from `REPORTING_FAULT`.

- [ ] **Step 1: Write failing exact-target and timeout tests**

```python
def test_fixture_submits_only_exact_instance(page, report_driver):
    receipt = report_driver.submit_fixture(page, INSTANCE_ID, LABEL, confirmed_fault())
    assert receipt.status == "SUBMITTED"
    assert receipt.instance_id == INSTANCE_ID

def test_wrong_target_blocks_click(page, report_driver):
    page.evaluate("window.fixture.instanceId = 999")
    with pytest.raises(TargetMismatch):
        report_driver.submit_fixture(page, INSTANCE_ID, LABEL, confirmed_fault())
    assert page.locator("[data-report-submitted=true]").count() == 0
```

- [ ] **Step 2: Implement fixture and driver**

Driver accepts only a frozen confirmed-fault bundle, validates instance ID and label in page text, captures redacted before/after screenshots, clicks fixture Report button, and records confirmation within 60 seconds. Live mode requires a genuine provider URL and never runs against synthetic faults.

- [ ] **Step 3: Run Playwright fixture validation**

Run: `python -m pytest -q tests/unit/test_desktop_report.py tests/e2e/test_report_fixture.py`  
Expected: PASS.  
Run: `python scripts/preflight_report.py --fixture --json artifacts/report-fixture-attestation.json`  
Expected: receipt and redacted screenshots; no provider request submitted.

- [ ] **Step 4: Commit**

```bash
git add src/srecon26_poc/desktop_report.py browser scripts/preflight_report.py tests docs/runbooks/provider-reporting.md
git commit -s -m "feat: add exact-target report adapter"
```

### Task 4: KVM Capability Gate and Single-Node k3s Manifests

**Files:**
- Create: `infra/k3s/bootstrap.sh`
- Create: `infra/k3s/nvidia-runtime.toml`
- Create: `infra/k3s/namespace.yaml`
- Create: `infra/k3s/device-plugin.yaml`
- Create: `infra/k3s/vllm.yaml`
- Create: `infra/k3s/prometheus.yaml`
- Create: `infra/k3s/metrics-server.yaml`
- Create: `infra/k3s/prometheus-adapter.yaml`
- Create: `infra/k3s/hpa-observer.yaml`
- Create: `src/srecon26_poc/canary.py`
- Create: `tests/unit/test_canary.py`
- Create: `tests/integration/test_k3s_manifests.py`

**Interfaces:**
- Produces: `evaluate_kvm_capabilities(facts: KvmFacts) -> CapabilityDecision` and `evaluate_canary_readiness(snapshot: CanarySnapshot) -> ReadinessDecision`.

- [ ] **Step 1: Write failing capability and readiness tests**

```python
def test_kvm_gate_requires_full_host_capabilities():
    facts = KvmFacts(systemd=True, cgroup_v2=True, containerd=True, nvidia_smi=True, cuda=True, privileged=True)
    assert evaluate_kvm_capabilities(facts).allowed

@pytest.mark.parametrize("missing", ["systemd", "cgroup_v2", "containerd", "nvidia_smi", "cuda", "privileged"])
def test_kvm_gate_rejects_missing_capability(missing, full_facts):
    assert not evaluate_kvm_capabilities(replace(full_facts, **{missing: False})).allowed
```

- [ ] **Step 2: Implement evaluators and manifests**

Pin vLLM image digest and model revision. Device plugin must expose `nvidia.com/gpu: 1`; vLLM pod must request it. Prometheus targets remain cluster-local. Metrics-server feeds CPU resource metrics; Prometheus Adapter exposes vLLM queue and KV custom metrics. SSH tunnel is sole access path.

- [ ] **Step 3: Run offline render and schema tests**

Run: `python -m pytest -q tests/unit/test_canary.py tests/integration/test_k3s_manifests.py`  
Expected: PASS.  
Run: `kubectl apply --dry-run=client -f infra/k3s/`  
Expected: manifests validate.  
Run: `semgrep --config auto infra scripts src tests`  
Expected: no unresolved high-severity finding.

- [ ] **Step 4: Commit**

```bash
git add infra/k3s src/srecon26_poc/canary.py tests
git commit -s -m "feat: define gated k3s GPU canary"
```

### Task 5: Paid GPU/CUDA Smoke and Metric-Path Canary

**Files:**
- Create: `scripts/run_canary.py`
- Create: `tests/integration/test_canary_runner.py`
- Create: `docs/runbooks/live-canary.md`

**Interfaces:**
- Consumes: foundation controller, Vast provider, guard, report driver, canary evaluator.
- Produces: finalized `gpu-smoke` and `metric-path` run bundles.

- [ ] **Step 1: Write failing runner fixture tests**

```python
def test_canary_stops_before_k3s_when_kvm_probe_fails(canary_harness):
    result = canary_harness.run(stage="metric-path", facts={"privileged": False})
    assert result.k3s_calls == 0
    assert result.destroyed_and_absent

def test_metric_path_requires_all_observation_layers(canary_harness):
    result = canary_harness.run(stage="metric-path", missing="custom_metrics_api")
    assert not result.claims.real_gpu_metric_path
```

- [ ] **Step 2: Implement bounded runner**

Runner calculates immutable report and teardown deadlines before create. GPU smoke captures provider identity, `nvidia-smi`, CUDA, KVM facts, spend, report classification, teardown, and absence. Metric-path stage begins only after successful smoke finalization and captures k3s node GPU, device plugin, vLLM pod, one warm-up and measured request, Prometheus, metrics APIs, HPA, events, spend, and teardown.

- [ ] **Step 3: Run offline paid-path simulation**

Run: `python -m pytest -q tests/integration/test_canary_runner.py`  
Expected: PASS for success, confirmed SKU fault, unresolved diagnosis, report timeout, create ambiguity, and teardown deadline fixtures.

- [ ] **Step 4: Execute live stages only when gates pass**

Run: `python scripts/run_canary.py --stage gpu-smoke --reserve 1.00 --require-zero-instances`  
Expected: complete smoke bundle and three absence reads; otherwise safe terminal limitation bundle.  
Run only after smoke passes: `python scripts/run_canary.py --stage metric-path --reserve 1.00 --require-zero-instances`  
Expected: full metric-path bundle and three absence reads; otherwise safe terminal limitation bundle.

- [ ] **Step 5: Commit code and external root anchors, never mutable paid artifacts**

```bash
git add scripts/run_canary.py tests/integration/test_canary_runner.py docs/runbooks/live-canary.md evidence-root-anchors
git commit -s -m "feat: run bounded GPU metric canary"
```

### Task 6: Conditional Three-Block HPA Comparison

**Files:**
- Create: `src/srecon26_poc/comparison.py`
- Create: `scripts/run_comparison.py`
- Create: `tests/unit/test_comparison.py`
- Create: `tests/integration/test_comparison_runner.py`
- Create: `docs/runbooks/paired-comparison.md`

**Interfaces:**
- Produces: `validate_comparison_plan(plan: ComparisonPlan, remaining: Decimal) -> ComparisonDecision` and three-block comparison bundle.

- [ ] **Step 1: Write failing comparability and budget tests**

```python
@pytest.mark.parametrize("changed", ["model_revision", "image_digest", "gpu_class", "trace_hash", "window_seconds"])
def test_comparison_rejects_changed_control(changed, valid_plan):
    assert not validate_comparison_plan(change(valid_plan, changed), Decimal("3.00")).allowed

def test_comparison_requires_three_complete_paired_blocks(valid_result):
    valid_result.blocks.pop()
    assert analyze_comparison(valid_result).classification == "EXPLORATORY_ONLY"

def test_comparison_rejects_reservation_over_remaining(valid_plan):
    assert not validate_comparison_plan(valid_plan, Decimal("2.99")).allowed
```

- [ ] **Step 2: Implement plan validator and runner**

Require two compatible ready GPU capacities, same pinned controls, fixed paired order A/B, B/A, A/B, full queue/TTFT/TPOT/GPU/KV/CPU/desired/ready records, and remaining reservation at most `$3.00`. Partial data cannot emit comparative claim.

- [ ] **Step 3: Run fixture suite**

Run: `python -m pytest -q tests/unit/test_comparison.py tests/integration/test_comparison_runner.py`  
Expected: PASS.

- [ ] **Step 4: Execute only if feasibility decision is allowed**

Run: `python scripts/run_comparison.py --reserve 3.00 --blocks 3 --order AB,BA,AB`  
Expected: three complete blocks, finalized bundle, teardown and absence for every capacity; otherwise explicit exploratory/blocked result with no comparison claim.

- [ ] **Step 5: Commit**

```bash
git add src/srecon26_poc/comparison.py scripts/run_comparison.py tests docs/runbooks/paired-comparison.md evidence-root-anchors
git commit -s -m "feat: add guarded paired HPA comparison"
```

