#!/usr/bin/env node
/* Build the editable SRECon26 evidence deck. */
const path = require('path');
const fs = require('fs');
const pptxgen = require('pptxgenjs');

const root = path.resolve(__dirname, '..');
const outputDir = path.join(root, 'artifacts', 'presentation');
const output = path.join(outputDir, 'srecon26-llm-hpa-evidence-poc.pptx');
const chart = (name) => path.join(outputDir, name);
fs.mkdirSync(outputDir, { recursive: true });

const pptx = new pptxgen();
pptx.layout = 'LAYOUT_WIDE';
pptx.author = 'SRECon26 PoC';
pptx.company = 'SRECon26';
pptx.subject = 'Evidence-first LLM autoscaling PoC';
pptx.title = 'Why Your Kubernetes Autoscaler Fails LLM Inference (And What vLLM/KServe/llm-d Do Instead)';
pptx.lang = 'en-US';
pptx.theme = {
  headFontFace: 'Cambria', bodyFontFace: 'Arial', lang: 'en-US',
};
pptx.defineLayout({ name: 'CUSTOM_WIDE', width: 13.333, height: 7.5 });
pptx.layout = 'CUSTOM_WIDE';

const C = {
  ink: '171B22', card: '242A35', card2: '1D2430', text: 'F5F7FA', muted: 'B9C2CF',
  teal: '24C4B8', gold: 'F6C85F', coral: 'FF6B6B', purple: 'A78BFA', grid: '3B4554', white: 'FFFFFF',
};
const S = pptx.ShapeType;
const CH = pptx.ChartType;
const noLine = { color: C.ink, transparency: 100 };
const tx = (slide, text, options = {}) => slide.addText(text, {
  fontFace: 'Arial', color: C.text, margin: 0, breakLine: false, isTextBox: true,
  fit: 'shrink', valign: 'mid', ...options,
});
const heading = (slide, title, kicker = null) => {
  if (kicker) tx(slide, kicker.toUpperCase(), { x: 0.66, y: 0.40, w: 4.8, h: 0.22, fontSize: 10, bold: true, color: C.teal, charSpacing: 1.6 });
  tx(slide, title, { x: 0.66, y: kicker ? 0.67 : 0.52, w: 11.85, h: 0.58, fontFace: 'Cambria', fontSize: 30, bold: true });
};
const chip = (slide, label) => {
  slide.addShape(S.roundRect, { x: 8.05, y: 6.88, w: 4.62, h: 0.34, rectRadius: 0.08, fill: { color: C.card2 }, line: { color: C.grid, transparency: 25 } });
  tx(slide, `PROVENANCE  ${label}`, { x: 8.23, y: 6.955, w: 4.25, h: 0.16, fontSize: 8.5, color: C.muted, bold: true, charSpacing: 0.55, align: 'center' });
};
const base = (chipText) => {
  const slide = pptx.addSlide();
  slide.background = { color: C.ink };
  chip(slide, chipText);
  return slide;
};
const notes = (slide, body) => slide.addNotes(body);
const roundedCard = (slide, x, y, w, h, fill = C.card) => slide.addShape(S.roundRect, { x, y, w, h, rectRadius: 0.08, fill: { color: fill }, line: { color: fill } });
const dot = (slide, x, y, color, r = 0.16) => slide.addShape(S.ellipse, { x, y, w: r, h: r, fill: { color }, line: { color } });
const arrow = (slide, x, y, w, color = C.teal) => slide.addShape(S.rightArrow, { x, y, w, h: 0.32, fill: { color }, line: { color } });
const line = (slide, x1, y1, x2, y2, color = C.grid, width = 1) => slide.addShape(S.line, { x: x1, y: y1, w: x2 - x1, h: y2 - y1, line: { color, width } });
const resultBadge = (slide, x, y, label, color) => {
  slide.addShape(S.roundRect, { x, y, w: 1.18, h: 0.34, rectRadius: 0.08, fill: { color, transparency: 12 }, line: { color, transparency: 50 } });
  tx(slide, label, { x: x + 0.08, y: y + 0.08, w: 1.02, h: 0.15, fontSize: 8.4, bold: true, align: 'center' });
};

