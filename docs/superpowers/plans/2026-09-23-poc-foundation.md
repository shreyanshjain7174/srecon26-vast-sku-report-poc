# PoC Safety Foundation and Local Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build crash-safe experiment control, a strict $5 exposure ledger, report-before-destroy ordering, and independently validated local CPU/queue/KV HPA evidence.

**Architecture:** A Python package owns typed contracts, an append-only hash-chained journal, budget decisions, provider/guard/report protocols, and local evidence evaluation. Kubernetes manifests and runner scripts remain thin adapters; all safety decisions are exercised through deterministic fakes before any provider operation.

**Tech Stack:** Python 3.14, standard library dataclasses/protocols/Decimal, pytest 9, Kubernetes `autoscaling/v2`, Prometheus Adapter, Bash only for bounded process wrappers.

**Spec:** `docs/superpowers/specs/2026-09-23-srecon26-llm-hpa-poc-design.md`

## Global Constraints

- New Vast.ai exposure across this repository is capped at exactly `$5.00`; all arithmetic uses `Decimal`.
- No paid create occurs until independent guard, report adapter, budget, security, and offline test gates pass.
- Exactly one provider create intent and request are allowed per run; ambiguous create is reconciled and never retried.
- Provider reporting occurs only for `PROVIDER_FAULT_CONFIRMED`, never for `DIAGNOSIS_UNRESOLVED`.
- Normal teardown validates exact instance ID plus nonce-bound label; break-glass teardown uses the same ownership contract.
- Every paid run requires three distinct absence reads before terminal success.
- Persisted prose, code, comments, commits, and third-party messages use normal professional language.
- Every commit uses `git commit -s`; no Codex coauthor trailer.

## Review Focus

- Crash after create intent but before response must resume without issuing a second create; Task 3 pins this.
- A `$5.01` cumulative reservation must fail even if earlier actual spend was lower but unverified; Task 2 pins this.
- Controller/network/model failures must never produce a false provider report; Task 3 pins this.
- Wrong instance ID, label, or nonce must prevent both report click and teardown; Task 3 pins this.
- Local signal independence must fail when either non-target metric crosses its safety margin; Task 4 pins this.

---

## File Structure

```text
pyproject.toml
Makefile
.gitignore
README.md
src/srecon26_poc/
  __init__.py
  types.py
  journal.py
  budget.py
  contracts.py
  provider.py
  guard.py
  reporting.py
  controller.py
  local_hpa.py
k8s/local/
  namespace.yaml
  base.yaml
  hpa-cpu.yaml
  hpa-queue.yaml
  hpa-kv.yaml
  prometheus.yaml
  prometheus-adapter.yaml
scripts/
  run_local_arm.py
  collect_local_evidence.py
tests/
  conftest.py
  unit/
  integration/
  fixtures/
```

### Task 1: Project Scaffold, Types, and Hash-Chained Journal

**Files:**
- Create: `pyproject.toml`
- Create: `Makefile`
- Create: `.gitignore`
- Create: `README.md`
- Create: `src/srecon26_poc/__init__.py`
- Create: `src/srecon26_poc/types.py`
- Create: `src/srecon26_poc/journal.py`
- Create: `tests/unit/test_journal.py`

**Interfaces:**
- Produces: `RunState`, `TerminalStatus`, `FaultClass`, `RunIdentity`, `TransitionEvent`, and `RunJournal`.
- `RunJournal.append(to_state: RunState, event_type: str, payload: Mapping[str, JSONValue], wall_time: datetime, monotonic_ns: int) -> TransitionEvent`
- `RunJournal.state() -> RunState`
- `RunJournal.events() -> tuple[TransitionEvent, ...]`

- [ ] **Step 1: Write failing transition and resume tests**

```python
def test_journal_rejects_skipped_transition(tmp_path, identity):
    journal = RunJournal.create(tmp_path, identity)
    with pytest.raises(InvalidTransition):
        journal.append(RunState.GUARD_ARMED, "guard.armed", {}, UTC, 1)

def test_create_intent_survives_reopen(tmp_path, identity):
    journal = RunJournal.create(tmp_path, identity)
    advance_to_offer_pinned(journal)
    journal.append(RunState.GUARD_ARMED, "guard.armed", {}, UTC, 10)
    journal.append(RunState.CREATE_REQUESTED, "provider.create_intent", {"key": "run-1"}, UTC, 11)
    assert RunJournal.open(tmp_path).events()[-1].payload["key"] == "run-1"
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python -m pytest -q tests/unit/test_journal.py`  
Expected: collection fails because `srecon26_poc.journal` does not exist.

