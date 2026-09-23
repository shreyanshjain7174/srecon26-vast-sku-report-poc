# Phase 01 Verification

## Result: PASSED (local-synthetic evidence; no provider activity)

- `make test`: 32 passed.
- `semgrep --config .semgrep.yml --error src tests scripts`: 0 findings.
- `kubectl apply --dry-run=client -f k8s/local/`: all manifests valid.
- A fresh, owned Kind rehearsal ran actual CPU, queue, and synthetic-KV HPA arms. Every arm held a 90-second negative control at desired replicas `1`, then reached desired and ready replicas `2`.
- The fail-closed validator passed: `python3 scripts/validate_live_evidence.py local-evidence/phase1-live-202609d03531790135589z/cpu local-evidence/phase1-live-202609d03531790135589z/queue local-evidence/phase1-live-202609d04031790136227z/kv` returned `{"cpu": true, "kv": true, "queue": true}`.
- The first full run captured CPU and queue success but correctly rejected synthetic-KV `0.95` as below its required `0.96` margin. The KV-only rerun injected `1.0` and passed; this failure is retained in the raw evidence.
- Source metrics, custom-metrics API output, resource metrics, HPA objects/events, deployment timelines, timestamps, owners, runner logs, and cleanup receipts are retained under `local-evidence/phase1-live-202609d03531790135589z/` and `local-evidence/phase1-live-202609d04031790136227z/` (intentionally gitignored raw evidence).
- Cleanup receipts show both owned clusters were deleted. `kind get clusters` afterward listed only `clawdlinux-demo`, `desktop`, and `ninevigil-demo`.
- No Vast inventory, report, create, destroy, credential, or external provisioning action was performed.

## Evidence Limits

This phase proves local-synthetic HPA signal plumbing and independence; it does not claim real-GPU or real-provider evidence.