// 1
{
  const s = base('scope + verified evidence');
  tx(s, 'WHY YOUR KUBERNETES AUTOSCALER\nFAILS LLM INFERENCE', { x: 0.78, y: 0.92, w: 7.75, h: 1.25, fontFace: 'Cambria', fontSize: 35, bold: true });
  tx(s, 'And what vLLM / KServe / llm-d do instead', { x: 0.81, y: 2.32, w: 7.7, h: 0.36, fontSize: 20, color: C.muted });
  tx(s, 'SRECon26 lightning talk • evidence-first PoC • live GPU path intentionally bounded', { x: 0.81, y: 2.86, w: 8.2, h: 0.28, fontSize: 13, color: C.muted });
  roundedCard(s, 8.95, 1.02, 3.35, 4.86, C.card);
  for (let i = 0; i < 3; i += 1) {
    const yy = 1.74 + i * 1.2;
    dot(s, 9.40, yy, [C.teal, C.gold, C.purple][i], 0.34);
    tx(s, ['CPU', 'QUEUE', 'SYNTHETIC KV'][i], { x: 9.9, y: yy + 0.02, w: 1.8, h: 0.25, fontSize: 18, bold: true });
    tx(s, ['resource signal', 'waiting work', 'pressure surrogate'][i], { x: 9.9, y: yy + 0.34, w: 1.75, h: 0.20, fontSize: 11, color: C.muted });
  }
  tx(s, '1→2', { x: 9.35, y: 5.04, w: 2.55, h: 0.48, fontSize: 40, fontFace: 'Cambria', bold: true, color: C.teal, align: 'center' });
  tx(s, 'local HPA transition', { x: 9.35, y: 5.55, w: 2.55, h: 0.22, fontSize: 12, color: C.muted, align: 'center' });
  notes(s, 'Opening: the point is not that CPU is useless. The point is that language-model demand has other observable pressure signals. This PoC proves local signal plumbing, then draws a hard boundary around what the live GPU work did not establish.');
}

// 2
{
  const s = base('claim • design hypothesis');
  heading(s, 'The claim is conditional, not universal', 'thesis');
  tx(s, 'CPU-only HPA can be late when waiting work or KV pressure rises before CPU produces a useful scaling signal.', { x: 0.86, y: 1.55, w: 8.0, h: 1.12, fontFace: 'Cambria', fontSize: 30, bold: true, color: C.text, breakLine: false });
  roundedCard(s, 9.2, 1.48, 2.95, 3.72, C.card);
  tx(s, 'Scope guard', { x: 9.55, y: 1.83, w: 2.2, h: 0.3, fontSize: 20, bold: true, color: C.gold });
  ['not “CPU never works”', 'not a live GPU result', 'not an A/B claim'].forEach((text, i) => { dot(s, 9.55, 2.48 + i * 0.68, C.coral, 0.14); tx(s, text, { x: 9.82, y: 2.42 + i * 0.68, w: 1.85, h: 0.25, fontSize: 14 }); });
  line(s, 1.04, 3.55, 7.83, 3.55, C.grid, 1.4);
  tx(s, 'Question for the talk: which observable signals should earn a replica before user latency gets worse?', { x: 1.04, y: 3.92, w: 7.6, h: 0.45, fontSize: 18, color: C.muted });
  notes(s, 'Say the claim precisely. CPU-only HPA may be adequate in some systems. The PoC asks whether queue and cache pressure can make a better early signal in an LLM-serving context.');
}