- [ ] **Step 3: Implement typed states and journal**

Use frozen dataclasses. Serialize each event as canonical JSON with `sequence`, `previous_hash`, and `event_hash`; write one line, flush, and `os.fsync`. On open, verify sequence, hash chain, allowed transition, monotonic time, and run ID before returning events. Allowed states exactly match design state machine, including `CAPTURING_FAULT`, `REPORTING_FAULT`, `DESTROYING`, and `ABSENCE_VERIFYING`.

```python
class RunState(StrEnum):
    NEW = "NEW"
    OFFLINE_VALIDATED = "OFFLINE_VALIDATED"
    BUDGET_RESERVED = "BUDGET_RESERVED"
    OFFER_PINNED = "OFFER_PINNED"
    REPORT_ADAPTER_READY = "REPORT_ADAPTER_READY"
    GUARD_ARMED = "GUARD_ARMED"
    CREATE_REQUESTED = "CREATE_REQUESTED"
    CREATED_VERIFYING = "CREATED_VERIFYING"
    RUNNING_CANARY = "RUNNING_CANARY"
    CAPTURING_FAULT = "CAPTURING_FAULT"
    REPORTING_FAULT = "REPORTING_FAULT"
    COLLECTING = "COLLECTING"
    DESTROYING = "DESTROYING"
    ABSENCE_VERIFYING = "ABSENCE_VERIFYING"
    TERMINAL = "TERMINAL"
```

- [ ] **Step 4: Run focused and full tests**

Run: `python -m pytest -q tests/unit/test_journal.py`  
Expected: PASS.  
Run: `git diff --check`  
Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml Makefile .gitignore README.md src/srecon26_poc tests/unit/test_journal.py
git commit -s -m "feat: add crash-safe run journal"
```

### Task 2: Budget Ledger and Provider Contracts

**Files:**
- Create: `src/srecon26_poc/budget.py`
- Create: `src/srecon26_poc/contracts.py`
- Create: `src/srecon26_poc/provider.py`
- Create: `tests/unit/test_budget.py`
- Create: `tests/unit/test_contracts.py`
- Create: `tests/fixtures/offers/rtx3090.json`
- Create: `tests/fixtures/instances/rtx3090-running.json`

**Interfaces:**
- Consumes: `RunIdentity`, `FaultClass` from Task 1.
- Produces: `ExposureLedger.reserve`, `ExposureLedger.commit_actual`, `OfferContract`, `InstanceContract`, `classify_fault`, and `Provider` protocol.
- `Provider.create_once(contract: OfferContract, request_key: str) -> InstanceContract`

- [ ] **Step 1: Write failing budget and classification tests**

```python
def test_cumulative_reservation_above_five_dollars_fails(tmp_path):
    ledger = ExposureLedger(tmp_path / "ledger.json")
    ledger.reserve("run-a", Decimal("2.00"), "gpu-smoke")
    with pytest.raises(BudgetExceeded):
        ledger.reserve("run-b", Decimal("3.01"), "canary")

@pytest.mark.parametrize("field", ["gpu_name", "num_gpus", "gpu_ram_mib", "compute_capability", "machine_id", "dph_total", "label"])
def test_offer_contract_mismatch_is_provider_fault(field, offer, instance):
    instance = replace_field(instance, field)
    assert classify_fault(offer, instance, ProbeOutcome.PASS) is FaultClass.PROVIDER_FAULT_CONFIRMED

def test_model_download_failure_is_unresolved(offer, instance):
    assert classify_fault(offer, instance, ProbeOutcome.MODEL_DOWNLOAD_FAILED) is FaultClass.DIAGNOSIS_UNRESOLVED
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python -m pytest -q tests/unit/test_budget.py tests/unit/test_contracts.py`  
Expected: import failure for missing modules.

- [ ] **Step 3: Implement ledger, contracts, and protocol**

Use `Decimal(str(value))` at JSON boundaries. Ledger writes atomically through `os.replace` and `fsync`; committed actual spend never releases remaining reservation until a three-read absence proof is attached. `create_once` documents `AmbiguousCreate` as non-retryable.

```python
MAX_EXPOSURE = Decimal("5.00")

