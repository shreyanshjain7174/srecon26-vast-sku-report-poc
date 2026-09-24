# Independent Azure guard runbook

This path uses an existing, dedicated Azure VM as a durable external Vast
teardown controller. The lifecycle script is read-only: it does not deploy,
modify, delete, request quota, or accept a Vast credential. A paid Vast create
remains forbidden until the controller is separately deployed, reachable, and
returns a nonce-bound arm receipt.

## Install the reviewed server on an existing controller

The deployable server is `guard/ssh_rpc.py`; its launcher is installed as
`/usr/local/sbin/guardctl`. `guard/install_azure_guard.sh` supports Ubuntu with
Python 3.11 or later (Ubuntu 24.04 supplies a suitable system Python). It does
not provision Azure resources, install packages, modify SSH authorization, or
accept provider credentials. Prepare systemd, OpenSSH with an Ed25519 host key,
and an audited Vast CLI installation under `/opt` or `/usr` first. The CLI,
its interpreter, and its dependencies must be root-owned and not writable by
other users; the service has no access to home directories.

Stage the reviewed `guard` directory under a root-owned private directory such
as `/root/srecon26-reviewed/guard`. All source/configuration ancestors must be
root-owned, free of symlinks, and not writable by group or others. The installer
rejects unsafe existing destination permissions instead of repairing them.
Create a root-owned `0600` `/root/controller.json` containing only these
non-secret fields, using identities from the verified Azure ownership check:

```json
{
  "azure_resource_id": "/subscriptions/<subscription-id>/resourceGroups/<group>/providers/Microsoft.Compute/virtualMachines/<vm>",
  "azure_vm_id": "<vm-uuid>",
  "vast_bin": "/opt/vast-venv/bin/vastai"
}
```

Then, on that existing controller, run:

```sh
sudo bash /root/srecon26-reviewed/guard/install_azure_guard.sh /root/controller.json
```

The installer writes root-owned code, a `0600` metadata file, private `0700`
state/credential directories, and `srecon26-azure-guard.service` plus its timer.
The calendar timer runs at seconds 00, 15, 30, and 45, uses `Persistent=true`
to catch up after downtime, and also triggers shortly after boot. A running
oneshot is never overlapped; slow provider operations can delay subsequent
checks. Its process deadline is 120 seconds and systemd's outer limit is 130
seconds. Inspect timer health separately; a timer cannot provide teardown
while its VM, systemd, network, or provider API is unavailable.

Before each fresh arm and preflight attestation, the gateway checks that the
timer is loaded, enabled, and active. The timer scans durable arm directories,
including older runs, and the worker uses each arm's saved timeout even after
restart. Provider failures use fixed public errors, and systemd discards worker
stdout/stderr. No exception text or provider response is echoed by the gateway.
Installation can precede credential bootstrap; early timer failures are
expected until the credential is installed. Verify a successful timer run
before renting anything.

This checkout provides code and local tests. Installing it or passing local
tests is not evidence that an Azure controller has been deployed or attested.

## Azure ownership preflight

Use a dedicated resource group whose group and every resource carry both
`srecon26-purpose=independent-vast-guard` and `srecon26-dedicated=true`. Attach
the named user-assigned identity to the named controller VM. Then run:

```sh
PYTHONPATH=src:. python3 scripts/azure_guard_lifecycle.py preflight \
  --subscription-id "$AZURE_SUBSCRIPTION_ID" \
  --resource-group srecon26-independent-guard-rg \
  --identity-name srecon26-guard-id \
  --controller-vm srecon26-guard-vm \
  --location centralindia \
  --output azure-guard-preflight.json
```

The script performs only `az group show`, `az identity show`, `az vm show`, and
`az resource list`. It verifies the group boundary, the dedicated tags on every
resource, and the exact user-assigned identity on the controller VM. Azure CLI
authentication comes from the operator's existing login; do not add provider
credentials, cloud-init, deployment parameters, or secret values to this
command or its output.

## Pin the host and restrict the runtime key

Obtain the VM's Ed25519 host-key fingerprint from an authenticated Azure
console or the separately verified image/provisioning record. Compare it over
that independent channel before writing the exact host key to a dedicated
known-hosts file. Never approve a first-seen key from the network and never use
`StrictHostKeyChecking=accept-new` or `no`.

The runtime gateway executes as root; place the runtime SSH public key in the
dedicated controller's root `authorized_keys`, with one entry like:

```text
restrict,command="/usr/local/sbin/guardctl" ssh-ed25519 <runtime-public-key>
```

`guardctl` must reject every `SSH_ORIGINAL_COMMAND` except the literal
`guardctl`, read at most one bounded `srecon26-guard-v1` JSON request from
stdin, dispatch only `preflight`, `arm`, `heartbeat`, `status`, `anchor`, or
`export`, and write exactly one JSON object to stdout. It must never start a
shell. The client additionally disables configuration files, PTY allocation,
agent and port forwarding, local commands, password/interactive
authentication, and host-key TOFU. Both stdout and stderr are read
incrementally; the client terminates SSH as soon as stdout exceeds 64 KiB or
stderr exceeds 4 KiB.

The launcher clears inherited environment variables and uses isolated Python.
Configure the dedicated SSH account to accept only the reviewed forced keys
(for root, `PermitRootLogin forced-commands-only` permits this pattern). The
installer deliberately does not change sshd or add keys. The runtime key
cannot invoke the local-only `tick-all` entry point or the bootstrap operation.
Each RPC accepts at most 16 KiB of UTF-8 JSON, rejects duplicate/unknown fields
and non-finite values, and has a 15-second process deadline. Responses are at
most 64 KiB including JSON escaping and the final newline. Rejections return
one fixed JSON object and a nonzero status with no stderr.