// 3
{
  const s = base('background • official docs • not measured here');
  heading(s, 'What the LLM stack does instead: expose, orchestrate, route', 'background');
  const cols = [
    ['vLLM', 'exposes waiting + KV metrics', C.teal], ['KServe', 'manages LLM scaling resources', C.gold], ['llm-d', 'routes on queue + cache state', C.purple],
  ];
  cols.forEach(([label, sub, color], i) => {
    const x = 0.82 + i * 4.15;
    roundedCard(s, x, 1.62, 3.48, 3.5);
    dot(s, x + 0.44, 2.03, color, 0.48);
    tx(s, label, { x: x + 1.12, y: 2.08, w: 1.8, h: 0.32, fontSize: 22, bold: true });
    tx(s, sub, { x: x + 0.44, y: 2.84, w: 2.55, h: 0.45, fontSize: 16, color: C.muted });
    tx(s, ['Prometheus /metrics', 'WVA, HPA, or KEDA', 'filter → score → pick'][i], { x: x + 0.44, y: 3.70, w: 2.6, h: 0.26, fontSize: 13, bold: true, color });
    tx(s, ['num_requests_waiting', 'LLMInferenceService', 'queue / KV / prefix-aware'][i], { x: x + 0.44, y: 4.14, w: 2.6, h: 0.22, fontSize: 12, color: C.muted });
  });
  tx(s, 'Official docs establish capabilities; this PoC does not claim it measured those products.', { x: 0.84, y: 5.55, w: 9.2, h: 0.28, fontSize: 14, italic: true, color: C.muted });
  tx(s, 'Sources: docs.vllm.ai metrics • kserve.github.io LLMInferenceService API • github.com/llm-d/llm-d scheduler', { x: 0.84, y: 6.02, w: 10.8, h: 0.22, fontSize: 10, color: C.muted });
  notes(s, 'Official documentation supplies the background: vLLM exposes waiting-request and KV-cache gauges; KServe LLMInferenceService can manage WVA, HPA, or KEDA scaling resources; llm-d scores endpoints using queue depth, KV utilization, and prefix-cache state. None of those product capabilities is presented as measured by this PoC.');
}

// 4
{
  const s = base('experiment contract • local-synthetic');
  heading(s, 'The PoC has two deliberately separate proof levels', 'contract');
  const levels = [
    [0.86, 'LOCAL SIGNAL PLUMBING', C.teal, 'Verified', ['Kind/local HPA', 'CPU + queue + synthetic KV', 'independent 1→2 transitions']],
    [6.98, 'LIVE GPU METRIC PATH', C.coral, 'Not complete', ['GPU + CUDA + KVM', 'real vLLM / Prometheus / HPA', 'request and latency evidence']],
  ];
  levels.forEach(([x, name, color, status, points]) => {
    roundedCard(s, x, 1.62, 5.46, 3.98);
    resultBadge(s, x + 3.88, 1.94, status, color);
    tx(s, name, { x: x + 0.42, y: 2.0, w: 3.2, h: 0.28, fontSize: 18, bold: true, color });
    points.forEach((p, i) => { dot(s, x + 0.48, 2.76 + i * 0.67, color, 0.13); tx(s, p, { x: x + 0.75, y: 2.70 + i * 0.67, w: 4.05, h: 0.27, fontSize: 15 }); });
  });
  arrow(s, 5.86, 3.30, 1.25, C.muted);
  tx(s, 'requires a completed capability path', { x: 5.04, y: 3.78, w: 2.85, h: 0.28, fontSize: 11, color: C.muted, align: 'center' });
  notes(s, 'This is an intentional design boundary. Local proof makes a narrow plumbing claim. The live canary requires a much stronger, independent evidence bundle before it can become a product or performance claim.');
}

// 5
{
  const s = base('selected evidence • d035/d042/d040');
  heading(s, 'Selected local evidence: three isolated arms', 'result');
  s.addImage({ path: chart('local-signal-independence.png'), x: 0.50, y: 1.25, w: 12.33, h: 5.85 });
  notes(s, 'These are the three selected runs, not every exploratory attempt. Each one retained raw HPA, deployment, metric, event, and timestamp captures. The selected roots were independently anchored.');
}