class Provider(Protocol):
    def account_snapshot(self) -> AccountSnapshot: ...
    def list_instances(self) -> tuple[InstanceContract, ...]: ...
    def get_offer(self, offer_id: int) -> OfferContract: ...
    def create_once(self, contract: OfferContract, request_key: str) -> InstanceContract: ...
    def get_instance(self, instance_id: int) -> InstanceContract: ...
    def destroy_exact(self, instance_id: int, expected_label: str) -> None: ...
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest -q tests/unit/test_budget.py tests/unit/test_contracts.py`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/srecon26_poc/budget.py src/srecon26_poc/contracts.py src/srecon26_poc/provider.py tests
git commit -s -m "feat: enforce exposure and provider contracts"
```

### Task 3: Guard, Report Gate, and Idempotent Controller

**Files:**
- Create: `src/srecon26_poc/guard.py`
- Create: `src/srecon26_poc/reporting.py`
- Create: `src/srecon26_poc/controller.py`
- Create: `tests/conftest.py`
- Create: `tests/unit/test_guard.py`
- Create: `tests/unit/test_reporting.py`
- Create: `tests/unit/test_controller.py`
- Create: `tests/integration/test_delayed_create.py`
- Create: `tests/integration/test_sku_report_order.py`
- Create: `tests/integration/test_report_timeout.py`
- Create: `tests/integration/test_absence_quorum.py`

**Interfaces:**
- Consumes: journal, ledger, contracts, and provider protocol from Tasks 1–2.
- Produces: `Guard`, `DesktopReportAdapter`, `ReportGate`, and `ExperimentController.run_or_resume()`.
- `ReportGate.handle(fault: FaultRecord, report_start_by: datetime) -> ReportReceipt`
- `ExperimentController.prove_absent(instance_id: int, label: str, reads: int = 3) -> AbsenceProof`

- [ ] **Step 1: Write failing order, crash, ownership, and timeout tests**

```python
def test_confirmed_sku_fault_reports_before_destroy(harness):
    result = harness.run(instance_override={"gpu_name": "RTX 3080"})
    assert result.events == [
        "fault.captured", "report.before", "report.submitted",
        "report.confirmed", "provider.destroy_exact",
        "provider.absent.1", "provider.absent.2", "provider.absent.3",
    ]

def test_unresolved_probe_never_submits_provider_report(harness):
    result = harness.run(probe="MODEL_DOWNLOAD_FAILED")
    assert "report.submitted" not in result.events
    assert result.terminal_status == "FAILED_SAFE"

def test_resume_after_ambiguous_create_does_not_create_twice(harness):
    harness.provider.raise_ambiguous_after_create = True
    harness.run_once_expect_interruption()
    harness.reopen().run_or_resume()
    assert harness.provider.create_calls == 1
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python -m pytest -q tests/unit/test_guard.py tests/unit/test_reporting.py tests/unit/test_controller.py tests/integration`  
Expected: missing module failures.

- [ ] **Step 3: Implement protocols and controller**

Journal intent before every external side effect. Guard attestation must include independent host identity, script hash, nonce, label, fixed hard deadline, and last heartbeat. Report adapter preflight must target exact instance fixture. Report timeout returns `REPORT_UNCONFIRMED` and teardown authorization. Absence proof requires three separately timestamped inventory reads.

```python
class Guard(Protocol):
    def preflight(self) -> GuardAttestation: ...
    def arm(self, identity: RunIdentity, hard_deadline: datetime) -> GuardAttestation: ...
    def record_heartbeat(self, identity: RunIdentity, monotonic_ns: int) -> None: ...

class DesktopReportAdapter(Protocol):
    def preflight_exact_instance(self, instance_id: int, label: str) -> None: ...
    def capture_before(self, fault: FaultRecord) -> Path: ...
    def submit(self, fault: FaultRecord) -> ReportReceipt: ...
    def capture_after(self, receipt: ReportReceipt) -> Path: ...
```

- [ ] **Step 4: Run focused and full tests**