## Install the provider credential manually

Credential installation is a one-time bootstrap operation, not a runtime RPC.
After the host key has been verified as above, add a separate temporary root
bootstrap public key with this forced-command restriction:

```text
restrict,command="/usr/local/sbin/guardctl install-credential" ssh-ed25519 <bootstrap-public-key>
```

Then invoke only that literal command and pass the credential on stdin:

```sh
ssh -F /dev/null -T -o BatchMode=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=/secure/azure-guard-known-hosts \
  -o GlobalKnownHostsFile=/dev/null -o IdentitiesOnly=yes -o ForwardAgent=no \
  -o ClearAllForwardings=yes -i /secure/bootstrap-key \
  root@guard.example.test 'guardctl install-credential' \
  < /secure/vast-api-key
```

The audited installer must write `/etc/srecon26-guard/vast-api-key` as a
root-owned `0600` file without echoing stdin, and must reject any different
`SSH_ORIGINAL_COMMAND`. Remove the bootstrap key and its authorization
immediately afterward. Never put the credential in source, Bicep, cloud-init,
shell arguments, environment dumps, logs, or evidence.

Bootstrap accepts one nonempty printable ASCII token of at most 4096 input
bytes, optionally ending in one newline. It creates the credential exclusively
and refuses symlinks or an existing file, so it cannot rotate a credential
while a run is active. A root operator must handle any later rotation only
after all runs have confirmed absence. The existing Vast adapter passes the
file's contents only through the provider child's minimal private environment;
the launcher discards any inherited `VAST_API_KEY`. Neither systemd units nor
the RPC protocol contain the credential.

## Arm, verify, and collect

Create a fresh 8-128 character nonce and the exact provider label
`<run-label>--nonce-<nonce>`. Send `arm` before any Vast create. Accept the
receipt only when it has all of the following:

- status `ARMED`;
- the exact nonce, label, and immutable UTC hard deadline requested;
- the exact immutable heartbeat timeout requested (1-600 seconds);
- the expected remote host identity and the deployed worker's SHA-256 hash;
- a 64-character lowercase journal root hash.

One controller holds one active binding. It refuses another nonce until every
older run is `ABSENCE_CONFIRMED` or `DISARMED`; ownership errors and incomplete
arms do not unlock replacement. Repeating an identical arm does not refresh
its heartbeat or deadline. If a crash separates the durable worker arm from
the binding write, the timer retains that watcher, and a new arm is refused
until it resolves. Missing a heartbeat timeout cannot be undone by a late
heartbeat even if the next timer invocation has not run yet.

An attestation reports `ARMED` while the worker is safely waiting for a create
to appear, and includes `worker_status=AWAITING_INSTANCE` in that case. It
never attests `ARMED` after teardown authority, heartbeat expiry, or deadline.

Call `preflight` after arming so `GuardClient` can construct and validate the
attestation. The transport rejects a preflight receipt unless it restates the
exact nonce, label, and immutable deadline from its validated `ARMED` receipt.
Reuse that armed transport for the rest of the run: heartbeat, status, and
anchor receipts must each restate the cached arm nonce, label, and immutable
deadline; an echo of later caller-supplied fields is not sufficient. Send
bounded heartbeats during the run. Anchor the local evidence root, then collect
`status` receipts.

After the final `status` response, invoke the bounded export RPC with that
receipt's nonce and journal root:

```python
exported = transport.export_evidence(
    nonce=status_receipt["nonce"],
    root_hash=status_receipt["root_hash"],
)
with open("azure-guard-journal.jsonl", "x", encoding="utf-8") as output:
    output.write(exported.journal)
```

The forced `guardctl` command must answer `export` with one JSON object whose
status is `EVIDENCE_EXPORTED` and whose `nonce` and `root_hash` exactly echo the
request. It returns the complete journal as `journal`, the literal encoding
`utf-8-jsonl`, and `journal_sha256`. The client streams the enclosing response
under the 64 KiB cap and verifies SHA-256 before returning
`GuardEvidenceExport`; a larger journal must be collected through a separately
approved bounded artifact channel and is not accepted by this RPC. Preserve
the JSONL file and its SHA-256 alongside the final receipt. Redact
infrastructure details as needed, but retain timestamps, worker hash, receipt
roots, exact instance ID and label, and provider observation outcomes. Never
include the credential or private SSH keys.

Export verifies the hash chain under the worker lock and requires the supplied
root to match its current journal root. A concurrent tick can make a previously
read root stale; reread status and export that new root. Never replace a stale
root with an unverified one. Explicit nonce status/export operations can still
collect a completed older run after a new binding has been armed.

## Cleanup gate

A destroy request, missing instance response, or one empty inventory read is
not cleanup proof. Keep the Azure VM, identity, resource group, runtime key,
timer, and journal intact through errors and `ABSENCE_PENDING`. Azure cleanup
is allowed only after the durable controller returns `ABSENCE_CONFIRMED` with
three distinct successful post-teardown provider reads showing no match for
both the exact numeric instance ID and exact nonce-bound label. Export and
verify the final journal as described above before manually deleting the
dedicated Azure resource group. If ownership changes, inventory is unknown,
any read fails, the three timestamps repeat or arrive out of order, or export
hash verification fails, retain the controller and escalate; do not disarm or
delete it.