// 6
{
  const s = base('CPU local arm • d035');
  heading(s, 'CPU arm: resource pressure independently scaled 1→2', 'local result');
  roundedCard(s, 0.84, 1.58, 5.45, 3.96);
  tx(s, 'd035', { x: 1.30, y: 2.00, w: 1.45, h: 0.36, fontFace: 'Cambria', fontSize: 30, bold: true, color: C.teal });
  tx(s, 'CPU signal crossed its criterion.', { x: 1.30, y: 2.74, w: 3.9, h: 0.30, fontSize: 18, bold: true });
  tx(s, 'Queue ≤ 0.8\nSynthetic KV ≤ 0.64', { x: 1.30, y: 3.35, w: 3.4, h: 0.60, fontSize: 18, color: C.muted, breakLine: false });
  tx(s, 'Verified HPA result', { x: 1.30, y: 4.58, w: 2.4, h: 0.25, fontSize: 13, color: C.muted });
  tx(s, '1 → 2', { x: 1.30, y: 4.88, w: 3.25, h: 0.48, fontFace: 'Cambria', fontSize: 38, bold: true, color: C.teal });
  roundedCard(s, 7.15, 1.58, 5.25, 3.96, C.card2);
  ['90-second negative control', 'target signal above margin', 'desired + ready transitions retained', 'non-target signals below margins'].forEach((t, i) => { dot(s, 7.62, 2.12 + i * 0.66, [C.teal, C.teal, C.gold, C.gold][i], 0.14); tx(s, t, { x: 7.93, y: 2.05 + i * 0.66, w: 3.65, h: 0.28, fontSize: 16 }); });
  tx(s, 'This is local synthetic evidence—not vLLM latency evidence.', { x: 1.0, y: 5.98, w: 8.1, h: 0.30, fontSize: 14, italic: true, color: C.muted });
  notes(s, 'The CPU arm is a clean control: CPU crossed its condition while queue and synthetic KV stayed below their limits. This demonstrates the CPU signal path and a recorded 1-to-2 HPA transition locally.');
}

// 7
{
  const s = base('queue local arm • d042');
  heading(s, 'Queue arm: waiting work scaled while CPU stayed low', 'local result');
  s.addImage({ path: chart('queue-cpu-control.png'), x: 0.50, y: 1.25, w: 12.33, h: 5.85 });
  notes(s, 'd042 is useful because it makes the separation concrete. The seven negative-control CPU captures peak at 16.456944 millicores, far below the 48 millicore independence ceiling, while the queue signal drove the HPA transition.');
}

// 8
{
  const s = base('queue local arm • native editable chart');
  heading(s, 'The queue control is also preserved as an editable chart', 'audit-friendly visual');
  tx(s, 'd042 CPU (millicores)', { x: 0.86, y: 1.43, w: 3.1, h: 0.26, fontSize: 15, bold: true, color: C.muted });
  s.addChart(CH.bar, [{ name: 'CPU', labels: ['observed peak', 'independence ceiling'], values: [16.456944, 48] }], {
    x: 0.82, y: 1.82, w: 6.6, h: 3.92, catAxisLabelFontFace: 'Arial', catAxisLabelFontSize: 14,
    valAxisLabelFontFace: 'Arial', valAxisLabelFontSize: 11, valAxisMinVal: 0, valAxisMaxVal: 60,
    valAxisMajorUnit: 12, chartColors: [C.teal], showLegend: false, showTitle: false,
    showValue: true, dataLabelPosition: 'outEnd', dataLabelColor: C.text, dataLabelFormatCode: '0.0',
    catAxisLabelColor: C.muted, valAxisLabelColor: C.muted, valGridLine: { color: C.grid, width: 1 }, catGridLine: { style: 'none' },
    showCatName: false, showValAxisTitle: false, showCatAxisTitle: false, showValue: true,
  });
  roundedCard(s, 8.00, 1.85, 4.15, 3.75);
  tx(s, 'Verified inference', { x: 8.44, y: 2.20, w: 3.0, h: 0.30, fontSize: 22, bold: true, color: C.gold });
  tx(s, 'CPU remained below the independence ceiling while the queue arm reached its HPA transition.', { x: 8.44, y: 2.92, w: 3.0, h: 1.10, fontSize: 21, bold: true });
  tx(s, 'Not a user-latency or GPU utilization measurement.', { x: 8.44, y: 4.65, w: 3.0, h: 0.38, fontSize: 14, color: C.muted, italic: true });
  notes(s, 'The chart is deliberately simple and native to PowerPoint so the numeric source can be audited or restyled. It repeats one exact evidence value: 16.456944 millicores against a 48 millicore ceiling.');
}

