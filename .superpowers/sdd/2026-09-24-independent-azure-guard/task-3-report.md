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
  not observe an instance ID. If status retrieval fails, export is still
  attempted against the bound arm root and the run remains evidence-incomplete.
  Only a locally observed instance requires the independent guard to reach
  three-read `ABSENCE_CONFIRMED`.
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

- Pytest: `384 passed in 4.50s`.
- Focused runner/evidence tests: `21 passed in 0.11s`.
- Semgrep: 290 rules over four changed Python files, zero findings and zero
  errors.
- Python compilation and `git diff --check`: passed.
- No live Vast create, Report action, Azure deployment, or cloud mutation was
  performed. The real run remains contingent on two separately hosted Azure
  controllers producing exact bound receipts; this code change is not live
  guard, billing, Kubernetes, vLLM, or HPA evidence.

## Fix round 2: attested independence and deferred ambiguity finalization

Status: DONE_WITH_CONCERNS. Commits `766b203` and `bb797a3` address the second
re-review.

- Azure arm receipts now bind the heartbeat timeout requested by the client,
  and the guard worker emits its actual configured timeout. Arm and preflight
  reject an absent or mismatched value. The paid runner passes the same value
  into both guard transports and `SshWorkloadConfig`.
- Azure preflight receipts must include a structurally valid Azure VM resource
  ID, Azure VM UUID, remote host identity, and SSH host-key fingerprint. The
  client computes the fingerprint of the single key in each dedicated pinned
  known-hosts file and rejects a remote fingerprint mismatch.
- Before the second arm callback can preflight Report or unlock the first paid
  create, the runner rejects equality across the two attested host identities,
  Azure resource IDs, Azure VM IDs, or pinned host-key fingerprints. DNS
  aliases, copied known-hosts files, and duplicated controllers therefore fail
  closed even when their CLI path strings differ.
- A no-local-instance ambiguous create no longer treats `AWAITING_INSTANCE` or
  another nonterminal export as final. It writes
  `deferred-azure-guard-finalizer.json`, marks finalization incomplete, and
  requires the independent Azure timer to produce `ABSENCE_CONFIRMED` with
  three observations after the immutable deadline. A terminal response with
  future timestamps is not accepted before the local deadline is reached.
- The two direct hostname SSH calls now use the workload's heartbeat/deadline
  streaming runner. Network `ssh-keyscan` was removed; the server bridge reuses
  the already pinned host entry from the controller known-hosts file. All SSH
  and SCP operations in the paid path now run through the heartbeat-aware
  wrapper.
- Evidence-pack receipt validation now also checks the attested Azure
  identities, host-key fingerprints, heartbeat timeout, and pairwise
  independence before presenting a bound-receipt or completed claim.

Fix-round verification one-liner:

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q && semgrep scan --config auto --error --json --output /var/tmp/srecon26-task3-fix2-semgrep.json guard/guard_worker.py scripts/build_evidence_pack.py scripts/run_two_node_metric_path.py src/srecon26_poc/azure_guard_transport.py src/srecon26_poc/guard.py src/srecon26_poc/guard_client.py src/srecon26_poc/live_factory.py tests/unit/test_azure_guard_transport.py tests/unit/test_build_evidence_pack.py tests/unit/test_live_factory.py tests/unit/test_two_node_runner.py && python3 -c 'import json; d=json.load(open("/var/tmp/srecon26-task3-fix2-semgrep.json")); assert not d.get("results") and not d.get("errors")' && git diff --check
```

Observed fix-round results:

- Pytest: `392 passed in 4.92s`.
- Focused Azure transport/factory/runner/evidence tests: `94 passed in 0.21s`.
- Semgrep: 290 rules over eleven changed Python files, zero findings and zero
  errors.
- Python compilation and `git diff --check`: passed.
- No cloud or provider mutation was performed. The external `guardctl`
  deployment must implement the new attestation fields from its real Azure and
  SSH configuration before any real run can arm; until that live contract is
  proven on two controllers, paid creation remains blocked.

## Fix round 3: immutable heartbeat state and executable deferred finalizer

Status: DONE_WITH_CONCERNS. Commit `b526f2f` addresses the third re-review.

- The guard worker now persists `heartbeat_timeout_seconds` in immutable arm
  state, validates it whenever state is reopened, emits it from that state in
  every receipt, and uses that same persisted value for heartbeat-loss timer
  decisions. Reopening a worker with a different CLI timeout cannot silently
  shorten an existing arm; a re-arm with the changed timeout is rejected.
- `DynamicAzureGuard` retains the authenticated arm receipt immediately after
  arm succeeds. A subsequent identity/preflight failure still blocks the arm
  callback and therefore blocks paid creation, while leaving the channel
  available for status and evidence export during normal finalization.
- The deferred descriptor now retains the validated immutable arm fields,
  pinned endpoint paths, SSH timeout, heartbeat timeout, and pinned host-key
  fingerprint without embedding credential material. Its resume path requires
  exact role, run ID, nonce, label, deadline, heartbeat timeout, manifest
  target, and local pinned-host-key agreement before restoring the channel; it
  never issues a second arm RPC.
- `scripts/finalize_deferred_azure_guards.py` implements the post-deadline
  lifecycle end to end. It refuses early execution, polls each saved channel,
  accepts only three-read post-deadline `ABSENCE_CONFIRMED`, exports the final
  journal, atomically updates the bound manifest and deferred descriptor, and
  rebuilds the evidence pack. Failed original runs remain globally
  evidence-incomplete even after their independently armed channels are fully
  finalized.
- The finalizer accepts only the exact `server` and `worker` role names and an
  exact `run-manifest.json` binding, preventing descriptor-controlled artifact
  path traversal or cross-run evidence completion.

Fix-round verification one-liner:

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q && semgrep scan --config auto --error --json --output /var/tmp/srecon26-task3-fix3-semgrep.json guard/guard_worker.py scripts/run_two_node_metric_path.py scripts/finalize_deferred_azure_guards.py src/srecon26_poc/azure_guard_transport.py src/srecon26_poc/live_factory.py tests/integration/test_guard_idempotency.py tests/unit/test_azure_guard_transport.py tests/unit/test_deferred_azure_finalizer.py tests/unit/test_live_factory.py tests/unit/test_two_node_runner.py && python3 -c 'import json; d=json.load(open("/var/tmp/srecon26-task3-fix3-semgrep.json")); assert not d.get("results") and not d.get("errors")' && git diff --check
```

Observed fix-round results:

- Pytest: `398 passed in 6.91s`.
- Focused guard/transport/factory/runner/finalizer/evidence tests: `123 passed
  in 0.33s`.
- Semgrep: 290 rules over ten changed Python files, zero findings and zero
  errors.
- Python compilation and `git diff --check`: passed.
- No live Vast create, Report action, Azure deployment, SSH connection, or
  cloud mutation was performed. Existing arm state created before the new
  immutable timeout field fails closed and must not be migrated or treated as
  current evidence. The real run remains blocked until two deployed Azure
  controllers return the complete new receipts and pass all independence
  checks.
