# Stack Research

**Domain:** Evidence pipeline for Kubernetes HPA / LLM inference observability PoC
**Researched:** 2026-09-23
**Confidence:** HIGH (constraints are explicit in PROJECT.md; no ambiguity in library selection)

---

## Design Principle: Two-Layer Stack

The project constraint is non-negotiable:

> "Standard library only for core safety logic (dataclasses, Decimal, protocols)"

This splits the stack into two explicit layers:

| Layer | Rule | Components |
|-------|------|------------|
| **Safety Core** | stdlib only — zero external deps | Budget ledger, journal, guard protocol, integrity checks |
| **Presentation / Infra** | External packages allowed | PPTX, charts, k8s client, Prometheus, SSH |

Every technology decision below is made against this split. If a library touches the budget path, the journal, or checksum logic, it is prohibited regardless of convenience.

---

## Recommended Stack

### Core Technologies (all required, non-negotiable)

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| Python | 3.14 | Runtime | Explicitly required; 3.14 ships `typing.Protocol` improvements and better `dataclasses` ergonomics; no alternatives considered |
| `decimal` (stdlib) | built-in | Budget arithmetic | Floating-point cannot represent $5.00 exactly; `Decimal` with `ROUND_HALF_UP` is the only safe choice for a hard cap |
| `hashlib` (stdlib) | built-in | SHA-256 checksums, hash-chain journal | Provides `sha256()` used for ROOT-HASH and per-event `previous_hash` chaining; no external dep needed |
| `dataclasses` (stdlib) | built-in | Typed value objects for journal events, budget entries | Structural clarity without Pydantic on the safety path |
| `json` (stdlib) | built-in | Journal serialization | Newline-delimited JSON (NDJSON) is the correct format for append-only, crash-safe logs; no binary format dependencies |
| `pathlib` (stdlib) | built-in | File-path manipulation | Preferred over `os.path`; type-safe, platform-independent |
| `subprocess` (stdlib) | built-in | kubectl, vastai CLI, ssh invocations | Explicit argument lists (not shell=True) prevent command injection; sufficient for CLI orchestration |
| `typing` (stdlib) | built-in | `Protocol`, `TypedDict`, `Literal` | Enables structural typing for guard/controller interfaces without runtime overhead |
| `asyncio` (stdlib) | built-in | Concurrent heartbeat + experiment polling | Single-process async avoids threading races in the controller |
| pytest | 9.x | Test suite | Explicitly required; 9.x has improved fixture scoping and better async support |

### Supporting Libraries (external, non-safety path only)

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `python-pptx` | ≥1.0.2 | Generate the 16-slide .pptx deck | Only in Phase 3 deck assembly; never imported by safety core |
| `matplotlib` | ≥3.9 | Generate chart PNGs embedded in slides | Phase 3 only; produces deterministic PNG output from validated result files |
| `kubernetes` | ≥31.0 | Python client for `autoscaling/v2` HPA reads and metric queries | Used in Phase 2 canary to verify HPA event log; safer than parsing `kubectl` JSON output |
| `prometheus_client` | ≥0.21 | Expose custom metrics (queue depth, synthetic KV) on local test nodes | Used in Phase 1 local arms to feed Prometheus scrape |
| `pytest-asyncio` | ≥0.24 | Async test support in pytest 9 | Required when testing asyncio controller loops; `asyncio_mode = "auto"` in pytest.ini |

### Development Tools

| Tool | Purpose | Notes |
|------|---------|-------|
| `semgrep` | Security scan before every paid run | Required by project; run with `--config auto` + custom rules for Decimal bypass detection |
| `mypy` | Static type checking | Run with `--strict`; catches Protocol mismatches and Decimal/float mixing before runtime |
| `ruff` | Linting + formatting | Faster than black+flake8; single tool; configure `target-version = "py314"` |
| `k3s` | Local single-node Kubernetes | The lightweight distribution for local HPA arms; install via curl script on Linux host |
| `vastai` CLI | Vast.ai instance lifecycle | Use subprocess to drive `vastai create instance`, `vastai destroy instance`, `vastai show instances`; never shell=True |

---

## Installation

```bash
# Python runtime (must be 3.14)
python3.14 -m venv .venv
source .venv/bin/activate

# Presentation and infra layer only (NOT imported by safety core)
pip install \
  "python-pptx>=1.0.2" \
  "matplotlib>=3.9" \
  "kubernetes>=31.0" \
  "prometheus_client>=0.21"

# Test dependencies
pip install \
  "pytest>=9.0" \
  "pytest-asyncio>=0.24"

# Dev tools (not in production path)
pip install mypy ruff semgrep
```

> **No `requirements.txt` shortcut for safety core.** The budget ledger, journal, and integrity modules must import nothing outside stdlib. Enforce this with a `semgrep` rule that rejects any `import` outside the stdlib allowlist in `src/core/`.

---

## Alternatives Considered

