---
phase: 02-live-gpu-canary-and-paired-comparison
plan: 06
status: paid-attempt-failed-safe
---

# Gated Live Dispatcher

Implemented and exercised the paid dispatcher through its guarded live path.  `LiveCanaryDispatcher`
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
cancel-unavailable. The final attempt exposed an endpoint-routing gap: it used
the provider proxy path and never obtained usable SSH. The launch contract now
requires Vast `--direct`, and the resolver prefers only the exact instance's
`public_ipaddr` paired with its published `22/tcp` host port, falling back only
to the provider's `ssh_host` plus `ssh_port` pair. This hardening is tested but
has not been exercised in another paid run.

The remote executor must also return exact Qwen model/revision and vLLM digest
facts.  A missing or mismatched value finalizes as a limitation; it cannot
produce a real-GPU claim.

The final distinct-machine run `gpu-smoke-distinct-20260923101734` created exact
instance `52212017`, charged `$0.025`, failed SSH readiness after 36 bounded
attempts, and finalized `FAILED_SAFE`. Exact teardown and three zero-match
absence reads succeeded; the provider inventory was empty. The controller did
not click Report because the access failure was unresolved rather than a
confirmed provider/SKU fault. No real GPU result or paired comparison exists.