// 9
{
  const s = base('synthetic KV local arm • d040');
  heading(s, 'Synthetic KV arm: pressure signal independently scaled 1→2', 'local result');
  const tiles = [
    ['KV condition', '≥ 0.96', C.purple], ['Queue control', '≤ 0.8', C.gold], ['CPU control', '≤ 48m', C.teal],
  ];
  tiles.forEach(([title, value, color], i) => {
    const x = 0.86 + i * 4.1;
    roundedCard(s, x, 1.80, 3.45, 2.22);
    dot(s, x + 0.46, 2.23, color, 0.26);
    tx(s, title, { x: x + 0.88, y: 2.20, w: 2.0, h: 0.26, fontSize: 17, bold: true });
    tx(s, value, { x: x + 0.45, y: 2.80, w: 2.4, h: 0.45, fontFace: 'Cambria', fontSize: 31, bold: true, color });
  });
  roundedCard(s, 0.86, 4.62, 11.65, 1.05, C.card2);
  tx(s, 'd040 outcome', { x: 1.22, y: 4.89, w: 1.7, h: 0.26, fontSize: 15, bold: true, color: C.muted });
  tx(s, 'Synthetic KV pressure drove the recorded desired and ready 1→2 HPA transition.', { x: 3.05, y: 4.80, w: 7.9, h: 0.44, fontSize: 22, bold: true });
  tx(s, 'Synthetic is intentional: this proves a custom-metric path, not GPU cache behavior.', { x: 0.96, y: 6.03, w: 8.9, h: 0.27, fontSize: 14, italic: true, color: C.muted });
  notes(s, 'This is the important caveat slide. The local KV metric is synthetic by design. It proves the adapter-to-HPA path can react to a KV-like signal, but it does not prove vLLM cache pressure on a GPU.');
}

// 10
{
  const s = base('integrity • independent guard anchor');
  heading(s, 'Evidence was selected, checksummed, and independently anchored', 'integrity');
  const steps = [
    ['01', 'Raw capture bundle', 'metric, HPA, deployment, events, timestamps', C.teal],
    ['02', 'SHA-256 root', 'allowlist + manifest define the selected proof', C.gold],
    ['03', 'Independent receipt', 'guard journal acknowledges all three roots', C.purple],
  ];
  steps.forEach(([num, title, sub, color], i) => {
    const x = 0.80 + i * 4.15;
    dot(s, x + 0.12, 1.75, color, 0.46);
    tx(s, num, { x: x + 0.12, y: 1.86, w: 0.46, h: 0.12, fontSize: 9, bold: true, align: 'center', color: C.ink });
    roundedCard(s, x, 2.55, 3.45, 2.58);
    tx(s, title, { x: x + 0.35, y: 2.96, w: 2.55, h: 0.34, fontSize: 20, bold: true });
    tx(s, sub, { x: x + 0.35, y: 3.67, w: 2.62, h: 0.62, fontSize: 15, color: C.muted });
    if (i < 2) arrow(s, x + 3.52, 3.59, 0.54, C.muted);
  });
  tx(s, 'Selected roots: CPU 1ae7…34d2  •  Queue f2d9…fceeb  •  Synthetic KV 0b61…be10', { x: 1.08, y: 5.95, w: 10.8, h: 0.27, fontSize: 13, color: C.muted, align: 'center' });
  notes(s, 'This is why we show only the selected local runs. The artifacts are checksummed and the roots were acknowledged by the independently hosted guard journal. It is evidence hygiene, not an extra performance claim.');
}

