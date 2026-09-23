# Architecture Research

**Domain:** Evidence pipeline + Kubernetes HPA experimentation PoC
**Researched:** 2026-09-23
**Confidence:** HIGH (derived directly from project requirements and key decisions)

## Standard Architecture

### System Overview

```
┌───────────────────────────────────────────────────────────────────┐
│                       SAFETY ENVELOPE                             │
│  ┌──────────────┐  ┌──────────────────┐  ┌──────────────────┐    │
│  │ Budget Ledger│  │  Independent     │  │ Credential       │    │
│  │ (Decimal)    │  │  Teardown Guard  │  │ Manager          │    │
│  │  $5.00 cap   │  │  (remote host)   │  │ (0600 secret)    │    │
│  └──────┬───────┘  └────────┬─────────┘  └────────┬─────────┘    │
│         │ blocks create      │ armed first          │ never logged │
└─────────┼────────────────────┼──────────────────────┼─────────────┘
          ↓                    ↓                      ↓
┌───────────────────────────────────────────────────────────────────┐
│                    EXPERIMENT CONTROLLER                           │
│  ┌────────────────────────────────────────────────────────────┐   │
│  │  Hash-Chained Append-Only Run Journal                      │   │
│  │  seq + previous_hash → crash-safe + idempotent resume      │   │
│  └────────────────────────────────────────────────────────────┘   │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐    │
│  │  Local Arms  │  │  GPU Canary  │  │  Paired Comparison   │    │
│  │  (CPU/Q/KV)  │  │  (Vast.ai)   │  │  (conditional A/B/A) │    │
│  └──────┬───────┘  └──────┬───────┘  └──────────┬───────────┘    │
└─────────┼─────────────────┼───────────────────────┼───────────────┘
          ↓                 ↓                       ↓
┌───────────────────────────────────────────────────────────────────┐
│                     SIGNAL & ARTIFACT LAYER                       │
│  ┌─────────────┐  ┌─────────────────────────────────────────┐    │
│  │ Local k3s   │  │ Remote k3s (Vast.ai RTX 3090)           │    │
│  │ mock HPA    │  │ vLLM v0.26.0 → Prometheus →             │    │
│  │ arms ×3     │  │ custom-metrics API → HPA autoscaling/v2  │    │
│  └──────┬──────┘  └──────────────────┬──────────────────────┘    │
└─────────┼─────────────────────────────┼─────────────────────────  ┘
          ↓                             ↓
┌───────────────────────────────────────────────────────────────────┐
│                    EVIDENCE INTEGRITY SYSTEM                       │
│  ┌────────────────────────────────────────────────────────────┐   │
│  │  SHA256SUMS + ROOT-HASH.txt → signed git commit anchor     │   │
│  │  Provenance labels: LOCAL_SYNTHETIC vs GPU_REAL            │   │
│  │  Analyzer: rejects missing | mixed | out-of-window |       │   │
│  │            non-monotonic | checksum-mismatch artifacts     │   │
│  └────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────┬────────────────────────────┘
                                       ↓
┌───────────────────────────────────────────────────────────────────┐
│                     DECK GENERATOR                                 │
│  ┌────────────────────────────────────────────────────────────┐   │
│  │  Claim-gated: slides 7–13 only from validated result files │   │
│  │  If results absent → substitute limitation slide           │   │
│  │  Output: 16-slide 16:9 .pptx with speaker notes            │   │
│  └────────────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────────┘
```

### Component Responsibilities

