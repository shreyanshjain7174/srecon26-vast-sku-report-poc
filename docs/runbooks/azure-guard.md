# Independent Azure guard runbook

This path uses an existing, dedicated Azure VM as a durable external Vast
teardown controller. The lifecycle script is read-only: it does not deploy,
modify, delete, request quota, or accept a Vast credential. A paid Vast create
remains forbidden until the controller is separately deployed, reachable, and
returns a nonce-bound arm receipt.

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

The runtime SSH public key must have a single `authorized_keys` entry like:

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

## Arm, verify, and collect

Create a fresh 8-128 character nonce and the exact provider label
`<run-label>--nonce-<nonce>`. Send `arm` before any Vast create. Accept the
receipt only when it has all of the following:

- status `ARMED`;
- the exact nonce, label, and immutable UTC hard deadline requested;
- the expected remote host identity and the deployed worker's SHA-256 hash;
- a 64-character lowercase journal root hash.

Call `preflight` after arming so `GuardClient` can construct and validate the
attestation. The transport rejects a preflight receipt unless it restates the
exact nonce, label, and immutable deadline from its validated `ARMED` receipt.
Send bounded heartbeats during the run. Anchor the local evidence root, then
collect `status` receipts.

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