// 11
{
  const s = base('live safety pipeline • no performance claim');
  heading(s, 'The live path is built to fail safe before it can claim success', 'safety');
  const stages = [
    ['reserve', 'budget ledger', C.gold], ['arm', 'independent guard', C.purple], ['create', 'exact provider ID', C.teal], ['verify', 'capability evidence', C.teal], ['destroy', 'exact ID + absence', C.coral],
  ];
  stages.forEach(([verb, label, color], i) => {
    const x = 0.56 + i * 2.55;
    roundedCard(s, x, 2.38, 2.10, 2.08);
    dot(s, x + 0.84, 2.78, color, 0.34);
    tx(s, verb.toUpperCase(), { x: x + 0.20, y: 3.32, w: 1.70, h: 0.24, fontSize: 14, bold: true, color, align: 'center' });
    tx(s, label, { x: x + 0.20, y: 3.77, w: 1.70, h: 0.24, fontSize: 12, color: C.muted, align: 'center' });
    if (i < stages.length - 1) arrow(s, x + 2.16, 3.20, 0.33, C.muted);
  });
  tx(s, 'The provider report path is ordered before destroy only when a genuine, evidence-backed provider/SKU fault is established.', { x: 1.07, y: 5.45, w: 11.10, h: 0.48, fontSize: 17, color: C.muted, align: 'center' });
  tx(s, 'Final attempt: $0.025 charged • exact instance 52212017 • three zero-match reads', { x: 1.07, y: 6.05, w: 11.10, h: 0.25, fontSize: 13, bold: true, color: C.gold, align: 'center' });
  notes(s, 'A paid run is not a permission to make a claim. The final attempt reserved $0.25, incurred an authoritative $0.025 invoice charge, and ended with exact-ID teardown plus three zero-match inventory reads. The guard is independent. If a real SKU issue is proved, the report workflow precedes normal teardown—without holding a failed machine just to gather a claim.');
}

// 12
{
  const s = base('live GPU path • limitation');
  heading(s, 'Final live GPU result: bounded FAILED_SAFE, not a demonstration', 'honest outcome');
  s.addImage({ path: chart('evidence-boundary.png'), x: 0.50, y: 1.25, w: 12.33, h: 5.85 });
  notes(s, 'Be explicit: the final paid attempt used provider offer 52180811 on distinct machine 147086 and exact instance 52212017. SSH closed all 36 bounded readiness attempts, so no direct GPU, CUDA, KVM, vLLM, or request evidence exists. The controller did not click Report because this was an unresolved access failure, not a confirmed provider or SKU contract fault. Exact teardown, three absence reads, invoice evidence, sealed hashes, and the independent guard anchor are retained.');
}

// 13
{
  const s = base('claim ledger • presentation guardrail');
  heading(s, 'Claim ledger: speak only to the evidence that exists', 'guardrail');
  const rows = [
    ['Local HPA signal plumbing', 'VERIFIED', C.teal],
    ['Guard, exact teardown, invoice + absence binding', 'VERIFIED / scoped', C.teal],
    ['Real GPU / CUDA / KVM capability', 'NOT DEMONSTRATED', C.coral],
    ['vLLM TTFT or queue/KV behavior', 'NOT MEASURED', C.coral],
    ['CPU-only vs aware-HPA A/B outcome', 'NOT RUN', C.coral],
  ];
  rows.forEach(([claim, state, color], i) => {
    const y = 1.55 + i * 0.88;
    roundedCard(s, 0.90, y, 11.52, 0.62, i % 2 ? C.card2 : C.card);
    tx(s, claim, { x: 1.22, y: y + 0.16, w: 6.5, h: 0.22, fontSize: 16, bold: true });
    resultBadge(s, 9.65, y + 0.13, state, color);
  });
  notes(s, 'This is the slide that protects the talk from overclaiming. The local result is real and useful. The rest remains an experiment plan until the full live evidence contract is met.');
}