| Component | Responsibility | Implementation |
|-----------|----------------|----------------|
| Budget Ledger | Track all Vast.ai exposure; block create if reservation would breach $5.00 | Python dataclass + `Decimal`; serializes to JSON with string-repr amounts |
| Teardown Guard | Independent host that arms a deadline before every paid create; fires teardown on heartbeat loss or hard TTL | Separate always-on host; never laptop; authority activates only after loss/deadline |
| Credential Manager | Read provider key from root-owned `0600` file; never pass via args, env, or logs | stdlib only; pathlib.read_text behind a permission check at startup |
| Experiment Controller | Orchestrate create/configure/observe/teardown sequence; consult budget + guard before acting | Python state machine backed by the journal |
| Run Journal | Append-only, hash-chained event log; seq + previous_hash per entry | Plain JSON-lines file; no external DB; SHA256 of prior entry as chain link |
| Local HPA Arms (×3) | CPU arm, Queue-depth arm, Synthetic-KV arm — each drives 1→2 replica transition independently with non-target signals held below margins | Local k3s + mock exporter; three separate pytest-invocable experiments |
| GPU Canary | End-to-end metric path: vLLM → Prometheus → custom-metrics API → autoscaling/v2 HPA on real RTX 3090 | Vast.ai single-node k3s; pinned image digest revalidated pre-run |
| Paired Comparison | CPU-only HPA vs queue/KV-aware HPA; three complete A/B/A–B/A/B blocks | Conditional on topology + budget; exploratory label if < 3 blocks |
| Integrity Bundle | SHA256SUMS + ROOT-HASH.txt per run; anchored in signed git commit; copied to guard journal | hashlib stdlib; git commit -s; guard receives bundle via SSH/HTTPS |
| Analyzer | Validate provenance, timestamps, ordering, checksums, root hash; reject on any violation | Pure Python, no external deps; returns typed result with rejection reason |
| Deck Generator | Assemble 16-slide 16:9 .pptx with speaker notes; gate claims against validated result files | python-pptx; slides 7–13 require validated artifacts; unsupported claims → limitation slide |

## Recommended Project Structure

```
srecon26-vast-sku-report-poc/
├── poc/
│   ├── safety/
│   │   ├── budget.py          # Decimal ledger, $5.00 cap, reservation/commit/release
│   │   ├── guard.py           # Guard client: arm, heartbeat, release
│   │   ├── credentials.py     # 0600 secret file reader
│   │   └── semgrep_gate.py    # Pre-run security scan assertion
│   ├── journal/
│   │   ├── journal.py         # Append-only, hash-chained event log
│   │   └── models.py          # JournalEntry dataclass (seq, previous_hash, payload)
│   ├── controller/
│   │   ├── controller.py      # Experiment orchestrator state machine
│   │   ├── provider.py        # Vast.ai adapter (create, status, destroy, report)
│   │   └── reconcile.py       # Label-based reconciliation; no retry on ambiguous create
│   ├── arms/
│   │   ├── cpu_arm.py         # CPU signal arm: drives 1→2 replica, holds Q+KV below margins
│   │   ├── queue_arm.py       # Queue-depth arm: drives 1→2 replica, holds CPU+KV below margins
│   │   └── kv_arm.py          # Synthetic-KV arm: drives 1→2 replica, holds CPU+Q below margins
│   ├── canary/
│   │   ├── canary.py          # GPU canary orchestrator (SSH, k3s config, observe HPA)
│   │   ├── image_pin.py       # Revalidate pinned digest before each paid run
│   │   └── prom_scraper.py    # Prometheus → custom-metrics API verification
│   ├── comparison/
│   │   ├── ab_runner.py       # A/B/A block runner; enforces 3-block minimum
│   │   └── topology_check.py  # Verify two compatible GPU capacities before arming
│   ├── integrity/
│   │   ├── bundle.py          # SHA256SUMS + ROOT-HASH.txt generation
│   │   ├── anchor.py          # Git commit anchor + guard journal copy
│   │   └── analyzer.py        # Provenance / timestamp / checksum / root-hash validator
│   └── deck/
│       ├── generator.py       # 16-slide .pptx assembler with claim-gating logic
│       ├── claim_gate.py      # Validates each claim against validated result files
│       └── templates/         # Slide layout templates
├── guard_host/
│   └── guard_server.py        # Always-on host daemon: arm/heartbeat/teardown API
├── tests/
│   ├── test_budget.py
│   ├── test_journal.py
│   ├── test_arms.py
│   ├── test_analyzer.py
│   └── test_deck_gate.py
├── artifacts/                 # Run artifacts (gitignored content, not structure)
│   └── .gitkeep
└── .planning/
    └── research/
        └── ARCHITECTURE.md    # This file
```