Run: `python -m pytest -q tests/unit/test_guard.py tests/unit/test_reporting.py tests/unit/test_controller.py tests/integration`  
Expected: PASS.  
Run: `python -m pytest -q`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/srecon26_poc tests
git commit -s -m "feat: enforce report-first guarded lifecycle"
```

### Task 4: Independent Local HPA Arms and Evaluation

**Files:**
- Create: `src/srecon26_poc/local_hpa.py`
- Create: `k8s/local/namespace.yaml`
- Create: `k8s/local/base.yaml`
- Create: `k8s/local/hpa-cpu.yaml`
- Create: `k8s/local/hpa-queue.yaml`
- Create: `k8s/local/hpa-kv.yaml`
- Create: `k8s/local/prometheus.yaml`
- Create: `k8s/local/prometheus-adapter.yaml`
- Create: `scripts/run_local_arm.py`
- Create: `scripts/collect_local_evidence.py`
- Create: `tests/unit/test_local_hpa.py`
- Create: `tests/integration/test_local_arm_evidence.py`
- Create: `tests/fixtures/metrics/local-{cpu,queue,kv}.json`

**Interfaces:**
- Consumes: run identity and journal from Task 1.
- Produces: `evaluate_arm(arm: LocalArm, samples: Sequence[Sample]) -> ArmEvaluation` and raw local bundle directories.

- [ ] **Step 1: Write failing independence tests**

```python
@pytest.mark.parametrize("arm", [LocalArm.CPU, LocalArm.QUEUE, LocalArm.KV])
def test_each_arm_scales_independently(arm, isolated_samples):
    result = evaluate_arm(arm, isolated_samples[arm])
    assert result.negative_control_seconds >= 90
    assert result.target_margin >= Decimal("0.20")
    assert result.non_target_margin >= Decimal("0.20")
    assert result.desired_transition == (1, 2)
    assert result.ready_transition == (1, 2)
    assert result.reconcile_intervals <= 4

def test_non_target_crossing_margin_invalidates_independence(queue_samples):
    queue_samples[-1].cpu_ratio = Decimal("0.85")
    assert not evaluate_arm(LocalArm.QUEUE, queue_samples).independent
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python -m pytest -q tests/unit/test_local_hpa.py tests/integration/test_local_arm_evidence.py`  
Expected: missing module failures.

- [ ] **Step 3: Implement evaluator, separate manifests, and runners**

Use `autoscaling/v2`. CPU uses resource average utilization. Queue and KV each use distinct Prometheus Adapter custom metrics and distinct HPA manifests. Runners record 15-second scrape/reconcile samples, a 90-second negative control, injection, desired/ready transitions, and Kubernetes events. They refuse to touch a cluster lacking run ownership label.

- [ ] **Step 4: Run static and fixture validation**

Run: `python -m pytest -q tests/unit/test_local_hpa.py tests/integration/test_local_arm_evidence.py`  
Expected: PASS.  
Run: `kubectl apply --dry-run=client -f k8s/local/`  
Expected: all resources validate.

- [ ] **Step 5: Run owned local rehearsal**

Run: `python scripts/run_local_arm.py --arm cpu --output artifacts/runs/local-cpu` then repeat with `queue` and `kv`.  
Expected: each command exits zero only after negative control and independent `1→2` desired/ready transitions; it writes no synthetic result when cluster execution fails.

- [ ] **Step 6: Commit**

```bash
git add src/srecon26_poc/local_hpa.py k8s/local scripts tests
git commit -s -m "feat: prove independent local HPA signals"
```

### Task 5: Foundation Quality and No-Spend Gate

**Files:**
- Modify: `Makefile`
- Modify: `README.md`
- Create: `.semgrep.yml`
- Create: `scripts/prepaid_gate.py`
- Create: `tests/integration/test_prepaid_gate.py`

**Interfaces:**
- Consumes: all foundation tests and controller preflight.
- Produces: `python scripts/prepaid_gate.py --json artifacts/prepaid-gate.json`; exit zero is necessary but not sufficient for live spend.

- [ ] **Step 1: Write failing no-spend gate test**

```python
def test_gate_blocks_missing_report_guard_or_security_evidence(tmp_path):
    result = run_gate(tmp_path, guard=False, report_fixture=False, semgrep=False)
    assert result.exit_code == 1
    assert set(result.missing) == {"guard_attestation", "report_fixture", "semgrep"}
```

- [ ] **Step 2: Implement gate and Make targets**

Gate checks full pytest result, Semgrep JSON, local-arm validation, current zero-instance inventory snapshot, `$5.00` ledger state, independent guard attestation, and report-adapter fixture receipt. It never creates or destroys provider resources.

- [ ] **Step 3: Run all gates**

Run: `python -m pytest -q`  
Run: `ruff check .`  
Run: `mypy src`  
Run: `semgrep --config .semgrep.yml --json --output artifacts/semgrep.json src tests scripts`  
Run: `python scripts/prepaid_gate.py --json artifacts/prepaid-gate.json`  
Expected: tests, lint, types, and scan pass; prepaid gate may remain blocked only for live guard/report attestations, with exact missing fields recorded.

- [ ] **Step 4: Commit**

```bash
git add Makefile README.md .semgrep.yml scripts/prepaid_gate.py tests/integration/test_prepaid_gate.py
git commit -s -m "test: add paid-run safety gate"
```