// 14
{
  const s = base('next experiment • planned');
  heading(s, 'What the next live run must capture before any comparison', 'planned protocol');
  const items = [
    ['GPU + CUDA', 'directly observed identity and runtime'], ['KVM + device plugin', 'host and Kubernetes capability'], ['vLLM request', 'warm-up plus measured request'], ['Metrics + HPA', 'queue, KV, CPU, replicas, events'], ['Paired A/B', 'same controls, order, and three valid blocks'],
  ];
  items.forEach(([name, sub], i) => {
    const y = 1.42 + i * 0.88;
    dot(s, 1.00, y + 0.17, [C.teal, C.teal, C.gold, C.purple, C.coral][i], 0.20);
    tx(s, name, { x: 1.40, y: y + 0.10, w: 2.3, h: 0.25, fontSize: 17, bold: true });
    tx(s, sub, { x: 4.03, y: y + 0.10, w: 5.6, h: 0.25, fontSize: 16, color: C.muted });
    tx(s, 'required', { x: 10.62, y: y + 0.10, w: 1.05, h: 0.25, fontSize: 12, bold: true, color: C.gold, align: 'right' });
  });
  roundedCard(s, 0.90, 6.03, 11.50, 0.55, C.card2);
  tx(s, 'No partial live data becomes an A/B conclusion.', { x: 1.20, y: 6.18, w: 10.85, h: 0.20, fontSize: 16, bold: true, color: C.text, align: 'center' });
  notes(s, 'Here is the exit criterion. A future comparison needs direct GPU and CUDA observations, a real vLLM request and metrics path, and repeated paired blocks. One working SSH connection would not be enough.');
}

// 15
{
  const s = base('lightning talk arc • 5 minutes');
  heading(s, 'A four-minute talk arc', 'delivery');
  const beats = [
    ['0:00', 'Question', 'When can CPU be late?'], ['0:30', 'Model', 'CPU, queue, synthetic KV'], ['1:00', 'Evidence', 'three local 1→2 transitions'], ['2:10', 'Boundary', 'what GPU work did not prove'], ['2:55', 'Method', 'safe live proof requirements'], ['3:35', 'Takeaway', 'measure before you claim'],
  ];
  beats.forEach(([time, label, sub], i) => {
    const x = 0.60 + i * 2.10;
    line(s, x + 0.36, 3.14, x + 1.86, 3.14, C.grid, 2);
    dot(s, x + 0.82, 2.87, [C.teal, C.gold, C.purple, C.coral, C.teal, C.gold][i], 0.52);
    tx(s, time, { x, y: 1.96, w: 1.72, h: 0.26, fontSize: 15, bold: true, color: C.muted, align: 'center' });
    tx(s, label, { x, y: 3.72, w: 1.72, h: 0.25, fontSize: 16, bold: true, align: 'center' });
    tx(s, sub, { x, y: 4.18, w: 1.72, h: 0.45, fontSize: 11, color: C.muted, align: 'center' });
  });
  notes(s, 'This pacing fits sixteen slides at roughly fifteen seconds each. It keeps the four-minute lightning talk on the practical contribution: disciplined evidence gates for a genuinely interesting autoscaling hypothesis.');
}

// 16
{
  const s = base('takeaway • Q&A');
  tx(s, 'MEASURE THE SIGNAL.\nEARN THE CLAIM.', { x: 0.88, y: 1.35, w: 8.0, h: 1.32, fontFace: 'Cambria', fontSize: 42, bold: true });
  tx(s, 'Verified today: local CPU, queue, and synthetic-KV HPA signal plumbing.\nNot claimed today: live GPU behavior or an A/B performance win.', { x: 0.93, y: 3.12, w: 7.62, h: 0.86, fontSize: 20, color: C.muted });
  roundedCard(s, 9.25, 1.35, 2.65, 3.84, C.card);
  ['CPU', 'QUEUE', 'SYNTHETIC KV'].forEach((label, i) => {
    dot(s, 9.72, 1.92 + i * 0.74, [C.teal, C.gold, C.purple][i], 0.22);
    tx(s, label, { x: 10.15, y: 1.91 + i * 0.74, w: 1.30, h: 0.22, fontSize: 15, bold: true });
  });
  tx(s, 'Questions', { x: 9.58, y: 4.48, w: 1.92, h: 0.28, fontFace: 'Cambria', fontSize: 23, bold: true, color: C.teal, align: 'center' });
  notes(s, 'Close by repeating the boundary: this deck proves a local, independently anchored plumbing result. It deliberately does not turn an incomplete GPU path into a success story.');
}

pptx.writeFile({ fileName: output });
console.log(output);
