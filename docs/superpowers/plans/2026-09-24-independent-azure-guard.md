# Independent Azure Guard Plan

## Spec

Run a real, two-node Vast Kubernetes/vLLM metric-path experiment only after an
independent controller can prove it owns nonce-bound teardown. The controller
must survive loss of the local runner, refuse ambiguous ownership, and retain
evidence until teardown has been proven by three successful provider reads.

## Global constraints

- Never put Vast credentials in source, Bicep parameters, cloud-init text,
  command arguments, logs, commits, or evidence.
- Arm before any Vast create. The receipt must bind nonce, exact label,
  immutable deadline, remote host identity, worker hash, and journal root.
- A destroy request is not proof. Only three timestamped provider reads with
  zero exact-ID and exact-label matches permit disarm or controller cleanup.
- A changed label, multiple matching instances, unknown API response, or
  failed provider read must refuse teardown/disarm and retain the guard.
- SSH transport permits fixed JSON RPC verbs only, with a forced command, no
  shell, PTY, port forwarding, agent forwarding, or host-key TOFU.
- Tests run with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`; run Semgrep on changed
  Python and shell code. Commit with `git commit -s`; do not touch user-owned
  `.gitignore` or `node_modules/` changes.

## Task 1: Harden absence confirmation in durable guard worker

**Files:** `guard/guard_worker.py`, relevant unit tests under `tests/`.

Replace the single absence observation terminal behavior with durable,
timestamped three-read quorum state. Quorum requires distinct successful
provider observations after the destroy request or missed-deadline handling,
with no match for both exact numeric instance ID and nonce-bound label. API
exceptions/unknown inventory reset or refuse the quorum. `disarm` may only
accept `ABSENCE_CONFIRMED`. Preserve exact-ID ownership checks and bounded
retry semantics. Add focused tests for three reads, API failure, and changed
or duplicate label refusal.

## Task 2: Add bounded Azure SSH guard transport and lifecycle preflight

**Files:** `src/srecon26_poc/azure_guard_transport.py`,
`scripts/azure_guard_lifecycle.py`, relevant tests, and
`docs/runbooks/azure-guard.md`.

Implement a transport compatible with `GuardClient` using a known-hosts file
and a fixed `guardctl` forced command. Each RPC is one validated JSON request
on stdin and one validated JSON response on stdout. No arbitrary remote
command construction. The lifecycle preflight must validate a dedicated Azure
resource group and controller identity without accepting credentials via CLI
arguments or deployment parameters. Document manual credential installation
through SSH stdin after host-key verification, arm receipt checks, collection,
and deletion only after `ABSENCE_CONFIRMED`. Tests must demonstrate command
construction/response validation and secret-free Azure argument construction.

## Task 3: Wire live two-node runner to Azure guard, then gated real run

**Files:** `src/srecon26_poc/live_factory.py`,
`scripts/run_two_node_metric_path.py`, relevant tests and evidence scripts.

Add an explicit Azure guard configuration path; preserve GitHub guard behavior.
Refuse a live run until both independent guards have bound arm receipts. Once
Azure capacity is confirmed and both guards pass, execute at most one
two-node attempt, gather raw Kubernetes/vLLM/HPA outputs plus billing and
three-read absence proof, then rebuild evidence pack. Report a Vast SKU only
when independently proven provider-contract fault exists and the exact website
Report action can be performed before normal teardown.

## Task 4: Deployable forced-command Azure guard server

**Files:** `guard/ssh_rpc.py`, `guard/install_azure_guard.sh`, service/timer
templates, focused tests, and `docs/runbooks/azure-guard.md`.

Implement server side required by Task 2 client: root-owned `guardctl`
forced-command JSON RPC gateway with allowlisted protocol, strict request
schemas, one-response stdout contract, bounded input, and no credential output.
Connect arm/preflight/heartbeat/status/anchor/export to `GuardWorker`, install
root-owned 0600 credential only through separate literal bootstrap command,
and run persistent systemd timer every 15 seconds. Include portable Ubuntu
installer refusing unsafe permissions. Test command rejection, request
validation, credential isolation, and export hash binding.