### Structure Rationale

- **poc/safety/**: All budget + guard + credential code isolated from experiment logic — the safety envelope must be auditable independently of what runs inside it.
- **poc/journal/**: Append-only log is its own module because crash safety and tamper-evidence are cross-cutting concerns; every other module writes to it, none reads their own state from elsewhere.
- **poc/arms/**: Three separate files because each arm's independence proof requires independent code paths — shared helpers are fine, but each arm's metric driver and margin enforcement must be readable in isolation.
- **poc/integrity/**: Separated from arms/canary because the integrity check is the last gate before any claim propagates to the deck; it must be callable without re-running experiments.
- **guard_host/**: Physically separate from poc/ to enforce the architectural invariant that the guard can be deployed to a different machine without pulling in experiment dependencies.

## Architectural Patterns

### Pattern 1: Hash-Chained Append-Only Journal

**What:** Every journal event carries `seq` (monotonically increasing) and `previous_hash` (SHA256 of the prior serialized entry). The chain is verifiable from any checkpoint.

**When to use:** Any state that must survive controller crashes and be verifiable post-hoc. Used for every experiment lifecycle event (create, arm, observe, teardown, report).

**Trade-offs:** Reads require scanning from the head (or a trusted checkpoint); no random-access updates. Acceptable because the journal is small (<1000 entries per project) and append-only semantics are exactly what crash-resume and tamper-evidence require.

**Example:**
```python
@dataclass
class JournalEntry:
    seq: int
    previous_hash: str        # SHA256 of prior entry's canonical JSON
    event_type: str
    payload: dict
    timestamp_utc: str        # ISO-8601, never relative

def append(journal_path: Path, event_type: str, payload: dict) -> JournalEntry:
    prior = _read_last(journal_path)
    entry = JournalEntry(
        seq=prior.seq + 1,
        previous_hash=_sha256(prior),
        event_type=event_type,
        payload=payload,
        timestamp_utc=datetime.utcnow().isoformat(),
    )
    with journal_path.open("a") as f:
        f.write(json.dumps(asdict(entry)) + "\n")
    return entry
```

### Pattern 2: Two-Phase Safety Check Before Any Paid Create

**What:** Before issuing any provider create, the controller must: (1) reserve budget in the Decimal ledger, (2) arm the independent teardown guard and confirm it is reachable. Only after both succeed does the create proceed.

**When to use:** Every call path that could result in a Vast.ai instance creation.

**Trade-offs:** Adds latency (a round-trip to the guard host) before every create. This is intentional — the cost of skipping the check is unbounded provider spend without a safety net.

**Example:**
```python
def create_instance(spec: InstanceSpec) -> Instance:
    reservation = budget.reserve(spec.estimated_cost_usd)  # raises if over $5.00
    try:
        guard.arm(deadline=spec.hard_ttl, on_miss="teardown")
        guard.assert_reachable()  # raises if guard host is unreachable
    except GuardUnreachableError:
        budget.release(reservation)
        raise
    instance = provider.create(spec)          # label-based; never retry on ambiguity
    journal.append("INSTANCE_CREATED", {...})
    return instance
```

### Pattern 3: Report-Before-Destroy Contract

**What:** On `PROVIDER_FAULT_CONFIRMED`, the sequence is: capture evidence → click Report → teardown. Teardown never precedes the report. `DIAGNOSIS_UNRESOLVED` never triggers the report path.

**When to use:** Any instance that reaches a fault diagnosis state.

**Trade-offs:** Extends instance lifetime by a bounded report window. This is accounted for by subtracting `report_window + teardown_margin` from the hard TTL when budgeting time.

**Example:**
```python
def handle_diagnosis(instance: Instance, diagnosis: Diagnosis) -> None:
    if diagnosis == Diagnosis.PROVIDER_FAULT_CONFIRMED:
        evidence = capture_evidence(instance)        # screenshots, logs, dmesg
        provider.report(instance, evidence=evidence) # Report button
        journal.append("FAULT_REPORTED", {...})
        provider.destroy(instance)                   # only after report
    elif diagnosis == Diagnosis.DIAGNOSIS_UNRESOLVED:
        journal.append("UNRESOLVED_NO_REPORT", {...})
        provider.destroy(instance)                   # teardown, no report
```

### Pattern 4: Provenance-Labeled Artifact

**What:** Every artifact file carries a provenance label (`LOCAL_SYNTHETIC` or `GPU_REAL`) in its metadata. The analyzer rejects any bundle that mixes provenance labels across artifacts claimed to support the same conclusion.

**When to use:** Every result file written by any arm or canary.

**Trade-offs:** Adds a required field to every artifact writer. The alternative — inferring provenance from file path or directory — would be fragile and easy to corrupt on copy.

**Example:**
```python
@dataclass
class ArtifactMetadata:
    provenance: Literal["LOCAL_SYNTHETIC", "GPU_REAL"]
    run_id: str
    captured_at_utc: str
    sha256: str

def write_artifact(path: Path, data: bytes, provenance: str) -> ArtifactMetadata:
    path.write_bytes(data)
    meta = ArtifactMetadata(
        provenance=provenance,
        run_id=current_run_id(),
        captured_at_utc=datetime.utcnow().isoformat(),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    path.with_suffix(".meta.json").write_text(json.dumps(asdict(meta)))
    return meta
```

### Pattern 5: Claim-Gated Deck Generation

**What:** Each slide in the 7–13 range declares which validated result file it depends on. The deck generator checks each dependency before rendering. A missing or unvalidated dependency substitutes a "limitation slide" rather than rendering unsupported content.

**When to use:** Deck generation only. Never used during experiment execution.

**Trade-offs:** Deck may have limitation slides if experiments don't complete. This is the correct behavior — presenting unsupported claims at a conference is a worse outcome than presenting a partial deck.

**Example:**
```python
SLIDE_DEPENDENCIES = {
    7:  "artifacts/local_cpu_arm_result.json",
    8:  "artifacts/local_queue_arm_result.json",
    9:  "artifacts/local_kv_arm_result.json",
    10: "artifacts/gpu_canary_result.json",
    11: "artifacts/paired_comparison_result.json",  # conditional
}

def render_slide(prs: Presentation, slide_num: int) -> None:
    dep = SLIDE_DEPENDENCIES.get(slide_num)
    if dep and not analyzer.validate(dep):
        render_limitation_slide(prs, slide_num, missing_dep=dep)
    else:
        render_evidence_slide(prs, slide_num, result_file=dep)
```

## Data Flow

### Experiment Lifecycle Flow

```
[Operator invokes controller]
    ↓
[Budget.reserve(estimated_cost)]  ← Decimal; raises BudgetBreachError if over $5.00
    ↓
[Guard.arm(deadline) + assert_reachable()]  ← Must succeed before create
    ↓
[Provider.create(spec, label=unique_label)]  ← Label-based; never retry ambiguity
    ↓
[Journal.append("INSTANCE_CREATED")]
    ↓
[Heartbeat loop (controller → guard)]
    ↓
[Experiment runs: observe metrics, drive HPA]
    ↓
[Artifacts written with provenance labels]
    ↓
[Diagnosis: PROVIDER_FAULT_CONFIRMED | DIAGNOSIS_UNRESOLVED | SUCCESS]
    ↓ (if PROVIDER_FAULT_CONFIRMED)
[CaptureEvidence → Provider.report() → Provider.destroy()]
    ↓ (otherwise)
[Provider.destroy()]
    ↓
[Budget.commit(actual_cost)]  ← Releases reservation, records actual spend
    ↓
[Guard.release()]
    ↓
[Integrity.bundle() → anchor in signed git commit → copy to guard journal]
```

### Evidence Validation Flow

```
[Run completes, artifacts on disk]
    ↓
[Analyzer.validate(bundle_dir)]
    ↓ checks:
    ├── All declared artifacts present
    ├── Each artifact SHA256 matches SHA256SUMS
    ├── ROOT-HASH.txt matches recomputed root hash
    ├── External anchor (git commit) matches ROOT-HASH.txt
    ├── All timestamps within run window (monotonic)
    ├── No mixed provenance within a conclusion group
    └── Guard journal copy matches local ROOT-HASH.txt
    ↓
[ValidationResult: VALID | REJECTED(reason)]
    ↓ (VALID)
[Mark result file as deck-eligible]
    ↓ (REJECTED)
[Log rejection reason; mark as ineligible; never silently degrade]
```

### Deck Assembly Flow

```
[generator.py invoked post-analysis]
    ↓
[For slides 1–6: render from static background/hypothesis templates]
    ↓
[For slides 7–13: check claim gate per slide]
    ├── Dependency validated? → render evidence slide from result file
    └── Dependency absent/invalid? → render limitation slide
    ↓
[For slides 14–16: future work / references / conclusion]
    ↓
[Write 16-slide 16:9 .pptx; validate Google Slides import via screenshot]
```

## Scaling Considerations

This is a single-run PoC, not a scaled service. Scaling considerations are about complexity growth, not user load.

| Concern | Current PoC | If Extended |
|---------|-------------|-------------|
| Journal size | <1000 entries; full scan acceptable | Add checkpoint every N entries |
| Artifact storage | <500MB per run; local disk | Move to content-addressed store (e.g. git-lfs) |
| Budget ledger | Single project, single file | Add per-experiment sub-ledgers with rollup |
| Guard host | Single guard for all experiments | Guard per concurrent experiment run |
| Parallel arms | Sequential (simplicity) | Could parallelize CPU/Queue/KV arms if run time matters |

## Anti-Patterns

### Anti-Pattern 1: Floating-Point Budget Arithmetic

**What people do:** Use Python `float` for cost tracking (`cost += 0.10`).

**Why it's wrong:** IEEE 754 rounding accumulates errors that can cause a $5.00 cap to pass $5.000000000000001 or fail at $4.999999999999999. With a hard $5.00 project cap, this is a correctness failure, not a minor inaccuracy.

**Do this instead:** Use `decimal.Decimal` throughout. Convert floats from provider APIs to `Decimal(str(value))` at the boundary — never `Decimal(float_value)` directly, which preserves the float's rounding error.

### Anti-Pattern 2: Retry on Ambiguous Create

**What people do:** When a provider create returns a timeout or ambiguous status, issue a second create request to "make sure."

**Why it's wrong:** The first create may have succeeded. A second create doubles the running instance count and the cost exposure. With a $5.00 total cap and $1–$3 per session, one accidental duplicate can exhaust the budget.

**Do this instead:** Reconcile by label. Every create carries a unique label. If a create returns ambiguously, query instances by label before deciding whether an instance exists. Never issue a second create until reconciliation confirms none exists.

### Anti-Pattern 3: Guard as a Notification, Not an Authority

**What people do:** Deploy the teardown guard but implement it as "send an alert to the operator" rather than "autonomously destroy the instance."

**Why it's wrong:** If the operator's laptop is asleep, the alert goes unseen. The guard's entire purpose is to act without the operator when the heartbeat goes silent. A notification-only guard provides no protection against unattended cost accumulation.

**Do this instead:** The guard must be able to call `provider.destroy(instance_id)` directly, using its own copy of the credential. It should alert as a side effect, but teardown must happen unconditionally once the authority condition is met.

### Anti-Pattern 4: Claiming GPU Evidence from Local Synthetic Runs

**What people do:** Run the local KV-cache arm, observe a 1→2 HPA transition, and present it in the deck as evidence of GPU metric-path behavior.

**Why it's wrong:** The local KV arm uses a synthetic metric exporter. It proves the HPA configuration responds to the signal, not that the signal flows from real GPU hardware through Prometheus to the custom-metrics API. These are different claims.

**Do this instead:** Label every artifact `LOCAL_SYNTHETIC` or `GPU_REAL`. The analyzer enforces that deck-eligible GPU claims must come from `GPU_REAL` artifacts. The local arms are evidence of HPA configuration correctness, clearly labeled as such.

### Anti-Pattern 5: Storing Credentials in the Journal

**What people do:** Log the full provider API request (including auth headers) to the journal for debugging.

**Why it's wrong:** The journal is the primary artifact. It gets anchored in git commits, copied to the guard host, and bundled into the evidence package. A credential in the journal propagates everywhere.

**Do this instead:** Redact the auth header before logging. The credential is read once at startup from the `0600` secret file and passed as an opaque object. Logging code receives only the redacted form `REDACTED` for the auth field.

## Integration Points

### External Services

| Service | Integration Pattern | Notes |
|---------|---------------------|-------|
| Vast.ai API | REST via `urllib.request` (stdlib) | Credential from `0600` file; reconcile by label; never retry ambiguous creates |
| Vast.ai instance SSH | `subprocess` + `paramiko` (or stdlib `ssh`) | Used for k3s config, vLLM startup verification, artifact retrieval |
| Kubernetes (local k3s) | `kubectl` subprocess or `kubernetes` client | `autoscaling/v2` API only; no v1 HPA |
| Prometheus (on GPU node) | HTTP scrape via `urllib.request` | Verify metrics exist before advancing to HPA observation step |
| custom-metrics API | `kubectl get --raw` or direct API call | Confirm value propagates before declaring canary success |
| Independent Guard Host | HTTP API (arm/heartbeat/release/status) | Must be reachable independently of laptop; SSH fallback acceptable |

### Internal Boundaries

| Boundary | Communication | Notes |
|----------|---------------|-------|
| Controller ↔ Budget | Direct function call | Budget is not a service; it's a module with Decimal state loaded from file |
| Controller ↔ Journal | Append-only write; never read own state back | Controller reconstructs state from journal only on crash-resume |
| Controller ↔ Guard | HTTP (arm/heartbeat/release) | Timeout on guard calls → abort create, don't proceed |
| Arms ↔ Integrity | Arms write artifacts; Integrity reads them post-run | No shared mutable state between arm execution and integrity validation |
| Analyzer ↔ Deck Generator | Analyzer produces `ValidationResult` files; Deck reads them | Deck generator is strictly downstream; never invokes analyzer mid-render |

## Sources

- Project requirements: `.planning/PROJECT.md`
- Key design decisions: hash-chained journal, Decimal budget, guard authority model, report-before-destroy, provenance labels, claim gating — all stated explicitly in PROJECT.md Key Decisions table
- Kubernetes HPA `autoscaling/v2` API: stable since Kubernetes 1.26; custom metrics via the `custom.metrics.k8s.io` API group
- vLLM observability: Prometheus metrics exported at `/metrics`; relevant metrics include `vllm:num_requests_waiting` (queue depth) and `vllm:gpu_cache_usage_perc` (KV-cache pressure)
- Python `decimal.Decimal` boundary discipline: stdlib docs — `Decimal(str(float_val))` vs `Decimal(float_val)` precision difference

---
*Architecture research for: SRECon26 LLM HPA PoC evidence pipeline*
*Researched: 2026-09-23*