| Recommended | Alternative | When to Use Alternative |
|-------------|-------------|-------------------------|
| `decimal.Decimal` (stdlib) | `float` | Never — floats cannot represent $5.00 exactly; one rounding error would undermine the hard cap |
| `decimal.Decimal` (stdlib) | `mpmath` | Never on the budget path; mpmath is an external dependency and overkill for 2-decimal-place money |
| NDJSON journal (`json` stdlib) | SQLite | Only if query complexity grows beyond grep; for this PoC, a flat append-only file with hash chaining is simpler and crash-safer than a WAL database |
| `subprocess` + vastai CLI | `vastai` Python SDK | Use the SDK only if the CLI becomes insufficient; SDK adds an external dep to orchestration paths |
| `matplotlib` | `plotly` | Use plotly only if interactive HTML charts are needed; for static PNGs embedded in PPTX, matplotlib produces more reproducible output |
| `paramiko` for SSH | `subprocess` + `ssh` | Use `subprocess + ssh` — it requires no external dep and the project already uses subprocess for CLI calls; paramiko adds complexity for one-way command dispatch to guard host |
| `kubernetes` Python client | `subprocess` + `kubectl` | Python client is preferred for HPA status reads (avoids fragile JSON parsing); kubectl subprocess is acceptable for imperative create/delete only |

---

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| `float` for any budget value | Cannot represent 0.01 exactly; two float additions can produce $5.000000000000001 which bypasses the cap | `decimal.Decimal` everywhere money appears |
| `pydantic` on the safety path | External dep; adds JSON coercion that can silently convert Decimal to float | `dataclasses` + manual `Decimal(str(value))` at JSON boundaries |
| `databases` / `sqlalchemy` / any ORM | External dep on the journal path; ORMs can auto-commit or buffer writes, defeating append-only crash safety | NDJSON via `json.dumps` + `file.write` + `file.flush` + `os.fsync` |
| `celery` / `rq` / task queues | External deps; controller is a single asyncio loop — no distributed task system needed for a PoC | `asyncio` with structured concurrency |
| `docker` SDK | External dep; Docker interaction for vLLM image validation is a single `docker manifest inspect` call | `subprocess` + `docker` CLI |
| `shell=True` in subprocess calls | Command injection risk (credential paths, instance IDs from Vast.ai API could be attacker-controlled in a less bounded system) | Explicit argument list: `subprocess.run(["vastai", "destroy", instance_id], ...)` |
| Any library that wraps `Decimal` with `float` internally (e.g., old versions of `simplejson`) | Silent precision loss at the JSON boundary | `json` stdlib with explicit `str(decimal_value)` serialization |

---

## Stack Patterns by Variant

**Phase 1 — Safety Foundation and Local Evidence:**
- 100% stdlib for all logic
- `prometheus_client` for exposing HPA test metrics
- `pytest 9` + `pytest-asyncio` for controller and journal tests
- `semgrep` scan before any resource creation

**Phase 2 — Live GPU Canary:**
- Add `kubernetes` client for reading HPA `.status.currentReplicas` and events
- `subprocess` + vastai CLI for instance lifecycle
- SSH via `subprocess + ssh -i key -o StrictHostKeyChecking=accept-new` (not paramiko)
- Continue stdlib-only for budget ledger and guard protocol

**Phase 3 — Evidence, Charts, Deck:**
- `matplotlib` for timeseries charts (CPU%, queue depth, TTFT) as PNG
- `python-pptx` for deck assembly from validated result files
- `mypy --strict` pass required before deck generation
- `semgrep` final scan

---

## Version Compatibility

| Package | Compatible With | Notes |
|---------|-----------------|-------|
| `pytest>=9.0` | `pytest-asyncio>=0.24` | pytest-asyncio 0.24 dropped legacy `asyncio_mode` detection; set `asyncio_mode = "auto"` in `pyproject.toml` |
| `kubernetes>=31.0` | Kubernetes API 1.29–1.32 | v31 client tracks k8s 1.31; `autoscaling/v2` HPA is stable since 1.23 — no compat risk |
| `python-pptx>=1.0.2` | Google Slides import | PPTX 1.0+ uses OOXML correctly; test import in Google Slides with screenshot evidence as required |
| `matplotlib>=3.9` | Python 3.14 | 3.9+ dropped Python 3.8 support; compatible with 3.14 |

---

## Sources

- PROJECT.md explicit constraints — HIGH confidence (primary source for all decisions)
- Python 3.14 stdlib documentation — HIGH confidence
- `autoscaling/v2` Kubernetes API — HIGH confidence (stable since k8s 1.23)
- python-pptx project (python-pptx.readthedocs.io) — HIGH confidence (only maintained PPTX library for Python)
- prometheus_client Python library (github.com/prometheus/client_python) — HIGH confidence (official Prometheus project)
- kubernetes Python client (github.com/kubernetes-client/python) — HIGH confidence (official client)
- pytest 9 release notes (docs.pytest.org) — HIGH confidence

---
*Stack research for: SRECon26 LLM HPA PoC evidence pipeline*
*Researched: 2026-09-23*
