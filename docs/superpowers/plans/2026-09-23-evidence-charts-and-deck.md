# Evidence, Charts, and 16-Slide Conference Deck Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn validated local and real run bundles into reproducible tables, graphs, claim-gated narrative, and a Google Slides–validated 16-slide conference deck.

**Architecture:** Python validates evidence, normalizes metrics, gates claims, and generates chart-ready data plus PNG/SVG assets. A standalone `pptxgenjs` generator consumes only gated deck data; validation, rendering, visual inspection, and actual Google Slides import are mandatory release gates.

**Tech Stack:** Python 3.14, pytest, JSON Schema, matplotlib, pandas only if available through workspace runtime, Node 25, pptxgenjs, LibreOffice wrapper, markitdown, Poppler, browser automation for Google Slides import QA.

**Spec:** `docs/superpowers/specs/2026-09-23-srecon26-llm-hpa-poc-design.md`

## Global Constraints

- No graph or slide may use unvalidated or mixed-provenance evidence.
- Local synthetic, real canary, real comparison, background, and future-work content remain visibly distinct.
- Missing comparison evidence produces a limitation slide, never fabricated or illustrative results.
- Root hash is externally anchored after bundle finalization; mutable output is excluded from bundle checksums.
- Browser screenshots and manifests must pass secret/redaction scanning.
- Deck contains exactly 16 slides and speaker notes for each slide.
- Google Slides compatibility requires actual upload/open/render inspection, not structural inference.
- Every commit uses `git commit -s`; no Codex coauthor trailer.

## Review Focus

- Rewritten artifact plus rewritten checksum file must fail external-anchor verification; Task 1 pins this.
- Metric timestamps outside run window or non-monotonic sequence must fail validation; Task 1 pins this.
- Missing three-block comparison must suppress performance language and paired chart data; Task 2 pins this.
- Chart captions must include units, window, and provenance; Task 3 pins this.
- Deck generator must replace unsupported result slides with explicit limitations while keeping exactly 16 slides; Task 4 pins this.

---

## File Structure

```text
schemas/v1/
config/
src/srecon26_poc/evidence/
src/srecon26_poc/analysis/
src/srecon26_poc/presentation/
scripts/
  finalize_evidence.py
  analyze_run.py
  render_charts.py
  build_deck.cjs
  validate_deck.sh
presentation/
  theme.cjs
  assets/
  output/
evidence-root-anchors/
tests/evidence/
tests/analysis/
tests/presentation/
```

### Task 1: Versioned Evidence Schemas, Integrity, and Redaction

**Files:**
- Create: `schemas/v1/run-manifest.schema.json`
- Create: `schemas/v1/event.schema.json`
- Create: `schemas/v1/metric-sample.schema.json`
- Create: `schemas/v1/workload-request.schema.json`
- Create: `schemas/v1/report-receipt.schema.json`
- Create: `schemas/v1/integrity-anchor.schema.json`
- Create: `src/srecon26_poc/evidence/models.py`
- Create: `src/srecon26_poc/evidence/canonical.py`
- Create: `src/srecon26_poc/evidence/integrity.py`
- Create: `src/srecon26_poc/evidence/redaction.py`
- Create: `src/srecon26_poc/evidence/validate.py`
- Create: `scripts/finalize_evidence.py`
- Create: `tests/evidence/test_integrity.py`
- Create: `tests/evidence/test_validate.py`
- Create: `tests/evidence/test_redaction.py`

**Interfaces:**
- Produces: `finalize_bundle(path: Path, anchor_dir: Path) -> IntegrityAnchor` and `validate_bundle(path: Path, anchor: IntegrityAnchor) -> ValidationReport`.

- [ ] **Step 1: Write failing integrity and redaction tests**

```python
def test_rewritten_file_and_checksums_fail_external_anchor(bundle, anchor):
    bundle.joinpath("metrics/raw.prom").write_text("rewritten")
    rewrite_checksums(bundle)
    report = validate_bundle(bundle, anchor)
    assert "root_hash_mismatch" in report.errors

@pytest.mark.parametrize("secret", ["api_key", "cookie", "private_key", "authorization", "ssh-rsa"])
def test_manifest_rejects_secret_fields(secret, manifest):
    manifest[secret] = "sensitive"
    assert secret in scan_secrets(manifest)
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python -m pytest -q tests/evidence`  
Expected: missing module failures.

- [ ] **Step 3: Implement canonical records and root hash**

`SHA256SUMS` uses sorted relative paths and LF endings. `ROOT-HASH.txt` is SHA-256 over canonical manifest bytes, one NUL byte, and exact checksum bytes. Anchor records run ID, root hash, pre-finalization code commit, independent journal receipt, and UTC time; it lives outside run bundle. Validation enforces UTC plus monotonic ordering, run window, trace identity, provenance, required files, and redaction.

- [ ] **Step 4: Run evidence tests**

