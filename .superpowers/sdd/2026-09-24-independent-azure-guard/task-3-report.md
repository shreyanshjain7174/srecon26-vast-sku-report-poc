# Task 3 report: Azure guard live wiring

## Status

DONE_WITH_CONCERNS

The Azure guard path is implemented and fail-closed. The real two-node Vast
attempt remains contingent and was not executed because no dedicated Azure
guard resource group or controller VM is present, no Azure guard connection
configuration is available, and therefore two bound `ARMED` receipts cannot
be obtained. A final read-only Vast inventory check returned zero instances.

## Implementation

- Added `DynamicAzureGuard` to the live factory using the bounded
  `AzureSshGuardTransport`, while keeping GitHub as the default backend.
- Added explicit Azure environment configuration for the single-node live
  factory and explicit Azure CLI options for the two-node runner.
- Persisted non-secret arm evidence for both node guards: status, nonce,
  exact label, immutable deadline, remote host identity, script hash, and
  journal root.
- Kept the first paid create behind both bound `ARMED` receipts and a fresh
  authenticated desktop Report preflight.
- Preserved one-attempt behavior: the runner calls `TwoNodeLease.run` once and
  never retries a provider create after ambiguity.
- Added exact-instance recording so partial server-only creates still retain
  teardown, billing, and absence targets.
- Restricted the Report action to frozen offer/instance contract mismatches or
  validated exact-instance startup-fault evidence. The Report sequence runs
  before normal provider teardown and retains the existing immutable cutoff.
- Added post-run exact invoice capture, three-read local absence artifacts,
  Azure `ABSENCE_CONFIRMED` polling and journal export, and evidence-pack
  rebuild. Missing invoice or guard evidence terminates as
  `evidence-incomplete` instead of claiming completion.
- Extended the evidence summary with bound-receipt, billing, absence, and
  normalized Report outcomes without copying credential material.

## Verification

Test one-liner:

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q && semgrep scan --config auto --error --json --output /var/tmp/srecon26-task3-semgrep-final.json src/srecon26_poc/live_factory.py scripts/run_two_node_metric_path.py scripts/build_evidence_pack.py tests/unit/test_live_factory.py tests/unit/test_two_node_runner.py && jq -e '(.results|length)==0 and (.errors|length)==0' /var/tmp/srecon26-task3-semgrep-final.json >/dev/null && git diff --check
```

Observed results:

- Pytest: `367 passed in 4.27s`.
- Semgrep: 290 rules over five changed Python files, zero findings and zero
  errors.
- Azure read-only inventory: zero resource groups and zero VMs tagged
  `srecon26-purpose=independent-vast-guard`.
- Vast read-only inventory after implementation: `instance_count: 0`.
- No credentials were added to source, arguments, logs, commits, or evidence.
- No live Vast create, report, Azure deployment, or teardown mutation occurred.

## Commits

- `24deeb5` — `feat: gate two-node runs with Azure guard`

## Concerns and remaining live gate

- There is no independently reachable Azure guard capacity to validate live.
- A real attempt requires both exact bound arm receipts, the Report adapter
  preflight, two currently valid distinct Vast offers/machines, and empty Vast
  inventory at execution time.
- Provider invoices may appear after teardown. The runner records this as
  pending and refuses a complete evidence claim until exact invoice evidence
  is available.
- No real Kubernetes, vLLM, HPA, billing, or Azure guard journal evidence was
  produced in this task; the implementation and tests must not be presented as
  a successful real-GPU run.

## Fix round 1: Critical/High review findings

Status: DONE_WITH_CONCERNS. Commit `edf3a17` supersedes the original report's
description of the paid two-node guard selection and shared Azure channel.

- The paid `run_two_node_metric_path.py` entry point now requires the explicit
  value `--guard-backend azure`; it has no GitHub choice or default. The
  single-node live factory retains its existing GitHub behavior.
- Server and worker guards now require separate host, user, identity,
  known-hosts, and port options. Validation rejects a shared controller host,
  SSH identity, or pinned known-hosts file before any provider read or create.
  Each role constructs its own `AzureGuardSshConfig` and transport.
- Every successfully armed Azure channel is statused and journal-exported
  during finalization, including a channel for which the local controller did
  not observe an instance ID. Only a locally observed instance requires the
  independent guard to reach three-read `ABSENCE_CONFIRMED`.
- The evidence pack no longer treats manifest status strings as proof. It
  validates each Azure receipt's backend, role, nonce, exact label, deadline,
  root hash, remote host, script hash, and heartbeat; rejects a shared remote
  host identity; verifies each exported guard journal file hash and event hash
  chain; and verifies the exact, hash-bound three-read absence and invoice
  payloads. A completed claim requires all of these checks.
- `--heartbeat-seconds` is now wired into `SshWorkloadConfig`. The CLI also
  requires the heartbeat-expiry value configured on both guards and refuses a
  nominal heartbeat interval greater than that timer. Blocking SSH work emits
  at half the nominal interval.

Fix-round verification one-liner:

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q && semgrep scan --config auto --error --json --output /var/tmp/srecon26-task3-fix1-semgrep.json scripts/run_two_node_metric_path.py scripts/build_evidence_pack.py tests/unit/test_two_node_runner.py tests/unit/test_build_evidence_pack.py && python3 -c 'import json; d=json.load(open("/var/tmp/srecon26-task3-fix1-semgrep.json")); assert not d.get("results") and not d.get("errors")' && git diff --check
```

Observed fix-round results:

- Pytest: `383 passed in 6.73s`.
- Focused runner/evidence tests: `20 passed in 0.09s`.
- Semgrep: 290 rules over four changed Python files, zero findings and zero
  errors.
- Python compilation and `git diff --check`: passed.
- No live Vast create, Report action, Azure deployment, or cloud mutation was
  performed. The real run remains contingent on two separately hosted Azure
  controllers producing exact bound receipts; this code change is not live
  guard, billing, Kubernetes, vLLM, or HPA evidence.
