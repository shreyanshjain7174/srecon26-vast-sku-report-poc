# Task 4 implementation report

Status: implemented; focused local validation passed. Actual Ubuntu installation
and systemd runtime validation remain unverified because local Docker could not
start the disposable test container. No Azure operation, SSH connection,
provider API call, or controller installation was performed.

Implementation commit: `221904616360454225649b2b94217be4cf3410bb`
(`feat: add isolated Azure guard RPC server and installer`). The commit uses
`git commit -s`, contains the configured user's Signed-off-by trailer, and has
no coauthor trailer. Work stays on the parent's existing feature branch
`feat/live-canary-factory`; unrelated dirty paths were preserved.

## Delivered

- `guard/ssh_rpc.py`: literal `guardctl` forced command, exact protocol/envelope
  and per-command schemas, duplicate-key/non-finite/type/nonce/label rejection,
  16 KiB input cap, 64 KiB encoded-response cap, 15-second RPC deadline, fixed
  errors, and one JSON stdout object without exception/provider diagnostics.
- Arm/preflight/heartbeat/status/anchor/export use `GuardWorker`; one controller
  has one persisted active binding. Replacement refuses any prior run without
  `ABSENCE_CONFIRMED` or `DISARMED`. Repeated arms cannot reset heartbeat,
  deadline, timeout, label, or run identity. Late heartbeat/preflight cannot
  revive a timed-out arm before the timer fires.
- Preflight reports an armed attestation while a healthy worker is awaiting
  instance visibility, retaining its real status in `worker_status`. Timer
  loaded/enabled/active state is checked before arm and preflight.
- Export checks the existing hash chain and requested current root under the
  worker lock; output contains exact JSONL, its SHA-256, nonce/root binding,
  and the expected encoding. Stale roots, tampering, and oversized encoded
  responses fail closed. Completed older runs remain collectible by nonce.
- Separate literal `guardctl install-credential` bootstrap requires root,
  consumes one bounded token on stdin, and exclusively creates a root-owned
  `0600` file in a private `0700` directory. Existing files and symlinks are
  refused. There is no credential RPC or runtime overwrite path. The existing
  provider adapter's minimal child-only environment remains unchanged, as
  explicitly confirmed by the parent; no credential enters argv, configuration,
  journals, exceptions, or RPC output.
- `guardctl.template` clears inherited environment and invokes isolated Python.
  Local-only `tick-all` rejects all SSH original commands, ticks all persisted
  runs, and isolates per-run failure. The worker reads its immutable stored
  heartbeat timeout after restart.
- Ubuntu installer, service, and calendar timer templates: root/non-writable
  staging and ancestor checks, no symlink targets, no silent repair of unsafe
  directories, root-owned private metadata/state, every-15-second persistent
  calendar schedule, boot trigger, hardened root service, private umask, and
  bounded process/service execution. Installer accepts only non-secret
  controller metadata and an already-audited root-owned Vast CLI path.
- Expanded runbook covers Ubuntu/Python prerequisites, root forced-key setup,
  bootstrap, immutable binding, timer behavior, export races, cleanup gates,
  and the distinction between tested code and a deployed controller.

## Validation

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=src:. python3 -m pytest \
  tests/unit/test_azure_guard_rpc.py tests/unit/test_azure_guard_transport.py \
  tests/unit/test_guard.py tests/integration/test_guard_idempotency.py \
  tests/integration/test_guard_deadline.py \
  tests/integration/test_azure_guard_installer.py -q
```

Result: **150 passed, 1 skipped**. The skip is the explicitly opt-in disposable
Ubuntu/root integration test. Tests cover all successful RPC receipts against
the real client validator, malicious commands and schemas, fixed output,
credential isolation/ownership/mode checks, hash binding and tampering, timeout
immutability across restart, missed-heartbeat behavior, prior-arm exclusion,
and enabled/active timer gating. New RPC test file: 75 passing test cases.

```sh
semgrep scan --config .semgrep.yml --config p/python --config p/security-audit \
  --error guard/ssh_rpc.py guard/install_azure_guard.sh \
  tests/unit/test_azure_guard_rpc.py tests/integration/test_azure_guard_installer.py
bash -n guard/install_azure_guard.sh
sh -n guard/guardctl.template
git diff --check
```

Result: Semgrep ran **202 applicable rules over 4 targets, 0 findings**; shell
syntax and whitespace checks passed.

## Remaining validation and operational constraints

The Ubuntu 24.04 image was locally available. An initial network-disabled empty
container ran and showed that Python/systemd were absent. The disposable test
container intended to install test-only packages never reached a running
process: Docker reported `Created`, `Pid: 0`, and no error. A minimal
network-disabled retry also stalled. Both exact task-owned containers were
removed; their stuck local Docker clients were terminated; a final filtered
inventory showed no remaining task containers. No unrelated container was
changed. The package installation and integration test did not execute.

`tests/integration/test_azure_guard_installer.py` is retained for an environment
where a disposable Ubuntu container starts successfully. It requires explicit
`SRECON26_TEST_UBUNTU_INSTALLER=1`, root, `/.dockerenv`, Python/pytest, systemd,
OpenSSH client, and a clean guard installation. It exercises actual installed
launchers, root-only credential access, request/response/export behavior,
installer refusal, and real systemd unit/calendar parsers. It stubs only
systemctl activation because systemd is not PID 1 in an ordinary container;
actual timer firing and reboot persistence still require a disposable VM.

Ubuntu 24.04/Python 3.12 compatibility is supported by syntax and the selected
standard-library APIs, but was not runtime-proven here. The worker imports
`datetime.UTC`, so the installer intentionally rejects Python below 3.11.
Slow provider operations can exceed a 15-second timer interval; oneshots do not
overlap. Process/service limits are 120/130 seconds. Loss of the controller or
provider connectivity cannot be represented as guaranteed billing safety.