Run: `python -m pytest -q tests/evidence`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add schemas src/srecon26_poc/evidence scripts/finalize_evidence.py tests/evidence
git commit -s -m "feat: validate and anchor evidence bundles"
```

### Task 2: Analysis, Summaries, and Claim Gates

**Files:**
- Create: `config/evidence-requirements.v1.yaml`
- Create: `config/claim-catalog.v1.yaml`
- Create: `src/srecon26_poc/analysis/ingest.py`
- Create: `src/srecon26_poc/analysis/local_signals.py`
- Create: `src/srecon26_poc/analysis/canary.py`
- Create: `src/srecon26_poc/analysis/comparison.py`
- Create: `src/srecon26_poc/analysis/claims.py`
- Create: `src/srecon26_poc/analysis/summaries.py`
- Create: `scripts/analyze_run.py`
- Create: `tests/analysis/test_local_signals.py`
- Create: `tests/analysis/test_canary.py`
- Create: `tests/analysis/test_comparison.py`
- Create: `tests/analysis/test_claims.py`
- Create: `tests/analysis/test_summaries.py`

**Interfaces:**
- Consumes: validated bundles from Task 1.
- Produces: `claim-results.json`, stable CSV summaries, and `DeckEvidence`.
- `evaluate_claims(index: EvidenceIndex) -> tuple[ClaimResult, ...]`

- [ ] **Step 1: Write failing claim matrix tests**

```python
def test_local_claim_requires_all_three_independent_arms(local_index):
    local_index.remove_arm("kv")
    claims = by_id(evaluate_claims(local_index))
    assert claims["local_signal_independence"].allowed is False

def test_incomplete_comparison_cannot_emit_performance_claim(comparison_index):
    comparison_index.blocks = comparison_index.blocks[:2]
    claims = by_id(evaluate_claims(comparison_index))
    assert claims["paired_hpa_comparison"].allowed is False
    assert "CPU-only versus" not in renderable_result_text(claims)
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python -m pytest -q tests/analysis`  
Expected: missing module failures.

- [ ] **Step 3: Implement normalization and gates**

Local claim requires three isolated arms, 90-second controls, target/non-target margins, and desired plus ready `1→2`. Canary claim requires provider/GPU/CUDA/k3s/device-plugin/vLLM/Prometheus/metrics APIs/HPA/request evidence. Comparison claim requires three valid blocks with fixed order and identical controls. Derived output copies claim ID, provenance, units, window, run IDs, and root hashes.

- [ ] **Step 4: Run analysis tests and fixture analysis**

Run: `python -m pytest -q tests/analysis`  
Expected: PASS.  
Run: `python scripts/analyze_run.py artifacts/fixtures/local-complete --output artifacts/analysis-fixture`  
Expected: stable CSV/JSON and only local claim allowed.

- [ ] **Step 5: Commit**

```bash
git add config src/srecon26_poc/analysis scripts/analyze_run.py tests/analysis
git commit -s -m "feat: gate claims on validated evidence"
```

### Task 3: Publication Graphs and Chart Data

**Files:**
- Create: `src/srecon26_poc/presentation/chart_data.py`
- Create: `scripts/render_charts.py`
- Create: `tests/presentation/test_chart_data.py`
- Create: `tests/presentation/test_chart_render.py`
- Create: `presentation/assets/.gitkeep`

**Interfaces:**
- Consumes: gated summaries and claim results.
- Produces: chart-data JSON, SVG, and 300-DPI PNG for local signals, canary path/time series, comparison when allowed, and budget/lifecycle.

- [ ] **Step 1: Write failing provenance and unsupported-series tests**

```python
def test_chart_series_carry_units_window_and_provenance(local_summary):
    charts = build_chart_data(local_summary)
    for series in charts.series:
        assert series.units
        assert series.window
        assert series.provenance in {"local-synthetic", "real-canary", "real-comparison"}

def test_no_comparison_chart_when_claim_denied(deck_evidence):
    deck_evidence.claim("paired_hpa_comparison").allowed = False
    assert "paired-comparison" not in build_chart_data(deck_evidence).chart_ids
```

- [ ] **Step 2: Implement chart data and renderer**

Use one restrained palette with distinct provenance styles. Every chart includes title, axes, units, sample window, and source run caption. Small multiples share scales where comparison is intended. Do not smooth or interpolate missing values.

- [ ] **Step 3: Run chart tests and render fixtures**

Run: `python -m pytest -q tests/presentation/test_chart_data.py tests/presentation/test_chart_render.py`  
Expected: PASS.  
Run: `python scripts/render_charts.py artifacts/analysis-fixture --output presentation/assets`  
Expected: SVG and PNG pairs with no comparison chart when gate denied.

- [ ] **Step 4: Commit**

```bash
git add src/srecon26_poc/presentation/chart_data.py scripts/render_charts.py tests/presentation presentation/assets
git commit -s -m "feat: render provenance-safe result charts"
```

### Task 4: Claim-Gated 16-Slide PPTX Generator

**Files:**
- Create: `config/deck-outline.v1.yaml`
- Create: `src/srecon26_poc/presentation/deck_data.py`
- Create: `presentation/theme.cjs`
- Create: `scripts/build_deck.cjs`
- Create: `tests/presentation/test_deck_data.py`
- Create: `tests/presentation/test_pptx_output.py`

**Interfaces:**
- Consumes: `DeckEvidence`, chart data, generated assets.
- Produces: `presentation/output/srecon26-llm-hpa-lightning-talk.pptx` and extracted deck manifest.

- [ ] **Step 1: Write failing exact-slide and fallback tests**

```python
def test_deck_has_exactly_sixteen_slides_and_notes(deck_data):
    slides = build_deck_model(deck_data)
    assert len(slides) == 16
    assert all(slide.notes.strip() for slide in slides)

