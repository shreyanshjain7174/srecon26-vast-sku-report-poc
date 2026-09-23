---
phase: 02-live-gpu-canary-and-paired-comparison
plan: 06
status: implemented-code-only
---

# Gated Live Dispatcher

Implemented the paid dispatcher without invoking Vast, GitHub workflows,
browser automation, SSH, or any provider mutation.  `LiveCanaryDispatcher`
requires explicit current Phase 1, Semgrep, provider, independent-guard, and
report-fixture paths.  It verifies a zero-inventory/auto-recharge-off KVM offer
contract, reserves Decimal exposure, arms a nonce-bound guard, allows one
create, reconciles only an ambiguous exact label, reports only confirmed
attributable provider faults before teardown, destroys exact ID/label, and
requires three absence reads.

The provider adapter now rejects default image selection.  Create requires the
researched frozen Ubuntu 22.04 template
`b7942f6bbc4374893ff66eb78145bbac` and records KVM image identity
`docker.io/vastai/kvm:ubuntu_cli_22.04-2025-05-16`, with 130 GiB disk, SSH, and
cancel-unavailable.  No `--direct` flag is emitted.  Tests use injected fake
provider, guard, reporter, clock, and remote workload only.

The remote executor must also return exact Qwen model/revision and vLLM digest
facts.  A missing or mismatched value finalizes as a limitation; it cannot
produce a real-GPU claim.

No real GPU result exists yet.  A live run remains conditional on a fresh
passing Phase 1 verification, a deployed independent guard with its secret,
current artifact gates, and an operator-owned injected remote executor.