def test_missing_comparison_uses_limitation_slide(deck_data):
    deck_data.claims["paired_hpa_comparison"].allowed = False
    slide = build_deck_model(deck_data)[12]
    assert slide.kind == "limitation"
    assert "not run" in slide.title.lower()
```

- [ ] **Step 2: Implement deck model and generator**

Set `LAYOUT_WIDE` before slides. Use Cambria headings and Arial body. Create exactly 16 slides matching spec. Every slide has one dominant visual and `slide.addNotes(...)`. Use native PowerPoint charts when Google import preserves them; use validated SVG/PNG replacement otherwise. Every `addText` uses `isTextBox: true` and safe margins. No gradients, title underlines, decorative stripes, placeholders, or unsupported claims.

- [ ] **Step 3: Generate and validate PPTX**

Run: `node scripts/build_deck.cjs artifacts/analysis-fixture presentation/output/srecon26-llm-hpa-lightning-talk.pptx`  
Run: `python /Users/sunny/.codex/plugins/cache/claude-cowork/anthropic-skills/1.0.0/skills/pptx/scripts/office/validate.py presentation/output/srecon26-llm-hpa-lightning-talk.pptx`  
Run: `markitdown presentation/output/srecon26-llm-hpa-lightning-talk.pptx > presentation/output/deck.txt`  
Expected: 16 slides, no structural errors, all content and notes present, no placeholder tokens.

- [ ] **Step 4: Render and inspect every slide**

Run: `python /Users/sunny/.codex/plugins/cache/claude-cowork/anthropic-skills/1.0.0/skills/pptx/scripts/office/soffice.py --headless --convert-to pdf presentation/output/srecon26-llm-hpa-lightning-talk.pptx`  
Run: `pdftoppm -jpeg -r 150 presentation/output/srecon26-llm-hpa-lightning-talk.pdf presentation/output/slide`  
Expected: all 16 renders show no overflow, overlap, low contrast, missing visual, or margins below 0.5 inches.

- [ ] **Step 5: Commit generator, tests, and fixture deck**

```bash
git add config/deck-outline.v1.yaml src/srecon26_poc/presentation/deck_data.py presentation/theme.cjs scripts/build_deck.cjs tests/presentation presentation/output
git commit -s -m "feat: build claim-gated conference deck"
```

### Task 5: Actual Google Slides Import QA and Release Bundle

**Files:**
- Create: `scripts/validate_deck.sh`
- Create: `docs/runbooks/google-slides-import.md`
- Create: `tests/presentation/test_release_bundle.py`
- Create at runtime: `presentation/output/google-slides-import-receipt.json`
- Create at runtime: redacted import-validation screenshots.

**Interfaces:**
- Consumes: final PPTX and reference slide renders.
- Produces: release manifest proving local validation plus Google Slides import inspection.

- [ ] **Step 1: Write failing release-bundle test**

```python
def test_release_requires_google_import_receipt(release_dir):
    report = validate_release(release_dir)
    assert "google_slides_import_receipt" in report.missing
    release_dir.joinpath("google-slides-import-receipt.json").write_text(valid_receipt())
    assert validate_release(release_dir).ok
```

- [ ] **Step 2: Implement local release validator**

Validator checks PPTX schema, 16-slide count, notes, extracted content, PDF/JPEG renders, chart assets, claim-results, source summaries, checksums, root anchors, and import receipt.

- [ ] **Step 3: Import into Google Slides and inspect**

Upload final PPTX through authenticated browser. Open all 16 slides. Compare slide order, titles, visuals, charts, fonts, notes, and clipping with local reference renders. Capture redacted screenshots and receipt containing UTC time, slide count, deck hash, browser-visible import status, and any replacements made. If native chart changes materially, replace it with validated SVG/PNG, rebuild, and repeat import.

- [ ] **Step 4: Run final release validation**

Run: `bash scripts/validate_deck.sh presentation/output/srecon26-llm-hpa-lightning-talk.pptx`  
Run: `python -m pytest -q tests/presentation/test_release_bundle.py`  
Expected: PASS only after actual Google Slides receipt exists and matches deck hash.

- [ ] **Step 5: Commit release metadata and final deck**

```bash
git add scripts/validate_deck.sh docs/runbooks/google-slides-import.md tests/presentation presentation/output evidence-root-anchors
git commit -s -m "docs: release SRECon lightning talk deck"
```

